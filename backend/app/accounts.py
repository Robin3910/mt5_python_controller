"""账户 / 持仓查询 API（需管理员鉴权）。"""
from fastapi import APIRouter, Depends, HTTPException

from .connections import manager
from .deps import get_current_admin, get_store
from .redis_store import RedisStore

router = APIRouter(prefix="/api", tags=["accounts"])


@router.get("/accounts")
async def all_accounts(store: RedisStore = Depends(get_store), _: str = Depends(get_current_admin)):
    """汇总所有节点的账户快照（含在线状态）。"""
    out = []
    for n in await store.all_nodes():
        acct = await store.get_account(n["node_id"]) or {}
        out.append(
            {
                "node_id": n["node_id"],
                "name": n.get("name"),
                "status": "online" if manager.is_node_online(n["node_id"]) else "offline",
                "account": acct,
            }
        )
    return out


@router.get("/nodes/{node_id}/account")
async def node_account(
    node_id: str, store: RedisStore = Depends(get_store), _: str = Depends(get_current_admin)
):
    """查询单个节点的账户快照；尚无上报时返回 404。"""
    acct = await store.get_account(node_id)
    if acct is None:
        raise HTTPException(status_code=404, detail="no account snapshot yet")
    return acct
