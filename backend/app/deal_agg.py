"""带单员绩效的**后台预聚合**（P4）。

为什么要有这个后台任务：
带单广场一次要展示 N 个带单员的绩效。逐个查库就是 N+1，节点一多必然超时。
所以这里每隔几分钟把「所有带单员 × 四个窗口」的绩效一次算完，写进 Redis；
列表接口只读 Redis，**未命中不回源查库**（保证接口延迟恒定）。

**口径唯一性**：这里的聚合调用 `deal_stats.leader_batch_multi`，与订单历史页
调的是同一套 `deal_stats` 实现 —— 两处各算一次必然漂移，届时会出现
「同一节点在订单历史页与带单页胜率不一致」。

TTL 15min > 刷新周期 5min：后台任务偶发失败时，缓存里的旧值还能顶一阵。

**两种口径**：
- `K_LEADER_STATS`（带单页）：带单员名下**全部节点**的成交聚合；
- `K_ROBOT_STATS`（AI 机器人页）：只聚合**主节点**。

两者分开 key、共用同一个 `deal_stats.leader_batch_multi` 实现 —— 口径不同的
是「聚合哪些节点」，聚合算法本身只有一份，不会漂移。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from . import deal_stats, follow_service, permissions, rbac_service, user_service
from .redis_store import RedisStore

logger = logging.getLogger(__name__)

# 预聚合周期（秒）与缓存 TTL（秒）
AGG_INTERVAL = 300
AGG_TTL = 900


class LeaderStatsAggregator:
    """带单员绩效预聚合 worker。

    与 `PollWorker` 一样在 lifespan 里 start/stop，随应用启停。
    """

    def __init__(self, store: RedisStore) -> None:
        self._store = store
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self.run(), name="leader-stats-agg")
        logger.info("leader stats aggregator started (interval=%ss)", AGG_INTERVAL)

    async def stop(self) -> None:
        self._stop.set()
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    async def run(self) -> None:
        """循环执行预聚合。任何一轮失败都不该让 worker 退出。"""
        while not self._stop.is_set():
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning("leader stats refresh failed: %s", e)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=AGG_INTERVAL)
            except asyncio.TimeoutError:
                continue

    async def refresh(self) -> int:
        """跑一轮预聚合，返回写入缓存的带单员数量。

        「有资格成为带单员」= 持有 follow_leader 能力且启用。
        绩效口径 = 该用户名下**全部节点**的成交聚合（与订单历史页同一套实现）。
        """
        store = self._store
        users = await user_service.list_users()
        leader_ids: set[int] = set()
        for u in users:
            if not u.is_active:
                continue
            principal = await rbac_service.load_principal(store, u.username)
            if principal and permissions.can_be_leader(principal):
                leader_ids.add(u.id)
        if not leader_ids:
            return 0

        all_nodes = await store.all_nodes()
        # leader_user_id -> 名下全部 node_id
        leader_nodes: dict[int, list[str]] = {uid: [] for uid in leader_ids}
        for n in all_nodes:
            owner = permissions.normalize_owner(n.get("owner_user_id"))
            if owner is None or owner not in leader_nodes:
                continue
            nid = n.get("node_id")
            if nid:
                leader_nodes[owner].append(str(nid))

        # 跟单人数：跟了该带单员任一分组的去重节点数（跟单关系表，非订单数据）
        followers_by_leader: dict[int, set[str]] = {}
        for f in await follow_service.list_for_leaders(leader_ids):
            followers_by_leader.setdefault(int(f["leader_user_id"]), set()).add(
                str(f.get("node_id"))
            )

        written = 0
        for window in deal_stats.WINDOWS:
            from_ts = deal_stats.window_start(window)
            # 一次查询算完全部带单员（不是逐个 leader_batch）
            summaries = await deal_stats.leader_batch_multi(leader_nodes, from_ts, None)
            for uid in leader_ids:
                # 缓存结构：{window: {**summary, follower_count}}
                # summary 的字段与 DealSummary 一致，follower_count 附加在后。
                perf = dict(summaries.get(uid) or {})
                perf["follower_count"] = len(followers_by_leader.get(uid, set()))
                cached = await store.get_leader_stats(uid) or {}
                cached[window] = perf
                await store.set_leader_stats(uid, cached, AGG_TTL)
            written = len(leader_ids)
        logger.info("leader stats refreshed: %d leader(s)", written)
        written += await self._refresh_robot_stats(store, users, all_nodes)
        return written

    async def _refresh_robot_stats(self, store: RedisStore, users: list, all_nodes: list[dict]) -> int:
        """AI 机器人的绩效预聚合 —— **主节点口径**，单独存一份。

        与带单页的差别在**节点范围**：带单页聚合名下全部节点，AI 机器人页只聚合
        主节点。AI 机器人的展示单位就是主节点（见 models.AiRobotSummary 类注释），
        跟着它走的账户看到的也是主节点实盘；混进名下其它节点会让横向对比失真。

        这里不能直接复用 `refresh()` 的 leader_nodes：那已经是「全部节点」了。
        重新从 all_nodes 里挑主节点拼一份 {user_id: [primary_node_id]}，
        仍然走 `leader_batch_multi` —— **一次查询**，口径实现与订单历史页同一套。
        """
        # user_id -> [主节点 node_id]（没有主节点的用户不入 map，leader_batch_multi 会给空统计）
        robot_nodes: dict[int, list[str]] = {}
        for n in all_nodes:
            owner = permissions.normalize_owner(n.get("owner_user_id"))
            if owner is None or not n.get("is_primary"):
                continue
            nid = n.get("node_id")
            if nid:
                robot_nodes.setdefault(owner, []).append(str(nid))
        if not robot_nodes:
            return 0

        written = 0
        for window in deal_stats.WINDOWS:
            from_ts = deal_stats.window_start(window)
            summaries = await deal_stats.leader_batch_multi(robot_nodes, from_ts, None)
            for uid in robot_nodes:
                cached = await store.get_robot_stats(uid) or {}
                cached[window] = dict(summaries.get(uid) or {})
                await store.set_robot_stats(uid, cached, AGG_TTL)
            written = len(robot_nodes)
        logger.info("robot stats refreshed: %d robot(s) (primary node only)", written)
        return written
