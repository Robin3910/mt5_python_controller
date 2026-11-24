"""普通带单分组继承已生效的跟单节点。"""
from collections import defaultdict
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import select

from . import follow_requests, follow_rules, follow_service, node_lifecycle, permissions, rbac_service
from . import strategy_templates
from .db import SessionLocal
from .orm import FollowRequest, GroupFollowerNode, Node, NodeGroup, NodeGroupMember, TradingStrategy, User


def _skip_reason(history, node, group, strategy, primary):
    for row in history:
        if row.status == "pending":
            if row.source != "inherit":
                return "主动申请仍需人工审核"
            if (row.follower_user_id != node.owner_user_id or row.leader_user_id != group.owner_user_id
                    or row.owner_user_id != node.owner_user_id or row.strategy_id != group.strategy_id
                    or row.leader_primary_node_id != primary.node_id or row.snapshot.get("symbol") != strategy.symbol):
                return "待审申请的带单源或策略已变更"
        if row.status == "rejected" or (
            row.status == "cancelled" and row.reason in ("带单员已移出该节点", "跟单已移出")
        ):
            return "目标组曾拒绝或移出该节点，请主动重新申请"
    return None


async def approve_inherited(store, target, source_ids, node_ids, actor, ip, *, limit_watch_node_id: str | None = None):
    if not node_ids:
        return [], []
    gid, leader_id = target["group_id"], target["owner_user_id"]
    keys = [node_lifecycle.group_key(g) for g in [gid, *source_ids]]
    keys.extend(node_lifecycle.node_key(n) for n in node_ids)
    adopted, skipped = [], []
    async with node_lifecycle.locked(*keys), SessionLocal() as session:
        # 两个分组可能互为来源，跨进程也按同一顺序锁组，再读取审批快照。
        await session.execute(select(NodeGroup.group_id).where(NodeGroup.group_id.in_([gid, *source_ids]))
                              .order_by(NodeGroup.group_id).with_for_update())
        try:
            group, strategy, primary, leader = await follow_requests.validate_group(session, store, gid, leader_id)
        except HTTPException as exc:
            if limit_watch_node_id is None or exc.status_code not in (400, 404, 409):
                raise
            return [], []
        if not strategy:
            return [], []
        if limit_watch_node_id is not None:
            if primary.node_id != limit_watch_node_id or not group.limit_watch_enabled:
                return [], []
            await session.refresh(strategy, with_for_update=True)
            rule = strategy_templates.pick_risk_sized_rule(strategy.config_json)
            if (strategy.template_id != strategy_templates.TEMPLATE_2_ID or not strategy.enabled
                    or not strategy_templates.is_limit_entry(rule)):
                return [], []
            reporting = await session.scalar(select(NodeGroupMember.id).where(
                NodeGroupMember.group_id == gid, NodeGroupMember.node_id == limit_watch_node_id,
                NodeGroupMember.limit_watch_report.is_(True),
            ).with_for_update())
            if reporting is None:
                return [], []
        sources = (await session.scalars(select(GroupFollowerNode)
            .join(NodeGroup, NodeGroup.group_id == GroupFollowerNode.group_id)
            .join(TradingStrategy, TradingStrategy.strategy_id == NodeGroup.strategy_id)
            .join(NodeGroupMember, NodeGroupMember.group_id == NodeGroup.group_id)
            .join(Node, Node.node_id == GroupFollowerNode.node_id)
            .where(NodeGroup.group_id.in_(source_ids), NodeGroup.owner_user_id == group.owner_user_id,
                   TradingStrategy.template_id.in_(follow_rules.FOLLOW_MARKET_TEMPLATE_IDS),
                   NodeGroupMember.node_id == primary.node_id, GroupFollowerNode.leader_user_id == leader_id,
                   Node.node_id.in_(node_ids), Node.owner_user_id == GroupFollowerNode.follower_user_id,
                   GroupFollowerNode.follow_state.in_(("running", "paused")))
            .order_by(GroupFollowerNode.id).with_for_update())).all()
        by_node = defaultdict(list)
        for source in sources:
            by_node[source.node_id].append(source)

        existing_ids = set(await session.scalars(select(GroupFollowerNode.node_id).where(
            GroupFollowerNode.group_id == gid, GroupFollowerNode.node_id.in_(by_node),
        ).with_for_update()))
        principals = {}
        for nid, relations in by_node.items():
            if nid in existing_ids:
                skipped.append({"node_id": nid, "reason": "目标分组已有跟单记录"})
                continue
            uid = relations[0].follower_user_id
            if uid not in principals:
                user = await session.get(User, uid)
                principals[uid] = await rbac_service.load_principal(store, user.username) if user and user.is_active else None
            principal = principals[uid]
            if not principal or not permissions.can_menu(principal, permissions.MENU_FOLLOW):
                skipped.append({"node_id": nid, "reason": "跟单用户已失效"})
                continue
            try:
                node = await follow_requests.validate_node(session, principal, nid, gid)
            except HTTPException as exc:
                skipped.append({"node_id": nid, "reason": exc.detail})
                continue
            history = (await session.scalars(select(FollowRequest).where(
                FollowRequest.group_id == gid, FollowRequest.node_id == nid,
                (FollowRequest.status == "pending") | (
                    (FollowRequest.follower_user_id == uid) & (FollowRequest.leader_user_id == leader_id)),
            ).order_by(FollowRequest.id.desc()).with_for_update())).all()
            reason = _skip_reason(history, node, group, strategy, primary)
            if reason:
                skipped.append({"node_id": nid, "reason": reason})
                continue
            follow_state = "paused" if any(r.follow_state == "paused" for r in relations) else "running"
            leader_paused = any(r.leader_paused for r in relations)
            if not await follow_service.write_following(session, group_id=gid, node_id=nid,
                    follower_user_id=uid, leader_user_id=leader_id, leader_primary_node_id=primary.node_id,
                    follow_state=follow_state, leader_paused=leader_paused, restart_stopped=False):
                skipped.append({"node_id": nid, "reason": "目标分组已有跟单记录"})
                continue
            row = next((r for r in history if r.status == "pending"), None)
            if row is None:
                row = FollowRequest(group_id=gid, node_id=nid, follower_user_id=uid, leader_user_id=leader_id,
                                    owner_user_id=node.owner_user_id, leader_primary_node_id=primary.node_id,
                                    strategy_id=group.strategy_id, source="inherit")
                session.add(row)
            row.snapshot = {**follow_requests.snapshot(node, group, strategy, leader), "follow_state": follow_state}
            row.status, row.pending_key, row.reviewed_at = "approved", None, datetime.now()
            await session.flush()
            result = {"group_id": gid, "node_id": nid, "follower_user_id": uid,
                      "from_group_id": relations[0].group_id, "request_id": row.id, "status": row.status}
            follow_requests.audit(session, actor, "follow_request_auto_approve", row.id, after=result, ip=ip,
                params={"source_group_ids": [r.group_id for r in relations],
                        "follow_state": follow_state, "leader_paused": leader_paused})
            adopted.append(result)
        await session.commit()
        # 上次可能已提交但缓存刷新失败，重复同步也要从权威数据恢复。
        await follow_service._refresh_groups(store, {gid})
    return adopted, skipped
