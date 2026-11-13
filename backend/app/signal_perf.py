"""信号的**绩效统计**口径（信号中心「绩效」面板的唯一实现）。

改动前请先读：
- 本模块是「信号侧绩效」的唯一口径来源。绩效面板、下钻明细都调这里，
  **不允许各页自己算一遍** —— 各处各算一次必然漂移，届时会出现
  「面板显示 62%，成交页显示 58%」这种对不上的情况。
- **两个层级，两套分母，不可混用**：

  | 口径            | 分母                     | 判定                                   |
  |-----------------|--------------------------|----------------------------------------|
  | **信号级胜率**  | 已收口的信号数           | 该信号**全部持仓合计净盈亏 > 0** → 赢   |
  | **持仓级胜率**  | 已收口的持仓数           | 单个 `position_id` 的净盈亏 > 0 → 赢    |

  持仓级与成交侧 `deal_stats` 的口径**完全一致**（同一套逐笔聚合），
  所以两边的数字必须对得上；信号级是本模块独有的口径。

**不做档位级胜率**（本轮拍板）：档位级的盈亏归集需要把「一次加仓」与
「一笔持仓」严格对应上，而 MT5 侧一次加仓可能分多次出场、也可能被合并平仓，
强行对应会产出**看起来精确、实则错位**的数字。档位信息改为在
「入场明细」里逐行展示（入场原因 / 手数 / 成交价 / 盈亏），不做胜率。

**三个必须显式声明的口径陷阱**（详见 §3.3 设计文档）：

1. **同一信号可能在多个分组、多个节点上各开一份仓**。
   信号级胜率**按信号号计一次**（不按节点重复计数）；
   持仓级按 `position_id` 计 —— 每个节点各持一份仓就是各自独立的持仓，
   这是持仓级的自然语义。
2. **未收口的信号（还在跑）不算输**，单列「进行中」，不进分母。
   把进行中的当输会把胜率算得极低，那不是策略的问题，是口径的问题。
3. **`group_signal_task.realized_profit`（任务收口累计值）与逐笔 `deal.net_profit`
   是两套口径，不可加总**。本模块**必须走逐笔 deal 聚合** —— 要按 position
   判胜负、要过滤进行中，还要能拆到入场级，都只能用逐笔数据。

**周期维度的两级语义**（务必分清）：

- `signal_history.timeframe` = **信号级**周期，来自 TV 告警，与加仓方式无关。
  **按周期出胜率一律用它** —— 口径干净。
- `signal_entry_leg.timeframe` = **入场级**周期，是这次入场实际生效的周期。
  **点数（point）与指定价（price）加仓不依赖周期**（配了也不读），
  所以它们的 `timeframe` 恒为 NULL —— 硬凑一个周期会把「点数间距」
  混进「ATR 间距」的分桶，统计出来的数字没有意义。

本模块**不参与任何交易判断**：查询失败、字段缺失都只影响统计展示。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

from sqlalchemy import func, select

from . import group_rules, signal_meta
from .db import SessionLocal
from .orm import (
    Deal,
    GroupSignalTask,
    GroupTaskDispatch,
    SignalEntryLeg,
    SignalHistory,
    TradingStrategy,
)

logger = logging.getLogger(__name__)

# 本模块的统计范围：**只算 AI 智能加仓（tpl_1）**。
# 其它模版（网格 / 趋势 / 以损定量）的仓没有「首仓 + 逐档加仓」这个结构，
# 混在一起算出来的胜率没有可比性。
AI_ADAPTIVE_TEMPLATE_ID = "tpl_1"

# 出场类 entry：只有这些计入「已实现盈亏」与胜率分母（与 deal_store 同口径）
OUT_ENTRIES = ("OUT", "INOUT", "OUT_BY")


# ---------------------------------------------------------------------------
# 聚合容器
# ---------------------------------------------------------------------------
class PerfBucket:
    """一个聚合桶（按周期 / 按类型 / 按入场原因各用一个）。

    信号级与持仓级的胜率**分开存**，因为分母不同、含义不同，
    混在一起算就会得出「看似合理实则无意义」的数字。
    """

    __slots__ = (
        "key", "label",
        "signals_total", "signals_closed", "signals_win",
        "signals_running", "signals_no_trade",
        "signal_profit", "running_profit",
        "positions_total", "positions_closed", "positions_win",
        "position_profit", "running_position_profit",
        "volume", "orders",
    )

    def __init__(self, key: str, label: str) -> None:
        self.key = key
        self.label = label
        # —— 信号级 ——
        self.signals_total = 0
        self.signals_closed = 0
        self.signals_win = 0
        self.signals_running = 0
        self.signals_no_trade = 0
        # 已收口信号的净盈亏合计（进胜率分母的那批）
        self.signal_profit = 0.0
        # 进行中信号的浮动盈亏（**单列，不进胜率分母**）
        self.running_profit = 0.0
        # —— 持仓级 ——
        self.positions_total = 0
        self.positions_closed = 0
        self.positions_win = 0
        self.position_profit = 0.0
        self.running_position_profit = 0.0
        # —— 其它 ——
        self.volume = 0.0
        self.orders = 0

    @property
    def signal_win_rate(self) -> float:
        """信号级胜率；分母 = 已收口且有成交的信号数。"""
        denom = self.signals_closed - self.signals_no_trade
        if denom <= 0:
            return 0.0
        return round(self.signals_win / denom * 100, 2)

    @property
    def position_win_rate(self) -> float:
        """持仓级胜率；分母 = 已收口的持仓数。"""
        if self.positions_closed <= 0:
            return 0.0
        return round(self.positions_win / self.positions_closed * 100, 2)

    @property
    def avg_signal_profit(self) -> float:
        """单信号平均净盈亏。"""
        denom = self.signals_closed - self.signals_no_trade
        if denom <= 0:
            return 0.0
        return round(self.signal_profit / denom, 2)

    @property
    def avg_position_profit(self) -> float:
        """单持仓平均净盈亏。"""
        if self.positions_closed <= 0:
            return 0.0
        return round(self.position_profit / self.positions_closed, 2)

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "signals_total": self.signals_total,
            "signals_closed": self.signals_closed,
            "signals_win": self.signals_win,
            "signals_running": self.signals_running,
            "signals_no_trade": self.signals_no_trade,
            "signal_win_rate": self.signal_win_rate,
            "signal_profit": round(self.signal_profit, 2),
            "avg_signal_profit": self.avg_signal_profit,
            # 进行中浮盈单列：不进胜率分母，但要看得见
            "running_profit": round(self.running_profit, 2),
            "positions_total": self.positions_total,
            "positions_closed": self.positions_closed,
            "positions_win": self.positions_win,
            "position_win_rate": self.position_win_rate,
            "position_profit": round(self.position_profit, 2),
            "avg_position_profit": self.avg_position_profit,
            "running_position_profit": round(self.running_position_profit, 2),
            "volume": round(self.volume, 2),
            "orders": self.orders,
        }


# ---------------------------------------------------------------------------
# 底层聚合
# ---------------------------------------------------------------------------
def _out_net_profit_expr():
    """出场净盈亏的 SQL 表达式：profit + swap + commission。

    与 `deal_stats` / `deal_store` 同口径（net_profit 字段本身也是这个式子，
    这里显式写出来是为了不依赖入库时的字段正确性）。
    """
    return (
        func.coalesce(Deal.profit, 0.0)
        + func.coalesce(Deal.swap, 0.0)
        + func.coalesce(Deal.commission, 0.0)
    )


async def _signal_task_map(signal_ids: list[str]) -> dict[str, list[dict]]:
    """信号 → 该信号下的主任务摘要（含状态与模版）。

    **只保留 AI 智能加仓（tpl_1）的任务**：本模块的胜率口径是围绕
    「首仓 + 逐档加仓」设计的，其它模版（网格 / 趋势 / 以损定量）的仓没有
    这个结构，混进来算出来的胜率没有可比性。
    """
    if not signal_ids:
        return {}
    out: dict[str, list[dict]] = {}
    try:
        async with SessionLocal() as s:
            rows = (
                await s.execute(
                    select(
                        GroupSignalTask.signal_id,
                        GroupSignalTask.task_id,
                        GroupSignalTask.group_id,
                        GroupSignalTask.status,
                        GroupSignalTask.action,
                        GroupSignalTask.symbol,
                        GroupSignalTask.strategy_id,
                        GroupSignalTask.strategy_snapshot_json,
                    ).where(GroupSignalTask.signal_id.in_(signal_ids))
                )
            ).all()
            # 策略快照里没有模版时，回查策略表确认（快照可能来自旧版本）
            need_lookup: set[str] = set()
            for r in rows:
                if not _snapshot_template(r.strategy_snapshot_json):
                    if r.strategy_id:
                        need_lookup.add(str(r.strategy_id))
            tpl_of_strategy: dict[str, str] = {}
            if need_lookup:
                for sid, tid in (
                    await s.execute(
                        select(TradingStrategy.strategy_id, TradingStrategy.template_id)
                        .where(TradingStrategy.strategy_id.in_(list(need_lookup)))
                    )
                ).all():
                    tpl_of_strategy[str(sid)] = str(tid or "")

            for sid, tid, gid, status, action, symbol, strategy_id, snap in rows:
                tpl = _snapshot_template(snap) or tpl_of_strategy.get(str(strategy_id or ""))
                if tpl != AI_ADAPTIVE_TEMPLATE_ID:
                    continue
                out.setdefault(sid, []).append({
                    "task_id": tid,
                    "group_id": gid,
                    "status": status,
                    "action": action,
                    "symbol": symbol,
                    "template_id": tpl,
                })
    except Exception as e:  # noqa: BLE001
        logger.warning("signal_perf._signal_task_map failed: %s", e)
    return out


# ---------------------------------------------------------------------------
# 聚合（纯函数）
# ---------------------------------------------------------------------------
def _aggregate(
    signals: list[Any],
    tasks_by_signal: dict[str, list[dict]],
    profit_by_task: dict[int, dict],
    key_fn: Optional[Any] = None,
) -> dict[str, PerfBucket]:
    """按 `key_fn` 把信号聚合成桶。

    **这是本模块唯一的口径实现** —— `performance()`（按周期 / 按类型分桶）
    与 `signal_quality`（按「类型×周期」联合桶算质量分先验）**共用它**。

    共用的意义：质量分与绩效面板的数字**必须逐位相等**。两边各写一套聚合，
    迟早会因为某天改了一处而对不上 —— 一旦对不上，管理员就再也不信这个数字了。

    `key_fn(sig) -> (key, label)`；返回 None 表示这条信号不参与聚合
    （例如非 `tpl_1` 的信号 —— 不能计入「零成交」，那会把非加仓信号算成亏损）。
    """
    buckets: dict[str, PerfBucket] = {}
    for sig in signals:
        sid = sig.signal_id
        tasks = tasks_by_signal.get(sid, [])
        if not tasks:
            # 这条信号名下**没有 tpl_1 的任务** —— 不属于本模块的统计范围，
            # 整个跳过（不能计入「零成交」，那会把非加仓信号算成亏损）。
            continue

        if key_fn is None:
            key, label = "__total__", "全部"
        else:
            keyed = key_fn(sig)
            if keyed is None:
                continue
            key, label = keyed

        bucket = buckets.setdefault(key, PerfBucket(key, label))

        # 该信号是否还在跑
        running = any(
            str(t.get("status") or "") in group_rules.TASK_ACTIVE for t in tasks
        )
        # 该信号名下所有持仓的净盈亏合计
        signal_profit = 0.0
        position_count = 0
        position_win = 0
        position_profit = 0.0
        volume = 0.0
        orders = 0
        for t in tasks:
            item = profit_by_task.get(int(t["task_id"])) or {}
            positions = item.get("positions") or {}
            for _pid, pl in positions.items():
                signal_profit += float(pl or 0.0)
                position_profit += float(pl or 0.0)
                position_count += 1
                if float(pl or 0.0) > 0:
                    position_win += 1
            volume += float(item.get("volume") or 0.0)
            orders += int(item.get("orders") or 0)

        bucket.signals_total += 1
        bucket.orders += orders
        bucket.volume += volume
        if running:
            # 进行中：不算输，单列，不进胜率分母。
            # 持仓同理 —— 还没平的仓没有最终胜负，进分母会把胜率压低，
            # 那不是策略的问题，是口径的问题。浮盈单列展示。
            bucket.signals_running += 1
            bucket.positions_total += position_count
            bucket.running_profit += signal_profit
            bucket.running_position_profit += position_profit
            continue
        bucket.signals_closed += 1
        bucket.signal_profit += signal_profit
        bucket.position_profit += position_profit
        bucket.positions_total += position_count
        bucket.positions_closed += position_count
        bucket.positions_win += position_win
        if position_count == 0:
            # 收口但零成交：没开仓就没有胜负，不进分母
            bucket.signals_no_trade += 1
            continue
        if signal_profit > 0:
            bucket.signals_win += 1
    return buckets


def _snapshot_template(snapshot: object) -> str:
    """从策略快照里取模版 ID；取不到返回空串。"""
    if not isinstance(snapshot, dict):
        return ""
    return str(snapshot.get("template_id") or "")


async def _magic_profit_map(task_ids: list[int]) -> dict[int, dict]:
    """task_id → 该任务名下各持仓的净盈亏汇总。

    返回 {task_id: {"positions": {(node_id, position_id): profit}, "total": float,
    "position_count": int, "volume": float, "orders": int}}。

    **为什么按 position_id 聚合**：一个持仓可能分多次出场（部分平仓），
    按成交笔数算胜率会把一笔仓算成好几笔 —— 那不是持仓级胜率。

    **为什么键里必须带 node_id**：MT5 的 `position_id` 是**账户内**的编号，
    两个不同节点（不同 MT5 账户）完全可能出现相同的 `position_id`。
    只按 `position_id` 分组会把两个账户的仓合并成一笔 —— 胜率直接算错。
    """
    if not task_ids:
        return {}
    out: dict[int, dict] = {}
    try:
        async with SessionLocal() as s:
            # 1) 按 (node_id, position_id) 聚合出场盈亏
            pos_rows = (
                await s.execute(
                    select(
                        Deal.task_id,
                        Deal.node_id,
                        Deal.position_id,
                        func.sum(_out_net_profit_expr()),
                    )
                    .where(
                        Deal.task_id.in_(task_ids),
                        Deal.position_id.is_not(None),
                        Deal.position_id > 0,
                        Deal.entry_type.in_(OUT_ENTRIES),
                    )
                    .group_by(Deal.task_id, Deal.node_id, Deal.position_id)
                )
            ).all()
            # 2) 手数与成交笔数
            agg_rows = (
                await s.execute(
                    select(
                        Deal.task_id,
                        func.sum(Deal.volume),
                        func.count(Deal.id),
                    )
                    .where(Deal.task_id.in_(task_ids))
                    .group_by(Deal.task_id)
                )
            ).all()
            for tid, node_id, pid, profit in pos_rows:
                item = out.setdefault(int(tid), {
                    "positions": {}, "total": 0.0,
                    "position_count": 0, "volume": 0.0, "orders": 0,
                })
                item["positions"][(str(node_id), int(pid))] = float(profit or 0.0)
                item["total"] += float(profit or 0.0)
                item["position_count"] += 1
            for tid, volume, orders in agg_rows:
                item = out.setdefault(int(tid), {
                    "positions": {}, "total": 0.0,
                    "position_count": 0, "volume": 0.0, "orders": 0,
                })
                item["volume"] = float(volume or 0.0)
                item["orders"] = int(orders or 0)
    except Exception as e:  # noqa: BLE001
        logger.warning("signal_perf._magic_profit_map failed: %s", e)
    return out


async def _dispatch_magic_map(task_ids: list[int]) -> dict[int, list[int]]:
    """task_id → 该任务下各节点子任务的魔术号列表。"""
    if not task_ids:
        return {}
    out: dict[int, list[int]] = {}
    try:
        async with SessionLocal() as s:
            rows = (
                await s.execute(
                    select(GroupTaskDispatch.task_id, GroupTaskDispatch.magic)
                    .where(
                        GroupTaskDispatch.task_id.in_(task_ids),
                        GroupTaskDispatch.magic.is_not(None),
                    )
                )
            ).all()
            for tid, magic in rows:
                out.setdefault(int(tid), []).append(int(magic))
    except Exception as e:  # noqa: BLE001
        logger.warning("signal_perf._dispatch_magic_map failed: %s", e)
    return out


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
async def performance(f: Any, *, group_ids: Optional[list[str]] = None) -> dict:
    """绩效总览：按周期 / 按信号类型 / 按入场原因分桶。

    `f` 是 `signal_stats.SignalFilters`（复用同一套筛选语义，避免
    「概览按品种过滤了、绩效却没过滤」这种对不上的情况）。

    返回：
    {
      "window": ..., "from_ts": ...,
      "totals": {...},                       # 全局合计
      "by_timeframe": [...],                # 按信号级周期
      "by_signal_type": [...],              # 按首仓信号类型
      "by_entry_reason": [...],             # 按每一次入场的入场原因
      "legs": [...],                        # 入场明细（逐行）
      "updated_at": ...
    }
    """
    from . import signal_stats

    signals = await signal_stats._fetch_signals(f)
    result: dict[str, Any] = {
        "window": getattr(f, "window", None),
        "from_ts": int(f.from_ts) if getattr(f, "from_ts", None) is not None else None,
        "totals": {},
        "by_timeframe": [],
        "by_signal_type": [],
        "by_entry_reason": [],
        "legs": [],
        "updated_at": int(time.time()),
    }
    if not signals:
        # 没有任何信号时也返回**完整的零值结构** —— 前端直接读 totals.* 渲染，
        # 返回空 dict 会让它拿到一堆 undefined。
        result["totals"] = PerfBucket("__total__", "全部").to_dict()
        return result

    signal_ids = [s.signal_id for s in signals]
    tasks_by_signal = await _signal_task_map(signal_ids)
    all_task_ids = [t["task_id"] for ts in tasks_by_signal.values() for t in ts]
    profit_by_task = await _magic_profit_map(all_task_ids)

    # —— 信号级聚合 ——
    # 三个桶（合计 / 按周期 / 按类型）走**同一个** `_aggregate` ——
    # 口径只写一遍，质量分模块（signal_quality）也复用它，两边数字必须相等。
    def _tf_key(sig: Any):
        return (
            signal_meta.normalize_timeframe(sig.timeframe) or "__none__",
            signal_meta.timeframe_label(sig.timeframe),
        )

    def _type_key(sig: Any):
        return (
            signal_meta.normalize_signal_type(sig.signal_type) or "__none__",
            signal_meta.signal_type_label(sig.signal_type),
        )

    totals = _aggregate(
        signals, tasks_by_signal, profit_by_task, None,
    ).get("__total__") or PerfBucket("__total__", "全部")
    tf_buckets = _aggregate(signals, tasks_by_signal, profit_by_task, _tf_key)
    type_buckets = _aggregate(signals, tasks_by_signal, profit_by_task, _type_key)

    # —— 入场明细（按入场原因分桶）——
    reason_buckets: dict[str, PerfBucket] = {}
    legs = await _entry_legs(all_task_ids)
    for leg in legs:
        reason = leg.get("reason") or "__none__"
        bucket = reason_buckets.setdefault(
            reason,
            PerfBucket(reason, signal_meta.entry_reason_label(reason)),
        )
        bucket.orders += 1
        bucket.volume += float(leg.get("volume") or 0.0)
        bucket.position_profit += float(leg.get("realized_profit") or 0.0)
        bucket.positions_total += 1
        if leg.get("closed"):
            bucket.positions_closed += 1
            if float(leg.get("realized_profit") or 0.0) > 0:
                bucket.positions_win += 1

    result["totals"] = totals.to_dict()
    result["by_timeframe"] = [
        b.to_dict() for b in sorted(
            tf_buckets.values(),
            key=lambda b: (-b.signals_total, b.key),
        )
    ]
    result["by_signal_type"] = [
        b.to_dict() for b in sorted(
            type_buckets.values(),
            key=lambda b: (-b.signals_total, b.key),
        )
    ]
    result["by_entry_reason"] = [
        b.to_dict() for b in sorted(
            reason_buckets.values(),
            key=lambda b: (-b.orders, b.key),
        )
    ]
    result["legs"] = legs[:500]
    return result


async def _entry_legs(task_ids: list[int]) -> list[dict]:
    """入场明细：逐行取出，并归集该入场对应持仓的净盈亏。

    归集方式：该入场记录了 MT5 订单票号 → 反查 `mt5_deal.order_id` 拿到
    `(node_id, position_id)` → 取该持仓的净盈亏。

    **注意**：这是**入场级的盈亏展示**，**不是档位级胜率** —— 档位级胜率本轮
    明确不做（见模块 docstring）。

    入场明细按 **task_id** 取（而不是 signal_id）：一条信号可能被路由到多个分组，
    每个分组的任务各有自己的入场序列；按 task 取才不会把不同分组的入场混在一起。
    """
    if not task_ids:
        return []
    out: list[dict] = []
    try:
        async with SessionLocal() as s:
            rows = (
                await s.execute(
                    select(SignalEntryLeg)
                    .where(SignalEntryLeg.task_id.in_(task_ids))
                    .order_by(
                        SignalEntryLeg.signal_id.asc(),
                        SignalEntryLeg.task_id.asc(),
                        SignalEntryLeg.seq.asc(),
                    )
                )
            ).scalars().all()
            if not rows:
                return []
            # 反查订单票号 → (node_id, position_id) → 净盈亏
            #
            # **必须按 node_id 一起查**：MT5 的 position_id 是账户内编号，
            # 不同节点（不同账户）会出现相同的 position_id，只按 position_id
            # 反查会把两个账户的盈亏混在一起。
            tickets = {r.order_ticket for r in rows if r.order_ticket}
            pos_of_ticket: dict[int, tuple[str, int]] = {}
            profit_of_pos: dict[tuple[str, int], float] = {}
            if tickets:
                for node_id, pid, oid in (
                    await s.execute(
                        select(Deal.node_id, Deal.position_id, Deal.order_id).where(
                            Deal.order_id.in_(list(tickets)),
                            Deal.position_id.is_not(None),
                            Deal.position_id > 0,
                        )
                    )
                ).all():
                    if oid and pid:
                        pos_of_ticket[int(oid)] = (str(node_id), int(pid))
                for node_id, pid, profit in (
                    await s.execute(
                        select(
                            Deal.node_id,
                            Deal.position_id,
                            func.sum(_out_net_profit_expr()),
                        )
                        .where(
                            Deal.position_id.is_not(None),
                            Deal.position_id > 0,
                            Deal.entry_type.in_(OUT_ENTRIES),
                        )
                        .group_by(Deal.node_id, Deal.position_id)
                    )
                ).all():
                    profit_of_pos[(str(node_id), int(pid))] = float(profit or 0.0)

            for r in rows:
                profit: Optional[float] = None
                closed = False
                if r.order_ticket:
                    pos = pos_of_ticket.get(int(r.order_ticket))
                    if pos is not None:
                        pl = profit_of_pos.get(pos)
                        if pl is not None:
                            profit = round(pl, 2)
                            closed = True
                out.append({
                    "signal_id": r.signal_id,
                    "task_id": r.task_id,
                    "node_id": r.node_id,
                    "symbol": r.symbol,
                    "action": r.action,
                    "seq": r.seq,
                    "reason": r.reason,
                    "reason_label": signal_meta.entry_reason_label(r.reason),
                    "rule_type": r.rule_type,
                    "level_index": r.level_index,
                    # 入场级周期：point / price 恒为 NULL（不依赖周期）
                    "timeframe": r.timeframe,
                    "timeframe_label": signal_meta.timeframe_label(r.timeframe),
                    "volume": r.volume,
                    "price": r.price,
                    "realized_profit": profit,
                    "closed": closed,
                })
    except Exception as e:  # noqa: BLE001
        logger.warning("signal_perf._entry_legs failed: %s", e)
    return out
