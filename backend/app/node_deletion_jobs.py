"""节点管理页的待删除请求，由已鉴权节点的新账户快照驱动。"""
import time
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import select

from . import close, mt5_identity, node_deletion, node_lifecycle, permissions, persist, rbac_service
from .ai_application_store import audit
from .connections import manager
from .db import SessionLocal
from .models import build_close_command
from .node_deletion_rules import has_readable_snapshot
from .orm import PendingNodeDeletion, SignalDispatch


RETRY_INTERVAL = 30
IN_FLIGHT = ("pending", "sent")


async def restore_pending_deletions() -> None:
    async with SessionLocal() as session:
        ids = await session.scalars(select(PendingNodeDeletion.node_id))
        node_lifecycle.pending_deletions.clear()
        node_lifecycle.pending_deletions.update(ids)


async def enqueue(session, principal, preview, ip):
    node = preview["nodes"][0]
    node_id = node["node_id"]
    existing = await session.get(PendingNodeDeletion, node_id)
    if existing is None:
        session.add(PendingNodeDeletion(
            node_id=node_id, mt5_login=node["mt5_login"], owner_user_id=node["owner_user_id"],
            actor_user_id=principal.user_id, operator=principal.username, ip=ip,
        ))
    audit(session, principal, "request_node_deletion", node_id, before=preview,
          after={"status": "pending"}, ip=ip, category="node", result="pending")
    await session.commit()
    node_lifecycle.pending_deletions.add(node_id)
    await node_deletion.notify_deletion(node_id, "pending")
    return {"status": "pending", "node_ids": [node_id]}


async def _authorized_actor(session, store, job):
    actor = await rbac_service.load_principal(None, job.operator)
    if not actor or actor.user_id != job.actor_user_id:
        return None
    try:
        nodes, _ = await node_deletion._targets(session, store, actor, node_id=job.node_id)
    except HTTPException:
        return None
    node = nodes[0]
    if node.mt5_login == job.mt5_login and permissions.same_owner(node.owner_user_id, job.owner_user_id):
        return actor
    return None


async def _finish_unreported(session, dispatches, error):
    for dispatch in dispatches:
        dispatch.status = "failed"
        dispatch.finished_at = datetime.now()
        dispatch.error = error
    await session.flush()
    for signal_id in {dispatch.signal_id for dispatch in dispatches}:
        await persist._refresh_signal_status(session, signal_id)


async def advance_deletion(store, group_dispatcher, snapshot) -> bool:
    """在节点生命周期锁内处理一帧快照，下发后由后续快照继续核验。"""
    node_id = snapshot["node_id"]
    async with SessionLocal() as session:
        job = await session.get(PendingNodeDeletion, node_id)
        if job is None:
            node_lifecycle.pending_deletions.discard(node_id)
            return False
        actor = await _authorized_actor(session, store, job)
        if actor is None:
            requester = permissions.Principal(job.actor_user_id, job.operator, False, frozenset())
            audit(session, requester, "request_node_deletion", node_id, result="cancelled", ip=job.ip,
                  category="node", after={"reason": "节点归属、绑定账号或删除权限已变更"})
            await session.delete(job)
            await session.commit()
            node_lifecycle.pending_deletions.discard(node_id)
            await node_deletion.notify_deletion(node_id, "cancelled")
            return False

        now = time.time()
        if (mt5_identity.parse_login(snapshot.get("login")) != job.mt5_login or snapshot.get("mt5_link") == "offline"
                or not has_readable_snapshot(snapshot, now)):
            return False

        receipt = None
        if job.close_signal_id:
            receipt = await session.scalar(select(SignalDispatch).where(
                SignalDispatch.node_id == node_id, SignalDispatch.signal_id == job.close_signal_id,
            ))
        settled = bool(receipt and receipt.status == "done" and receipt.finished_at
                       and snapshot["updated_at"] > receipt.finished_at.timestamp())
        if not job.close_signal_id or settled:
            current = await node_deletion._preview(session, store, actor, node_id=node_id, settled=settled)
            if not current["blocked"]:
                if settled:
                    # 全平回报发生在该连接此前的开仓指令之后，缺失的回报仍按未确认记录。
                    dispatches = (await session.scalars(select(SignalDispatch).where(
                        SignalDispatch.node_id == node_id, SignalDispatch.status.in_(IN_FLIGHT),
                    ))).all()
                    await _finish_unreported(session, dispatches, "账户已核验清空，原指令未收到回报")
                await node_deletion._delete_current(session, store, actor, current, ip=job.ip, node_id=node_id)
                return True

        if now - job.last_attempt_at < RETRY_INTERVAL:
            return False
        if receipt and receipt.status in IN_FLIGHT:
            await _finish_unreported(session, [receipt], "节点未回报全平结果，等待重试")
        signal_id = close._cmd_id()
        job.close_signal_id = signal_id
        job.last_attempt_at = now
        persist.add_manual_close(session, signal_id, node_id, "all")
        await session.commit()

        strategies = await close._stop_strategies_on_flatten(group_dispatcher, node_id, signal_id, "all")
        sent = await manager.send_to_node(node_id, build_close_command(signal_id, "all"))
        if not sent:
            await persist.update_dispatch_result(signal_id, node_id, "failed", {"error": "节点离线，等待重试"})
        audit(session, actor, "close_for_node_deletion", node_id, ip=job.ip, category="node",
              result="sent" if sent else "offline", after={"signal_id": signal_id, "strategies": strategies})
        await session.commit()
        return False
