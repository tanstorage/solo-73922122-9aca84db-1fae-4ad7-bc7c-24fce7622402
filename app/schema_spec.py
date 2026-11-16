"""受限 JSON Schema 子集的解析、校验与纯 Python 报文判定。

允许的语法（其余一律拒绝）：
- 平面对象：{"type": "object", "properties": {...}, "required": [...], "additionalProperties": false}
- 顶层 anyOf：{"anyOf": [<平面对象>, ...]}
- 整数上下界：{"type": "integer", "minimum": m, "maximum": M}（上下界均可省略）
- 字符串/整数/空值枚举：{"enum": [...]}（可配 "type" 进一步限定）
- 空值类型：{"type": "null"}
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

MAX_BRANCHES = 16
MAX_PROPERTIES = 64
MAX_ENUM_VALUES = 256
MAX_STRING_LEN = 128
INT_BOUND = 2**53


class SchemaError(ValueError):
    """报文格式不符合受限语法。"""


@dataclass
class FieldSpec:
    allow_null: bool = False
    int_kind: str = "none"  # none | range | set
    int_lo: Optional[int] = None
    int_hi: Optional[int] = None
    int_set: Optional[frozenset] = None
    str_set: Optional[frozenset] = None


@dataclass
class Branch:
    properties: dict = field(default_factory=dict)  # name -> FieldSpec
    required: frozenset = frozenset()


@dataclass
class Schema:
    branches: tuple = ()  # tuple[Branch, ...]，顶层 anyOf 的各分支；单对象时为 1 个分支


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _check_int(v, where: str) -> None:
    if not _is_int(v):
        raise SchemaError(f"{where}: 必须是整数（不接受布尔/浮点/字符串）")
    if abs(v) > INT_BOUND:
        raise SchemaError(f"{where}: 整数绝对值不能超过 {INT_BOUND}")


def parse_field(name: str, doc, where: str) -> FieldSpec:
    if not isinstance(doc, dict):
        raise SchemaError(f"{where}: 字段定义必须是 JSON 对象")
    extra = set(doc) - {"type", "enum", "minimum", "maximum"}
    if extra:
        raise SchemaError(f"{where}: 不支持的语法关键字 {sorted(extra)}")
    typ = doc.get("type")
    if typ is not None and typ not in ("integer", "string", "null"):
        raise SchemaError(f"{where}: 不支持的 type {typ!r}（仅允许 integer / string / null）")

    has_min = "minimum" in doc
    has_max = "maximum" in doc
    enum = doc.get("enum")

    if has_min or has_max:
        if typ != "integer":
            raise SchemaError(f"{where}: minimum/maximum 只能用于 integer 类型")
        if enum is not None:
            raise SchemaError(f"{where}: enum 与 minimum/maximum 不能同时使用")
        lo = doc.get("minimum") if has_min else None
        hi = doc.get("maximum") if has_max else None
        if has_min:
            _check_int(lo, f"{where}.minimum")
        if has_max:
            _check_int(hi, f"{where}.maximum")
        if has_min and has_max and lo > hi:
            raise SchemaError(f"{where}: minimum({lo}) 大于 maximum({hi})")
        return FieldSpec(int_kind="range", int_lo=lo, int_hi=hi)

    if enum is not None:
        if not isinstance(enum, list) or not enum:
            raise SchemaError(f"{where}: enum 必须是非空数组")
        if len(enum) > MAX_ENUM_VALUES:
            raise SchemaError(f"{where}: enum 取值个数超过 {MAX_ENUM_VALUES}")
        allow_null, ints, strs = False, set(), set()
        for v in enum:
            if v is None:
                allow_null = True
            elif _is_int(v):
                _check_int(v, f"{where}.enum")
                ints.add(v)
            elif isinstance(v, str):
                if len(v) > MAX_STRING_LEN:
                    raise SchemaError(f"{where}: 字符串枚举值长度超过 {MAX_STRING_LEN}")
                strs.add(v)
            else:
                raise SchemaError(f"{where}: 枚举值只允许 字符串/整数/null，收到 {v!r}")
        if typ == "integer" and (allow_null or strs):
            raise SchemaError(f"{where}: type=integer 与枚举值类型不一致")
        if typ == "string" and (allow_null or ints):
            raise SchemaError(f"{where}: type=string 与枚举值类型不一致")
        if typ == "null" and (ints or strs):
            raise SchemaError(f"{where}: type=null 与枚举值类型不一致")
        return FieldSpec(
            allow_null=allow_null,
            int_kind="set" if ints else "none",
            int_set=frozenset(ints) if ints else None,
            str_set=frozenset(strs) if strs else None,
        )

    if typ == "integer":
        return FieldSpec(int_kind="range")  # 无上下界的整数
    if typ == "string":
        raise SchemaError(f"{where}: string 类型必须用 enum 列举允许值")
    if typ == "null":
        return FieldSpec(allow_null=True)
    raise SchemaError(f"{where}: 字段必须给出 type 或 enum")


def parse_branch(doc, where: str) -> Branch:
    if not isinstance(doc, dict):
        raise SchemaError(f"{where}: 必须是 JSON 对象")
    extra = set(doc) - {"type", "properties", "required", "additionalProperties"}
    if extra:
        raise SchemaError(f"{where}: 不支持的关键字 {sorted(extra)}（仅允许平面对象）")
    if doc.get("type") != "object":
        raise SchemaError(f'{where}: type 必须为 "object"')
    if doc.get("additionalProperties") is not False:
        raise SchemaError(f"{where}: 必须显式声明 \"additionalProperties\": false")
    props = doc.get("properties")
    if not isinstance(props, dict):
        raise SchemaError(f"{where}: 缺少 properties 对象")
    if len(props) > MAX_PROPERTIES:
        raise SchemaError(f"{where}: 字段个数超过 {MAX_PROPERTIES}")
    required = doc.get("required", [])
    if not isinstance(required, list) or not all(isinstance(r, str) for r in required):
        raise SchemaError(f"{where}: required 必须是字符串数组")
    req = frozenset(required)
    unknown = req - set(props)
    if unknown:
        raise SchemaError(f"{where}: required 引用了未声明字段 {sorted(unknown)}")
    fields = {}
    for fname, fdoc in props.items():
        if not fname:
            raise SchemaError(f"{where}: 字段名不能为空")
        fields[fname] = parse_field(fname, fdoc, f"{where}.{fname}")
    return Branch(properties=fields, required=req)


def parse_schema(doc) -> Schema:
    """把受限 JSON Schema 文档解析为 Schema；不符合子集语法时抛 SchemaError。"""
    if not isinstance(doc, dict):
        raise SchemaError("顶层必须是 JSON 对象")
    if "anyOf" in doc:
        extra = set(doc) - {"anyOf"}
        if extra:
            raise SchemaError(f"anyOf 只能单独出现在顶层，不允许与其他关键字混用: {sorted(extra)}")
        alts = doc["anyOf"]
        if not isinstance(alts, list) or not alts:
            raise SchemaError("anyOf 必须是非空数组")
        if len(alts) > MAX_BRANCHES:
            raise SchemaError(f"anyOf 分支个数超过 {MAX_BRANCHES}")
        return Schema(branches=tuple(parse_branch(b, f"anyOf[{i}]") for i, b in enumerate(alts)))
    return Schema(branches=(parse_branch(doc, "顶层"),))


# ---------- 纯 Python 报文判定（用于复核 z3 给出的反例） ----------

def message_valid(msg, schema: Schema) -> bool:
    if not isinstance(msg, dict):
        return False
    return any(_branch_valid(msg, b) for b in schema.branches)


def _branch_valid(msg: dict, b: Branch) -> bool:
    for k in msg:
        if k not in b.properties:  # additionalProperties: false
            return False
    if not b.required <= set(msg):
        return False
    return all(_field_valid(v, b.properties[k]) for k, v in msg.items())


def _field_valid(v, spec: FieldSpec) -> bool:
    if v is None:
        return spec.allow_null
    if isinstance(v, bool):
        return False
    if isinstance(v, int):
        if spec.int_kind == "range":
            if spec.int_lo is not None and v < spec.int_lo:
                return False
            if spec.int_hi is not None and v > spec.int_hi:
                return False
            return True
        if spec.int_kind == "set":
            return v in spec.int_set
        return False
    if isinstance(v, str):
        return spec.str_set is not None and v in spec.str_set
    return False
