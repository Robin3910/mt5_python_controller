"""节点管理 API（需管理员鉴权）。"""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import IntegrityError

from . import node_service, persist
from .connections import manager
from .deps import client_ip, get_current_admin, get_store
from .models import LotBatch, NodeCreate, NodeOut, NodeUpdate, PaginatedNodeDispatches
from .redis_store import RedisStore

router = APIRouter(prefix="/api/nodes", tags=["nodes"])


def _node_matches_search(node: dict, term: str) -> bool:
    """按节点名称（忽略大小写）或 MT5 账号（子串）模糊匹配。"""
    q = term.lower()
    if q in (node.get("name") or "").lower():
        return True
    mt5 = node.get("mt5_login")
    return mt5 is not None and term in str(mt5)


async def _to_node_out(store: RedisStore, d: dict) -> NodeOut:
    """把缓存里的节点 dict 组装成对外的 NodeOut（合并在线状态与账户登录信息）。"""
    acct = await store.get_account(d["node_id"]) or {}
    return NodeOut(
        node_id=d["node_id"],
        name=d["name"],
        enabled=d.get("enabled", True),
        # 在线状态以“当前进程内是否有活动连接”为准（单实例下最实时）
        status="online" if manager.is_node_online(d["node_id"]) else "offline",
        filters=d.get("filters"),
        mt5_login=d.get("mt5_login"),
        mt5_server=d.get("mt5_server") or acct.get("server"),
        created_at=d.get("created_at", 0),
        last_seen=acct.get("updated_at"),
    )


@router.get("", response_model=list[NodeOut])
async def list_nodes(
    q: str | None = None,
    store: RedisStore = Depends(get_store),
    _: str = Depends(get_current_admin),
):
    """节点列表（按创建时间排序）；可选 q 按名称 / MT5 账号模糊搜索。"""
    nodes = await store.all_nodes()
    if q and (term := q.strip()):
        nodes = [n for n in nodes if _node_matches_search(n, term)]
    nodes.sort(key=lambda n: n.get("created_at", 0))
    return [await _to_node_out(store, n) for n in nodes]


@router.post("", response_model=NodeOut, status_code=201)
async def create_node(
    body: NodeCreate,
    request: Request,
    store: RedisStore = Depends(get_store),
    admin: str = Depends(get_current_admin),
):
    """创建节点（管理员手动）。鉴权令牌为全局共享，见账户设置 → 节点令牌。

    mt5_login 全局唯一；若已存在同 MT5 登录号的节点则返回 409。
    """
    try:
        d = await node_service.create_node(store, body)
    except IntegrityError:
        raise HTTPException(
            status_code=409,
            detail=f"node with mt5_login={body.mt5_login} already exists",
        )
    await persist.audit(
        admin, "create_node", d["node_id"],
        {"name": d["name"], "mt5_login": d["mt5_login"]}, "ok", client_ip(request),
    )
    return await _to_node_out(store, d)


@router.get("/{node_id}", response_model=NodeOut)
async def get_node(node_id: str, store: RedisStore = Depends(get_store), _: str = Depends(get_current_admin)):
    d = await store.get_node(node_id)
    if not d:
        raise HTTPException(status_code=404, detail="node not found")
    return await _to_node_out(store, d)


@router.get("/{node_id}/dispatches", response_model=PaginatedNodeDispatches)
async def node_dispatches(
    node_id: str,
    page: int = 1,
    page_size: int = 20,
    store: RedisStore = Depends(get_store),
    _: str = Depends(get_current_admin),
):
    """某节点分发/成交明细分页（持久化历史，供详情页「信号」「成交回报」Tab）。"""
    if not await store.get_node(node_id):
        raise HTTPException(status_code=404, detail="node not found")
    return await persist.recent_dispatches(node_id, page, page_size)


@router.patch("/{node_id}", response_model=NodeOut)
async def update_node(
    node_id: str,
    body: NodeUpdate,
    request: Request,
    store: RedisStore = Depends(get_store),
    admin: str = Depends(get_current_admin),
):
    """更新节点配置（手数策略、跟随开关、轮询顺序、启用状态等）。"""
    d = await node_service.update_node(store, node_id, body)
    if not d:
        raise HTTPException(status_code=404, detail="node not found")
    await persist.audit(admin, "update_node", node_id, body.model_dump(exclude_none=True), "ok", client_ip(request))
    return await _to_node_out(store, d)


@router.delete("/{node_id}")
async def delete_node(
    node_id: str,
    request: Request,
    store: RedisStore = Depends(get_store),
    admin: str = Depends(get_current_admin),
):
    ok = await node_service.delete_node(store, node_id)
    if not ok:
        raise HTTPException(status_code=404, detail="node not found")
    await persist.audit(admin, "delete_node", node_id, None, "ok", client_ip(request))
    return {"status": "deleted", "node_id": node_id}


@router.post("/lot")
async def batch_lot(
    body: LotBatch,
    request: Request,
    store: RedisStore = Depends(get_store),
    admin: str = Depends(get_current_admin),
):
    """批量设置多个节点的手数策略。"""
    updated = []
    for nid in body.node_ids:
        d = await node_service.update_node(
            store, nid, NodeUpdate(lot_mode=body.lot_mode, lot=body.lot)
        )
        if d:
            updated.append(nid)
    await persist.audit(admin, "batch_lot", ",".join(updated), body.model_dump(), "ok", client_ip(request))
    return {"status": "ok", "updated": updated}
