"""AI 申请、整笔审核及存量复审；待审节点不写入生效关系。"""
from __future__ import annotations

from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import select, update

from . import follow_service, node_lifecycle, permissions, rbac_service
from .db import SessionLocal
from .orm import (
    AiFollowApplication as Application,
    AiFollowApplicationNode as ApplicationNode,
    AuditLog, GroupFollowerNode, Node, NodeGroup, NodeGroupMember,
    SystemSetting, TradingStrategy, User,
)

MIGRATION_KEY = "ai_follow_review_v1"


def audit(session, principal, action, target=None, *, before=None, after=None, params=None, ip=None, result="ok", category="ai_follow"):
    session.add(AuditLog(
        operator=principal.username if principal else "system",
        actor_user_id=principal.user_id if principal else None,
        action=action, target=str(target) if target is not None else None,
        category=category, params_json=params, before_json=before,
        after_json=after, result=result, ip=ip,
    ))


def node_snapshot(node: Node) -> dict:
    return {"node_id": node.node_id, "name": node.name, "mt5_login": node.mt5_login,
            "is_primary": bool(node.is_primary), "owner_user_id": node.owner_user_id}


def risk_snapshot(strategy: TradingStrategy) -> dict:
    rules = strategy.config_json or []
    return {key: next((rule[key] for rule in rules if rule.get(key) is not None), None)
            for key in ("max_allow_num", "total_lot_limit", "risk_amount")}


async def validate_group(session, store, group_id: str, leader_user_id: int):
    group = await session.scalar(select(NodeGroup).where(NodeGroup.group_id == group_id).with_for_update())
    user = await session.get(User, leader_user_id)
    principal = await rbac_service.load_principal(store, user.username) if user and user.is_active else None
    if not group or not principal or not permissions.can_host_ai_robot(principal):
        raise HTTPException(404, "机器人不存在或未开放 AI 跟单")
    if not permissions.same_owner(group.owner_user_id, permissions.owner_for_new(principal)):
        raise HTTPException(404, "机器人不存在或未开放 AI 跟单")
    strategy = await session.get(TradingStrategy, group.strategy_id) if group.strategy_id else None
    if not strategy or strategy.template_id != "tpl_1" or not strategy.enabled:
        raise HTTPException(409, "机器人策略已失效")
    if not group.enabled or not group.accept_ai_follow or user.ai_follow_paused:
        raise HTTPException(409, "机器人已暂停或关闭 AI 跟单")
    owner_condition = Node.owner_user_id.is_(None) if group.owner_user_id is None else Node.owner_user_id == group.owner_user_id
    primary = await session.scalar(select(Node).where(owner_condition, Node.is_primary.is_(True), Node.enabled.is_(True)))
    member = await session.scalar(select(NodeGroupMember.id).where(
        NodeGroupMember.group_id == group_id, NodeGroupMember.node_id == primary.node_id,
    )) if primary else None
    if not primary or member is None:
        raise HTTPException(409, "机器人主节点已变更或不可用")
    return group, strategy, primary


async def validate_node(session, node_id: str, owner_user_id, group_id: str) -> Node:
    node = await session.scalar(select(Node).where(Node.node_id == node_id).with_for_update())
    if not node or not permissions.same_owner(node.owner_user_id, owner_user_id):
        raise HTTPException(404, "节点不存在或归属已变更")
    if not node.enabled or node.approval_status != "approved":
        raise HTTPException(409, "节点已停用或尚未开通")
    if await session.scalar(select(NodeGroupMember.id).where(
        NodeGroupMember.group_id == group_id, NodeGroupMember.node_id == node_id,
    )) is not None:
        raise HTTPException(409, "节点已是该机器人分组成员")
    return node


