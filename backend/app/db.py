"""异步数据库引擎 / 会话（SQLAlchemy 2.0）。"""
import logging

from sqlalchemy import event, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .orm import Base
from .settings import settings

logger = logging.getLogger(__name__)

_IS_SQLITE = settings.mysql_dsn.startswith("sqlite")

# pool_pre_ping：取连接前先 ping，避免使用到已被服务端关闭的死连接
# SQLite（本地开发 / 测试）：整库只有一把写锁，timeout 让并发写等待而不是立刻报
# "database is locked"；MySQL 不需要该参数。
engine = create_async_engine(
    settings.mysql_dsn,
    pool_pre_ping=True,
    future=True,
    **({"connect_args": {"timeout": 30}} if _IS_SQLITE else {}),
)

if _IS_SQLITE:
    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record) -> None:
        """WAL 模式允许「一写多读」并发，避免多节点回报同时落库时互相阻塞。"""
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()
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
    """为已存在的 signal_history 表补充信号来源与处理模型列（create_all 不会改已存在的表）。"""
    inspector = inspect(sync_conn)
    if "signal_history" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("signal_history")}
    if "source" not in cols:
        sync_conn.execute(text("ALTER TABLE signal_history ADD COLUMN source VARCHAR(16)"))
    if "model" not in cols:
        sync_conn.execute(text("ALTER TABLE signal_history ADD COLUMN model VARCHAR(16)"))


def _migrate_audit_columns(sync_conn) -> None:
    """为已存在的 audit_log 表补充分类与操作前后数据列。"""
    inspector = inspect(sync_conn)
    if "audit_log" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("audit_log")}
    if "category" not in cols:
        sync_conn.execute(text("ALTER TABLE audit_log ADD COLUMN category VARCHAR(16)"))
    if "before_json" not in cols:
        sync_conn.execute(text("ALTER TABLE audit_log ADD COLUMN before_json JSON"))
    if "after_json" not in cols:
        sync_conn.execute(text("ALTER TABLE audit_log ADD COLUMN after_json JSON"))


def _migrate_node_group_strategy_id(sync_conn) -> None:
    """为已存在的 node_group 表补充一对一策略绑定列（create_all 不会改已存在的表）。"""
    inspector = inspect(sync_conn)
    if "node_group" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("node_group")}
    if "strategy_id" in cols:
        return
    dialect = sync_conn.engine.dialect.name
    sync_conn.execute(text("ALTER TABLE node_group ADD COLUMN strategy_id VARCHAR(32)"))
    # 唯一索引：同一策略只能绑定一个分组；多行 NULL 在 MySQL/SQLite 均允许
    if dialect == "mysql":
        sync_conn.execute(
            text("CREATE UNIQUE INDEX uq_node_group_strategy_id ON node_group (strategy_id)")
        )
    else:
        sync_conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_node_group_strategy_id "
                "ON node_group (strategy_id)"
            )
        )


def _migrate_node_group_trend_risk(sync_conn) -> None:
    """node_group.trend_risk_enabled：分组趋势风控开关（默认关闭）。"""
    inspector = inspect(sync_conn)
    if "node_group" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("node_group")}
    if "trend_risk_enabled" in cols:
        return
    dialect = sync_conn.engine.dialect.name
    col_type = "TINYINT(1) NOT NULL DEFAULT 0" if dialect == "mysql" else "BOOLEAN NOT NULL DEFAULT 0"
    sync_conn.execute(text(f"ALTER TABLE node_group ADD COLUMN trend_risk_enabled {col_type}"))


def _migrate_node_group_limit_watch(sync_conn) -> None:
    """node_group.limit_watch_enabled / limit_watch_keyword：限价挂单监听（默认关，关键字默认为 limit，允许空串）。"""
    inspector = inspect(sync_conn)
    if "node_group" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("node_group")}
    dialect = sync_conn.engine.dialect.name
    if "limit_watch_enabled" not in cols:
        col_type = "TINYINT(1) NOT NULL DEFAULT 0" if dialect == "mysql" else "BOOLEAN NOT NULL DEFAULT 0"
        sync_conn.execute(text(f"ALTER TABLE node_group ADD COLUMN limit_watch_enabled {col_type}"))
    if "limit_watch_keyword" not in cols:
        sync_conn.execute(
            text("ALTER TABLE node_group ADD COLUMN limit_watch_keyword VARCHAR(32) NOT NULL DEFAULT 'limit'")
        )


