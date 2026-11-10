"""带影响预览的真实节点删除；校验、业务删除与审计保持同一作用域。"""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import delete, func, select

from . import ai_application_store as applications, follow_service, group_rules, node_lifecycle, permissions
from .connections import manager
from .db import SessionLocal
from .node_deletion_rules import deletion_blockers
from .orm import (
    AiFollowApplication, AiFollowApplicationNode, DeletedNode, GroupFollowerNode,
    GroupTaskDispatch, Node, NodeCredential, NodeGroupMember, SignalDispatch,
)


async def _targets(session, store, principal, *, node_id=None, group_id=None, follower_user_id=None):
    if node_id is not None:
        node = await session.get(Node, node_id)
        if node is None and principal.is_admin:
            cached = await store.get_node(node_id)
            if cached and cached.get("mt5_login"):
                node = Node(node_id=node_id, name=cached.get("name") or node_id, mt5_login=cached["mt5_login"],
                            owner_user_id=cached.get("owner_user_id"), enabled=cached.get("enabled", False),
                            is_primary=cached.get("is_primary", False))
        if not node or not permissions.owns(principal, node.owner_user_id):
            raise HTTPException(404, "节点不存在或不属于当前账号")
        return [node], []
    from .ai_channel_service import owned_group
    await owned_group(session, principal, group_id)
    query = select(AiFollowApplicationNode, AiFollowApplication).join(
        AiFollowApplication, AiFollowApplication.id == AiFollowApplicationNode.application_id,
    ).where(AiFollowApplication.group_id == group_id,
            AiFollowApplication.leader_user_id == principal.user_id,
            AiFollowApplication.status.in_(["pending", "approved", "rejected"]),
            AiFollowApplicationNode.status.in_(["pending", "approved", "rejected"]))
    if follower_user_id is not None:
        query = query.where(AiFollowApplication.follower_user_id == follower_user_id)
    entries = (await session.execute(query)).all()
    ids = {item.node_id for item, _ in entries}
    nodes = (await session.scalars(select(Node).where(Node.node_id.in_(ids)).order_by(Node.node_id))).all()
    expected = {item.node_id: item.owner_user_id for item, _ in entries}
    if not nodes or {node.node_id for node in nodes} != ids:
        raise HTTPException(409, "申请节点已失效，请刷新频道")
    if any(not permissions.same_owner(node.owner_user_id, expected[node.node_id]) for node in nodes):
        raise HTTPException(409, "节点归属已变更，禁止删除历史申请人的节点")
    from .node_credentials import is_admin_user
    for item, application in entries:
        if item.owner_user_id is None and not await is_admin_user(session, application.follower_user_id):
            raise HTTPException(409, "申请人已不再拥有管理员节点")
    return nodes, entries


