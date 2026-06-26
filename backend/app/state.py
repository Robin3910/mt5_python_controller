"""应用生命周期内装配的共享单例（Redis 存储、分发器、轮询 worker）。"""
from typing import Optional

from .dispatcher import Dispatcher
from .poll_queue import PollWorker
from .redis_store import RedisStore


class AppState:
    store: Optional[RedisStore] = None        # Redis 访问层
    dispatcher: Optional[Dispatcher] = None   # 分发引擎
    poll_worker: Optional[PollWorker] = None  # 轮询后台 worker


# 全局应用状态（在 main.py 的 lifespan 中初始化）
state = AppState()
