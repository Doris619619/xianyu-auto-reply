"""
本文件定义 SQLite ORM 模型与引擎工厂。

它属于 models 模块，只描述表结构与会话工厂，不包含业务编排。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path

from sqlalchemy import Boolean, DateTime, Integer, String, Text, create_engine, func, inspect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


class QueueItemStatus(StrEnum):
    """队列项状态枚举。"""

    QUEUED = "queued"
    ACTIVE = "active"
    PARKED = "parked"
    DONE_AGREED = "done_agreed"
    DONE_REFUSED = "done_refused"
    DONE_MANUAL = "done_manual"
    FAILED = "failed"


class Base(DeclarativeBase):
    """SQLAlchemy 声明式基类。"""


class QueueItem(Base):
    """
    表示一条砍价队列项。

    保存商品链接、状态、排序与结果摘要；不保存密钥或登录态。
    """

    __tablename__ = "queue_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_id: Mapped[str] = mapped_column(String(64), index=True)
    detail_url: Mapped[str] = mapped_column(String(512))
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(String(32), index=True, default=QueueItemStatus.QUEUED)
    position: Mapped[int] = mapped_column(Integer, index=True, default=0)
    seller_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    processing_reply_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)
    result_summary: Mapped[str | None] = mapped_column(String(512), nullable=True)
    fail_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rounds_sent: Mapped[int] = mapped_column(Integer, default=0)
    waiting_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


class SessionMessage(Base):
    """
    表示当前或历史会话中的一条消息快照。

    仅用于面板展示；不作为发送证据源。
    """

    __tablename__ = "session_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    queue_item_id: Mapped[int] = mapped_column(Integer, index=True)
    speaker: Mapped[str] = mapped_column(String(16))
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AppSetting(Base):
    """
    表示可在面板修改的运行时设置（单行表）。
    """

    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    reply_timeout_seconds: Mapped[int] = mapped_column(Integer, default=180)
    max_rounds: Mapped[int] = mapped_column(Integer, default=6)
    auto_send: Mapped[bool] = mapped_column(Boolean, default=True)
    reply_mode: Mapped[str] = mapped_column(String(16), default="ai")
    worker_enabled: Mapped[bool] = mapped_column(Boolean, default=False)


_engine = None
SessionLocal: sessionmaker | None = None


def init_db(database_url: str) -> sessionmaker:
    """
    初始化 SQLite 引擎、建表并返回会话工厂。

    参数为 SQLAlchemy 连接串；副作用为创建数据目录与表。
    """

    global _engine, SessionLocal
    if database_url.startswith("sqlite:///./"):
        db_path = Path(database_url.removeprefix("sqlite:///./"))
        db_path.parent.mkdir(parents=True, exist_ok=True)
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    _engine = create_engine(database_url, future=True, connect_args=connect_args)
    Base.metadata.create_all(_engine)
    _upgrade_sqlite_schema(_engine, database_url)
    SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False, future=True)
    return SessionLocal


def _upgrade_sqlite_schema(engine, database_url: str) -> None:
    """为已存在的 SQLite 数据库补充可安全添加的模式字段。"""

    if not database_url.startswith("sqlite"):
        return
    inspector = inspect(engine)
    with engine.begin() as connection:
        setting_columns = {column["name"] for column in inspector.get_columns("app_settings")}
        if "reply_mode" not in setting_columns:
            connection.exec_driver_sql(
                "ALTER TABLE app_settings ADD COLUMN reply_mode VARCHAR(16) NOT NULL DEFAULT 'ai'"
            )
        item_columns = {column["name"] for column in inspector.get_columns("queue_items")}
        if "processing_reply_mode" not in item_columns:
            connection.exec_driver_sql(
                "ALTER TABLE queue_items ADD COLUMN processing_reply_mode VARCHAR(16)"
            )


def get_session_factory() -> sessionmaker:
    """
    返回已初始化的会话工厂；未初始化时抛出 RuntimeError。
    """

    if SessionLocal is None:
        raise RuntimeError("数据库尚未初始化，请先调用 init_db")
    return SessionLocal
