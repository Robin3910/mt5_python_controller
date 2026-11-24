"""跟单人当前关系的成交账本与暂停操作。"""
from collections import defaultdict

from fastapi import HTTPException
from sqlalchemy import and_, case, func, select, tuple_
from sqlalchemy.orm import aliased

from . import follow_requests, follow_rules, follow_service, node_lifecycle
from .db import SessionLocal
from .deal_stats import OUT_ENTRIES
from .follow_channels import closed_status
from .orm import Deal, GroupFollowerNode, GroupSignalTask, GroupTaskDispatch, Node, NodeGroup, TradingStrategy


async def set_state(store, principal, body, ip):
    pairs = {(r.group_id, r.node_id) for r in body.relations}
    keys = [key for gid, nid in pairs for key in (node_lifecycle.group_key(gid), node_lifecycle.node_key(nid))]
    target = "paused" if body.paused else "running"
    async with node_lifecycle.locked(*keys), SessionLocal() as session:
        rows = (await session.scalars(select(GroupFollowerNode)
            .join(NodeGroup, NodeGroup.group_id == GroupFollowerNode.group_id)
            .join(Node, Node.node_id == GroupFollowerNode.node_id)
            .join(TradingStrategy, TradingStrategy.strategy_id == NodeGroup.strategy_id)
            .where(tuple_(GroupFollowerNode.group_id, GroupFollowerNode.node_id).in_(pairs),
                   GroupFollowerNode.follower_user_id == principal.user_id,
                   Node.owner_user_id == principal.user_id, NodeGroup.owner_user_id == GroupFollowerNode.leader_user_id,
                   GroupFollowerNode.follow_state.in_(("running", "paused")),
                   TradingStrategy.template_id.in_(follow_rules.FOLLOW_MARKET_TEMPLATE_IDS))
            .with_for_update())).all()
        if len(rows) != len(pairs):
            raise HTTPException(409, "跟单关系不属于本人、尚未生效或已变更，请刷新")
        changed = 0
        for row in rows:
            if row.follow_state != target:
                changed += 1
                row.follow_state = target
        follow_requests.audit(session, principal, "follow_pause_mine" if body.paused else "follow_resume_mine",
                              params=body.model_dump(), after={"changed": changed}, ip=ip)
        await session.commit()
        await follow_service._refresh_groups(store, {gid for gid, _ in pairs})
    return {"changed": changed}


async def own_dispatches(user_id, node_id, group_id, dispatch_ids):
    d, f = GroupTaskDispatch, GroupFollowerNode
    async with SessionLocal() as session:
        return set(await session.scalars(select(d.id)
            .join(f, and_(f.node_id == d.node_id, f.group_id == d.group_id))
            .join(Node, Node.node_id == f.node_id)
            .where(f.follower_user_id == user_id, Node.owner_user_id == user_id, f.follow_state != "stopped",
                   d.node_id == node_id, d.group_id == group_id, d.id.in_(dispatch_ids), d.dispatched_at >= f.joined_at)))


