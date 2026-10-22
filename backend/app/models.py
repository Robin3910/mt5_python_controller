"""Pydantic 模型：API 入参/出参、领域对象、以及下发给节点的命令构造。"""
from __future__ import annotations

import time
from typing import Any, Optional

from pydantic import BaseModel, Field


# ----------------------------- 节点 -----------------------------
class NodeCreate(BaseModel):
    """创建节点的入参。"""
    name: Optional[str] = None  # 留空则自动生成 "{序号}-{mt5_login}"（序号从 1 起按节点位置递增）
    mt5_login: int = Field(gt=0, description="绑定的 MT5 账户登录号（全局唯一）")
    filters: Optional[dict] = None  # 节点级按币种配置（分发参与、手数策略、轮询顺序）


class NodeUpdate(BaseModel):
    """更新节点的入参（全部可选，仅更新提供的字段；mt5_login 创建后不可改）。"""
    name: Optional[str] = None
    enabled: Optional[bool] = None
    filters: Optional[dict] = None
    # 账户级风控（浮盈亏比等）；结构见 risk_control.normalize_risk
    risk: Optional[dict] = None


class NodePrimaryUpdate(BaseModel):
    """设置 / 取消节点的主节点标记入参。"""
    is_primary: bool


class NodeOut(BaseModel):
    """节点对外展示对象（合并了在线状态与账户登录信息）。"""
    node_id: str
    name: str
    enabled: bool = True
    status: str = "offline"  # online / offline
    filters: Optional[dict] = None
    risk: Optional[dict] = None
    mt5_login: Optional[int] = None
    mt5_server: Optional[str] = None
    # 节点鉴权首包上报的客户端版本；旧版本客户端不上报，为空即未知
    client_version: Optional[str] = None
    client_version_at: Optional[float] = None
    created_at: float = 0
    last_seen: Optional[float] = None
    # 数据归属：空 = 管理员名下
    owner_user_id: Optional[int] = None
    owner_username: Optional[str] = None
    approval_status: str = "approved"
    requested_by_user_id: Optional[int] = None
    requested_by_username: Optional[str] = None
    admin_enable_requested: bool = False
    credential_generation: int = 0
    legacy_allowed: bool = True
    has_credential: bool = False
    # 主节点：每个 owner 维度下唯一；归管理员名下的节点（owner_user_id 为空）共享
    # 同一个「管理员」槽位，最多也只能有一个主节点。
    is_primary: bool = False


class NodeTokenInfo(BaseModel):
    """全局节点接入令牌（所有节点共享）。明文存储，便于管理员复制到各节点 .env。"""
    token: str
    updated_at: float = 0


class WebhookAuthInfo(BaseModel):
    """Webhook 共享 token（.env 的 AUTH_TOKEN），供管理员复制进 TradingView 请求体。"""
    enabled: bool
    token: str


class WebhookAuthUpdate(BaseModel):
    """配置页开关：开启后 /webhook 必须带 AUTH_TOKEN。"""
    enabled: bool


class ClientVersionOut(BaseModel):
    """客户端安装包版本条目。"""
    version: str
    filename: str
    size: int = 0
    sha256: str = ""
    notes: Optional[str] = None
    uploaded_by: str = ""
    created_at: float = 0
    is_current: bool = False       # 是否为当前发布版本
    node_count: int = 0            # 已上报运行该版本的节点数


class ClientReleaseOut(BaseModel):
    """当前发布指针；previous 是服务端回滚的落点。"""
    version: str = ""
    previous: str = ""
    updated_at: float = 0


class ClientVersionListOut(BaseModel):
    """版本清单 + 发布指针 + 节点版本分布。"""
    items: list[ClientVersionOut] = Field(default_factory=list)
    release: ClientReleaseOut = Field(default_factory=ClientReleaseOut)
    unknown_node_count: int = 0    # 未上报版本的节点数（旧客户端或从未上线）


class ClientReleasePayload(BaseModel):
    """发布某个版本；降级为高危动作，必须显式确认。"""
    confirm_downgrade: bool = False


class LotBatch(BaseModel):
    """批量设置节点手数策略。"""
    node_ids: list[str]
    lot_mode: str
    lot: Optional[float] = None


class NodeDispatchRecord(BaseModel):
    """单节点分发/成交明细（信号原始数据 + 本节点处理情况）。"""
    id: int                       # 分发明细行唯一 ID（同一 signal_id 可能有多条，用于前端行级展开）
    signal_id: str
    symbol: Optional[str] = None
    action: Optional[str] = None
    volume: Optional[float] = None
    sl: Optional[float] = None
    tp: Optional[float] = None
    comment: Optional[str] = None
    source_ip: Optional[str] = None
    parsed_ok: Optional[bool] = None
    dispatch_mode: Optional[str] = None
    signal_status: Optional[str] = None
    received_at: Optional[float] = None
    raw_payload: Optional[str] = None
    decided_vol: Optional[float] = None
    gate_result: Optional[str] = None
    skip_reason: Optional[str] = None
    status: str = "pending"
    retcode: Optional[int] = None
    order: Optional[int] = None
    deal: Optional[int] = None
    price: Optional[float] = None
    error: Optional[str] = None
    dispatched_at: Optional[float] = None
    finished_at: Optional[float] = None


class PaginatedNodeDispatches(BaseModel):
    """节点分发/成交明细分页结果。"""
    items: list[NodeDispatchRecord]
    total: int
    page: int
    page_size: int


class DealRecord(BaseModel):
    """单笔 MT5 成交明细。"""
    id: int
    node_id: str
    deal_id: int
    order_id: Optional[int] = None
    position_id: Optional[int] = None
    symbol: Optional[str] = None
    direction: str = "BUY"
    # 出场成交的方向色按「被平掉的那个持仓的方向」取（平多单在 MT5 里是 SELL）
    position_direction: str = "BUY"
    entry_type: str = "IN"
    entry_label: str = "开仓"
    reason: int = 0
    volume: float = 0.0
    price: float = 0.0
    profit: float = 0.0
    swap: float = 0.0
    commission: float = 0.0
    net_profit: float = 0.0
    magic: Optional[int] = None
    comment: Optional[str] = None
    source: str = "terminal"
    source_label: str = "终端"
    signal_id: Optional[str] = None
    task_id: Optional[int] = None
    group_id: Optional[str] = None
    deal_time: int = 0


