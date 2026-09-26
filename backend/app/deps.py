"""FastAPI 依赖项（鉴权 + 共享服务注入）。

后台鉴权分三层：`get_principal` 校验 JWT 得到登录身份；`require_menu` /
`require_admin` 校验功能权限；`owned_*_or_404` 校验数据归属（不属于自己的
一律 404，不暴露资源是否存在）。
"""
from typing import Optional

from fastapi import Depends, Header, HTTPException, Request

from . import permissions, rbac_service, system_settings
from .dispatcher import Dispatcher
from .group_dispatcher import GroupDispatcher
from .permissions import Principal
from .redis_store import RedisStore
from .security import compare_secret
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


async def get_principal(authorization: Optional[str] = Header(default=None)) -> Principal:
    """从 Authorization: Bearer <jwt> 解析登录身份（已禁用 / 会话已吊销视为无效）。"""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    p = await rbac_service.principal_from_token(state.store, authorization.split(" ", 1)[1])
    if p is None:
        raise HTTPException(status_code=401, detail="invalid or expired token")
    return p


async def get_current_admin(p: Principal = Depends(get_principal)) -> str:
    """当前登录用户名（沿用旧名）。只要求已登录；功能与数据权限由 require_* / owned_* 把关。"""
    return p.username


def require_menu(*codes: str):
    """要求拥有任一菜单（同一操作可能出现在多个页面）；管理员直接放行。"""
    async def _require(p: Principal = Depends(get_principal)) -> Principal:
        if not permissions.can_menu(p, *codes):
            raise HTTPException(status_code=403, detail="无权访问该功能")
        return p
    return _require


async def require_admin(p: Principal = Depends(get_principal)) -> Principal:
    """仅超级管理员：全局配置、全局写操作与系统级页面。"""
    if not p.is_admin:
        raise HTTPException(status_code=403, detail="仅管理员可执行该操作")
    return p


async def get_admin_username(p: Principal = Depends(require_admin)) -> str:
    """仅超级管理员，返回用户名（供只需要审计操作人的管理员接口直接替换 get_current_admin）。"""
    return p.username


async def owned_node_or_404(store: RedisStore, p: Principal, node_id: str) -> dict:
    d = await store.get_node(node_id)
    if not d or not permissions.owns(p, d.get("owner_user_id")):
        raise HTTPException(status_code=404, detail="node not found")
    return d


async def owned_group_or_404(store: RedisStore, p: Principal, group_id: str) -> dict:
    d = await store.get_group(group_id)
    if not d or not permissions.owns(p, d.get("owner_user_id")):
        raise HTTPException(status_code=404, detail="group not found")
    return d


async def owned_strategy_or_404(store: RedisStore, p: Principal, strategy_id: str) -> dict:
    d = await store.get_strategy(strategy_id)
    if not d or not permissions.owns(p, d.get("owner_user_id")):
        raise HTTPException(status_code=404, detail="strategy not found")
    return d


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
