"""轮询领取后台 worker（文档 9.6）。

语义：一条“轮询信号”会被所有符合条件的节点按确定顺序（poll_order，其次
created_at）依次串行消费。进度游标持久化到 Redis，进程重启后能从断点续跑；
某节点出错/超时会重试 POLL_MAX_RETRY 次，仍失败则跳过，绝不阻塞后续节点。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from .connections import manager
from .dispatcher import Dispatcher, dict_to_signal
from .redis_store import RedisStore
from .settings import settings

logger = logging.getLogger(__name__)


class PollWorker:
    def __init__(self, store: RedisStore, dispatcher: Dispatcher) -> None:
        self.store = store
        self.dispatcher = dispatcher
        self._task: Optional[asyncio.Task] = None
        self._stop = False

    def start(self) -> None:
        """随应用启动，作为后台任务常驻运行。"""
        self._stop = False
        self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        """应用关闭时优雅停止。"""
        self._stop = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def run(self) -> None:
        """主循环：从队列取出待处理 signal_id，逐个处理；队列空时短暂休眠。"""
        logger.info("poll worker started")
        while not self._stop:
            try:
                signal_id = await self.store.poll_dequeue()
                if not signal_id:
                    await asyncio.sleep(0.5)
                    continue
                await self.process(signal_id)
            except asyncio.CancelledError:
                break
            except Exception:  # noqa: BLE001
                # 单条处理异常不应让整个 worker 退出
                logger.exception("poll worker loop error")
                await asyncio.sleep(1)

    async def process(self, signal_id: str) -> None:
        """处理一条轮询信号：从 cursor 开始，对剩余节点依次执行并持久化进度。"""
        progress = await self.store.get_poll_progress(signal_id)
        if not progress:
            return
        signal = dict_to_signal(progress["signal"])
        scope = progress.get("scope", "symbol")
        nodes: list[str] = progress["nodes"]
        cursor: int = progress.get("cursor", 0)

        global_lot = await self.store.get_lot_global()
        filters = await self.store.get_filters()
        nodemap = {n["node_id"]: n for n in await self.store.all_nodes()}

        i = cursor
        while i < len(nodes):
            node_id = nodes[i]
            node = nodemap.get(node_id)
            # 节点需仍然存在、启用且在线，否则跳过（不重试）
            if node and node.get("enabled", True) and manager.is_node_online(node_id):
                await self._run_with_retry(node, signal, signal_id, scope, global_lot, filters)
            else:
                logger.info("poll skip offline/disabled node %s", node_id)
            # 每处理完一个节点就推进并持久化游标，保证可断点续跑
            i += 1
            progress["cursor"] = i
            await self.store.save_poll_progress(signal_id, progress)

        logger.info("poll done %s (%d nodes)", signal_id, len(nodes))

    async def _run_with_retry(self, node, signal, signal_id, scope, global_lot, filters) -> str:
        """对单节点执行开仓并等待回报；失败/超时按指数退避重试，超过上限则跳过。"""
        attempts = 0
        while attempts <= settings.poll_max_retry:
            res = await self.dispatcher.try_open(
                node, signal, signal_id, scope, global_lot, filters,
                wait=True, timeout=settings.poll_ack_timeout,
            )
            status = res.get("status")
            # done=成功、skipped=被过滤（均无需重试）
            if status in ("done", "skipped"):
                return status
            attempts += 1
            if attempts <= settings.poll_max_retry:
                await asyncio.sleep(min(2 ** attempts, 8))  # 退避：2s,4s,8s 封顶
        logger.warning("poll node %s failed after retries, skipping", node.get("node_id"))
        return "failed"
