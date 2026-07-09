"""轮询轮转领取后台 worker（文档 9.6）。

语义：一条“轮询信号”只会被参与该品种轮询的节点中、轮转队首**第一个能真正成交**的
节点领取消费——即“一个信号 = 一个节点开仓”。领取成功后该节点被移动到轮转队尾，其余
节点保持原相对顺序，等待下次新信号按序领取，如此循环轮转（round-robin 派单）。

要点：
- 轮转顺序按品种独立维护，持久化于 Redis（signal:poll:rotation:{symbol}）；初始顺序
  按 poll_order、其次 created_at。
- 队首节点若离线 / 被 9.2/9.3 过滤跳过 / 重试后仍失败，则顺延给下一个候选节点，直到
  有节点成交；仅“真正开仓成功”的节点移动到队尾，被跳过/未轮到的节点保持原位。
- 每个节点轮到时用其最新账户快照实时判定区间/持仓（9.2/9.3）。
- 单节点失败/超时按 POLL_MAX_RETRY 指数退避重试后再顺延。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from . import rules
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
        """处理一条轮询信号：在该品种轮转顺序里挑选队首第一个可成交的节点领取消费。"""
        progress = await self.store.get_poll_progress(signal_id)
        if not progress:
            return
        signal = dict_to_signal(progress["signal"])
        scope = progress.get("scope", "symbol")
        symbol = signal.symbol
        # 轮转顺序按“逻辑品种”归一化后作为 key，避免大小写/标点造成多份轮转
        sym_key = rules.base_symbol(symbol) or symbol

        filters = await self.store.get_filters()
        nodes = await self.store.all_nodes()
        nodemap = {n["node_id"]: n for n in nodes}

        # 合并轮转顺序：保留既有相对次序，剔除已不参与的节点，追加新参与节点到队尾
        participants = rules.poll_participant_ids(nodes, symbol, filters)
        order = self._reconcile_rotation(
            await self.store.get_poll_rotation(sym_key), participants,
        )

        consumer: Optional[str] = None
        for node_id in order:
            node = nodemap.get(node_id)
            # 队首节点离线/被禁用：顺延到下一个（保持其在轮转中的位置，不移到队尾）
            if not (node and node.get("enabled", True) and manager.is_node_online(node_id)):
                logger.info("poll rotation skip offline/disabled node %s", node_id)
                continue
            status = await self._run_with_retry(
                node, signal, signal_id, scope, global_lot, filters,
            )
            if status == "done":
                consumer = node_id
                break
            # skipped（被 9.2/9.3 过滤）/ failed（重试后仍失败）→ 顺延到下一个候选节点
            logger.info("poll rotation node %s -> %s, fall through", node_id, status)

        if consumer:
            # 领取成功：把消费节点移动到队尾，其余节点保持原相对顺序
            order = [nid for nid in order if nid != consumer] + [consumer]
            progress["status"] = "done"
            progress["consumer"] = consumer
            logger.info("poll rotation: signal %s consumed by %s", signal_id, consumer)
        else:
            progress["status"] = "unconsumed"
            logger.warning("poll rotation: signal %s not consumed by any node", signal_id)

        # 持久化轮转顺序（同时清理掉已失效的节点）与本条信号的最终状态
        await self.store.save_poll_rotation(sym_key, order)
        await self.store.save_poll_progress(signal_id, progress)

    @staticmethod
    def _reconcile_rotation(order: list[str], participants: list[str]) -> list[str]:
        """把持久化的轮转顺序与“当前参与节点”对齐：

        - 保留仍在参与的节点的既有相对顺序（维持轮转位置）；
        - 丢弃已不参与（删除 / 禁用 / 取消 follow_poll）的节点；
        - 追加新参与的节点到队尾（按 participants 的 poll_order 次序）。
        """
        participant_set = set(participants)
        merged = [nid for nid in order if nid in participant_set]
        seen = set(merged)
        for nid in participants:
            if nid not in seen:
                merged.append(nid)
                seen.add(nid)
        return merged

    async def _run_with_retry(self, node, signal, signal_id, scope, global_lot, filters) -> str:
        """对单节点执行开仓并等待回报，返回该节点的最终状态：

        - "done"：成功开仓（调用方据此判定该节点领取了本信号）；
        - "skipped"：被 9.2/9.3 过滤或并发锁拦截（无需重试，调用方顺延下一个节点）；
        - "failed"：失败/超时按指数退避重试 POLL_MAX_RETRY 次后仍未成交（调用方顺延）。
        """
        attempts = 0
        while attempts <= settings.poll_max_retry:
            res = await self.dispatcher.try_open(
                node, signal, signal_id, scope, filters,
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