async def _preview(session, store, principal, **scope):
    nodes, entries = await _targets(session, store, principal, **scope)
    ids = [node.node_id for node in nodes]
    members = (await session.scalars(select(NodeGroupMember).where(NodeGroupMember.node_id.in_(ids)))).all()
    followers = (await session.scalars(select(GroupFollowerNode).where(GroupFollowerNode.node_id.in_(ids)))).all()
    references = (await session.execute(select(AiFollowApplicationNode, AiFollowApplication).join(
        AiFollowApplication, AiFollowApplication.id == AiFollowApplicationNode.application_id,
    ).where(AiFollowApplicationNode.node_id.in_(ids), AiFollowApplicationNode.status != "cancelled",
            AiFollowApplication.status.in_(["pending", "approved", "rejected"])))).all()
    source_pending = (await session.scalars(select(AiFollowApplication).where(
        AiFollowApplication.leader_primary_node_id.in_(ids), AiFollowApplication.status == "pending",
    ))).all()
    signature = {
        "scope": scope,
        "nodes": [(n.node_id, n.name, n.mt5_login, n.owner_user_id, n.is_primary, n.enabled, str(n.updated_at)) for n in nodes],
        "members": sorted((r.group_id, r.node_id) for r in members),
        "followers": sorted((r.group_id, r.node_id, r.follow_state, r.leader_paused) for r in followers),
        "applications": sorted((a.id, a.status, item.node_id, item.status) for item, a in references),
        "source_applications": sorted((a.id, a.group_id, a.status) for a in source_pending),
    }
    blocked = []
    for node in nodes:
        account = await store.get_account(node.node_id)
        active = await session.scalar(select(GroupTaskDispatch.id).where(
            GroupTaskDispatch.node_id == node.node_id,
            GroupTaskDispatch.status.notin_(group_rules.SUBTASK_TERMINAL),
        ).limit(1))
        pending = await session.scalar(select(SignalDispatch.id).where(
            SignalDispatch.node_id == node.node_id, SignalDispatch.status.in_(["pending", "sent"]),
        ).limit(1))
        recent = []
        for model in (SignalDispatch, GroupTaskDispatch):
            values = (await session.execute(select(func.max(model.sent_at), func.max(model.finished_at))
                                           .where(model.node_id == node.node_id))).one()
            recent.extend(value.timestamp() for value in values if value is not None)
        occupied = False
        for pattern in (f"lock:exec:{node.node_id}:*", f"group:node:busy:*:{node.node_id}",
                        f"cancel_pending:lock:{node.node_id}", f"cancel_pending:busy:{node.node_id}",
                        f"limit_watch:lock:{node.node_id}:*"):
            if any([key async for key in store.r.scan_iter(match=pattern, count=100)]):
                occupied = True
                break
        reasons = deletion_blockers(account, online=manager.is_node_online(node.node_id), now=time.time(),
                                    active=active is not None, pending=pending is not None, occupied=occupied,
                                    last_trade_at=max(recent) if recent else None)
        if reasons:
            blocked.append({"node_id": node.node_id, "name": node.name, "reasons": reasons})
    groups = {r.group_id for r in members + followers}
    groups.update(application.group_id for _, application in references)
    groups.update(application.group_id for application in source_pending)
    bindings = {(r.group_id, r.node_id) for r in members + followers}
    bindings.update((application.group_id, item.node_id) for item, application in references)
    return {
        "node_ids": ids, "nodes": [applications.node_snapshot(node) for node in nodes],
        "node_count": len(ids), "member_count": len({a.follower_user_id for _, a in entries}) if entries else 1,
        "binding_count": len(bindings),
        "cancelled_application_count": len({a.id for _, a in references if a.status == "pending"} | {a.id for a in source_pending}),
        "other_group_count": len(groups - {scope.get("group_id")}), "blocked": blocked,
        "version": hashlib.sha256(json.dumps(signature, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
    }


async def preview(store, principal, *, record=False, ip=None, **scope):
    async with SessionLocal() as session:
        result = await _preview(session, store, principal, **scope)
        if record:
            applications.audit(session, principal, "preview_ai_channel_deletion" if scope.get("group_id") else "preview_node_deletion",
                scope.get("group_id") or scope.get("node_id"), after=result, ip=ip,
                result="failed" if result["blocked"] else "ok", category="ai_follow" if scope.get("group_id") else "node")
            await session.commit()
        return result


async def execute(store, principal, version, expected_ids, *, ip=None, **scope):
    initial = await preview(store, principal, **scope)
    keys = [node_lifecycle.node_key(nid) for nid in initial["node_ids"]]
    if scope.get("group_id"):
        keys.append(node_lifecycle.group_key(scope["group_id"]))
    async with node_lifecycle.locked(*keys), SessionLocal() as session:
        current = await _preview(session, store, principal, **scope)
        if current["version"] != version or set(current["node_ids"]) != set(expected_ids):
            raise HTTPException(409, "删除范围已变化，请重新预览并确认")
        if current["blocked"]:
            raise HTTPException(409, {"message": "存在交易或状态不明的节点，整次删除已阻止", "blocked": current["blocked"]})
        ids = current["node_ids"]
        await session.execute(select(Node).where(Node.node_id.in_(ids)).with_for_update())
        groups = set((await session.scalars(select(NodeGroupMember.group_id).where(NodeGroupMember.node_id.in_(ids)))).all())
        groups.update((await session.scalars(select(GroupFollowerNode.group_id).where(GroupFollowerNode.node_id.in_(ids)))).all())
        for node in current["nodes"]:
            tombstone = await session.get(DeletedNode, node["mt5_login"])
            if tombstone is None:
                session.add(DeletedNode(mt5_login=node["mt5_login"], node_id=node["node_id"], deleted_by_user_id=principal.user_id))
            else:
                tombstone.node_id = node["node_id"]
                tombstone.deleted_by_user_id = principal.user_id
                tombstone.deleted_at = datetime.now()
        await applications.cancel_nodes(session, ids, "系统节点已删除")
        source_groups = (await session.scalars(select(AiFollowApplication.group_id).where(
            AiFollowApplication.leader_primary_node_id.in_(ids), AiFollowApplication.status == "pending",
        ))).all()
        await applications.cancel_applications(session, "机器人主节点已删除", group_ids=source_groups, pending_only=True)
        for model in (NodeCredential, NodeGroupMember, GroupFollowerNode, Node):
            await session.execute(delete(model).where(model.node_id.in_(ids)))
        applications.audit(session, principal, "delete_ai_channel_nodes" if scope.get("group_id") else "delete_node", scope.get("group_id") or scope.get("node_id"),
            before=current, after={"deleted_node_ids": ids}, ip=ip, category="ai_follow" if scope.get("group_id") else "node")
        await session.commit()
        for nid in ids:
            ws = manager.nodes.pop(nid, None)
            if ws:
                try:
                    await ws.close(code=4401)
                except (RuntimeError, ConnectionError):
                    pass
        for nid in ids:
            await store.delete_node(nid)
        await follow_service._refresh_groups(store, groups)
    await manager.broadcast_admin({"type": "nodes_changed", "data": {"deleted_node_ids": ids}})
    return {"status": "deleted", "node_ids": ids}


async def failed(principal, action, target, error, ip=None):
    async with SessionLocal() as session:
        applications.audit(session, principal, action, target, result="failed", ip=ip,
                           category="node" if action in ("delete_node", "preview_node_deletion") else "ai_follow",
                           after={"detail": error.detail if isinstance(error, HTTPException) else "操作失败"})
        await session.commit()
