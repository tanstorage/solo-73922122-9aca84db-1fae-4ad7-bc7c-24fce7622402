"""持久化层：PostgreSQL（生产，docker-compose）/ SQLite（本地自测，由 DATABASE_URL 切换）。"""
import os

from sqlalchemy import (Column, DateTime, ForeignKey, Integer, String,
                        UniqueConstraint, create_engine, event, func)
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.types import JSON

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./contracts.db")

_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, pool_pre_ping=True, connect_args=_connect_args)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
Base = declarative_base()

# PostgreSQL 上使用 JSONB，其余方言退化为通用 JSON
from sqlalchemy.dialects.postgresql import JSONB  # noqa: E402
JsonDoc = JSON().with_variant(JSONB(), "postgresql")

if DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _sqlite_busy_timeout(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()


class DeviceModel(Base):
    __tablename__ = "device_models"
    id = Column(Integer, primary_key=True)
    name = Column(String(64), unique=True, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class FirmwareFormat(Base):
    """历代固件写入格式：只追加，每型号至多 8 份。"""
    __tablename__ = "firmware_formats"
    id = Column(Integer, primary_key=True)
    model_id = Column(Integer, ForeignKey("device_models.id"), nullable=False, index=True)
    version = Column(Integer, nullable=False)
    doc = Column("schema", JsonDoc, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (UniqueConstraint("model_id", "version"),)


class ProgramFormat(Base):
    """现用程序读取格式：按程序号替换，每型号至多 8 个程序。"""
    __tablename__ = "program_formats"
    id = Column(Integer, primary_key=True)
    model_id = Column(Integer, ForeignKey("device_models.id"), nullable=False, index=True)
    program_id = Column(String(64), nullable=False)
    doc = Column("schema", JsonDoc, nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(),
                        onupdate=func.now())
    __table_args__ = (UniqueConstraint("model_id", "program_id"),)


class PublishLog(Base):
    """发布日志：成功/拒绝/无效均留痕，拒绝时保存反例等依据。"""
    __tablename__ = "publish_log"
    id = Column(Integer, primary_key=True)
    model_id = Column(Integer, ForeignKey("device_models.id"), nullable=False, index=True)
    action = Column(String(16), nullable=False)      # firmware | program
    program_id = Column(String(64), nullable=True)
    doc = Column("schema", JsonDoc, nullable=False)
    result = Column(String(16), nullable=False)      # published | rejected | invalid
    detail = Column(JsonDoc, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