class DealSummary(BaseModel):
    """成交汇总（统计口径集中在 deal_stats，前端直接展示不要自己再算）。"""
    total_profit: float = 0.0
    profit_rate: float = 0.0
    win_rate: float = 0.0
    # 交易笔数 = 持仓数（按 position_id 聚合），不是成交笔数
    total_trades: int = 0
    win_trades: int = 0
    lose_trades: int = 0
    gross_volume: float = 0.0
    commission_total: float = 0.0
    swap_total: float = 0.0
    # 区间期初权益（收益率的分母）
    base_equity: float = 0.0
    # 期初权益拿不到时收益率为 0 且此标志为 False（前端应显示「暂无数据」）
    profit_rate_valid: bool = True
    # 节点未上线 / 无成交 / 同步未开 —— 与「有数据但全亏」区分
    data_available: bool = False
    updated_at: Optional[int] = None


class PaginatedDeals(BaseModel):
    """成交明细分页结果（summary 与 items 同一 WHERE 条件）。"""
    items: list[DealRecord]
    total: int
    page: int
    page_size: int
    summary: DealSummary = Field(default_factory=DealSummary)


class LeaderPerformance(BaseModel):
    """带单员绩效（由成交聚合而来）。"""
    leader_user_id: int
    window: str = "30d"
    performance: DealSummary = Field(default_factory=DealSummary)
    follower_count: int = 0


class SignalEventDispatch(BaseModel):
    """单条 Webhook 信号在某节点上的处理明细。"""
    id: int
    node_id: str
    node_name: Optional[str] = None
    decided_vol: Optional[float] = None
    gate_result: str = "passed"
    skip_reason: Optional[str] = None
    status: str = "pending"
    retcode: Optional[int] = None
    order: Optional[int] = None
    deal: Optional[int] = None
    price: Optional[float] = None
    error: Optional[str] = None
    dispatched_at: Optional[float] = None
    finished_at: Optional[float] = None


class SignalEventRecord(BaseModel):
    """Webhook 信号事件（原始参数 + 各节点处理情况）。"""
    signal_id: str
    received_at: Optional[float] = None
    source_ip: Optional[str] = None
    raw_payload: Optional[str] = None
    action: Optional[str] = None
    symbol: Optional[str] = None
    volume: Optional[float] = None
    sl: Optional[float] = None
    tp: Optional[float] = None
    comment: Optional[str] = None
    parsed_ok: bool = False
    dispatch_mode: Optional[str] = None
    status: str = "pending"
    source: Optional[str] = None  # tradingview（外部 Webhook）/ manual（中控台手动触发）
    model: Optional[str] = None   # normal（按币种分发）/ strategy（按分组分发）
    dispatches: list[SignalEventDispatch] = Field(default_factory=list)


class PaginatedSignalEvents(BaseModel):
    """Webhook 信号事件分页结果。"""
    items: list[SignalEventRecord]
    total: int
    page: int
    page_size: int


# ----------------------------- 分组 -----------------------------
# Webhook 新增字段 model 的取值：normal（默认，按币种分发）/ strategy（按分组分发）
SIGNAL_MODEL_NORMAL = "normal"
SIGNAL_MODEL_STRATEGY = "strategy"
SIGNAL_MODELS = (SIGNAL_MODEL_NORMAL, SIGNAL_MODEL_STRATEGY)

# 分组分发模式（与中控台一致的两种模式，但作用于整个分组而非单个币种）
GROUP_DISPATCH_MODES = ("sync", "poll")


class GroupNodeRef(BaseModel):
    """分组内的成员节点（合并了在线状态，供分组列表展示）。"""
    node_id: str
    name: Optional[str] = None
    mt5_login: Optional[int] = None
    enabled: bool = True
    status: str = "offline"  # online / offline
    sort_order: int = 0
    # 该成员的挂单是否作为限价监听触发来源（默认关）
    limit_watch_report: bool = False


class GroupCreate(BaseModel):
    """创建分组的入参。"""
    name: str = Field(min_length=1, max_length=64)
    enabled: bool = True
    dispatch_mode: str = "sync"  # sync / poll
    # 信号并发：仅全员同步 + 绑定网格/趋势策略时可用；开启后同组同节点可同时处理多条开仓信号（默认开）
    signal_concurrent_enabled: bool = False
    # 趋势风控：开仓前按全局趋势参数对各节点算信号品种趋势（默认关）
    trend_risk_enabled: bool = False
    # 限价挂单监听：仅绑定趋势策略的分组可开（默认关）；关键字默认 limit，空串表示不按注释过滤
    limit_watch_enabled: bool = False
    limit_watch_keyword: Optional[str] = Field(default=None, max_length=32)
    # 是否开放【AI 交易】广场的跟单（默认关）；只对绑定 tpl_1 的分组有意义
    accept_ai_follow: bool = False
    remark: Optional[str] = None
    # 一对一绑定交易策略；空 / null = 不绑定
    strategy_id: Optional[str] = Field(default=None, max_length=32)
    node_ids: list[str] = Field(default_factory=list)
    # 这些成员打开「限价监听上报」；省略或未列入的成员为关闭。不在 node_ids 里的 id 忽略
    limit_watch_node_ids: Optional[list[str]] = None


class GroupUpdate(BaseModel):
    """更新分组的入参（全部可选，仅更新提供的字段）。"""
    name: Optional[str] = None
    enabled: Optional[bool] = None
    dispatch_mode: Optional[str] = None
    signal_concurrent_enabled: Optional[bool] = None
    trend_risk_enabled: Optional[bool] = None
    limit_watch_enabled: Optional[bool] = None
    limit_watch_keyword: Optional[str] = Field(default=None, max_length=32)
    # 是否开放【AI 交易】广场的跟单（默认关）；只对绑定 tpl_1 的分组有意义
    accept_ai_follow: Optional[bool] = None
    remark: Optional[str] = None
    # 传入空字符串或 null 表示解除绑定；省略字段则不改
    strategy_id: Optional[str] = Field(default=None, max_length=32)
    node_ids: Optional[list[str]] = None  # 传入即整体替换成员列表
    # 传入即按该列表重算成员上报开关（仅对当前成员生效）。省略则：未改成员时保持原开关；
    # 替换成员时，留下的节点保留原开关，新节点默认关闭
    limit_watch_node_ids: Optional[list[str]] = None


class AdoptFollowersResponse(BaseModel):
    """分组 owner 认领跟单节点的结果（跨策略类型 / 品种继承）。

    adopted = 实际新写入的跟单关系；skipped = 因已存在 / 源信息缺失而未写入的条目。
    """
    adopted: list[dict] = Field(default_factory=list)
    skipped: list[dict] = Field(default_factory=list)
    # 参与扫描的来源分组数（不含目标分组自身）
    scanned_groups: int = 0