async def _create(session, group, strategy, primary_id, follower_id, nodes, *, leader_user_id, source="join", states=None):
    row = Application(
        group_id=group.group_id, leader_user_id=leader_user_id,
        follower_user_id=follower_id, pending_key=f"{group.group_id}:{follower_id}",
        status="pending", source=source, robot_name=strategy.name, symbol=strategy.symbol,
        strategy_id=strategy.strategy_id, leader_primary_node_id=primary_id, risk_snapshot=risk_snapshot(strategy),
    )
    session.add(row)
    await session.flush()
    for node in nodes:
        previous = (states or {}).get(node.node_id) or {}
        session.add(ApplicationNode(
            application_id=row.id, node_id=node.node_id, owner_user_id=node.owner_user_id,
            snapshot=node_snapshot(node), status="pending",
            original_follow_state=previous.get("follow_state", "running"),
            joined_at=previous.get("joined_at"),
        ))
    return row


async def submit(store, principal, leader_user_id, group_ids, node_ids, *, ip=None, source="join", follow_states=None) -> dict:
    if not permissions.can_menu(principal, permissions.MENU_AI_TRADE):
        raise HTTPException(403, "无权申请 AI 跟单")
    group_ids, node_ids = list(dict.fromkeys(group_ids)), list(dict.fromkeys(node_ids))
    pending, skipped = [], []
    keys = [node_lifecycle.group_key(gid) for gid in group_ids] + [node_lifecycle.node_key(nid) for nid in node_ids]
    async with node_lifecycle.locked(*keys), SessionLocal() as session:
        for gid in group_ids:
            group, strategy, primary = await validate_group(session, store, gid, leader_user_id)
            existing = await session.scalar(select(Application).where(Application.pending_key == f"{gid}:{principal.user_id}"))
            if existing:
                skipped.append({"group_id": gid, "reason": "该申请审核中", "request_id": existing.id})
                continue
            nodes = []
            for nid in node_ids:
                try:
                    node = await validate_node(session, nid, permissions.owner_for_new(principal), gid)
                    relation = await session.scalar(select(GroupFollowerNode).where(
                        GroupFollowerNode.group_id == gid, GroupFollowerNode.node_id == nid,
                    ))
                    if relation and relation.follow_state != "stopped":
                        skipped.append({"group_id": gid, "node_id": nid, "reason": "已跟随该机器人"})
                    else:
                        nodes.append(node)
                except HTTPException as exc:
                    skipped.append({"group_id": gid, "node_id": nid, "reason": exc.detail})
            if not nodes:
                continue
            row = await _create(session, group, strategy, primary.node_id, principal.user_id, nodes,
                                leader_user_id=leader_user_id, source=source,
                                states={nid: {"follow_state": state} for nid, state in (follow_states or {}).items()})
            pending.append({"request_id": row.id, "group_id": gid, "node_ids": [n.node_id for n in nodes]})
        audit(session, principal, "ai_follow_request_create", leader_user_id,
              after={"pending": pending, "skipped": skipped}, ip=ip)
        await session.commit()
    return {"joined": [], "pending": pending, "skipped": skipped}


