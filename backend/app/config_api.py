"""运行期配置 API：区间过滤、全局节点令牌（需管理员鉴权）。

这些配置存于 Redis（运行期实时态），下发分发时即时读取生效。
节点令牌为持久化配置（MySQL/SQLite + Redis 缓存，见 system_settings）。
"""
from fastapi import APIRouter, Depends, HTTPException, Request

from . import persist, rules, system_settings
from .connections import manager
from .deps import client_ip, get_current_admin, get_store
from .models import NodeTokenInfo
from .redis_store import RedisStore

router = APIRouter(prefix="/api/config", tags=["config"])


async def push_watch_symbols_to_nodes(filters_cfg: dict) -> None:
    """把中控台 filters 品种列表推给所有在线节点，供其合并进观察报价列表。"""
    msg = {
        "type": "watch_symbols",
        "data": {"symbols": rules.filter_watch_symbols(filters_cfg)},
    }
    for node_id in manager.online_node_ids():
        await manager.send_to_node(node_id, msg)


@router.get("/filters")
async def get_filters(store: RedisStore = Depends(get_store), _: str = Depends(get_current_admin)):
    return await store.get_filters()


@router.put("/filters")
async def set_filters(
    body: dict,
    request: Request,
    store: RedisStore = Depends(get_store),
    admin: str = Depends(get_current_admin),
):
    """设置多区间方向过滤（以品种为键的对象，结构见前端配置页说明）。

    关闭某品种「启用全局手数」时，若仍有节点将该品种手数策略设为「跟随中控台」，则拒收。
    """
    nodes = await store.all_nodes()
    err = rules.validate_disable_global_lot(body or {}, nodes)
    if err:
        raise HTTPException(status_code=400, detail=err)
    await store.set_filters(body)
    await push_watch_symbols_to_nodes(body)
    await persist.audit(admin, "set_filters", None, body, "ok", client_ip(request))
    return body


# ----------------- 全局节点接入令牌 -----------------
@router.get("/node-token", response_model=NodeTokenInfo)
async def get_node_token(
    store: RedisStore = Depends(get_store),
    _: str = Depends(get_current_admin),
):
    """获取当前全局节点接入令牌（所有节点共享）。"""
    token, updated_at = await system_settings.get_node_token(store)
    return NodeTokenInfo(token=token, updated_at=updated_at)


@router.post("/node-token/rotate", response_model=NodeTokenInfo)
async def rotate_node_token(
    request: Request,
    store: RedisStore = Depends(get_store),
    admin: str = Depends(get_current_admin),
):
    """重置全局节点接入令牌：旧令牌立即失效，所有节点需要更新 .env 后重连。"""
    token, updated_at = await system_settings.rotate_node_token(store)
    await persist.audit(admin, "rotate_node_token", None, None, "ok", client_ip(request))
    return NodeTokenInfo(token=token, updated_at=updated_at)