class LimitWatchLogRecord(BaseModel):
    """分组限价监听日志（落库 + 后台 WS 实时推送）。"""
    id: int
    ts: Optional[float] = None
    group_id: str
    group_name: Optional[str] = None
    node_id: str
    ticket: int = 0
    symbol: Optional[str] = None
    action: Optional[str] = None
    volume: Optional[float] = None
    price: Optional[float] = None
    sl: Optional[float] = None
    tp: Optional[float] = None
    comment: Optional[str] = None
    event: str
    message: str = ""
    detail: Optional[dict] = None


class PaginatedLimitWatchLogs(BaseModel):
    """分组限价监听日志分页。"""
    items: list[LimitWatchLogRecord]
    total: int
    page: int
    page_size: int


class GroupFollowerRef(BaseModel):
    """分组内的跟单节点（跟单员把自己的非主节点放进他人分组）。

    与 GroupNodeRef 严格分离：跟单节点对分组 owner 只读，不可排序 / 移出 /
    开限价监听（那都是 NodeGroupMember 的职责）。
    """
    node_id: str
    name: Optional[str] = None
    mt5_login: Optional[int] = None
    enabled: bool = True
    status: str = "offline"  # online / offline
    # 跟单员（节点所有者）—— 管理员需要知道是谁在跟
    follower_user_id: int
    follower_username: Optional[str] = None
    # 加入跟单的时间（epoch 秒）
    joined_at: Optional[float] = None


class GroupOut(BaseModel):
    """分组对外展示对象（含成员节点与信号计数）。"""
    group_id: str
    name: str
    enabled: bool = True
    dispatch_mode: str = "sync"
    signal_concurrent_enabled: bool = False
    trend_risk_enabled: bool = False
    limit_watch_enabled: bool = False
    # 空串 = 不按注释过滤；缺省展示仍是 limit
    limit_watch_keyword: str = "limit"
    # 是否开放【AI 交易】广场的跟单（只对绑定 tpl_1 的分组有意义）
    accept_ai_follow: bool = False
    strategy_id: Optional[str] = None
    strategy_name: Optional[str] = None
    remark: Optional[str] = None
    created_at: float = 0
    nodes: list[GroupNodeRef] = Field(default_factory=list)
    node_count: int = 0
    # 有效节点数（已启用 + 在线）= owner 成员与跟单节点的合并口径
    online_node_count: int = 0
    # 跟单节点（跟单员放进本分组的节点，只读；与上面的 nodes 互不重叠）
    followers: list[GroupFollowerRef] = Field(default_factory=list)
    follower_count: int = 0
    # 跟单节点中已启用且在线的数量
    online_follower_count: int = 0
    signal_count: int = 0        # 该分组已处理的信号主任务数
    active_task_count: int = 0   # 进行中主任务数（pending/dispatching/running）
    # 已开监听时附带最近若干条；未开启为空列表
    limit_watch_logs: list[LimitWatchLogRecord] = Field(default_factory=list)
    # 数据归属：空 = 管理员名下
    owner_user_id: Optional[int] = None
    owner_username: Optional[str] = None


class GroupTaskDispatchRecord(BaseModel):
    """节点信号任务：策略执行的实际单元，自持魔术号。"""
    id: int
    node_id: str
    symbol: Optional[str] = None
    node_name: Optional[str] = None
    decided_vol: Optional[float] = None
    status: str = "pending"
    skip_reason: Optional[str] = None
    retcode: Optional[int] = None
    order: Optional[int] = None
    deal: Optional[int] = None
    price: Optional[float] = None
    error: Optional[str] = None
    magic: Optional[int] = None
    # —— 策略托管运行期快照 ——
    position_count: int = 0
    # 未成交挂单笔数；限价开仓在成交前只有它
    pending_orders: int = 0
    add_count: int = 0
    total_orders: int = 0
    total_volume: float = 0.0
    realized_profit: float = 0.0
    finish_reason: Optional[str] = None
    dispatched_at: Optional[float] = None
    opened_at: Optional[float] = None
    last_report_at: Optional[float] = None
    finished_at: Optional[float] = None


class GroupSignalTaskRecord(BaseModel):
    """分组信号主任务：分发记录（信号信息 + 下发数据 + 各节点子任务）。

    魔术号在子任务上（各节点独立），主任务本身不持有。
    """
    task_id: int
    signal_id: str
    group_id: str
    group_name: Optional[str] = None
    created_at: Optional[float] = None
    action: Optional[str] = None
    symbol: Optional[str] = None
    volume: Optional[float] = None
    sl: Optional[float] = None
    tp: Optional[float] = None
    comment: Optional[str] = None
    source_ip: Optional[str] = None
    raw_payload: Optional[str] = None
    strategy_id: Optional[str] = None
    strategy_name: Optional[str] = None
    dispatch_mode: str = "sync"
    payload: Optional[dict] = None
    node_ids: list[str] = Field(default_factory=list)
    node_count: int = 0
    status: str = "pending"
    skip_reason: Optional[str] = None
    # —— 策略托管汇总（各子任务累加）——
    total_orders: int = 0
    total_volume: float = 0.0
    realized_profit: float = 0.0
    opened_at: Optional[float] = None
    finished_at: Optional[float] = None
    dispatches: list[GroupTaskDispatchRecord] = Field(default_factory=list)


class PaginatedGroupSignals(BaseModel):
    """分组信号主任务分页结果。"""
    items: list[GroupSignalTaskRecord]
    total: int
    page: int
    page_size: int


class GroupTaskEventRecord(BaseModel):
    """节点策略子任务的执行事件（开仓 / 加仓 / 平仓等关联订单）。"""
    id: int
    task_id: int
    node_id: str
    magic: Optional[int] = None
    created_at: Optional[float] = None
    event_type: str
    symbol: Optional[str] = None
    action: Optional[str] = None
    volume: Optional[float] = None
    price: Optional[float] = None
    order_ticket: Optional[int] = None
    position_count: Optional[int] = None
    total_volume: Optional[float] = None
    profit: Optional[float] = None
    message: Optional[str] = None       # 开单原因（人读的一句话说明）
    detail: Optional[dict] = None       # 计算依据明细：偏离 / 阈值 / 手数公式等逐项参数


# ----------------------------- 策略管理 ----------------------------
class StrategyBatchLevel(BaseModel):
    """分批加仓档位：本规则第几次加仓区间内的间距 / 倍数。

    加仓间距由 calc_type 决定读哪个参数。点数 / 指定价 / ATR / 波幅换算成偏离点数；
    fvg / ob / pdz / fibo 只作标识，触发价来自 TradingView 推送。
    """
    pos_from: int = Field(default=1, ge=0)
    pos_to: int = Field(default=4, ge=0)
    calc_type: str = Field(
        default="point",
        description=(
            "间距计算方式：point=点数 / price=指定价位 / atr=ATR / range=K线波幅 / "
            "fvg / ob / pdz / fibo=TradingView 价位标识（仅逆势）"
        ),
    )
    point: float = Field(default=100, ge=0, description="calc_type=point 时的触发点数")
    price: float = Field(default=0.0, ge=0, description="calc_type=price 时的绝对价位")
    timeframe: str = Field(
        default="M5",
        description="atr / range 的统计周期，或 fvg / ob / pdz / fibo 用来匹配推送的周期标识",
    )
    lot_times: float = Field(default=1.0, ge=0)
    extra_lot: float = Field(default=0.0, ge=0)


