"""策略托管执行器：一个分组任务一个实例，事件驱动，直到魔术号持仓全平。

职责：
1. 收到 strategy_start 后下首单（魔术号 = 服务端下发的任务魔术号）；
2. 订阅 MarketHub 事件，在持仓构成或价格变化时按策略规则推进仓位；
3. 收到 GONE（已确认该魔术号持仓归零）即上报 strategy_finished，服务端据此收口；
4. strategy_stop 时平掉该魔术号全部持仓后结束。

按策略快照里的规则走三条互斥的执行路径：

- 加仓路径（模版1）：首单手数用信号手数，之后按逆势 / 顺势规则加仓，判定见
  `strategy_rules.evaluate`；
- 以损定量路径（模版2）：手数由「风险金额 ÷ 止损距离」反推，底仓市价成交后按间距
  分批补齐，可选浮盈达标后移动止损保本，计算见 `risk_sizing`；
- 网格路径（模版3）：区间内逐格买卖的手动网格，空仓是正常运行态，
  只由止损 / 止盈 / strategy_stop 收口，计算见 `grid_trading`。

执行器自己不读行情与持仓，也没有轮询循环——监控数据由 MarketHub 统一采样后以
事件送达（见 market_hub.py）；下单与平仓仍由执行器直接调用 MT5。

完成判定放在节点侧：只有节点能实时看到 MT5 持仓。服务端另有账户快照对账兜底，
所以这里即使漏报一次，最终也不会让分组永久卡住。网格任务因 hold_when_empty
豁免「空仓即收口」。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, Optional

import grid_trading
import risk_sizing
from market_hub import GONE, STALE, MarketEvent, MarketHub, Subscription
from risk_sizing import EntryPlan, RiskSizedConfig, SymbolSpec
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

# 执行路径
_MODE_ADD_ON = "add_on"
_MODE_RISK_SIZED = "risk_sized"
_MODE_GRID = "grid"

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

        # 三条互斥执行路径：网格 > 以损定量 > 加仓（按规则 type 优先级）
        grid_rule = grid_trading.pick_grid_rule(self.strategy.get("rules"))
        risk_rule = (
            None if grid_rule
            else risk_sizing.pick_risk_sized_rule(self.strategy.get("rules"))
        )
        if grid_rule:
            self._mode = _MODE_GRID
            self._grid_cfg: Optional[grid_trading.GridConfig] = (
                grid_trading.GridConfig.from_rule(grid_rule)
            )
            self.direction = grid_trading.resolve_side(self._grid_cfg, self.direction)
        else:
            self._grid_cfg = None
            self._mode = _MODE_RISK_SIZED if risk_rule else _MODE_ADD_ON

        self._risk_cfg: Optional[RiskSizedConfig] = (
            RiskSizedConfig.from_rule(risk_rule) if risk_rule else None
        )
        self._risk_plan: Optional[EntryPlan] = None
        self._risk_spec: Optional[SymbolSpec] = None
        self._breakeven_done = False

        self._grid_plan: Optional[grid_trading.GridPlan] = None
        self._grid_spec: Optional[grid_trading.SymbolSpec] = None
        self._grid_prev_price: float = 0.0
        # 平移后越界、但当时没平成的持仓：脱离了格位映射，只能逐轮重试兑现
        self._grid_orphans: set[int] = set()

    @property
    def risk_sized(self) -> bool:
        """本任务是否走以损定量路径。"""
        return self._mode == _MODE_RISK_SIZED

    @property
    def is_grid(self) -> bool:
        """本任务是否走网格路径。"""
        return self._mode == _MODE_GRID

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
            hold_when_empty=self.is_grid,
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
            if self.is_grid:
                # 网格空仓是常态；hub 已不会发 GONE，这里做双重保险
                return False
            if not self._opened:
                return False
            await self._finish("done", self._stop_reason)
            return True

        positions = list(event.positions)
        if self._seed_pending:
            self._seed_from_positions(positions)
            if self.risk_sized:
                await self._risk_rebuild_plan(positions, event.point)
            elif self.is_grid:
                await self._grid_rebuild(positions)

        if self.is_grid:
            if await self._grid_advance(event, positions):
                return True
        elif not positions:
            return False
        elif self.risk_sized:
            if await self._risk_advance(event, positions):
                return False
        else:
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
        """下首单；失败直接结束子任务（服务端会据此收口）。

        以损定量路径先反推手数，因此首单手数、止盈都与信号自带的值无关。
        网格路径可能先等触发价，再按现价上方格位初始建仓。
        """
        if self.is_grid:
            return await self._grid_open_first()
        if self.risk_sized:
            return await self._risk_open_base()
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
    # 以损定量路径（模版2）
    # ------------------------------------------------------------------
    async def _risk_symbol_spec(self) -> SymbolSpec:
        """读一次品种规格并缓存——任务期内合约规格不会变。"""
        if self._risk_spec is None:
            raw = dict(await self._exec(self._mt5.symbol_spec, self.symbol) or {})
            self._risk_spec = SymbolSpec(
                point=_as_float(raw.get("point")),
                digits=int(raw.get("digits") or 5),
                tick_size=_as_float(raw.get("tick_size")),
                tick_value=_as_float(raw.get("tick_value")),
                volume_min=_as_float(raw.get("volume_min")),
                volume_step=_as_float(raw.get("volume_step")),
                volume_max=_as_float(raw.get("volume_max")),
            )
        return self._risk_spec

    async def _risk_entry_quote(self) -> float:
        """下底仓前的预估开仓价：多单取 ask、空单取 bid。

        手数必须在下单前算出来，所以只能用当前报价预估；成交后再用真实成交价把
        止盈与补仓触发价重挂一次（见 risk_sizing.anchor_to_fill）。
        """
        quotes = dict(await self._exec(self._mt5.quotes, [self.symbol]) or {})
        quote = quotes.get(self.symbol) or next(iter(quotes.values()), {})
        key = "ask" if self.direction == "BUY" else "bid"
        return _as_float(quote.get(key)) or _as_float(quote.get("mid"))

    async def _risk_open_base(self) -> bool:
        """以损定量首单：反推总手数后按底仓比例市价成交。"""
        cfg = self._risk_cfg
        assert cfg is not None
        spec = await self._risk_symbol_spec()
        plan = risk_sizing.plan_entries(
            cfg,
            direction=self.direction,
            entry_price=await self._risk_entry_quote(),
            stop_loss=_as_float(self.entry.get("stop_loss")),
            spec=spec,
        )
        if not plan.ok:
            logger.warning("task %s risk sizing rejected: %s", self.task_id, plan.reject)
            await self._emit_progress(
                "error", phase="failed",
                message=f"以损定量无法开仓：{plan.reject}",
                detail={"kind": "risk_sized_reject", "reason": plan.reject},
            )
            await self._finish("failed", f"risk_sizing_rejected: {plan.reject}")
            return False

        self._risk_plan = plan
        self.base_volume = plan.base_volume
        planned_tp = plan.take_profit
        res = dict(await self._exec(
            self._mt5.place_market_order,
            self.symbol, self.direction, plan.base_volume,
            plan.stop_loss, planned_tp or None,
            self._open_comment(), self.magic,
        ) or {})
        res["signal_id"] = self.signal_id
        res["task_id"] = self.task_id
        res["magic"] = self.magic
        res.setdefault("symbol", self.symbol)
        await self._send({"type": "trade_result", "data": res})

        if not res.get("success"):
            logger.warning("task %s base order failed: %s", self.task_id, res.get("error"))
            return False

        risk_sizing.anchor_to_fill(plan, cfg, _as_float(res.get("price")), spec)
        await self._risk_fix_base_target(res, plan, planned_tp)
        self._opened = True
        self.total_orders += 1
        self.total_volume += plan.base_volume
        await self._emit_progress(
            "open", phase="opened",
            last_order={
                "ticket": res.get("order") or res.get("ticket"),
                "price": res.get("price"),
                "volume": plan.base_volume,
            },
            message=self._risk_describe_open(plan, cfg, spec),
            detail={
                **self._open_detail(),
                **risk_sizing.plan_detail(plan, cfg, spec),
                "kind": "open",
            },
        )
        return True

    async def _risk_fix_base_target(self, res: dict, plan: EntryPlan,
                                    planned_tp: float) -> None:
        """底仓成交后把止盈校正到按真实成交价算出的价位。

        下单时只能用预估价算止盈——带着它下单是为了万一后续改单失败也有保护。滑点会
        让这个值偏掉，而补仓单用的是重挂后的止盈：不校正的话同一笔交易里会出现两个
        止盈价，盈亏比也不再是配置值。
        """
        if not plan.take_profit or plan.take_profit == planned_tp:
            return
        ticket = res.get("order") or res.get("ticket")
        if not ticket:
            return
        fixed = dict(await self._exec(
            self._mt5.modify_position_sl, int(ticket), plan.stop_loss, plan.take_profit,
        ) or {})
        if not fixed.get("success"):
            logger.warning(
                "task %s base take-profit not corrected (%s -> %s): %s",
                self.task_id, planned_tp, plan.take_profit, fixed.get("error"),
            )

    async def _risk_rebuild_plan(self, positions: list[dict], point: float) -> None:
        """恢复后按真实持仓重建建仓计划。

        断线时计划已随进程丢失，而持仓上还挂着当初写进 MT5 的止损：用最早一笔的
        开仓价与止损价就能把计划还原出来。若止损已被保本移动过（不在不利侧），
        plan_entries 会拒绝重建，此时降级为只监控到全平，不再补仓也不再动止损。
        """
        cfg = self._risk_cfg
        if cfg is None or not positions:
            return
        earliest = min(positions, key=lambda p: (p.get("time") or 0, p.get("ticket") or 0))
        spec = await self._risk_symbol_spec()
        plan = risk_sizing.plan_entries(
            cfg,
            direction=self.direction,
            entry_price=_as_float(earliest.get("price_open")),
            stop_loss=_as_float(earliest.get("sl")),
            spec=spec,
        )
        if not plan.ok:
            logger.info(
                "task %s resume without risk plan (%s): monitor only",
                self.task_id, plan.reject,
            )
            self._breakeven_done = True
            return
        self._risk_plan = plan
        self.base_volume = plan.base_volume

    async def _risk_advance(self, event: MarketEvent, positions: list[dict]) -> bool:
        """推进以损定量任务：先补仓，再看保本。返回 True 表示本轮已有动作。"""
        cfg, plan = self._risk_cfg, self._risk_plan
        if cfg is None or plan is None or not plan.ok:
            return False
        batch = risk_sizing.next_batch(plan, cfg, filled=self.total_orders, price=event.price)
        if batch is not None:
            await self._risk_add_batch(batch, plan, cfg, event.price, positions)
            return True
        if self._breakeven_done:
            return False
        move = risk_sizing.breakeven_move(
            cfg, plan, positions=positions, price=event.price,
            digits=(self._risk_spec.digits if self._risk_spec else 5),
        )
        if move is not None:
            await self._risk_move_breakeven(move, positions)
            return True
        return False

    async def _risk_add_batch(self, batch, plan: EntryPlan, cfg: RiskSizedConfig,
                              price: float, positions: list[dict]) -> None:
        """补进一批仓位；沿用同一组止损止盈，总风险因此保持不变。"""
        spec = await self._risk_symbol_spec()
        reason = risk_sizing.describe_batch(plan, cfg, batch, spec, price)
        res = dict(await self._exec(
            self._mt5.place_market_order,
            self.symbol, self.direction, batch.volume,
            plan.stop_loss, plan.take_profit or None,
            risk_sizing.batch_comment(batch), self.magic,
        ) or {})
        detail = risk_sizing.batch_detail(plan, cfg, batch, spec, price)
        if not res.get("success"):
            logger.warning("task %s batch %s failed: %s", self.task_id, batch.index, res.get("error"))
            await self._emit_progress(
                "error", phase="running", positions=positions,
                message=f"补仓失败：{res.get('error') or 'batch order failed'}；{reason}",
                detail={**detail, "error": str(res.get("error") or "")},
            )
            return
        self.add_count += 1
        self.total_orders += 1
        self.total_volume += batch.volume
        logger.info("task %s batch %s filled: %s", self.task_id, batch.index, reason)
        await self._emit_progress(
            "add_trend", phase="running",
            last_order={
                "ticket": res.get("order") or res.get("ticket"),
                "price": res.get("price"),
                "volume": batch.volume,
            },
            message=reason,
            detail=detail,
        )

    async def _risk_move_breakeven(self, move, positions: list[dict]) -> None:
        """浮盈达标：把该魔术号全部持仓的止损挪到加权均价。"""
        digits = self._risk_spec.digits if self._risk_spec else 5
        reason = move.describe(digits)
        res = dict(await self._exec(
            self._mt5.modify_sl_by_magic, self.magic, move.stop_loss,
        ) or {})
        if not res.get("success"):
            logger.warning("task %s breakeven failed: %s", self.task_id, res)
            await self._emit_progress(
                "error", phase="running", positions=positions,
                message=f"保本止损设置失败；{reason}",
                detail={**move.detail(), "error": "modify_sl_failed"},
            )
            return
        # 只成功一次即置位：反复改单没有意义，还会把日志和事件流刷满
        self._breakeven_done = True
        logger.info("task %s breakeven: %s", self.task_id, reason)
        await self._emit_progress(
            "breakeven", phase="running", positions=positions,
            message=reason,
            detail={**move.detail(), "modified": res.get("modified")},
        )

    def _risk_describe_open(self, plan: EntryPlan, cfg: RiskSizedConfig,
                            spec: SymbolSpec) -> str:
        """以损定量首单的开单原因：信号来源 + 手数是怎么反推出来的。"""
        return "；".join([
            f"策略信号底仓：{self.direction} {self.symbol} "
            f"{plan.base_volume} 手（托管策略「{self._strategy_name()}」）",
            risk_sizing.describe_plan(plan, cfg, spec),
            f"信号 {self.signal_id or '—'}",
        ])

    # ------------------------------------------------------------------
    # 网格路径（模版3）
    # ------------------------------------------------------------------
    async def _grid_symbol_spec(self) -> grid_trading.SymbolSpec:
        """读一次品种规格并缓存。"""
        if self._grid_spec is None:
            raw = dict(await self._exec(self._mt5.symbol_spec, self.symbol) or {})
            self._grid_spec = grid_trading.SymbolSpec(
                point=_as_float(raw.get("point")),
                digits=int(raw.get("digits") or 5),
                volume_min=_as_float(raw.get("volume_min")),
                volume_step=_as_float(raw.get("volume_step")),
                volume_max=_as_float(raw.get("volume_max")),
            )
        return self._grid_spec

    async def _grid_quote(self) -> float:
        """取当前价：多头网格看 ask（买入）、空头看 bid。"""
        quotes = dict(await self._exec(self._mt5.quotes, [self.symbol]) or {})
        quote = quotes.get(self.symbol) or next(iter(quotes.values()), {})
        key = "ask" if self.direction == "BUY" else "bid"
        return _as_float(quote.get(key)) or _as_float(quote.get("mid"))

    async def _grid_open_first(self) -> bool:
        """网格启动：建计划 → 等触发价 → 可选初始建仓。"""
        cfg = self._grid_cfg
        assert cfg is not None
        spec = await self._grid_symbol_spec()
        plan = grid_trading.plan_grid(
            cfg, signal_action=self.entry.get("action") or self.direction, spec=spec,
        )
        if not plan.ok:
            logger.warning("task %s grid rejected: %s", self.task_id, plan.reject)
            await self._emit_progress(
                "error", phase="failed",
                message=f"网格无法启动：{plan.reject}",
                detail={"kind": "grid_plan", "reason": plan.reject},
            )
            await self._finish("failed", f"grid_rejected: {plan.reject}")
            return False

        self._grid_plan = plan
        self.direction = plan.side
        self.base_volume = plan.lot_per_grid

        price = await self._grid_quote()
        self._grid_prev_price = price
        if not plan.triggered and not grid_trading.trigger_reached(
            cfg, price, prev_price=0.0,
        ):
            # 未到触发价：进入事件循环等待，空仓存活靠 hold_when_empty
            self._opened = True
            await self._emit_progress(
                "open", phase="running",
                message=(
                    f"网格等待触发价 {cfg.trigger_price}，现价 {price}；"
                    + grid_trading.describe_plan(plan, cfg, spec)
                ),
                detail={
                    **self._open_detail(),
                    **grid_trading.plan_detail(plan, cfg, spec),
                    "kind": "grid_plan",
                    "waiting_trigger": True,
                },
            )
            return True

        plan.triggered = True
        return await self._grid_do_prefill(price)

    async def _grid_do_prefill(self, price: float) -> bool:
        """按现价上方格位初始建仓（可关闭）。"""
        cfg, plan = self._grid_cfg, self._grid_plan
        assert cfg is not None and plan is not None
        spec = await self._grid_symbol_spec()

        if cfg.prefill_enabled and price > 0:
            indices = grid_trading.capped_prefill(
                plan, cfg, grid_trading.prefill_indices(plan, price),
            )
        else:
            indices = []

        opened_any = False
        for level in indices:
            ok = await self._grid_buy_level(level, price, positions=[])
            if ok:
                opened_any = True

        self._opened = True
        self._grid_prev_price = price
        msg = grid_trading.describe_plan(plan, cfg, spec)
        if indices:
            msg = f"网格初始建仓 {len(indices)} 格；{msg}"
        elif cfg.prefill_enabled:
            msg = f"网格启动（现价下方无格可预填）；{msg}"
        else:
            msg = f"网格启动（未开启初始建仓）；{msg}"
        await self._emit_progress(
            "open", phase="opened" if opened_any else "running",
            message=msg,
            detail={
                **self._open_detail(),
                **grid_trading.plan_detail(plan, cfg, spec),
                "kind": "grid_plan",
                "prefill_levels": indices,
            },
        )
        return True

    async def _grid_rebuild(self, positions: list[dict]) -> None:
        """恢复后按规则重建计划，并用持仓 comment / 开仓价还原格位映射。"""
        cfg = self._grid_cfg
        if cfg is None:
            return
        spec = await self._grid_symbol_spec()
        plan = grid_trading.plan_grid(
            cfg, signal_action=self.entry.get("action") or self.direction, spec=spec,
        )
        if not plan.ok:
            logger.info(
                "task %s resume without grid plan (%s): monitor only",
                self.task_id, plan.reject,
            )
            return
        plan.triggered = True
        self._grid_plan = plan
        self.direction = plan.side
        self.base_volume = plan.lot_per_grid

        price = await self._grid_quote()
        if price <= 0 and positions:
            latest = max(positions, key=lambda p: (p.get("time") or 0, p.get("ticket") or 0))
            price = _as_float(latest.get("price_current")) or _as_float(latest.get("price_open"))

        # 平移量不落库：按现价把网格追回断线前的位置，再拿 shift_count 去还原
        # 备注里的绝对格位，否则整批持仓都会被当成已移出网格。
        if cfg.trailing_up and price > 0:
            shift = grid_trading.trailing_shift(plan, cfg, price, digits=spec.digits)
            if shift is not None:
                grid_trading.apply_shift(plan, shift)
                logger.info(
                    "task %s resume: grid shifted %s to [%s, %s]",
                    self.task_id, shift.steps, plan.levels[0], plan.levels[-1],
                )

        for ticket in grid_trading.rebuild_holdings(plan, positions):
            if not await self._grid_close_orphan(ticket, price, positions):
                self._grid_orphans.add(ticket)
        if price > 0:
            self._grid_prev_price = price

    async def _grid_advance(self, event: MarketEvent, positions: list[dict]) -> bool:
        """推进网格：等触发 → 穿越买卖 → 止损止盈。返回 True 表示任务已收口。"""
        cfg, plan = self._grid_cfg, self._grid_plan
        if cfg is None or plan is None or not plan.ok:
            return False

        price = event.price
        if price <= 0:
            return False

        # 同步 holdings：外部平仓（止损单等）后清掉已不存在的 ticket
        live_tickets = {
            int(p.get("ticket") or 0) for p in positions if p.get("ticket")
        }
        stale = [lv for lv, tk in plan.holdings.items() if tk not in live_tickets]
        for lv in stale:
            plan.holdings.pop(lv, None)

        # 越界持仓不在格位映射里，没人会再触发它的卖出，只能在这里重试
        self._grid_orphans &= live_tickets
        for ticket in sorted(self._grid_orphans):
            if await self._grid_close_orphan(ticket, price, positions):
                self._grid_orphans.discard(ticket)

        if not plan.triggered:
            if not grid_trading.trigger_reached(cfg, price, self._grid_prev_price):
                self._grid_prev_price = price
                return False
            plan.triggered = True
            await self._grid_do_prefill(price)
            return False

        reason = grid_trading.terminate_reason(plan, price)
        if reason:
            await self._grid_terminate(reason)
            return True

        if await self._grid_apply_trailing(price, positions):
            # 网格线整体换过了，旧的参考价不能再拿来判穿越
            self._grid_prev_price = price
            return False

        prev = self._grid_prev_price or price
        for crossing in grid_trading.crossings(plan, prev, price):
            buy_lv = grid_trading.buy_level_for_crossing(plan, crossing)
            if buy_lv is not None and grid_trading.can_buy_more(plan, cfg):
                await self._grid_buy_level(buy_lv, price, positions)
                continue
            sell_lv = grid_trading.sell_level_for_crossing(plan, crossing)
            if sell_lv is not None:
                await self._grid_sell_level(sell_lv, price, positions)

        self._grid_prev_price = price
        return False

    async def _grid_buy_level(self, level: int, price: float,
                              positions: list[dict]) -> bool:
        """买入一格并登记 ticket。"""
        plan = self._grid_plan
        if plan is None or level in plan.holdings:
            return False
        spec = await self._grid_symbol_spec()
        reason = grid_trading.describe_fill(
            plan, level, price=price, action="BUY", spec=spec,
        )
        res = dict(await self._exec(
            self._mt5.place_market_order,
            self.symbol, plan.side, plan.lot_per_grid,
            None, None, grid_trading.grid_comment(plan.absolute_index(level)), self.magic,
        ) or {})
        detail = grid_trading.fill_detail(
            plan, level, price=price, action="BUY",
            ticket=res.get("order") or res.get("ticket"),
        )
        if not res.get("success"):
            logger.warning(
                "task %s grid buy L%s failed: %s", self.task_id, level, res.get("error"),
            )
            await self._emit_progress(
                "error", phase="running", positions=positions,
                message=f"网格买入失败：{res.get('error') or 'order failed'}；{reason}",
                detail={**detail, "error": str(res.get("error") or "")},
            )
            return False
        ticket = int(res.get("order") or res.get("ticket") or 0)
        if ticket:
            plan.holdings[level] = ticket
        self.add_count += 1
        self.total_orders += 1
        self.total_volume += plan.lot_per_grid
        logger.info("task %s grid buy L%s: %s", self.task_id, level, reason)
        await self._emit_progress(
            "grid_add", phase="running",
            last_order={
                "ticket": ticket or None,
                "price": res.get("price"),
                "volume": plan.lot_per_grid,
            },
            message=reason,
            detail=detail,
        )
        return True

    async def _grid_sell_level(self, level: int, price: float,
                               positions: list[dict]) -> None:
        """卖出（平掉）一格。"""
        plan = self._grid_plan
        if plan is None:
            return
        ticket = plan.holdings.get(level)
        if not ticket:
            return
        await self._grid_close_level(level, int(ticket), price, positions)

    async def _grid_close_level(self, level: int, ticket: int, price: float,
                                positions: list[dict], *, note: str = "") -> bool:
        """平掉某一格的持仓并从格位映射里摘除。"""
        plan = self._grid_plan
        if plan is None:
            return False
        spec = await self._grid_symbol_spec()
        reason = grid_trading.describe_fill(
            plan, level, price=price, action="CLOSE", spec=spec,
        )
        if note:
            reason = f"{note}；{reason}"
        res = dict(await self._exec(self._mt5.close_ticket, int(ticket)) or {})
        detail = grid_trading.fill_detail(
            plan, level, price=price, action="CLOSE", ticket=ticket,
        )
        if not res.get("success"):
            logger.warning(
                "task %s grid sell L%s failed: %s", self.task_id, level, res.get("error"),
            )
            await self._emit_progress(
                "error", phase="running", positions=positions,
                message=f"网格卖出失败：{res.get('error') or 'close failed'}；{reason}",
                detail={**detail, "error": str(res.get("error") or "")},
            )
            return False
        plan.holdings.pop(level, None)
        logger.info("task %s grid sell L%s: %s", self.task_id, level, reason)
        await self._emit_progress(
            "close_partial", phase="running",
            last_order={
                "ticket": ticket,
                "price": res.get("price") or price,
                "volume": plan.lot_per_grid,
            },
            message=reason,
            detail=detail,
        )
        return True

    async def _grid_close_orphan(self, ticket: int, price: float,
                                 positions: list[dict]) -> bool:
        """兑现越界持仓：它的格位已被平移挤出网格，留着没人管。"""
        res = dict(await self._exec(self._mt5.close_ticket, int(ticket)) or {})
        ok = bool(res.get("success"))
        logger.info(
            "task %s close out-of-grid ticket %s: %s",
            self.task_id, ticket, "ok" if ok else res.get("error"),
        )
        await self._emit_progress(
            "close_partial" if ok else "error", phase="running", positions=positions,
            message=(
                f"网格已平移，兑现越界持仓 #{ticket}"
                if ok else
                f"越界持仓 #{ticket} 平仓失败：{res.get('error') or 'close failed'}"
            ),
            detail={
                "kind": "grid_close",
                "ticket": ticket,
                "price": res.get("price") or price,
                "out_of_grid": True,
                "error": str(res.get("error") or ""),
            },
        )
        return ok

    async def _grid_apply_trailing(self, price: float, positions: list[dict]) -> bool:
        """向上追踪：价格越过区间外沿时整体平移网格。返回是否发生了平移。"""
        cfg, plan = self._grid_cfg, self._grid_plan
        if cfg is None or plan is None:
            return False
        spec = await self._grid_symbol_spec()
        shift = grid_trading.trailing_shift(plan, cfg, price, digits=spec.digits)
        if shift is None:
            return False

        # 被挤出网格的格位先兑现：它们的卖出线已不在新区间内，不平就永远卖不掉。
        # 这里平不掉的转入 orphans，由后续事件重试，避免持仓脱管到任务结束。
        for level, ticket in shift.dropped:
            if not await self._grid_close_level(
                level, ticket, price, positions, note="网格平移移出",
            ):
                self._grid_orphans.add(ticket)
        grid_trading.apply_shift(plan, shift)

        message = grid_trading.describe_shift(plan, shift, price=price, spec=spec)
        logger.info("task %s %s", self.task_id, message)
        await self._emit_progress(
            "grid_shift", phase="running", positions=positions,
            message=message,
            detail=grid_trading.shift_detail(plan, shift, price=price),
        )
        return True

    async def _grid_terminate(self, reason: str) -> None:
        """止损 / 止盈触发：按配置清仓后收口。"""
        cfg = self._grid_cfg
        self._stop_reason = reason
        if cfg is not None and cfg.close_on_stop:
            await self._close_all()
            return
        await self._finish("done", reason)

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
