"""远程平仓 API：单节点 / 全员广播（需管理员鉴权）。"""
import secrets
import time

from fastapi import APIRouter, Depends, HTTPException, Request

from . import persist
from .connections import manager
from .deps import client_ip, get_current_admin, get_store
from .models import CloseBatchRequest, CloseRequest, build_close_command
from .redis_store import RedisStore

router = APIRouter(prefix="/api", tags=["close"])


def _cmd_id() -> str:
    """平仓命令 ID（用于关联回报与审计）。"""
    return "cls_" + format(int(time.time() * 1000), "x") + secrets.token_hex(2)


@router.post("/nodes/{node_id}/close")
async def close_node(
    node_id: str,
    body: CloseRequest,
    request: Request,
    store: RedisStore = Depends(get_store),
    admin: str = Depends(get_current_admin),
):
    """对单个节点下发平仓（全平 / 按品种 / 按订单）。"""
    if not await store.get_node(node_id):
        raise HTTPException(status_code=404, detail="node not found")
    signal_id = _cmd_id()
    cmd = build_close_command(signal_id, body.target, body.symbol, body.ticket)
    await persist.record_manual_close(signal_id, node_id, body.target, body.symbol, body.ticket)
    sent = await manager.send_to_node(node_id, cmd)
    await persist.audit(admin, "close_node", node_id, body.model_dump(), "ok" if sent else "offline", client_ip(request))
    if not sent:
        raise HTTPException(status_code=409, detail="node offline")
    return {"status": "sent", "node_id": node_id, **body.model_dump()}


@router.post("/close-all")
async def close_all(
    body: CloseRequest,
    request: Request,
    store: RedisStore = Depends(get_store),
    admin: str = Depends(get_current_admin),
):
    """对所有在线节点广播平仓（危险操作，前端会二次确认）。"""
    signal_id = _cmd_id()
    cmd = build_close_command(signal_id, body.target, body.symbol, body.ticket)
    sent = []
    for nid in manager.online_node_ids():
        await persist.record_manual_close(signal_id, nid, body.target, body.symbol, body.ticket)
        if await manager.send_to_node(nid, cmd):
            sent.append(nid)
    await persist.audit(admin, "close_all", ",".join(sent), body.model_dump(), "ok", client_ip(request))
    return {"status": "sent", "nodes": sent, **body.model_dump()}


@router.post("/close-batch")
async def close_batch(
    body: CloseBatchRequest,
    request: Request,
    store: RedisStore = Depends(get_store),
    admin: str = Depends(get_current_admin),
):
    """对指定节点批量下发平仓（全平 / 按品种 / 按订单）。"""
    signal_id = _cmd_id()
    cmd = build_close_command(signal_id, body.target, body.symbol, body.ticket)
    sent: list[str] = []
    failed: list[dict] = []
    seen: set[str] = set()
    for node_id in body.node_ids:
        if node_id in seen:
            continue
        seen.add(node_id)
        if not await store.get_node(node_id):
            failed.append({"node_id": node_id, "reason": "not_found"})
            continue
        await persist.record_manual_close(signal_id, node_id, body.target, body.symbol, body.ticket)
        if await manager.send_to_node(node_id, cmd):
            sent.append(node_id)
        else:
            failed.append({"node_id": node_id, "reason": "offline"})
    await persist.audit(
        admin,
        "close_batch",
        ",".join(sent),
        {"node_ids": body.node_ids, **body.model_dump(exclude={"node_ids"})},
        "ok" if sent else "fail",
        client_ip(request),
    )
    if not sent and failed:
        raise HTTPException(status_code=409, detail="no nodes online")
    return {"status": "sent", "sent": sent, "failed": failed, **body.model_dump(exclude={"node_ids"})}
