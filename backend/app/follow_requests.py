"""带单节点申请、独立审核及生命周期清理。待审记录不进入分发缓存。"""
from __future__ import annotations

from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import select

from . import follow_rules, follow_service, node_lifecycle, permissions, rbac_service
from .db import SessionLocal
from .orm import (
    AuditLog, FollowRequest, GroupFollowerNode, Node, NodeGroup, NodeGroupMember,
    SystemSetting, TradingStrategy, User,
)

MIGRATION_KEY = "follow_review_v1"


def audit(session, principal, action, target=None, *, before=None, after=None, params=None, ip=None):
    session.add(AuditLog(
        operator=principal.username if principal else "system",
        actor_user_id=principal.user_id if principal else None, action=action,
        target=str(target) if target is not None else None, category="follow",
        before_json=before, after_json=after, params_json=params, result="ok", ip=ip,
    ))


async def validate_group(session, store, group_id, leader_user_id):
    user = await session.get(User, leader_user_id)
    principal = await rbac_service.load_principal(store, user.username) if user and user.is_active else None
    if not principal or not permissions.can_be_leader(principal):
        raise HTTPException(404, "带单员不存在或未开放带单")
    if user.follow_paused:
        raise HTTPException(400, follow_rules.PAUSED_FOLLOW_REASON)
    group = await session.scalar(select(NodeGroup).where(NodeGroup.group_id == group_id).with_for_update())
    owner = permissions.owner_for_new(principal)
    if not group or not permissions.same_owner(group.owner_user_id, owner):
        raise HTTPException(404, "带单分组不存在")
    primary = await session.scalar(select(Node).where(
        Node.owner_user_id.is_(None) if owner is None else Node.owner_user_id == owner,
        Node.is_primary.is_(True), Node.enabled.is_(True),
    ).with_for_update())
    member = await session.scalar(select(NodeGroupMember.id).where(
        NodeGroupMember.group_id == group_id, NodeGroupMember.node_id == primary.node_id,
    )) if primary else None
    if not primary or member is None or not group.enabled:
        raise HTTPException(409, "带单分组或主节点已失效")
    strategy = await session.get(TradingStrategy, group.strategy_id) if group.strategy_id else None
    if group.strategy_id and (not strategy or not strategy.enabled or not follow_rules.template_in_market(
        strategy.template_id, follow_rules.FOLLOW_MARKET_TEMPLATE_IDS,
    )):
        raise HTTPException(409, "该策略不在带单市场内或已停用")
    return group, strategy, primary, user


async def validate_node(session, principal, node_id, group_id):
    node = await session.scalar(select(Node).where(Node.node_id == node_id).with_for_update())
    if not node or not permissions.same_owner(node.owner_user_id, permissions.owner_for_new(principal)):
        raise HTTPException(404, "节点不存在或不属于你")
    if not node.enabled or node.approval_status != "approved":
        raise HTTPException(409, "节点已停用或尚未开通")
    if await session.scalar(select(NodeGroupMember.id).where(
        NodeGroupMember.group_id == group_id, NodeGroupMember.node_id == node_id,
    )) is not None:
        raise HTTPException(409, "节点已是该分组的自有成员")
    return node


def snapshot(node, group, strategy, user):
    return {"name": node.name, "mt5_login": node.mt5_login, "is_primary": bool(node.is_primary),
            "group_name": group.name, "strategy_name": strategy.name if strategy else None,
            "symbol": strategy.symbol if strategy else None,
            "template_id": strategy.template_id if strategy else None,
            "leader_name": user.leader_name or user.display_name or user.username}


async def submit(store, principal, leader_user_id, group_ids, node_ids, *, source="join", actor=None, ip=None, follow_states=None):
    if not permissions.can_menu(principal, permissions.MENU_FOLLOW):
        raise HTTPException(403, "无权申请带单")
    group_ids = list(dict.fromkeys(str(g).strip() for g in group_ids if str(g).strip()))
    node_ids = list(dict.fromkeys(str(n).strip() for n in node_ids if str(n).strip()))
    pending, skipped = [], []
    duplicate = False
    primary_id = None
    keys = [node_lifecycle.group_key(g) for g in group_ids] + [node_lifecycle.node_key(n) for n in node_ids]
    async with node_lifecycle.locked(*keys), SessionLocal() as session:
        for gid in group_ids:
            group, strategy, primary, user = await validate_group(session, store, gid, leader_user_id)
            primary_id = primary.node_id
            for nid in node_ids:
                try:
                    node = await validate_node(session, principal, nid, gid)
                except HTTPException as exc:
                    skipped.append({"group_id": gid, "node_id": nid, "reason": exc.detail})
                    continue
                existing = await session.scalar(select(FollowRequest).where(FollowRequest.pending_key == f"{gid}:{nid}"))
                relation = await session.scalar(select(GroupFollowerNode).where(
                    GroupFollowerNode.group_id == gid, GroupFollowerNode.node_id == nid,
                ))
                if existing or (relation and relation.follow_state != "stopped"):
                    duplicate = True
                    skipped.append({"group_id": gid, "node_id": nid,
                                    "reason": "该节点申请审核中" if existing else "已跟随该带单分组"})
                    continue
                row = FollowRequest(group_id=gid, node_id=nid, follower_user_id=principal.user_id,
                    leader_user_id=leader_user_id, owner_user_id=node.owner_user_id,
                    leader_primary_node_id=primary.node_id, strategy_id=group.strategy_id,
                    snapshot={**snapshot(node, group, strategy, user), "follow_state": (follow_states or {}).get(nid, "running")}, source=source,
                    pending_key=f"{gid}:{nid}", status="pending")
                session.add(row)
                await session.flush()
                pending.append({"request_id": row.id, "group_id": gid, "node_id": nid})
        if not pending and not duplicate:
            reason = skipped[0]["reason"] if skipped else "没有可用分组"
            raise HTTPException(400, f"没有可用于跟单的节点或分组：{reason}")
        result = {"joined": [], "pending": pending, "skipped": skipped, "leader_primary_node_id": primary_id}
        audit(session, actor or principal, "follow_request_create", leader_user_id,
              after=result, params={"source": source, "follower_user_id": principal.user_id}, ip=ip)
        await session.commit()
    return result


