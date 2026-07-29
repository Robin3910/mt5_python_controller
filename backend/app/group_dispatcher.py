"""strategy 信号的分组分发引擎（model=strategy 专用，与 `dispatcher.py` 完全隔离）。

与 normal 链路（`dispatcher.py`）的差异：

- 分发单元是「分组」而不是「币种」：不读中控台 filters，不做币种准入、多区间方向
  过滤、持仓过滤，也不读节点的按币种配置；
- 只有「已启用 + 绑定了启用中策略 + 策略品种与信号品种一致」的分组才参与；
- 每个入选分组独立处理同一条信号，分组的 sync / poll 模式由分组自身决定；
- 有效节点口径固定为「节点已启用 + 当前在线」；
- 下发前先为每个分组生成一条主任务（group_signal_task），任务号换算出的魔术号
  随命令下发，节点在 MT5 下单时写入该魔术号，从而能由成交单反查主任务；
- 明细写 group_task_dispatch（子节点信号任务），不写 normal 链路的 signal_dispatch。

策略托管：开仓信号下发的是 `strategy_start`，携带首单参数与策略规则快照，
节点据此持续监控加仓，直到该魔术号的持仓全部平掉才算完成。

并发控制：一个分组同时只允许存在一个进行中的主任务（Redis 占位 + DB 权威校验）。
CLOSE 信号是终止指令，绕过互斥直接结束分组当前任务。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from . import group_persist, group_rules, persist
from .config import Config
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


def build_strategy_start_command(
    signal_id: str, signal: TradingSignal, volume: float,
    task_id: int, magic: int, group_id: str, strategy_snapshot: dict,
) -> dict:
    """策略托管开仓命令：首单参数 + 策略规则快照，节点据此常驻监控加仓。"""
    return {
        "cmd": "strategy_start",
        "signal_id": signal_id,
        "task_id": task_id,
        "magic": magic,
        "group_id": group_id,
        "model": SIGNAL_MODEL_STRATEGY,
        "entry": {
            "action": signal.action,
            "symbol": signal.symbol,
            "volume": volume,
            "stop_loss": signal.stop_loss,
            "take_profit": signal.take_profit,
            "comment": signal.comment or "",
        },
        "strategy": strategy_snapshot,
        "report_interval": Config.STRATEGY_REPORT_INTERVAL,
    }


def build_strategy_stop_command(
    signal_id: str, task_id: int, magic: int, group_id: str,
    symbol: Optional[str], reason: str = "close_signal",
) -> dict:
    """策略终止命令：平掉该魔术号的全部持仓并结束节点侧监控。"""
    return {
        "cmd": "strategy_stop",
        "signal_id": signal_id,
        "task_id": task_id,
        "magic": magic,
        "group_id": group_id,
        "symbol": symbol,
        "model": SIGNAL_MODEL_STRATEGY,
        "reason": reason,
    }


def build_strategy_resume_command(task: dict) -> dict:
    """策略恢复命令：节点重连后据此重建监控（不重复下首单）。"""
    return {
        "cmd": "strategy_resume",
        "signal_id": task.get("signal_id"),
        "task_id": task.get("task_id"),
        "magic": task.get("magic"),
        "group_id": task.get("group_id"),
        "model": SIGNAL_MODEL_STRATEGY,
        "entry": {
            "action": task.get("action"),
            "symbol": task.get("symbol"),
            "volume": task.get("volume"),
        },
        "strategy": task.get("strategy_snapshot") or {},
        "report_interval": Config.STRATEGY_REPORT_INTERVAL,
    }


class GroupDispatcher:
    def __init__(self, store: RedisStore) -> None:
        self.store = store

    # ------------------------------------------------------------------
    # 分组筛选
    # ------------------------------------------------------------------
    async def _candidate_groups(self, signal: TradingSignal) -> tuple[list[tuple[dict, dict]], list[str]]:
        """挑出该信号应当进入的分组。

        入选条件：分组启用 + 绑定了策略 + 策略启用 + 策略品种与信号品种一致。
        返回 (入选的 (group, strategy) 列表, 落选原因说明)。
        """
        groups = sorted(
            await self.store.all_groups(),
            key=lambda g: (g.get("created_at", 0), g.get("group_id", "")),
        )
        matched: list[tuple[dict, dict]] = []
        reasons: list[str] = []
        for group in groups:
            name = group.get("name") or group.get("group_id")
            if not group.get("enabled", True):
                continue
            strategy_id = group.get("strategy_id")
            if not strategy_id:
                reasons.append(f"{name}：未绑定策略")
                continue
            strategy = await self.store.get_strategy(strategy_id)
            if not strategy:
                reasons.append(f"{name}：绑定的策略不存在（{strategy_id}）")
                continue
            if not strategy.get("enabled", True):
                reasons.append(f"{name}：绑定策略已禁用（{strategy.get('name')}）")
                continue
            if not group_rules.symbol_match(strategy.get("symbol"), signal.symbol):
                reasons.append(
                    f"{name}：策略品种 {strategy.get('symbol')} 与信号 {signal.symbol} 不符"
                )
                continue
            matched.append((group, strategy))
        return matched, reasons

    # ------------------------------------------------------------------
    # 入口
    # ------------------------------------------------------------------
    async def dispatch(self, signal: TradingSignal, signal_id: str,
                       source_ip: Optional[str] = None,
                       raw_payload: Optional[str] = None,
                       source: str = "tradingview") -> dict:
        """分发入口：按绑定策略的品种筛选分组，各自独立处理本信号。"""
        if signal.action == "CLOSE":
            return await self._dispatch_close(
                signal, signal_id, source_ip=source_ip, raw_payload=raw_payload, source=source,
            )

        matched, reasons = await self._candidate_groups(signal)
        if not matched:
            await persist.record_signal(
                signal_id, signal, source_ip, True, None, status="rejected",
                raw_payload=raw_payload, source=source, model=SIGNAL_MODEL_STRATEGY,
            )
            detail = "；".join(reasons) if reasons else "没有已启用且绑定该品种策略的分组"
            reason = f"无匹配分组：{detail}"
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
        for group, strategy in matched:
            tasks.append(
                await self._dispatch_group(
                    group, strategy, signal, signal_id, nodemap, online_ids,
                    source_ip=source_ip, raw_payload=raw_payload,
                )
            )
        return {
            "mode": "group",
            "groups": len(tasks),
            "targets": sum(t.get("targets", 0) for t in tasks),
            "tasks": tasks,
        }

    # ------------------------------------------------------------------
    # CLOSE：终止指令，绕过互斥
    # ------------------------------------------------------------------
    async def _dispatch_close(
        self, signal: TradingSignal, signal_id: str, *,
        source_ip: Optional[str], raw_payload: Optional[str], source: str,
    ) -> dict:
        """CLOSE 信号：结束匹配分组当前进行中的任务，平掉对应魔术号的持仓。

        不建新主任务、不受互斥锁限制——它本身就是用来解锁的。
        """
        matched, reasons = await self._candidate_groups(signal)
        if not matched:
            await persist.record_signal(
                signal_id, signal, source_ip, True, None, status="rejected",
                raw_payload=raw_payload, source=source, model=SIGNAL_MODEL_STRATEGY,
            )
            detail = "；".join(reasons) if reasons else "没有已启用且绑定该品种策略的分组"
            return {"mode": "rejected", "targets": 0, "groups": 0,
                    "reason": f"无匹配分组：{detail}", "tasks": []}

        await persist.record_signal(
            signal_id, signal, source_ip, True, SIGNAL_DISPATCH_MODE,
            raw_payload=raw_payload, source=source, model=SIGNAL_MODEL_STRATEGY,
        )

        results = []
        for group, _strategy in matched:
            results.append(await self._close_group(group, signal, signal_id))
        return {
            "mode": "group_close",
            "groups": len(results),
            "targets": sum(r.get("targets", 0) for r in results),
            "tasks": results,
        }

    async def _close_group(self, group: dict, signal: TradingSignal, signal_id: str) -> dict:
        group_id = group["group_id"]
        active = await group_persist.active_task(group_id)
        if not active:
            logger.info("group %s has no active task, CLOSE ignored", group_id)
            return self._result(group, group.get("dispatch_mode", "sync"), 0,
                                "skipped", "分组没有进行中的任务")

        task_id, magic = active["task_id"], active["magic"]
        node_ids = await group_persist.running_node_ids(task_id)
        cmd = build_strategy_stop_command(
            signal_id, task_id, magic, group_id, active.get("symbol") or signal.symbol,
        )
        sent = [nid for nid in node_ids if await manager.send_to_node(nid, cmd)]
        await group_persist.mark_subtasks_closing(task_id, sent)
        # 目标节点全部离线时无人能平仓，强制收口避免分组被永久锁死
        if not sent:
            await group_persist.force_finish_task(
                task_id, reason="close_signal_all_nodes_offline",
            )
            await self.store.release_group_busy(group_id)
            return self._result(group, active.get("dispatch_mode", "sync"), 0,
                                "failed", "目标节点均不在线，已强制结束任务", task_id, magic)
        logger.info("group %s CLOSE -> task %s, %d node(s)", group_id, task_id, len(sent))
        return self._result(group, active.get("dispatch_mode", "sync"), len(sent),
                            "closing", None, task_id, magic)

    # ------------------------------------------------------------------
    # 单分组开仓
    # ------------------------------------------------------------------
    async def _dispatch_group(
        self, group: dict, strategy: dict, signal: TradingSignal, signal_id: str,
        nodemap: dict[str, dict], online_ids: set[str],
        *, source_ip: Optional[str], raw_payload: Optional[str],
    ) -> dict:
        """单个分组的处理：抢互斥 -> 建主任务 -> 选节点 -> 下发策略 -> 落子任务。"""
        group_id = group["group_id"]
        mode = group_rules.normalize_dispatch_mode(group.get("dispatch_mode"))
        effective = group_rules.effective_node_ids(group, nodemap, online_ids)

        busy = await self._check_busy(group_id)
        if busy:
            logger.info("group %s busy: %s", group_id, busy)
            return self._result(group, mode, 0, "rejected", busy)

        snapshot = group_rules.strategy_rules_snapshot(strategy)
        task = await group_persist.create_task(
            signal_id=signal_id, group=group, signal=signal, dispatch_mode=mode,
            source_ip=source_ip, raw_payload=raw_payload, strategy=strategy,
            strategy_snapshot=snapshot,
        )
        if task is None:
            await self.store.release_group_busy(group_id)
            logger.warning("group %s task creation failed, skip dispatch", group_id)
            return self._result(group, mode, 0, "failed", "主任务创建失败，已放弃下发")

        task_id, magic = task["task_id"], task["magic"]
        await self.store.set_group_busy(group_id, str(task_id), Config.GROUP_BUSY_TTL)

        skip_reason = group_rules.group_skip_reason(group, effective)
        if skip_reason:
            await group_persist.mark_task_skipped(task_id, skip_reason)
            await self.store.release_group_busy(group_id)
            logger.info("group %s skipped: %s", group_id, skip_reason)
            return self._result(group, mode, 0, "skipped", skip_reason, task_id, magic)

        if mode == "poll":
            sent = await self._send_poll(
                group, signal, signal_id, task_id, magic, effective, snapshot,
            )
        else:
            sent = await self._send_sync(
                group, signal, signal_id, task_id, magic, effective, snapshot,
            )

        if not sent:
            reason = "下发失败：目标节点连接均不可用"
            await group_persist.mark_task_skipped(task_id, reason)
            await self.store.release_group_busy(group_id)
            return self._result(group, mode, 0, "failed", reason, task_id, magic)
        return self._result(group, mode, len(sent), "dispatching", None, task_id, magic)

    async def _check_busy(self, group_id: str) -> Optional[str]:
        """分组互斥：抢不到占位说明有任务在跑。DB 为权威，用于清理过期占位。"""
        got = await self.store.acquire_group_busy(
            group_id, "pending", Config.GROUP_BUSY_TTL,
        )
        if got:
            return None
        active = await group_persist.active_task(group_id)
        if active:
            return (
                f"分组存在进行中的任务 #{active['task_id']}"
                f"（{active.get('status')}），本次信号不接收"
            )
        # Redis 有占位但库里已收口（进程异常等），清掉后放行
        logger.info("group %s stale busy marker cleared", group_id)
        await self.store.release_group_busy(group_id)
        if await self.store.acquire_group_busy(group_id, "pending", Config.GROUP_BUSY_TTL):
            return None
        return "分组占位竞争失败，本次信号不接收"

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
        task_id: int, magic: int, effective: list[str], snapshot: dict,
    ) -> list[str]:
        """全员同步：分组内所有有效节点并发下发策略任务。"""
        volume = group_rules.resolve_volume(signal.volume)
        cmd = build_strategy_start_command(
            signal_id, signal, volume, task_id, magic, group["group_id"], snapshot,
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
        task_id: int, magic: int, effective: list[str], snapshot: dict,
    ) -> list[str]:
        """组内轮转：按轮转顺序把任务交给队首第一个可下发的有效节点，成功后移到队尾。"""
        group_id = group["group_id"]
        order = group_rules.reconcile_rotation(
            await self.store.get_group_rotation(group_id), group_rules.member_ids(group),
        )
        volume = group_rules.resolve_volume(signal.volume)
        cmd = build_strategy_start_command(
            signal_id, signal, volume, task_id, magic, group_id, snapshot,
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

    async def _record_nodes(
        self, group: dict, signal: TradingSignal, signal_id: str,
        task_id: int, magic: int, node_ids: list[str],
        volume: Optional[float], status: str, skip_reason: Optional[str] = None,
    ) -> None:
        """落库子任务并推送后台实时展示。"""
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
