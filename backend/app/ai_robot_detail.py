"""我的机器人详情：按本人节点与机器人分组限定实时持仓及成交统计。"""
from __future__ import annotations

import asyncio

from sqlalchemy import select

from . import deal_stats, follow_rules, permissions
from .db import SessionLocal
from .models import AiMyRobot
from .orm import GroupSignalTask, GroupTaskDispatch, TradingStrategy
from .permissions import Principal
from .redis_store import RedisStore


async def group_node_magics(node_ids: list[str], group_ids: list[str]) -> dict[str, dict[str, set[int]]]:
    scopes = {group_id: {node_id: set() for node_id in node_ids} for group_id in group_ids}
    async with SessionLocal() as session:
        rows = (await session.execute(
            select(
                GroupTaskDispatch.group_id,
                GroupTaskDispatch.node_id,
                GroupTaskDispatch.magic,
                GroupSignalTask.strategy_snapshot_json,
                TradingStrategy.template_id,
            )
            .join(GroupSignalTask, GroupSignalTask.task_id == GroupTaskDispatch.task_id)
            .outerjoin(TradingStrategy, TradingStrategy.strategy_id == GroupSignalTask.strategy_id)
            .where(
                GroupTaskDispatch.node_id.in_(node_ids),
                GroupTaskDispatch.group_id.in_(group_ids),
                GroupTaskDispatch.magic.is_not(None),
            )
        )).all()
    for group_id, node_id, magic, snapshot, fallback in rows:
        template_id = (snapshot or {}).get("template_id") or fallback
        if follow_rules.template_in_market(template_id, follow_rules.AI_MARKET_TEMPLATE_IDS):
            scopes[group_id][node_id].add(int(magic))
    return scopes


async def build_detail(store: RedisStore, principal: Principal, entry: AiMyRobot) -> dict:
    followed_ids = {node["node_id"] for group in entry.groups for node in group.my_nodes}
    requested_ids = set(followed_ids)
    if entry.is_owner and entry.robot.primary_node:
        requested_ids.add(entry.robot.primary_node["node_id"])
    owner = permissions.owner_for_new(principal)
    node_ids = sorted(
        node["node_id"] for node in await store.all_nodes()
        if node["node_id"] in requested_ids
        and permissions.same_owner(node.get("owner_user_id"), owner)
    )
    group_ids = [group.group_id for group in entry.groups]
    scopes = await group_node_magics(node_ids, group_ids)
    node_magics = scopes[entry.robot.group_id]

    accounts = await asyncio.gather(*(store.get_account(node_id) for node_id in node_ids))
    profits = []
    position_count = 0
    volume = 0.0
    updated = []
    for node_id, account in zip(node_ids, accounts):
        if account is None:
            continue
        positions = [
            position for position in account.get("positions") or []
            if int(position.get("magic") or 0) in node_magics[node_id]
        ]
        profits.append(sum(float(position.get("profit") or 0) for position in positions))
        position_count += len(positions)
        volume += sum(float(position.get("volume") or 0) for position in positions)
        if account.get("updated_at"):
            updated.append(float(account["updated_at"]))
    live_available = bool(node_ids) and len(profits) == len(node_ids)
    metrics = {
        "follow_node_count": len(followed_ids.intersection(node_ids)),
        "node_count": len(node_ids),
        "snapshot_node_count": len(profits),
        "floating_profit": round(sum(profits), 2) if live_available else None,
        "position_count": position_count if live_available else None,
        "volume": round(volume, 2) if live_available else None,
        "profitable_node_count": sum(profit > 0 for profit in profits) if live_available else None,
    }
    performance = await deal_stats.leader_batch(node_ids, node_magics=node_magics)
    daily_profit = await deal_stats.daily_net_profit(node_ids, days=7, node_magics=node_magics)
    starts = [
        float(node["joined_at"])
        for group in entry.groups for node in group.my_nodes if node.get("joined_at")
    ]
    if entry.is_owner:
        configs = await asyncio.gather(*(store.get_group(group_id) for group_id in group_ids))
        starts.extend(float(group["created_at"]) for group in configs if group and group.get("created_at"))
        title_group = next(
            (group for group in entry.groups if group.strategy_name == entry.robot.strategy_name), None,
        )
        strategy = await store.get_strategy(title_group.strategy_id) if title_group and title_group.strategy_id else None
        if strategy and permissions.same_owner(strategy.get("owner_user_id"), owner):
            entry.robot.editable_strategy_id = strategy["strategy_id"]
    return {
        "metrics": metrics,
        "performance": performance,
        "daily_profit": daily_profit,
        "started_at": min(starts) if starts else None,
        "updated_at": max(updated) if updated else None,
        "can_control": entry.is_owner and permissions.can_host_ai_robot(principal),
    }
