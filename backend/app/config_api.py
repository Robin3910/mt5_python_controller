"""运行期配置 API：区间过滤、全局节点令牌、Webhook token、趋势面板参数。

这些配置存于 Redis（运行期实时态），下发分发时即时读取生效。
节点令牌与趋势面板参数为持久化配置（MySQL/SQLite，见 system_settings）。

全部是全局配置：读写都仅管理员；唯一例外是趋势参数的读取（普通用户的趋势面板
要用它换算得分）。Webhook token 只给管理员——它同样能发 normal 信号。
"""
from fastapi import APIRouter, Depends, HTTPException, Request

from . import persist, rules, system_settings
from .connections import manager
from .deps import client_ip, get_current_admin, get_store, require_admin
from .models import NodeTokenInfo, WebhookAuthInfo, WebhookAuthUpdate
from .permissions import Principal
from .settings import settings
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
async def get_filters(store: RedisStore = Depends(get_store), _: Principal = Depends(require_admin)):
    return await store.get_filters()


@router.put("/filters")
async def set_filters(
    body: dict,
    request: Request,
    store: RedisStore = Depends(get_store),
    p: Principal = Depends(require_admin),
):
    """设置多区间方向过滤（以品种为键的对象，结构见前端配置页说明）。

    关闭某品种「启用全局手数」时，若仍有节点将该品种手数策略设为「跟随中控台」，则拒收。
    """
    nodes = await store.all_nodes()
    err = rules.validate_disable_global_lot(body or {}, nodes)
    if err:
        raise HTTPException(status_code=400, detail=err)
    before = await store.get_filters()
    await store.set_filters(body)
    await push_watch_symbols_to_nodes(body)
    await persist.audit(
        p.username, "set_filters", None, None, "ok", client_ip(request),
        category="console", before=before or {}, after=body or {},
    )
    return body


# ----------------- 全局节点接入令牌 -----------------
@router.get("/node-token", response_model=NodeTokenInfo)
async def get_node_token(
    store: RedisStore = Depends(get_store),
    _: Principal = Depends(require_admin),
):
    """获取当前全局节点接入令牌（所有节点共享）。"""
    token, updated_at = await system_settings.get_node_token(store)
    return NodeTokenInfo(token=token, updated_at=updated_at)


@router.post("/node-token/rotate", response_model=NodeTokenInfo)
async def rotate_node_token(
    request: Request,
    store: RedisStore = Depends(get_store),
    p: Principal = Depends(require_admin),
):
    """重置全局节点接入令牌：旧令牌立即失效，所有节点需要更新 .env 后重连。"""
    token, updated_at = await system_settings.rotate_node_token(store)
    await persist.audit(
        p.username, "rotate_node_token", None, None, "ok", client_ip(request), category="system",
    )
    return NodeTokenInfo(token=token, updated_at=updated_at)


@router.get("/webhook-auth", response_model=WebhookAuthInfo)
async def get_webhook_auth(
    store: RedisStore = Depends(get_store),
    _: Principal = Depends(require_admin),
):
    """读取 Webhook 鉴权开关与共享 token，供中控台把 token 写进可复制的信号 JSON。"""
    enabled = await system_settings.is_webhook_auth_enabled(store)
    return WebhookAuthInfo(enabled=enabled, token=settings.auth_token or "")


@router.put("/webhook-auth", response_model=WebhookAuthInfo)
async def set_webhook_auth(
    body: WebhookAuthUpdate,
    request: Request,
    store: RedisStore = Depends(get_store),
    p: Principal = Depends(require_admin),
):
    """保存 Webhook token 校验开关。关闭后 /webhook 不再校验 AUTH_TOKEN。"""
    before = await system_settings.is_webhook_auth_enabled(store)
    enabled = await system_settings.set_webhook_auth_enabled(store, body.enabled)
    await persist.audit(
        p.username, "set_webhook_auth", None, {"enabled": enabled}, "ok", client_ip(request),
        category="console", before={"enabled": before}, after={"enabled": enabled},
    )
    return WebhookAuthInfo(enabled=enabled, token=settings.auth_token or "")


# ----------------- 趋势面板参数（全局共享，不分节点/币种）-----------------
@router.get("/trend")
async def get_trend_config(_: str = Depends(get_current_admin)):
    """获取全局趋势面板参数（EMA/RSI 周期、权重、阈值等）。"""
    return await system_settings.get_trend_config()


@router.put("/trend")
async def set_trend_config(
    body: dict,
    request: Request,
    p: Principal = Depends(require_admin),
):
    """保存全局趋势面板参数；越界值会被夹回合法区间。"""
    before = await system_settings.get_trend_config()
    after = await system_settings.set_trend_config(body or {})
    await persist.audit(
        p.username, "set_trend_config", None, None, "ok", client_ip(request),
        category="console", before=before, after=after,
    )
    return after
