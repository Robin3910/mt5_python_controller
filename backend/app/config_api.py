"""运行期配置 API：全局手数、区间过滤、全局节点令牌（需管理员鉴权）。

这些配置存于 Redis（运行期实时态），下发分发时即时读取生效。
节点令牌为持久化配置（MySQL/SQLite + Redis 缓存，见 system_settings）。
"""
from fastapi import APIRouter, Depends, Request

from . import persist, system_settings
from .deps import client_ip, get_current_admin, get_store
from .models import LotConfig, NodeTokenInfo
from .redis_store import RedisStore

router = APIRouter(prefix="/api/config", tags=["config"])


@router.get("/lot", response_model=LotConfig)
async def get_lot(store: RedisStore = Depends(get_store), _: str = Depends(get_current_admin)):
    return LotConfig(**await store.get_lot_global())


@router.put("/lot", response_model=LotConfig)
async def set_lot(
    body: LotConfig,
    request: Request,
    store: RedisStore = Depends(get_store),
    admin: str = Depends(get_current_admin),
):
    """设置全局手数（影响所有“跟随全局”策略的节点）。"""
    await store.set_lot_global(body.model_dump())
    await persist.audit(admin, "set_lot_global", None, body.model_dump(), "ok", client_ip(request))
    return body


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
    """设置多区间方向过滤（以品种为键的对象，结构见前端配置页说明）。"""
    await store.set_filters(body)
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
