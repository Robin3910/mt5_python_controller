"""应用生命周期内装配的共享单例（Redis 存储、分发器、轮询 worker）。"""
from typing import TYPE_CHECKING, Optional

from .dispatcher import Dispatcher
from .group_dispatcher import GroupDispatcher
from .poll_queue import PollWorker
from .redis_store import RedisStore

if TYPE_CHECKING:
    # deal_agg 会 import follow_service -> group_service -> limit_watch -> state，
    # 在这里真 import 会形成环（state 还没定义完）。只用类型标注，故走 TYPE_CHECKING。
    from .deal_agg import LeaderStatsAggregator


class AppState:
    store: Optional[RedisStore] = None        # Redis 访问层
    dispatcher: Optional[Dispatcher] = None   # 分发引擎（model=normal，按币种）
    group_dispatcher: Optional[GroupDispatcher] = None  # 分发引擎（model=strategy，按分组）
    poll_worker: Optional[PollWorker] = None  # 轮询后台 worker
    # 带单员绩效预聚合 worker（后台跑，落 Redis 缓存）
    leader_stats_agg: Optional["LeaderStatsAggregator"] = None


# 全局应用状态（在 main.py 的 lifespan 中初始化）
state = AppState()
