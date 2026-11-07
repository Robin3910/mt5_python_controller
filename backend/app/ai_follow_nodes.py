"""AI 跟单子节点的去重统计、持久运行状态与按节点平仓编排。"""
from __future__ import annotations

import asyncio

from fastapi import HTTPException
from sqlalchemy import select

from . import ai_robot_detail, follow_rules, follow_service, permissions, persist
from .db import SessionLocal
from .follow_channel_api import close_follow_channel
from .models import FollowChannelCloseRequest
from .orm import AuditLog, GroupFollowerNode, Node, NodeGroup, TradingStrategy


async def node_stats(store, principal) -> dict:
    groups = {group["group_id"]: group for group in await store.all_groups()}
    strategies = await follow_service._strategies_of(list(groups.values()), store)
    ai_groups = {
        group_id: group for group_id, group in groups.items()
        if follow_rules.template_in_market(
            (strategies.get(group.get("strategy_id") or "") or {}).get("template_id"),
            follow_rules.AI_MARKET_TEMPLATE_IDS,
        )
    }
    owner = permissions.owner_for_new(principal)
    owned_nodes = {
        node["node_id"]: node for node in await store.all_nodes()
        if permissions.same_owner(node.get("owner_user_id"), owner) and not node.get("is_primary")
    }
    by_node: dict[str, list[dict]] = {}
    for relation in await follow_service.list_followings(principal.user_id, include_stopped=True):
        if relation["group_id"] in ai_groups and relation["node_id"] in owned_nodes:
            by_node.setdefault(relation["node_id"], []).append(relation)
    node_ids = list(by_node)
    group_ids = list({relation["group_id"] for relations in by_node.values() for relation in relations})
    scopes = await ai_robot_detail.group_node_magics(node_ids, group_ids)
    accounts = await asyncio.gather(*(store.get_account(node_id) for node_id in node_ids))
    cards = []
    for node_id, account in zip(node_ids, accounts):
        node = owned_nodes[node_id]
        relations = by_node[node_id]
        states = {relation["follow_state"] for relation in relations}
        follow_state = "running" if "running" in states else "paused" if "paused" in states else "stopped"
        magics = set().union(*(scopes[relation["group_id"]][node_id] for relation in relations))
        positions = [
            position for position in (account or {}).get("positions") or []
            if int(position.get("magic") or 0) in magics
        ]
        related_groups = []
        for relation in relations:
            group = ai_groups[relation["group_id"]]
            strategy = strategies.get(group.get("strategy_id") or "") or {}
            related_groups.append({
                "group_id": group["group_id"], "strategy_name": strategy.get("name"),
                "symbol": strategy.get("symbol"), "follow_state": relation["follow_state"],
            })
        cards.append({
            "node_id": node_id, "name": node.get("name"),
            "mt5_login": node.get("mt5_login"), "mt5_server": node.get("mt5_server"),
            "symbols": list(dict.fromkeys(group["symbol"] for group in related_groups if group["symbol"])),
            "follow_state": follow_state,
            "position_count": len(positions) if account is not None else None,
            "volume": round(sum(float(position.get("volume") or 0) for position in positions), 2) if account is not None else None,
            "floating_profit": round(sum(float(position.get("profit") or 0) for position in positions), 2) if account is not None else None,
            "balance": account.get("balance") if account is not None else None,
            "groups": related_groups,
            "positions": positions,
        })
    active = [card for card in cards if card["follow_state"] != "stopped"]
    available = all(card["position_count"] is not None for card in active)
    return {
        "nodes": cards,
        "summary": {
            "node_count": len(cards), "active_node_count": len(active),
            "position_count": sum(card["position_count"] for card in active) if available else None,
            "volume": round(sum(card["volume"] for card in active), 2) if available else None,
            "floating_profit": round(sum(card["floating_profit"] for card in active), 2) if available else None,
            "profitable_node_count": sum(card["floating_profit"] > 0 for card in active) if available else None,
            "investment_amount": None,
        },
    }


