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
    # 趋势面板参数（只影响展示口径）；结构见 trend_indicators.normalize_config
    trend: Optional[dict] = None


class NodeOut(BaseModel):
    """节点对外展示对象（合并了在线状态与账户登录信息）。"""
    node_id: str
    name: str
    enabled: bool = True
    status: str = "offline"  # online / offline
    filters: Optional[dict] = None
    risk: Optional[dict] = None
    trend: Optional[dict] = None
    mt5_login: Optional[int] = None
    mt5_server: Optional[str] = None
    created_at: float = 0
    last_seen: Optional[float] = None


class NodeTokenInfo(BaseModel):
    """全局节点接入令牌（所有节点共享）。明文存储，便于管理员复制到各节点 .env。"""
    token: str
    updated_at: float = 0


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


class GroupCreate(BaseModel):
    """创建分组的入参。"""
    name: str = Field(min_length=1, max_length=64)
    enabled: bool = True
    dispatch_mode: str = "sync"  # sync / poll
    remark: Optional[str] = None
    # 一对一绑定交易策略；空 / null = 不绑定
    strategy_id: Optional[str] = Field(default=None, max_length=32)
    node_ids: list[str] = Field(default_factory=list)


class GroupUpdate(BaseModel):
    """更新分组的入参（全部可选，仅更新提供的字段）。"""
    name: Optional[str] = None
    enabled: Optional[bool] = None
    dispatch_mode: Optional[str] = None
    remark: Optional[str] = None
    # 传入空字符串或 null 表示解除绑定；省略字段则不改
    strategy_id: Optional[str] = Field(default=None, max_length=32)
    node_ids: Optional[list[str]] = None  # 传入即整体替换成员列表


class GroupOut(BaseModel):
    """分组对外展示对象（含成员节点与信号计数）。"""
    group_id: str
    name: str
    enabled: bool = True
    dispatch_mode: str = "sync"
    strategy_id: Optional[str] = None
    strategy_name: Optional[str] = None
    remark: Optional[str] = None
    created_at: float = 0
    nodes: list[GroupNodeRef] = Field(default_factory=list)
    node_count: int = 0
    online_node_count: int = 0   # 有效节点数（已启用 + 在线）
    signal_count: int = 0        # 该分组已处理的信号主任务数
    active_task_count: int = 0   # 进行中主任务数（pending/dispatching/running）


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
    """分批加仓档位：持仓笔数区间内的加仓间距 / 倍数。

    加仓间距由 calc_type 决定读哪个参数，四种方式最终都换算成触发所需的偏离点数。
    """
    pos_from: int = Field(default=2, ge=0)
    pos_to: int = Field(default=4, ge=0)
    calc_type: str = Field(
        default="point",
        description="间距计算方式：point=点数 / price=指定价位 / atr=ATR / range=K线波幅",
    )
    point: float = Field(default=100, ge=0, description="calc_type=point 时的触发点数")
    price: float = Field(default=0.0, ge=0, description="calc_type=price 时的绝对价位")
    timeframe: str = Field(
        default="M5", description="calc_type=atr / range 时统计用的 K 线周期",
    )
    lot_times: float = Field(default=1.0, ge=0)
    extra_lot: float = Field(default=0.0, ge=0)


class StrategyRule(BaseModel):
    """单条策略规则，字段按 type 分组使用。

    type=1 逆势加仓 / type=2 顺势加仓（模版1）：point ~ batch_levels；
    type=3 以损定量趋势单（模版2）：risk_amount ~ breakeven_times；
    type=4 网格交易（模版3）：price_lower ~ trailing_max。

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
    total_lot_limit: float = Field(default=0.0, ge=0, description="总手数上限，0=不限制")
    batch_levels: list[StrategyBatchLevel] = Field(default_factory=list)
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


# ----------------------- 中控台手动触发 -----------------------
class ManualSignalRequest(BaseModel):
    """后台手动触发的信号（复用 Webhook 分发流程）。

    volume 只有开仓（BUY / SELL）才必填，由接口层按 action 校验：CLOSE 是终止
    指令，手数没有意义。stop_loss / take_profit / comment 对齐 Webhook 的同名字段。
    """
    symbol: str = Field(min_length=1)
    action: str  # BUY / SELL / CLOSE（CLOSE 仅 strategy 链路开放）
    volume: Optional[float] = Field(default=None, gt=0)
    model: Optional[str] = None  # normal（默认）/ strategy
    stop_loss: Optional[float] = Field(default=None, gt=0)
    take_profit: Optional[float] = Field(default=None, gt=0)
    comment: Optional[str] = Field(default=None, max_length=64)
    # 策略模版定向（仅 strategy 链路）：只发给绑定了这些模版的分组；空 = 不限制
    template_ids: list[str] = Field(default_factory=list)
    # 分组定向（仅 strategy 链路）：只发给 ID 在列表内的分组；空 = 不限制
    group_ids: list[str] = Field(default_factory=list)


# 清空交易记录时前端/调用方必须原样提交的确认词
PURGE_TRADE_LOGS_CONFIRM = "清空交易记录"


class PurgeTradeLogsRequest(BaseModel):
    """清空全部交易日志/记录表；需提交确认词防止误触。"""
    confirm: str = Field(description=f"必须为「{PURGE_TRADE_LOGS_CONFIRM}」")


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
    """修改后台管理员密码（需携带当前密码）。"""
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
