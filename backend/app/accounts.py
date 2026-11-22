"""账户 / 持仓查询 API（普通用户只看本人节点）。"""
import logging

from fastapi import APIRouter, Depends, HTTPException

from . import group_persist, permissions
from .connections import manager
from .deps import get_principal, get_store, owned_node_or_404
from .node_lifecycle import locked, node_key
from .permissions import Principal
from .redis_store import RedisStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["accounts"])


async def annotate_strategy_templates(node_id: str, acct: dict) -> dict:
    """由子任务快照为持仓和挂单补充策略模版与开仓方式。

    节点只上报裸的 MT5 订单，不知道它属于哪条策略；按魔术号回子任务快照反查，
    前端据此把「AI 智能加仓（tpl_1）」的仓位从普通交易列表里摘出去。
    反查失败不报错：标不上模版，前端按普通持仓展示。

    快照入库链路（`ws_gateway`）与查询接口共用本函数，保证 REST / WS 两份数据一致。
    """
    if not isinstance(acct, dict):
        return acct
    magics: set[int] = set()
    for key in ("positions", "orders"):
        for row in acct.get(key) or []:
            if not isinstance(row, dict):
                continue
            try:
                magic = int(row.get("magic") or 0)
            except (TypeError, ValueError):
                continue
            if magic:
                magics.add(magic)
    if not magics:
        return acct
    try:
        metadata = await group_persist.strategy_metadata_by_magic(node_id, magics)
    except Exception:  # noqa: BLE001
        logger.warning("annotate strategy metadata failed for node %s", node_id, exc_info=True)
        return acct
    if not metadata:
        return acct
    for key in ("positions", "orders"):
        rows = acct.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                tpl, opening_method = metadata.get(int(row.get("magic") or 0), (None, None))
            except (TypeError, ValueError):
                continue
            if tpl:
                row["ai_template"] = tpl
            if opening_method:
                row["opening_method"] = opening_method
    return acct


async def snapshot_with_templates(node_id: str) -> dict | None:
    """读取 Redis 里的最新账户快照并补齐策略模版和开仓方式。

    与节点快照入库共用 `node:<id>` 锁：避免读到「DB 里子任务还没落好、
    快照已经更新」的中间态，也避免同一快照被并发标两次。
    快照不存在时返回 None。
    """
    from .state import state
    async with locked(node_key(node_id)):
        acct = await state.store.get_account(node_id)
        if acct is None:
            return None
        return await annotate_strategy_templates(node_id, acct)


@router.get("/accounts")
@router.get("/accounts")
async def all_accounts(
    store: RedisStore = Depends(get_store), p: Principal = Depends(get_principal),
):
    """汇总可见节点的账户快照（含在线状态）。"""
    out = []
    for n in permissions.visible(p, await store.all_nodes_with_db_fallback()):
        acct = await snapshot_with_templates(n["node_id"]) or {}
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
    node_id: str, store: RedisStore = Depends(get_store), p: Principal = Depends(get_principal),
):
    """查询单个节点的账户快照；尚无上报时返回 404。"""
    await owned_node_or_404(store, p, node_id)
    acct = await snapshot_with_templates(node_id)
    if acct is None:
        raise HTTPException(status_code=404, detail="no account snapshot yet")
    return acct