class SignalFloatPlRatio(BaseModel):
    """模版1 信号级浮盈亏比。次数耗尽是节点任务内存态，配置里不存 remaining_times。"""
    enabled: bool = False
    ratio: float = Field(default=-20.0, description="触发比例（%）；负=浮亏侧，正=浮盈侧")
    action: str = Field(default="close_all", description="触达后操作，目前仅清该信号全部")
    monitor_mode: str = Field(default="loop", description="loop=循环 / times=指定次数")
    max_times: int = Field(default=1, ge=1, description="指定次数模式下的次数上限")


class SignalLotPlTier(BaseModel):
    min_lot: float = Field(default=0.1, ge=0)
    pl_amount: float = Field(default=50.0, description="盈亏金额；负=亏损侧达阈值")


class SignalLotPlTiers(BaseModel):
    """模版1 信号级分档手数盈亏。"""
    enabled: bool = False
    batch_count: int = Field(default=2, ge=1, le=10)
    close_action: str = Field(default="all", description="all|buy|sell")
    tiers: list[SignalLotPlTier] = Field(
        default_factory=lambda: [
            SignalLotPlTier(min_lot=0.1, pl_amount=50.0),
            SignalLotPlTier(min_lot=0.5, pl_amount=100.0),
        ],
    )


class ManualScatterConfig(BaseModel):
    """模版1 手动分散仓：视觉上像分批最后一档，运行时与分批加仓隔离。"""
    enabled: bool = False
    entry_price: float = Field(
        default=0.0, ge=0,
        description="入场价；成交侧到价后市价开仓（BUY 卖价 / SELL 买价）",
    )
    take_profit: float = Field(default=0.0, ge=0, description="止盈价")
    stop_loss: float = Field(default=0.0, ge=0, description="止损价，0=不设")
    volume: float = Field(default=0.0, ge=0, description="手数；0 且未锁定时由节点按分档手数盈亏反推")
    volume_locked: bool = Field(default=False, description="用户手改手数后不再自动重算")
    close_all_on_tp: bool = Field(
        default=True,
        description="止盈联动清仓（默认开启）：该手动单被券商止盈平掉后，节点平掉该信号其余持仓并收口",
    )


class StrategyRule(BaseModel):
    """单条策略规则，字段按 type 分组使用。

    type=1 逆势加仓 / type=2 顺势加仓（模版1）：point ~ batch_levels、manual_scatter，以及
    策略共享的信号级盈亏控制 float_pl_ratio / lot_pl_tiers；
    type=3 以损定量趋势单（模版2）：risk_amount ~ breakeven_times；
    type=4 网格交易（模版3）：price_lower ~ assist_max_loss。

    保持单一扁平模型是为了让 API 契约、前端类型与 config_json 落库格式都不变；
    服务端 `strategy_templates.normalize_rule` 会按 type 只保留该类型的字段，
    因此传给别的 type 的字段不会被写进库。
    """
    type: int = Field(description="1=逆势加仓，2=顺势加仓，3=以损定量趋势单，4=网格交易")
    status: int = Field(description="0=关闭，1=启用")
    action: str = Field(default="all", description="监控方向 all|buy|sell")
    # --- type=1 / 2：加仓类 ---
    point: float = Field(default=100, ge=0)
    lot_times: float = Field(default=1.0, ge=0)
    extra_lot: float = Field(default=0.0, ge=0)
    max_allow_num: int = Field(default=5, ge=0)
    batch_enabled: bool = Field(default=False, description="是否启用分批加仓")
    batch_action: str = Field(default="all", description="分批监控方向 all|buy|sell")
    batch_count: int = Field(default=0, ge=0, description="分批批数")
    total_lot_limit: float = Field(
        default=0.0, ge=0,
        description="模版1=本规则最多加仓笔数（不含开仓）；模版3=总手数上限；0=不限制",
    )
    batch_levels: list[StrategyBatchLevel] = Field(default_factory=list)
    # 模版1 信号级盈亏控制（顺势/逆势写入相同值；其它 type 规范化时丢弃）
    float_pl_ratio: Optional[SignalFloatPlRatio] = None
    lot_pl_tiers: Optional[SignalLotPlTiers] = None
    # 模版1 手动分散仓（顺势 / 逆势各至多一条；不进 batch_levels）
    manual_scatter: Optional[ManualScatterConfig] = None
    # --- type=3：以损定量趋势单 ---
    risk_amount: float = Field(default=100.0, ge=0, description="风险金额（账户货币）")
    rr_ratio: float = Field(default=2.5, ge=0, description="盈亏比：止盈距离 = 止损距离 × 该值（挂在分散仓）")
    base_ratio: float = Field(default=30.0, ge=0, le=100, description="底仓占总手数的百分比（底仓止盈为 0）")
    add_batches: int = Field(default=10, ge=0, le=50, description="分散仓单数，0=底仓即全仓")
    max_total_lot: float = Field(default=0.0, ge=0, description="总手数上限，0=只受单笔上限约束")
    breakeven_enabled: bool = Field(default=True, description="是否启用保本触发")
    breakeven_times: float = Field(
        default=2.0, ge=0, description="浮盈达到止损距离 × 该倍数时把止损移到保本",
    )
    breakeven_mode: str = Field(
        default="once",
        description="保本监控：once=按次（触发一次后停止）/ loop=循环（可持续监控）",
    )
    entry_mode: str = Field(
        default="market",
        description="开仓方式：market=市价打齐 / limit=在信号入场价挂限价等成交",
    )
    # --- type=4：网格交易 ---
    price_lower: float = Field(default=0.0, ge=0, description="网格区间下限")
    price_upper: float = Field(default=0.0, ge=0, description="网格区间上限")
    grid_count: int = Field(default=10, ge=2, le=200, description="网格数量（2-200）")
    grid_mode: str = Field(
        default="arithmetic", description="网格模式：arithmetic=等差 / geometric=等比",
    )
    grid_side: str = Field(
        default="long", description="网格方向：long=只做多 / short=只做空",
    )
    lot_per_grid: float = Field(default=0.01, ge=0, description="每格手数")
    trigger_price: float = Field(default=0.0, ge=0, description="触发价，0=立即启动")
    stop_lower: float = Field(
        default=0.0, ge=0,
        description="下沿终止价（须低于区间下限；多头为止损、空头为止盈），0=不设",
    )
    stop_upper: float = Field(
        default=0.0, ge=0,
        description="上沿终止价（须高于区间上限；多头为止盈、空头为止损），0=不设",
    )
    close_on_stop: bool = Field(default=True, description="终止时是否清仓")
    prefill_enabled: bool = Field(
        default=True, description="是否按现价上方格位初始建仓",
    )
    trailing_up: bool = Field(
        default=False,
        description="向上追踪：价格越过区间外沿时网格连同止损止盈整体平移一格",
    )
    trailing_max: int = Field(default=0, ge=0, description="最大平移格数，0=不限")
    # type=4 的配置期试算助手：只记录建议值是怎么算出来的，执行层不读
    assist_enabled: bool = Field(
        default=False, description="是否启用网格试算助手（仅配置期，默认关闭）",
    )
    assist_timeframe: str = Field(
        default="H1", description="试算 ATR 所用的 K 线周期",
    )
    assist_atr_mult: float = Field(
        default=1.0, ge=0, description="格距 = ATR × 该倍数",
    )
    assist_spacing: float = Field(
        default=0.0, ge=0, description="手改后的格距，0=沿用 ATR × 倍数的结果",
    )
    assist_max_loss: float = Field(
        default=0.0, ge=0, description="试算用的最大可接受亏损（账户货币）",
    )


