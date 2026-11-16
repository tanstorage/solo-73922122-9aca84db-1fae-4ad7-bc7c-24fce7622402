"""发布事务逻辑：校验 -> 加锁 -> 全量兼容检查 -> 落库，失败整体回滚。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import select, update

from db import (DeviceModel, FirmwareFormat, ProgramFormat, PublishLog,
                SessionLocal)
from schema_spec import SchemaError, parse_schema
from solver import check_compatibility

MAX_FORMATS = 8  # 每型号固件格式、程序格式各至多 8 份
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


@dataclass
class PublishOutcome:
    ok: bool
    message: str
    problems: list = field(default_factory=list)
    version: Optional[int] = None


def valid_name(name: str) -> bool:
    return bool(NAME_RE.match(name or ""))


def _lock_model(s, model: DeviceModel):
    """按型号串行化发布：PostgreSQL 用 SELECT ... FOR UPDATE 行锁；
    SQLite 用一次空 UPDATE 提前获取写锁（配合 busy_timeout 等待）。
    保证并发升级不会各自通过后留下不兼容组合。"""
    if s.bind.dialect.name == "postgresql":
        s.execute(select(DeviceModel.id).where(DeviceModel.id == model.id)
                  .with_for_update()).one()
    else:
        s.execute(update(DeviceModel).where(DeviceModel.id == model.id)
                  .values(name=model.name))


def _log(model_id: int, action: str, program_id, doc, result: str, detail):
    with SessionLocal() as s:
        s.add(PublishLog(model_id=model_id, action=action, program_id=program_id,
                         doc=doc, result=result, detail=detail))
        s.commit()


def _load_state(s, model_id: int):
    firmwares = s.execute(
        select(FirmwareFormat).where(FirmwareFormat.model_id == model_id)
        .order_by(FirmwareFormat.version)).scalars().all()
    programs = s.execute(
        select(ProgramFormat).where(ProgramFormat.model_id == model_id)
        .order_by(ProgramFormat.program_id)).scalars().all()
    return firmwares, programs


def publish_firmware(model_id: int, raw_doc) -> PublishOutcome:
    """追加一份固件写入格式（只追加）。"""
    try:
        schema = parse_schema(raw_doc)
    except SchemaError as e:
        _log(model_id, "firmware", None, raw_doc, "invalid", {"error": str(e)})
        return PublishOutcome(False, f"格式不符合受限语法：{e}")

    with SessionLocal() as s:
        model = s.get(DeviceModel, model_id)
        if model is None:
            return PublishOutcome(False, "型号不存在")
        _lock_model(s, model)
        firmwares, programs = _load_state(s, model_id)
        if len(firmwares) >= MAX_FORMATS:
            s.rollback()
            msg = f"固件格式已达 {MAX_FORMATS} 份上限，无法继续追加"
            _log(model_id, "firmware", None, raw_doc, "invalid", {"error": msg})
            return PublishOutcome(False, msg)

        new_version = (firmwares[-1].version if firmwares else 0) + 1
        fw_pairs = [(f.version, parse_schema(f.doc)) for f in firmwares]
        fw_pairs.append((new_version, schema))
        prog_pairs = [(p.program_id, parse_schema(p.doc)) for p in programs]

        problems = check_compatibility(fw_pairs, prog_pairs)
        if problems:
            s.rollback()  # 失败不改已发布格式
            _log(model_id, "firmware", None, raw_doc, "rejected", {"problems": problems})
            return PublishOutcome(False, "存在不兼容组合，已拒绝发布（已发布格式未变）", problems)

        s.add(FirmwareFormat(model_id=model_id, version=new_version, doc=raw_doc))
        s.add(PublishLog(model_id=model_id, action="firmware", program_id=None,
                         doc=raw_doc, result="published",
                         detail={"version": new_version,
                                 "checked_pairs": len(fw_pairs) * len(prog_pairs)}))
        s.commit()
        return PublishOutcome(True, f"固件写入格式 v{new_version} 已发布", version=new_version)


def publish_program(model_id: int, program_id: str, raw_doc) -> PublishOutcome:
    """按程序号发布/替换程序读取格式。"""
    if not valid_name(program_id):
        return PublishOutcome(False, "程序号只能由字母数字及 _ . - 组成，长度 1~64")
    try:
        schema = parse_schema(raw_doc)
    except SchemaError as e:
        _log(model_id, "program", program_id, raw_doc, "invalid", {"error": str(e)})
        return PublishOutcome(False, f"格式不符合受限语法：{e}")

    with SessionLocal() as s:
        model = s.get(DeviceModel, model_id)
        if model is None:
            return PublishOutcome(False, "型号不存在")
        _lock_model(s, model)
        firmwares, programs = _load_state(s, model_id)
        existing = {p.program_id: p for p in programs}
        if program_id not in existing and len(programs) >= MAX_FORMATS:
            s.rollback()
            msg = f"程序格式已达 {MAX_FORMATS} 个上限，新程序号无法登记"
            _log(model_id, "program", program_id, raw_doc, "invalid", {"error": msg})
            return PublishOutcome(False, msg)

        fw_pairs = [(f.version, parse_schema(f.doc)) for f in firmwares]
        prog_pairs = [(p.program_id, parse_schema(p.doc)) for p in programs
                      if p.program_id != program_id]
        prog_pairs.append((program_id, schema))

        problems = check_compatibility(fw_pairs, prog_pairs)
        if problems:
            s.rollback()  # 失败不改已发布格式
            _log(model_id, "program", program_id, raw_doc, "rejected", {"problems": problems})
            return PublishOutcome(False, "存在不兼容组合，已拒绝发布（已发布格式未变）", problems)

        if program_id in existing:
            existing[program_id].doc = raw_doc
        else:
            s.add(ProgramFormat(model_id=model_id, program_id=program_id, doc=raw_doc))
        s.add(PublishLog(model_id=model_id, action="program", program_id=program_id,
                         doc=raw_doc, result="published",
                         detail={"checked_pairs": len(fw_pairs) * len(prog_pairs)}))
        s.commit()
        return PublishOutcome(True, f"程序「{program_id}」读取格式已发布")
