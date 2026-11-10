"""异步数据库引擎 / 会话（SQLAlchemy 2.0）。"""
import logging

from sqlalchemy import event, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .orm import Base, UserReview
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


def _migrate_users_follow_paused(sync_conn) -> None:
    """users 的「暂停接单」四列：带单市场与 AI 市场各一个独立开关。

    - follow_paused / follow_paused_at：暂停【带单】市场（tpl_2 趋势 / tpl_3 网格）
    - ai_follow_paused / ai_follow_paused_at：暂停【AI 交易】市场（tpl_1）

    两组各存各的，不合成一个布尔：一个带单员可能同时做普通带单与 AI 机器人，
    合并后无法表达「只停一边」。

    布尔列给 NOT NULL DEFAULT 0（存量用户一律视为未暂停，零行为变化），
    时间列可空（未暂停时没有暂停时刻）。
    """
    inspector = inspect(sync_conn)
    if "users" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("users")}
    dialect = sync_conn.engine.dialect.name
    bool_type = "TINYINT(1) NOT NULL DEFAULT 0" if dialect == "mysql" else "BOOLEAN NOT NULL DEFAULT 0"
    for name in ("follow_paused", "ai_follow_paused"):
        if name not in cols:
            sync_conn.execute(text(f"ALTER TABLE users ADD COLUMN {name} {bool_type}"))
    for name in ("follow_paused_at", "ai_follow_paused_at"):
        if name not in cols:
            sync_conn.execute(text(f"ALTER TABLE users ADD COLUMN {name} DATETIME"))


def _migrate_follow_node_state(sync_conn) -> None:
    inspector = inspect(sync_conn)
    tables = set(inspector.get_table_names())
    if "group_follower_node" not in tables:
        return
    columns = {column["name"] for column in inspector.get_columns("group_follower_node")}
    if "follow_state" not in columns:
        sync_conn.execute(text("ALTER TABLE group_follower_node ADD COLUMN follow_state VARCHAR(16) NOT NULL DEFAULT 'running'"))
    if "leader_paused" not in columns:
        sync_conn.execute(text("ALTER TABLE group_follower_node ADD COLUMN leader_paused BOOLEAN NOT NULL DEFAULT 0"))
    if "single_dispatch_id" not in columns:
        sync_conn.execute(text("ALTER TABLE group_follower_node ADD COLUMN single_dispatch_id BIGINT NULL"))
    for table in ("group_follower_node", "ai_follow_application_node", "group_task_dispatch"):
        if table not in tables:
            continue
        fields = {column["name"] for column in inspector.get_columns(table)}
        if "follow_settings_json" not in fields:
            sync_conn.execute(text(f"ALTER TABLE {table} ADD COLUMN follow_settings_json JSON NULL"))


def _migrate_users_profile_columns(sync_conn) -> None:
    """users 的个人中心自助维护列：email / leader_name / leader_intro。

    - email：联系邮箱，**不参与登录**，故不设唯一约束（允许重复）。
    - leader_name：带单员展示名。为 NULL 时前端回退显示 display_name，
      所以不强制填 —— 存量用户不填也不影响带单广场显示名字。
    - leader_intro：带单员介绍（自由文本），目前只在个人中心自己可见。

    三列都可空且**没有默认值**：存量用户一律 NULL —— 零行为变化。
    """
    inspector = inspect(sync_conn)
    if "users" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("users")}
    additions = {
        "email": "VARCHAR(128)",
        "leader_name": "VARCHAR(64)",
        "leader_intro": "TEXT",
    }
    for name, ddl in additions.items():
        if name not in cols:
            sync_conn.execute(text(f"ALTER TABLE users ADD COLUMN {name} {ddl}"))


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


def _migrate_signal_center_columns(sync_conn) -> None:
    """信号中心所需的新增列（全部可空，不影响任何交易逻辑）。

    - signal_history.reject_code：信号整体拒收原因的结构化编码
    - signal_dispatch.skip_code：逐节点跳过原因的结构化编码
    - group_task_dispatch.skip_code：同上（strategy 链路）
    - group_task_dispatch.action：冗余存一份动作，统计 UNION 时不必 join 主任务表

    这些列只服务于统计，写漏 / 写坏都不影响信号下发与成交。
    """
    inspector = inspect(sync_conn)
    tables = set(inspector.get_table_names())
    additions = {
        "signal_history": {"reject_code": "VARCHAR(24)"},
        "signal_dispatch": {"skip_code": "VARCHAR(24)"},
        "group_task_dispatch": {"skip_code": "VARCHAR(24)", "action": "VARCHAR(8)"},
        "group_signal_task": {"skip_code": "VARCHAR(24)"},
    }
    for table, cols_map in additions.items():
        if table not in tables:
            continue
        cols = {c["name"] for c in inspector.get_columns(table)}
        for name, ddl in cols_map.items():
            if name in cols:
                continue
            sync_conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
        # 统计要按 skip_code 聚合，补一个普通索引
        if "skip_code" in cols_map:
            index = f"ix_{table}_skip_code"
            existing = {i.get("name") for i in inspector.get_indexes(table)}
            if index not in existing:
                sync_conn.execute(text(f"CREATE INDEX {index} ON {table} (skip_code)"))


