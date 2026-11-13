"""信号管理中心 API（仅管理员：信号历史是全局数据）。

与旧的 `/api/events/signals` 并存：
- 旧接口：分页返回 Webhook 信号原始事件，前端【事件】页在用，**保持不变**；
- 新接口：信号中心的聚合视图（概览 / 统一流 / 节点命中 / 时延 / 策略任务），
  口径全部落在 `signal_stats` 这一个模块里。

所有接口共用同一套筛选语义（`signal_stats.SignalFilters`），
避免「概览按品种过滤了，信号流却没过滤」这种对不上的情况。
"""
from typing import Optional

import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from . import (
    signal_ai_center,
    signal_meta,
    signal_perf,
    signal_quality,
    signal_routing,
    signal_stats,
)
from .deps import client_ip, get_group_dispatcher, get_store, require_admin
from .group_dispatcher import GroupDispatcher
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


class SignalQualityItem(BaseModel):
    """单条信号的质量分。"""
    # 质量分（0~100）；None = 没有可用先验（暂无历史），与「分数是 0」不同
    score: Optional[float] = None
    # 该桶的原始胜率（百分数）
    win_rate: float = 0.0
    # 样本量 / 赢数
    n: int = 0
    wins: int = 0
    # 单信号平均净盈亏（参考用，不进分数）
    avg_profit: float = 0.0
    # 分数取自哪个桶（主键 / 类型|周期 / __global__）
    basis: str = ""
    basis_label: str = ""
    # 回退层级：type_tf / type / global / none
    level: str = "none"
    # 样本充分度：sufficient / thin / insufficient / none
    sample_level: str = "none"


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
    # 周期 / 信号类型 / 路由状态（缺失 = 未标注）
    timeframe: Optional[str] = None
    signal_type: Optional[str] = None
    route_state: Optional[str] = None
    reject_code: Optional[str] = None
    reject_label: Optional[str] = None
    targets: int = 0
    done: int = 0
    skipped: int = 0
    failed: int = 0
    elapsed_ms: Optional[int] = None
    # 质量分：历史同类信号的成功率（只展示，不参与交易决策）
    quality: Optional[SignalQualityItem] = None
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


# ---------------------------------------------------------------------------
# 绩效（信号级 / 持仓级胜率）
# ---------------------------------------------------------------------------
class PerfBucketItem(BaseModel):
    """一个绩效聚合桶（按周期 / 按类型 / 按入场原因）。"""
    key: str
    label: str
    # —— 信号级 ——
    signals_total: int = 0
    signals_closed: int = 0
    signals_win: int = 0
    signals_running: int = 0
    signals_no_trade: int = 0
    signal_win_rate: float = 0.0
    signal_profit: float = 0.0
    avg_signal_profit: float = 0.0
    # 进行中浮盈：单列，不进胜率分母
    running_profit: float = 0.0
    # —— 持仓级 ——
    positions_total: int = 0
    positions_closed: int = 0
    positions_win: int = 0
    position_win_rate: float = 0.0
    position_profit: float = 0.0
    avg_position_profit: float = 0.0
    running_position_profit: float = 0.0
    volume: float = 0.0
    orders: int = 0


class EntryLegItem(BaseModel):
    """一条入场明细（首仓 / 某一档加仓）。"""
    signal_id: str
    task_id: Optional[int] = None
    node_id: Optional[str] = None
    symbol: Optional[str] = None
    action: Optional[str] = None
    seq: int = 0
    reason: str = ""
    reason_label: str = ""
    rule_type: Optional[int] = None
    level_index: Optional[int] = None
    # 入场级周期；点数 / 指定价加仓恒为 None（不依赖周期）
    timeframe: Optional[str] = None
    timeframe_label: str = ""
    volume: Optional[float] = None
    price: Optional[float] = None
    realized_profit: Optional[float] = None
    closed: bool = False


class SignalCenterPerformance(BaseModel):
    """绩效面板响应体。"""
    window: str
    from_ts: Optional[int] = None
    totals: PerfBucketItem = Field(default_factory=PerfBucketItem)
    by_timeframe: list[PerfBucketItem] = Field(default_factory=list)
    by_signal_type: list[PerfBucketItem] = Field(default_factory=list)
    by_entry_reason: list[PerfBucketItem] = Field(default_factory=list)
    legs: list[EntryLegItem] = Field(default_factory=list)
    updated_at: int = 0
    # 口径说明：前端必须原样展示，避免用户把两套数字混为一谈
    scope: str = (
        "信号级=该信号全部持仓净盈亏合计>0；持仓级=单个持仓净盈亏>0；"
        "进行中不入分母（单列）；不统计档位级胜率"
    )


