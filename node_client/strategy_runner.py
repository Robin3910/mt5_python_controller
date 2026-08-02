"""策略托管执行器：一个分组任务一个实例，事件驱动，直到魔术号持仓全平。

职责：
1. 收到 strategy_start 后下首单（魔术号 = 服务端下发的任务魔术号）；
2. 订阅 MarketHub 事件，在持仓构成或价格变化时按策略规则触发逆势 / 顺势加仓；
3. 收到 GONE（已确认该魔术号持仓归零）即上报 strategy_finished，服务端据此收口；
4. strategy_stop 时平掉该魔术号全部持仓后结束。

执行器自己不读行情与持仓，也没有轮询循环——监控数据由 MarketHub 统一采样后以
事件送达（见 market_hub.py）；下单与平仓仍由执行器直接调用 MT5。

完成判定放在节点侧：只有节点能实时看到 MT5 持仓。服务端另有账户快照对账兜底，
所以这里即使漏报一次，最终也不会让分组永久卡住。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, Optional

from market_hub import GONE, STALE, MarketEvent, MarketHub, Subscription
from strategy_rules import (
    CALC_BAR_TYPES,
    MT5_COMMENT_LIMIT,
    PositionCtx,
    decision_comment,
    decision_detail,
    describe_decision,
    evaluate,
    metric_key,
)

# 事件说明落库字段为 VARCHAR(255)，本地先截断，避免整条上报被后端丢弃
MESSAGE_LIMIT = 255

logger = logging.getLogger("node.strategy")


def _as_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


class StrategyRunner:
    """单个分组任务在本节点上的执行体。"""

    def __init__(
        self,
        *,
        task_id: int,
        magic: int,
        group_id: str,
        signal_id: str,
        entry: dict,
        strategy: dict,
        mt5,
        hub: MarketHub,
        exec_fn: Callable,
        send_fn: Callable,
        report_interval: float = 5.0,
    ) -> None:
        self.task_id = int(task_id)
        self.magic = int(magic)
        self.group_id = group_id
        self.signal_id = signal_id
        self.entry = entry or {}
        self.strategy = strategy or {}
        self._mt5 = mt5
        self._hub = hub
        self._exec = exec_fn      # async (fn, *args) -> result，把阻塞 MT5 调用丢线程池
        self._send = send_fn      # async (dict) -> None，向服务端发消息
        self.report_interval = max(1.0, float(report_interval or 5.0))

        self.symbol = str(self.entry.get("symbol") or "")
        self.direction = str(self.entry.get("action") or "BUY").upper()
        self.base_volume = _as_float(self.entry.get("volume"))
        self.add_count = 0
        self.total_orders = 0
        self.total_volume = 0.0
        self._opened = False
        self._stopping = False
        self._stop_reason = "positions_cleared"
        self._finished = False
        self._seed_pending = False
        self._task: Optional[asyncio.Task] = None
        self._sub: Optional[Subscription] = None
        self._last_report = 0.0
        self._metric_specs: Optional[list[tuple[str, str]]] = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self, *, resume: bool = False) -> None:
        self._task = asyncio.create_task(self._run(resume=resume))

    def request_stop(self, reason: str = "stop_command") -> None:
        """请求终止；立刻唤醒事件等待，不必等下一轮采样。"""
        self._stopping = True
        self._stop_reason = reason
        if self._sub is not None:
            self._sub.wake()

    def cancel(self) -> None:
        """连接断开时只取消本地循环，不上报结束——持仓还在，等重连后恢复。"""
        if self._task and not self._task.done():
            self._task.cancel()

    @property
    def done(self) -> bool:
        return self._task is None or self._task.done()

    # ------------------------------------------------------------------
    # 事件循环
    # ------------------------------------------------------------------
    async def _run(self, *, resume: bool) -> None:
        sub = self._hub.subscribe(
            symbol=self.symbol, magic=self.magic, direction=self.direction,
        )
        self._sub = sub
        try:
            if not resume:
                if not await self._open_first():
                    return
            else:
                # 恢复场景：首单早已成交，按现有持仓续跑，计数待首个事件重建
                self._opened = True
                self._seed_pending = True
                await self._emit_progress("resume", phase="running")

            while True:
                if self._stopping:
                    await self._close_all()
                    return
                event = await sub.next_event()
                if event is None:
                    logger.info("task %s subscription closed, monitor exits", self.task_id)
                    return
                if self._stopping:
                    await self._close_all()
                    return
                if await self._on_event(event):
                    return
        except asyncio.CancelledError:
            logger.info("task %s monitor cancelled (will resume on reconnect)", self.task_id)
            raise
        except Exception as e:  # noqa: BLE001
            logger.exception("task %s runner crashed: %s", self.task_id, e)
            await self._finish("failed", f"runner_error: {e}")
        finally:
            self._hub.unsubscribe(sub)
            self._sub = None

    async def _on_event(self, event: MarketEvent) -> bool:
        """处理一个事件；返回 True 表示任务已收口。"""
        if event.kind == STALE:
            # 这一轮读不到持仓：绝不能据此判定已全平，等下次成功采样
            logger.debug("task %s positions unreadable, hold judgement", self.task_id)
            return False
        if event.kind == GONE:
            if not self._opened:
                return False
            await self._finish("done", self._stop_reason)
            return True

        positions = list(event.positions)
        if not positions:
            return False
        if self._seed_pending:
            self._seed_from_positions(positions)

        ctx = self._ctx(event, positions)
        ctx.bar_metrics = await self._read_bar_metrics()
        decision = evaluate(self.strategy.get("rules") or [], ctx)
        if decision is not None:
            await self._add_position(decision, positions)
            return False

        if time.time() - self._last_report >= self.report_interval:
            await self._emit_progress("heartbeat", phase="running", positions=positions)
        return False

    def _bar_metric_specs(self) -> list[tuple[str, str]]:
        """扫策略快照，收集需要读 K 线的 (指标, 周期) 组合。

        规则在任务期内不变，所以只解析一次；没有 ATR / 波幅档位时结果为空，
        整条 K 线读取链路都不会被触发。
        """
        if self._metric_specs is not None:
            return self._metric_specs
        specs: list[tuple[str, str]] = []
        for rule in self.strategy.get("rules") or []:
            if not isinstance(rule, dict) or not rule.get("batch_enabled"):
                continue
            for level in rule.get("batch_levels") or []:
                if not isinstance(level, dict):
                    continue
                calc_type = str(level.get("calc_type") or "").strip().lower()
                if calc_type not in CALC_BAR_TYPES:
                    continue
                spec = (calc_type, str(level.get("timeframe") or "").strip().upper())
                if spec[1] and spec not in specs:
                    specs.append(spec)
        self._metric_specs = specs
        return specs

    async def _read_bar_metrics(self) -> dict[str, float]:
        """判定前预取 ATR / 波幅。取值走 MarketHub 的按周期缓存，不会每轮都读 K 线。"""
        specs = self._bar_metric_specs()
        if not specs:
            return {}
        metrics: dict[str, float] = {}
        for metric, timeframe in specs:
            value = await self._hub.bar_metric(self.symbol, timeframe, metric)
            if value > 0:
                metrics[metric_key(metric, timeframe)] = value
        return metrics

    def _ctx(self, event: MarketEvent, positions: list[dict]) -> PositionCtx:
        """加仓判定上下文：偏离基准取最近一笔订单的开仓价。"""
        latest = max(positions, key=lambda p: (p.get("time") or 0, p.get("ticket") or 0))
        return PositionCtx(
            direction=self.direction,
            position_count=len(positions),
            base_volume=self.base_volume,
            base_price=_as_float(latest.get("price_open")),
            price=event.price,
            point=event.point,
            add_count=self.add_count,
        )

    def _seed_from_positions(self, positions: list[dict]) -> None:
        """恢复后按真实持仓重建计数。

        断线时节点内存里的计数已丢失，若从 0 重新计，非分批规则的 max_allow_num
        会失效而超额加仓。这里以当前持仓笔数反推：首单 1 笔，其余都是加仓。
        """
        self._seed_pending = False
        count = len(positions)
        if count <= 0:
            return
        self.add_count = max(self.add_count, count - 1)
        self.total_orders = max(self.total_orders, count)
        volume = round(sum(_as_float(p.get("volume")) for p in positions), 4)
        self.total_volume = max(self.total_volume, volume)
        logger.info(
            "task %s resumed with %d position(s): add_count=%s total_volume=%s",
            self.task_id, count, self.add_count, self.total_volume,
        )

    # ------------------------------------------------------------------
    # 交易动作
    # ------------------------------------------------------------------
    async def _open_first(self) -> bool:
        """下首单；失败直接结束子任务（服务端会据此收口）。"""
        res = await self._exec(
            self._mt5.place_market_order,
            self.symbol, self.direction, self.base_volume,
            self.entry.get("stop_loss"), self.entry.get("take_profit"),
            self._open_comment(), self.magic,
        )
        res = dict(res or {})
        res["signal_id"] = self.signal_id
        res["task_id"] = self.task_id
        res["magic"] = self.magic
        res.setdefault("symbol", self.symbol)
        await self._send({"type": "trade_result", "data": res})

        if not res.get("success"):
            logger.warning("task %s first order failed: %s", self.task_id, res.get("error"))
            return False

        self._opened = True
        self.total_orders += 1
        self.total_volume += self.base_volume
        await self._emit_progress(
            "open", phase="opened",
            last_order={
                "ticket": res.get("order") or res.get("ticket"),
                "price": res.get("price"),
                "volume": self.base_volume,
            },
            message=self._describe_open(),
            detail=self._open_detail(),
        )
        return True

    async def _add_position(self, decision, positions: list[dict]) -> None:
        reason = describe_decision(decision)
        res = await self._exec(
            self._mt5.place_market_order,
            self.symbol, decision.action, decision.volume,
            None, None, decision_comment(decision), self.magic,
        )
        res = dict(res or {})
        if not res.get("success"):
            logger.warning("task %s add failed: %s", self.task_id, res.get("error"))
            # 失败也带上判定依据，便于对照「本该按什么规则加仓」排查
            await self._emit_progress(
                "error", phase="running", positions=positions,
                message=f"加仓失败：{res.get('error') or 'add order failed'}；{reason}",
                detail={**decision_detail(decision), "error": str(res.get("error") or "")},
            )
            return
        self.add_count += 1
        self.total_orders += 1
        self.total_volume += decision.volume
        logger.info("task %s add #%s: %s", self.task_id, self.add_count, reason)
        await self._emit_progress(
            decision.event_type, phase="running",
            last_order={
                "ticket": res.get("order") or res.get("ticket"),
                "price": res.get("price"),
                "volume": decision.volume,
            },
            message=reason,
            detail=decision_detail(decision),
        )

    # ------------------------------------------------------------------
    # 首单的开单原因
    # ------------------------------------------------------------------
    def _strategy_name(self) -> str:
        return str(self.strategy.get("name") or self.strategy.get("strategy_id") or "未命名策略")

    def _open_comment(self) -> str:
        """首单的 MT5 备注：信号自带备注优先，为空时回落到可反查任务的编码。"""
        comment = str(self.entry.get("comment") or "").strip()
        return (comment or f"S{self.task_id}")[:MT5_COMMENT_LIMIT]

    def _describe_open(self) -> str:
        """首单的开单原因：说明这一单来自哪条信号、按哪个策略托管。"""
        parts = [
            f"策略信号首单：{self.direction} {self.symbol} {self.base_volume} 手",
            f"托管策略「{self._strategy_name()}」（{self._enabled_rule_count()} 条规则生效）",
        ]
        sl, tp = self.entry.get("stop_loss"), self.entry.get("take_profit")
        parts.append(f"止损 {sl if sl else '不设'}，止盈 {tp if tp else '不设'}")
        parts.append(f"信号 {self.signal_id or '—'}")
        return "；".join(parts)

    def _enabled_rule_count(self) -> int:
        return sum(
            1 for r in (self.strategy.get("rules") or [])
            if isinstance(r, dict) and str(r.get("status") or "0") not in ("0", "False")
        )

    def _open_detail(self) -> dict:
        return {
            "kind": "open",
            "signal_id": self.signal_id,
            "task_id": self.task_id,
            "magic": self.magic,
            "symbol": self.symbol,
            "action": self.direction,
            "volume": self.base_volume,
            "stop_loss": self.entry.get("stop_loss"),
            "take_profit": self.entry.get("take_profit"),
            "signal_comment": self.entry.get("comment") or None,
            "strategy_id": self.strategy.get("strategy_id"),
            "strategy_name": self.strategy.get("name"),
            "template_id": self.strategy.get("template_id"),
            "rule_count": len(self.strategy.get("rules") or []),
            "enabled_rule_count": self._enabled_rule_count(),
        }

    async def _close_all(self) -> None:
        """终止指令：平掉该魔术号的全部持仓后收口。"""
        res = await self._exec(self._mt5.close_by_magic, self.magic)
        res = dict(res or {})
        ok = bool(res.get("success", True))
        await self._finish(
            "done" if ok else "failed",
            self._stop_reason if ok else f"close_failed: {res.get('error')}",
        )

    # ------------------------------------------------------------------
    # 上报
    # ------------------------------------------------------------------
    def _snapshot(self, positions: Optional[list[dict]] = None) -> dict:
        pos = positions or []
        return {
            "position_count": len(pos),
            "add_count": self.add_count,
            "total_orders": self.total_orders,
            "total_volume": round(self.total_volume, 4),
            "profit": round(sum(_as_float(p.get("profit")) for p in pos), 2),
        }

    async def _emit_progress(
        self, event: str, *, phase: str,
        positions: Optional[list[dict]] = None,
        last_order: Optional[dict] = None,
        message: Optional[str] = None,
        detail: Optional[dict] = None,
    ) -> None:
        self._last_report = time.time()
        data = {
            "task_id": self.task_id,
            "magic": self.magic,
            "group_id": self.group_id,
            "signal_id": self.signal_id,
            "symbol": self.symbol,
            "action": self.direction,
            "event": event,
            "phase": phase,
            **self._snapshot(positions),
        }
        if last_order:
            data["last_order"] = last_order
        if message:
            data["message"] = message[:MESSAGE_LIMIT]
        if detail:
            data["detail"] = detail
        await self._send({"type": "strategy_progress", "data": data})

    async def _finish(self, status: str, reason: str) -> None:
        if self._finished:
            return
        self._finished = True
        logger.info("task %s finished: %s (%s)", self.task_id, status, reason)
        await self._send({
            "type": "strategy_finished",
            "data": {
                "task_id": self.task_id,
                "magic": self.magic,
                "group_id": self.group_id,
                "signal_id": self.signal_id,
                "symbol": self.symbol,
                "status": status,
                "reason": reason,
                "total_orders": self.total_orders,
                "total_volume": round(self.total_volume, 4),
                "realized_profit": 0.0,
            },
        })
