"""**马丁止盈档位**的唯一实现：AI 逆势加仓「加了几仓最终止盈」。

一句话定义：

> **档位 = 最终止盈那一刻，该魔术号下的持仓笔数**（含首仓）。
> 1 单 = 只有首仓就止盈；5 单 = 逆势加了 4 仓后止盈。

设计文档：`docs/superpowers/specs/2026-11-13-ai-signal-center-prototype-design.md`

## 三条铁律

1. **按节点分开统计**：统计单元是 **(signal_id, node_id)** —— 一条信号被路由到
   3 个节点，就是 3 个独立单元，各自有自己的档位，**不合并、不取最大值**。
   理由：不同节点是不同 MT5 账户，手数 / 加仓节奏 / 止盈时点都不同，
   合成一个「档位」会产出一个两边都不认的数字。
2. **止损不进止盈分布**：止损收口没有止盈事件，不产生档位
   （`tp_state = "sl"`，前端单独标注，不计入分布分母）。
3. **不落库**：档位是**派生计算**，不新增任何列、不加迁移
   （理由同 `signal_quality`：先验是活的；落库会给交易热路径加写入）。

## 口径细节

- **TP 时刻**：该 `magic` 下所有出场类成交（`entry_type ∈ OUT/INOUT/OUT_BY`）
  且 `reason == 5`（MT5 `DEAL_REASON_TAKE_PROFIT`）里**最早**的那个 `deal_time`。
- **在场判定**：`position_id` 满足「首个 IN 成交时间 ≤ TP 时刻」且
  「最后一个 OUT 成交时间 ≥ TP 时刻（同一秒算在场）」。
- **档位计数**：该魔术号下 TP 时刻**仍在场的全部持仓笔数**（字面口径）。
  逆向链（首仓 + `rule_type=1`）的持仓数另行记录为 `chain_positions`，
  **仅供展示，不参与档位计算** —— 口径必须与「持仓笔数」严格一致。
- **持仓中**：尚无止盈成交 → `tp_level = None`，`tp_state = "running"`，不进分母。

本模块**不参与任何交易判断**：查询失败、字段缺失都只影响统计展示。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy import select

from . import signal_perf
from .db import SessionLocal
from .orm import Deal, GroupSignalTask, GroupTaskDispatch, SignalEntryLeg

logger = logging.getLogger(__name__)

# MT5 DEAL_REASON
DEAL_REASON_SL = 4
DEAL_REASON_TP = 5

# 出场类 entry（与 signal_perf.OUT_ENTRIES 同口径）
OUT_ENTRIES = ("OUT", "INOUT", "OUT_BY")

# 逆势加仓的规则类型（signal_entry_leg.rule_type）
RULE_TYPE_COUNTER_TREND = 1
RULE_TYPE_TREND = 2

# 档位上限（AI 逆势加仓链最多 5 档）；超出的异常值归到 5 并告警
MAX_TP_LEVEL = 5

# 状态
TP_STATE_TP = "tp"
TP_STATE_SL = "sl"
TP_STATE_RUNNING = "running"
TP_STATE_NONE = "none"

# 链条类型
CHAIN_COUNTER_TREND = "counter_trend"
CHAIN_TREND = "trend"


# ---------------------------------------------------------------------------
# 纯函数（便于单测）
# ---------------------------------------------------------------------------
def tp_state_of(
    tp_at: Optional[int], sl_at: Optional[int], has_position: bool,
) -> str:
    """判定一个统计单元的收口状态。

    - 有止盈成交 → `tp`
    - 无止盈但有止损成交 → `sl`
    - 无止盈无止损，但还有持仓 → `running`
    - 其它 → `none`
    """
    if tp_at is not None:
        return TP_STATE_TP
    if sl_at is not None:
        return TP_STATE_SL
    if has_position:
        return TP_STATE_RUNNING
    return TP_STATE_NONE


def position_present(
    first_in: Optional[int], last_out: Optional[int], tp_at: int,
) -> bool:
    """该持仓在 TP 时刻是否在场。

    - 首个 IN 时间 ≤ TP 时刻（已开）
    - 最后一个 OUT 时间 ≥ TP 时刻（还没完全平掉；**同一秒算在场**）
    """
    if first_in is None or first_in > tp_at:
        return False
    if last_out is None:
        return True
    return last_out >= tp_at


def clamp_level(level: Optional[int]) -> Optional[int]:
    """档位归一到 1..MAX_TP_LEVEL；超出的归到上限（并由调用方告警）。"""
    if level is None:
        return None
    try:
        v = int(level)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    return min(v, MAX_TP_LEVEL)


# ---------------------------------------------------------------------------
# 数据获取
# ---------------------------------------------------------------------------
async def _dispatch_rows(task_ids: list[int]) -> list[dict]:
    """task_id → 节点子任务（魔术号 + 节点）。"""
    if not task_ids:
        return []
    out: list[dict] = []
    try:
        async with SessionLocal() as s:
            rows = (
                await s.execute(
                    select(
                        GroupTaskDispatch.task_id,
                        GroupTaskDispatch.node_id,
                        GroupTaskDispatch.magic,
                        GroupTaskDispatch.signal_id,
                        GroupTaskDispatch.group_id,
                        GroupTaskDispatch.status,
                    ).where(
                        GroupTaskDispatch.task_id.in_(task_ids),
                        GroupTaskDispatch.magic.is_not(None),
                    )
                )
            ).all()
            for tid, node_id, magic, sid, gid, status in rows:
                out.append({
                    "task_id": int(tid),
                    "node_id": str(node_id or ""),
                    "magic": int(magic),
                    "signal_id": str(sid or ""),
                    "group_id": str(gid or ""),
                    "status": str(status or ""),
                })
    except Exception as e:  # noqa: BLE001
        logger.warning("signal_tp_levels._dispatch_rows failed: %s", e)
    return out


async def _deal_rows(magics: list[int]) -> list[dict]:
    """magic → 该魔术号下的成交明细（时间 / 方向 / 持仓 / 原因 / 订单）。"""
    if not magics:
        return []
    out: list[dict] = []
    try:
        async with SessionLocal() as s:
            rows = (
                await s.execute(
                    select(
                        Deal.magic,
                        Deal.node_id,
                        Deal.position_id,
                        Deal.order_id,
                        Deal.entry_type,
                        Deal.reason,
                        Deal.deal_time,
                    ).where(
                        Deal.magic.in_(magics),
                        Deal.position_id.is_not(None),
                        Deal.position_id > 0,
                    )
                )
            ).all()
            for magic, node_id, pid, oid, entry, reason, dtime in rows:
                out.append({
                    "magic": int(magic),
                    "node_id": str(node_id or ""),
                    "position_id": int(pid),
                    "order_id": int(oid) if oid else None,
                    "entry_type": str(entry or ""),
                    "reason": int(reason or 0),
                    "deal_time": int(dtime or 0),
                })
    except Exception as e:  # noqa: BLE001
        logger.warning("signal_tp_levels._deal_rows failed: %s", e)
    return out


async def _leg_rows(task_ids: list[int]) -> list[dict]:
    """task_id → 入场明细（首仓 / 逐档加仓）。"""
    if not task_ids:
        return []
    out: list[dict] = []
    try:
        async with SessionLocal() as s:
            rows = (
                await s.execute(
                    select(
                        SignalEntryLeg.task_id,
                        SignalEntryLeg.signal_id,
                        SignalEntryLeg.node_id,
                        SignalEntryLeg.magic,
                        SignalEntryLeg.seq,
                        SignalEntryLeg.rule_type,
                        SignalEntryLeg.level_index,
                        SignalEntryLeg.order_ticket,
                    ).where(SignalEntryLeg.task_id.in_(task_ids))
                )
            ).all()
            for tid, sid, node_id, magic, seq, rule_type, lv, ticket in rows:
                out.append({
                    "task_id": int(tid) if tid is not None else 0,
                    "signal_id": str(sid or ""),
                    "node_id": str(node_id or ""),
                    "magic": int(magic) if magic else None,
                    "seq": int(seq or 0),
                    "rule_type": int(rule_type) if rule_type is not None else None,
                    "level_index": int(lv) if lv is not None else None,
                    "order_ticket": int(ticket) if ticket else None,
                })
    except Exception as e:  # noqa: BLE001
        logger.warning("signal_tp_levels._leg_rows failed: %s", e)
    return out


async def _task_signal_map(task_ids: list[int]) -> dict[int, str]:
    """task_id → signal_id（子任务表里没有信号号时回查主任务）。"""
    if not task_ids:
        return {}
    out: dict[int, str] = {}
    try:
        async with SessionLocal() as s:
            for tid, sid in (
                await s.execute(
                    select(GroupSignalTask.task_id, GroupSignalTask.signal_id)
                    .where(GroupSignalTask.task_id.in_(task_ids))
                )
            ).all():
                out[int(tid)] = str(sid or "")
    except Exception as e:  # noqa: BLE001
        logger.warning("signal_tp_levels._task_signal_map failed: %s", e)
    return out


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
async def tp_units(task_ids: list[int]) -> list[dict]:
    """算出每个「信号 × 节点」统计单元的止盈档位。

    返回（每个单元一条）：
    ```
    {
      "signal_id": "...",
      "node_id": "...",
      "task_id": 123,
      "group_id": "...",
      "magic": 456,
      "tp_level": 3,          # None = 持仓中 / 无数据
      "tp_at": 1731000000,    # TP 成交时刻
      "tp_state": "tp",       # tp / sl / running / none
      "chain": "counter_trend"
    }
    ```

    **只统计 AI 智能加仓（`tpl_1`）的任务** —— 其它模版的加仓语义不同。
    """
    if not task_ids:
        return []
    task_ids = sorted({int(t) for t in task_ids})
    dispatches = await _dispatch_rows(task_ids)
    if not dispatches:
        return []

    magics = sorted({int(d["magic"]) for d in dispatches})
    deals = await _deal_rows(magics)
    legs = await _leg_rows(task_ids)
    task_signals = await _task_signal_map(task_ids)

    # —— 按 magic 归集成交 ——
    # 每个 magic 下的：TP 时刻 / SL 时刻 / 各持仓的首个 IN 与最后一个 OUT
    tp_at: dict[int, int] = {}
    sl_at: dict[int, int] = {}
    pos_first_in: dict[int, dict[int, int]] = {}
    pos_last_out: dict[int, dict[int, int]] = {}
    pos_of_order: dict[int, dict[int, int]] = {}   # magic → order_id → position_id

    for d in deals:
        magic = int(d["magic"])
        pid = int(d["position_id"])
        dtime = int(d["deal_time"])
        entry = d["entry_type"]
        reason = int(d["reason"])
        if entry in OUT_ENTRIES:
            if reason == DEAL_REASON_TP:
                cur = tp_at.get(magic)
                if cur is None or dtime < cur:
                    tp_at[magic] = dtime
            elif reason == DEAL_REASON_SL:
                cur = sl_at.get(magic)
                if cur is None or dtime < cur:
                    sl_at[magic] = dtime
            last = pos_last_out.setdefault(magic, {})
            if pid not in last or dtime > last[pid]:
                last[pid] = dtime
        elif entry == "IN":
            first = pos_first_in.setdefault(magic, {})
            if pid not in first or dtime < first[pid]:
                first[pid] = dtime
            if d["order_id"]:
                pos_of_order.setdefault(magic, {})[int(d["order_id"])] = pid

    # —— 按 magic 归集入场腿 ——
    # 每个 magic 下：逆向链的腿（首仓 + rule_type=1）与全部腿
    chain_legs: dict[int, list[dict]] = {}
    all_legs: dict[int, list[dict]] = {}
    for leg in legs:
        magic = leg.get("magic")
        if magic is None:
            continue
        all_legs.setdefault(int(magic), []).append(leg)
        if leg.get("rule_type") == RULE_TYPE_COUNTER_TREND:
            chain_legs.setdefault(int(magic), []).append(leg)
        elif leg.get("seq") == 0:
            # 首仓的 rule_type 恒为 NULL —— 它是逆向链的起点
            chain_legs.setdefault(int(magic), []).append(leg)

    # —— 逐个统计单元 ——
    out: list[dict] = []
    for d in dispatches:
        magic = int(d["magic"])
        node_id = d["node_id"]
        signal_id = d["signal_id"] or task_signals.get(d["task_id"], "")
        t_at = tp_at.get(magic)
        s_at = sl_at.get(magic)
        first_in = pos_first_in.get(magic, {})
        last_out = pos_last_out.get(magic, {})

        # 该 magic 下此刻仍在场的持仓
        present: set[int] = set()
        if t_at is not None:
            for pid, f_in in first_in.items():
                if position_present(f_in, last_out.get(pid), t_at):
                    present.add(pid)
        else:
            present = set(first_in.keys())

        # 链条选择：优先逆向链（马丁）；没有逆向腿则退回全部腿（顺势链）
        legs_for_chain = chain_legs.get(magic) or []
        chain = CHAIN_COUNTER_TREND if legs_for_chain else CHAIN_TREND
        if not legs_for_chain:
            legs_for_chain = all_legs.get(magic, [])

        # 入场腿 → 持仓（order_ticket → order_id → position_id）
        order_map = pos_of_order.get(magic, {})
        chain_positions: set[int] = set()
        for leg in legs_for_chain:
            ticket = leg.get("order_ticket")
            if not ticket:
                continue
            pid = order_map.get(int(ticket))
            if pid is not None:
                chain_positions.add(int(pid))

        # —— 档位 = 最终止盈那一刻的**持仓笔数**（拍板口径）——
        #
        # 字面口径：那一刻这个魔术号下有几笔仓，就是几档。
        # 逆向链的持仓数另行记录（chain_positions），仅用于展示，
        # **不参与档位计算** —— 否则口径会与「持仓笔数」不一致。
        if t_at is not None:
            level = clamp_level(len(present))
        else:
            level = None

        out.append({
            "signal_id": signal_id,
            "node_id": node_id,
            "task_id": d["task_id"],
            "group_id": d["group_id"],
            "magic": magic,
            "tp_level": level,
            # 逆向链持仓数（展示用，不参与档位计算）
            "chain_positions": len(present & chain_positions),
            "tp_at": t_at,
            "tp_state": tp_state_of(t_at, s_at, bool(present)),
            "chain": chain,
        })
    return out


async def units_for_signals(signal_ids: list[str]) -> list[dict]:
    """给定信号号集合，算出它们的止盈档位统计单元。

    只算 **AI 智能加仓（`tpl_1`）** 的任务（口径与绩效面板一致）。
    """
    if not signal_ids:
        return []
    tasks_by_signal = await signal_perf._signal_task_map(list(signal_ids))
    all_task_ids = [t["task_id"] for ts in tasks_by_signal.values() for t in ts]
    if not all_task_ids:
        return []
    return await tp_units(all_task_ids)


# ---------------------------------------------------------------------------
# 分布
# ---------------------------------------------------------------------------
def build_distribution(
    units: list[dict],
    *,
    node_names: Optional[dict[str, str]] = None,
) -> dict:
    """把统计单元聚成「按节点分开」的分布结构。

    ```
    {
      "closed_tp_units": 78,        # 已止盈的单元数（分布分母）
      "by_node": [
        {"node_id": "...", "node_name": "...", "units": 42,
         "buckets": [{"level": 1, "label": "1单止盈", "count": 9, "pct": 21.4}]}
      ],
      "overall": {"units": 78, "buckets": [...]},
      "states": {"tp": 78, "sl": 5, "running": 3, "none": 1}
    }
    ```
    """
    names = node_names or {}
    levels = list(range(1, MAX_TP_LEVEL + 1))
    labels = {lv: f"{lv}单止盈" for lv in levels}

    by_node: dict[str, dict] = {}
    overall_counts: dict[int, int] = {lv: 0 for lv in levels}
    states = {TP_STATE_TP: 0, TP_STATE_SL: 0, TP_STATE_RUNNING: 0, TP_STATE_NONE: 0}
    closed = 0

    for u in units:
        st = str(u.get("tp_state") or TP_STATE_NONE)
        if st in states:
            states[st] += 1
        lv = u.get("tp_level")
        if lv is None:
            continue
        closed += 1
        node_id = str(u.get("node_id") or "")
        node = by_node.setdefault(node_id, {
            "node_id": node_id,
            "node_name": names.get(node_id) or node_id or "未知节点",
            "units": 0,
            "counts": {lv: 0 for lv in levels},
        })
        node["units"] += 1
        node["counts"][int(lv)] = node["counts"].get(int(lv), 0) + 1
        overall_counts[int(lv)] = overall_counts.get(int(lv), 0) + 1

    def _buckets(counts: dict[int, int], total: int) -> list[dict]:
        out = []
        for lv in levels:
            c = int(counts.get(lv, 0))
            out.append({
                "level": lv,
                "label": labels[lv],
                "count": c,
                "pct": round(c / total * 100, 2) if total else 0.0,
            })
        return out

    node_list = []
    for node in by_node.values():
        node_list.append({
            "node_id": node["node_id"],
            "node_name": node["node_name"],
            "units": node["units"],
            "buckets": _buckets(node["counts"], node["units"]),
        })
    node_list.sort(key=lambda x: (-x["units"], x["node_id"]))

    return {
        "closed_tp_units": closed,
        "by_node": node_list,
        "overall": {
            "units": closed,
            "buckets": _buckets(overall_counts, closed),
        },
        "states": states,
        "levels": levels,
        "level_labels": labels,
    }
