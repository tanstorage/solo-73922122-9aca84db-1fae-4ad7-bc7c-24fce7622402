"""用 z3 全量判定：某固件写入格式允许的全部报文是否都被某程序读取格式接受。

判定 L(write) ⊆ L(read)：向求解器查询「满足 write 但不满足 read」的报文。
- UNSAT  => 不存在反例，兼容（这是对全部报文的证明，不是抽样）。
- SAT    => 取模型还原出一个合法反例报文（满足 write、被 read 拒收）。

报文编码：对每个字段建立 4 个变量 —— 是否出现 present、值类型 kind
(0=null, 1=integer, 2=string)、整数值 ival、字符串值在字段字符串表中的下标 sval。
"""
from __future__ import annotations

from z3 import And, Bool, Implies, Int, Not, Or, Solver, is_true, sat

from schema_spec import Schema, message_valid

KIND_NULL, KIND_INT, KIND_STR = 0, 1, 2
SOLVER_TIMEOUT_MS = 15000


class _Encoder:
    def __init__(self, fields, str_tables):
        self.fields = fields
        self.str_tables = str_tables  # field -> sorted list[str]
        self.present, self.kind, self.ival, self.sval = {}, {}, {}, {}
        for i, f in enumerate(fields):
            self.present[f] = Bool(f"p{i}")
            self.kind[f] = Int(f"k{i}")
            self.ival[f] = Int(f"v{i}")
            self.sval[f] = Int(f"s{i}")

    def wellformed(self):
        cons = []
        for f in self.fields:
            p, k, v, s = self.present[f], self.kind[f], self.ival[f], self.sval[f]
            tbl = self.str_tables.get(f) or []
            cons.append(Or(k == KIND_NULL, k == KIND_INT, k == KIND_STR))
            # 缺省字段取确定默认值，保证反例报文干净
            cons.append(Implies(Not(p), And(k == KIND_NULL, v == 0, s == 0)))
            cons.append(And(s >= 0, s < len(tbl)) if tbl else (s == 0))
        return cons

    def field_ok(self, f, spec):
        k, v, s = self.kind[f], self.ival[f], self.sval[f]
        opts = []
        if spec.allow_null:
            opts.append(k == KIND_NULL)
        if spec.int_kind == "range":
            c = [k == KIND_INT]
            if spec.int_lo is not None:
                c.append(v >= spec.int_lo)
            if spec.int_hi is not None:
                c.append(v <= spec.int_hi)
            opts.append(And(*c))
        elif spec.int_kind == "set":
            opts.append(And(k == KIND_INT, Or(*[v == x for x in sorted(spec.int_set)])))
        if spec.str_set:
            tbl = self.str_tables[f]
            opts.append(And(k == KIND_STR, Or(*[s == tbl.index(x) for x in sorted(spec.str_set)])))
        return Or(*opts)

    def branch_ok(self, branch):
        cons = []
        props = branch.properties
        for f in self.fields:
            if f in props:
                c = self.field_ok(f, props[f])
                if f in branch.required:
                    cons.append(And(self.present[f], c))
                else:
                    cons.append(Implies(self.present[f], c))
            else:
                # additionalProperties: false —— 未声明字段不得出现
                cons.append(Not(self.present[f]))
        return And(*cons)

    def schema_ok(self, schema: Schema):
        return Or(*[self.branch_ok(b) for b in schema.branches])


def find_counterexample(write: Schema, read: Schema):
    """若存在被 read 拒收的 write 合法报文，返回该报文 dict；否则返回 None。"""
    names = set()
    for sch in (write, read):
        for b in sch.branches:
            names |= set(b.properties)
    fields = sorted(names)
    str_tables = {}
    for f in fields:
        strs = set()
        for sch in (write, read):
            for b in sch.branches:
                spec = b.properties.get(f)
                if spec and spec.str_set:
                    strs |= set(spec.str_set)
        if strs:
            str_tables[f] = sorted(strs)

    enc = _Encoder(fields, str_tables)
    s = Solver()
    s.set("timeout", SOLVER_TIMEOUT_MS)
    s.add(enc.wellformed())
    s.add(enc.schema_ok(write))
    s.add(Not(enc.schema_ok(read)))
    r = s.check()
    if r != sat:
        if str(r) == "unknown":
            raise RuntimeError("z3 求解超时/未知，无法完成全量判定")
        return None

    m = s.model()
    msg = {}
    for f in fields:
        if is_true(m.eval(enc.present[f], model_completion=True)):
            k = m.eval(enc.kind[f], model_completion=True).as_long()
            if k == KIND_NULL:
                msg[f] = None
            elif k == KIND_INT:
                msg[f] = m.eval(enc.ival[f], model_completion=True).as_long()
            else:
                msg[f] = str_tables[f][m.eval(enc.sval[f], model_completion=True).as_long()]
    # 复核：反例必须确实满足写入格式、且确实被读取格式拒收
    if not message_valid(msg, write) or message_valid(msg, read):
        raise RuntimeError(f"内部错误：z3 反例复核失败 {msg!r}")
    return msg


def check_compatibility(fw_pairs, prog_pairs):
    """全量交叉检查：每种固件写入格式 × 每个现用程序。

    fw_pairs: [(version, Schema)]，prog_pairs: [(program_id, Schema)]
    返回问题列表 [{firmware_version, program_id, counterexample}]，空列表表示全部兼容。
    """
    problems = []
    for fv, fw in fw_pairs:
        for pid, prog in prog_pairs:
            cex = find_counterexample(fw, prog)
            if cex is not None:
                problems.append({
                    "firmware_version": fv,
                    "program_id": pid,
                    "counterexample": cex,
                })
    return problems