class StrategyTemplateOut(BaseModel):
    """策略模版（只读）。"""
    template_id: str
    name: str
    description: str = ""
    rules: list[StrategyRule] = Field(default_factory=list)


class StrategyCreate(BaseModel):
    """新建策略：选择模版 + 名称 + 绑定品种；可选覆盖模版默认规则。"""
    template_id: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=64)
    symbol: str = Field(min_length=1, max_length=32)
    enabled: bool = True
    remark: Optional[str] = None
    # 传入则用自定义规则覆盖模版默认值；不传则复制模版默认规则
    rules: Optional[list[StrategyRule]] = None


class StrategyUpdate(BaseModel):
    """更新策略（全部可选）。"""
    name: Optional[str] = None
    symbol: Optional[str] = None
    enabled: Optional[bool] = None
    remark: Optional[str] = None
    rules: Optional[list[StrategyRule]] = None


class StrategyOut(BaseModel):
    """策略对外展示对象。"""
    strategy_id: str
    name: str
    template_id: str
    template_name: str
    symbol: str
    enabled: bool = True
    rules: list[StrategyRule] = Field(default_factory=list)
    remark: Optional[str] = None
    created_at: float = 0
    # 数据归属：空 = 管理员名下
    owner_user_id: Optional[int] = None
    owner_username: Optional[str] = None


class AuditRecord(BaseModel):
    """操作审计记录（含操作前后数据）。"""
    id: int
    ts: Optional[float] = None
    operator: str
    action: str
    target: Optional[str] = None
    params: Optional[dict] = None
    result: str = "ok"
    ip: Optional[str] = None
    category: Optional[str] = None  # console / node / system
    before: Optional[Any] = None
    after: Optional[Any] = None


class PaginatedAudits(BaseModel):
    """操作审计分页结果。"""
    items: list[AuditRecord]
    total: int
    page: int
    page_size: int


# --------------------------- 账户 ---------------------------
class Position(BaseModel):
    """单个持仓。"""
    ticket: int
    symbol: str
    type: str  # BUY / SELL
    volume: float
    price_open: float = 0
    price_current: float = 0
    sl: float = 0      # 止损价（0 表示未设置）
    tp: float = 0      # 止盈价（0 表示未设置）
    profit: float = 0
    magic: int = 0
    comment: str = ""
    time: float = 0


class QuoteInfo(BaseModel):
    """单个品种的实时报价。"""
    bid: float = 0       # 买价
    ask: float = 0       # 卖价
    mid: float = 0         # 中间价（供区间过滤）
    change: float = 0    # 日变化 %（MT5 SYMBOL_PRICE_CHANGE，相对昨收，与终端 Daily Change 一致）


class AccountSnapshot(BaseModel):
    """节点上报的账户快照。"""
    node_id: Optional[str] = None
    login: Optional[int] = None
    server: Optional[str] = None
    balance: float = 0
    equity: float = 0
    margin: float = 0
    free_margin: float = 0
    leverage: int = 0
    positions: list[Position] = Field(default_factory=list)
    prices: dict[str, float] = Field(default_factory=dict)  # 品种 -> 中间价（供区间过滤）
    quotes: dict[str, QuoteInfo] = Field(default_factory=dict)  # 品种 -> 完整报价
    updated_at: float = Field(default_factory=lambda: time.time())
    # 券商 MT5 服务器相对真实 UTC 的偏移（秒）。IC Markets 夏令时约为 +10800。
    server_time_offset: Optional[int] = None


# ---------------------------- 配置 ---------------------------
class IntervalRule(BaseModel):
    """单条价格区间规则：在 [low, high] 内允许哪些方向。"""
    low: float
    high: float
    allow: list[str] = Field(default_factory=list)  # BUY / SELL 的子集


class SymbolFilter(BaseModel):
    """某品种的多区间过滤与分发配置（全局 filters 键值）。"""
    enabled: bool = True  # False：拒收该品种全部信号（含 Webhook CLOSE；手动平仓除外）
    allow_buy: bool = True
    allow_sell: bool = True
    dispatch_mode: str = "sync"  # sync / poll
    position_scope: str = "symbol"  # symbol / account
    default_action: str = "block"  # 不在任何区间时：block 拦截 / pass 放行
    lot_enabled: bool = False  # 是否启用该品种的全局手数
    lot: float = 0.01  # 该品种全局手数（lot_enabled 时生效）
    intervals: list[IntervalRule] = Field(default_factory=list)


class NodeSymbolDispatchRule(BaseModel):
    """节点 filters 中单个品种的配置。"""
    follow_sync: bool = True
    follow_poll: bool = True
    lot_mode: str = "fixed"  # global / fixed / signal
    lot: Optional[float] = 0.01
    poll_order: int = 0


# ------------------------- 平仓请求 -----------------------
class CloseRequest(BaseModel):
    """远程平仓请求。"""
    target: str = "all"  # all（全平）/ symbol（按品种）/ ticket（按订单）
    symbol: Optional[str] = None
    ticket: Optional[int] = None


class CloseBatchRequest(CloseRequest):
    """对指定节点批量平仓。"""
    node_ids: list[str] = Field(min_length=1)


