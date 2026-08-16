"""远程平仓 API：单节点 / 全员广播（需管理员鉴权）。"""
import secrets
import time

from fastapi import APIRouter, Depends, HTTPException, Request

from . import persist
from .connections import manager
from .deps import client_ip, get_current_admin, get_group_dispatcher, get_store
from .group_dispatcher import GroupDispatcher
from .models import CloseBatchRequest, CloseRequest, build_close_command
from .redis_store import RedisStore

router = APIRouter(prefix="/api", tags=["close"])

# 总览/节点全平附带终止策略任务时写入 strategy_stop.reason，便于开单原因展示
_CLOSE_ALL_STOP_REASON = "manual_close_all"


def _cmd_id() -> str:
    """平仓命令 ID（用于关联回报与审计）。"""
    return "cls_" + format(int(time.time() * 1000), "x") + secrets.token_hex(2)


async def _stop_strategies_on_flatten(
    group_dispatcher: GroupDispatcher, node_id: str, signal_id: str, target: str,
) -> dict:
    """账户级全平才停策略监控；节点必须在线，避免离线强制收口却没平掉仓。"""
    empty = {"stopped": 0, "forced": 0, "total": 0}
    if target != "all" or node_id not in manager.nodes:
        return empty
    return await group_dispatcher.stop_all_on_node(
        node_id, signal_id=signal_id, reason=_CLOSE_ALL_STOP_REASON,
    )


@router.post("/nodes/{node_id}/close")
async def close_node(
    node_id: str,
    body: CloseRequest,
    request: Request,
    store: RedisStore = Depends(get_store),
    group_dispatcher: GroupDispatcher = Depends(get_group_dispatcher),
    admin: str = Depends(get_current_admin),
):
    """对单个节点下发平仓（全平 / 按品种 / 按订单）。

    全平会先终止该节点上所有未收口的策略子任务，再下发 close，避免网格/限价监控把仓补回来。
    """
    if not await store.get_node(node_id):
        raise HTTPException(status_code=404, detail="node not found")
    signal_id = _cmd_id()
    strategies = await _stop_strategies_on_flatten(
        group_dispatcher, node_id, signal_id, body.target,
    )
    cmd = build_close_command(signal_id, body.target, body.symbol, body.ticket)
    await persist.record_manual_close(signal_id, node_id, body.target, body.symbol, body.ticket)
    sent = await manager.send_to_node(node_id, cmd)
    after = {**body.model_dump(), "strategies": strategies}
    await persist.audit(
        admin, "close_node", node_id, body.model_dump(), "ok" if sent else "offline",
        client_ip(request),
        category="node", before=None, after=after,
    )
    if not sent:
        raise HTTPException(status_code=409, detail="node offline")
    return {"status": "sent", "node_id": node_id, "strategies": strategies, **body.model_dump()}


@router.post("/close-all")
async def close_all(
    body: CloseRequest,
    request: Request,
    store: RedisStore = Depends(get_store),
    group_dispatcher: GroupDispatcher = Depends(get_group_dispatcher),
    admin: str = Depends(get_current_admin),
):
    """对所有在线节点广播平仓（危险操作，前端会二次确认）。"""
    signal_id = _cmd_id()
    cmd = build_close_command(signal_id, body.target, body.symbol, body.ticket)
    sent = []
    strategies_total = 0
    for nid in manager.online_node_ids():
        strategies = await _stop_strategies_on_flatten(
            group_dispatcher, nid, signal_id, body.target,
        )
        strategies_total += int(strategies.get("total") or 0)
        await persist.record_manual_close(signal_id, nid, body.target, body.symbol, body.ticket)
        if await manager.send_to_node(nid, cmd):
            sent.append(nid)
    await persist.audit(
        admin, "close_all", ",".join(sent), body.model_dump(), "ok", client_ip(request),
        category="node", before=None, after={
            "sent": sent, "strategies_total": strategies_total, **body.model_dump(),
        },
    )
    return {
        "status": "sent", "nodes": sent, "strategies_total": strategies_total,
        **body.model_dump(),
    }


@router.post("/close-batch")
async def close_batch(
    body: CloseBatchRequest,
    request: Request,
    store: RedisStore = Depends(get_store),
    group_dispatcher: GroupDispatcher = Depends(get_group_dispatcher),
    admin: str = Depends(get_current_admin),
):
    """对指定节点批量下发平仓（全平 / 按品种 / 按订单）。"""
    signal_id = _cmd_id()
    cmd = build_close_command(signal_id, body.target, body.symbol, body.ticket)
    sent: list[str] = []
    failed: list[dict] = []
    seen: set[str] = set()
    strategies_total = 0
    for node_id in body.node_ids:
        if node_id in seen:
            continue
        seen.add(node_id)
        if not await store.get_node(node_id):
            failed.append({"node_id": node_id, "reason": "not_found"})
            continue
        strategies = await _stop_strategies_on_flatten(
            group_dispatcher, node_id, signal_id, body.target,
        )
        strategies_total += int(strategies.get("total") or 0)
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
        category="node",
        before=None,
        after={
            "sent": sent, "failed": failed, "strategies_total": strategies_total,
            **body.model_dump(),
        },
    )
    if not sent and failed:
        raise HTTPException(status_code=409, detail="no nodes online")
    return {
        "status": "sent", "sent": sent, "failed": failed,
        "strategies_total": strategies_total,
        **body.model_dump(exclude={"node_ids"}),
    }