async def review(store, principal, request_id: int, *, approve: bool, reason="", ip=None) -> dict:
    if not permissions.can_host_ai_robot(principal):
        raise HTTPException(403, "需要 AI 策略及带单能力")
    async with SessionLocal() as read:
        application = await read.get(Application, request_id)
        if not application or (not principal.is_admin and application.leader_user_id != principal.user_id):
            raise HTTPException(404, "申请不存在")
        items = (await read.scalars(select(ApplicationNode).where(ApplicationNode.application_id == request_id))).all()
        keys = [node_lifecycle.group_key(application.group_id), *(node_lifecycle.node_key(n.node_id) for n in items)]
    async with node_lifecycle.locked(*keys), SessionLocal() as session:
        row = await session.scalar(select(Application).where(Application.id == request_id).with_for_update())
        if not row or (not principal.is_admin and row.leader_user_id != principal.user_id):
            raise HTTPException(404, "申请不存在")
        desired = "approved" if approve else "rejected"
        if row.status == desired:
            return {"request_id": row.id, "status": row.status, "skipped": True}
        if row.status != "pending":
            raise HTTPException(409, "申请已处理或失效")
        group = await session.get(NodeGroup, row.group_id)
        if not group or not permissions.owns(principal, group.owner_user_id):
            raise HTTPException(404, "申请不存在")
        nodes = (await session.scalars(select(ApplicationNode).where(ApplicationNode.application_id == row.id))).all()
        if approve:
            follower = await session.get(User, row.follower_user_id)
            follower_principal = await rbac_service.load_principal(store, follower.username) if follower and follower.is_active else None
            if not follower_principal or not permissions.can_menu(follower_principal, permissions.MENU_AI_TRADE):
                raise HTTPException(409, "申请人已失效或无 AI 跟单权限")
            follower_owner = permissions.owner_for_new(follower_principal)
            if any(not permissions.same_owner(item.owner_user_id, follower_owner) for item in nodes):
                raise HTTPException(409, "申请人已不再拥有所选节点")
            group, strategy, primary = await validate_group(session, store, row.group_id, row.leader_user_id)
            if strategy.strategy_id != row.strategy_id or primary.node_id != row.leader_primary_node_id:
                raise HTTPException(409, "机器人配置已变更，请重新申请")
            validated = [await validate_node(session, item.node_id, item.owner_user_id, row.group_id) for item in nodes]
            if len(validated) != len(nodes) or not nodes:
                raise HTTPException(409, "申请节点已失效")
            for item in nodes:
                relation = await session.scalar(select(GroupFollowerNode).where(
                    GroupFollowerNode.group_id == row.group_id, GroupFollowerNode.node_id == item.node_id,
                ))
                if relation and relation.follower_user_id != row.follower_user_id:
                    raise HTTPException(409, "节点跟单归属已变更")
                if not relation:
                    relation = GroupFollowerNode(group_id=row.group_id, node_id=item.node_id,
                        follower_user_id=row.follower_user_id, leader_user_id=row.leader_user_id,
                        leader_primary_node_id=primary.node_id, sort_order=0)
                    session.add(relation)
                relation.follow_state = item.original_follow_state
                relation.leader_paused = False
                relation.joined_at = item.joined_at or datetime.now()
        for item in nodes:
            item.status = desired
        row.status, row.pending_key, row.reason = desired, None, reason or None
        row.reviewer_user_id, row.reviewed_at = principal.user_id, datetime.now()
        output = {"request_id": row.id, "status": desired, "group_id": row.group_id, "node_ids": [n.node_id for n in nodes]}
        audit(session, principal, f"ai_follow_request_{'approve' if approve else 'reject'}", row.id,
              before={"status": "pending"}, after=output, params={"reason": reason}, ip=ip)
        await session.commit()
    await follow_service._refresh_groups(store, {row.group_id})
    return output


async def list_mine(user_id: int) -> list[dict]:
    async with SessionLocal() as session:
        rows = (await session.scalars(select(Application).where(
            Application.follower_user_id == user_id,
        ).order_by(Application.id.desc()))).all()
        return await serialize(session, rows)


async def serialize(session, rows) -> list[dict]:
    if not rows:
        return []
    items = (await session.scalars(select(ApplicationNode).where(ApplicationNode.application_id.in_([r.id for r in rows])))).all()
    nodes = {n.node_id: n for n in (await session.scalars(select(Node).where(Node.node_id.in_([n.node_id for n in items])))).all()}
    users = {u.id: u for u in (await session.scalars(select(User).where(User.id.in_([r.follower_user_id for r in rows])))).all()}
    result = []
    for row in rows:
        user = users.get(row.follower_user_id)
        visible_items = [item for item in items if item.application_id == row.id]
        result.append({
            "request_id": row.id, "group_id": row.group_id, "leader_user_id": row.leader_user_id,
            "follower_user_id": row.follower_user_id, "applicant_name": (user.display_name or user.username) if user else "已删除用户",
            "robot_name": row.robot_name, "symbol": row.symbol, "status": row.status,
            "source": row.source, "reason": row.reason, "risk": row.risk_snapshot or {},
            "created_at": row.created_at.timestamp(),
            "nodes": [{**item.snapshot, "status": item.status,
                       "joined_at": item.joined_at.timestamp() if item.joined_at else row.created_at.timestamp(),
                       "available": item.node_id in nodes and permissions.same_owner(nodes[item.node_id].owner_user_id, item.owner_user_id)}
                      for item in visible_items],
        })
    return result


