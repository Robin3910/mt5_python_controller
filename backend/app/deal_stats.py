"""成交统计口径的**唯一实现**。

改动前请先读：
- 「带单员绩效」= 「主节点绩效」= `node_summary(主节点)`，**同一套函数**，不是两套实现。
  两处各写一份必然漂移，届时会出现「同一节点在订单历史页与带单页胜率不一致」。
- **胜率按 position_id 聚合**（持仓级），不是按成交笔数。
  一笔持仓可能分多次出场（分批止盈 + 移动止损），按成交计数会把一笔持仓算成多笔，
  胜率失真。这是本模块最容易写错的地方。

窗口定义（带单页可切换，四档）：
    7d / 30d（默认）/ 90d / all
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from sqlalchemy import case, func, select

from .db import SessionLocal
from .orm import Deal

logger = logging.getLogger(__name__)

# 出场类 entry：只有这些计入「已实现盈亏」与胜率分母
OUT_ENTRIES = ("OUT", "INOUT", "OUT_BY")


def _out_profit():
    """出场类成交的 net_profit（其它 entry 记 0）。

    用 `case` 而非 `func.iif`：iif 是 SQLite 方言函数，生产 MySQL 上会直接报错。
    """
    return case(
        (Deal.entry_type.in_(OUT_ENTRIES), Deal.net_profit),
        else_=0.0,
    )

# 可选统计窗口（天）。all = 不限时间。
WINDOWS: dict[str, Optional[int]] = {
    "7d": 7,
    "30d": 30,
    "90d": 90,
    "all": None,
}
DEFAULT_WINDOW = "30d"


def window_start(window: str) -> Optional[float]:
    """窗口起点（Unix 秒）；all 返回 None（不限时间）。"""
    days = WINDOWS.get(str(window or "").strip().lower())
    if days is None:
        # 非法窗口按默认档处理，避免「传错参数 = 全量查询」的意外
        days = WINDOWS[DEFAULT_WINDOW]
    if days is None:
        return None
    return time.time() - days * 86400


def _empty_summary() -> dict:
    """空统计的占位。data_available=False 表示「节点没数据」，与「有数据但全亏」区分。"""
    return {
        "total_profit": 0.0,
        "profit_rate": 0.0,
        "win_rate": 0.0,
        "total_trades": 0,
        "win_trades": 0,
        "lose_trades": 0,
        "gross_volume": 0.0,
        "commission_total": 0.0,
        "swap_total": 0.0,
        "base_equity": 0.0,
        "profit_rate_valid": True,
        "data_available": False,
        "updated_at": None,
    }


def _base_conds(
    node_ids: list[str],
    from_ts: Optional[float] = None,
    to_ts: Optional[float] = None,
    symbol: Optional[str] = None,
) -> list:
    """构造查询条件。node_ids 为空列表时调用方应直接返回空统计。"""
    conds = [Deal.node_id.in_(node_ids)]
    if from_ts is not None:
        conds.append(Deal.deal_time >= int(from_ts))
    if to_ts is not None:
        conds.append(Deal.deal_time <= int(to_ts))
    if symbol:
        conds.append(Deal.symbol == symbol.upper())
    return conds


async def node_summary(
    node_id: str,
    from_ts: Optional[float] = None,
    to_ts: Optional[float] = None,
    *,
    symbol: Optional[str] = None,
) -> dict:
    """单个节点的成交汇总（带单页与订单历史页共用这套口径）。"""
    if not node_id:
        return _empty_summary()
    return await leader_batch([node_id], from_ts, to_ts, symbol=symbol)


async def leader_batch(
    node_ids: list[str],
    from_ts: Optional[float] = None,
    to_ts: Optional[float] = None,
    *,
    symbol: Optional[str] = None,
) -> dict:
    """若干节点的合并汇总（带单员名下全部节点 = 其绩效）。

    一次查询算完，避免逐节点查库的 N+1。
    """
    ids = [str(i) for i in (node_ids or []) if i]
    if not ids:
        return _empty_summary()

    conds = _base_conds(ids, from_ts, to_ts, symbol)
    try:
        async with SessionLocal() as s:
            # —— 持仓级聚合（胜率的分子与分母）——
            # 出场盈亏按 position_id 汇总；同一持仓分多次出场只算一笔。
            pos_rows = (
                await s.execute(
                    select(
                        Deal.position_id,
                        func.sum(_out_profit()),
                    )
                    .where(*conds, Deal.position_id.is_not(None), Deal.position_id > 0)
                    .group_by(Deal.position_id)
                )
            ).all()
            # —— 汇总量 ——
            totals = (
                await s.execute(
                    select(
                        func.sum(_out_profit()),
                        func.sum(Deal.volume),
                        func.sum(Deal.commission),
                        func.sum(Deal.swap),
                        func.count(Deal.id),
                        func.max(Deal.equity),
                        func.max(Deal.deal_time),
                    ).where(*conds)
                )
            ).one()
            # 期初权益：窗口内**最早**一笔成交的 equity（不是期末，也不是 max）
            base_equity = (
                await s.execute(
                    select(Deal.equity)
                    .where(*conds, Deal.equity > 0)
                    .order_by(Deal.deal_time.asc(), Deal.id.asc())
                    .limit(1)
                )
            ).scalar_one_or_none()
    except Exception as e:  # noqa: BLE001
        logger.warning("leader_batch failed: %s", e)
        return _empty_summary()

    return _build_summary(
        position_profits=[float(pl or 0.0) for _pid, pl in pos_rows],
        net_profit=totals[0],
        gross_volume=totals[1],
        commission_total=totals[2],
        swap_total=totals[3],
        deal_count=totals[4],
        base_equity=base_equity,
        updated_at=totals[6],
    )


def _build_summary(
    *,
    position_profits: list[float],
    net_profit: Optional[float],
    gross_volume: Optional[float],
    commission_total: Optional[float],
    swap_total: Optional[float],
    deal_count: Optional[int],
    base_equity: Optional[float],
    updated_at: Optional[int],
) -> dict:
    """把原始聚合结果算成最终统计口径。

    **这是口径的唯一实现**：`leader_batch`（单/多节点）与 `leader_batch_multi`
    （按带单员分组批量）都调它，保证订单历史页与带单页的胜率、净盈亏
    永远一致 —— 两处各算一次必然漂移。
    """
    net = float(net_profit or 0.0)
    vol = float(gross_volume or 0.0)
    comm = float(commission_total or 0.0)
    swap = float(swap_total or 0.0)
    count = int(deal_count or 0)

    # 持仓级胜率
    position_count = len(position_profits)
    win_positions = sum(1 for pl in position_profits if pl > 0)
    win_rate = round(win_positions / position_count * 100, 2) if position_count else 0.0

    # 收益率分母：区间期初权益
    equity = float(base_equity or 0.0)
    if equity > 0:
        profit_rate = round(net / equity * 100, 2)
        profit_rate_valid = True
    else:
        profit_rate = 0.0
        profit_rate_valid = False

    return {
        "total_profit": round(net, 2),
        "profit_rate": profit_rate,
        "win_rate": win_rate,
        "total_trades": position_count,
        "win_trades": win_positions,
        "lose_trades": max(0, position_count - win_positions),
        "gross_volume": round(vol, 2),
        "commission_total": round(comm, 2),
        "swap_total": round(swap, 2),
        "base_equity": round(equity, 2),
        "profit_rate_valid": profit_rate_valid,
        # 有成交才认为「有数据」；否则前端显示「暂无数据」而不是 0
        "data_available": count > 0,
        "updated_at": int(updated_at or 0) or None,
    }


async def leader_batch_multi(
    leader_nodes: dict[int, list[str]],
    from_ts: Optional[float] = None,
    to_ts: Optional[float] = None,
) -> dict[int, dict]:
    """按带单员分组批量汇总（{leader_user_id: [node_id, ...]}）。

    带单广场一次要展示 N 个带单员的绩效，逐个调 `leader_batch` 就是 N+1
    查询，节点一多必然卡死。这里**一次查询**算完全部带单员：
    1. 持仓级盈亏按 (node_id, position_id) 聚合；
    2. 汇总量按 node_id 聚合；
    3. 期初权益按 node_id 取窗口内最早一笔；
    4. 再把 node_id 的结果归并到各带单员。

    返回 {leader_user_id: summary}；无数据的带单员也会给出一份
    `data_available=False` 的空统计（前端据此显示「暂无数据」而不是 0）。
    """
    result: dict[int, dict] = {}
    # node_id -> leader_user_id：一个节点只属于一个 owner
    node_owner: dict[str, int] = {}
    for uid, nids in (leader_nodes or {}).items():
        for nid in nids or []:
            nid = str(nid)
            if nid:
                node_owner[nid] = uid
                result.setdefault(uid, {})
    if not node_owner:
        return {uid: _empty_summary() for uid in (leader_nodes or {}).keys()}

    node_ids = list(node_owner.keys())
    conds = _base_conds(node_ids, from_ts, to_ts)
    try:
        async with SessionLocal() as s:
            # 1) 持仓级盈亏（按 node_id + position_id）
            pos_rows = (
                await s.execute(
                    select(
                        Deal.node_id,
                        Deal.position_id,
                        func.sum(_out_profit()),
                    )
                    .where(*conds, Deal.position_id.is_not(None), Deal.position_id > 0)
                    .group_by(Deal.node_id, Deal.position_id)
                )
            ).all()
            # 2) 汇总量（按 node_id）
            total_rows = (
                await s.execute(
                    select(
                        Deal.node_id,
                        func.sum(_out_profit()),
                        func.sum(Deal.volume),
                        func.sum(Deal.commission),
                        func.sum(Deal.swap),
                        func.count(Deal.id),
                        func.max(Deal.deal_time),
                    ).where(*conds)
                    .group_by(Deal.node_id)
                )
            ).all()
            # 3) 期初权益：每个 node_id 在窗口内最早一笔的 equity
            #    用 row_number() 取每组第一行（SQLite 3.25+ / MySQL 8.0+ 都支持）
            rn = func.row_number().over(
                partition_by=Deal.node_id,
                order_by=(Deal.deal_time.asc(), Deal.id.asc()),
            )
            base_sub = (
                select(
                    Deal.node_id.label("node_id"),
                    Deal.equity.label("equity"),
                    Deal.deal_time.label("deal_time"),
                    rn.label("rn"),
                )
                .where(*conds, Deal.equity > 0)
                .subquery()
            )
            base_rows = (
                await s.execute(
                    select(
                        base_sub.c.node_id,
                        base_sub.c.equity,
                        base_sub.c.deal_time,
                    ).where(base_sub.c.rn == 1)
                )
            ).all()
    except Exception as e:  # noqa: BLE001
        logger.warning("leader_batch_multi failed: %s", e)
        return {uid: _empty_summary() for uid in (leader_nodes or {}).keys()}

    # 归并到各带单员
    pos_by_node: dict[str, list[float]] = {}
    for node_id, _pid, pl in pos_rows:
        pos_by_node.setdefault(node_id, []).append(float(pl or 0.0))
    tot_by_node = {r[0]: r for r in total_rows}
    # node_id -> (最早成交时间, 该笔的 equity)
    base_by_node = {r[0]: (int(r[2] or 0), float(r[1] or 0.0)) for r in base_rows}

    agg: dict[int, dict] = {}
    for node_id, uid in node_owner.items():
        pos = pos_by_node.get(node_id, [])
        t = tot_by_node.get(node_id)
        cur = agg.setdefault(
            uid,
            {"positions": [], "net": 0.0, "vol": 0.0, "comm": 0.0, "swap": 0.0,
             "count": 0, "base": None, "updated_at": 0},
        )
        cur["positions"].extend(pos)
        if t:
            cur["net"] += float(t[1] or 0.0)
            cur["vol"] += float(t[2] or 0.0)
            cur["comm"] += float(t[3] or 0.0)
            cur["swap"] += float(t[4] or 0.0)
            cur["count"] += int(t[5] or 0)
            cur["updated_at"] = max(cur["updated_at"], int(t[6] or 0))
        # 期初权益：名下多节点时取**窗口内最早那一笔**的 equity
        # （与 leader_batch 的口径一致：最早，不是最大）
        b = base_by_node.get(node_id)
        if b and (cur["base"] is None or b[0] < cur["base"][0]):
            cur["base"] = b

    out: dict[int, dict] = {}
    for uid in (leader_nodes or {}).keys():
        cur = agg.get(uid)
        if not cur:
            out[uid] = _empty_summary()
            continue
        base = cur["base"]
        out[uid] = _build_summary(
            position_profits=cur["positions"],
            net_profit=cur["net"],
            gross_volume=cur["vol"],
            commission_total=cur["comm"],
            swap_total=cur["swap"],
            deal_count=cur["count"],
            base_equity=base[1] if base else 0.0,
            updated_at=cur["updated_at"],
        )
    return out



async def position_agg(
    node_id: str,
    from_ts: Optional[float] = None,
    to_ts: Optional[float] = None,
) -> list[dict]:
    """持仓级聚合明细（绩效下钻用：每笔持仓的盈亏与出场次数）。"""
    if not node_id:
        return []
    conds = _base_conds([node_id], from_ts, to_ts)
    try:
        async with SessionLocal() as s:
            rows = (
                await s.execute(
                    select(
                        Deal.position_id,
                        func.max(Deal.symbol),
                        func.sum(_out_profit()),
                        func.min(Deal.deal_time),
                        func.max(Deal.deal_time),
                        func.count(Deal.id),
                    )
                    .where(*conds, Deal.position_id.is_not(None), Deal.position_id > 0)
                    .group_by(Deal.position_id)
                    .order_by(func.max(Deal.deal_time).desc())
                )
            ).all()
            return [
                {
                    "position_id": int(pid or 0),
                    "symbol": sym,
                    "net_profit": round(float(pl or 0.0), 2),
                    "open_ts": int(o or 0),
                    "close_ts": int(c or 0),
                    "deal_count": int(cnt or 0),
                }
                for pid, sym, pl, o, c, cnt in rows
            ]
    except Exception as e:  # noqa: BLE001
        logger.warning("position_agg failed: %s", e)
        return []
