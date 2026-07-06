"""FastAPI 应用入口。

在 lifespan 中完成装配：建表 -> 连接 Redis -> 预热节点缓存 -> 启动轮询 worker。
路由划分：登录 / Webhook / 节点 WS 网关 / 节点管理 / 配置 / 账户 / 平仓 / 后台 WS。
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import (
    accounts,
    admin_ws,
    auth,
    close,
    config_api,
    events,
    node_service,
    nodes,
    system_settings,
    user_service,
    webhook,
    ws_gateway,
    twofa,
)
from .connections import manager
from .db import init_db
from .dispatcher import Dispatcher
from .poll_queue import PollWorker
from .redis_store import RedisStore
from .settings import settings
from .state import state

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("mt5hub")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动/关闭钩子：在此装配并回收所有共享资源。"""
    await init_db()
    store = RedisStore.from_url()
    try:
        await store.r.ping()
        logger.info("redis connected: %s", settings.redis_url)
    except Exception as e:  # noqa: BLE001
        # Redis 不可用时仍允许启动（降级），但实时态能力会受影响
        logger.warning("redis ping failed (%s) — running degraded", e)
    count = await node_service.warm_cache(store)
    logger.info("warmed %d node(s) into cache", count)
    await user_service.seed_default_admin(store)
    # 保证全局节点接入令牌存在；首次启动自动生成（管理员可在「账户设置」页面查看/重置）
    token = await system_settings.ensure_node_token(store)
    logger.info("global node token ready (length=%d)", len(token))

    state.store = store
    state.dispatcher = Dispatcher(store)
    state.poll_worker = PollWorker(store, state.dispatcher)
    state.poll_worker.start()
    logger.info("MT5 hub ready")
    try:
        yield
    finally:
        # 优雅关闭：停止轮询 worker、断开 Redis
        if state.poll_worker:
            await state.poll_worker.stop()
        await store.close()


app = FastAPI(title="MT5 Multi-Node Hub", version="0.1.0", lifespan=lifespan)
# 跨域：前后端同源部署时其实用不到；分离开发时放开（不带凭证）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=False,
)

# 注册所有路由
for r in (
    auth.router,
    twofa.router,
    webhook.router,
    ws_gateway.router,
    nodes.router,
    config_api.router,
    events.router,
    accounts.router,
    close.router,
    admin_ws.router,
):
    app.include_router(r)


@app.get("/health")
async def health():
    """健康检查：供容器 HEALTHCHECK / 负载均衡探活使用。"""
    redis_ok = False
    if state.store:
        try:
            redis_ok = bool(await state.store.r.ping())
        except Exception:  # noqa: BLE001
            redis_ok = False
    return {
        "status": "ok",
        "redis": redis_ok,
        "online_nodes": len(manager.online_node_ids()),
    }


@app.get("/")
async def root():
    return {"service": "mt5-multi-node-hub", "docs": "/docs", "health": "/health"}
