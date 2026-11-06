"""信号管理中心 API（仅管理员：信号历史是全局数据）。

与旧的 `/api/events/signals` 并存：
- 旧接口：分页返回 Webhook 信号原始事件，前端【事件】页在用，**保持不变**；
- 新接口：信号中心的聚合视图（概览 / 统一流 / 节点命中 / 时延 / 策略任务），
  口径全部落在 `signal_stats` 这一个模块里。

所有接口共用同一套筛选语义（`signal_stats.SignalFilters`），
避免「概览按品种过滤了，信号流却没过滤」这种对不上的情况。
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from . import signal_stats
from .deps import get_store, require_admin
from .permissions import Principal
from .redis_store import RedisStore

router = APIRouter(prefix="/api/signal-center", tags=["signal-center"])


# ---------------------------------------------------------------------------
# 响应模型
# ---------------------------------------------------------------------------
class SignalCenterTotals(BaseModel):
    """概览总量。"""
    signals: int = 0
    accepted: int = 0
    rejected: int = 0
    # 去重抑制次数：本期不采集（决策点 3），恒为 0 且 collected=False，
    # 前端据此显示「—（未采集）」而不是「0」
    duplicate: int = 0
    duplicate_collected: bool = False
    dispatch_total: int = 0
    passed: int = 0
    skipped: int = 0
    failed: int = 0
    pending: int = 0
    # 信号级成功率（分母 = 非拒收信号数）
    signal_success_rate: float = 0.0
    # 分发级成功率（分母 = 投递总数）；与上面是两套口径，不可混用
    dispatch_success_rate: float = 0.0
    avg_targets_per_signal: float = 0.0


class StatusCount(BaseModel):
    status: str
    count: int


class SourceCount(BaseModel):
    source: str
    count: int


class ModelCount(BaseModel):
    model: str
    count: int


class SymbolCount(BaseModel):
    symbol: str
    count: int
    success_rate: float = 0.0


class SkipReasonCount(BaseModel):
    code: str
    label: str
    count: int


class DailySignalPoint(BaseModel):
    date: str
    ts: int
    signals: int = 0
    accepted: int = 0
    failed: int = 0
    success_rate: float = 0.0


class SignalCenterOverview(BaseModel):
    """概览页响应体。"""
    window: str
    from_ts: Optional[int] = None
    totals: SignalCenterTotals = Field(default_factory=SignalCenterTotals)
    status_distribution: list[StatusCount] = Field(default_factory=list)
    by_source: list[SourceCount] = Field(default_factory=list)
    by_model: list[ModelCount] = Field(default_factory=list)
    by_symbol: list[SymbolCount] = Field(default_factory=list)
    skip_reasons: list[SkipReasonCount] = Field(default_factory=list)
    daily: list[DailySignalPoint] = Field(default_factory=list)
    updated_at: int = 0


class StreamDispatch(BaseModel):
    """统一信号流里的一条分发明细。"""
    node_id: str
    model: str = "normal"
    status: str = "pending"
    skip_code: Optional[str] = None
    skip_label: Optional[str] = None
    skip_reason: Optional[str] = None
    decided_vol: Optional[float] = None
    error: Optional[str] = None


class StreamItem(BaseModel):
    """统一信号流里的一条信号。"""
    signal_id: str
    received_at: Optional[float] = None
    source: str = "tradingview"
    model: str = "normal"
    action: Optional[str] = None
    symbol: Optional[str] = None
    volume: Optional[float] = None
    status: str = "pending"
    reject_code: Optional[str] = None
    reject_label: Optional[str] = None
    targets: int = 0
    done: int = 0
    skipped: int = 0
    failed: int = 0
    elapsed_ms: Optional[int] = None
    dispatches: list[StreamDispatch] = Field(default_factory=list)


class SignalCenterStream(BaseModel):
    """统一信号流响应体（游标分页）。"""
    items: list[StreamItem] = Field(default_factory=list)
    next_cursor: Optional[str] = None


class DetailDispatch(BaseModel):
    """单条信号明细里的分发明细（比列表版多两段时延与闸门结果）。"""
    node_id: str
    model: str = "normal"
    status: str = "pending"
    gate_result: str = "passed"
    skip_code: Optional[str] = None
    skip_label: Optional[str] = None
    skip_reason: Optional[str] = None
    decided_vol: Optional[float] = None
    sent_at: Optional[float] = None
    finished_at: Optional[float] = None
    queued_ms: Optional[float] = None
    order_to_fill_ms: Optional[float] = None
    error: Optional[str] = None


class DetailGroupTask(BaseModel):
    """strategy 链路的主任务摘要。"""
    task_id: int
    group_id: str
    group_name: Optional[str] = None
    strategy_name: Optional[str] = None
    status: str = "pending"
    skip_code: Optional[str] = None
    skip_label: Optional[str] = None
    skip_reason: Optional[str] = None
    node_count: int = 0
    total_orders: int = 0
    total_volume: float = 0.0
    realized_profit: float = 0.0
    opened_at: Optional[float] = None
    finished_at: Optional[float] = None


class SignalCenterDetail(BaseModel):
    """单条信号的完整明细（含原始 Webhook 载荷）。"""
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
    source: str = "tradingview"
    model: str = "normal"
    reject_code: Optional[str] = None
    reject_label: Optional[str] = None
    dispatches: list[DetailDispatch] = Field(default_factory=list)
    group_tasks: list[DetailGroupTask] = Field(default_factory=list)


class NodeHitItem(BaseModel):
    """一个节点的命中统计。"""
    node_id: str
    node_name: str
    signal_count: int = 0
    dispatch_count: int = 0
    done: int = 0
    skipped: int = 0
    failed: int = 0
    pending: int = 0
    success_rate: float = 0.0
    latency_p50_ms: Optional[float] = None
    latency_p90_ms: Optional[float] = None
    top_skip_code: Optional[str] = None
    top_skip_label: Optional[str] = None
    # 节点内两段耗时：只在同一节点内部可比
    queued_p50_ms: Optional[float] = None
    queued_p90_ms: Optional[float] = None
    fill_p50_ms: Optional[float] = None
    fill_p90_ms: Optional[float] = None


class SignalCenterNodes(BaseModel):
    items: list[NodeHitItem] = Field(default_factory=list)


class LatencyBucket(BaseModel):
    p50: Optional[float] = None
    p90: Optional[float] = None
    samples: int = 0
    scope: str = "server_clock"
    note: Optional[str] = None


class LatencyEndToEnd(LatencyBucket):
    p99: Optional[float] = None


class NodeLatencyRow(BaseModel):
    node_id: str
    queued_p50: Optional[float] = None
    queued_p90: Optional[float] = None
    fill_p50: Optional[float] = None
    fill_p90: Optional[float] = None


class SlowNode(BaseModel):
    node_id: str
    p50: Optional[float] = None
    p90: Optional[float] = None
    samples: int = 0


class DailyLatencyPoint(BaseModel):
    date: str
    ts: int
    p50: Optional[float] = None
    p90: Optional[float] = None
    samples: int = 0


class SignalCenterLatency(BaseModel):
    """时延面板响应体。"""
    window: str
    end_to_end: LatencyEndToEnd = Field(default_factory=LatencyEndToEnd)
    queued_ms: LatencyBucket = Field(default_factory=LatencyBucket)
    order_to_fill_ms: LatencyBucket = Field(default_factory=LatencyBucket)
    by_node: list[NodeLatencyRow] = Field(default_factory=list)
    slow_nodes: list[SlowNode] = Field(default_factory=list)
    daily: list[DailyLatencyPoint] = Field(default_factory=list)
    note: str = ""


class StrategyTaskItem(BaseModel):
    """一个分组的策略任务汇总。"""
    group_id: str
    group_name: str
    tasks: int = 0
    orders: int = 0
    volume: float = 0.0
    # 口径：group_signal_task.realized_profit（任务收口时节点上报的累计值），
    # 与 deal_stats 的逐笔净盈亏不是同一口径，两者不可加总
    realized_profit: float = 0.0
    statuses: dict[str, int] = Field(default_factory=dict)


class StrategyTaskTotals(BaseModel):
    tasks: int = 0
    orders: int = 0
    volume: float = 0.0
    realized_profit: float = 0.0


class TaskDispatchItem(BaseModel):
    """主任务下的节点子任务。"""
    node_id: str
    status: str = "pending"
    skip_code: Optional[str] = None
    skip_label: Optional[str] = None
    skip_reason: Optional[str] = None
    decided_vol: Optional[float] = None
    total_orders: int = 0
    total_volume: float = 0.0
    realized_profit: float = 0.0
    finish_reason: Optional[str] = None
    error: Optional[str] = None
    queued_ms: Optional[float] = None
    order_to_fill_ms: Optional[float] = None
    dispatched_at: Optional[float] = None
    finished_at: Optional[float] = None


class StrategyTaskDetail(BaseModel):
    """一个策略主任务（含子任务明细）。"""
    task_id: int
    signal_id: str
    group_id: str
    group_name: str
    strategy_name: Optional[str] = None
    symbol: Optional[str] = None
    action: Optional[str] = None
    volume: Optional[float] = None
    status: str = "pending"
    skip_code: Optional[str] = None
    skip_label: Optional[str] = None
    skip_reason: Optional[str] = None
    node_count: int = 0
    total_orders: int = 0
    total_volume: float = 0.0
    realized_profit: float = 0.0
    created_at: Optional[float] = None
    opened_at: Optional[float] = None
    finished_at: Optional[float] = None
    dispatches: list[TaskDispatchItem] = Field(default_factory=list)


class SignalCenterTasks(BaseModel):
    items: list[StrategyTaskItem] = Field(default_factory=list)
    totals: StrategyTaskTotals = Field(default_factory=StrategyTaskTotals)
    # 主任务列表（可展开看子任务）：从汇总下钻到具体任务
    tasks: list[StrategyTaskDetail] = Field(default_factory=list)
    # 明确告知前端这批盈亏的口径来源，避免与成交侧净盈亏混用
    profit_scope: str = (
        "group_signal_task.realized_profit（策略任务收口累计值）；"
        "与成交侧逐笔净盈亏（deal_stats）口径不同，不可加总"
    )


# ---------------------------------------------------------------------------
# 公共依赖
# ---------------------------------------------------------------------------
def _filters(
    window: str = Query(signal_stats.DEFAULT_WINDOW),
    source: Optional[str] = Query(None),
    model: Optional[str] = Query(None),
    symbol: Optional[str] = Query(None),
    action: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    node_id: Optional[str] = Query(None),
    group_id: Optional[str] = Query(None),
    keyword: Optional[str] = Query(None),
) -> signal_stats.SignalFilters:
    """把查询参数收敛成统一筛选条件。"""
    return signal_stats.SignalFilters(
        window=window, source=source, model=model, symbol=symbol,
        action=action, status=status, node_id=node_id,
        group_id=group_id, keyword=keyword,
    )


# ---------------------------------------------------------------------------
# 接口
# ---------------------------------------------------------------------------
@router.get("/overview", response_model=SignalCenterOverview)
async def overview(
    filters: signal_stats.SignalFilters = Depends(_filters),
    _: Principal = Depends(require_admin),
):
    """概览：总量 + 状态分布 + 来源 / 链路 / 品种 / 拦截原因 + 每日趋势。"""
    return await signal_stats.overview(filters)


@router.get("/stream", response_model=SignalCenterStream)
async def stream(
    cursor: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    filters: signal_stats.SignalFilters = Depends(_filters),
    _: Principal = Depends(require_admin),
):
    """统一信号流：normal + strategy 混排，游标分页。"""
    return await signal_stats.stream(filters, cursor=cursor, limit=limit)


@router.get("/nodes", response_model=SignalCenterNodes)
async def nodes(
    filters: signal_stats.SignalFilters = Depends(_filters),
    store: RedisStore = Depends(get_store),
    _: Principal = Depends(require_admin),
):
    """节点命中：按节点聚合信号数 / 成功率 / 时延分位。"""
    all_nodes = await store.all_nodes()
    names = {n["node_id"]: n.get("name") or n["node_id"] for n in all_nodes}
    return await signal_stats.nodes(filters, node_names=names)


@router.get("/latency", response_model=SignalCenterLatency)
async def latency(
    filters: signal_stats.SignalFilters = Depends(_filters),
    _: Principal = Depends(require_admin),
):
    """时延：端到端（跨节点可比）+ 节点内两段（分节点展示）。"""
    return await signal_stats.latency(filters)


@router.get("/tasks", response_model=SignalCenterTasks)
async def tasks(
    filters: signal_stats.SignalFilters = Depends(_filters),
    store: RedisStore = Depends(get_store),
    _: Principal = Depends(require_admin),
):
    """策略任务：按分组汇总任务数 / 状态 / 已实现盈亏（独立口径，不与成交侧合并）。"""
    all_groups = await store.all_groups()
    names = {g["group_id"]: g.get("name") or g["group_id"] for g in all_groups}
    return await signal_stats.strategy_tasks(filters, group_names=names)


@router.get("/signal/{signal_id}", response_model=SignalCenterDetail)
async def signal_detail(
    signal_id: str,
    _: Principal = Depends(require_admin),
):
    """单条信号明细：原始载荷 + 两条链路的节点明细。

    列表页只放摘要，原始 Webhook JSON 按需拉取 —— 否则一页 20 条要带 20 份载荷。
    """
    detail = await signal_stats.signal_detail(signal_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="信号不存在")
    return detail
