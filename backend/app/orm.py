"""SQLAlchemy ORM 模型（生产用 MySQL，本地开发用 SQLite）。

承担“持久化”职责：节点账本、后台用户、操作审计、信号历史、分发明细，
以及 strategy 信号的分组账本与主任务链路（node_group* / group_signal_task*）。
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
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON

# 自增主键类型：MySQL 用 BIGINT AUTO_INCREMENT；SQLite 只有声明为 INTEGER 的主键
# 才是 rowid 别名（BIGINT 主键不会自增，插入会撞 NOT NULL），故按方言取变体。
AutoPK = BigInteger().with_variant(Integer, "sqlite")


class Base(DeclarativeBase):
    pass


class Node(Base):
    """节点账本（权威数据源；Redis 仅作缓存）。

    自 v0.2 起鉴权令牌改为全局共享（见 SystemSetting），节点身份用 mt5_login 唯一标识。
    """
    __tablename__ = "nodes"

    node_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    lot_mode: Mapped[str] = mapped_column(String(16), default="global")
    lot: Mapped[float | None] = mapped_column(Float, nullable=True)
    follow_sync: Mapped[bool] = mapped_column(Boolean, default=True)
    follow_poll: Mapped[bool] = mapped_column(Boolean, default=True)
    poll_order: Mapped[int] = mapped_column(Integer, default=0)
    filters_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # 节点级过滤
    risk_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # 账户级风控
    # mt5_login 自 v0.2 起作为节点的业务唯一键（不可为空、全局唯一）
    mt5_login: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    mt5_server: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class SystemSetting(Base):
    """系统级 key/value 配置（持久化）。

    当前用途：`node_token` — 所有节点共享的接入令牌（明文，便于管理员复制到各节点 .env）。
    """
    __tablename__ = "system_setting"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
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
    """操作审计：谁、在何时、对什么、做了什么、结果如何；可附带操作前后数据。"""
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(AutoPK, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    operator: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str | None] = mapped_column(String(64), nullable=True)
    params_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    result: Mapped[str] = mapped_column(String(16))
    ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    # console（中控台）/ node（节点）/ system（其它）
    category: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    before_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # 操作前数据
    after_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)   # 操作后数据


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
    # 信号来源：tradingview（外部 Webhook）/ manual（中控台手动触发）；历史为空按 tradingview 展示
    source: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # 信号处理模型：normal（按币种分发，默认）/ strategy（按分组分发）；历史为空按 normal 展示
    model: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)


class SignalDispatch(Base):
    """分发明细：单条信号 × 单个节点的一次执行结果。"""
    __tablename__ = "signal_dispatch"

    id: Mapped[int] = mapped_column(AutoPK, primary_key=True, autoincrement=True)
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


# ===================== strategy 信号：分组账本与主任务链路 =====================
# 以下四张表只服务于 model=strategy 的分组分发流程，与上面的 signal_dispatch
# （model=normal 的按币种分发）互不读写，保证两条链路的规则与数据完全隔离。


class NodeGroup(Base):
    """节点分组（strategy 信号的分发单元；权威数据源，Redis 仅作缓存）。

    分组自带分发模式（sync / poll），不按币种区分——这是与中控台按币种配置的
    根本差别，也是 strategy 与 normal 两条链路隔离的关键。
    每个分组最多绑定一个交易策略（一对一）；strategy_id 为空表示未绑定。
    """
    __tablename__ = "node_group"

    group_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # sync（全员同步）/ poll（组内轮转，一个信号只由一个节点领取）
    dispatch_mode: Mapped[str] = mapped_column(String(8), default="sync")
    # 一对一绑定 TradingStrategy；unique 保证同一策略不能挂到多个分组
    strategy_id: Mapped[str | None] = mapped_column(String(32), nullable=True, unique=True, index=True)
    remark: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class NodeGroupMember(Base):
    """分组成员：节点加入分组的关联关系（一个节点可同时属于多个分组）。"""
    __tablename__ = "node_group_member"
    __table_args__ = (UniqueConstraint("group_id", "node_id", name="uq_group_node"),)

    id: Mapped[int] = mapped_column(AutoPK, primary_key=True, autoincrement=True)
    group_id: Mapped[str] = mapped_column(String(32), index=True)
    node_id: Mapped[str] = mapped_column(String(32), index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)  # 组内轮询顺序（越小越先）
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class GroupSignalTask(Base):
    """分组信号主任务：一条 strategy 信号在一个分组内的**分发记录**。

    主任务本身不是执行单元、不持有魔术号、也不参与并发互斥——真正执行策略的是
    下面的 GroupTaskDispatch（节点子任务），每个节点独立持有自己的魔术号。
    这里只记录「这条信号在这个分组命中了哪些节点、整体进展如何」，status 由各
    子任务状态汇总而来（见 group_rules.aggregate_task_status）。
    """
    __tablename__ = "group_signal_task"

    task_id: Mapped[int] = mapped_column(AutoPK, primary_key=True, autoincrement=True)
    signal_id: Mapped[str] = mapped_column(String(32), index=True)
    group_id: Mapped[str] = mapped_column(String(32), index=True)
    group_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    # —— 信号信息 ——
    action: Mapped[str | None] = mapped_column(String(8), nullable=True)
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    sl: Mapped[float | None] = mapped_column(Float, nullable=True)
    tp: Mapped[float | None] = mapped_column(Float, nullable=True)
    comment: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    raw_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    # —— 绑定策略（触发时快照，运行期不受策略后续编辑影响）——
    strategy_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    strategy_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    strategy_snapshot_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # —— 分组信息与下发数据 ——
    dispatch_mode: Mapped[str] = mapped_column(String(8), default="sync")
    payload_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)   # 实际下发给节点的命令
    node_ids_json: Mapped[list | None] = mapped_column(JSON, nullable=True)  # 下发节点 ID 列表
    node_count: Mapped[int] = mapped_column(Integer, default=0)
    # pending / dispatching / running / done / partial / failed / skipped
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    skip_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # —— 策略运行期汇总（各子任务累加）——
    opened_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    total_orders: Mapped[int] = mapped_column(Integer, default=0)
    total_volume: Mapped[float] = mapped_column(Float, default=0.0)
    realized_profit: Mapped[float] = mapped_column(Float, default=0.0)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class GroupTaskDispatch(Base):
    """节点信号任务：主任务 × 节点，一对多，是策略执行的实际单元。

    `magic = NODE_TASK_MAGIC_BASE + id`，即每个节点持有独立魔术号，因此一个魔术号
    全局唯一地对应一次节点执行，MT5 订单可直接反查到本行（历史数据里存在多节点
    共用魔术号的旧记录，所以 magic 上不加唯一索引）。

    生命周期：pending -> sent -> opened -> running -> closing -> done。
    完成判定默认是「该节点上 magic 关联的持仓全部平掉」，由节点主动上报，
    服务端再用账户快照对账兜底（见 group_persist.reconcile_*）。
    网格等策略空仓是常态时 hold_when_empty=True，对账不会因无持仓强制收口。

    并发互斥也落在这一层：子任务处于非终态时，(node_id, symbol) 会持有一个 Redis
    占位，同一节点同品种不会被重复下发（不同品种可并行）。
    """
    __tablename__ = "group_task_dispatch"

    id: Mapped[int] = mapped_column(AutoPK, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(BigInteger, index=True)
    signal_id: Mapped[str] = mapped_column(String(32), index=True)
    group_id: Mapped[str] = mapped_column(String(32), index=True)
    node_id: Mapped[str] = mapped_column(String(32), index=True)
    # 冗余存一份品种：释放节点互斥占位、按品种对账时不必回查主任务
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    magic: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    decided_vol: Mapped[float | None] = mapped_column(Float, nullable=True)
    # pending / sent / opened / running / closing / done / failed / skipped / offline
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    skip_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    retcode: Mapped[int | None] = mapped_column(Integer, nullable=True)
    order_ticket: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    deal: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    error: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # —— 策略运行期实时快照 ——
    position_count: Mapped[int] = mapped_column(Integer, default=0)   # 当前 magic 持仓笔数
    add_count: Mapped[int] = mapped_column(Integer, default=0)        # 已加仓次数
    total_orders: Mapped[int] = mapped_column(Integer, default=0)     # 累计下单笔数
    total_volume: Mapped[float] = mapped_column(Float, default=0.0)   # 累计手数
    realized_profit: Mapped[float] = mapped_column(Float, default=0.0)
    finish_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 网格等策略空仓是常态：为 True 时账户快照对账不会因无持仓强制收口
    hold_when_empty: Mapped[bool] = mapped_column(Boolean, default=False)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_report_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class GroupTaskEvent(Base):
    """子任务的策略执行事件流：开仓 / 加仓 / 平仓 / 异常。

    只记录状态变更事件；纯行情心跳不落库（只更新子任务的实时快照字段），
    否则长周期任务会把表写爆。
    """
    __tablename__ = "group_task_event"

    id: Mapped[int] = mapped_column(AutoPK, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(BigInteger, index=True)
    node_id: Mapped[str] = mapped_column(String(32), index=True)
    magic: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    # open / add_counter / add_trend / grid_add / grid_shift /
    # close_partial / close_all / error / resume
    event_type: Mapped[str] = mapped_column(String(16), index=True)
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True)
    action: Mapped[str | None] = mapped_column(String(8), nullable=True)
    volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    order_ticket: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    position_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    message: Mapped[str | None] = mapped_column(String(255), nullable=True)
    detail_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)


# ===================== 策略管理（加仓规则实例） =====================


class TradingStrategy(Base):
    """交易策略实例：基于策略模版创建，绑定品种，保存一份可独立修改的规则配置。"""
    __tablename__ = "trading_strategy"

    strategy_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    template_id: Mapped[str] = mapped_column(String(32), index=True)
    template_name: Mapped[str] = mapped_column(String(64))
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # 规则列表：逆势/顺势各一条，含可选分批档位 batch_levels
    # [{type, status, action, point, lot_times, extra_lot, max_allow_num,
    #   batch_enabled, batch_action, batch_count, total_lot_limit, batch_levels}, ...]
    config_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    remark: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