# ---------------------------------------------------------------------------
# 质量分
# ---------------------------------------------------------------------------
class QualityBucketItem(BaseModel):
    """先验表的一行（按「信号类型 × 周期」）。"""
    key: str = ""
    signal_type: Optional[str] = None
    timeframe: Optional[str] = None
    label: str = ""
    n: int = 0
    wins: int = 0
    win_rate: float = 0.0
    score: float = 0.0
    avg_profit: float = 0.0
    sample_level: str = "none"


class SignalCenterQuality(BaseModel):
    """质量分先验表响应体。"""
    window: str = "30d"
    global_: dict = Field(default_factory=dict, alias="global")
    buckets: list[QualityBucketItem] = Field(default_factory=list)
    updated_at: int = 0
    # 口径说明：前端必须原样展示
    scope: str = (
        "质量分=历史同类信号（类型×周期）的信号级胜率，"
        "小样本按贝叶斯收缩向全局回归；只展示，不参与任何交易决策"
    )

    model_config = {"populate_by_name": True}


# ---------------------------------------------------------------------------
# AI 信号管理中心（原型复刻）
# ---------------------------------------------------------------------------
class AiKpi(BaseModel):
    """四个指标卡。"""
    # 今日触发信号（UTC+8 自然日）
    today_signals: int = 0
    # 覆盖周期：只给个数，**不写分母**
    timeframes_covered: int = 0
    # 高可靠信号：等级为「高」的信号条数
    high_reliability: int = 0
    # 平均信号质量：有可用先验的信号的质量分均值
    avg_quality: float = 0.0
    # 窗口内信号总数 / 有可用先验的条数（分母说明用）
    signals_total: int = 0
    signals_scored: int = 0


class AiReliabilityItem(BaseModel):
    """可靠性等级分布的一档。"""
    level: str = "none"
    label: str = ""
    count: int = 0
    pct: float = 0.0
    advice: str = ""


class AiScoreBucketItem(BaseModel):
    """质量分区间分布的一档。"""
    key: str = ""
    label: str = ""
    count: int = 0
    pct: float = 0.0


class AiSignalRow(BaseModel):
    """信号明细的一行（也用于 TOP 5 列表）。"""
    signal_id: str = ""
    received_at: Optional[float] = None
    timeframe: Optional[str] = None
    timeframe_label: str = ""
    signal_type: Optional[str] = None
    signal_type_label: str = ""
    type_group: str = ""
    name: str = ""
    action: Optional[str] = None
    action_label: str = ""
    symbol: Optional[str] = None
    quality: Optional[SignalQualityItem] = None
    score: Optional[float] = None
    # 可靠性等级：high / mid / low / none
    level: str = "none"
    advice: str = ""
    # 马丁止盈档位（None = 未止盈 / 持仓中）
    tp_level: Optional[int] = None
    tp_level_label: Optional[str] = None
    # tp / sl / running / none
    tp_state: str = "none"
    tp_units: list[dict] = Field(default_factory=list)


class AiTypeQualityItem(BaseModel):
    """信号类型质量对比的一栏（按业务分类聚合）。"""
    key: str = ""
    label: str = ""
    count: int = 0
    avg_score: float = 0.0
    high_count: int = 0
    level: str = "none"
    advice: str = ""
    best_signal: Optional[AiSignalRow] = None
    # 该分类下的类型码（联动筛选用）
    types: list[str] = Field(default_factory=list)


class AiTimeframeQualityItem(BaseModel):
    """周期质量总览的一张卡。"""
    key: str = ""
    label: str = ""
    count: int = 0
    avg_score: float = 0.0
    high_count: int = 0
    level: str = "none"
    advice: str = ""
    best_signal: Optional[AiSignalRow] = None


class AiOverview(BaseModel):
    """AI 信号管理中心上半屏。"""
    window: str = "30d"
    from_ts: Optional[int] = None
    kpi: AiKpi = Field(default_factory=AiKpi)
    reliability: list[AiReliabilityItem] = Field(default_factory=list)
    score_buckets: list[AiScoreBucketItem] = Field(default_factory=list)
    by_type: list[AiTypeQualityItem] = Field(default_factory=list)
    by_timeframe: list[AiTimeframeQualityItem] = Field(default_factory=list)
    top_signals: list[AiSignalRow] = Field(default_factory=list)
    updated_at: int = 0
    scope: str = signal_ai_center.QUALITY_SCOPE


class AiTpBucket(BaseModel):
    """一个止盈档位。"""
    level: int = 0
    label: str = ""
    count: int = 0
    pct: float = 0.0