# 时延埋点列：sent_at / recv_at / order_at / filled_at + 两段耗时（毫秒）
# signal_dispatch（normal 链路）与 group_task_dispatch（strategy 链路）共用同一组列名，
# 便于后台用同一套口径展示两条链路的时延分解。
_LATENCY_COLUMNS: tuple[tuple[str, str], ...] = (
    ("sent_at", "DATETIME"),
    ("recv_at", "DATETIME"),
    ("order_at", "DATETIME"),
    ("filled_at", "DATETIME"),
    ("queued_ms", "FLOAT"),
    ("order_to_fill_ms", "FLOAT"),
)


def _migrate_latency_marks(sync_conn) -> None:
    """为已存在的分发明细表补时延埋点列（create_all 不会改已存在的表）。"""
    inspector = inspect(sync_conn)
    tables = set(inspector.get_table_names())
    for table in ("signal_dispatch", "group_task_dispatch"):
        if table not in tables:
            continue
        cols = {c["name"] for c in inspector.get_columns(table)}
        for name, ddl in _LATENCY_COLUMNS:
            if name not in cols:
                sync_conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


# 时延埋点列需要毫秒精度的表 / 列：
#   signal_dispatch    —— sent_at → finished_at 是普通信号分发的「命令往返时延」
#   group_task_dispatch —— sent_at → opened_at 是策略任务的「命令往返时延」
#                         （opened_at = 节点首次回报开仓；finished_at 是策略平仓收口，
#                          那是持仓时长，不是时延，两者语义完全不同）
# MySQL 的 DATETIME 默认整秒，发包与回报同秒就记成 0ms，跨机往返时延根本算不出来，
# 所以这几列要 ALTER 成 DATETIME(3)。SQLite 的 DateTime 本身就存微秒，不用动。
_MS_PRECISION_COLUMNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("signal_dispatch", ("sent_at", "finished_at", "recv_at", "order_at", "filled_at")),
    ("group_task_dispatch", ("sent_at", "opened_at", "recv_at", "order_at", "filled_at")),
)


def _migrate_latency_precision(sync_conn) -> None:
    """把时延埋点列放宽到毫秒精度（MySQL DATETIME → DATETIME(3)）。"""
    inspector = inspect(sync_conn)
    if sync_conn.engine.dialect.name != "mysql":
        return
    tables = set(inspector.get_table_names())
    for table, columns in _MS_PRECISION_COLUMNS:
        if table not in tables:
            continue
        for col in inspector.get_columns(table):
            name = col["name"]
            if name not in columns:
                continue
            # 已经是毫秒精度就跳过：新建库的列本来就是 DATETIME(3)，
            # 每次启动都 ALTER 一遍会白白锁表。
            if getattr(col.get("type"), "fsp", None) == 3:
                continue
            sync_conn.execute(
                text(f"ALTER TABLE {table} MODIFY COLUMN {name} DATETIME(3) NULL")
            )


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


def _migrate_node_group_signal_concurrent(sync_conn) -> None:
    """node_group.signal_concurrent_enabled：全员同步信号并发（默认关闭）。"""
    inspector = inspect(sync_conn)
    if "node_group" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("node_group")}
    if "signal_concurrent_enabled" in cols:
        return
    dialect = sync_conn.engine.dialect.name
    col_type = "TINYINT(1) NOT NULL DEFAULT 0" if dialect == "mysql" else "BOOLEAN NOT NULL DEFAULT 0"
    sync_conn.execute(text(
        f"ALTER TABLE node_group ADD COLUMN signal_concurrent_enabled {col_type}"
    ))


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


