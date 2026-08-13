"""FastAPI 依赖项（鉴权 + 共享服务注入）。"""
from typing import Optional

from fastapi import Depends, Header, HTTPException, Request

from . import system_settings
from .dispatcher import Dispatcher
from .group_dispatcher import GroupDispatcher
from .redis_store import RedisStore
from .security import compare_secret, verify_jwt
from .state import state


def get_store() -> RedisStore:
    """注入 Redis 存储；服务尚未就绪时返回 503。"""
    if state.store is None:
        raise HTTPException(status_code=503, detail="service not ready")
    return state.store


def get_dispatcher() -> Dispatcher:
    """注入分发引擎（model=normal，按币种分发）。"""
    if state.dispatcher is None:
        raise HTTPException(status_code=503, detail="service not ready")
    return state.dispatcher


def get_group_dispatcher() -> GroupDispatcher:
    """注入分组分发引擎（model=strategy，按分组分发）。"""
    if state.group_dispatcher is None:
        raise HTTPException(status_code=503, detail="service not ready")
    return state.group_dispatcher


async def get_current_admin(authorization: Optional[str] = Header(default=None)) -> str:
    """从 Authorization: Bearer <jwt> 解析并校验管理员身份。"""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    sub = verify_jwt(authorization.split(" ", 1)[1])
    if not sub:
        raise HTTPException(status_code=401, detail="invalid or expired token")
    return sub


async def get_node_token_auth(
    x_node_token: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
    store: RedisStore = Depends(get_store),
) -> str:
    """校验全局节点接入令牌（NODE_TOKEN），供节点端与本机运维面板调用。

    面板手上只有节点 .env 里的 NODE_TOKEN，没有管理员 JWT。该令牌全局共享且以
    明文分发到每台节点机，所以只授权「查发布版本」「下载安装包」这类只读接口；
    上传、发布、降级、删除一律仍需管理员 JWT。
    """
    token = (x_node_token or "").strip()
    if not token and authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="missing node token")
    expected, _ = await system_settings.get_node_token(store)
    if not expected or not compare_secret(token, expected):
        raise HTTPException(status_code=401, detail="invalid node token")
    return token


def client_ip(request: Request) -> str:
    """获取客户端真实 IP（优先取 nginx 透传的 X-Forwarded-For）。"""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "?"
