"""策略托管执行器：一个分组任务一个实例，常驻监控直到魔术号持仓全平。

职责：
1. 收到 strategy_start 后下首单（魔术号 = 服务端下发的任务魔术号）；
2. 循环监控该魔术号的持仓与行情，按策略规则触发逆势 / 顺势加仓；
3. 持仓归零即上报 strategy_finished，服务端据此收口子任务与主任务；
4. strategy_stop 时平掉该魔术号全部持仓后结束。

完成判定放在节点侧：只有节点能实时看到 MT5 持仓。服务端另有账户快照对账兜底，
所以这里即使漏报一次，最终也不会让分组永久卡住。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, Optional

from strategy_rules import PositionCtx, evaluate

logger = logging.getLogger("node.strategy")

# 监控轮询间隔（秒）：足够跟上加仓节奏，又不至于把 MT5 调用压满
POLL_INTERVAL = 1.0


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
        self._exec = exec_fn      # async (fn, *args) -> result，把阻塞 MT5 调用丢线程池
        self._send = send_fn      # async (dict) -> None，向服务端发消息
        self.report_interval = max(1.0, float(report_interval or 5.0))

        self.symbol = str(self.entry.get("symbol") or "")
        self.direction = str(self.entry.get("action") or "BUY").upper()
        self.base_volume = float(self.entry.get("volume") or 0.0)
        self.add_count = 0
        self.total_orders = 0
        self.total_volume = 0.0
        self._opened = False
        self._stopping = False
        self._stop_reason = "positions_cleared"
        self._task: Optional[asyncio.Task] = None
        self._last_report = 0.0

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self, mt5, *, resume: bool = False) -> None:
        self._task = asyncio.create_task(self._run(mt5, resume=resume))

    def request_stop(self, reason: str = "stop_command") -> None:
        self._stopping = True
        self._stop_reason = reason

    def cancel(self) -> None:
        """连接断开时只取消本地循环，不上报结束——持仓还在，等重连后恢复。"""
        if self._task and not self._task.done():
            self._task.cancel()

    @property
    def done(self) -> bool:
        return self._task is None or self._task.done()

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
    async def _run(self, mt5, *, resume: bool) -> None:
        try:
            if not resume:
                if not await self._open_first(mt5):
                    return
            else:
                # 恢复场景：首单早已成交，按现有持仓续跑
                self._opened = True
                await self._emit_progress("resume", phase="running")

            while True:
                if self._stopping:
                    await self._close_all(mt5)
                    return
                positions = await self._positions(mt5)
                if self._opened and not positions:
                    await self._finish("done", self._stop_reason)
                    return
                if positions:
                    await self._tick(mt5, positions)
                await asyncio.sleep(POLL_INTERVAL)
        except asyncio.CancelledError:
            logger.info("task %s monitor cancelled (will resume on reconnect)", self.task_id)
            raise
        except Exception as e:  # noqa: BLE001
            logger.exception("task %s runner crashed: %s", self.task_id, e)
            await self._finish("failed", f"runner_error: {e}")

    async def _open_first(self, mt5) -> bool:
        """下首单；失败直接结束子任务（服务端会据此收口）。"""
        res = await self._exec(
            mt5.place_market_order,
            self.symbol, self.direction, self.base_volume,
            self.entry.get("stop_loss"), self.entry.get("take_profit"),
            self.entry.get("comment") or "", self.magic,
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
        )
        return True

    async def _tick(self, mt5, positions: list[dict]) -> None:
        """一轮监控：判定是否加仓，并按间隔上报快照。"""
        price = await self._price(mt5, positions)
        point = await self._point(mt5)
        latest = max(positions, key=lambda p: (p.get("time") or 0, p.get("ticket") or 0))
        ctx = PositionCtx(
            direction=self.direction,
            position_count=len(positions),
            base_volume=self.base_volume,
            base_price=float(latest.get("price_open") or 0.0),
            price=price,
            point=point,
            add_count=self.add_count,
        )
        decision = evaluate(self.strategy.get("rules") or [], ctx)
        if decision is not None:
            await self._add_position(mt5, decision, positions)
            return

        now = time.time()
        if now - self._last_report >= self.report_interval:
            await self._emit_progress("heartbeat", phase="running", positions=positions)

    async def _add_position(self, mt5, decision, positions: list[dict]) -> None:
        res = await self._exec(
            mt5.place_market_order,
            self.symbol, decision.action, decision.volume,
            None, None, f"add#{self.add_count + 1}", self.magic,
        )
        res = dict(res or {})
        if not res.get("success"):
            logger.warning("task %s add failed: %s", self.task_id, res.get("error"))
            await self._emit_progress(
                "error", phase="running", positions=positions,
                message=str(res.get("error") or "add order failed")[:200],
            )
            return
        self.add_count += 1
        self.total_orders += 1
        self.total_volume += decision.volume
        logger.info(
            "task %s add #%s %s %.2f (dev %.1f >= %.1f)",
            self.task_id, self.add_count, decision.action,
            decision.volume, decision.deviation, decision.threshold,
        )
        await self._emit_progress(
            decision.event_type, phase="running",
            last_order={
                "ticket": res.get("order") or res.get("ticket"),
                "price": res.get("price"),
                "volume": decision.volume,
            },
            message=f"level={decision.level_index} dev={decision.deviation:.1f}",
        )

    async def _close_all(self, mt5) -> None:
        """终止指令：平掉该魔术号的全部持仓后收口。"""
        res = await self._exec(mt5.close_by_magic, self.magic)
        res = dict(res or {})
        ok = bool(res.get("success", True))
        await self._finish(
            "done" if ok else "failed",
            self._stop_reason if ok else f"close_failed: {res.get('error')}",
        )

    # ------------------------------------------------------------------
    # MT5 访问
    # ------------------------------------------------------------------
    async def _positions(self, mt5) -> list[dict]:
        try:
            return await self._exec(mt5.positions_by_magic, self.magic)
        except Exception as e:  # noqa: BLE001
            logger.debug("task %s read positions failed: %s", self.task_id, e)
            return []

    async def _price(self, mt5, positions: list[dict]) -> float:
        """取当前价：优先用行情，取不到时回落到持仓的 price_current。"""
        try:
            quotes = await self._exec(mt5.quotes, [self.symbol])
            q = quotes.get(self.symbol) or next(iter(quotes.values()), None)
            if q:
                # 多单看买价（平仓价），空单看卖价，与偏离方向口径一致
                return float(q["bid"] if self.direction == "BUY" else q["ask"])
        except Exception:  # noqa: BLE001
            pass
        latest = positions[-1] if positions else {}
        return float(latest.get("price_current") or latest.get("price_open") or 0.0)

    async def _point(self, mt5) -> float:
        try:
            return float(await self._exec(mt5.symbol_point, self.symbol))
        except Exception:  # noqa: BLE001
            return 0.0

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
            "profit": round(sum(float(p.get("profit") or 0.0) for p in pos), 2),
        }

    async def _emit_progress(
        self, event: str, *, phase: str,
        positions: Optional[list[dict]] = None,
        last_order: Optional[dict] = None,
        message: Optional[str] = None,
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
            data["message"] = message
        await self._send({"type": "strategy_progress", "data": data})

    async def _finish(self, status: str, reason: str) -> None:
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
