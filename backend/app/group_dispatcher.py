"""strategy 信号的分组分发引擎（model=strategy 专用，与 `dispatcher.py` 完全隔离）。

与 normal 链路（`dispatcher.py`）的差异：

- 分发单元是「分组」而不是「币种」：不读中控台 filters，不做币种准入、多区间方向
  过滤、持仓过滤，也不读节点的按币种配置；
- 每个启用的分组独立处理同一条信号，分组的 sync / poll 模式由分组自身决定；
- 有效节点口径固定为「节点已启用 + 当前在线」；
- 下发前先为每个分组生成一条主任务（group_signal_task），任务号换算出的魔术号
  随命令下发，节点在 MT5 下单时写入该魔术号，从而能由成交单反查主任务；
- 明细写 group_task_dispatch，不写 normal 链路的 signal_dispatch。

并发控制：分组链路不使用 normal 链路的 (节点, 品种) 执行锁，避免两条链路互相阻塞；
重复信号由 Webhook 层的去重窗口拦截。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from . import group_persist, group_rules, persist
from .connections import manager
from .models import SIGNAL_MODEL_STRATEGY, build_open_command
from .parser import TradingSignal
from .redis_store import RedisStore

logger = logging.getLogger(__name__)

# 写入 signal_history.dispatch_mode 的标识，用于把 strategy 信号与 sync/poll/manual 区分开
SIGNAL_DISPATCH_MODE = "group"


def build_group_open_command(
    signal_id: str, signal: TradingSignal, volume: float,
    task_id: int, magic: int, group_id: str,
) -> dict:
    """分组开仓命令：在标准开仓命令上附带主任务号与分组信息。"""
    cmd = build_open_command(
        signal_id, signal.action, signal.symbol, volume,
        signal.stop_loss, signal.take_profit, signal.comment, magic,
    )
    cmd.update({"model": SIGNAL_MODEL_STRATEGY, "task_id": task_id, "group_id": group_id})
    return cmd


def build_group_close_command(
    signal_id: str, symbol: str, task_id: int, magic: int, group_id: str,
) -> dict:
    """分组平仓命令：按品种平仓，并带上主任务号/魔术号供节点侧按需缩小范围。"""
    return {
        "cmd": "close",
        "signal_id": signal_id,
        "close_target": "symbol",
        "close_symbol": symbol,
        "close_ticket": None,
        "model": SIGNAL_MODEL_STRATEGY,
        "task_id": task_id,
        "magic": magic,
        "group_id": group_id,
    }


class GroupDispatcher:
    def __init__(self, store: RedisStore) -> None:
        self.store = store

    async def dispatch(self, signal: TradingSignal, signal_id: str,
                       source_ip: Optional[str] = None,
                       raw_payload: Optional[str] = None,
                       source: str = "tradingview") -> dict:
        """分发入口：所有启用的分组各自处理一次本信号。"""
        groups = sorted(
            (g for g in await self.store.all_groups() if g.get("enabled", True)),
            key=lambda g: (g.get("created_at", 0), g.get("group_id", "")),
        )
        if not groups:
            await persist.record_signal(
                signal_id, signal, source_ip, True, None, status="rejected",
                raw_payload=raw_payload, source=source, model=SIGNAL_MODEL_STRATEGY,
            )
            reason = "分组未配置：没有已启用的分组，strategy 信号拒收"
            logger.info("strategy signal rejected: %s", reason)
            return {"mode": "rejected", "targets": 0, "groups": 0, "reason": reason, "tasks": []}

        await persist.record_signal(
            signal_id, signal, source_ip, True, SIGNAL_DISPATCH_MODE,
            raw_payload=raw_payload, source=source, model=SIGNAL_MODEL_STRATEGY,
        )

        nodes = await self.store.all_nodes()
        nodemap = {n["node_id"]: n for n in nodes}
        online_ids = set(manager.online_node_ids())

        tasks = []
        for group in groups:
            tasks.append(
                await self._dispatch_group(
                    group, signal, signal_id, nodemap, online_ids,
                    source_ip=source_ip, raw_payload=raw_payload,
                )
            )
        return {
            "mode": "group",
            "groups": len(tasks),
            "targets": sum(t.get("targets", 0) for t in tasks),
            "tasks": tasks,
        }

    async def _dispatch_group(
        self, group: dict, signal: TradingSignal, signal_id: str,
        nodemap: dict[str, dict], online_ids: set[str],
        *, source_ip: Optional[str], raw_payload: Optional[str],
    ) -> dict:
        """单个分组的处理：建主任务 -> 选节点 -> 下发 -> 落明细。"""
        group_id = group["group_id"]
        mode = group_rules.normalize_dispatch_mode(group.get("dispatch_mode"))
        effective = group_rules.effective_node_ids(group, nodemap, online_ids)

        # 下发节点之前先生成主任务，任务号即魔术号来源
        task = await group_persist.create_task(
            signal_id=signal_id, group=group, signal=signal, dispatch_mode=mode,
            source_ip=source_ip, raw_payload=raw_payload,
        )
        if task is None:
            logger.warning("group %s task creation failed, skip dispatch", group_id)
            return self._result(group, mode, 0, "failed", "主任务创建失败，已放弃下发")

        task_id, magic = task["task_id"], task["magic"]

        skip_reason = group_rules.group_skip_reason(group, effective)
        if skip_reason:
            await group_persist.mark_task_skipped(task_id, skip_reason)
            logger.info("group %s skipped: %s", group_id, skip_reason)
            return self._result(group, mode, 0, "skipped", skip_reason, task_id, magic)

        if signal.action == "CLOSE":
            sent = await self._send_close(
                group, signal, signal_id, task_id, magic, effective,
            )
        elif mode == "poll":
            sent = await self._send_poll(
                group, signal, signal_id, task_id, magic, effective,
            )
        else:
            sent = await self._send_sync(
                group, signal, signal_id, task_id, magic, effective,
            )

        if not sent:
            reason = "下发失败：目标节点连接均不可用"
            await group_persist.mark_task_skipped(task_id, reason)
            return self._result(group, mode, 0, "failed", reason, task_id, magic)
        return self._result(group, mode, len(sent), "dispatching", None, task_id, magic)

    @staticmethod
    def _result(group: dict, mode: str, targets: int, status: str,
                reason: Optional[str] = None,
                task_id: Optional[int] = None, magic: Optional[int] = None) -> dict:
        return {
            "group_id": group["group_id"],
            "group_name": group.get("name"),
            "dispatch_mode": mode,
            "task_id": task_id,
            "magic": magic,
            "targets": targets,
            "status": status,
            "reason": reason,
        }

    async def _send_sync(
        self, group: dict, signal: TradingSignal, signal_id: str,
        task_id: int, magic: int, effective: list[str],
    ) -> list[str]:
        """全员同步：分组内所有有效节点并发下发（fire-and-forget）。"""
        volume = group_rules.resolve_volume(signal.volume)
        cmd = build_group_open_command(
            signal_id, signal, volume, task_id, magic, group["group_id"],
        )
        results = await asyncio.gather(
            *[manager.send_to_node(nid, cmd) for nid in effective],
            return_exceptions=True,
        )
        sent = [nid for nid, ok in zip(effective, results) if ok is True]
        failed = [nid for nid in effective if nid not in sent]
        await group_persist.mark_task_dispatched(task_id, payload=cmd, node_ids=sent)
        await self._record_nodes(
            group, signal, signal_id, task_id, magic, sent, volume, "sent",
        )
        await self._record_nodes(
            group, signal, signal_id, task_id, magic, failed, volume, "offline",
            skip_reason="下发失败：节点连接已断开",
        )
        return sent

    async def _send_poll(
        self, group: dict, signal: TradingSignal, signal_id: str,
        task_id: int, magic: int, effective: list[str],
    ) -> list[str]:
        """组内轮转：按轮转顺序把信号交给队首第一个可下发的有效节点，成功后移到队尾。"""
        group_id = group["group_id"]
        order = group_rules.reconcile_rotation(
            await self.store.get_group_rotation(group_id), group_rules.member_ids(group),
        )
        volume = group_rules.resolve_volume(signal.volume)
        cmd = build_group_open_command(
            signal_id, signal, volume, task_id, magic, group_id,
        )

        eligible = set(effective)
        consumer: Optional[str] = None
        offline: list[str] = []
        for node_id in order:
            if node_id not in eligible:
                continue
            if await manager.send_to_node(node_id, cmd):
                consumer = node_id
                break
            offline.append(node_id)
            logger.info("group %s poll node %s unreachable, fall through", group_id, node_id)

        if offline:
            await self._record_nodes(
                group, signal, signal_id, task_id, magic, offline, volume, "offline",
                skip_reason="下发失败：节点连接已断开",
            )
        if not consumer:
            return []

        await group_persist.mark_task_dispatched(task_id, payload=cmd, node_ids=[consumer])
        await self._record_nodes(
            group, signal, signal_id, task_id, magic, [consumer], volume, "sent",
        )
        # 领取成功的节点移到队尾，其余保持原相对顺序，实现循环轮转
        await self.store.save_group_rotation(
            group_id, [nid for nid in order if nid != consumer] + [consumer],
        )
        logger.info("group %s poll: task %s consumed by %s", group_id, task_id, consumer)
        return [consumer]

    async def _send_close(
        self, group: dict, signal: TradingSignal, signal_id: str,
        task_id: int, magic: int, effective: list[str],
    ) -> list[str]:
        """CLOSE：不受分发模式影响，通知分组内所有有效节点平掉该品种。"""
        cmd = build_group_close_command(
            signal_id, signal.symbol, task_id, magic, group["group_id"],
        )
        sent = [nid for nid in effective if await manager.send_to_node(nid, cmd)]
        failed = [nid for nid in effective if nid not in sent]
        await group_persist.mark_task_dispatched(task_id, payload=cmd, node_ids=sent)
        await self._record_nodes(group, signal, signal_id, task_id, magic, sent, None, "sent")
        await self._record_nodes(
            group, signal, signal_id, task_id, magic, failed, None, "offline",
            skip_reason="下发失败：节点连接已断开",
        )
        return sent

    async def _record_nodes(
        self, group: dict, signal: TradingSignal, signal_id: str,
        task_id: int, magic: int, node_ids: list[str],
        volume: Optional[float], status: str, skip_reason: Optional[str] = None,
    ) -> None:
        """落库明细并推送后台实时展示。"""
        for node_id in node_ids:
            await group_persist.record_dispatch(
                task_id=task_id, signal_id=signal_id, group_id=group["group_id"],
                node_id=node_id, magic=magic, decided_vol=volume,
                status=status, skip_reason=skip_reason,
            )
            await manager.broadcast_admin({
                "type": "group_dispatch",
                "data": {
                    "signal_id": signal_id,
                    "task_id": task_id,
                    "magic": magic,
                    "group_id": group["group_id"],
                    "group_name": group.get("name"),
                    "node_id": node_id,
                    "action": signal.action,
                    "symbol": signal.symbol,
                    "volume": volume,
                    "status": status,
                    "reason": skip_reason,
                },
            })