async def review(store, principal, request_id, *, approve, reason="", ip=None):
    if not permissions.can_be_leader(principal):
        raise HTTPException(403, "需要带单能力")
    async with SessionLocal() as read:
        initial = await read.get(FollowRequest, request_id)
        if not initial or initial.leader_user_id != principal.user_id:
            raise HTTPException(404, "申请不存在")
        keys = [node_lifecycle.group_key(initial.group_id), node_lifecycle.node_key(initial.node_id)]
    async with node_lifecycle.locked(*keys), SessionLocal() as session:
        row = await session.scalar(select(FollowRequest).where(FollowRequest.id == request_id).with_for_update())
        if not row or row.leader_user_id != principal.user_id:
            raise HTTPException(404, "申请不存在")
        desired = "approved" if approve else "rejected"
        repeated = row.status == desired
        if not repeated:
            if row.status != "pending":
                raise HTTPException(409, "申请已处理或失效，请刷新")
            if approve:
                group, strategy, primary, _ = await validate_group(session, store, row.group_id, row.leader_user_id)
                if group.strategy_id != row.strategy_id or primary.node_id != row.leader_primary_node_id or (
                    strategy and strategy.symbol != row.snapshot.get("symbol")
                ):
                    raise HTTPException(409, "带单源或策略已变更，请重新申请")
                follower = await session.get(User, row.follower_user_id)
                applicant = await rbac_service.load_principal(store, follower.username) if follower and follower.is_active else None
                if not applicant or not permissions.can_menu(applicant, permissions.MENU_FOLLOW):
                    raise HTTPException(409, "申请人已失效或无跟单权限")
                node = await validate_node(session, applicant, row.node_id, row.group_id)
                if not permissions.same_owner(node.owner_user_id, row.owner_user_id):
                    raise HTTPException(409, "节点归属已变更")
                relation = await session.scalar(select(GroupFollowerNode).where(
                    GroupFollowerNode.group_id == row.group_id, GroupFollowerNode.node_id == row.node_id,
                ))
                if relation and relation.follower_user_id != row.follower_user_id:
                    raise HTTPException(409, "跟单关系归属已变更")
                await follow_service.write_following(session, group_id=row.group_id, node_id=row.node_id,
                    follower_user_id=row.follower_user_id, leader_user_id=row.leader_user_id,
                    leader_primary_node_id=primary.node_id, follow_state=row.snapshot.get("follow_state", "running"))
            row.status, row.pending_key, row.reason = desired, None, reason or None
            row.reviewer_user_id, row.reviewed_at = principal.user_id, datetime.now()
        result = {"request_id": row.id, "group_id": row.group_id, "node_id": row.node_id,
                  "status": desired, "skipped": repeated}
        audit(session, principal, "follow_request_approve" if approve else "follow_request_reject",
              row.id, before={"status": desired if repeated else "pending"}, after=result, params={"reason": reason}, ip=ip)
        await session.commit()
    if approve:
        await follow_service._refresh_groups(store, {row.group_id})
    return result


async def cancel(session, reason, *, node_ids=None, group_ids=None, user_id=None,
                 leader_user_id=None, follower_user_id=None, primary_ids=None, pending_only=True):
    query = select(FollowRequest).where(FollowRequest.status.in_(["pending"] if pending_only else ["pending", "approved", "rejected"]))
    if node_ids is not None:
        query = query.where(FollowRequest.node_id.in_(node_ids))
    if group_ids is not None:
        query = query.where(FollowRequest.group_id.in_(group_ids))
    if user_id is not None:
        query = query.where((FollowRequest.follower_user_id == user_id) | (FollowRequest.leader_user_id == user_id))
    if leader_user_id is not None:
        query = query.where(FollowRequest.leader_user_id == leader_user_id)
    if follower_user_id is not None:
        query = query.where(FollowRequest.follower_user_id == follower_user_id)
    if primary_ids is not None:
        query = query.where(FollowRequest.leader_primary_node_id.in_(primary_ids))
    for row in (await session.scalars(query)).all():
        row.status, row.pending_key, row.reason = "cancelled", None, reason


