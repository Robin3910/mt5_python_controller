"""策略托管执行器：一个分组任务一个实例，事件驱动，直到魔术号持仓全平。

职责：
1. 收到 strategy_start 后下首单（魔术号 = 服务端下发的任务魔术号）；
2. 订阅 MarketHub 事件，在持仓构成或价格变化时按策略规则推进仓位；
3. 收到 GONE（已确认该魔术号持仓归零）即上报 strategy_finished，服务端据此收口；
4. strategy_stop 时平掉该魔术号全部持仓后结束。

按策略快照里的规则走三条互斥的执行路径：

- 加仓路径（模版1）：首单手数用信号手数，之后按逆势 / 顺势规则加仓，判定见
  `strategy_rules.evaluate`；
- 以损定量路径（模版2）：手数由「风险金额 ÷ 止损距离」反推，底仓市价成交（TP=0）
  后立即拆开等手数分散仓市价单（按等分阶梯挂止盈），可选浮盈达标后移动止损保本，
  计算见 `risk_sizing`；
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
import close_reason
from strategy_rules import (
    CALC_BAR_TYPES,
    MT5_COMMENT_LIMIT,
    PositionCtx,
    counter_anchor_price,
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

# 模版 -> 期望执行路径。快照里找不到对应规则时必须失败退出而不是回退到加仓路径：
# 加仓路径会用信号自带的手数与止损直接开一笔市价单，与网格 / 以损定量的语义完全不同。
_TEMPLATE_MODES = {
    "tpl_2": _MODE_RISK_SIZED,
    "tpl_3": _MODE_GRID,
}

# 事件说明落库字段为 VARCHAR(255)，本地先截断，避免整条上报被后端丢弃
MESSAGE_LIMIT = 255

# 真正的终态：服务端收到后会清零持仓数、释放节点占位。
# stop_failed / detached / faulted 表示「已停止交易但魔术号下仍有持仓」，
# 属于非终态，服务端据此保留占位，不会把这批仓位当成已经平掉。
_TERMINAL_STATUSES = ("done", "failed")
# 一次停止流程内的「读持仓 -> 平仓」轮次；平仓本身在 mt5_client 层已有瞬时错误重试
_CLOSE_ALL_ATTEMPTS = 3

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
        runtime: Optional[dict] = None,
    ) -> None:
        self.task_id = int(task_id)
        self.magic = int(magic)
        self.group_id = group_id
        self.signal_id = signal_id
        self.entry = entry or {}
        self.strategy = strategy or {}
        # 服务端落库的网格运行态：重连 resume 时优先按它还原，而不是靠现价猜平移量
        self._resume_runtime = dict(runtime) if isinstance(runtime, dict) else {}
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
        self._stop_detail: Optional[dict] = None
        self._finished = False
        self._seed_pending = False
        self._task: Optional[asyncio.Task] = None
        self._sub: Optional[Subscription] = None
        self._last_report = 0.0
        self._started_at = time.time()
        self._last_floating_profit = 0.0
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
        # 卖出失败的格位：价格不会再穿越同一条线，不重试这一格就永远卖不掉
        self._grid_pending_sells: set[int] = set()
        # 已停止网格交易但持仓仍保留（close_on_stop=False）
        self._grid_detached = False
        self._reported_stuck: Optional[tuple[str, int]] = None
        self._mode_reject = self._template_mode_reject()

    def _template_mode_reject(self) -> Optional[str]:
        """模版与快照规则对不上时的拒绝原因；一致或未知模版返回 None。"""
        template_id = str(self.strategy.get("template_id") or "").strip().lower()
        expected = _TEMPLATE_MODES.get(template_id)
        if expected is None or expected == self._mode:
            return None
        return (
            f"策略模版 {template_id} 需要 {expected} 执行路径，"
            f"但快照里没有启用中的对应规则"
        )

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

    def request_stop(
        self, reason: str = "stop_command", *, detail: Optional[dict] = None,
    ) -> None:
        """请求终止；立刻唤醒事件等待，不必等下一轮采样。"""
        self._stopping = True
        self._stop_reason = reason
        if detail is not None:
            self._stop_detail = detail
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
        if self._mode_reject:
            logger.warning("task %s strategy mismatch: %s", self.task_id, self._mode_reject)
            await self._emit_progress(
                "error", phase="failed",
                message=f"策略无法执行：{self._mode_reject}",
                detail={"kind": "strategy_rule_missing", "reason": self._mode_reject},
            )
            await self._finish("failed", f"strategy_rule_missing: {self._mode_reject}")
            return
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
                if self._stopping and await self._close_all():
                    return
                event = await sub.next_event()
                if event is None:
                    logger.info("task %s subscription closed, monitor exits", self.task_id)
                    return
                if self._stopping:
                    # 停止流程还没平干净：回到开头重试，期间不再交易
                    continue
                if await self._on_event(event):
                    return
        except asyncio.CancelledError:
            logger.info("task %s monitor cancelled (will resume on reconnect)", self.task_id)
            raise
        except Exception as e:  # noqa: BLE001
            logger.exception("task %s runner crashed: %s", self.task_id, e)
            held = await self._magic_positions()
            if held:
                # 持仓还在就不能报终态，否则服务端会释放占位、这批仓位再没人负责
                await self._finish("faulted", f"runner_error: {e}", residual=len(held))
            else:
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
            # 默认 positions_cleared 时查成交历史，区分止损 / 止盈 / 人工
            if self._stop_reason == close_reason.DEFAULT_CLEAR_REASON:
                await self._resolve_passive_close_reason()
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
        """加仓判定上下文：顺势取最近一笔；逆势取不利方向最深开仓价（通常即首仓）。"""
        latest = max(positions, key=lambda p: (p.get("time") or 0, p.get("ticket") or 0))
        opens = [_as_float(p.get("price_open")) for p in positions]
        return PositionCtx(
            direction=self.direction,
            position_count=len(positions),
            base_volume=self.base_volume,
            base_price=_as_float(latest.get("price_open")),
            counter_base_price=counter_anchor_price(self.direction, opens),
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

    async def _risk_quote_pair(self) -> tuple[float, float]:
        """下底仓前的报价对：(开仓侧, 止损触发侧)。

        多单在 ask 成交、止损由 bid 触发，空单相反；止损距离要按触发侧算，阶梯止盈
        则锚在开仓侧，两者差一个点差。手数必须在下单前算出来，所以只能用当前报价
        预估；底仓成交后再用真实成交价把阶梯重锚一次（见 risk_sizing.anchor_to_fill）。
        """
        quotes = dict(await self._exec(self._mt5.quotes, [self.symbol]) or {})
        quote = quotes.get(self.symbol) or next(iter(quotes.values()), {})
        mid = _as_float(quote.get("mid"))
        bid = _as_float(quote.get("bid")) or mid
        ask = _as_float(quote.get("ask")) or mid
        return (ask, bid) if self.direction == "BUY" else (bid, ask)

    async def _risk_open_base(self) -> bool:
        """以损定量开仓：反推总手数 → 底仓市价（TP=0）→ 立即开齐分散仓。"""
        cfg = self._risk_cfg
        assert cfg is not None
        spec = await self._risk_symbol_spec()
        entry_quote, risk_quote = await self._risk_quote_pair()
        plan = risk_sizing.plan_entries(
            cfg,
            direction=self.direction,
            entry_price=entry_quote,
            stop_loss=_as_float(self.entry.get("stop_loss")),
            spec=spec,
            risk_price=risk_quote,
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
        # 底仓按截图语义：市价 + 止损 + 止盈为 0
        res = dict(await self._exec(
            self._mt5.place_market_order,
            self.symbol, self.direction, plan.base_volume,
            plan.stop_loss, None,
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
        # 底仓成交后立即市价开齐分散仓（对齐「订单数量 = 1 + N」）
        if self._stopping:
            return True
        await self._risk_open_pending_distribute(
            quote=_as_float(res.get("price")) or entry_quote,
        )
        return True

    async def _risk_open_pending_distribute(self, *, quote: float,
                                           positions: Optional[list[dict]] = None) -> int:
        """把尚未开出的分散仓一次打齐。返回成功笔数。"""
        cfg, plan = self._risk_cfg, self._risk_plan
        if cfg is None or plan is None or not plan.ok:
            return 0
        opened = 0
        for batch in risk_sizing.pending_batches(plan, filled=self.total_orders):
            # stop 到达后立刻停手，余下分散仓不再打；外层循环会去平仓
            if self._stopping:
                break
            ok = await self._risk_add_batch(batch, plan, cfg, quote, positions or [])
            if not ok:
                break
            opened += 1
        return opened

    async def _risk_rebuild_plan(self, positions: list[dict], point: float) -> None:
        """恢复后按真实持仓重建建仓计划。

        断线时计划已随进程丢失，而持仓上还挂着当初写进 MT5 的止损：用最早一笔的
        开仓价与止损价就能把计划还原出来。当初的点差已无从查起，用当前点差近似。
        若止损已被保本移动过（不在不利侧），plan_entries 会拒绝重建，此时降级为
        只监控到全平，不再补开分散仓也不再动止损。

        已开档位按 R3B* 备注还原，不能用持仓笔数——中间档止盈离场后笔数会变少，
        误当「未开完」会重复开分散仓、突破 risk_amount。
        """
        del point  # 点差改由当前买卖价差近似，保留参数兼容调用方
        cfg = self._risk_cfg
        if cfg is None or not positions:
            return
        earliest = min(positions, key=lambda p: (p.get("time") or 0, p.get("ticket") or 0))
        spec = await self._risk_symbol_spec()
        entry_quote, risk_quote = await self._risk_quote_pair()
        spread = abs(entry_quote - risk_quote)
        sign = 1 if self.direction == "BUY" else -1
        entry_price = _as_float(earliest.get("price_open"))
        plan = risk_sizing.plan_entries(
            cfg,
            direction=self.direction,
            entry_price=entry_price,
            stop_loss=_as_float(earliest.get("sl")),
            spec=spec,
            risk_price=entry_price - sign * spread,
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
        filled = risk_sizing.filled_orders_from_positions(positions)
        self.total_orders = max(self.total_orders, filled)
        self.add_count = max(self.add_count, max(0, filled - 1))
        logger.info(
            "task %s risk resume: filled=%s positions=%s pending=%s",
            self.task_id, filled, len(positions),
            len(risk_sizing.pending_batches(plan, filled=filled)),
        )

    async def _risk_advance(self, event: MarketEvent, positions: list[dict]) -> bool:
        """推进以损定量任务：先补齐未开的分散仓，再看保本。返回 True 表示本轮已有动作。"""
        cfg, plan = self._risk_cfg, self._risk_plan
        if cfg is None or plan is None or not plan.ok:
            return False
        pending = risk_sizing.pending_batches(plan, filled=self.total_orders)
        if pending:
            opened = await self._risk_open_pending_distribute(
                quote=event.price, positions=positions,
            )
            if opened:
                return True
        # 按次模式触发过后不再看；循环模式持续监控（改单本身会跳过已到位的止损）
        if self._breakeven_done and not cfg.breakeven_is_loop:
            return False
        move = risk_sizing.breakeven_move(
            cfg, plan, positions=positions, price=event.price,
            digits=(self._risk_spec.digits if self._risk_spec else 5),
        )
        if move is not None:
            await self._risk_move_breakeven(move, positions, cfg=cfg)
            return True
        return False

    async def _risk_add_batch(self, batch, plan: EntryPlan, cfg: RiskSizedConfig,
                              price: float, positions: list[dict]) -> bool:
        """开一笔分散仓；共用信号止损，挂阶梯上属于自己的那一档止盈。成功返回 True。"""
        spec = await self._risk_symbol_spec()
        reason = risk_sizing.describe_batch(plan, cfg, batch, spec, price)
        res = dict(await self._exec(
            self._mt5.place_market_order,
            self.symbol, self.direction, batch.volume,
            plan.stop_loss, batch.take_profit or None,
            risk_sizing.batch_comment(batch), self.magic,
        ) or {})
        detail = risk_sizing.batch_detail(plan, cfg, batch, spec, price)
        if not res.get("success"):
            logger.warning("task %s distribute %s failed: %s", self.task_id, batch.index, res.get("error"))
            await self._emit_progress(
                "error", phase="running", positions=positions,
                message=f"分散仓失败：{res.get('error') or 'distribute order failed'}；{reason}",
                detail={**detail, "error": str(res.get("error") or "")},
            )
            return False
        self.add_count += 1
        self.total_orders += 1
        self.total_volume += batch.volume
        logger.info("task %s distribute %s filled: %s", self.task_id, batch.index, reason)
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
        return True

    async def _risk_move_breakeven(self, move, positions: list[dict], *,
                                   cfg: RiskSizedConfig) -> None:
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
        # 按次：成功一次即置位；循环：保持监控，下次止损被拉回或均价变化时可再移
        if not cfg.breakeven_is_loop:
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

    async def _grid_quote(self, *, entry: bool = False) -> float:
        """取当前价。

        默认与 MarketHub 的口径一致（多头 bid / 空头 ask），保证 `_grid_prev_price`
        与事件价在点差的同一侧，否则光是启动那一下就能造出一次假穿越。
        entry=True 取真实入场价（多头 ask / 空头 bid），用于判断预填会不会开仓即亏。
        """
        quotes = dict(await self._exec(self._mt5.quotes, [self.symbol]) or {})
        quote = quotes.get(self.symbol) or next(iter(quotes.values()), {})
        if entry:
            key = "ask" if self.direction == "BUY" else "bid"
        else:
            key = "bid" if self.direction == "BUY" else "ask"
        return _as_float(quote.get(key)) or _as_float(quote.get("mid"))

    async def _grid_account_reject(self) -> Optional[str]:
        """网格按「一格一笔持仓 + comment 记格位」运行，净持仓账户会把仓位合并。"""
        try:
            acct = dict(await self._exec(self._mt5.account_info) or {})
        except Exception:  # noqa: BLE001
            logger.debug("task %s read account_info failed", self.task_id, exc_info=True)
            return None
        mode = str(acct.get("margin_mode") or "").strip().lower()
        # 读不到模式的旧节点 / 模拟环境不拦截，只拦明确的非对冲账户
        if not mode or mode == "hedging":
            return None
        return f"账户为 {mode} 模式，网格需要对冲（hedging）账户才能逐格持仓"

    async def _grid_open_first(self) -> bool:
        """网格启动：建计划 → 等触发价 → 可选初始建仓。"""
        cfg = self._grid_cfg
        assert cfg is not None
        spec = await self._grid_symbol_spec()
        reject = await self._grid_account_reject()
        if reject is None:
            plan = grid_trading.plan_grid(
                cfg, signal_action=self.entry.get("action") or self.direction, spec=spec,
            )
            reject = plan.reject if not plan.ok else None
        else:
            plan = grid_trading.GridPlan(side=self.direction, reject=reject)
        if reject:
            logger.warning("task %s grid rejected: %s", self.task_id, reject)
            await self._emit_progress(
                "error", phase="failed",
                message=f"网格无法启动：{reject}",
                detail={"kind": "grid_plan", "reason": reject},
            )
            await self._finish("failed", f"grid_rejected: {reject}")
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
            # 预填是市价成交，成本是入场价那一侧，按它判断哪些格位还能盈利兑现
            entry_price = await self._grid_quote(entry=True) or price
            indices = grid_trading.capped_prefill(
                plan, cfg, grid_trading.prefill_indices(plan, entry_price),
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

    def _grid_runtime_payload(self) -> Optional[dict]:
        """网格运行态快照：触发 / 平移 / 当前网格线，供服务端落库与重连还原。"""
        plan = self._grid_plan
        if plan is None or not plan.ok:
            return None
        return {
            "triggered": bool(plan.triggered),
            "shift_count": int(plan.shift_count),
            "levels": list(plan.levels),
            "stop_lower": plan.stop_lower or 0.0,
            "stop_upper": plan.stop_upper or 0.0,
            "side": plan.side,
            "lot_per_grid": plan.lot_per_grid,
            "total_orders": self.total_orders,
            "total_volume": round(self.total_volume, 4),
            "started_at": self._started_at,
        }

    def _apply_grid_runtime(self, plan: "grid_trading.GridPlan", runtime: dict) -> bool:
        """用落库的 runtime 还原网格线与触发态；字段齐全返回 True。"""
        levels = runtime.get("levels")
        if not isinstance(levels, list) or len(levels) < 3:
            return False
        try:
            restored = [float(x) for x in levels]
        except (TypeError, ValueError):
            return False
        if any(restored[i + 1] <= restored[i] for i in range(len(restored) - 1)):
            return False
        plan.levels = restored
        plan.triggered = bool(runtime.get("triggered", True))
        try:
            plan.shift_count = int(runtime.get("shift_count") or 0)
        except (TypeError, ValueError):
            plan.shift_count = 0
        plan.stop_lower = _as_float(runtime.get("stop_lower"))
        plan.stop_upper = _as_float(runtime.get("stop_upper"))
        if runtime.get("started_at"):
            try:
                self._started_at = float(runtime["started_at"])
            except (TypeError, ValueError):
                pass
        return True

    async def _grid_rebuild(self, positions: list[dict]) -> None:
        """恢复后按规则重建计划，优先用 runtime 还原，否则用持仓 / 现价猜测。"""
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
        # 默认假定已触发；若 runtime 明确写了未触发，下面会覆盖
        plan.triggered = True
        self._grid_plan = plan
        self.direction = plan.side
        self.base_volume = plan.lot_per_grid

        price = await self._grid_quote()
        if price <= 0 and positions:
            latest = max(positions, key=lambda p: (p.get("time") or 0, p.get("ticket") or 0))
            price = _as_float(latest.get("price_current")) or _as_float(latest.get("price_open"))

        runtime = self._resume_runtime
        restored = bool(runtime) and self._apply_grid_runtime(plan, runtime)
        if restored:
            logger.info(
                "task %s resume from runtime: shift=%s levels=[%s, %s] triggered=%s",
                self.task_id, plan.shift_count,
                plan.levels[0], plan.levels[-1], plan.triggered,
            )
        elif cfg.trailing_up and price > 0:
            # 无 runtime 时的旧路径：按现价把网格追回断线前的位置，
            # 再拿 shift_count 去还原备注里的绝对格位
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
        # 止损止盈由同 tick 的 _grid_advance 立刻判定，这里只还原状态

    async def _grid_advance(self, event: MarketEvent, positions: list[dict]) -> bool:
        """推进网格：等触发 → 穿越买卖 → 止损止盈。返回 True 表示任务已收口。"""
        cfg, plan = self._grid_cfg, self._grid_plan
        if cfg is None or plan is None or not plan.ok:
            return False

        if self._grid_detached:
            # 已停止网格交易但持仓保留：只等这批仓位被外部平光后收口
            if positions:
                return False
            await self._finish("done", self._stop_reason)
            return True

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

        # 上一轮卖失败的格位同理：价格不会再穿越同一条线，只能逐轮重试
        self._grid_pending_sells &= set(plan.holdings)
        for level in sorted(self._grid_pending_sells):
            await self._grid_sell_level(level, price, positions)

        if not plan.triggered:
            if not grid_trading.trigger_reached(cfg, price, self._grid_prev_price):
                self._grid_prev_price = price
                return False
            plan.triggered = True
            await self._grid_do_prefill(price)
            return False

        reason = grid_trading.terminate_reason(plan, price)
        if reason:
            return await self._grid_terminate(reason)

        # 先按当前网格兑现本轮穿越，再平移：突破外沿的那一根 tick 往往同时穿过卖出线，
        # 反过来先平移会把这一整根的成交连同格位映射一起丢掉
        prev = self._grid_prev_price or price
        for crossing in grid_trading.crossings(plan, prev, price):
            buy_lv = grid_trading.buy_level_for_crossing(plan, crossing)
            if buy_lv is not None and grid_trading.can_buy_more(plan, cfg):
                await self._grid_buy_level(buy_lv, price, positions)
                continue
            sell_lv = grid_trading.sell_level_for_crossing(plan, crossing)
            if sell_lv is not None:
                await self._grid_sell_level(sell_lv, price, positions)

        await self._grid_apply_trailing(price, positions)
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
                               positions: list[dict]) -> bool:
        """卖出（平掉）一格；失败的格位登记下来由后续事件重试。"""
        plan = self._grid_plan
        if plan is None:
            return False
        ticket = plan.holdings.get(level)
        if not ticket:
            self._grid_pending_sells.discard(level)
            return False
        if await self._grid_close_level(level, int(ticket), price, positions):
            self._grid_pending_sells.discard(level)
            return True
        self._grid_pending_sells.add(level)
        return False

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
        # 格位号整体平移了，待重试的卖出格位要跟着重新映射（被挤出网格的转 orphans）
        self._grid_pending_sells = {
            lv - shift.steps for lv in self._grid_pending_sells
        } & set(plan.holdings)

        message = grid_trading.describe_shift(plan, shift, price=price, spec=spec)
        logger.info("task %s %s", self.task_id, message)
        await self._emit_progress(
            "grid_shift", phase="running", positions=positions,
            message=message,
            detail=grid_trading.shift_detail(plan, shift, price=price),
        )
        return True

    async def _grid_terminate(self, reason: str) -> bool:
        """止损 / 止盈触发。返回 True 表示任务已收口、监控可以退出。

        close_on_stop=True 时把清仓交给主循环的停止分支，平不干净会逐轮重试。
        close_on_stop=False 时只停止网格交易、持仓原样留着，这时不能上报终态：
        服务端会清零持仓数并释放节点占位，这批仓位就再没人负责了。改报非终态
        detached，等持仓被外部平掉或管理端 CLOSE 再真正收口。
        """
        self._stop_reason = reason
        cfg = self._grid_cfg
        if cfg is None or cfg.close_on_stop:
            self._stopping = True
            return False
        self._grid_detached = True
        held = await self._magic_positions()
        if held:
            await self._finish("detached", reason, residual=len(held))
            return False
        await self._finish("done", reason)
        return True

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

    async def _magic_positions(self) -> Optional[list[dict]]:
        """读该魔术号当前持仓；读失败返回 None——「读不到」不等于「已平光」。"""
        try:
            rows = await self._exec(self._mt5.positions_by_magic, self.magic)
        except Exception:  # noqa: BLE001
            logger.debug("task %s read positions failed", self.task_id, exc_info=True)
            return None
        return None if rows is None else list(rows)

    async def _close_all(self) -> bool:
        """终止指令：平掉该魔术号的全部持仓。返回是否已确认无持仓并收口。

        必须复查确认确实平干净了才能上报终态：服务端收到终态会把持仓数清零并释放
        节点占位，此时若还有残仓，这批仓位就再也没人负责了。
        """
        error = ""
        left = 0
        for attempt in range(_CLOSE_ALL_ATTEMPTS):
            held = await self._magic_positions()
            if held is not None:
                left = len(held)
                if not held:
                    await self._finish("done", self._stop_reason)
                    return True
                # 平仓前先记下浮盈，成交历史查询失败时作为回退
                self._last_floating_profit = round(
                    sum(_as_float(p.get("profit")) for p in held), 2,
                )
            if attempt + 1 >= _CLOSE_ALL_ATTEMPTS:
                break
            res = dict(await self._exec(self._mt5.close_by_magic, self.magic) or {})
            if not res.get("success", True):
                error = str(res.get("error") or "close failed")
        await self._finish(
            "stop_failed",
            f"close_failed: {error or '平仓后仍有持仓'}",
            residual=left,
        )
        return False

    # ------------------------------------------------------------------
    # 上报
    # ------------------------------------------------------------------
    def _snapshot(self, positions: Optional[list[dict]] = None) -> dict:
        pos = positions or []
        profit = round(sum(_as_float(p.get("profit")) for p in pos), 2)
        if pos:
            self._last_floating_profit = profit
        return {
            "position_count": len(pos),
            "add_count": self.add_count,
            "total_orders": self.total_orders,
            "total_volume": round(self.total_volume, 4),
            "profit": profit,
        }

    async def _resolve_realized_profit(self) -> float:
        """收口时的已实现盈亏：成交历史优先；历史仍为 0 时回退平仓前浮盈快照。

        网格等路径会在任务中途平仓，成交历史能累计全程盈亏；刚平完时历史库可能
        尚未写入，此时用平仓前浮盈避免把盈亏上报成 0。
        """
        floating = round(_as_float(self._last_floating_profit), 2)
        since = self._started_at or (time.time() - 86400)
        try:
            if hasattr(self._mt5, "realized_profit_by_magic"):
                val = await self._exec(
                    self._mt5.realized_profit_by_magic, self.magic, since,
                )
                if val is not None:
                    hist = round(float(val), 2)
                    if hist == 0.0 and floating != 0.0:
                        return floating
                    return hist
        except Exception:  # noqa: BLE001
            logger.debug(
                "task %s realized_profit_by_magic failed", self.task_id, exc_info=True,
            )
        return floating

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
        # 非心跳附带网格运行态，服务端落 runtime_json，重连可原样恢复
        if event != "heartbeat" and self.is_grid:
            runtime = self._grid_runtime_payload()
            if runtime is not None:
                data["runtime"] = runtime
        await self._send({"type": "strategy_progress", "data": data})

    async def _resolve_passive_close_reason(self) -> None:
        """被动全平（终端止损/止盈/人工）时，用成交历史把默认码换成中文原因。

        主动 stop（strategy_stop / 账户风控）已写过 _stop_reason，不会走进这里。
        """
        if not hasattr(self._mt5, "exit_deals_by_magic"):
            return
        since = self._started_at or (time.time() - 86400)
        try:
            deals = await self._exec(self._mt5.exit_deals_by_magic, self.magic, since)
        except Exception:  # noqa: BLE001
            logger.debug(
                "task %s exit_deals_by_magic failed", self.task_id, exc_info=True,
            )
            return
        message, detail = close_reason.summarize_exit_deals(list(deals or []))
        if message == close_reason.DEFAULT_CLEAR_REASON:
            return
        self._stop_reason = message
        if detail is not None and self._stop_detail is None:
            self._stop_detail = detail

    async def _finish(self, status: str, reason: str, *, residual: int = 0) -> None:
        """上报收口结果。

        residual > 0 时 status 必须是非终态：持仓还在，服务端要继续持有节点占位。
        非终态可能被反复触发（每轮重试都会走到这里），同一状态只上报一次。
        """
        if self._finished:
            return
        if status in _TERMINAL_STATUSES:
            self._finished = True
        elif self._reported_stuck == (status, residual):
            return
        else:
            self._reported_stuck = (status, residual)
            logger.warning(
                "task %s %s: %s (residual=%s)", self.task_id, status, reason, residual,
            )
        realized = await self._resolve_realized_profit()
        logger.info(
            "task %s finished: %s (%s) realized_profit=%s",
            self.task_id, status, reason, realized,
        )
        data: dict = {
            "task_id": self.task_id,
            "magic": self.magic,
            "group_id": self.group_id,
            "signal_id": self.signal_id,
            "symbol": self.symbol,
            "status": status,
            "reason": str(reason or "")[:MESSAGE_LIMIT],
            "total_orders": self.total_orders,
            "total_volume": round(self.total_volume, 4),
            "realized_profit": realized,
            "residual_positions": residual,
        }
        if self._stop_detail:
            data["detail"] = self._stop_detail
        await self._send({"type": "strategy_finished", "data": data})
