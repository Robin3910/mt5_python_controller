"""FastAPI 依赖项（鉴权 + 共享服务注入）。"""
from typing import Optional

from fastapi import Header, HTTPException, Request

from .dispatcher import Dispatcher
from .redis_store import RedisStore
from .security import verify_jwt
from .state import state


def get_store() -> RedisStore:
    """注入 Redis 存储；服务尚未就绪时返回 503。"""
    if state.store is None:
        raise HTTPException(status_code=503, detail="service not ready")
    return state.store


def get_dispatcher() -> Dispatcher:
    """注入分发引擎。"""
    if state.dispatcher is None:
        raise HTTPException(status_code=503, detail="service not ready")
    return state.dispatcher


async def get_current_admin(authorization: Optional[str] = Header(default=None)) -> str:
    """从 Authorization: Bearer <jwt> 解析并校验管理员身份。"""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    sub = verify_jwt(authorization.split(" ", 1)[1])
    if not sub:
        raise HTTPException(status_code=401, detail="invalid or expired token")
    return sub


def client_ip(request: Request) -> str:
    """获取客户端真实 IP（优先取 nginx 透传的 X-Forwarded-For）。"""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "?"