async def set_node_state(
    store, principal, node_ids: list[str], target_state: str, ip: str | None,
    *, group_id: str | None = None,
) -> dict:
    selected_ids = set(node_ids)
    owner = permissions.owner_for_new(principal)
    async with SessionLocal() as session:
        owned_ids = set((await session.scalars(select(Node.node_id).where(
            Node.node_id.in_(selected_ids), Node.is_primary.is_(False),
            Node.owner_user_id.is_(None) if owner is None else Node.owner_user_id == owner,
        ))).all())
        if owned_ids != selected_ids:
            raise HTTPException(status_code=404, detail="跟单子节点不存在或不属于当前账号")
        relations = (await session.scalars(
            select(GroupFollowerNode)
            .join(NodeGroup, NodeGroup.group_id == GroupFollowerNode.group_id)
            .join(TradingStrategy, TradingStrategy.strategy_id == NodeGroup.strategy_id)
            .where(
                GroupFollowerNode.follower_user_id == principal.user_id,
                GroupFollowerNode.node_id.in_(selected_ids),
                TradingStrategy.template_id.in_(follow_rules.AI_MARKET_TEMPLATE_IDS),
                *([GroupFollowerNode.group_id == group_id] if group_id is not None else []),
            ).with_for_update()
        )).all()
        if {relation.node_id for relation in relations} != selected_ids:
            raise HTTPException(status_code=404, detail="节点没有当前账号的 AI 跟单记录")
        before = {str(relation.id): relation.follow_state for relation in relations}
        changed_groups = set()
        changed_count = 0
        for relation in relations:
            if relation.follow_state == target_state:
                continue
            if relation.follow_state == "stopped":
                continue
            if target_state == "running" and relation.follow_state != "paused":
                continue
            relation.follow_state = target_state
            changed_groups.add(relation.group_id)
            changed_count += 1
        session.add(AuditLog(
            operator=principal.username, actor_user_id=principal.user_id,
            action={"running": "resume_ai_follow_nodes", "paused": "pause_ai_follow_nodes", "stopped": "stop_ai_follow_nodes"}[target_state],
            target=group_id,
            params_json={"node_ids": node_ids, "state": target_state, **({"group_id": group_id} if group_id is not None else {})},
            result="ok", ip=ip, category="ai_follow", before_json=before,
            after_json={str(relation.id): relation.follow_state for relation in relations},
        ))
        await session.commit()
    await follow_service._refresh_groups(store, changed_groups)
    return {"node_count": len(selected_ids), "changed_count": changed_count, "state": target_state}


async def close_nodes(store, principal, node_ids: list[str], request, dispatcher, ip: str | None) -> dict:
    data = await node_stats(store, principal)
    cards = {card["node_id"]: card for card in data["nodes"]}
    selected_ids = list(dict.fromkeys(node_ids))
    if any(node_id not in cards for node_id in selected_ids):
        raise HTTPException(status_code=404, detail="跟单子节点不存在或不属于当前账号")
    submitted = 0
    failed = []
    for node_id in selected_ids:
        card = cards[node_id]
        if card["position_count"] is None:
            failed.append({"node_id": node_id, "detail": "节点尚未上报账户快照"})
            continue
        for position in card["positions"]:
            try:
                result = await close_follow_channel(
                    node_id, FollowChannelCloseRequest(market="ai", mode="ticket", ticket=position["ticket"]),
                    request, store=store, group_dispatcher=dispatcher, p=principal,
                )
                submitted += result["closed"]
            except HTTPException as error:
                failed.append({"node_id": node_id, "detail": str(error.detail)})
                break
    result = {"node_count": len(selected_ids), "submitted": submitted, "failed": failed}
    await persist.audit(
        principal.username, "close_ai_follow_nodes", None, {"node_ids": selected_ids},
        "partial" if failed else "ok", ip, category="ai_follow", after=result,
    )
    return result