# ------------------------- 删挂单请求 -----------------------
class CancelPendingRequest(BaseModel):
    """远程批量撤挂单请求（只撤未成交挂单，不动持仓）。"""
    # all（默认，撤光）/ manual（只撤手工单）/ strategy（只撤指定魔术号，暂不开放）
    scope: str = "all"
    magics: Optional[list[int]] = None


# ----------------------- 中控台手动触发 -----------------------
class ManualSignalRequest(BaseModel):
    """后台手动触发的信号（复用 Webhook 分发流程）。

    volume 只有开仓（BUY / SELL）才必填，由接口层按 action 校验：CLOSE 是终止
    指令，手数没有意义。stop_loss / take_profit / entry_price / comment 对齐
    Webhook 的同名字段。

    type=smc-data 时不走开仓分发，改为推送 SMC 价位：此时 action 可空，
    用 tf + data，分组定向仍用 group_ids。
    """
    symbol: str = Field(min_length=1)
    action: str = ""  # BUY / SELL / CLOSE；SMC 价位推送可空
    type: Optional[str] = None  # smc-data：TradingView 价位推送
    tf: Optional[str] = None
    data: list[dict] = Field(default_factory=list)
    volume: Optional[float] = Field(default=None, gt=0)
    model: Optional[str] = None  # normal（默认）/ strategy
    stop_loss: Optional[float] = Field(default=None, gt=0)
    take_profit: Optional[float] = Field(default=None, gt=0)
    # 限价开仓的挂单价，对应 Webhook 的 limit_price；只有配成限价的模版2 会用它
    entry_price: Optional[float] = Field(default=None, gt=0)
    comment: Optional[str] = Field(default=None, max_length=64)
    # 策略模版定向（仅 strategy 链路）：只发给绑定了这些模版的分组；空 = 不限制
    template_ids: list[str] = Field(default_factory=list)
    # 分组定向（仅 strategy 链路）：只发给 ID 在列表内的分组；空 = 不限制
    group_ids: list[str] = Field(default_factory=list)
    # 节点定向（仅 strategy 链路）：把信号限制到指定节点；空 = 不限制（按分组参与者全量）。
    # Webhook 同名字段 node_ids；用于 OrderDialog「带单」关闭时把信号只推到主节点。
    node_ids: list[str] = Field(default_factory=list)


# 清空交易记录时前端/调用方必须原样提交的确认词
PURGE_TRADE_LOGS_CONFIRM = "清空交易记录"


class PurgeTradeLogsRequest(BaseModel):
    """清空全部交易日志/记录表；需提交确认词防止误触。"""
    confirm: str = Field(description=f"必须为「{PURGE_TRADE_LOGS_CONFIRM}」")


# ----------------------------- 交易页一键触发 -----------------------------
STRATEGY_TYPE_TREND_MARKET = "trend_market"
STRATEGY_TYPE_TREND_LIMIT = "trend_limit"
STRATEGY_TYPE_GRID = "grid"
TRADING_STRATEGY_TYPES = (
    STRATEGY_TYPE_TREND_MARKET,
    STRATEGY_TYPE_TREND_LIMIT,
    STRATEGY_TYPE_GRID,
)


class QuickTriggerRequest(BaseModel):
    """交易页一键触发：主节点 + 策略类型 + 开仓参数。"""
    node_id: str = Field(min_length=1, max_length=32)
    strategy_type: str = Field(description="trend_market / trend_limit / grid")
    symbol: str = Field(min_length=1, max_length=32)
    volume: float = Field(gt=0)
    sl: Optional[float] = Field(default=None, gt=0)
    tp: Optional[float] = Field(default=None, gt=0)
    # 限价开仓必填；其它模式忽略
    entry_price: Optional[float] = Field(default=None, gt=0)
    # 网格模式必填（all/buy/sell，默认 long=all=long）；
    # 趋势模式忽略（后端根据现价/SL 自动判断）
    direction: Optional[str] = Field(default=None)
    remark: Optional[str] = Field(default=None, max_length=64)
    # 网格参数
    price_lower: Optional[float] = Field(default=None, ge=0)
    price_upper: Optional[float] = Field(default=None, ge=0)
    grid_count: Optional[int] = Field(default=None, ge=2, le=200)
    grid_mode: Optional[str] = Field(default=None)
    grid_side: Optional[str] = Field(default=None)
    lot_per_grid: Optional[float] = Field(default=None, gt=0)
    prefill_enabled: Optional[bool] = Field(default=None)
    trigger_price: Optional[float] = Field(default=None, ge=0)
    stop_loss: Optional[float] = Field(default=None, ge=0)
    stop_profit: Optional[float] = Field(default=None, ge=0)
    trailing_up: Optional[bool] = Field(default=None)
    assist_max_loss: Optional[float] = Field(default=None, ge=0)


class QuickTriggerResult(BaseModel):
    """一键触发结果。"""
    group_id: str
    strategy_id: str
    result: dict  # webhook.process_signal 的返回体


class PurgeTradeLogsResult(BaseModel):
    """清空交易记录的结果：各表删除行数与 Redis 运行态清理数。"""
    deleted: dict[str, int]
    redis_cleared: int
    total_deleted: int
    # 清空前已下发终止指令的策略子任务数，以及因节点离线未能下发的数量
    strategies_stopped: int = 0
    strategies_unreachable: int = 0


# ----------------------------- 鉴权 ----------------------------
class LoginRequest(BaseModel):
    username: str
    password: str


class ChangePasswordRequest(BaseModel):
    """修改当前登录用户的密码（需携带当前密码）。"""
    current_password: str
    new_password: str


class Login2FARequest(BaseModel):
    login_token: str
    totp_code: str


class TwoFACodeRequest(BaseModel):
    totp_code: str


class TwoFAPasswordRequest(BaseModel):
    password: str
    totp_code: Optional[str] = None


# ----------------------------- 用户与权限 ----------------------------
USERNAME_PATTERN = r"^[A-Za-z0-9_.\-]{3,32}$"
ROLE_CODE_PATTERN = r"^[a-z][a-z0-9_]{1,31}$"


class RoleRef(BaseModel):
    id: int
    code: str
    name: str
    enabled: bool = True


class MeOut(BaseModel):
    """当前登录用户：前端据 menus 渲染导航与路由守卫，据 is_admin 显示管理员操作，
    据 capabilities 判断能否使用 AI 智能加仓策略等细粒度功能。"""
    user_id: int
    username: str
    display_name: Optional[str] = None
    is_admin: bool = False
    roles: list[RoleRef] = Field(default_factory=list)
    menus: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)


class MenuOut(BaseModel):
    """菜单注册表条目；assignable=False 的只属于超级管理员，不可分配给其它角色。"""
    code: str
    name: str
    path: str
    assignable: bool


