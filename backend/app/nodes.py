"""节点管理 API。

普通用户只能看到 / 修改管理员分配给自己的节点（改名、启停、账户风控）；
节点入库、删除、批量手数与按币种配置（normal 链路）只有管理员能做。
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from . import node_service, permissions, persist, rbac_service, risk_control
from .connections import manager
from .deps import (
    client_ip,
    get_principal,
    get_store,
    owned_node_or_404,
    require_admin,
    require_menu,
)
from .models import LotBatch, NodeCreate, NodeOut, NodeUpdate, PaginatedNodeDispatches
from .db import SessionLocal
from .orm import NodeCredential
from .permissions import MENU_NODES, Principal
from .redis_store import RedisStore

router = APIRouter(prefix="/api/nodes", tags=["nodes"])


def _node_audit_snapshot(d: dict | None) -> dict | None:
    """节点审计快照：只保留配置相关字段。"""
    if not d:
        return None
    return {
        "node_id": d.get("node_id"),
        "name": d.get("name"),
        "enabled": d.get("enabled", True),
        "filters": d.get("filters"),
        "risk": d.get("risk"),
        "mt5_login": d.get("mt5_login"),
        "mt5_server": d.get("mt5_server"),
        "owner_user_id": d.get("owner_user_id"),
        "approval_status": d.get("approval_status", "approved"),
        "requested_by_user_id": d.get("requested_by_user_id"),
        "admin_enable_requested": d.get("admin_enable_requested", False),
    }


def _node_matches_search(node: dict, term: str) -> bool:
    """按节点名称（忽略大小写）或 MT5 账号（子串）模糊匹配。"""
    q = term.lower()
    if q in (node.get("name") or "").lower():
        return True
    mt5 = node.get("mt5_login")
    return mt5 is not None and term in str(mt5)


async def _to_node_out(
    store: RedisStore, d: dict, owner_names: dict[int, str] | None = None,
) -> NodeOut:
    """把缓存里的节点 dict 组装成对外的 NodeOut（合并在线状态与账户登录信息）。"""
    acct = await store.get_account(d["node_id"]) or {}
    owner = permissions.normalize_owner(d.get("owner_user_id"))
    async with SessionLocal() as session:
        credential = await session.get(NodeCredential, d["node_id"])
    return NodeOut(
        node_id=d["node_id"],
        name=d["name"],
        enabled=d.get("enabled", True),
        # 在线状态以“当前进程内是否有活动连接”为准（单实例下最实时）
        status="online" if manager.is_node_online(d["node_id"]) else "offline",
        filters=d.get("filters"),
        risk=risk_control.normalize_risk(d.get("risk")),
        mt5_login=d.get("mt5_login"),
        mt5_server=d.get("mt5_server") or acct.get("server"),
        client_version=d.get("client_version"),
        client_version_at=d.get("client_version_at"),
        created_at=d.get("created_at", 0),
        last_seen=acct.get("updated_at"),
        owner_user_id=owner,
        owner_username=(owner_names or {}).get(owner) if owner is not None else None,
        approval_status=d.get("approval_status", "approved"),
        requested_by_user_id=d.get("requested_by_user_id"),
        requested_by_username=(owner_names or {}).get(d.get("requested_by_user_id")),
        admin_enable_requested=bool(d.get("admin_enable_requested")),
        credential_generation=credential.generation if credential else 0,
        legacy_allowed=bool(credential.legacy_allowed) if credential else True,
        has_credential=bool(credential and credential.token_sha256),
    )


@router.get("", response_model=list[NodeOut])
async def list_nodes(
    q: str | None = None,
    store: RedisStore = Depends(get_store),
    p: Principal = Depends(get_principal),
):
    """节点列表（按创建时间排序）；可选 q 按名称 / MT5 账号模糊搜索。普通用户只看本人节点。"""
    nodes = permissions.visible(p, await store.all_nodes())
    if q and (term := q.strip()):
        nodes = [n for n in nodes if _node_matches_search(n, term)]
    nodes.sort(key=lambda n: n.get("created_at", 0))
    names = await rbac_service.owner_names_for(p)
    return [await _to_node_out(store, n, names) for n in nodes]


@router.post("", response_model=NodeOut, status_code=201)
async def create_node(
    body: NodeCreate,
    request: Request,
    store: RedisStore = Depends(get_store),
    p: Principal = Depends(require_admin),
):
    """创建节点（管理员手动）。鉴权令牌为全局共享，见账户设置 → 节点令牌。

    mt5_login 全局唯一；若已存在同 MT5 登录号的节点则返回 409。
    """
    try:
        d = await node_service.create_node(store, body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except IntegrityError:
        raise HTTPException(
            status_code=409,
            detail=f"node with mt5_login={body.mt5_login} already exists",
        )
    await persist.audit(
        p.username, "create_node", d["node_id"], None, "ok", client_ip(request),
        category="node", before=None, after=_node_audit_snapshot(d),
    )
    return await _to_node_out(store, d)


@router.get("/{node_id}", response_model=NodeOut)
async def get_node(
    node_id: str,
    store: RedisStore = Depends(get_store),
    p: Principal = Depends(get_principal),
):
    d = await owned_node_or_404(store, p, node_id)
    return await _to_node_out(store, d, await rbac_service.owner_names_for(p))


@router.get("/{node_id}/dispatches", response_model=PaginatedNodeDispatches)
async def node_dispatches(
    node_id: str,
    page: int = 1,
    page_size: int = 20,
    store: RedisStore = Depends(get_store),
    p: Principal = Depends(get_principal),
):
    """某节点分发/成交明细分页（持久化历史，供详情页「信号」「成交回报」Tab）。"""
    await owned_node_or_404(store, p, node_id)
    return await persist.recent_dispatches(node_id, page, page_size)


@router.patch("/{node_id}", response_model=NodeOut)
async def update_node(
    node_id: str,
    body: NodeUpdate,
    request: Request,
    store: RedisStore = Depends(get_store),
    p: Principal = Depends(require_menu(MENU_NODES)),
):
    """更新节点配置（名称、启用状态、账户级风控；按币种配置仅管理员）。"""
    current = await owned_node_or_404(store, p, node_id)
    if current.get("approval_status") == "pending" and body.enabled is not None and not p.is_admin:
        raise HTTPException(status_code=403, detail="新增节点须由管理员审核开通")
    if body.filters is not None and not p.is_admin:
        raise HTTPException(status_code=403, detail="按币种配置（normal 链路）仅管理员可修改")
    before = _node_audit_snapshot(current)
    try:
        d = await node_service.update_node(store, node_id, body, admin_enable_intent=p.is_admin,
                                           audit_principal=p, audit_ip=client_ip(request))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except SQLAlchemyError as e:
        raise HTTPException(status_code=503, detail="audit or database unavailable") from e
    if not d:
        raise HTTPException(status_code=404, detail="node not found")
    # 风控配置变更：立即下发给在线节点（离线节点登录时从 auth_ok 拉取）
    if body.risk is not None:
        await risk_control.push_risk_config_to_node(node_id, d.get("risk"))
    await persist.audit(
        p.username, "update_node", node_id, body.model_dump(exclude_none=True), "ok",
        client_ip(request),
        category="node", before=before, after=_node_audit_snapshot(d),
    )
    return await _to_node_out(store, d, await rbac_service.owner_names_for(p))


@router.delete("/{node_id}")
async def delete_node(
    node_id: str,
    request: Request,
    store: RedisStore = Depends(get_store),
    p: Principal = Depends(require_admin),
):
    before = _node_audit_snapshot(await store.get_node(node_id))
    ok = await node_service.delete_node(store, node_id)
    if not ok:
        raise HTTPException(status_code=404, detail="node not found")
    await persist.audit(
        p.username, "delete_node", node_id, None, "ok", client_ip(request),
        category="node", before=before, after=None,
    )
    return {"status": "deleted", "node_id": node_id}


@router.post("/lot")
async def batch_lot(
    body: LotBatch,
    request: Request,
    store: RedisStore = Depends(get_store),
    p: Principal = Depends(require_admin),
):
    """批量设置多个节点的手数策略。"""
    updated = []
    for nid in body.node_ids:
        d = await node_service.update_node(
            store, nid, NodeUpdate(lot_mode=body.lot_mode, lot=body.lot)
        )
        if d:
            updated.append(nid)
    await persist.audit(
        p.username, "batch_lot", ",".join(updated), body.model_dump(), "ok", client_ip(request),
        category="node", before=None, after=body.model_dump(),
    )
    return {"status": "ok", "updated": updated}