async def legacy_relations(user_id: int) -> list[dict]:
    applications = await list_mine(user_id)
    return [{
        "group_id": row["group_id"], "node_id": node["node_id"], "follower_user_id": user_id,
        "leader_user_id": row["leader_user_id"], "follow_state": "paused", "leader_paused": True,
        "joined_at": node["joined_at"], "review_status": row["status"],
    } for row in applications if row["source"] == "legacy" and row["status"] in ("pending", "rejected", "cancelled")
      for node in row["nodes"] if node["available"]]


async def cancel_nodes(session, node_ids: list[str], reason: str, *, group_id=None, follower_user_id=None):
    query = select(ApplicationNode, Application).join(Application, Application.id == ApplicationNode.application_id).where(
        ApplicationNode.node_id.in_(node_ids), ApplicationNode.status.in_(["pending", "approved", "rejected"]),
    )
    if group_id is not None:
        query = query.where(Application.group_id == group_id)
    if follower_user_id is not None:
        query = query.where(Application.follower_user_id == follower_user_id)
    rows = (await session.execute(query)).all()
    for item, application in rows:
        item.status = "cancelled"
        if application.status == "pending":
            application.status, application.pending_key, application.reason = "cancelled", None, reason
            await session.execute(update(ApplicationNode).where(ApplicationNode.application_id == application.id).values(status="cancelled"))
    await session.flush()
    for application in {a.id: a for _, a in rows}.values():
        statuses = (await session.scalars(select(ApplicationNode.status).where(ApplicationNode.application_id == application.id))).all()
        if statuses and all(status == "cancelled" for status in statuses):
            application.status, application.pending_key, application.reason = "cancelled", None, reason


async def cancel_applications(session, reason: str, *, group_ids=None, user_id=None, pending_only=False):
    query = select(Application).where(Application.status.in_(["pending"] if pending_only else ["pending", "approved", "rejected"]))
    if group_ids is not None:
        query = query.where(Application.group_id.in_(group_ids))
    if user_id is not None:
        query = query.where((Application.follower_user_id == user_id) | (Application.leader_user_id == user_id))
    rows = (await session.scalars(query)).all()
    for row in rows:
        row.status, row.pending_key, row.reason = "cancelled", None, reason
    if rows:
        await session.execute(update(ApplicationNode).where(ApplicationNode.application_id.in_([row.id for row in rows]))
                              .values(status="cancelled"))


async def migrate_legacy():
    async with SessionLocal() as session:
        if await session.get(SystemSetting, MIGRATION_KEY):
            return
        rows = (await session.execute(select(GroupFollowerNode, NodeGroup, TradingStrategy, Node)
            .join(NodeGroup, NodeGroup.group_id == GroupFollowerNode.group_id)
            .join(TradingStrategy, TradingStrategy.strategy_id == NodeGroup.strategy_id)
            .join(Node, Node.node_id == GroupFollowerNode.node_id)
            .where(TradingStrategy.template_id == "tpl_1", GroupFollowerNode.follow_state != "stopped"))).all()
        grouped = {}
        for relation, group, strategy, node in rows:
            grouped.setdefault((group.group_id, relation.follower_user_id), []).append((relation, group, strategy, node))
        for values in grouped.values():
            relation, group, strategy, _ = values[0]
            await _create(session, group, strategy, relation.leader_primary_node_id,
                relation.follower_user_id, [v[3] for v in values], source="legacy",
                states={v[0].node_id: {"follow_state": v[0].follow_state, "joined_at": v[0].joined_at} for v in values},
                leader_user_id=relation.leader_user_id)
            for previous, *_ in values:
                await session.delete(previous)
        session.add(SystemSetting(key=MIGRATION_KEY, value="done"))
        audit(session, None, "ai_follow_legacy_review", after={"application_count": len(grouped), "node_count": len(rows)})
        await session.commit()
