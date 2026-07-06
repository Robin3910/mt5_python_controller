"""信号分发引擎（文档第 9 章）：过滤 + 手数计算 + 全员同步广播 + 轮询入队。

分发模式：
- sync（全员同步）：所有符合条件的在线节点并发执行，不等待回报；
- poll（轮询领取）：信号入队，由 PollWorker 按顺序逐节点串行执行（见 poll_queue.py）。

CLOSE 信号特殊处理：不走过滤，直接广播给所有在线节点各自平掉对应品种。
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
from typing import Optional

from . import persist, results, rules
from .config import Config
from .connections import manager
from .models import build_close_command, build_open_command
from .parser import TradingSignal
from .redis_store import RedisStore
from .settings import settings

logger = logging.getLogger(__name__)


def signal_to_dict(signal: TradingSignal) -> dict:
    """TradingSignal -> dict（用于写入 Redis 的轮询进度，方便重启续跑）。"""
    return dataclasses.asdict(signal)


def dict_to_signal(d: dict) -> TradingSignal:
    """dict -> TradingSignal（轮询进度反序列化）。"""
    return TradingSignal(**d)


class Dispatcher:
    def __init__(self, store: RedisStore) -> None:
        self.store = store

    async def _eligible_nodes(
        self, follow: str, symbol: str, filters: dict, signal_id: str,
    ) -> list[dict]:
        """筛选“可参与本次分发”的节点：已启用 + 在线 + 已配按币种 + 参与对应分发模式。

        中控台已配但节点未配该品种时，对该节点拒收并写入节点信号日志（skipped）。
        """
        nodes = await self.store.all_nodes()
        out: list[dict] = []
        for n in nodes:
            if not n.get("enabled", True):
                continue
            if not manager.is_node_online(n["node_id"]):
                continue
            if not rules.node_has_symbol_config(n, symbol):
                await self._skip(
                    signal_id, n["node_id"], rules.node_symbol_not_configured_reason(symbol),
                )
                continue
            if not rules.node_participates(n, symbol, follow, filters):
                continue
            out.append(n)
        return out

    async def _online_enabled_nodes(self) -> list[dict]:
        """所有已启用且在线的节点（用于 CLOSE 广播）。"""
        nodes = await self.store.all_nodes()
        return [
            n for n in nodes
            if n.get("enabled", True) and manager.is_node_online(n["node_id"])
        ]

    async def dispatch(self, signal: TradingSignal, signal_id: str,
                       source_ip: Optional[str] = None) -> dict:
        """分发入口：按信号品种的分发配置选择 sync / poll，CLOSE 走专用广播。"""
        filters = await self.store.get_filters()
        mode, scope, reject_reason = rules.resolve_dispatch_config(signal.symbol, filters)
        if reject_reason:
            await persist.record_signal(signal_id, signal, source_ip, True, None, status="rejected")
            logger.info("signal rejected: %s", reject_reason)
            return {"mode": "rejected", "targets": 0, "reason": reject_reason}

        # 先落库一条信号历史（best-effort，不阻塞交易）
        await persist.record_signal(signal_id, signal, source_ip, True, mode)

        if signal.action == "CLOSE":
            n = await self._dispatch_close(signal, signal_id)
            return {"mode": "close", "targets": n}

        if mode == "poll":
            n = await self._enqueue_poll(signal, signal_id, scope, filters)
            return {"mode": "poll", "targets": n}

        n = await self._dispatch_sync(signal, signal_id, scope, filters)
        return {"mode": "sync", "targets": n}

    async def _dispatch_sync(
        self, signal: TradingSignal, signal_id: str, scope: str, filters: dict,
    ) -> int:
        """9.5 全员同步：对所有目标节点并发下发（fire-and-forget）。"""
        global_lot = await self.store.get_lot_global()
        targets = await self._eligible_nodes("sync", signal.symbol, filters, signal_id)
        # 并发执行；单个节点异常不影响其它节点
        await asyncio.gather(
            *[
                self.try_open(n, signal, signal_id, scope, global_lot, filters, wait=False)
                for n in targets
            ],
            return_exceptions=True,
        )
        return len(targets)

    async def _enqueue_poll(
        self, signal: TradingSignal, signal_id: str, scope: str, filters: dict,
    ) -> int:
        """9.6 轮询领取：固定节点顺序 + 进度写入 Redis，再把 signal_id 入队。"""
        targets = await self._eligible_nodes("poll", signal.symbol, filters, signal_id)
        # 排序规则：poll_order 小者优先，其次按创建时间
        targets.sort(
            key=lambda n: (rules.node_poll_order(n, signal.symbol), n.get("created_at", 0)),
        )
        progress = {
            "signal": signal_to_dict(signal),
            "signal_id": signal_id,
            "scope": scope,
            "nodes": [n["node_id"] for n in targets],
            "cursor": 0,  # 已处理到第几个节点（持久化，支持重启续跑）
            "status": {},
        }
        await self.store.save_poll_progress(signal_id, progress)
        await self.store.poll_enqueue(signal_id)
        logger.info("poll enqueued %s -> %d nodes", signal_id, len(targets))
        return len(targets)

    async def try_open(self, node: dict, signal: TradingSignal, signal_id: str,
                       scope: str, global_lot: dict, filters: dict,
                       wait: bool = False, timeout: int = 15) -> dict:
        """对单个节点执行“开仓”决策与下发。

        wait=False（同步模式）：发完即返回 "sent"，不等回报；
        wait=True （轮询模式）：注册 future 并等待节点回报或超时，返回最终状态。
        """
        node_id = node["node_id"]
        account = await self.store.get_account(node_id) or {}
        positions = account.get("positions", [])

        # 9.2 多区间方向过滤
        eff = rules.effective_filters(node, filters)
        price = rules.pick_price(account, signal.symbol)
        ok, reason = rules.interval_filter(signal.action, signal.symbol, price, eff)
        if not ok:
            await self._skip(signal_id, node_id, reason)
            return {"status": "skipped", "reason": reason}

        # 9.3 持仓过滤
        ok, reason = rules.position_gate(
            signal.action, signal.allow_position, positions, signal.symbol, scope
        )
        if not ok:
            await self._skip(signal_id, node_id, reason)
            return {"status": "skipped", "reason": reason}

        # 9.7 并发控制：同一 (节点, 品种) 同时只允许一笔在途订单
        if not await self.store.acquire_exec_lock(node_id, signal.symbol):
            reason = f"并发控制：{signal.symbol}已有在途订单未完成，跳过本次"
            await self._skip(signal_id, node_id, reason)
            return {"status": "skipped", "reason": reason}

        vol = rules.resolve_volume(node, signal.volume, global_lot, signal.symbol)
        cmd = build_open_command(
            signal_id, signal.action, signal.symbol, vol,
            signal.stop_loss, signal.take_profit, signal.comment,
            Config.DEFAULT_MAGIC_NUMBER,
        )

        # 轮询模式下先注册“等待回报”的 future
        fut = results.register(signal_id, node_id) if wait else None
        sent = await manager.send_to_node(node_id, cmd)
        await persist.record_dispatch(signal_id, node_id, vol, "passed", None, "pending")
        # 推送给后台前端做实时展示
        await manager.broadcast_admin(
            {"type": "dispatch", "data": {"signal_id": signal_id, "node_id": node_id,
                                          "action": signal.action, "symbol": signal.symbol,
                                          "volume": vol, "sl": signal.stop_loss,
                                          "tp": signal.take_profit,
                                          "status": "sent" if sent else "offline"}}
        )
        if not sent:
            # 发送失败（连接已断）：释放锁并标记失败
            await self.store.release_exec_lock(node_id, signal.symbol)
            if fut is not None:
                results.discard(signal_id, node_id)
            await persist.update_dispatch_result(signal_id, node_id, "failed", {"error": "offline"})
            return {"status": "failed", "reason": "offline"}

        if wait and fut is not None:
            # 轮询模式：等待节点回报或超时
            try:
                res = await asyncio.wait_for(fut, timeout=timeout)
                await self.store.release_exec_lock(node_id, signal.symbol)
                success = bool(res.get("success"))
                await persist.update_dispatch_result(
                    signal_id, node_id, "done" if success else "failed", res
                )
                return {"status": "done" if success else "failed", "result": res}
            except asyncio.TimeoutError:
                # 超时：丢弃 future、释放锁，交由上层决定是否重试
                results.discard(signal_id, node_id)
                await self.store.release_exec_lock(node_id, signal.symbol)
                await persist.update_dispatch_result(signal_id, node_id, "failed", {"error": "timeout"})
                return {"status": "timeout"}
        # 同步模式：发完即返回，回报由 ws_gateway 异步处理（锁靠 TTL 自动过期）
        return {"status": "sent"}

    async def _skip(self, signal_id: str, node_id: str, reason: Optional[str]) -> None:
        """记录一次“被过滤跳过”的分发，并推送到后台。"""
        await persist.record_dispatch(signal_id, node_id, None, "skipped", reason, "skipped")
        await manager.broadcast_admin(
            {"type": "dispatch", "data": {"signal_id": signal_id, "node_id": node_id,
                                          "status": "skipped", "reason": reason}}
        )

    async def _dispatch_close(self, signal: TradingSignal, signal_id: str) -> int:
        """CLOSE 信号：通知每个在线节点各自平掉该品种（不受分发模式开关限制）。"""
        targets = await self._online_enabled_nodes()
        cmd = build_close_command(signal_id, "symbol", signal.symbol)
        count = 0
        for node in targets:
            if await manager.send_to_node(node["node_id"], cmd):
                count += 1
        return count