def _migrate_node_group_accept_ai_follow(sync_conn) -> None:
    """node_group.accept_ai_follow：是否开放【AI 交易】广场的跟单（默认关闭）。"""
    inspector = inspect(sync_conn)
    if "node_group" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("node_group")}
    if "accept_ai_follow" in cols:
        return
    dialect = sync_conn.engine.dialect.name
    col_type = "TINYINT(1) NOT NULL DEFAULT 0" if dialect == "mysql" else "BOOLEAN NOT NULL DEFAULT 0"
    sync_conn.execute(
        text(f"ALTER TABLE node_group ADD COLUMN accept_ai_follow {col_type}")
    )


def _migrate_node_group_member_limit_watch(sync_conn) -> None:
    """node_group_member.limit_watch_report：成员是否作为限价监听触发来源（默认关）。"""
    inspector = inspect(sync_conn)
    if "node_group_member" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("node_group_member")}
    if "limit_watch_report" in cols:
        return
    dialect = sync_conn.engine.dialect.name
    col_type = "TINYINT(1) NOT NULL DEFAULT 0" if dialect == "mysql" else "BOOLEAN NOT NULL DEFAULT 0"
    sync_conn.execute(
        text(f"ALTER TABLE node_group_member ADD COLUMN limit_watch_report {col_type}")
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


def _migrate_user_phone(sync_conn) -> None:
    """users.phone / users.phone_verified：手机号登录凭据。

    - phone：归一化后的 E.164（+86xxxxxxxxxxx），唯一索引；存量用户为 NULL，不受影响
    - phone_verified：是否已通过短信验证（管理员手工绑定时为 False）

    MySQL 不支持 `CREATE UNIQUE INDEX IF NOT EXISTS`，需按方言分支。
    """
    inspector = inspect(sync_conn)
    if "users" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("users")}
    dialect = sync_conn.engine.dialect.name
    if "phone" not in cols:
        sync_conn.execute(text("ALTER TABLE users ADD COLUMN phone VARCHAR(20)"))
    # 唯一索引单独处理：存量列已存在但索引缺失（手工加过列的库）时也要补上。
    # 索引名与 ORM 的 index=True 生成的一致（ix_users_phone），新库与升级库结构相同。
    indexes = {i["name"] for i in inspector.get_indexes("users") if i.get("unique")}
    if "ix_users_phone" not in indexes:
        # 建唯一索引前先扫重：存量全 NULL 不会冲突，但手工绑过手机号的库可能有重复
        dup = sync_conn.execute(
            text(
                "SELECT phone, COUNT(*) AS c FROM users "
                "WHERE phone IS NOT NULL GROUP BY phone HAVING c > 1"
            )
        ).fetchall()
        if dup:
            logger.warning(
                "users.phone has %d duplicate value(s); unique index skipped", len(dup)
            )
        elif dialect == "mysql":
            sync_conn.execute(text("CREATE UNIQUE INDEX ix_users_phone ON users (phone)"))
        else:
            sync_conn.execute(
                text("CREATE UNIQUE INDEX IF NOT EXISTS ix_users_phone ON users (phone)")
            )
    if "phone_verified" not in cols:
        col_type = (
            "TINYINT(1) NOT NULL DEFAULT 0" if dialect == "mysql"
            else "BOOLEAN NOT NULL DEFAULT 0"
        )
        sync_conn.execute(text(f"ALTER TABLE users ADD COLUMN phone_verified {col_type}"))


def _migrate_user_rbac_columns(sync_conn) -> None:
    """users.token_version / display_name：会话吊销版本号与显示名。"""
    inspector = inspect(sync_conn)
    if "users" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("users")}
    if "token_version" not in cols:
        sync_conn.execute(
            text("ALTER TABLE users ADD COLUMN token_version INTEGER NOT NULL DEFAULT 0")
        )
    if "display_name" not in cols:
        sync_conn.execute(text("ALTER TABLE users ADD COLUMN display_name VARCHAR(64)"))


# 按用户归属数据的三张表：空值 = 管理员名下
_OWNER_TABLES = ("nodes", "node_group", "trading_strategy")


def _migrate_owner_columns(sync_conn) -> None:
    """nodes / node_group / trading_strategy.owner_user_id：数据归属列 + 索引。

    存量行保持空值（归管理员），不回填。索引名与 ORM 的 index=True 生成的一致，
    新库与升级库结构相同。
    """
    inspector = inspect(sync_conn)
    tables = set(inspector.get_table_names())
    dialect = sync_conn.engine.dialect.name
    for table in _OWNER_TABLES:
        if table not in tables:
            continue
        cols = {c["name"] for c in inspector.get_columns(table)}
        if "owner_user_id" in cols:
            continue
        sync_conn.execute(text(f"ALTER TABLE {table} ADD COLUMN owner_user_id INTEGER"))
        index = f"ix_{table}_owner_user_id"
        if dialect == "mysql":
            sync_conn.execute(text(f"CREATE INDEX {index} ON {table} (owner_user_id)"))
        else:
            sync_conn.execute(
                text(f"CREATE INDEX IF NOT EXISTS {index} ON {table} (owner_user_id)")
            )


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