def _migrate_group_task_strategy_columns(sync_conn) -> None:
    """为已存在的分组任务表补充策略托管所需列（create_all 不会改已存在的表）。"""
    inspector = inspect(sync_conn)
    tables = set(inspector.get_table_names())
    dialect = sync_conn.engine.dialect.name
    json_type = "JSON" if dialect == "mysql" else "TEXT"

    if "group_signal_task" in tables:
        cols = {c["name"] for c in inspector.get_columns("group_signal_task")}
        additions = {
            "strategy_id": "VARCHAR(32)",
            "strategy_name": "VARCHAR(64)",
            "strategy_snapshot_json": json_type,
            "opened_at": "DATETIME",
            "total_orders": "INTEGER NOT NULL DEFAULT 0",
            "total_volume": "FLOAT NOT NULL DEFAULT 0",
            "realized_profit": "FLOAT NOT NULL DEFAULT 0",
        }
        for name, ddl in additions.items():
            if name not in cols:
                sync_conn.execute(
                    text(f"ALTER TABLE group_signal_task ADD COLUMN {name} {ddl}")
                )

    if "group_task_dispatch" in tables:
        cols = {c["name"] for c in inspector.get_columns("group_task_dispatch")}
        additions = {
            # 节点子任务自持魔术号后，品种冗余到子任务上，便于释放节点互斥占位
            "symbol": "VARCHAR(32)",
            "position_count": "INTEGER NOT NULL DEFAULT 0",
            # 限价开仓在成交前只有挂单，没有这列就看不出任务是「在途」还是「空转」
            "pending_orders": "INTEGER NOT NULL DEFAULT 0",
            "add_count": "INTEGER NOT NULL DEFAULT 0",
            "total_orders": "INTEGER NOT NULL DEFAULT 0",
            "total_volume": "FLOAT NOT NULL DEFAULT 0",
            "realized_profit": "FLOAT NOT NULL DEFAULT 0",
            "finish_reason": "VARCHAR(255)",
            "opened_at": "DATETIME",
            "last_report_at": "DATETIME",
            # 停止交易后仍未平掉的持仓笔数，以及供节点重连恢复的策略运行态
            "residual_positions": "INTEGER NOT NULL DEFAULT 0",
            "runtime_json": json_type,
            "stop_requested_at": "DATETIME",
        }
        for name, ddl in additions.items():
            if name not in cols:
                sync_conn.execute(
                    text(f"ALTER TABLE group_task_dispatch ADD COLUMN {name} {ddl}")
                )
        # 账户风控结束原因含规则名与参数，需从历史 VARCHAR(64) 放宽
        if "finish_reason" in cols and dialect == "mysql":
            sync_conn.execute(
                text(
                    "ALTER TABLE group_task_dispatch "
                    "MODIFY COLUMN finish_reason VARCHAR(255) NULL"
                )
            )
        # 布尔列需按方言区分：MySQL 用 TINYINT(1)，SQLite 用 BOOLEAN
        if "hold_when_empty" not in cols:
            if dialect == "mysql":
                sync_conn.execute(
                    text(
                        "ALTER TABLE group_task_dispatch "
                        "ADD COLUMN hold_when_empty TINYINT(1) NOT NULL DEFAULT 0"
                    )
                )
            else:
                sync_conn.execute(
                    text(
                        "ALTER TABLE group_task_dispatch "
                        "ADD COLUMN hold_when_empty BOOLEAN NOT NULL DEFAULT 0"
                    )
                )

    if "group_task_event" in tables:
        # 开单原因的计算依据明细（偏离 / 阈值 / 手数公式等逐项参数）
        cols = {c["name"] for c in inspector.get_columns("group_task_event")}
        if "detail_json" not in cols:
            sync_conn.execute(
                text(f"ALTER TABLE group_task_event ADD COLUMN detail_json {json_type}")
            )


def _migrate_node_risk_json(sync_conn) -> None:
    """nodes.risk_json：账户级风控配置（浮盈亏比等）。"""
    inspector = inspect(sync_conn)
    if "nodes" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("nodes")}
    if "risk_json" in cols:
        return
    dialect = sync_conn.dialect.name
    json_type = "JSON" if dialect == "mysql" else "TEXT"
    sync_conn.execute(text(f"ALTER TABLE nodes ADD COLUMN risk_json {json_type}"))


def _migrate_node_trend_json(sync_conn) -> None:
    """nodes.trend_json：趋势面板参数（EMA/RSI 周期、权重与阈值）。"""
    inspector = inspect(sync_conn)
    if "nodes" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("nodes")}
    if "trend_json" in cols:
        return
    dialect = sync_conn.dialect.name
    json_type = "JSON" if dialect == "mysql" else "TEXT"
    sync_conn.execute(text(f"ALTER TABLE nodes ADD COLUMN trend_json {json_type}"))


def _migrate_node_client_version(sync_conn) -> None:
    """nodes.client_version / client_version_at：节点上报的客户端版本与上报时间。"""
    inspector = inspect(sync_conn)
    if "nodes" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("nodes")}
    if "client_version" not in cols:
        sync_conn.execute(text("ALTER TABLE nodes ADD COLUMN client_version VARCHAR(32)"))
    if "client_version_at" not in cols:
        sync_conn.execute(text("ALTER TABLE nodes ADD COLUMN client_version_at DATETIME"))


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
        await conn.run_sync(_migrate_audit_columns)
        await conn.run_sync(_migrate_node_group_strategy_id)
        await conn.run_sync(_migrate_node_group_trend_risk)
        await conn.run_sync(_migrate_node_group_limit_watch)
        await conn.run_sync(_migrate_group_task_strategy_columns)
        await conn.run_sync(_migrate_node_risk_json)
        await conn.run_sync(_migrate_node_trend_json)
        await conn.run_sync(_migrate_node_client_version)
    logger.info("Database initialized (%s)", engine.url.render_as_string(hide_password=True))
