"""信号统计口径的**唯一实现**（信号管理中心）。

改动前请先读：
- 本模块是「信号侧」统计的唯一口径来源。概览页、信号流、节点命中、时延面板
  全部调这里，**不允许各页自己算一遍**——各处各算一次必然漂移，届时会出现
  「概览显示 1284 条，信号流列表只有 1200 条」这种对不上的情况。
- 「信号级」与「分发级」是两套口径，**不可混用**：
    - 信号级：一条 signal_history = 一条信号。成功率 = 成功信号数 / 信号总数。
    - 分发级：一条 dispatch 明细 = 一次「信号→节点」投递尝试。
      成功率 = 通过数 / 投递总数。
  两者分母不同，前端必须分别标注，不许混着说。

**时延口径（这块最容易写错，单独强调）**：
节点本地时钟与服务端时钟不同源，跨机的**绝对时刻**不可比。因此：

    | 指标              | 计算式                  | 可比性         |
    |-------------------|-------------------------|----------------|
    | 端到端时延        | finished_at - sent_at   | 跨节点可比     |
    | 节点排队 queued_ms| order_at - recv_at      | 仅节点内可比   |
    | 券商往返 fill_ms  | filled_at - order_at    | 仅节点内可比   |

`queued_ms` / `order_to_fill_ms` **只能分节点展示，绝不允许跨节点求和或求均值**
（那等于把不同机器的时钟差混在一起算，数字看着漂亮但完全无意义）。
本模块为此把这两段**按节点分组**输出，见 `latency_by_node()`。

**分日时区**：统一按 UTC+8 自然日切分，与 `deal_stats` 的成交侧口径一致。

**盈亏口径**：`group_signal_task.realized_profit`（策略任务收口时节点上报的累计值）
与 `deal_stats` 的逐笔净盈亏是**两套口径**，本模块**不做加总**，
分别由 `strategy_task_profit()` 与成交侧接口各自输出，前端分区展示。
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import time
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import func, select

from . import signal_codes, signal_meta, signal_quality
from .db import SessionLocal
from .orm import (
    GroupSignalTask,
    GroupTaskDispatch,
    SignalDispatch,
    SignalHistory,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 窗口（与 deal_stats.WINDOWS 保持一致，不另立一套）
# ---------------------------------------------------------------------------
WINDOWS: dict[str, Optional[int]] = {
    "7d": 7,
    "30d": 30,
    "90d": 90,
    "all": None,
}
DEFAULT_WINDOW = "30d"

# 分日用的固定偏移（秒）：UTC+8（与 deal_stats 对齐）
_DAY_OFFSET = 8 * 3600
_DAY_SECONDS = 86400

# 分发明细的跳过态
_DISPATCH_SKIPPED = "skipped"
# 分发明细的失败态
_DISPATCH_FAILED = "failed"

# strategy 链路的「已成功」子任务状态：opened/running/done 都算成功落地
_STRATEGY_SUCCESS_STATUS = ("opened", "running", "done")


def window_start(window: str) -> Optional[float]:
    """窗口起点（Unix 秒）；all 返回 None（不限时间）。

    注意区分两种「取不到值」：
    - 传了非法窗口（如 "bogus"）→ 按默认档 30d 处理，避免「传错参数 = 全量查询」；
    - 明确传了 "all" → 不限时间。
    这两者的返回值不同（一个是数字，一个是 None），不能混为一谈，
    否则 "all" 会被当成非法值而静默变成 30 天窗口。
    """
    key = str(window or "").strip().lower()
    if key not in WINDOWS:
        key = DEFAULT_WINDOW
    days = WINDOWS[key]
    if days is None:
        return None
    return time.time() - days * 86400


def normalize_window(window: Optional[str]) -> str:
    """把窗口参数收敛成合法值。"""
    w = str(window or "").strip().lower()
    return w if w in WINDOWS else DEFAULT_WINDOW


# ---------------------------------------------------------------------------
# 分日工具（UTC+8 自然日）
# ---------------------------------------------------------------------------
def day_index(ts: float, day_start: float) -> int:
    """某个 Unix 秒落在「以 day_start 为第 0 天的 UTC+8 自然日」的第几天。"""
    return int((int(ts) - int(day_start)) // _DAY_SECONDS)


def day_label(ts: float) -> str:
    """UTC+8 自然日的 'MM-DD' 标签。

    用 gmtime(ts + 8h) 取「UTC+8 的墙上时钟」——不能用 localtime：
    服务器本地时区不确定，localtime 会再叠一层偏移。
    """
    d = time.gmtime(int(ts) + _DAY_OFFSET)
    return f"{d.tm_mon:02d}-{d.tm_mday:02d}"


def today_start_utc8() -> int:
    """今天（UTC+8）00:00 对应的 Unix 秒。"""
    now = int(time.time())
    return ((now + _DAY_OFFSET) // _DAY_SECONDS) * _DAY_SECONDS - _DAY_OFFSET


def _pct(part: int, total: int) -> float:
    """百分比，分母为 0 时返回 0.0（不是 None，前端直接显示 0）。"""
    if not total:
        return 0.0
    return round(part / total * 100, 2)


def percentile(values: list[float], q: float) -> Optional[float]:
    """线性插值分位数（q ∈ [0, 100]）。空样本返回 None。

    SQLite / MySQL 都没有内置 PERCENTILE_CONT，明细量级（万级）拉到 Python
    侧排序取分位完全可行，与 `deal_stats` 的做法一致。
    """
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 2)
    pos = (len(ordered) - 1) * (q / 100.0)
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    frac = pos - low
    val = ordered[low] + (ordered[high] - ordered[low]) * frac
    return round(val, 2)


# ---------------------------------------------------------------------------
# 筛选条件
# ---------------------------------------------------------------------------
class SignalFilters:
    """信号中心的统一筛选条件。

    刻意做成一个类而不是一堆散参：概览 / 流 / 节点 / 时延 / 绩效 / 路由
    六个接口共用同一套筛选语义，避免「概览按 symbol 过滤了，流却没过滤」
    这种对不上的情况。

    **新增的四个维度**（信号中心路由中枢与绩效面板需要）：
    - `timeframe`：信号级周期（M15 / H1 / …），归一后匹配；`"none"` 匹配未标注。
    - `signal_type`：首仓信号类型（ob / fvg / …）；`"none"` 匹配未标注。
    - `route_state`：路由状态（pending / auto / routed / dropped）。
    - `direction`：多空（buy / sell），整合中控台的「只做多 / 只做空」筛选。
    """

    __slots__ = (
        "window", "source", "model", "symbol", "action", "status",
        "node_id", "group_id", "keyword",
        "timeframe", "signal_type", "route_state", "direction",
    )

    def __init__(
        self,
        window: str = DEFAULT_WINDOW,
        source: Optional[str] = None,
        model: Optional[str] = None,
        symbol: Optional[str] = None,
        action: Optional[str] = None,
        status: Optional[str] = None,
        node_id: Optional[str] = None,
        group_id: Optional[str] = None,
        keyword: Optional[str] = None,
        timeframe: Optional[str] = None,
        signal_type: Optional[str] = None,
        route_state: Optional[str] = None,
        direction: Optional[str] = None,
    ) -> None:
        self.window = normalize_window(window)
        self.source = _clean(source)
        self.model = _clean(model)
        self.symbol = _clean_upper(symbol)
        self.action = _clean_upper(action)
        self.status = _clean_lower(status)
        self.node_id = _clean(node_id)
        self.group_id = _clean(group_id)
        self.keyword = _clean(keyword)
        # 周期 / 类型走 signal_meta 归一：认不出的一律 None（= 不按这个维度筛），
        # 绝不因为一个统计维度写错就整页报错
        self.timeframe = _clean_lower(timeframe)
        self.signal_type = _clean_lower(signal_type)
        self.route_state = _clean_lower(route_state)
        self.direction = _clean_lower(direction)

    @property
    def from_ts(self) -> Optional[float]:
        return window_start(self.window)

    def cache_key(self) -> str:
        """筛选条件的指纹（用于 Redis 缓存 key）。"""
        parts = [
            self.window, self.source or "", self.model or "", self.symbol or "",
            self.action or "", self.status or "", self.node_id or "",
            self.group_id or "", self.keyword or "",
            self.timeframe or "", self.signal_type or "",
            self.route_state or "", self.direction or "",
        ]
        return "|".join(parts)


def _clean(value: Optional[str]) -> Optional[str]:
    v = str(value or "").strip()
    return v or None


def _clean_upper(value: Optional[str]) -> Optional[str]:
    v = _clean(value)
    return v.upper() if v else None


def _clean_lower(value: Optional[str]) -> Optional[str]:
    v = _clean(value)
    return v.lower() if v else None


# ---------------------------------------------------------------------------
# 信号级查询
# ---------------------------------------------------------------------------
def _signal_conds(f: SignalFilters) -> list:
    """signal_history 的筛选条件。"""
    conds = []
    if f.source:
        conds.append(SignalHistory.source == f.source)
    if f.model:
        conds.append(SignalHistory.model == f.model)
    if f.symbol:
        conds.append(SignalHistory.symbol == f.symbol)
    if f.action:
        conds.append(SignalHistory.action == f.action)
    if f.status:
        conds.append(SignalHistory.status == f.status)
    if f.node_id:
        # 命中了指定节点的信号：normal 链路由分发明细推导；
        # strategy 链路由主任务的 node_ids_json 推导
        conds.append(
            SignalHistory.signal_id.in_(
                select(SignalDispatch.signal_id).where(SignalDispatch.node_id == f.node_id)
            )
        )
    if f.group_id:
        conds.append(
            SignalHistory.signal_id.in_(
                select(GroupSignalTask.signal_id).where(GroupSignalTask.group_id == f.group_id)
            )
        )
    if f.keyword:
        like = f"%{f.keyword}%"
        conds.append(
            SignalHistory.signal_id.like(like)
            | SignalHistory.symbol.like(like)
            | SignalHistory.comment.like(like)
            | SignalHistory.source_ip.like(like)
        )
    # —— 信号中心新增的四个维度 ——
    # 周期 / 类型：认不出就不过滤（统计维度写错不该让整页空掉）
    if f.timeframe:
        tf = signal_meta.normalize_timeframe(f.timeframe)
        if tf:
            conds.append(SignalHistory.timeframe == tf)
    if f.signal_type:
        st = signal_meta.normalize_signal_type(f.signal_type)
        if st:
            conds.append(SignalHistory.signal_type == st)
    if f.route_state:
        rs = signal_meta.safe_route_state(f.route_state)
        if rs:
            conds.append(SignalHistory.route_state == rs)
    if f.direction:
        # 多空筛选：整合中控台的「只做多 / 只做空」
        d = f.direction.lower()
        if d in ("buy", "long"):
            conds.append(SignalHistory.action == "BUY")
        elif d in ("sell", "short"):
            conds.append(SignalHistory.action == "SELL")
    return conds


async def _fetch_signals(
    f: SignalFilters, *, limit: Optional[int] = None, offset: int = 0,
    order_desc: bool = True,
) -> list[SignalHistory]:
    """按筛选条件取信号历史行。"""
    conds = _signal_conds(f)
    from_ts = f.from_ts
    if from_ts is not None:
        # received_at 是服务端本地 naive 时间，与库内其它时间列同口径
        conds.append(SignalHistory.received_at >= datetime_from_ts(from_ts))
    try:
        async with SessionLocal() as s:
            stmt = select(SignalHistory).where(*conds)
            stmt = stmt.order_by(
                SignalHistory.received_at.desc(), SignalHistory.signal_id.desc()
            ) if order_desc else stmt.order_by(
                SignalHistory.received_at.asc(), SignalHistory.signal_id.asc()
            )
            if limit is not None:
                stmt = stmt.offset(offset).limit(limit)
            return list((await s.execute(stmt)).scalars().all())
    except Exception as e:  # noqa: BLE001
        logger.warning("signal_stats._fetch_signals failed: %s", e)
        return []


def datetime_from_ts(ts: float) -> datetime:
    """Unix 秒 -> naive datetime（与库里其它时间列同口径）。"""
    return datetime.fromtimestamp(float(ts))


def datetime_to_ts(value: Optional[datetime]) -> Optional[float]:
    """naive datetime -> Unix 秒。"""
    if value is None:
        return None
    try:
        return value.timestamp()
    except (OverflowError, OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 概览
# ---------------------------------------------------------------------------
async def overview(f: SignalFilters) -> dict:
    """概览：总量 + 状态分布 + 来源 / 链路 / 品种 / 拦截原因 + 每日趋势。"""
    signals = await _fetch_signals(f)
    result: dict[str, Any] = {
        "window": f.window,
        "from_ts": int(f.from_ts) if f.from_ts is not None else None,
        "totals": {},
        "status_distribution": [],
        "by_source": [],
        "by_model": [],
        "by_symbol": [],
        "skip_reasons": [],
        "daily": [],
        "updated_at": int(time.time()),
    }
    if not signals:
        return result

    signal_ids = [s.signal_id for s in signals]
    dispatches = await _fetch_dispatches_for_signals(signal_ids)

    # —— 信号级汇总 ——
    status_counter: dict[str, int] = {}
    source_counter: dict[str, int] = {}
    model_counter: dict[str, int] = {}
    symbol_counter: dict[str, int] = {}
    symbol_success: dict[str, int] = {}
    rejected = 0
    accepted = 0
    done_signals = 0
    for s in signals:
        status = str(s.status or "pending")
        status_counter[status] = status_counter.get(status, 0) + 1
        source_counter[str(s.source or "tradingview")] = (
            source_counter.get(str(s.source or "tradingview"), 0) + 1
        )
        model_counter[str(s.model or "normal")] = (
            model_counter.get(str(s.model or "normal"), 0) + 1
        )
        sym = str(s.symbol or "-")
        symbol_counter[sym] = symbol_counter.get(sym, 0) + 1
        if status == "rejected":
            rejected += 1
        else:
            accepted += 1
        if status == "done":
            done_signals += 1
            symbol_success[sym] = symbol_success.get(sym, 0) + 1

    # —— 分发级汇总 ——
    dispatch_total = len(dispatches)
    passed = sum(1 for d in dispatches if d["status"] == "done")
    skipped = sum(1 for d in dispatches if d["status"] == _DISPATCH_SKIPPED)
    failed = sum(1 for d in dispatches if d["status"] in (_DISPATCH_FAILED, "offline"))
    pending = sum(1 for d in dispatches if d["status"] in ("pending", "sent"))

    skip_counter: dict[str, int] = {}
    for d in dispatches:
        code = d.get("skip_code")
        if d["status"] == _DISPATCH_SKIPPED or code:
            key = code or signal_codes.SKIP_UNKNOWN
            skip_counter[key] = skip_counter.get(key, 0) + 1

    total_signals = len(signals)
    result["totals"] = {
        "signals": total_signals,
        "accepted": accepted,
        "rejected": rejected,
        # 去重抑制本期不统计（决策点 3）：恒为 0，前端显示「—」
        "duplicate": 0,
        "duplicate_collected": False,
        "dispatch_total": dispatch_total,
        "passed": passed,
        "skipped": skipped,
        "failed": failed,
        "pending": pending,
        # 信号级成功率：成功信号 / 非拒收信号
        "signal_success_rate": _pct(done_signals, accepted),
        # 分发级成功率：投递成功 / 投递总数
        "dispatch_success_rate": _pct(passed, dispatch_total),
        "avg_targets_per_signal": round(dispatch_total / total_signals, 2) if total_signals else 0.0,
    }

    result["status_distribution"] = [
        {"status": k, "count": v}
        for k, v in sorted(status_counter.items(), key=lambda kv: -kv[1])
    ]
    result["by_source"] = [
        {"source": k, "count": v}
        for k, v in sorted(source_counter.items(), key=lambda kv: -kv[1])
    ]
    result["by_model"] = [
        {"model": k, "count": v}
        for k, v in sorted(model_counter.items(), key=lambda kv: -kv[1])
    ]
    result["by_symbol"] = [
        {
            "symbol": k,
            "count": v,
            "success_rate": _pct(symbol_success.get(k, 0), v),
        }
        for k, v in sorted(symbol_counter.items(), key=lambda kv: -kv[1])[:20]
    ]
    result["skip_reasons"] = [
        {
            "code": code,
            "label": signal_codes.skip_label(code),
            "count": count,
        }
        for code, count in sorted(skip_counter.items(), key=lambda kv: -kv[1])
    ]

    result["daily"] = await _daily_signals(signals, dispatches)
    return result


async def _daily_signals(
    signals: list[SignalHistory], dispatches: list[dict],
) -> list[dict]:
    """按 UTC+8 自然日聚合信号量与成功率。

    没有信号的日子**也要占位**（signals=0）——X 轴必须连续，否则
    「那天没信号」会被渲染成「那天不存在」。
    """
    if not signals:
        return []
    start = today_start_utc8()
    # 从最早一条信号所在自然日算起，最多回溯 90 天（避免超长窗口把图撑爆）
    earliest = min(
        (datetime_to_ts(s.received_at) or start) for s in signals
    )
    first_day = start - min(90, max(0, int((start - earliest) // _DAY_SECONDS))) * _DAY_SECONDS
    span_days = int((start - first_day) // _DAY_SECONDS) + 1

    buckets: dict[int, dict] = {
        i: {"signals": 0, "accepted": 0, "failed": 0, "done": 0}
        for i in range(span_days)
    }
    for s in signals:
        ts = datetime_to_ts(s.received_at)
        if ts is None:
            continue
        idx = day_index(ts, first_day)
        if idx not in buckets:
            continue
        b = buckets[idx]
        b["signals"] += 1
        status = str(s.status or "")
        if status == "rejected":
            continue
        b["accepted"] += 1
        if status == "failed":
            b["failed"] += 1
        elif status == "done":
            b["done"] += 1

    out: list[dict] = []
    for i in range(span_days):
        ts = first_day + i * _DAY_SECONDS
        b = buckets[i]
        out.append({
            "date": day_label(ts),
            "ts": ts,
            "signals": b["signals"],
            "accepted": b["accepted"],
            "failed": b["failed"],
            "success_rate": _pct(b["done"], b["accepted"]),
        })
    return out


async def _fetch_dispatches_for_signals(signal_ids: list[str]) -> list[dict]:
    """取这些信号下的全部分发明细（两条链路），归一化成统一 dict。"""
    if not signal_ids:
        return []
    try:
        async with SessionLocal() as s:
            normal_rows = (
                await s.execute(
                    select(
                        SignalDispatch.signal_id,
                        SignalDispatch.node_id,
                        SignalDispatch.status,
                        SignalDispatch.gate_result,
                        SignalDispatch.skip_code,
                        SignalDispatch.skip_reason,
                        SignalDispatch.decided_vol,
                        SignalDispatch.sent_at,
                        SignalDispatch.finished_at,
                        SignalDispatch.queued_ms,
                        SignalDispatch.order_to_fill_ms,
                        SignalDispatch.error,
                    ).where(SignalDispatch.signal_id.in_(signal_ids))
                )
            ).all()
            strategy_rows = (
                await s.execute(
                    select(
                        GroupTaskDispatch.signal_id,
                        GroupTaskDispatch.node_id,
                        GroupTaskDispatch.status,
                        GroupTaskDispatch.skip_code,
                        GroupTaskDispatch.skip_reason,
                        GroupTaskDispatch.decided_vol,
                        GroupTaskDispatch.sent_at,
                        GroupTaskDispatch.finished_at,
                        GroupTaskDispatch.queued_ms,
                        GroupTaskDispatch.order_to_fill_ms,
                        GroupTaskDispatch.error,
                    ).where(GroupTaskDispatch.signal_id.in_(signal_ids))
                )
            ).all()
    except Exception as e:  # noqa: BLE001
        logger.warning("signal_stats._fetch_dispatches_for_signals failed: %s", e)
        return []

    out: list[dict] = []
    for r in normal_rows:
        out.append({
            "signal_id": r[0], "node_id": r[1], "status": r[2],
            "gate_result": r[3], "skip_code": r[4], "skip_reason": r[5],
            "decided_vol": r[6], "sent_at": r[7], "finished_at": r[8],
            "queued_ms": r[9], "order_to_fill_ms": r[10], "error": r[11],
            "model": "normal",
        })
    for r in strategy_rows:
        # strategy 链路的成功态与 normal 不同：opened/running/done 都算落地
        status = "done" if r[2] in _STRATEGY_SUCCESS_STATUS else r[2]
        out.append({
            "signal_id": r[0], "node_id": r[1], "status": status,
            "gate_result": "passed", "skip_code": r[3], "skip_reason": r[4],
            "decided_vol": r[5], "sent_at": r[6], "finished_at": r[7],
            "queued_ms": r[8], "order_to_fill_ms": r[9], "error": r[10],
            "model": "strategy",
        })
    return out


# ---------------------------------------------------------------------------
# 统一信号流（游标分页）
# ---------------------------------------------------------------------------
def encode_cursor(ts: float, signal_id: str) -> str:
    """游标：接收时间 + 信号号（时间相同时用信号号稳定排序）。"""
    raw = json.dumps({"ts": int(ts), "sid": signal_id}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def decode_cursor(cursor: Optional[str]) -> Optional[tuple[int, str]]:
    """解析游标；非法游标返回 None（当作没有游标，退回第一页）。"""
    if not cursor:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        obj = json.loads(raw)
        return int(obj["ts"]), str(obj["sid"])
    except (ValueError, KeyError, TypeError, binascii.Error, UnicodeDecodeError):
        return None


async def stream(f: SignalFilters, *, cursor: Optional[str] = None,
                 limit: int = 20) -> dict:
    """统一信号流：normal + strategy 混排，按接收时间倒序的游标分页。"""
    limit = max(1, min(int(limit or 20), 100))
    cur = decode_cursor(cursor)

    conds = _signal_conds(f)
    from_ts = f.from_ts
    if from_ts is not None:
        conds.append(SignalHistory.received_at >= datetime_from_ts(from_ts))
    if cur is not None:
        # (received_at, signal_id) 的严格小于 —— 复合字典序
        conds.append(
            (SignalHistory.received_at < datetime_from_ts(cur[0]))
            | (
                (SignalHistory.received_at == datetime_from_ts(cur[0]))
                & (SignalHistory.signal_id < cur[1])
            )
        )
    try:
        async with SessionLocal() as s:
            rows = (
                await s.execute(
                    select(SignalHistory)
                    .where(*conds)
                    .order_by(
                        SignalHistory.received_at.desc(),
                        SignalHistory.signal_id.desc(),
                    )
                    .limit(limit + 1)
                )
            ).scalars().all()
    except Exception as e:  # noqa: BLE001
        logger.warning("signal_stats.stream failed: %s", e)
        return {"items": [], "next_cursor": None}

    has_more = len(rows) > limit
    rows = rows[:limit]
    items = await _stream_items(rows)
    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        ts = datetime_to_ts(last.received_at) or 0
        next_cursor = encode_cursor(ts, last.signal_id)
    return {"items": items, "next_cursor": next_cursor}


async def _stream_items(rows: list[SignalHistory]) -> list[dict]:
    """把信号行组装成前端要的形状（含分发明细摘要）。"""
    if not rows:
        return []
    signal_ids = [r.signal_id for r in rows]
    dispatches = await _fetch_dispatches_for_signals(signal_ids)
    by_signal: dict[str, list[dict]] = {}
    for d in dispatches:
        by_signal.setdefault(d["signal_id"], []).append(d)

    # 质量分：历史同类信号的成功率。先验与筛选条无关（见 signal_quality 铁律 3）。
    # 算不出先验时为 None —— 与「分数是 0」是两件事，前端要分开显示。
    quality_by_key = await signal_quality.quality_map(rows)

    items: list[dict] = []
    for r in rows:
        ds = by_signal.get(r.signal_id, [])
        qkey = (
            signal_meta.normalize_signal_type(r.signal_type) or "",
            signal_meta.normalize_timeframe(r.timeframe) or "",
        )
        quality = quality_by_key.get(qkey)
        done = sum(1 for d in ds if d["status"] == "done")
        skipped = sum(1 for d in ds if d["status"] == _DISPATCH_SKIPPED)
        failed = sum(1 for d in ds if d["status"] in (_DISPATCH_FAILED, "offline"))
        sent_at = min(
            (datetime_to_ts(d["sent_at"]) for d in ds if d["sent_at"]), default=None,
        )
        finished_at = max(
            (datetime_to_ts(d["finished_at"]) for d in ds if d["finished_at"]), default=None,
        )
        elapsed_ms = None
        if sent_at is not None and finished_at is not None and finished_at >= sent_at:
            elapsed_ms = int(round((finished_at - sent_at) * 1000))
        items.append({
            "signal_id": r.signal_id,
            "received_at": datetime_to_ts(r.received_at),
            "source": r.source or "tradingview",
            "model": r.model or "normal",
            "action": r.action,
            "symbol": r.symbol,
            "volume": r.volume,
            "status": r.status,
            # 周期 / 信号类型 / 路由状态：统计与路由维度，缺失就是未标注
            "timeframe": r.timeframe,
            "signal_type": r.signal_type,
            "route_state": r.route_state,
            "reject_code": r.reject_code,
            "reject_label": (
                signal_codes.reject_label(r.reject_code) if r.reject_code else None
            ),
            # 质量分：历史同类信号的成功率。None = 没有可用先验（暂无历史）
            "quality": quality,
            "targets": len(ds),
            "done": done,
            "skipped": skipped,
            "failed": failed,
            "elapsed_ms": elapsed_ms,
            "dispatches": [
                {
                    "node_id": d["node_id"],
                    "model": d["model"],
                    "status": d["status"],
                    "skip_code": d["skip_code"],
                    "skip_label": (
                        signal_codes.skip_label(d["skip_code"]) if d["skip_code"] else None
                    ),
                    "skip_reason": d["skip_reason"],
                    "decided_vol": d["decided_vol"],
                    "error": d["error"],
                }
                for d in ds
            ],
        })
    return items


# ---------------------------------------------------------------------------
# 节点命中
# ---------------------------------------------------------------------------
async def nodes(f: SignalFilters, *, node_names: Optional[dict[str, str]] = None) -> dict:
    """按节点聚合命中情况：信号数 / 成功 / 跳过 / 失败 / 成功率 / 时延分位。

    时延分位按节点单独算 —— 端到端时延跨节点可比，但 queued / fill 两段
    **只在节点内部**有意义，因此它们的分位也是「每节点各算各的」。
    """
    names = node_names or {}
    from_ts = f.from_ts
    conds = []
    if from_ts is not None:
        conds.append(SignalDispatch.sent_at >= datetime_from_ts(from_ts))
    if f.symbol:
        conds.append(SignalDispatch.symbol == f.symbol)
    if f.node_id:
        conds.append(SignalDispatch.node_id == f.node_id)

    strat_conds = []
    if from_ts is not None:
        strat_conds.append(GroupTaskDispatch.sent_at >= datetime_from_ts(from_ts))
    if f.symbol:
        strat_conds.append(GroupTaskDispatch.symbol == f.symbol)
    if f.node_id:
        strat_conds.append(GroupTaskDispatch.node_id == f.node_id)

    try:
        async with SessionLocal() as s:
            normal_rows = (
                await s.execute(
                    select(
                        SignalDispatch.node_id,
                        SignalDispatch.signal_id,
                        SignalDispatch.status,
                        SignalDispatch.skip_code,
                        SignalDispatch.sent_at,
                        SignalDispatch.finished_at,
                        SignalDispatch.queued_ms,
                        SignalDispatch.order_to_fill_ms,
                    ).where(*conds)
                )
            ).all()
            strategy_rows = (
                await s.execute(
                    select(
                        GroupTaskDispatch.node_id,
                        GroupTaskDispatch.signal_id,
                        GroupTaskDispatch.status,
                        GroupTaskDispatch.skip_code,
                        GroupTaskDispatch.sent_at,
                        GroupTaskDispatch.finished_at,
                        GroupTaskDispatch.queued_ms,
                        GroupTaskDispatch.order_to_fill_ms,
                    ).where(*strat_conds)
                )
            ).all()
    except Exception as e:  # noqa: BLE001
        logger.warning("signal_stats.nodes failed: %s", e)
        return {"items": []}

    agg: dict[str, dict] = {}
    for r in normal_rows:
        item = agg.setdefault(r[0], {
            "node_id": r[0], "signal_ids": set(), "done": 0, "skipped": 0,
            "failed": 0, "pending": 0, "e2e": [], "queued": [], "fill": [],
            "skip_codes": {},
        })
        item["signal_ids"].add(r[1])
        _accumulate(item, r[2], r[3], r[4], r[5], r[6], r[7])

    for r in strategy_rows:
        item = agg.setdefault(r[0], {
            "node_id": r[0], "signal_ids": set(), "done": 0, "skipped": 0,
            "failed": 0, "pending": 0, "e2e": [], "queued": [], "fill": [],
            "skip_codes": {},
        })
        item["signal_ids"].add(r[1])
        status = "done" if r[2] in _STRATEGY_SUCCESS_STATUS else r[2]
        _accumulate(item, status, r[3], r[4], r[5], r[6], r[7])

    items: list[dict] = []
    for node_id, item in agg.items():
        signal_count = len(item["signal_ids"])
        total = item["done"] + item["skipped"] + item["failed"] + item["pending"]
        top_code = None
        if item["skip_codes"]:
            top_code = max(item["skip_codes"].items(), key=lambda kv: kv[1])[0]
        items.append({
            "node_id": node_id,
            "node_name": names.get(node_id, node_id),
            "signal_count": signal_count,
            "dispatch_count": total,
            "done": item["done"],
            "skipped": item["skipped"],
            "failed": item["failed"],
            "pending": item["pending"],
            "success_rate": _pct(item["done"], total),
            "latency_p50_ms": percentile(item["e2e"], 50),
            "latency_p90_ms": percentile(item["e2e"], 90),
            "top_skip_code": top_code,
            "top_skip_label": signal_codes.skip_label(top_code) if top_code else None,
            # 节点内两段耗时：分节点算，绝不跨节点合并
            "queued_p50_ms": percentile(item["queued"], 50),
            "queued_p90_ms": percentile(item["queued"], 90),
            "fill_p50_ms": percentile(item["fill"], 50),
            "fill_p90_ms": percentile(item["fill"], 90),
        })
    items.sort(key=lambda x: -x["signal_count"])
    return {"items": items}


def _accumulate(item: dict, status: Optional[str], skip_code: Optional[str],
                sent_at: Optional[datetime], finished_at: Optional[datetime],
                queued_ms: Optional[float], fill_ms: Optional[float]) -> None:
    """把一条分发明细累加进某个节点的统计桶。"""
    st = str(status or "")
    if st == "done":
        item["done"] += 1
    elif st == _DISPATCH_SKIPPED:
        item["skipped"] += 1
    elif st in (_DISPATCH_FAILED, "offline"):
        item["failed"] += 1
    else:
        item["pending"] += 1
    if st == _DISPATCH_SKIPPED or skip_code:
        key = skip_code or signal_codes.SKIP_UNKNOWN
        item["skip_codes"][key] = item["skip_codes"].get(key, 0) + 1
    # 端到端：服务端钟（sent_at → finished_at），跨节点可比
    ts = datetime_to_ts(sent_at)
    tf = datetime_to_ts(finished_at)
    if ts is not None and tf is not None and tf >= ts:
        item["e2e"].append((tf - ts) * 1000)
    # 节点内两段：只在节点内部有可比性
    if queued_ms is not None:
        item["queued"].append(float(queued_ms))
    if fill_ms is not None:
        item["fill"].append(float(fill_ms))


# ---------------------------------------------------------------------------
# 时延
# ---------------------------------------------------------------------------
async def latency(f: SignalFilters) -> dict:
    """时延面板：端到端分位（跨节点可比）+ 节点内两段（分节点展示）。"""
    from_ts = f.from_ts
    conds = []
    if from_ts is not None:
        conds.append(SignalDispatch.sent_at >= datetime_from_ts(from_ts))
    if f.symbol:
        conds.append(SignalDispatch.symbol == f.symbol)
    if f.node_id:
        conds.append(SignalDispatch.node_id == f.node_id)

    strat_conds = []
    if from_ts is not None:
        strat_conds.append(GroupTaskDispatch.sent_at >= datetime_from_ts(from_ts))
    if f.symbol:
        strat_conds.append(GroupTaskDispatch.symbol == f.symbol)
    if f.node_id:
        strat_conds.append(GroupTaskDispatch.node_id == f.node_id)

    try:
        async with SessionLocal() as s:
            rows = (
                await s.execute(
                    select(
                        SignalDispatch.node_id,
                        SignalDispatch.sent_at,
                        SignalDispatch.finished_at,
                        SignalDispatch.queued_ms,
                        SignalDispatch.order_to_fill_ms,
                    ).where(*conds)
                )
            ).all()
            strat_rows = (
                await s.execute(
                    select(
                        GroupTaskDispatch.node_id,
                        GroupTaskDispatch.sent_at,
                        GroupTaskDispatch.finished_at,
                        GroupTaskDispatch.queued_ms,
                        GroupTaskDispatch.order_to_fill_ms,
                    ).where(*strat_conds)
                )
            ).all()
    except Exception as e:  # noqa: BLE001
        logger.warning("signal_stats.latency failed: %s", e)
        return _empty_latency(f.window)

    e2e: list[float] = []
    queued: list[float] = []
    fill: list[float] = []
    by_node: dict[str, dict[str, list[float]]] = {}
    node_e2e: dict[str, list[float]] = {}
    daily: dict[int, list[float]] = {}
    today = today_start_utc8()

    def _add(node_id: str, sent_at, finished_at, q, fl) -> None:
        ts = datetime_to_ts(sent_at)
        tf = datetime_to_ts(finished_at)
        if ts is not None and tf is not None and tf >= ts:
            ms = (tf - ts) * 1000
            e2e.append(ms)
            node_e2e.setdefault(node_id, []).append(ms)
            idx = day_index(ts, today)
            daily.setdefault(idx, []).append(ms)
        if q is not None:
            queued.append(float(q))
            by_node.setdefault(node_id, {"queued": [], "fill": []})["queued"].append(float(q))
        if fl is not None:
            fill.append(float(fl))
            by_node.setdefault(node_id, {"queued": [], "fill": []})["fill"].append(float(fl))

    for r in rows:
        _add(r[0], r[1], r[2], r[3], r[4])
    for r in strat_rows:
        _add(r[0], r[1], r[2], r[3], r[4])

    daily_out: list[dict] = []
    for idx in sorted(daily.keys()):
        ts = today + idx * _DAY_SECONDS
        daily_out.append({
            "date": day_label(ts),
            "ts": ts,
            "p50": percentile(daily[idx], 50),
            "p90": percentile(daily[idx], 90),
            "samples": len(daily[idx]),
        })

    slow_nodes = []
    for node_id, vals in node_e2e.items():
        slow_nodes.append({
            "node_id": node_id,
            "p90": percentile(vals, 90),
            "p50": percentile(vals, 50),
            "samples": len(vals),
        })
    slow_nodes.sort(key=lambda x: -(x["p90"] or 0))

    return {
        "window": f.window,
        "end_to_end": {
            "p50": percentile(e2e, 50),
            "p90": percentile(e2e, 90),
            "p99": percentile(e2e, 99),
            "samples": len(e2e),
            "scope": "server_clock",
        },
        "queued_ms": {
            "p50": percentile(queued, 50),
            "p90": percentile(queued, 90),
            "scope": "node_local",
            "note": "节点内排队耗时，仅同一节点内部可比，不可跨节点求和",
        },
        "order_to_fill_ms": {
            "p50": percentile(fill, 50),
            "p90": percentile(fill, 90),
            "scope": "node_local",
            "note": "券商往返耗时，仅同一节点内部可比，不可跨节点求和",
        },
        "by_node": [
            {
                "node_id": node_id,
                "queued_p50": percentile(vals["queued"], 50),
                "queued_p90": percentile(vals["queued"], 90),
                "fill_p50": percentile(vals["fill"], 50),
                "fill_p90": percentile(vals["fill"], 90),
            }
            for node_id, vals in sorted(by_node.items())
        ],
        "slow_nodes": slow_nodes[:20],
        "daily": daily_out,
        "note": (
            "端到端时延用服务端时钟，跨节点可比；"
            "queued_ms / order_to_fill_ms 是节点内部耗时，只在同一节点内可比，禁止跨节点求和。"
        ),
    }


def _empty_latency(window: str) -> dict:
    return {
        "window": window,
        "end_to_end": {"p50": None, "p90": None, "p99": None, "samples": 0,
                       "scope": "server_clock"},
        "queued_ms": {"p50": None, "p90": None, "scope": "node_local"},
        "order_to_fill_ms": {"p50": None, "p90": None, "scope": "node_local"},
        "by_node": [],
        "slow_nodes": [],
        "daily": [],
        "note": "暂无数据",
    }


# ---------------------------------------------------------------------------
# 策略任务（strategy 链路的分组维度）
# ---------------------------------------------------------------------------
async def strategy_tasks(f: SignalFilters, *, group_names: Optional[dict[str, str]] = None) -> dict:
    """策略任务维度：按分组汇总任务数 / 状态 / 已实现盈亏。

    **口径警告**：`realized_profit` 来自 `group_signal_task`（任务收口时节点上报的
    累计值），与 `deal_stats` 的逐笔净盈亏**不是同一口径**，两者不可加总、
    不可互相替代。本接口只输出前者，前端必须分区标注来源。
    """
    names = group_names or {}
    conds = []
    from_ts = f.from_ts
    if from_ts is not None:
        conds.append(GroupSignalTask.created_at >= datetime_from_ts(from_ts))
    if f.group_id:
        conds.append(GroupSignalTask.group_id == f.group_id)
    if f.symbol:
        conds.append(GroupSignalTask.symbol == f.symbol)
    if f.model == "normal":
        # 只看 normal 链路时，strategy 任务表整张不参与
        return {"items": [], "totals": _empty_task_totals(), "tasks": []}
    if f.node_id:
        conds.append(GroupSignalTask.signal_id.in_(
            select(GroupTaskDispatch.signal_id).where(
                GroupTaskDispatch.node_id == f.node_id
            )
        ))

    try:
        async with SessionLocal() as s:
            rows = (
                await s.execute(
                    select(
                        GroupSignalTask.group_id,
                        GroupSignalTask.group_name,
                        GroupSignalTask.status,
                        func.count(GroupSignalTask.task_id),
                        func.sum(GroupSignalTask.total_orders),
                        func.sum(GroupSignalTask.total_volume),
                        func.sum(GroupSignalTask.realized_profit),
                    ).where(*conds).group_by(
                        GroupSignalTask.group_id,
                        GroupSignalTask.group_name,
                        GroupSignalTask.status,
                    )
                )
            ).all()
    except Exception as e:  # noqa: BLE001
        logger.warning("signal_stats.strategy_tasks failed: %s", e)
        return {"items": [], "totals": _empty_task_totals(), "tasks": []}

    by_group: dict[str, dict] = {}
    for r in rows:
        gid = r[0]
        item = by_group.setdefault(gid, {
            "group_id": gid,
            "group_name": r[1] or names.get(gid, gid),
            "tasks": 0, "orders": 0, "volume": 0.0, "realized_profit": 0.0,
            "statuses": {},
        })
        item["tasks"] += int(r[3] or 0)
        item["orders"] += int(r[4] or 0)
        item["volume"] += float(r[5] or 0.0)
        item["realized_profit"] += float(r[6] or 0.0)
        st = str(r[2] or "pending")
        item["statuses"][st] = item["statuses"].get(st, 0) + int(r[3] or 0)

    items = []
    totals = _empty_task_totals()
    for item in by_group.values():
        item["realized_profit"] = round(item["realized_profit"], 2)
        item["volume"] = round(item["volume"], 4)
        items.append(item)
        totals["tasks"] += item["tasks"]
        totals["orders"] += item["orders"]
        totals["volume"] += item["volume"]
        totals["realized_profit"] += item["realized_profit"]
    totals["volume"] = round(totals["volume"], 4)
    totals["realized_profit"] = round(totals["realized_profit"], 2)
    items.sort(key=lambda x: -x["tasks"])
    return {
        "items": items,
        "totals": totals,
        "tasks": await _task_list(f, group_names=names),
    }


async def _task_list(f: SignalFilters, *, group_names: Optional[dict[str, str]] = None,
                     limit: int = 50) -> list[dict]:
    """主任务列表（含子任务明细）。

    汇总表回答「哪个分组任务多」，主任务列表回答「具体是哪个任务卡住了」——
    两者一起给，管理员才能从汇总直接下钻到具体任务。
    """
    names = group_names or {}
    conds = []
    if f.from_ts is not None:
        conds.append(GroupSignalTask.created_at >= datetime_from_ts(f.from_ts))
    if f.group_id:
        conds.append(GroupSignalTask.group_id == f.group_id)
    if f.symbol:
        conds.append(GroupSignalTask.symbol == f.symbol)
    if f.node_id:
        conds.append(GroupSignalTask.signal_id.in_(
            select(GroupTaskDispatch.signal_id).where(
                GroupTaskDispatch.node_id == f.node_id
            )
        ))

    try:
        async with SessionLocal() as s:
            tasks = (
                await s.execute(
                    select(GroupSignalTask)
                    .where(*conds)
                    .order_by(GroupSignalTask.created_at.desc())
                    .limit(limit)
                )
            ).scalars().all()
            if not tasks:
                return []
            task_ids = [t.task_id for t in tasks]
            dispatches = (
                await s.execute(
                    select(GroupTaskDispatch).where(
                        GroupTaskDispatch.task_id.in_(task_ids)
                    )
                )
            ).scalars().all()
    except Exception as e:  # noqa: BLE001
        logger.warning("signal_stats._task_list failed: %s", e)
        return []

    by_task: dict[int, list[dict]] = {}
    for d in dispatches:
        by_task.setdefault(d.task_id, []).append({
            "node_id": d.node_id,
            "status": d.status,
            "skip_code": d.skip_code,
            "skip_label": (
                signal_codes.skip_label(d.skip_code) if d.skip_code else None
            ),
            "skip_reason": d.skip_reason,
            "decided_vol": d.decided_vol,
            "total_orders": d.total_orders,
            "total_volume": d.total_volume,
            "realized_profit": d.realized_profit,
            "finish_reason": d.finish_reason,
            "error": d.error,
            "queued_ms": d.queued_ms,
            "order_to_fill_ms": d.order_to_fill_ms,
            "dispatched_at": datetime_to_ts(d.dispatched_at),
            "finished_at": datetime_to_ts(d.finished_at),
        })

    out: list[dict] = []
    for t in tasks:
        out.append({
            "task_id": t.task_id,
            "signal_id": t.signal_id,
            "group_id": t.group_id,
            "group_name": t.group_name or names.get(t.group_id, t.group_id),
            "strategy_name": t.strategy_name,
            "symbol": t.symbol,
            "action": t.action,
            "volume": t.volume,
            "status": t.status,
            "skip_code": t.skip_code,
            "skip_label": (
                signal_codes.skip_label(t.skip_code) if t.skip_code else None
            ),
            "skip_reason": t.skip_reason,
            "node_count": t.node_count,
            "total_orders": t.total_orders,
            "total_volume": t.total_volume,
            "realized_profit": t.realized_profit,
            "created_at": datetime_to_ts(t.created_at),
            "opened_at": datetime_to_ts(t.opened_at),
            "finished_at": datetime_to_ts(t.finished_at),
            "dispatches": by_task.get(t.task_id, []),
        })
    return out


def _empty_task_totals() -> dict:
    return {"tasks": 0, "orders": 0, "volume": 0.0, "realized_profit": 0.0}


# ---------------------------------------------------------------------------
# 单条信号明细
# ---------------------------------------------------------------------------
async def signal_detail(signal_id: str) -> Optional[dict]:
    """单条信号的完整明细：原始载荷 + 两条链路的主任务 / 子任务。

    统一信号流列表里只放摘要（省带宽），原始 Webhook 载荷、策略任务快照这些
    大字段按需拉 —— 否则一个 20 条的列表页要拉 20 份 JSON。
    """
    try:
        async with SessionLocal() as s:
            sig = (
                await s.execute(
                    select(SignalHistory).where(SignalHistory.signal_id == signal_id)
                )
            ).scalar_one_or_none()
            if sig is None:
                return None
            tasks = (
                await s.execute(
                    select(GroupSignalTask).where(
                        GroupSignalTask.signal_id == signal_id
                    )
                )
            ).scalars().all()
    except Exception as e:  # noqa: BLE001
        logger.warning("signal_stats.signal_detail failed: %s", e)
        return None

    dispatches = await _fetch_dispatches_for_signals([signal_id])

    return {
        "signal_id": sig.signal_id,
        "received_at": datetime_to_ts(sig.received_at),
        "source_ip": sig.source_ip,
        "raw_payload": sig.raw_payload,
        "action": sig.action,
        "symbol": sig.symbol,
        "volume": sig.volume,
        "sl": sig.sl,
        "tp": sig.tp,
        "comment": sig.comment,
        "parsed_ok": sig.parsed_ok,
        "dispatch_mode": sig.dispatch_mode,
        "status": sig.status,
        "source": sig.source or "tradingview",
        "model": sig.model or "normal",
        "reject_code": sig.reject_code,
        "reject_label": (
            signal_codes.reject_label(sig.reject_code) if sig.reject_code else None
        ),
        "dispatches": [
            {
                "node_id": d["node_id"],
                "model": d["model"],
                "status": d["status"],
                "gate_result": d["gate_result"],
                "skip_code": d["skip_code"],
                "skip_label": (
                    signal_codes.skip_label(d["skip_code"]) if d["skip_code"] else None
                ),
                "skip_reason": d["skip_reason"],
                "decided_vol": d["decided_vol"],
                "sent_at": datetime_to_ts(d["sent_at"]),
                "finished_at": datetime_to_ts(d["finished_at"]),
                "queued_ms": d["queued_ms"],
                "order_to_fill_ms": d["order_to_fill_ms"],
                "error": d["error"],
            }
            for d in dispatches
        ],
        "group_tasks": [
            {
                "task_id": t.task_id,
                "group_id": t.group_id,
                "group_name": t.group_name,
                "strategy_name": t.strategy_name,
                "status": t.status,
                "skip_code": t.skip_code,
                "skip_label": (
                    signal_codes.skip_label(t.skip_code) if t.skip_code else None
                ),
                "skip_reason": t.skip_reason,
                "node_count": t.node_count,
                "total_orders": t.total_orders,
                "total_volume": t.total_volume,
                "realized_profit": t.realized_profit,
                "opened_at": datetime_to_ts(t.opened_at),
                "finished_at": datetime_to_ts(t.finished_at),
            }
            for t in tasks
        ],
    }