class AiTpNodeItem(BaseModel):
    """一个节点的止盈档位分布（**按节点分开统计**）。"""
    node_id: str = ""
    node_name: str = ""
    units: int = 0
    buckets: list[AiTpBucket] = Field(default_factory=list)


class AiTpDistribution(BaseModel):
    """马丁止盈分布。"""
    window: str = "30d"
    # 窗口内信号总数（原型「全部信号」）
    total_signals: int = 0
    # 已止盈的统计单元数（分布分母）
    closed_tp_units: int = 0
    by_node: list[AiTpNodeItem] = Field(default_factory=list)
    overall: dict = Field(default_factory=dict)
    # tp / sl / running / none 的单元数
    states: dict[str, int] = Field(default_factory=dict)
    # 每个档位的重点信号 TOP 5（按质量分降序）
    top_by_level: dict[str, list[AiSignalRow]] = Field(default_factory=dict)
    updated_at: int = 0
    scope: str = signal_ai_center.TP_SCOPE


class AiSignalList(BaseModel):
    """信号明细表（页码分页）。"""
    window: str = "30d"
    total: int = 0
    total_signals: int = 0
    page: int = 1
    pages: int = 1
    page_size: int = 10
    items: list[AiSignalRow] = Field(default_factory=list)
    updated_at: int = 0
    scope: str = signal_ai_center.QUALITY_SCOPE


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
class RouteRequest(BaseModel):
    """路由一条（或多条）信号到指定分组。"""
    group_ids: list[str] = Field(default_factory=list)
    # 参数覆盖：缺省沿用信号原始值
    volume: Optional[float] = None
    sl: Optional[float] = None
    tp: Optional[float] = None
    note: Optional[str] = Field(default=None, max_length=255)


class BatchRouteRequest(RouteRequest):
    """批量路由：显式给 signal_ids。"""
    signal_ids: list[str] = Field(default_factory=list)


class RouteResultItem(BaseModel):
    signal_id: str
    groups: int = 0
    targets: int = 0


class BatchRouteResponse(BaseModel):
    ok: list[RouteResultItem] = Field(default_factory=list)
    conflicts: list[dict] = Field(default_factory=list)
    failed: list[dict] = Field(default_factory=list)
    summary: dict[str, int] = Field(default_factory=dict)


class RouteResponse(BaseModel):
    signal_id: str
    groups: int = 0
    targets: int = 0
    tasks: list[dict] = Field(default_factory=list)
    route_state: str = ""
    note: Optional[str] = None


