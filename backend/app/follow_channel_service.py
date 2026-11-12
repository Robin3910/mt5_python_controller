"""本人带单频道的节点审核视图与成员管理。"""
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import delete, select

from . import deal_stats, follow_requests, follow_rules, follow_service, node_lifecycle, permissions
from .db import SessionLocal
from .follow_channel_rules import channel_level, matches
from .orm import FollowRequest, GroupFollowerNode, Node, NodeGroup, TradingStrategy, User


def require_leader(principal):
    if not permissions.can_be_leader(principal):
        raise HTTPException(403, "需要带单能力")


async def latest_requests(session, principal):
    require_leader(principal)
    rows = (await session.scalars(select(FollowRequest).where(
        FollowRequest.leader_user_id == principal.user_id,
    ).order_by(FollowRequest.id.desc()))).all()
    latest = {}
    for row in rows:
        latest.setdefault((row.group_id, row.node_id), row)
    return await follow_requests.serialize(session, list(latest.values()))


def counts(rows):
    return {"pending_nodes": len({r["node_id"] for r in rows if r["status"] == "pending"}),
            "reviewed_nodes": len({r["node_id"] for r in rows if r["status"] != "pending"}),
            "following_nodes": len({r["node_id"] for r in rows if r["status"] == "approved"
                                    and r["follow_state"] == "running" and not r["leader_paused"] and r["marketable"]})}


async def requests(principal, status="pending", keyword="", page=1, page_size=25):
    async with SessionLocal() as session:
        rows = await latest_requests(session, principal)
    selected = [row for row in rows if (row["status"] == "pending") == (status == "pending") and matches(row, keyword)]
    applicants = {}
    for row in selected:
        entry = applicants.setdefault(row["follower_user_id"], {
            "follower_user_id": row["follower_user_id"], "applicant_name": row["applicant_name"], "nodes": [],
        })
        entry["nodes"].append(row)
    return {"applicants": list(applicants.values())[(page - 1) * page_size:page * page_size],
            "total": len(applicants), "page": page, "page_size": page_size,
            "all_pending_request_ids": [row["request_id"] for row in selected if row["status"] == "pending"],
            "stats": counts(rows)}


async def profile(store, principal):
    require_leader(principal)
    owner = permissions.owner_for_new(principal)
    async with SessionLocal() as session:
        user = await session.get(User, principal.user_id)
        rows = await latest_requests(session, principal)
        groups = (await session.scalars(select(NodeGroup).outerjoin(
            TradingStrategy, TradingStrategy.strategy_id == NodeGroup.strategy_id,
        ).where(NodeGroup.owner_user_id.is_(None) if owner is None else NodeGroup.owner_user_id == owner,
                (NodeGroup.strategy_id.is_(None)) | TradingStrategy.template_id.in_(follow_rules.FOLLOW_MARKET_TEMPLATE_IDS)))).all()
        primary = await session.scalar(select(Node).where(
            Node.owner_user_id.is_(None) if owner is None else Node.owner_user_id == owner,
            Node.is_primary.is_(True),
        ))
        strategies = (await session.scalars(select(TradingStrategy).where(
            TradingStrategy.strategy_id.in_([g.strategy_id for g in groups if g.strategy_id]),
        ))).all()
        days = max(0, (datetime.now() - min(g.created_at for g in groups)).days) if groups else 0
        stats = counts(rows)
        if user.follow_paused:
            stats["following_nodes"] = 0
        level, label = channel_level(stats["following_nodes"], days)
        performance = await deal_stats.leader_batch([primary.node_id]) if primary else None
        return {"display_name": user.leader_name or user.display_name or user.username,
                "intro": user.leader_intro or "跟单申请经审核后生效，策略开平仓自动同步。",
                "level": level, "level_name": label, "running_days": days,
                "symbols": sorted({s.symbol for s in strategies if s.symbol}),
                "total_profit": performance["total_profit"] if performance and performance.get("data_available") else None,
                "follow_paused": bool(user.follow_paused), **stats}


async def members(store, principal, request_ids, *, paused=None, remove=False, ip=None):
    require_leader(principal)
    ids = set(request_ids)
    async with SessionLocal() as read:
        rows = (await read.scalars(select(FollowRequest).where(
            FollowRequest.id.in_(ids), FollowRequest.leader_user_id == principal.user_id,
        ))).all()
        if len(rows) != len(ids):
            raise HTTPException(404, "跟单记录不存在")
        keys = [key for row in rows for key in (node_lifecycle.group_key(row.group_id), node_lifecycle.node_key(row.node_id))]
    async with node_lifecycle.locked(*keys), SessionLocal() as session:
        rows = (await session.scalars(select(FollowRequest).where(FollowRequest.id.in_(ids)).with_for_update())).all()
        changed, group_ids = [], set()
        for row in rows:
            if row.leader_user_id != principal.user_id or row.status != "approved":
                raise HTTPException(409, "记录状态已变化，请刷新")
            group = await session.get(NodeGroup, row.group_id)
            strategy = await session.get(TradingStrategy, group.strategy_id) if group and group.strategy_id else None
            if not group or not permissions.same_owner(group.owner_user_id, permissions.owner_for_new(principal)) or (
                strategy and not follow_rules.template_in_market(strategy.template_id, follow_rules.FOLLOW_MARKET_TEMPLATE_IDS)
            ):
                raise HTTPException(409, "该记录已不属于当前带单频道")
            node = await session.scalar(select(Node).where(Node.node_id == row.node_id).with_for_update())
            if not node or not permissions.same_owner(node.owner_user_id, row.owner_user_id):
                raise HTTPException(409, "节点归属已变化，请刷新")
            relation = await session.scalar(select(GroupFollowerNode).where(
                GroupFollowerNode.group_id == row.group_id, GroupFollowerNode.node_id == row.node_id,
                GroupFollowerNode.follower_user_id == row.follower_user_id,
            ).with_for_update())
            if not relation:
                raise HTTPException(409, "跟单关系已移出，请刷新")
            before = {"leader_paused": bool(relation.leader_paused), "status": row.status}
            if remove:
                await session.execute(delete(GroupFollowerNode).where(GroupFollowerNode.id == relation.id))
                row.status, row.reason = "cancelled", "带单员已移出该节点"
            else:
                relation.leader_paused = paused
            follow_requests.audit(session, principal, "follow_channel_remove" if remove else "follow_channel_pause" if paused else "follow_channel_resume",
                row.id, before=before, after={"status": row.status, "leader_paused": paused}, ip=ip)
            changed.append(row.id)
            group_ids.add(row.group_id)
        await session.commit()
    await follow_service._refresh_groups(store, group_ids)
    return {"changed_request_ids": changed, "changed_count": len(changed)}