class RoleOut(BaseModel):
    id: int
    code: str
    name: str
    is_builtin: bool = False
    enabled: bool = True
    remark: Optional[str] = None
    menus: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    user_count: int = 0
    created_at: Optional[float] = None


class CapabilityOut(BaseModel):
    """能力注册表条目；角色编辑页据此列出可勾选项。"""

    code: str
    name: str


class RoleCreate(BaseModel):
    code: str = Field(pattern=ROLE_CODE_PATTERN, description="小写字母开头，2~32 位字母数字下划线")
    name: str = Field(min_length=1, max_length=64)
    remark: Optional[str] = Field(default=None, max_length=255)
    enabled: bool = True
    menus: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)


class RoleUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=64)
    remark: Optional[str] = Field(default=None, max_length=255)
    enabled: Optional[bool] = None


class RoleMenusUpdate(BaseModel):
    menus: list[str] = Field(default_factory=list)


class RoleCapabilitiesUpdate(BaseModel):
    capabilities: list[str] = Field(default_factory=list)


# ── 带单（follow）────────────────────────────────────────────
class FollowLeaderSummary(BaseModel):
    """带单员列表 / 详情里单个带单员的摘要。"""
    leader_user_id: int
    username: str
    display_name: Optional[str] = None
    primary_node: Optional[dict] = None
    followable_group_count: int = 0
    follower_node_count: int = 0
    my_follow_group_count: int = 0
    my_follow_node_count: int = 0
    marketable: bool = True
    # —— 绩效（由历史成交聚合，与订单历史页同一套口径）——
    # 口径 = **主节点**：主节点就是带单源，跟单员收到的是它的信号，
    # 广场上给他看的绩效也必须来自同一个账户。混进名下其它节点（闲置盘、
    # 跟单测试盘）会让选单依据与他实际承担的风险脱节。
    # 聚合绩效公开可见：带单广场是公开的，绩效就是给潜在跟单者看的。
    # 没有主节点或主节点无成交 -> data_available=False（前端显示「暂无数据」）
    performance: DealSummary = Field(default_factory=DealSummary)
    # 当前统计窗口（7d / 30d / 90d / all）
    perf_window: str = "30d"
    # 跟单人数（来自跟单关系表，非订单数据）
    follower_count: int = 0
    # 聚合缓存是否命中（未命中 = 该带单员暂无成交数据）
    perf_available: bool = False


class FollowLeaderListResponse(BaseModel):
    leaders: list[FollowLeaderSummary]


class FollowGroupDetail(BaseModel):
    """带单员详情页的分组项。"""
    group_id: str
    name: Optional[str] = None
    symbol: Optional[str] = None
    strategy_id: Optional[str] = None
    dispatch_mode: str = "sync"
    signal_concurrent_enabled: bool = False
    trend_risk_enabled: bool = False
    enabled: bool = True
    member_count: int = 0
    follower_count: int = 0
    online_count: int = 0
    active_task_count: int = 0
    signal_count: int = 0
    my_follow_node_ids: list[str] = Field(default_factory=list)
    selectable: bool = True
    unselectable_reason: Optional[str] = None


class FollowDetailOut(BaseModel):
    leader: FollowLeaderSummary
    groups: list[FollowGroupDetail]
    # 不在普通跟单市场内的分组（AI 智能加仓策略等）：仅提示，不可选
    hidden_groups: list[FollowGroupDetail] = Field(default_factory=list)
    my_nodes: list[dict] = Field(default_factory=list)


class FollowJoinRequest(BaseModel):
    leader_user_id: int = Field(ge=1)
    group_ids: list[str] = Field(min_length=1, max_length=50)
    node_ids: list[str] = Field(min_length=1, max_length=50)


class FollowJoinResponse(BaseModel):
    joined: list[dict] = Field(default_factory=list)
    skipped: list[dict] = Field(default_factory=list)
    leader_primary_node_id: Optional[str] = None


class FollowLeaveRequest(BaseModel):
    leader_user_id: int = Field(ge=1)
    group_ids: list[str] = Field(min_length=1, max_length=50)
    node_ids: list[str] = Field(min_length=1, max_length=50)
    close_tasks: bool = False


class FollowLeaveResponse(BaseModel):
    removed: list[dict] = Field(default_factory=list)
    closing: list[dict] = Field(default_factory=list)
    forced: list[dict] = Field(default_factory=list)
    warning: Optional[str] = None
    # 跨市场跳过的分组（关系**未**被删除）：[{group_id, group_name, reason}]
    skipped: list[dict] = Field(default_factory=list)


class FollowMyGroup(BaseModel):
    """我的跟单里一项（按分组维度）。"""
    group_id: str
    group_name: Optional[str] = None
    symbol: Optional[str] = None
    strategy_id: Optional[str] = None
    marketable: bool = True
    unmarketable_reason: Optional[str] = None
    active_task_count: int = 0
    my_nodes: list[dict] = Field(default_factory=list)


class FollowMyLeader(BaseModel):
    leader: FollowLeaderSummary
    groups: list[FollowMyGroup]


class FollowMeResponse(BaseModel):
    following: list[FollowMyLeader] = Field(default_factory=list)
    summary: dict = Field(default_factory=dict)


# ── AI 交易（ai_follow）────────────────────────────────────────
class AiRobotSummary(BaseModel):
    """机器人广场里一个「机器人」的摘要。

    展示单位 = 主节点：一位带单员的主节点即一个机器人，其名下绑定 tpl_1
    且已开放 AI 跟单的分组挂在它下面。
    """
    leader_user_id: int
    username: str
    display_name: Optional[str] = None
    primary_node: Optional[dict] = None
    # 已开放 AI 跟单的 tpl_1 分组数
    ai_group_count: int = 0
    # 被多少个节点跟随（全量跟单关系）
    follower_node_count: int = 0
    my_follow_group_count: int = 0
    my_follow_node_count: int = 0
    # 机器人对外公开的风险上限（取各分组最大值）—— 跟单员不控手数，
    # 必须能一眼看到风险量级（决策：不控手数的对价是公开风险参数）
    max_allow_num: Optional[int] = None
    total_lot_limit: Optional[float] = None
    # 该机器人是否已对我关闭 AI 跟单（能力被撤销 / 分组下架）
    marketable: bool = True
    unmarketable_reason: Optional[str] = None
    # —— 绩效（由主节点的成交聚合而来）——
    # 口径 = **主节点**（不是名下全部节点）：AI 机器人的展示单位就是主节点
    # （见类注释），用户点进去看的也是主节点账号的实盘，用全部节点会把
    # 与该机器人无关的账户盈亏混进来，横向对比就失真了。
    performance: DealSummary = Field(default_factory=DealSummary)
    # 当前统计窗口（7d / 30d / 90d / all）
    perf_window: str = "30d"
    # 跟单人数（AI 市场内跟了该机器人的去重节点数）
    follower_count: int = 0