def _migrate_strategy_config_version(sync_conn) -> None:
    """trading_strategy.config_version：配置版本号（热推版本门 + 上限下限校验）。"""
    inspector = inspect(sync_conn)
    if "trading_strategy" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("trading_strategy")}
    if "config_version" not in cols:
        sync_conn.execute(
            text("ALTER TABLE trading_strategy ADD COLUMN config_version INTEGER NOT NULL DEFAULT 0")
        )


def _migrate_group_dispatch_add_counts(sync_conn) -> None:
    """group_task_dispatch.add_counts_json：各规则已开笔数（前端上限下限校验的依据）。

    节点上报里已经带了 add_counts（按规则类型分开），但历史只落了合计的
    add_count。存 JSON 是因为规则类型是动态键（"1" / "2"）。
    """
    inspector = inspect(sync_conn)
    if "group_task_dispatch" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("group_task_dispatch")}
    if "add_counts_json" in cols:
        return
    dialect = sync_conn.engine.dialect.name
    json_type = "JSON" if dialect == "mysql" else "TEXT"
    sync_conn.execute(
        text(f"ALTER TABLE group_task_dispatch ADD COLUMN add_counts_json {json_type}")
    )


def _migrate_group_dispatch_add_progress(sync_conn) -> None:
    """group_task_dispatch.add_progress_json：各规则的加仓进度。

    节点上报里已经带了 add_progress（当前第几批 / 还能再加几笔 / 下一批参数），
    存 JSON 是因为规则类型是动态键（"1" / "2"）。
    """
    inspector = inspect(sync_conn)
    if "group_task_dispatch" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("group_task_dispatch")}
    if "add_progress_json" in cols:
        return
    dialect = sync_conn.engine.dialect.name
    json_type = "JSON" if dialect == "mysql" else "TEXT"
    sync_conn.execute(
        text(f"ALTER TABLE group_task_dispatch ADD COLUMN add_progress_json {json_type}")
    )


async def init_db() -> None:
    """不存在则建表。生产环境建议改用 Alembic 迁移而非 create_all。"""
    async with engine.begin() as conn:
        await conn.run_sync(_drop_legacy_nodes_table)
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_migrate_user_totp_columns)
        await conn.run_sync(_migrate_users_follow_paused)
        await conn.run_sync(_migrate_follow_node_state)
        await conn.run_sync(_migrate_users_profile_columns)
        await conn.run_sync(_migrate_dispatch_price_column)
        await conn.run_sync(_migrate_signal_source_column)
        await conn.run_sync(_migrate_signal_center_columns)
        await conn.run_sync(_migrate_latency_marks)
        await conn.run_sync(_migrate_latency_precision)
        await conn.run_sync(_migrate_audit_columns)
        await conn.run_sync(_migrate_node_group_strategy_id)
        await conn.run_sync(_migrate_node_group_trend_risk)
        await conn.run_sync(_migrate_node_group_signal_concurrent)
        await conn.run_sync(_migrate_node_group_limit_watch)
        await conn.run_sync(_migrate_node_group_accept_ai_follow)
        await conn.run_sync(_migrate_node_group_member_limit_watch)
        await conn.run_sync(_migrate_group_task_strategy_columns)
        await conn.run_sync(_migrate_node_risk_json)
        await conn.run_sync(_migrate_node_trend_json)
        await conn.run_sync(_migrate_node_client_version)
        await conn.run_sync(_migrate_user_rbac_columns)
        await conn.run_sync(_migrate_user_phone)
        await conn.run_sync(_migrate_owner_columns)
        await conn.run_sync(_migrate_strategy_config_version)
        await conn.run_sync(_migrate_group_dispatch_add_counts)
        await conn.run_sync(_migrate_group_dispatch_add_progress)
        await conn.run_sync(_migrate_dashboard_columns)
        await conn.run_sync(_migrate_node_credentials)
        await conn.run_sync(_migrate_node_primary)
        await conn.run_sync(_migrate_role_capabilities)
        await conn.run_sync(_migrate_mt5_deal_columns)
        await conn.run_sync(_migrate_user_reviews)
    logger.info("Database initialized (%s)", engine.url.render_as_string(hide_password=True))