class DropResponse(BaseModel):
    signal_id: str
    route_state: str = ""
    note: Optional[str] = None


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
    timeframe: Optional[str] = Query(None),
    signal_type: Optional[str] = Query(None),
    route_state: Optional[str] = Query(None),
    direction: Optional[str] = Query(None),
) -> signal_stats.SignalFilters:
    """把查询参数收敛成统一筛选条件。"""
    return signal_stats.SignalFilters(
        window=window, source=source, model=model, symbol=symbol,
        action=action, status=status, node_id=node_id,
        group_id=group_id, keyword=keyword,
        timeframe=timeframe, signal_type=signal_type,
        route_state=route_state, direction=direction,
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


# ---------------------------------------------------------------------------
# 绩效
# ---------------------------------------------------------------------------
@router.get("/performance", response_model=SignalCenterPerformance)
async def performance(
    filters: signal_stats.SignalFilters = Depends(_filters),
    _: Principal = Depends(require_admin),
):
    """绩效：信号级 + 持仓级胜率，按周期 / 信号类型 / 入场原因分桶。

    只统计 **AI 智能加仓（tpl_1）** 的任务 —— 其它模版的加仓语义不同，
    混在一起算出来的胜率没有可比性。
    """
    return await signal_perf.performance(filters)


@router.get("/quality", response_model=SignalCenterQuality)
async def quality(
    window: str = Query(signal_quality.DEFAULT_PRIOR_WINDOW),
    _: Principal = Depends(require_admin),
):
    """质量分先验表：按「信号类型 × 周期」列出历史成功率。

    **与绩效面板同源同口径**（都走 `signal_perf._aggregate`），数字必须对得上。

    注意：先验**不受信号中心的其它筛选条影响** —— 它是背景知识，
    固定按 `window` 拉全量算（见 `signal_quality` 模块 docstring 铁律 3）。
    """
    prior = await signal_quality.get_prior(window)
    return {
        "window": prior.get("window") or window,
        "global": prior.get("global") or {},
        "buckets": prior.get("buckets") or [],
        "updated_at": prior.get("updated_at") or 0,
    }


@router.get("/ai/overview", response_model=AiOverview)
async def ai_overview(
    filters: signal_stats.SignalFilters = Depends(_filters),
    _: Principal = Depends(require_admin),
):
    """AI 信号管理中心 · 上半屏：指标卡 + 质量总览 + 类型对比 + 周期总览 + TOP5。

    质量分**只有一个实现**（`signal_quality`），等级是绝对阈值
    （≥60 高 / 50-60 中 / <50 低）。
    """
    return await signal_ai_center.overview(filters)


@router.get("/ai/tp-distribution", response_model=AiTpDistribution)
async def ai_tp_distribution(
    filters: signal_stats.SignalFilters = Depends(_filters),
    store: RedisStore = Depends(get_store),
    _: Principal = Depends(require_admin),
):
    """AI 信号管理中心 · 马丁止盈分布。

    **按节点分开统计**（统计单元 = 信号 × 节点），不合并。
    止损与持仓中的单元不进分布分母。
    """
    all_nodes = await store.all_nodes()
    names = {n["node_id"]: n.get("name") or n["node_id"] for n in all_nodes}
    return await signal_ai_center.tp_distribution(filters, node_names=names)


@router.get("/ai/signals", response_model=AiSignalList)
async def ai_signals(
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=200),
    tp_level: Optional[int] = Query(None, ge=0, le=5),
    node_id: Optional[str] = Query(None),
    type_group: Optional[str] = Query(None),
    filters: signal_stats.SignalFilters = Depends(_filters),
    _: Principal = Depends(require_admin),
):
    """AI 信号管理中心 · 信号明细表（页码分页）。

    - `tp_level`：1..5 按档位筛；0 = 未止盈（持仓中 / 止损）；不传 = 不限；
    - `type_group`：按**业务分类**筛（结构分析 = ob + fvg + pdz）——
      分类下的多个类型码会展开成 `IN (...)`，只取一个码会漏掉另外两个。
    """
    return await signal_ai_center.signals_page(
        filters, page=page, page_size=page_size,
        tp_level=tp_level, node_id=node_id, type_group=type_group,
    )


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
@router.post("/signals/{signal_id}/route", response_model=RouteResponse)
async def route_signal(
    signal_id: str,
    body: RouteRequest,
    request: Request,
    dispatcher: GroupDispatcher = Depends(get_group_dispatcher),
    _: Principal = Depends(require_admin),
):
    """把一条信号路由到指定的分组。

    幂等：同一信号在同一分组已有**未收口**任务 → 409，并返回已有 task_id。
    """
    try:
        result = await signal_routing.route_signal(
            signal_id,
            group_ids=body.group_ids,
            dispatcher=dispatcher,
            source_ip=client_ip(request),
            volume=body.volume,
            stop_loss=body.sl,
            take_profit=body.tp,
            note=body.note,
        )
    except signal_routing.RouteConflict as e:
        raise HTTPException(
            status_code=409,
            detail={
                "message": str(e),
                "task_ids": e.task_ids,
            },
        )
    except signal_routing.RouteInputError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "signal_id": signal_id,
        "groups": result.get("groups", 0),
        "targets": result.get("targets", 0),
        "tasks": result.get("tasks", []),
        "route_state": result.get("route_state", signal_meta.ROUTE_ROUTED),
        "note": result.get("note"),
    }


@router.post("/signals/batch-route", response_model=BatchRouteResponse)
async def batch_route(
    body: BatchRouteRequest,
    request: Request,
    dispatcher: GroupDispatcher = Depends(get_group_dispatcher),
    _: Principal = Depends(require_admin),
):
    """批量路由：逐条独立处理，一条失败不影响其余。"""
    if not body.signal_ids:
        raise HTTPException(status_code=400, detail="未指定信号")
    if not body.group_ids:
        raise HTTPException(status_code=400, detail="未指定目标分组")
    return await signal_routing.route_many(
        body.signal_ids,
        group_ids=body.group_ids,
        dispatcher=dispatcher,
        source_ip=client_ip(request),
        volume=body.volume,
        stop_loss=body.sl,
        take_profit=body.tp,
        note=body.note,
    )


@router.post("/signals/{signal_id}/drop", response_model=DropResponse)
async def drop_signal(
    signal_id: str,
    body: RouteRequest,
    _: Principal = Depends(require_admin),
):
    """丢弃一条信号：不再路由到 center 分组。

    **不影响已在跑的仓位** —— 要平仓请走 CLOSE 信号。
    """
    try:
        result = await signal_routing.drop_signal(signal_id, note=body.note)
    except signal_routing.RouteInputError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {
        "signal_id": result["signal_id"],
        "route_state": result["route_state"],
        "note": result.get("note"),
    }
