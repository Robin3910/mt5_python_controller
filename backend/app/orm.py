"""SQLAlchemy ORM 模型（生产用 MySQL，本地开发用 SQLite）。

承担“持久化”职责：节点账本、后台用户、操作审计、信号历史、分发明细。
"""
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON


class Base(DeclarativeBase):
    pass


class Node(Base):
    """节点账本（权威数据源；Redis 仅作缓存）。"""
    __tablename__ = "nodes"

    node_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # 仅存哈希
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    lot_mode: Mapped[str] = mapped_column(String(16), default="global")
    lot: Mapped[float | None] = mapped_column(Float, nullable=True)
    follow_sync: Mapped[bool] = mapped_column(Boolean, default=True)
    follow_poll: Mapped[bool] = mapped_column(Boolean, default=True)
    poll_order: Mapped[int] = mapped_column(Integer, default=0)
    filters_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # 节点级过滤
    mt5_login: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    mt5_server: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class User(Base):
    """后台管理员用户（权威数据源）。"""
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(64))  # SHA256 十六进制
    role: Mapped[str] = mapped_column(String(16), default="admin")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    totp_secret: Mapped[str | None] = mapped_column(String(32), nullable=True)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class AuditLog(Base):
    """操作审计：谁、在何时、对什么、做了什么、结果如何。"""
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    operator: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str | None] = mapped_column(String(64), nullable=True)
    params_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    result: Mapped[str] = mapped_column(String(16))
    ip: Mapped[str | None] = mapped_column(String(45), nullable=True)


class SignalHistory(Base):
    """信号历史：每条 Webhook 信号的解析与分发概况。"""
    __tablename__ = "signal_history"

    signal_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    received_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    source_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    raw_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    action: Mapped[str | None] = mapped_column(String(8), nullable=True)
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    sl: Mapped[float | None] = mapped_column(Float, nullable=True)
    tp: Mapped[float | None] = mapped_column(Float, nullable=True)
    comment: Mapped[str | None] = mapped_column(String(128), nullable=True)
    parsed_ok: Mapped[bool] = mapped_column(Boolean, default=False)
    dispatch_mode: Mapped[str | None] = mapped_column(String(8), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")


class SignalDispatch(Base):
    """分发明细：单条信号 × 单个节点的一次执行结果。"""
    __tablename__ = "signal_dispatch"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    signal_id: Mapped[str] = mapped_column(String(32), index=True)
    node_id: Mapped[str] = mapped_column(String(32), index=True)
    decided_vol: Mapped[float | None] = mapped_column(Float, nullable=True)  # 实际决策手数
    gate_result: Mapped[str] = mapped_column(String(16), default="passed")   # passed / skipped
    skip_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    retcode: Mapped[int | None] = mapped_column(Integer, nullable=True)      # MT5 返回码
    order_ticket: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    deal: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)         # 成交价位
    error: Mapped[str | None] = mapped_column(String(255), nullable=True)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
