"""发单人频道的查询与成员暂停编排。"""
from __future__ import annotations

from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import select

from . import ai_application_store as applications, ai_robot_detail, deal_stats, follow_service, node_lifecycle, permissions
from .ai_channel_rules import application_matches, channel_level
from .db import SessionLocal
from .connections import manager
from .orm import AiFollowApplication, GroupFollowerNode, Node, NodeGroup, NodeGroupMember, TradingStrategy, User


def require_host(principal):
    if not permissions.can_host_ai_robot(principal):
        raise HTTPException(403, "需要 AI 策略及带单能力")


async def owned_group(session, principal, group_id):
    require_host(principal)
    group = await session.get(NodeGroup, group_id)
    if not group or not permissions.same_owner(group.owner_user_id, permissions.owner_for_new(principal)):
        raise HTTPException(404, "机器人不存在")
    return group


async def requests(principal, status="pending", keyword="", page=1, page_size=25):
    require_host(principal)
    async with SessionLocal() as session:
        owner = permissions.owner_for_new(principal)
        groups = (await session.scalars(select(NodeGroup).where(
            NodeGroup.owner_user_id.is_(None) if owner is None else NodeGroup.owner_user_id == owner,
        ))).all()
        group_map = {group.group_id: group for group in groups}
        rows = (await session.scalars(select(AiFollowApplication).where(
            AiFollowApplication.leader_user_id == principal.user_id,
            AiFollowApplication.group_id.in_(group_map),
            AiFollowApplication.status.in_(["pending", "approved", "rejected"]),
        ).order_by(AiFollowApplication.id.desc()))).all()
        serialized = await applications.serialize(session, rows)
        relations = (await session.scalars(select(GroupFollowerNode).where(GroupFollowerNode.group_id.in_(group_map)))).all()
        relation_map = {(r.group_id, r.node_id): r for r in relations}
        for row in serialized:
            for node in row["nodes"]:
                relation = relation_map.get((row["group_id"], node["node_id"]))
                node["leader_paused"] = bool(relation and relation.leader_paused)
                node["follow_state"] = relation.follow_state if relation else None
        displayed = serialized
        if status == "reviewed":
            by_applicant = {}
            for row in serialized:
                if row["status"] not in ("approved", "rejected"):
                    continue
                key = row["group_id"], row["follower_user_id"]
                if key not in by_applicant:
                    by_applicant[key] = {**row, "nodes": []}
                merged = by_applicant[key]
                existing_ids = {node["node_id"] for node in merged["nodes"]}
                merged["nodes"].extend(node for node in row["nodes"] if node["node_id"] not in existing_ids and node["status"] != "cancelled")
                if any(node["status"] == "approved" for node in merged["nodes"]):
                    merged["status"] = "approved"
            displayed = list(by_applicant.values())
        filtered = [row for row in displayed if application_matches(row, keyword)
                    and (row["status"] == "pending" if status == "pending" else row["status"] in ("approved", "rejected"))]
        all_pending = [row["request_id"] for row in serialized if row["status"] == "pending" and application_matches(row, keyword)]
        selected = filtered[(page - 1) * page_size:page * page_size]
        result = []
        for gid in dict.fromkeys(row["group_id"] for row in selected):
            group_rows = [row for row in serialized if row["group_id"] == gid]
            result.append({
                "group_id": gid, "robot_name": group_rows[0]["robot_name"],
                "member_count": len({row["follower_user_id"] for row in group_rows}),
                "pending_count": sum(row["status"] == "pending" for row in group_rows),
                "pending_node_count": sum(len(row["nodes"]) for row in group_rows if row["status"] == "pending"),
                "approved_count": sum(row["status"] == "approved" for row in group_rows),
                "rejected_count": sum(row["status"] == "rejected" for row in group_rows),
                "pending_request_ids": [row["request_id"] for row in group_rows if row["status"] == "pending"],
                "leader_paused": bool([r for r in relations if r.group_id == gid and r.follow_state != "stopped"])
                    and all(r.leader_paused for r in relations if r.group_id == gid and r.follow_state != "stopped"),
                "requests": [row for row in selected if row["group_id"] == gid],
            })
        return {"groups": result, "total": len(filtered), "page": page, "page_size": page_size,
                "all_pending_request_ids": all_pending,
                "all_pending_node_count": sum(len(row["nodes"]) for row in serialized if row["request_id"] in all_pending),
                "stats": {"pending_requests": sum(row["status"] == "pending" for row in serialized),
                          "reviewed_requests": sum(row["status"] != "pending" for row in serialized)}}


