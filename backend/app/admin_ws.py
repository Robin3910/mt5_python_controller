"""后台实时 WebSocket：推送节点上下线、账户快照、分发与成交事件。

鉴权方式：连接 URL 携带 ?token=<JWT>（后台前端在登录后建立连接）。
"""
import logging

from fastapi import APIRouter, Query, WebSocket
from starlette.websockets import WebSocketDisconnect

from .connections import manager
from .security import verify_jwt
from .state import state

logger = logging.getLogger(__name__)
router = APIRouter()


@router.websocket("/ws/admin")
async def admin_ws(ws: WebSocket, token: str = Query(default="")):
    # 校验 JWT；非法直接拒绝（不 accept，前端会收到握手失败）
    if not verify_jwt(token):
        await ws.close(code=4401)
        return
    await ws.accept()
    manager.add_admin(ws)
    store = state.store
    try:
        # 连接建立后先推一份全量快照，前端即可立即渲染
        nodes = await store.all_nodes() if store else []
        snapshot = []
        for n in nodes:
            acct = await store.get_account(n["node_id"]) or {}
            snapshot.append(
                {
                    "node_id": n["node_id"],
                    "name": n.get("name"),
                    "status": "online" if manager.is_node_online(n["node_id"]) else "offline",
                    "account": acct,
                }
            )
        await ws.send_json(
            {
                "type": "snapshot",
                "data": {
                    "nodes": snapshot,
                    "lot": await store.get_lot_global() if store else {},
                },
            }
        )
        # 之后保持连接：仅处理前端的 ping 保活，其余实时事件由 broadcast_admin 主动推送
        while True:
            msg = await ws.receive_json()
            if isinstance(msg, dict) and msg.get("type") == "ping":
                await ws.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        logger.debug("admin ws closed")
    finally:
        manager.remove_admin(ws)
