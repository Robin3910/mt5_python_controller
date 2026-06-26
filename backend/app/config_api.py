"""运行期配置 API：全局手数、区间过滤、分发模式/范围（需管理员鉴权）。

这些配置存于 Redis（运行期实时态），下发分发时即时读取生效。
"""
from fastapi import APIRouter, Depends, Request

from . import persist
from .deps import client_ip, get_current_admin, get_store
from .models import DispatchConfig, LotConfig
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


@router.get("/dispatch", response_model=DispatchConfig)
async def get_dispatch(store: RedisStore = Depends(get_store), _: str = Depends(get_current_admin)):
    return DispatchConfig(**await store.get_dispatch())


@router.put("/dispatch", response_model=DispatchConfig)
async def set_dispatch(
    body: DispatchConfig,
    request: Request,
    store: RedisStore = Depends(get_store),
    admin: str = Depends(get_current_admin),
):
    """切换分发模式(sync/poll)与持仓判定范围(symbol/account)。"""
    await store.set_dispatch(body.model_dump())
    await persist.audit(admin, "set_dispatch", None, body.model_dump(), "ok", client_ip(request))
    return body
