"""发布事务逻辑：快照 -> 锁外全量兼容检查 -> 加锁复核 -> 落库，失败整体回滚。

并发设计（关键）：
- 兼容审查（z3 全量判定，耗时较长）在 **不持有数据库锁** 的情况下进行，
  不同型号的审查可在 review.py 的独立工作进程中真正并行；
- 审查通过后才开启短事务：锁定型号行 -> 重新读取状态 -> 复核审查所用快照
  与当前已提交状态一致，才落库；若期间有同型号并发提交，则放弃本次结论并重试；
- 同型号并发升级因此仍被严格串行化（行锁 + 快照复核 + 有限重试），
  不会各自通过却留下不兼容组合；
- 审查失败/被拒绝/快照过期都不会写入任何固件/程序格式，已发布格式不变。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import select, update

from db import (DeviceModel, FirmwareFormat, ProgramFormat, PublishLog,
                SessionLocal)
from review import ReviewError, run_review
from schema_spec import SchemaError, parse_schema

MAX_FORMATS = 8  # 每型号固件格式、程序格式各至多 8 份
MAX_REVIEW_RETRIES = 5  # 同型号并发导致快照过期时的最大重试次数
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

# 测试开关：让下一次兼容审查的工作进程模拟原生层崩溃 / 注入延迟。
# 生产请求路径没有任何地方会置位它们，仅 tests/test_parallel.py 使用。
crash_inject_next_review = False
test_review_delay_s = 0.0


@dataclass
class PublishOutcome:
    ok: bool
    message: str
    problems: list = field(default_factory=list)
    version: Optional[int] = None
    review_failed: bool = False  # 审查进程崩溃/超时等未能完成的情形（区别于被拒绝）
    conflict: bool = False       # 同型号并发冲突、重试耗尽（可重新提交）


def valid_name(name: str) -> bool:
    return bool(NAME_RE.match(name or ""))


def _run_review(fw_pairs, prog_pairs):
    global crash_inject_next_review
    inject = crash_inject_next_review
    crash_inject_next_review = False  # 一次性开关
    # 延迟可由进程内开关或环境变量注入（仅测试用；生产默认 0，不产生任何延迟）
    delay = test_review_delay_s
    if delay == 0.0:
        try:
            delay = float(os.environ.get("REVIEW_DELAY_S", "0") or 0)
        except ValueError:
            delay = 0.0
    return run_review(fw_pairs, prog_pairs,
                      crash_inject=inject, delay_s=delay)


def _lock_model(s, model: DeviceModel):
    """按型号串行化发布落库：PostgreSQL 用 SELECT ... FOR UPDATE 行锁；
    SQLite 用一次空 UPDATE 提前获取写锁（配合 busy_timeout 等待）。
    锁只在审查结束后的短提交事务内持有，不跨 z3 求解。"""
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


def _snapshot(model_id: int):
    """只读快照（不加锁、不进入写事务），供锁外审查使用。

    返回 (firmwares, programs)：分别为 [(version, doc)] / [(program_id, doc)]；
    型号不存在时返回 None。
    """
    with SessionLocal() as s:
        if s.get(DeviceModel, model_id) is None:
            return None
        firmwares, programs = _load_state(s, model_id)
        return ([(f.version, f.doc) for f in firmwares],
                [(p.program_id, p.doc) for p in programs])


def _snapshot_unchanged(snap, firmwares, programs) -> bool:
    """复核审查所用快照与加锁后读到的已提交状态完全一致。"""
    fw_now = [(f.version, f.doc) for f in firmwares]
    pg_now = [(p.program_id, p.doc) for p in programs]
    return snap[0] == fw_now and snap[1] == pg_now


def publish_firmware(model_id: int, raw_doc) -> PublishOutcome:
    """追加一份固件写入格式（只追加）。"""
    try:
        schema = parse_schema(raw_doc)
    except SchemaError as e:
        _log(model_id, "firmware", None, raw_doc, "invalid", {"error": str(e)})
        return PublishOutcome(False, f"格式不符合受限语法：{e}")

    for _ in range(MAX_REVIEW_RETRIES):
        snap = _snapshot(model_id)
        if snap is None:
            return PublishOutcome(False, "型号不存在")
        snap_fw, snap_pg = snap
        if len(snap_fw) >= MAX_FORMATS:
            msg = f"固件格式已达 {MAX_FORMATS} 份上限，无法继续追加"
            _log(model_id, "firmware", None, raw_doc, "invalid", {"error": msg})
            return PublishOutcome(False, msg)

        new_version = (snap_fw[-1][0] if snap_fw else 0) + 1
        fw_pairs = [(v, parse_schema(doc)) for v, doc in snap_fw]
        fw_pairs.append((new_version, schema))
        prog_pairs = [(pid, parse_schema(doc)) for pid, doc in snap_pg]

        # 锁外审查：不阻塞其他型号；结论只针对本次快照
        try:
            problems = _run_review(fw_pairs, prog_pairs)
        except ReviewError as e:
            _log(model_id, "firmware", None, raw_doc, "review_failed", {"error": str(e)})
            return PublishOutcome(False, f"兼容审查未能完成，已发布格式未变：{e}",
                                  review_failed=True)

        # 短事务加锁落库：复核快照未被同型号并发提交改变
        with SessionLocal() as s:
            model = s.get(DeviceModel, model_id)
            if model is None:
                return PublishOutcome(False, "型号不存在")
            _lock_model(s, model)
            firmwares, programs = _load_state(s, model_id)
            if not _snapshot_unchanged(snap, firmwares, programs):
                s.rollback()  # 同型号并发已提交：本次结论作废，基于新状态重试
                continue
            if problems:
                s.rollback()  # 失败不改已发布格式
                _log(model_id, "firmware", None, raw_doc,
                     "rejected", {"problems": problems})
                return PublishOutcome(
                    False, "存在不兼容组合，已拒绝发布（已发布格式未变）", problems)
            if len(firmwares) >= MAX_FORMATS:  # 锁内最终上限复核
                s.rollback()
                msg = f"固件格式已达 {MAX_FORMATS} 份上限，无法继续追加"
                _log(model_id, "firmware", None, raw_doc, "invalid", {"error": msg})
                return PublishOutcome(False, msg)

            new_version = (firmwares[-1].version if firmwares else 0) + 1
            s.add(FirmwareFormat(model_id=model_id, version=new_version, doc=raw_doc))
            s.add(PublishLog(model_id=model_id, action="firmware", program_id=None,
                             doc=raw_doc, result="published",
                             detail={"version": new_version,
                                     "checked_pairs": len(fw_pairs) * len(prog_pairs)}))
            s.commit()
            return PublishOutcome(True, f"固件写入格式 v{new_version} 已发布",
                                  version=new_version)

    return PublishOutcome(False, "同型号并发发布冲突，多次重试仍未赶上，请重新提交",
                          conflict=True)


def publish_program(model_id: int, program_id: str, raw_doc) -> PublishOutcome:
    """按程序号发布/替换程序读取格式。"""
    if not valid_name(program_id):
        return PublishOutcome(False, "程序号只能由字母数字及 _ . - 组成，长度 1~64")
    try:
        schema = parse_schema(raw_doc)
    except SchemaError as e:
        _log(model_id, "program", program_id, raw_doc, "invalid", {"error": str(e)})
        return PublishOutcome(False, f"格式不符合受限语法：{e}")

    for _ in range(MAX_REVIEW_RETRIES):
        snap = _snapshot(model_id)
        if snap is None:
            return PublishOutcome(False, "型号不存在")
        snap_fw, snap_pg = snap
        existing_ids = {pid for pid, _ in snap_pg}
        if program_id not in existing_ids and len(snap_pg) >= MAX_FORMATS:
            msg = f"程序格式已达 {MAX_FORMATS} 个上限，新程序号无法登记"
            _log(model_id, "program", program_id, raw_doc, "invalid", {"error": msg})
            return PublishOutcome(False, msg)

        fw_pairs = [(v, parse_schema(doc)) for v, doc in snap_fw]
        prog_pairs = [(pid, parse_schema(doc)) for pid, doc in snap_pg
                      if pid != program_id]
        prog_pairs.append((program_id, schema))

        # 锁外审查：不阻塞其他型号；结论只针对本次快照
        try:
            problems = _run_review(fw_pairs, prog_pairs)
        except ReviewError as e:
            _log(model_id, "program", program_id, raw_doc,
                 "review_failed", {"error": str(e)})
            return PublishOutcome(False, f"兼容审查未能完成，已发布格式未变：{e}",
                                  review_failed=True)

        # 短事务加锁落库：复核快照未被同型号并发提交改变
        with SessionLocal() as s:
            model = s.get(DeviceModel, model_id)
            if model is None:
                return PublishOutcome(False, "型号不存在")
            _lock_model(s, model)
            firmwares, programs = _load_state(s, model_id)
            if not _snapshot_unchanged(snap, firmwares, programs):
                s.rollback()  # 同型号并发已提交：本次结论作废，基于新状态重试
                continue
            if problems:
                s.rollback()  # 失败不改已发布格式
                _log(model_id, "program", program_id, raw_doc,
                     "rejected", {"problems": problems})
                return PublishOutcome(
                    False, "存在不兼容组合，已拒绝发布（已发布格式未变）", problems)
            existing = {p.program_id: p for p in programs}
            if program_id not in existing and len(programs) >= MAX_FORMATS:
                s.rollback()  # 锁内最终上限复核
                msg = f"程序格式已达 {MAX_FORMATS} 个上限，新程序号无法登记"
                _log(model_id, "program", program_id, raw_doc, "invalid", {"error": msg})
                return PublishOutcome(False, msg)

            if program_id in existing:
                existing[program_id].doc = raw_doc
            else:
                s.add(ProgramFormat(model_id=model_id, program_id=program_id, doc=raw_doc))
            s.add(PublishLog(model_id=model_id, action="program",
                             program_id=program_id, doc=raw_doc, result="published",
                             detail={"checked_pairs": len(fw_pairs) * len(prog_pairs)}))
            s.commit()
            return PublishOutcome(True, f"程序「{program_id}」读取格式已发布")

    return PublishOutcome(False, "同型号并发发布冲突，多次重试仍未赶上，请重新提交",
                          conflict=True)