async def profile(store, principal):
    require_host(principal)
    owner = permissions.owner_for_new(principal)
    async with SessionLocal() as session:
        user = await session.get(User, principal.user_id)
        pairs = (await session.execute(select(NodeGroup, TradingStrategy).join(
            TradingStrategy, TradingStrategy.strategy_id == NodeGroup.strategy_id,
        ).where(TradingStrategy.template_id == "tpl_1",
                NodeGroup.owner_user_id.is_(None) if owner is None else NodeGroup.owner_user_id == owner))).all()
        primary = await session.scalar(select(Node).where(Node.is_primary.is_(True),
            Node.owner_user_id.is_(None) if owner is None else Node.owner_user_id == owner))
        relations = (await session.scalars(select(GroupFollowerNode).where(
            GroupFollowerNode.group_id.in_([g.group_id for g, _ in pairs]), GroupFollowerNode.follow_state != "stopped",
        ))).all()
        counts = await requests(principal)
        days = max(0, (datetime.now() - min(g.created_at for g, _ in pairs)).days) if pairs else 0
        source_groups = set((await session.scalars(select(NodeGroupMember.group_id).where(
            NodeGroupMember.node_id == primary.node_id,
        ))).all()) if primary and primary.enabled and manager.is_node_online(primary.node_id) else set()
        running = sum(g.enabled and s.enabled and g.accept_ai_follow and g.group_id in source_groups for g, s in pairs) if not user.ai_follow_paused else 0
        level, title = channel_level(len({r.node_id for r in relations}), days, running)
        scopes = await ai_robot_detail.group_node_magics([primary.node_id], [g.group_id for g, _ in pairs]) if primary else {}
        magics = set().union(*(scopes[g.group_id][primary.node_id] for g, _ in pairs)) if scopes else set()
        performance = await deal_stats.leader_batch([primary.node_id], node_magics={primary.node_id: magics}) if primary else None
        return {
            "display_name": user.leader_name or user.display_name or user.username,
            "intro": user.leader_intro or "AI 机器人策略同步频道，接入申请经审核后生效。",
            "level": level, "level_name": title, "running_days": days,
            "symbols": sorted({s.symbol for _, s in pairs}), "running_robots": running,
            "channel_status": "paused" if user.ai_follow_paused else "online" if running else "offline",
            "total_profit": performance["total_profit"] if performance and performance.get("data_available") else None,
            **counts["stats"],
        }


async def set_paused(store, principal, group_id, follower_user_id, paused, ip=None):
    async with node_lifecycle.locked(node_lifecycle.group_key(group_id)), SessionLocal() as session:
        await owned_group(session, principal, group_id)
        query = select(GroupFollowerNode).where(GroupFollowerNode.group_id == group_id, GroupFollowerNode.follow_state != "stopped")
        if follower_user_id is not None:
            query = query.where(GroupFollowerNode.follower_user_id == follower_user_id)
        rows = (await session.scalars(query)).all()
        nodes = {node.node_id: node for node in (await session.scalars(select(Node).where(
            Node.node_id.in_([row.node_id for row in rows]),
        ).order_by(Node.node_id).with_for_update())).all()}
        from .node_credentials import is_admin_user
        for row in rows:
            node = nodes.get(row.node_id)
            if not node or (node.owner_user_id != row.follower_user_id
                            and not (node.owner_user_id is None and await is_admin_user(session, row.follower_user_id))):
                raise HTTPException(409, "跟单节点归属已变更，请刷新频道")
        before = {str(row.id): row.leader_paused for row in rows}
        for row in rows:
            row.leader_paused = paused
        applications.audit(session, principal, "ai_channel_pause" if paused else "ai_channel_resume", group_id,
            before=before, after={str(row.id): paused for row in rows}, ip=ip,
            params={"follower_user_id": follower_user_id})
        await session.commit()
    await follow_service._refresh_groups(store, {group_id})
    return {"changed_count": len(rows), "paused": paused}
