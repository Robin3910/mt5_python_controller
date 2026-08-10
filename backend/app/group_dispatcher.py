"""strategy 信号的分组分发引擎（model=strategy 专用，与 `dispatcher.py` 完全隔离）。

与 normal 链路（`dispatcher.py`）的差异：

- 分发单元是「分组」而不是「币种」：不读中控台 filters，不做币种准入、多区间方向
  过滤、持仓过滤，也不读节点的按币种配置；
- 只有「已启用 + 绑定了启用中策略 + 策略品种与信号品种一致」的分组才参与；信号带
  `group_ids` / `template_ids` 时再叠加定向：分别按分组 ID、按绑定的策略模版收窄；
- 每个入选分组独立处理同一条信号，分组的 sync / poll 模式由分组自身决定；
- 有效节点口径固定为「节点已启用 + 当前在线」；
- 明细写 group_task_dispatch（节点子任务），不写 normal 链路的 signal_dispatch。

执行单元是**节点子任务**：每个节点各自持有一个魔术号（由子任务自增号派生），
在 MT5 下单时写入，因此一个魔术号全局唯一地对应「哪个节点上的哪次执行」。
分组主任务（group_signal_task）只记录这条信号在该分组内的分发情况。

策略托管：开仓信号下发的是 `strategy_start`，携带首单参数与策略规则快照，
节点据此持续监控加仓，直到该魔术号的持仓全部平掉才算完成。

并发控制的粒度是「分组 + 节点」：同一分组内一个节点同时只允许一个策略任务
（Redis 占位 + 落库的子任务状态双保险）；不同分组各自独立，同一节点可以同时承接
多个分组的任务。CLOSE 信号是终止指令，绕过互斥直接结束相关子任务。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from . import (
    group_persist,
    group_rules,
    market_probe,
    persist,
    strategy_templates,
    system_settings,
    trend_indicators,
)
from .config import Config
from .connections import manager
from .models import SIGNAL_MODEL_STRATEGY
from .parser import TradingSignal
from .redis_store import RedisStore

logger = logging.getLogger(__name__)

# 写入 signal_history.dispatch_mode 的标识，用于把 strategy 信号与 sync/poll/manual 区分开
SIGNAL_DISPATCH_MODE = "group"


def build_strategy_start_command(
    signal_id: str, signal: TradingSignal, volume: float,
    task_id: int, group_id: str, strategy_snapshot: dict,
) -> dict:
    """策略托管开仓命令的公共部分：首单参数 + 策略规则快照。

    魔术号与子任务号按节点各不相同，下发前用 `for_node` 补齐。
    """
    return {
        "cmd": "strategy_start",
        "signal_id": signal_id,
        "task_id": task_id,
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


def for_node(command: dict, *, dispatch_id: int, magic: int) -> dict:
    """给公共命令补上该节点自己的子任务号与魔术号。"""
    return {**command, "dispatch_id": dispatch_id, "magic": magic}


def build_strategy_stop_command(
    signal_id: str, task_id: int, dispatch_id: int, magic: Optional[int],
    group_id: str, symbol: Optional[str], reason: str = "close_signal",
) -> dict:
    """策略终止命令：平掉该魔术号的全部持仓并结束节点侧监控。"""
    return {
        "cmd": "strategy_stop",
        "signal_id": signal_id,
        "task_id": task_id,
        "dispatch_id": dispatch_id,
        "magic": magic,
        "group_id": group_id,
        "symbol": symbol,
        "model": SIGNAL_MODEL_STRATEGY,
        "reason": reason,
    }


def build_strategy_resume_command(subtask: dict) -> dict:
    """策略恢复命令：节点重连后据此重建监控（不重复下首单）。"""
    cmd = {
        "cmd": "strategy_resume",
        "signal_id": subtask.get("signal_id"),
        "task_id": subtask.get("task_id"),
        "dispatch_id": subtask.get("dispatch_id"),
        "magic": subtask.get("magic"),
        "group_id": subtask.get("group_id"),
        "model": SIGNAL_MODEL_STRATEGY,
        "entry": {
            "action": subtask.get("action"),
            "symbol": subtask.get("symbol"),
            "volume": subtask.get("volume"),
        },
        "strategy": subtask.get("strategy_snapshot") or {},
        "report_interval": Config.STRATEGY_REPORT_INTERVAL,
    }
    runtime = subtask.get("runtime")
    if isinstance(runtime, dict) and runtime:
        cmd["runtime"] = runtime
    return cmd


class GroupDispatcher:
    def __init__(self, store: RedisStore) -> None:
        self.store = store

    # ------------------------------------------------------------------
    # 分组筛选
    # ------------------------------------------------------------------
    async def _candidate_groups(self, signal: TradingSignal) -> tuple[list[tuple[dict, dict]], list[str]]:
        """挑出该信号应当进入的分组。

        入选条件：命中信号的分组定向（group_ids，为空则不限制）+ 分组启用 +
        绑定了策略 + 策略启用 + 命中信号的策略模版定向（template_ids，为空则不限制）+
        策略品种与信号品种一致 +
        通过策略自身的开仓准入（如以损定量趋势单要求信号带止损价）。
        返回 (入选的 (group, strategy) 列表, 落选原因说明)。

        group_ids 是精确点名：没被点名的分组静默跳过，落选说明只讲被点名的那几个，
        否则一条只发给单个分组的信号会带回几十条无关的落选原因。
        """
        groups = sorted(
            await self.store.all_groups(),
            key=lambda g: (g.get("created_at", 0), g.get("group_id", "")),
        )
        matched: list[tuple[dict, dict]] = []
        reasons: list[str] = []
        if missing := group_rules.missing_group_ids(groups, signal.group_ids):
            reasons.append(f"指定的分组不存在：{'、'.join(missing)}")
        for group in groups:
            name = group.get("name") or group.get("group_id")
            if not group_rules.group_targeted(group, signal.group_ids):
                continue
            if not group.get("enabled", True):
                if signal.group_ids:
                    reasons.append(f"{name}：分组已禁用")
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
            off_target = group_rules.template_reject_reason(strategy, signal.template_ids)
            if off_target:
                reasons.append(f"{name}：{off_target}")
                continue
            if not group_rules.symbol_match(strategy.get("symbol"), signal.symbol):
                reasons.append(
                    f"{name}：策略品种 {strategy.get('symbol')} 与信号 {signal.symbol} 不符"
                )
                continue
            reject = group_rules.entry_reject_reason(
                strategy, signal.stop_loss, signal_action=signal.action,
            )
            if reject:
                reasons.append(f"{name}：{reject}")
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
        """CLOSE 信号：结束匹配的活动子任务，平掉各自魔术号的持仓。

        不建新任务、不受互斥限制——它本身就是用来解锁的。
        候选按「活动子任务 + 开仓快照」筛选，不复用开仓 `_candidate_groups`：
        分组/策略后来被禁用、或规则已改到通不过准入时，仍应能终止在跑任务。
        """
        overview = await group_persist.active_subtasks_overview()
        matched_subs, reasons = self._close_candidates(signal, overview)
        if not matched_subs:
            await persist.record_signal(
                signal_id, signal, source_ip, True, None, status="rejected",
                raw_payload=raw_payload, source=source, model=SIGNAL_MODEL_STRATEGY,
            )
            detail = "；".join(reasons) if reasons else "没有进行中的匹配子任务"
            return {"mode": "rejected", "targets": 0, "groups": 0,
                    "reason": f"无匹配任务：{detail}", "tasks": []}

        await persist.record_signal(
            signal_id, signal, source_ip, True, SIGNAL_DISPATCH_MODE,
            raw_payload=raw_payload, source=source, model=SIGNAL_MODEL_STRATEGY,
        )

        by_group: dict[str, list[dict]] = {}
        for sub in matched_subs:
            by_group.setdefault(str(sub.get("group_id") or ""), []).append(sub)

        results = []
        for group_id, subs in by_group.items():
            group = await self.store.get_group(group_id) or {
                "group_id": group_id, "name": group_id,
            }
            results.append(await self._close_group_subs(group, signal_id, subs))
        return {
            "mode": "group_close",
            "groups": len(results),
            "targets": sum(r.get("targets", 0) for r in results),
            "tasks": results,
        }

    @staticmethod
    def _close_candidates(
        signal: TradingSignal, overview: list[dict],
    ) -> tuple[list[dict], list[str]]:
        """CLOSE 候选：按 group_ids / template_ids（快照）/ 品种过滤活动子任务。"""
        reasons: list[str] = []
        if not overview:
            return [], ["当前没有进行中的策略子任务"]

        wanted_groups = {
            str(g).strip().lower() for g in (signal.group_ids or []) if str(g).strip()
        }
        if wanted_groups:
            present = {str(s.get("group_id") or "").strip().lower() for s in overview}
            missing = sorted(wanted_groups - present)
            if missing:
                reasons.append(f"指定的分组没有活动子任务：{'、'.join(missing)}")

        matched: list[dict] = []
        for sub in overview:
            gid = str(sub.get("group_id") or "").strip().lower()
            if wanted_groups and gid not in wanted_groups:
                continue
            if not group_rules.symbol_match(sub.get("symbol"), signal.symbol):
                continue
            # template_ids 对快照里的 template_id 做定向；空则不限制
            fake_strategy = {"template_id": sub.get("template_id")}
            off = group_rules.template_reject_reason(fake_strategy, signal.template_ids)
            if off:
                continue
            matched.append(sub)

        if not matched and not reasons:
            reasons.append(
                f"没有与品种 {signal.symbol} 匹配的活动子任务"
                + ("（含分组/模版定向）" if (signal.group_ids or signal.template_ids) else "")
            )
        return matched, reasons

    async def _close_group_subs(
        self, group: dict, signal_id: str, subtasks: list[dict],
    ) -> dict:
        """对同一分组下已筛选的子任务下发终止。"""
        group_id = group["group_id"]
        mode = group_rules.normalize_dispatch_mode(group.get("dispatch_mode"))
        if not subtasks:
            return self._result(group, mode, 0, "skipped", "分组没有进行中的任务")

        closing = 0
        forced = 0
        task_id = subtasks[0].get("task_id")
        for sub in subtasks:
            outcome = await self._stop_subtask(
                sub, signal_id=signal_id, reason="close_signal",
            )
            if outcome["status"] == "closing":
                closing += 1
            elif outcome["status"] == "forced":
                forced += 1

        if not closing:
            return self._result(group, mode, 0, "failed",
                                "目标节点均不在线，已强制结束子任务，待其重连后补发平仓",
                                task_id)
        logger.info(
            "group %s CLOSE -> %d subtask(s) closing, %d forced", group_id, closing, forced,
        )
        return self._result(group, mode, closing, "closing", None, task_id)

    async def _close_group(self, group: dict, signal: TradingSignal, signal_id: str) -> dict:
        """兼容旧调用：按分组拉活动子任务后终止（手动场景仍可用）。"""
        del signal  # 品种已在上层筛过；此处只按分组收口
        subtasks = await group_persist.active_subtasks(group["group_id"])
        return await self._close_group_subs(group, signal_id, subtasks)

    async def close_subtask(
        self, group_id: str, dispatch_id: int, *, reason: str = "manual_close",
    ) -> dict:
        """手动终止单个节点子任务：下发 strategy_stop，在线标 closing，离线强制收口并排队补发。"""
        sub = await group_persist.get_subtask(group_id, dispatch_id)
        if not sub:
            raise ValueError("子任务不存在或不属于该分组")
        if sub["status"] in group_rules.SUBTASK_TERMINAL:
            raise ValueError(f"子任务已结束（{sub['status']}），无需平仓")
        signal_id = sub.get("signal_id") or f"mclose_{dispatch_id}"
        outcome = await self._stop_subtask(sub, signal_id=signal_id, reason=reason)
        logger.info(
            "group %s dispatch #%s manual close -> %s (node %s)",
            group_id, dispatch_id, outcome["status"], sub["node_id"],
        )
        return outcome

    async def _stop_subtask(
        self, sub: dict, *, signal_id: str, reason: str,
    ) -> dict:
        """对单个子任务下发终止指令；返回 status=closing|forced。"""
        dispatch_id = sub["dispatch_id"]
        node_id = sub["node_id"]
        cmd = build_strategy_stop_command(
            signal_id, sub["task_id"], dispatch_id, sub["magic"],
            sub.get("group_id") or "", sub.get("symbol"),
            reason=reason,
        )
        if await manager.send_to_node(node_id, cmd):
            await group_persist.mark_dispatches_closing([dispatch_id])
            return {
                "status": "closing",
                "dispatch_id": dispatch_id,
                "node_id": node_id,
                "task_id": sub["task_id"],
                "magic": sub.get("magic"),
            }

        # 目标节点离线：强制收口并放开占位，终止指令入队等重连补发
        await self.store.push_pending_stop(node_id, cmd, Config.NODE_BUSY_TTL)
        res = await group_persist.force_finish_subtasks(
            [dispatch_id],
            reason=f"{reason}_node_offline"[:64],
        )
        await self._release(res)
        return {
            "status": "forced",
            "dispatch_id": dispatch_id,
            "node_id": node_id,
            "task_id": sub["task_id"],
            "magic": sub.get("magic"),
            "reason": "目标节点不在线，已强制结束子任务，待其重连后补发平仓",
        }

    # ------------------------------------------------------------------
    # 单分组开仓
    # ------------------------------------------------------------------
    async def _dispatch_group(
        self, group: dict, strategy: dict, signal: TradingSignal, signal_id: str,
        nodemap: dict[str, dict], online_ids: set[str],
        *, source_ip: Optional[str], raw_payload: Optional[str],
    ) -> dict:
        """单个分组的处理：建主任务 -> 选节点 -> 逐节点抢占位并下发。"""
        mode = group_rules.normalize_dispatch_mode(group.get("dispatch_mode"))
        effective = group_rules.effective_node_ids(group, nodemap, online_ids)

        snapshot = group_rules.strategy_rules_snapshot(strategy)
        task_id = await group_persist.create_task(
            signal_id=signal_id, group=group, signal=signal, dispatch_mode=mode,
            source_ip=source_ip, raw_payload=raw_payload, strategy=strategy,
            strategy_snapshot=snapshot,
        )
        if task_id is None:
            logger.warning("group %s task creation failed, skip dispatch",
                           group["group_id"])
            return self._result(group, mode, 0, "failed", "主任务创建失败，已放弃下发")

        skip_reason = group_rules.group_skip_reason(group, effective)
        if skip_reason:
            await group_persist.mark_task_skipped(task_id, skip_reason)
            logger.info("group %s skipped: %s", group["group_id"], skip_reason)
            return self._result(group, mode, 0, "skipped", skip_reason, task_id)

        command = build_strategy_start_command(
            signal_id, signal, group_rules.resolve_volume(signal.volume),
            task_id, group["group_id"], snapshot,
        )
        if mode == "poll":
            outcomes = await self._send_poll(group, signal, task_id, effective, command)
        else:
            outcomes = await self._send_sync(group, signal, task_id, effective, command)

        sent = [o for o in outcomes if o["status"] == "sent"]
        await group_persist.mark_task_dispatched(
            task_id, payload=command, node_ids=[o["node_id"] for o in sent],
        )
        for outcome in outcomes:
            await self._broadcast(group, signal, signal_id, task_id, outcome)

        if not sent:
            status, reason = self._no_target(outcomes)
            await group_persist.mark_task_skipped(task_id, reason)
            return self._result(group, mode, 0, status, reason, task_id)
        return self._result(group, mode, len(sent), "dispatching", None, task_id)

    @staticmethod
    def _no_target(outcomes: list[dict]) -> tuple[str, str]:
        """一个节点都没发出去时的 (状态, 原因)：区分节点忙与连接不可用。"""
        if outcomes and all(o["status"] == "skipped" for o in outcomes):
            return "skipped", "目标节点在本分组内均有进行中的任务"
        return "failed", "下发失败：目标节点连接均不可用"

    # ------------------------------------------------------------------
    # 逐节点下发
    # ------------------------------------------------------------------
    async def _send_sync(
        self, group: dict, signal: TradingSignal, task_id: int,
        effective: list[str], command: dict,
    ) -> list[dict]:
        """全员同步：分组内所有有效节点并发下发（各节点独立抢占位、独立魔术号）。"""
        results = await asyncio.gather(
            *[
                self._dispatch_node(group, signal, task_id, nid, command)
                for nid in effective
            ],
            return_exceptions=True,
        )
        outcomes: list[dict] = []
        for node_id, res in zip(effective, results):
            if isinstance(res, dict):
                outcomes.append(res)
                continue
            logger.warning("group %s node %s dispatch error: %s",
                           group["group_id"], node_id, res)
            outcomes.append(
                {"node_id": node_id, "status": "offline", "magic": None,
                 "volume": command["entry"]["volume"],
                 "reason": "下发失败：节点处理异常"}
            )
        return outcomes

    async def _send_poll(
        self, group: dict, signal: TradingSignal, task_id: int,
        effective: list[str], command: dict,
    ) -> list[dict]:
        """组内轮转：把任务交给队首第一个可下发的有效节点，成功后移到队尾。"""
        group_id = group["group_id"]
        order = group_rules.reconcile_rotation(
            await self.store.get_group_rotation(group_id), group_rules.member_ids(group),
        )
        eligible = set(effective)
        outcomes: list[dict] = []
        consumer: Optional[str] = None
        for node_id in order:
            if node_id not in eligible:
                continue
            outcome = await self._dispatch_node(group, signal, task_id, node_id, command)
            outcomes.append(outcome)
            if outcome["status"] == "sent":
                consumer = node_id
                break
            logger.info("group %s poll node %s unavailable (%s), fall through",
                        group_id, node_id, outcome.get("reason"))

        if consumer:
            # 领取成功的节点移到队尾，其余保持原相对顺序，实现循环轮转
            await self.store.save_group_rotation(
                group_id, [nid for nid in order if nid != consumer] + [consumer],
            )
            logger.info("group %s poll: task %s consumed by %s", group_id, task_id, consumer)
        return outcomes

    async def _dispatch_node(
        self, group: dict, signal: TradingSignal, task_id: int,
        node_id: str, command: dict,
    ) -> dict:
        """单节点下发：抢组内节点占位 -> 建子任务拿魔术号 -> 发命令。

        占位在「建子任务」之前抢、在「下发失败」时立即放开；下发成功后由子任务
        收口（节点上报 / 快照对账 / CLOSE）负责释放。
        """
        group_id = group["group_id"]
        volume = command["entry"]["volume"]
        base = {
            "task_id": task_id, "signal_id": command["signal_id"],
            "group_id": group_id, "node_id": node_id,
            "symbol": signal.symbol, "decided_vol": volume,
        }

        if not await self.store.acquire_group_node_busy(
            group_id, node_id, "pending", Config.NODE_BUSY_TTL,
        ):
            reason = await self._busy_reason(group_id, node_id)
            await group_persist.record_skipped_dispatch(
                **base, status="skipped", skip_reason=reason,
            )
            logger.info("group %s node %s busy: %s", group_id, node_id, reason)
            return {"node_id": node_id, "status": "skipped", "magic": None,
                    "volume": volume, "reason": reason}

        # 占位有 TTL 兜底，长期运行的子任务可能在占位过期后被再次抢到；
        # 以库里的真实状态复核，命中则续上占位并跳过，避免同一节点跑两个任务
        stale = await group_persist.active_subtask_id(group_id, node_id)
        if stale is not None:
            await self.store.set_group_node_busy(
                group_id, node_id, str(stale), Config.NODE_BUSY_TTL,
            )
            reason = f"该节点在本分组内已有进行中的子任务 #{stale}，本次跳过"
            await group_persist.record_skipped_dispatch(
                **base, status="skipped", skip_reason=reason,
            )
            logger.info("group %s node %s busy (db): %s", group_id, node_id, reason)
            return {"node_id": node_id, "status": "skipped", "magic": None,
                    "volume": volume, "reason": reason}

        # 趋势风控：占位已抢到、建子任务前，按该节点终端行情做顺势门禁
        if group.get("trend_risk_enabled"):
            blocked = await self._trend_risk_block(
                group_id, node_id, signal, base, volume,
            )
            if blocked is not None:
                return blocked

        try:
            return await self._dispatch_locked_node(base, command)
        except Exception as e:  # noqa: BLE001
            # 抢到占位后中途出错：立刻放开，否则要等 TTL 才能再下发
            await self.store.release_group_node_busy(group_id, node_id)
            logger.warning("group %s node %s dispatch failed: %s", group_id, node_id, e)
            return {"node_id": node_id, "status": "offline", "magic": None,
                    "volume": volume, "reason": "下发失败：节点处理异常"}

    async def _trend_risk_block(
        self,
        group_id: str,
        node_id: str,
        signal: TradingSignal,
        base: dict,
        volume: float,
    ) -> Optional[dict]:
        """开启趋势风控时：探针 + 判定；需拦截则释放占位并记 skipped，返回 outcome。

        放行时返回 None，由调用方继续建子任务下发。
        """
        reason: Optional[str] = None
        try:
            cfg = await system_settings.get_trend_config()
            market = await market_probe.fetch(
                node_id,
                str(signal.symbol or "").strip().upper(),
                str(cfg["timeframe"]),
                trend_indicators.bars_needed(cfg),
            )
            result = trend_indicators.evaluate(
                market.get("bars") or [],
                market_probe.quote_price(market.get("quote")),
                cfg,
            )
            reason = group_rules.trend_risk_reject_reason(
                signal.action,
                str(result.get("trend") or ""),
                ready=bool(result.get("ready")),
                score=result.get("score"),
            )
        except market_probe.ProbeError as e:
            reason = f"趋势风控：读取行情失败：{e}"
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "group %s node %s trend risk probe failed: %s", group_id, node_id, e,
            )
            reason = f"趋势风控：读取行情失败：{e}"

        if not reason:
            return None

        await self.store.release_group_node_busy(group_id, node_id)
        await group_persist.record_skipped_dispatch(
            **base, status="skipped", skip_reason=reason,
        )
        logger.info("group %s node %s trend risk skip: %s", group_id, node_id, reason)
        return {
            "node_id": node_id, "status": "skipped", "magic": None,
            "volume": volume, "reason": reason,
        }

    async def _dispatch_locked_node(self, base: dict, command: dict) -> dict:
        """已持有组内节点占位后的下发：建子任务拿魔术号 -> 发命令。"""
        node_id, group_id = base["node_id"], base["group_id"]
        volume = base["decided_vol"]
        # 网格等策略空仓是常态：建子任务时写入，对账热路径不必再解析策略快照
        strategy_snap = command.get("strategy") or {}
        hold_when_empty = strategy_templates.pick_grid_rule(
            strategy_snap.get("rules"),
        ) is not None
        created = await group_persist.create_dispatch(
            **base, hold_when_empty=hold_when_empty,
        )
        if created is None:
            await self.store.release_group_node_busy(group_id, node_id)
            reason = "子任务创建失败，已放弃该节点"
            logger.warning("group %s node %s: %s", group_id, node_id, reason)
            return {"node_id": node_id, "status": "offline", "magic": None,
                    "volume": volume, "reason": reason}

        dispatch_id, magic = created["dispatch_id"], created["magic"]
        await self.store.set_group_node_busy(
            group_id, node_id, str(dispatch_id), Config.NODE_BUSY_TTL,
        )
        cmd = for_node(command, dispatch_id=dispatch_id, magic=magic)
        if await manager.send_to_node(node_id, cmd):
            await group_persist.set_dispatch_status(dispatch_id, "sent")
            return {"node_id": node_id, "status": "sent", "magic": magic,
                    "dispatch_id": dispatch_id, "volume": volume, "reason": None}

        reason = "下发失败：节点连接已断开"
        await group_persist.set_dispatch_status(dispatch_id, "offline", skip_reason=reason)
        await self.store.release_group_node_busy(group_id, node_id)
        return {"node_id": node_id, "status": "offline", "magic": magic,
                "dispatch_id": dispatch_id, "volume": volume, "reason": reason}

    async def _busy_reason(self, group_id: str, node_id: str) -> str:
        marker = await self.store.get_group_node_busy(group_id, node_id)
        if marker and marker != "pending":
            return f"该节点在本分组内已有进行中的子任务 #{marker}，本次跳过"
        return "该节点在本分组内已有进行中的子任务，本次跳过"

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------
    async def _release(self, result: Optional[dict]) -> None:
        """释放持久化层回传的组内节点占位。"""
        for group_id, node_id in (result or {}).get("released", []):
            await self.store.release_group_node_busy(group_id, node_id)

    @staticmethod
    def _result(group: dict, mode: str, targets: int, status: str,
                reason: Optional[str] = None,
                task_id: Optional[int] = None) -> dict:
        return {
            "group_id": group["group_id"],
            "group_name": group.get("name"),
            "dispatch_mode": mode,
            "task_id": task_id,
            "targets": targets,
            "status": status,
            "reason": reason,
        }

    @staticmethod
    async def _broadcast(
        group: dict, signal: TradingSignal, signal_id: str,
        task_id: int, outcome: dict,
    ) -> None:
        """把单节点的下发结果推给后台实时展示。"""
        await manager.broadcast_admin({
            "type": "group_dispatch",
            "data": {
                "signal_id": signal_id,
                "task_id": task_id,
                "dispatch_id": outcome.get("dispatch_id"),
                "magic": outcome.get("magic"),
                "group_id": group["group_id"],
                "group_name": group.get("name"),
                "node_id": outcome["node_id"],
                "action": signal.action,
                "symbol": signal.symbol,
                "volume": outcome.get("volume"),
                "status": outcome["status"],
                "reason": outcome.get("reason"),
            },
        })