async def read_books(user_id, groups, accounts):
    """只关联本次加入后创建的跟单子任务。收益全量计算，退出成交每组展示最近 50 条。"""
    if not groups:
        return {}
    f, d = GroupFollowerNode, GroupTaskDispatch
    partition = [d.group_id, d.node_id]
    executed = (select(d.group_id, d.node_id, d.magic, d.deal, d.opening_balance,
                       func.coalesce(d.last_report_at, d.opened_at, d.dispatched_at).label("sync"),
                       func.row_number().over(partition_by=partition, order_by=d.id.desc()).label("latest"))
        .join(f, and_(f.node_id == d.node_id, f.group_id == d.group_id))
        .join(Node, Node.node_id == f.node_id)
        .join(GroupSignalTask, GroupSignalTask.task_id == d.task_id)
        .outerjoin(TradingStrategy, TradingStrategy.strategy_id == GroupSignalTask.strategy_id)
        .where(f.follower_user_id == user_id, Node.owner_user_id == user_id,
               f.follow_state != "stopped", d.group_id.in_(groups), d.dispatched_at >= f.joined_at,
               d.magic > 0, d.status.notin_(("pending", "skipped", "offline", "failed")),
               func.coalesce(GroupSignalTask.strategy_snapshot_json["template_id"].as_string(),
                             TradingStrategy.template_id).in_(follow_rules.FOLLOW_MARKET_TEMPLATE_IDS))
        .subquery())
    t = executed.c
    live = {p.get("magic") for account in accounts.values() for p in account.get("positions") or [] if p.get("magic")}
    deal_time = Deal.deal_time - Deal.server_time_offset
    pair = [t.group_id, t.node_id]
    is_exit = Deal.entry_type.in_(OUT_ENTRIES)
    # 终端手工退出可能没有 magic，仅当该仓位的所有开仓成交都属于同一任务时回溯。
    opening = aliased(Deal)
    position_magic = (select(func.min(opening.magic))
        .where(opening.node_id == Deal.node_id, opening.position_id == Deal.position_id,
               opening.entry_type.in_(("IN", "INOUT")), opening.position_id > 0)
        .having(func.min(func.coalesce(opening.magic, 0)) == func.max(func.coalesce(opening.magic, 0)))
        .correlate(Deal).scalar_subquery())
    attributed_magic = func.coalesce(func.nullif(Deal.magic, 0), case((is_exit, position_magic)))
    book = (select(t.group_id, t.node_id, Deal.deal_id, Deal.symbol, Deal.direction, Deal.volume, Deal.price,
                   Deal.entry_type, Deal.reason, Deal.net_profit, Deal.equity, Deal.backfill, deal_time.label("time"),
                   case((Deal.deal_id == t.deal, t.opening_balance)).label("balance"),
                   func.sum(case((is_exit, Deal.net_profit), else_=0)).over(partition_by=pair).label("net"),
                   func.row_number().over(partition_by=pair, order_by=[deal_time, Deal.deal_id]).label("first"),
                   func.row_number().over(partition_by=[t.group_id, is_exit],
                                          order_by=[deal_time.desc(), Deal.deal_id.desc()]).label("recent"))
        .join(Deal, and_(Deal.node_id == t.node_id, attributed_magic == t.magic)).subquery())
    b = book.c
    async with SessionLocal() as session:
        magics = (await session.execute(select(executed).where((t.latest == 1) | t.magic.in_(live)))).all()
        syncs = (await session.execute(select(t.group_id, func.max(t.sync)).group_by(t.group_id))).all()
        deals = (await session.execute(select(book).where((b.first == 1) | (b.entry_type.in_(OUT_ENTRIES) & (b.recent <= 50)))
                                       .order_by(b.time.desc(), b.deal_id.desc()))).all()
    for row in magics:
        groups[row.group_id].setdefault("magics", {}).setdefault(row.node_id, []).append(row.magic)
    for gid, synced in syncs:
        groups[gid]["last_sync"] = synced.timestamp() if synced else None
    rates = []
    equities = defaultdict(list)
    nets = defaultdict(float)
    first_by_node = {}
    for row in deals:
        if row.first == 1:
            nets[row.group_id] += row.net
            opening = row.entry_type in ("IN", "INOUT")
            equities[row.group_id].append(row.equity if opening and not row.backfill else 0)
            first = (row.time, row.deal_id)
            if row.node_id not in first_by_node or first < first_by_node[row.node_id][0]:
                first_by_node[row.node_id] = (first, row.balance if opening else None)
        if row.entry_type in OUT_ENTRIES and row.recent <= 50:
            groups[row.group_id].setdefault("history", []).append({
                "node_id": row.node_id, "deal_id": row.deal_id, "symbol": row.symbol or "",
                "direction": "SELL" if row.direction == "BUY" else "BUY", "volume": row.volume,
                "price": row.price, "closed_at": row.time, "profit": row.net_profit,
                "status": closed_status(row.reason)[1],
            })
    for gid, net in nets.items():
        if all(eq > 0 for eq in equities[gid]):
            rates.append(net / sum(equities[gid]) * 100)
    balances = [first_by_node.get(nid, (None, None))[1] for nid in accounts]
    return {"net_profit": round(sum(nets.values()), 2) if nets else None,
            "average_rate": round(sum(rates) / len(rates), 2) if rates else None,
            "first_balance": round(sum(balances), 2) if balances and None not in balances else None}