def _migrate_mt5_deal_columns(sync_conn) -> None:
    """为已存在的 mt5_deal 表补充 server_time_offset 列。

    `mt5_deal` 是新表，create_all 会直接建好；但如果先上线了成交上报、
    后补这一列，create_all 不会改已存在的表，必须显式 ALTER 补上。

    server_time_offset = 该节点券商服务器钟面相对真实 UTC 的偏移（秒）。
    分日归集时要减掉它，否则经纪商不在 UTC+0 时会把成交归到错误的一天。
    """
    inspector = inspect(sync_conn)
    if "mt5_deal" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("mt5_deal")}
    if "server_time_offset" not in cols:
        sync_conn.execute(
            text("ALTER TABLE mt5_deal ADD COLUMN server_time_offset INTEGER NOT NULL DEFAULT 0")
        )


def _migrate_dashboard_columns(sync_conn) -> None:
    """保留存量节点启用状态；专属凭证另表由 create_all 创建。"""
    inspector = inspect(sync_conn)
    tables = set(inspector.get_table_names())
    additions = {
        "nodes": {
            "approval_status": "VARCHAR(16) NOT NULL DEFAULT 'approved'",
            "requested_by_user_id": "INTEGER",
            "admin_enable_requested": "BOOLEAN NOT NULL DEFAULT 0",
        },
        "audit_log": {"request_key": "VARCHAR(128)", "actor_user_id": "INTEGER"},
    }
    for table, columns in additions.items():
        if table not in tables:
            continue
        existing = {c["name"] for c in inspector.get_columns(table)}
        for column, definition in columns.items():
            if column not in existing:
                sync_conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {definition}"))
    if "audit_log" in tables:
        inspector = inspect(sync_conn)
        unique_columns = {tuple(i["column_names"]) for i in inspector.get_indexes("audit_log") if i.get("unique")}
        unique_columns.update(tuple(c["column_names"]) for c in inspector.get_unique_constraints("audit_log"))
        if ("request_key",) not in unique_columns:
            sync_conn.execute(text("CREATE UNIQUE INDEX uq_audit_dashboard_request ON audit_log (request_key)"))


def _migrate_node_credentials(sync_conn) -> None:
    """为本功能迭代中的独立凭证表补所属快照；不触碰 nodes 的旧令牌迁移。"""
    inspector = inspect(sync_conn)
    tables = set(inspector.get_table_names())
    if "node_credentials" not in tables:
        return
    columns = {c["name"] for c in inspector.get_columns("node_credentials")}
    if "owner_user_id" not in columns:
        sync_conn.execute(text("ALTER TABLE node_credentials ADD COLUMN owner_user_id INTEGER"))
        sync_conn.execute(text(
            "UPDATE node_credentials SET owner_user_id = "
            "(SELECT owner_user_id FROM nodes WHERE nodes.node_id = node_credentials.node_id)"
        ))
    # 未部署版本曾使用单数表名；保留数据并幂等拷贝，不做丢表操作。
    if "node_credential" in tables:
        old_cols = {c["name"] for c in inspector.get_columns("node_credential")}
        owner_expr = "old.owner_user_id" if "owner_user_id" in old_cols else "n.owner_user_id"
        sync_conn.execute(text(
            "INSERT INTO node_credentials (node_id, token_sha256, generation, legacy_allowed, owner_user_id) "
            f"SELECT old.node_id, old.token_sha256, old.generation, old.legacy_allowed, {owner_expr} "
            "FROM node_credential old JOIN nodes n ON n.node_id = old.node_id "
            "WHERE NOT EXISTS (SELECT 1 FROM node_credentials fresh WHERE fresh.node_id = old.node_id)"
        ))


def _migrate_role_capabilities(sync_conn) -> None:
    """sys_role.capabilities：角色能力码 JSON 数组（无 AI 策略授权 = 空数组）。

    MySQL 不允许 TEXT / BLOB / JSON 列带 DEFAULT，所以这里不能写成
    `ADD COLUMN capabilities TEXT NOT NULL DEFAULT '[]'`。存量行补列后会拿到空串，
    再统一回填 '[]'——解码侧也会把非法 JSON 当空数组，双保险。
    """
    inspector = inspect(sync_conn)
    if "sys_role" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("sys_role")}
    if "capabilities" in cols:
        return
    sync_conn.execute(text("ALTER TABLE sys_role ADD COLUMN capabilities TEXT NULL"))
    # 旧行为空串、新增行由 ORM default 写 '[]'；这里把空值统一成 '[]'，
    # WHERE 用 = '' 而非 IS NULL：MySQL 补列给存量行的是 ''，不是 NULL。
    sync_conn.execute(
        text("UPDATE sys_role SET capabilities = '[]' WHERE capabilities IS NULL OR capabilities = ''")
    )


