"""策略监控的事件总线：统一采样 MT5，按变化派发事件给订阅者。

MT5 的 Python API 只能主动查询，没有 OnTick 之类的回调，所以事件由本模块的采样
协程统一生成：每轮只调用一次 positions() 与一次 quotes()，一次覆盖全部订阅者，
再按「持仓构成变化 / 价格变动 / 空闲保活」决定要不要派发。MT5 调用次数因此与
策略任务数解耦，StrategyRunner 不再自己读 MT5、也不再自带轮询循环。

事件源是可替换的：订阅方只依赖 `Subscription.next_event()` 这一契约。将来若改用
MQL5 EA 主动推送，只需换掉本模块的采样实现，执行器不必改动。

两条关键约定，都是为了不把「读不到」当成「没有」：
- 持仓读取失败派发 STALE 而不是空持仓，订阅方据此区分「确实已全平」与「这一轮
  没读到」，避免查询异常被误判为任务完成；
- 只有在曾经观察到过该魔术号的持仓之后，才可能派发 GONE。首单尚未反映到终端时
  不会误报收口，这种漏报由服务端的账户快照对账兜底。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import bar_metrics

logger = logging.getLogger("node.hub")

# —— 事件类型 ——
TICK = "tick"      # 价格或持仓构成发生变化，需要重新判定
GONE = "gone"      # 已确认该魔术号无持仓（可据此收口）
STALE = "stale"    # 本轮持仓读取失败，不可做任何判定
IDLE = "idle"      # 长时间无变化的保活事件（供订阅方按节奏上报）
WAKE = "wake"      # 订阅方自己唤醒（如收到终止指令）

# 待消费事件被新事件覆盖时只保留信息量更全的那个。GONE 最高——收口判定不能被
# 随后的保活或读取失败盖掉；WAKE 只负责唤醒，不该顶掉带行情的 TICK。
_PRIORITY = {STALE: 0, IDLE: 1, WAKE: 2, TICK: 3, GONE: 4}


def _as_int(value: object) -> Optional[int]:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class MarketEvent:
    """派发给订阅者的一次事件快照。"""
    kind: str
    symbol: str
    magic: int
    positions: tuple[dict, ...] = ()
    price: float = 0.0
    point: float = 0.0
    ts: float = 0.0


@dataclass
class Subscription:
    """一个策略任务对某魔术号的订阅。

    只保留最新一份未消费事件：订阅方在下单等耗时操作上阻塞时，堆积的旧行情已无
    意义，合并成最新一份既省内存又避免拿着过期价格做判定。
    """
    symbol: str
    magic: int
    direction: str

    # —— 变更检测状态（仅 MarketHub 读写）——
    signature: tuple = ()
    price: Optional[float] = None
    empty_hits: int = 0
    seen: bool = False
    published_at: float = 0.0

    _ready: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    _latest: Optional[MarketEvent] = field(default=None, repr=False)
    _closed: bool = field(default=False, repr=False)

    @property
    def closed(self) -> bool:
        return self._closed

    def offer(self, event: MarketEvent) -> None:
        """投递事件；已有未消费事件时按优先级合并。"""
        if self._closed:
            return
        pending = self._latest if self._ready.is_set() else None
        if pending is None or _PRIORITY.get(event.kind, 0) >= _PRIORITY.get(pending.kind, 0):
            self._latest = event
        self._ready.set()

    def wake(self) -> None:
        """立刻唤醒等待中的订阅方（终止指令不必等下一轮采样）。"""
        self.offer(
            MarketEvent(kind=WAKE, symbol=self.symbol, magic=self.magic, ts=time.time())
        )

    async def next_event(self) -> Optional[MarketEvent]:
        """等下一个事件；订阅已关闭时返回 None。"""
        await self._ready.wait()
        self._ready.clear()
        if self._closed:
            return None
        return self._latest

    def close(self) -> None:
        self._closed = True
        self._ready.set()  # 唤醒可能正在等待的订阅方，让它退出循环


class MarketHub:
    """节点内唯一的行情/持仓采样器，把采样结果转成事件派发给各策略任务。"""

    def __init__(
        self,
        mt5,
        exec_fn: Callable,
        *,
        interval: float = 0.5,
        idle_interval: float = 5.0,
        empty_confirm: int = 2,
    ) -> None:
        self._mt5 = mt5
        self._exec = exec_fn  # async (fn, *args) -> result，把阻塞 MT5 调用丢线程池
        self.interval = max(0.05, float(interval or 0.5))
        self.idle_interval = max(self.interval, float(idle_interval or 5.0))
        self.empty_confirm = max(1, int(empty_confirm or 1))
        self._subs: list[Subscription] = []
        self._points: dict[str, float] = {}
        # (symbol, timeframe, metric) -> (指标值, 过期时刻)
        self._metrics: dict[tuple[str, str, str], tuple[float, float]] = {}
        self._task: Optional[asyncio.Task] = None
        self._closed = False
        self._awake = asyncio.Event()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self) -> None:
        """启动采样协程（幂等）。由持有者负责调用，subscribe 不自动启动。"""
        if self._closed:
            return
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def close(self) -> None:
        self._closed = True
        self._awake.set()
        for sub in list(self._subs):
            sub.close()
        self._subs.clear()
        task, self._task = self._task, None
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def subscribe(self, *, symbol: str, magic: int, direction: str) -> Subscription:
        sub = Subscription(
            symbol=str(symbol or ""), magic=int(magic),
            direction=str(direction or "BUY").upper(),
        )
        self._subs.append(sub)
        self._awake.set()  # 采样协程可能正在空转等待，唤醒它开始工作
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        sub.close()
        with contextlib.suppress(ValueError):
            self._subs.remove(sub)

    # ------------------------------------------------------------------
    # 采样循环
    # ------------------------------------------------------------------
    async def _loop(self) -> None:
        while not self._closed:
            if not self._subs:
                # 没有策略任务时完全不碰 MT5，等订阅唤醒
                self._awake.clear()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._awake.wait(), timeout=self.idle_interval)
                continue
            try:
                await self.sample()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning("market hub sample failed: %s", e)
            await asyncio.sleep(self.interval)

    async def sample(self) -> None:
        """采样一轮并派发事件（单测可直接调用，无需等定时器）。"""
        subs = [s for s in self._subs if not s.closed]
        if not subs:
            return
        now = time.time()
        ok, positions = await self._read_positions()
        if not ok:
            self._offer_stale(subs, now)
            return

        by_magic: dict[int, list[dict]] = {}
        for pos in positions:
            magic = _as_int((pos or {}).get("magic"))
            if magic is None:
                continue
            by_magic.setdefault(magic, []).append(pos)

        quotes = await self._read_quotes([s.symbol for s in subs])
        for sub in subs:
            held = by_magic.get(sub.magic, [])
            price = self._pick_price(quotes, sub, held)
            point = await self._point(sub.symbol)
            kind = self._classify(sub, held, price, now)
            if kind is None:
                continue
            sub.published_at = now
            sub.offer(
                MarketEvent(
                    kind=kind, symbol=sub.symbol, magic=sub.magic,
                    positions=tuple(held), price=price, point=point, ts=now,
                )
            )

    def _offer_stale(self, subs: list[Subscription], now: float) -> None:
        """读不到持仓：只发 STALE，且按保活间隔限流，避免故障期刷爆日志。"""
        for sub in subs:
            if now - sub.published_at < self.idle_interval:
                continue
            sub.published_at = now
            sub.offer(
                MarketEvent(kind=STALE, symbol=sub.symbol, magic=sub.magic, ts=now)
            )

    def _classify(
        self, sub: Subscription, held: list[dict], price: float, now: float,
    ) -> Optional[str]:
        """判断这一轮要不要给该订阅者派发事件，以及派发哪一种。"""
        if not held:
            if not sub.seen:
                # 从未观察到过持仓：首单可能还没反映到终端，此时判全平会误收口，
                # 真正的漏报交给服务端账户快照对账兜底
                return None
            sub.empty_hits += 1
            # 连续多轮确认为空才判全平，容忍 MT5 偶发返回不完整持仓
            if sub.empty_hits >= self.empty_confirm:
                return GONE
            return None

        sub.seen = True
        sub.empty_hits = 0
        signature = (
            len(held),
            tuple(sorted(_as_int(p.get("ticket")) or 0 for p in held)),
        )
        if signature != sub.signature or price != sub.price:
            sub.signature, sub.price = signature, price
            return TICK
        if now - sub.published_at >= self.idle_interval:
            return IDLE
        return None

    # ------------------------------------------------------------------
    # MT5 访问
    # ------------------------------------------------------------------
    async def _read_positions(self) -> tuple[bool, list[dict]]:
        """读全部持仓。返回 (是否读到, 持仓)，失败与「确实为空」必须能区分开。"""
        try:
            rows = await self._exec(self._mt5.positions)
        except Exception as e:  # noqa: BLE001
            logger.debug("hub read positions failed: %s", e)
            return False, []
        if rows is None:
            return False, []
        return True, list(rows)

    async def _read_quotes(self, symbols: list[str]) -> dict[str, dict]:
        wanted = sorted({s for s in symbols if s})
        if not wanted:
            return {}
        try:
            return await self._exec(self._mt5.quotes, wanted) or {}
        except Exception as e:  # noqa: BLE001
            logger.debug("hub read quotes failed: %s", e)
            return {}

    async def _point(self, symbol: str) -> float:
        """品种最小变动单位；取到有效值后缓存（不会变），取不到下轮再试。"""
        cached = self._points.get(symbol)
        if cached:
            return cached
        try:
            point = float(await self._exec(self._mt5.symbol_point, symbol) or 0.0)
        except Exception as e:  # noqa: BLE001
            logger.debug("hub read point failed for %s: %s", symbol, e)
            return 0.0
        if point > 0:
            self._points[symbol] = point
        return point

    async def bar_metric(self, symbol: str, timeframe: str, metric: str) -> float:
        """ATR / 波幅指标（价格距离），按 K 线周期缓存。

        采样每 0.5 秒一轮，但指标统计的是已收盘 K 线，同一根 K 线内不会变，
        因此按周期缓存，避免把 MT5 的 K 线接口打满。与 point 一样只缓存有效值。
        """
        key = (symbol, str(timeframe or "").upper(), metric)
        now = time.time()
        cached = self._metrics.get(key)
        if cached and cached[1] > now:
            return cached[0]
        try:
            bars = await self._exec(
                self._mt5.closed_bars, symbol, timeframe, bar_metrics.bars_needed(metric),
            )
        except Exception as e:  # noqa: BLE001
            logger.debug("hub read bars failed for %s %s: %s", symbol, timeframe, e)
            return 0.0
        value = bar_metrics.compute(metric, list(bars or []))
        if value > 0:
            self._metrics[key] = (value, now + bar_metrics.cache_seconds(timeframe))
        return value

    @staticmethod
    def _pick_price(quotes: dict, sub: Subscription, held: list[dict]) -> float:
        """取当前价：多单看买价、空单看卖价（与平仓口径一致）；取不到回落到持仓价。"""
        quote = quotes.get(sub.symbol) or quotes.get(sub.symbol.upper())
        if quote:
            side = "bid" if sub.direction == "BUY" else "ask"
            try:
                value = float(quote.get(side) or 0.0)
            except (TypeError, ValueError):
                value = 0.0
            if value:
                return value
        latest = held[-1] if held else {}
        try:
            return float(latest.get("price_current") or latest.get("price_open") or 0.0)
        except (TypeError, ValueError):
            return 0.0
