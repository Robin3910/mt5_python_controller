"""异步数据库引擎 / 会话（SQLAlchemy 2.0）。"""
import logging

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .orm import Base
from .settings import settings

logger = logging.getLogger(__name__)

# pool_pre_ping：取连接前先 ping，避免使用到已被服务端关闭的死连接
engine = create_async_engine(settings.mysql_dsn, pool_pre_ping=True, future=True)
# expire_on_commit=False：提交后对象仍可读，省去额外刷新
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


def _migrate_user_totp_columns(sync_conn) -> None:
    inspector = inspect(sync_conn)
    if "users" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("users")}
    dialect = sync_conn.engine.dialect.name
    if "totp_secret" not in cols:
        sync_conn.execute(text("ALTER TABLE users ADD COLUMN totp_secret VARCHAR(32)"))
    if "totp_enabled" not in cols:
        if dialect == "mysql":
            sync_conn.execute(
                text("ALTER TABLE users ADD COLUMN totp_enabled TINYINT(1) NOT NULL DEFAULT 0")
            )
        else:
            sync_conn.execute(
                text("ALTER TABLE users ADD COLUMN totp_enabled BOOLEAN NOT NULL DEFAULT 0")
            )


def _migrate_dispatch_price_column(sync_conn) -> None:
    """为已存在的 signal_dispatch 表补充成交价位列（create_all 不会改已存在的表）。"""
    inspector = inspect(sync_conn)
    if "signal_dispatch" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("signal_dispatch")}
    if "price" not in cols:
        sync_conn.execute(text("ALTER TABLE signal_dispatch ADD COLUMN price FLOAT"))


def _migrate_signal_source_column(sync_conn) -> None:
    """为已存在的 signal_history 表补充信号来源列（create_all 不会改已存在的表）。"""
    inspector = inspect(sync_conn)
    if "signal_history" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("signal_history")}
    if "source" not in cols:
        sync_conn.execute(text("ALTER TABLE signal_history ADD COLUMN source VARCHAR(16)"))


def _drop_legacy_nodes_table(sync_conn) -> None:
    """v0.2 迁移：旧表带 `token_hash` 列（一节点一令牌）；新方案改为全局共享令牌，
    且 `mt5_login` 升级为 UNIQUE NOT NULL，无法平滑 ALTER —— 直接丢弃旧表，由
    `create_all` 重建为新结构。已选择「清空所有节点重建」策略。
    """
    inspector = inspect(sync_conn)
    if "nodes" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("nodes")}
    if "token_hash" in cols:
        logger.warning(
            "legacy `nodes` table detected (token_hash column present); dropping for v0.2 schema reset"
        )
        sync_conn.execute(text("DROP TABLE nodes"))


async def init_db() -> None:
    """不存在则建表。生产环境建议改用 Alembic 迁移而非 create_all。"""
    async with engine.begin() as conn:
        await conn.run_sync(_drop_legacy_nodes_table)
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_migrate_user_totp_columns)
        await conn.run_sync(_migrate_dispatch_price_column)
        await conn.run_sync(_migrate_signal_source_column)
    logger.info("Database initialized (%s)", engine.url.render_as_string(hide_password=True))