def _migrate_node_primary(sync_conn) -> None:
    """nodes.is_primary：按所有者唯一的主节点标记（默认 0）。"""
    inspector = inspect(sync_conn)
    if "nodes" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("nodes")}
    if "is_primary" in cols:
        return
    dialect = sync_conn.engine.dialect.name
    # MySQL / SQLite 都把 Boolean 映射为 0/1；统一加在末端即可。
    col_type = "TINYINT(1) NOT NULL DEFAULT 0" if dialect == "mysql" else "BOOLEAN NOT NULL DEFAULT 0"
    sync_conn.execute(text(f"ALTER TABLE nodes ADD COLUMN is_primary {col_type}"))
    sync_conn.execute(text(
        f"CREATE INDEX IF NOT EXISTS ix_nodes_is_primary ON nodes (is_primary)"
    ))


def _migrate_user_reviews(sync_conn) -> None:
    """user_reviews 表：注册用户的 KYC 实名审核单。

    首次引入时是**新表**，用 SQLAlchemy 按方言建表（而不是手写 DDL）——
    手写的话 MySQL 与 SQLite 在「自增主键语法」「CREATE INDEX IF NOT EXISTS」
    上都不兼容（AUTOINCREMENT 是 SQLite 专有，IF NOT EXISTS 的索引 MySQL
    不支持），得写两套方言分支。

    **实名材料从「姓名 / 证件类型 / 证件号 / 邮箱」简化为「姓名 / 邮箱 / 手机号」**
    （2026-11-05），所以旧表里的 `cert_type` / `cert_no_masked` /
    `cert_no_hash` 三列要删掉，并补一列 `phone`。

    ⚠️ **删列前必须先删索引**：MySQL 删列会连带删掉该列上的索引，但
    **SQLite 不会** —— 留着 `ix_user_reviews_cert_no_hash` 再删
    `cert_no_hash` 列，SQLite 会报
    `error in index ix_user_reviews_cert_no_hash after drop column:
    no such column: cert_no_hash`，整条迁移直接失败。

    ⚠️ **DROP INDEX 的语法两方言不同**：
    - MySQL：`DROP INDEX <name> ON <table>`（**没有** `IF EXISTS`）
    - SQLite：`DROP INDEX IF EXISTS <name>`
    所以这里必须按方言分支。

    这三列上没有外键约束，删列不会牵连别的表。
    """
    inspector = inspect(sync_conn)
    if "user_reviews" not in inspector.get_table_names():
        UserReview.__table__.create(bind=sync_conn, checkfirst=True)
        return

    cols = {c["name"] for c in inspector.get_columns("user_reviews")}
    dialect = sync_conn.engine.dialect.name

    # 1. 先删掉挂在旧证件列上的索引（SQLite 删列不会连带删索引）
    legacy_cols = ("cert_type", "cert_no_masked", "cert_no_hash")
    for idx in inspector.get_indexes("user_reviews"):
        indexed = set(idx.get("column_names") or [])
        name = idx.get("name") or ""
        # 单列索引且落在待删列上 → 先删掉
        if len(indexed) != 1 or not (indexed & set(legacy_cols)):
            continue
        # SQLite 的 UNIQUE 约束会生成 `sqlite_autoindex_*` 隐式索引，
        # 那种索引 DROP 不了（会报「no such index」）；跳过即可 ——
        # 删列时 SQLite 会自动把它一并处理掉。
        if name.startswith("sqlite_autoindex"):
            continue
        if dialect == "mysql":
            sync_conn.execute(text(f"DROP INDEX {name} ON user_reviews"))
        else:
            sync_conn.execute(text(f"DROP INDEX IF EXISTS {name}"))

    # 2. 删掉旧的证件相关列
    for legacy in legacy_cols:
        if legacy in cols:
            sync_conn.execute(text(f"ALTER TABLE user_reviews DROP COLUMN {legacy}"))

    # 3. 补上手机号快照列
    if "phone" not in cols:
        sync_conn.execute(text("ALTER TABLE user_reviews ADD COLUMN phone VARCHAR(20) NULL"))