class AiRobotListResponse(BaseModel):
    robots: list[AiRobotSummary] = Field(default_factory=list)


class AiRobotGroupDetail(BaseModel):
    """机器人详情页的分组项（只含已开放 AI 跟单的 tpl_1 分组）。"""
    group_id: str
    name: Optional[str] = None
    symbol: Optional[str] = None
    strategy_id: Optional[str] = None
    strategy_name: Optional[str] = None
    strategy_template: Optional[str] = None
    enabled: bool = True
    member_count: int = 0
    follower_count: int = 0
    online_count: int = 0
    active_task_count: int = 0
    signal_count: int = 0
    my_follow_node_ids: list[str] = Field(default_factory=list)
    selectable: bool = True
    unselectable_reason: Optional[str] = None
    # AI 特有的风险参数（必须公开，跟单员不控手数）
    risk_amount: Optional[float] = None
    max_allow_num: Optional[int] = None
    total_lot_limit: Optional[float] = None
    lot_times: Optional[float] = None
    point: Optional[float] = None
    # AI 智能加仓策略不支持信号并发：同一节点同时只跑一单
    signal_concurrent_enabled: bool = False
    concurrent_unavailable_reason: Optional[str] = None


class AiRobotDetailResponse(BaseModel):
    robot: AiRobotSummary
    groups: list[AiRobotGroupDetail] = Field(default_factory=list)
    my_nodes: list[dict] = Field(default_factory=list)


class AiMyGroup(BaseModel):
    """我的 AI 跟单里一项（按分组维度）。"""
    group_id: str
    group_name: Optional[str] = None
    symbol: Optional[str] = None
    strategy_id: Optional[str] = None
    marketable: bool = True
    unmarketable_reason: Optional[str] = None
    active_task_count: int = 0
    # 批次已用 / 批次上限 —— AI 跟单员最需要盯的指标（决定还剩多少加仓空间）
    batch_used: int = 0
    batch_limit: int = 0
    my_nodes: list[dict] = Field(default_factory=list)


class AiMyRobot(BaseModel):
    robot: AiRobotSummary
    groups: list[AiMyGroup]


class AiMeResponse(BaseModel):
    following: list[AiMyRobot] = Field(default_factory=list)
    summary: dict = Field(default_factory=dict)


class AiJoinRequest(BaseModel):
    leader_user_id: int = Field(ge=1)
    group_ids: list[str] = Field(min_length=1, max_length=50)
    node_ids: list[str] = Field(min_length=1, max_length=50)


class AiJoinResponse(BaseModel):
    joined: list[dict] = Field(default_factory=list)
    skipped: list[dict] = Field(default_factory=list)
    leader_primary_node_id: Optional[str] = None


class AiLeaveRequest(BaseModel):
    leader_user_id: int = Field(ge=1)
    group_ids: list[str] = Field(min_length=1, max_length=50)
    node_ids: list[str] = Field(min_length=1, max_length=50)
    # False = 在跑任务不停止，继续按策略运行至结束（与带单同一口径）
    close_tasks: bool = False


class AiLeaveResponse(BaseModel):
    removed: list[dict] = Field(default_factory=list)
    closing: list[dict] = Field(default_factory=list)
    forced: list[dict] = Field(default_factory=list)
    warning: Optional[str] = None
    # 跨市场跳过的分组（关系**未**被删除）：[{group_id, group_name, reason}]
    skipped: list[dict] = Field(default_factory=list)


class UserOut(BaseModel):
    id: int
    username: str
    display_name: Optional[str] = None
    is_active: bool = True
    is_admin: bool = False
    totp_enabled: bool = False
    roles: list[RoleRef] = Field(default_factory=list)
    node_ids: list[str] = Field(default_factory=list)
    group_count: int = 0
    strategy_count: int = 0
    created_at: Optional[float] = None
    # 启用角色的能力码并集（如 ["follow_leader"]）；管理员固定全部能力。供用户列表「能力」列展示，
    # **不**参与鉴权（鉴权走 Principal，与 menus 是同一口径：只用于显示控制）。
    capabilities: list[str] = Field(default_factory=list)


class UserCreate(BaseModel):
    username: str = Field(pattern=USERNAME_PATTERN, description="3~32 位字母数字及 _ . -")
    password: str = Field(min_length=6, max_length=128)
    display_name: Optional[str] = Field(default=None, max_length=64)
    is_active: bool = True
    # 为空时绑定内置「普通用户」角色
    role_ids: list[int] = Field(default_factory=list)


class UserUpdate(BaseModel):
    display_name: Optional[str] = Field(default=None, max_length=64)
    is_active: Optional[bool] = None


class UserRolesUpdate(BaseModel):
    role_ids: list[int] = Field(default_factory=list)


class UserPasswordReset(BaseModel):
    new_password: str = Field(min_length=6, max_length=128)


class UserNodesUpdate(BaseModel):
    """整体替换用户名下节点。节点仍在分组里时需 confirm=True 才会把它移出分组。"""
    node_ids: list[str] = Field(default_factory=list)
    confirm: bool = False


def build_open_command(signal_id: str, action: str, symbol: str, volume: float,
                       stop_loss: Optional[float], take_profit: Optional[float],
                       comment: str = "", magic: Optional[int] = None) -> dict[str, Any]:
    """构造下发给节点的“开仓”命令。"""
    return {
        "cmd": "open",
        "signal_id": signal_id,
        "action": action,
        "symbol": symbol,
        "volume": volume,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "comment": comment,
        "magic": magic,
    }


def build_close_command(signal_id: str, target: str, symbol: Optional[str] = None,
                        ticket: Optional[int] = None) -> dict[str, Any]:
    """构造下发给节点的“平仓”命令。"""
    return {
        "cmd": "close",
        "signal_id": signal_id,
        "close_target": target,
        "close_symbol": symbol,
        "close_ticket": ticket,
    }


# ------------------------- 删挂单 -----------------------
CANCEL_PENDING_SCOPES = ("all", "manual", "strategy")


def build_cancel_all_command(scope: str = "all", *,
                            magics: Optional[list[int]] = None) -> dict[str, Any]:
    """构造下发给节点的“批量撤挂单”命令（不动持仓）。

    req_id 由 `node_rpc.request` 注入。
    """
    want = scope if scope in CANCEL_PENDING_SCOPES else "all"
    cmd: dict[str, Any] = {"cmd": "cancel_pending_all", "scope": want}
    if magics:
        cmd["magics"] = [int(m) for m in magics]
    return cmd
