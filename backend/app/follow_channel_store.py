"""节点主页频道数据的读取：魔术号 → 策略模版，以及已平仓的历史行。

查询集中在这里。频道划分与持仓聚合在 follow_channels，本模块只取数。
"""
from __future__ import annotations

from sqlalchemy import select

from .db import SessionLocal
from .follow_channels import (
    aggregate_closed_positions,
    channel_of_template,
    is_active_status,
    prefer_magic_row,
)
from .orm import Deal, GroupSignalTask, GroupTaskDispatch, TradingStrategy

_OUT_ENTRIES = ("OUT", "OUT_BY", "INOUT")
_HISTORY_SCAN = 800


def _deal_dict(row: Deal) -> dict:
    return {
        "deal_id": row.deal_id,
        "position_id": row.position_id,
        "symbol": row.symbol,
        "direction": row.direction,
        "entry_type": row.entry_type,
        "reason": row.reason,
        "volume": row.volume,
        "price": row.price,
        "net_profit": row.net_profit,
        "deal_time": row.deal_time,
        "magic": row.magic,
    }


def _template_id(snap: object, fallback: object) -> str:
    if isinstance(snap, dict) and snap.get("template_id"):
        return str(snap.get("template_id"))
    return str(fallback or "")


async def load_follow_channels(node_id: str, live_magics: set[int]) -> dict:
    """该节点 AI / 带单频道的魔术号映射，以及各自最近的历史持仓。

    live_magics 是当前持仓和挂单上的魔术号。历史再补最近平仓成交的魔术号，
    避免把该节点全部历史子任务都拉出来。
    """
    empty = {"magics": [], "ai_history": [], "follow_history": []}
    if not node_id:
        return empty
    async with SessionLocal() as session:
        recent = (
            await session.execute(
                select(Deal.magic)
                .where(
                    Deal.node_id == node_id,
                    Deal.magic.is_not(None),
                    Deal.entry_type.in_(_OUT_ENTRIES),
                )
                .order_by(Deal.deal_time.desc())
                .limit(_HISTORY_SCAN)
            )
        ).all()
        needed = {int(m) for m in live_magics if m}
        needed.update(int(row[0]) for row in recent if row[0])
        if not needed:
            return empty

        rows = (
            await session.execute(
                select(
                    GroupTaskDispatch.magic,
                    GroupTaskDispatch.id,
                    GroupTaskDispatch.group_id,
                    GroupTaskDispatch.status,
                    GroupSignalTask.strategy_snapshot_json,
                    TradingStrategy.template_id,
                )
                .join(
                    GroupSignalTask,
                    GroupSignalTask.task_id == GroupTaskDispatch.task_id,
                )
                .outerjoin(
                    TradingStrategy,
                    TradingStrategy.strategy_id == GroupSignalTask.strategy_id,
                )
                .where(
                    GroupTaskDispatch.node_id == node_id,
                    GroupTaskDispatch.magic.in_(list(needed)),
                )
            )
        ).all()

        picked: dict[int, dict] = {}
        for magic, dispatch_id, group_id, status, snap, strategy_tpl in rows:
            template_id = _template_id(snap, strategy_tpl)
            market = channel_of_template(template_id)
            if not market or magic is None:
                continue
            item = {
                "magic": int(magic),
                "market": market,
                "template_id": template_id,
                "group_id": group_id or "",
                "dispatch_id": int(dispatch_id),
                "active": is_active_status(status),
            }
            picked[int(magic)] = prefer_magic_row(picked.get(int(magic)), item)
        if not picked:
            return empty

        out_rows = (
            await session.execute(
                select(Deal)
                .where(
                    Deal.node_id == node_id,
                    Deal.magic.in_(list(picked)),
                    Deal.entry_type.in_(_OUT_ENTRIES),
                )
                .order_by(Deal.deal_time.desc())
                .limit(_HISTORY_SCAN)
            )
        ).scalars().all()

        by_market: dict[str, list[dict]] = {"ai": [], "follow": []}
        pos_ids: set[int] = set()
        for deal in out_rows:
            info = picked.get(int(deal.magic or 0))
            if not info:
                continue
            by_market[info["market"]].append(_deal_dict(deal))
            if deal.position_id:
                pos_ids.add(int(deal.position_id))

        ins_by_pos: dict[int, list[dict]] = {}
        if pos_ids:
            ins = (
                await session.execute(
                    select(Deal).where(
                        Deal.node_id == node_id,
                        Deal.entry_type == "IN",
                        Deal.position_id.in_(list(pos_ids)),
                    )
                )
            ).scalars().all()
            for deal in ins:
                if deal.position_id:
                    ins_by_pos.setdefault(int(deal.position_id), []).append(_deal_dict(deal))

        history: dict[str, list] = {}
        for market, deals in by_market.items():
            seen = {int(row["position_id"]) for row in deals if row.get("position_id")}
            merged = list(deals)
            for pid in seen:
                merged.extend(ins_by_pos.get(pid, []))
            history[market] = aggregate_closed_positions(merged)
        return {
            "magics": list(picked.values()),
            "ai_history": history.get("ai", []),
            "follow_history": history.get("follow", []),
        }