async def serialize(session, rows):
    if not rows:
        return []
    active_groups = set((await session.scalars(select(NodeGroup.group_id)
        .join(NodeGroupMember, NodeGroupMember.group_id == NodeGroup.group_id)
        .join(Node, Node.node_id == NodeGroupMember.node_id)
        .outerjoin(TradingStrategy, TradingStrategy.strategy_id == NodeGroup.strategy_id)
        .where(NodeGroup.group_id.in_({r.group_id for r in rows}), NodeGroup.enabled.is_(True),
               Node.is_primary.is_(True), Node.enabled.is_(True),
               (Node.owner_user_id == NodeGroup.owner_user_id) | (Node.owner_user_id.is_(None) & NodeGroup.owner_user_id.is_(None)),
               NodeGroup.strategy_id.is_(None) | (TradingStrategy.enabled.is_(True) & TradingStrategy.template_id.in_(follow_rules.FOLLOW_MARKET_TEMPLATE_IDS))))).all())
    nodes = {n.node_id: n for n in (await session.scalars(select(Node).where(Node.node_id.in_({r.node_id for r in rows})))).all()}
    users = {u.id: u for u in (await session.scalars(select(User).where(User.id.in_({r.follower_user_id for r in rows})))).all()}
    relations = {(r.group_id, r.node_id): r for r in (await session.scalars(select(GroupFollowerNode).where(
        GroupFollowerNode.group_id.in_({r.group_id for r in rows}),
    ))).all()}
    result = []
    for row in rows:
        node, user = nodes.get(row.node_id), users.get(row.follower_user_id)
        relation = relations.get((row.group_id, row.node_id))
        available = bool(node and permissions.same_owner(node.owner_user_id, row.owner_user_id))
        result.append({**row.snapshot, "request_id": row.id, "group_id": row.group_id, "node_id": row.node_id,
            "leader_user_id": row.leader_user_id, "follower_user_id": row.follower_user_id,
            "applicant_name": (user.display_name or user.username) if user else "已删除用户",
            "status": row.status, "source": row.source, "reason": row.reason,
            "created_at": row.created_at.timestamp(), "reviewed_at": row.reviewed_at.timestamp() if row.reviewed_at else None,
            "available": available, "name": node.name if available else row.snapshot.get("name"),
            "marketable": bool(available and node.enabled and node.approval_status == "approved" and row.group_id in active_groups),
            "is_primary": bool(node.is_primary) if available else row.snapshot.get("is_primary", False),
            "leader_paused": bool(relation and relation.leader_paused),
            "follow_state": relation.follow_state if relation and relation.follower_user_id == row.follower_user_id else None})
    return result


async def list_mine(user_id):
    async with SessionLocal() as session:
        rows = (await session.scalars(select(FollowRequest).where(FollowRequest.follower_user_id == user_id)
                                      .order_by(FollowRequest.id.desc()))).all()
        latest = {}
        for row in rows:
            latest.setdefault((row.group_id, row.node_id), row)
        return await serialize(session, list(latest.values()))


async def migrate_legacy():
    async with SessionLocal() as session:
        if await session.get(SystemSetting, MIGRATION_KEY):
            return
        rows = (await session.execute(select(GroupFollowerNode, NodeGroup, Node, User, TradingStrategy)
            .join(NodeGroup, NodeGroup.group_id == GroupFollowerNode.group_id)
            .join(Node, Node.node_id == GroupFollowerNode.node_id)
            .join(User, User.id == GroupFollowerNode.leader_user_id)
            .outerjoin(TradingStrategy, TradingStrategy.strategy_id == NodeGroup.strategy_id))).all()
        count = 0
        for relation, group, node, user, strategy in rows:
            if strategy and not follow_rules.template_in_market(strategy.template_id, follow_rules.FOLLOW_MARKET_TEMPLATE_IDS):
                continue
            if await session.scalar(select(FollowRequest.id).where(
                FollowRequest.group_id == group.group_id, FollowRequest.node_id == node.node_id,
            )) is not None:
                continue
            session.add(FollowRequest(group_id=group.group_id, node_id=node.node_id,
                follower_user_id=relation.follower_user_id, leader_user_id=relation.leader_user_id,
                owner_user_id=node.owner_user_id, leader_primary_node_id=relation.leader_primary_node_id,
                strategy_id=group.strategy_id, snapshot=snapshot(node, group, strategy, user),
                status="approved", source="legacy", created_at=relation.joined_at, reviewed_at=relation.joined_at))
            count += 1
        session.add(SystemSetting(key=MIGRATION_KEY, value="done"))
        audit(session, None, "follow_legacy_approved", after={"request_count": count})
        await session.commit()
