"""事件总线单测：变更检测、读取失败与全平判定、事件合并、采样开销。

统一直接调用 `hub.sample()`，不启动采样协程，避免依赖定时器让断言不稳定。
"""
import asyncio

import market_hub as mh
from market_hub import MarketHub

MAGIC = 900000001
OTHER_MAGIC = 900000002


class FakeMT5:
    """可控的 MT5 替身：能注入读取失败，并统计调用次数。"""

    def __init__(self) -> None:
        self._positions: list[dict] = []
        self._orders: list[dict] = []
        self.quote = {"bid": 2330.0, "ask": 2330.2, "mid": 2330.1, "change": 0.0}
        self.point = 0.01
        self.fail_positions = False
        self.fail_orders = False
        self.positions_calls = 0
        self.orders_calls = 0
        self.quotes_calls = 0
        self.bars_calls = 0
        self.fail_bars = False
        # 每根 K 线高低差固定 2.0，ATR 与最大波幅都等于 2.0
        self.bar_span = 2.0

    def positions(self) -> list[dict]:
        self.positions_calls += 1
        if self.fail_positions:
            raise RuntimeError("terminal unavailable")
        return [dict(p) for p in self._positions]

    def pending_orders(self) -> list[dict]:
        self.orders_calls += 1
        if self.fail_orders:
            raise RuntimeError("terminal unavailable")
        return [dict(o) for o in self._orders]

    def quotes(self, symbols) -> dict:
        self.quotes_calls += 1
        return {s: dict(self.quote) for s in symbols}

    def symbol_point(self, symbol) -> float:
        return self.point

    def closed_bars(self, symbol, timeframe, count) -> list[dict]:
        self.bars_calls += 1
        if self.fail_bars:
            raise RuntimeError("no history")
        mid = 2330.0
        half = self.bar_span / 2
        return [
            {"time": float(i), "open": mid, "high": mid + half, "low": mid - half, "close": mid}
            for i in range(int(count))
        ]

    def add(self, *, ticket: int, magic: int, price: float = 2330.0) -> None:
        self._positions.append({
            "ticket": ticket, "magic": magic, "symbol": "XAUUSD", "type": "BUY",
            "volume": 0.1, "price_open": price, "price_current": price,
            "profit": 0.0, "time": ticket,
        })

    def add_order(self, *, ticket: int, magic: int, price: float = 2320.0) -> None:
        self._orders.append({
            "ticket": ticket, "magic": magic, "symbol": "XAUUSD", "type": "BUY",
            "pending_kind": "limit", "volume": 0.1, "price_open": price,
            "price_current": price, "sl": 2300.0, "tp": 0.0, "time": ticket,
        })

    def clear(self) -> None:
        self._positions = []

    def clear_orders(self) -> None:
        self._orders = []


async def _exec(fn, *args):
    return fn(*args)


def _hub(mt5, **kw) -> MarketHub:
    kw.setdefault("interval", 0.01)
    kw.setdefault("idle_interval", 10.0)
    kw.setdefault("empty_confirm", 2)
    return MarketHub(mt5, _exec, **kw)


async def _next_or_none(sub, timeout: float = 0.02):
    """取下一个事件；超时返回 None，用于断言“这一轮没有派发”。"""
    try:
        return await asyncio.wait_for(sub.next_event(), timeout)
    except asyncio.TimeoutError:
        return None


async def test_sample_emits_tick_with_price_and_point():
    mt5 = FakeMT5()
    mt5.add(ticket=1, magic=MAGIC)
    hub = _hub(mt5)
    sub = hub.subscribe(symbol="XAUUSD", magic=MAGIC, direction="BUY")

    await hub.sample()
    event = await sub.next_event()

    assert event.kind == mh.TICK
    assert event.price == 2330.0  # 多单看买价
    assert event.point == 0.01
    assert len(event.positions) == 1
    assert event.positions[0]["ticket"] == 1


async def test_sell_direction_uses_ask_price():
    mt5 = FakeMT5()
    mt5.add(ticket=1, magic=MAGIC)
    hub = _hub(mt5)
    sub = hub.subscribe(symbol="XAUUSD", magic=MAGIC, direction="SELL")

    await hub.sample()

    assert (await sub.next_event()).price == 2330.2


async def test_price_falls_back_to_position_when_quote_missing():
    mt5 = FakeMT5()
    mt5.add(ticket=1, magic=MAGIC, price=2222.0)
    mt5.quote = {}
    hub = _hub(mt5)
    sub = hub.subscribe(symbol="XAUUSD", magic=MAGIC, direction="BUY")

    await hub.sample()

    assert (await sub.next_event()).price == 2222.0


async def test_unchanged_sample_emits_nothing():
    mt5 = FakeMT5()
    mt5.add(ticket=1, magic=MAGIC)
    hub = _hub(mt5)
    sub = hub.subscribe(symbol="XAUUSD", magic=MAGIC, direction="BUY")

    await hub.sample()
    assert (await sub.next_event()).kind == mh.TICK
    await hub.sample()

    assert await _next_or_none(sub) is None


async def test_price_change_emits_tick():
    mt5 = FakeMT5()
    mt5.add(ticket=1, magic=MAGIC)
    hub = _hub(mt5)
    sub = hub.subscribe(symbol="XAUUSD", magic=MAGIC, direction="BUY")

    await hub.sample()
    await sub.next_event()
    mt5.quote = {"bid": 2331.0, "ask": 2331.2, "mid": 2331.1, "change": 0.0}
    await hub.sample()

    event = await sub.next_event()
    assert event.kind == mh.TICK
    assert event.price == 2331.0


async def test_new_position_emits_tick():
    mt5 = FakeMT5()
    mt5.add(ticket=1, magic=MAGIC)
    hub = _hub(mt5)
    sub = hub.subscribe(symbol="XAUUSD", magic=MAGIC, direction="BUY")

    await hub.sample()
    await sub.next_event()
    mt5.add(ticket=2, magic=MAGIC)
    await hub.sample()

    event = await sub.next_event()
    assert event.kind == mh.TICK
    assert len(event.positions) == 2


async def test_idle_keepalive_when_nothing_changes():
    mt5 = FakeMT5()
    mt5.add(ticket=1, magic=MAGIC)
    # interval 有 50ms 下限，保活间隔不会低于它
    hub = _hub(mt5, interval=0.05, idle_interval=0.05)
    sub = hub.subscribe(symbol="XAUUSD", magic=MAGIC, direction="BUY")

    await hub.sample()
    assert (await sub.next_event()).kind == mh.TICK
    await asyncio.sleep(0.07)
    await hub.sample()

    event = await sub.next_event()
    assert event.kind == mh.IDLE
    assert len(event.positions) == 1


async def test_read_failure_emits_stale_not_gone():
    mt5 = FakeMT5()
    mt5.add(ticket=1, magic=MAGIC)
    hub = _hub(mt5, interval=0.05, idle_interval=0.05)
    sub = hub.subscribe(symbol="XAUUSD", magic=MAGIC, direction="BUY")

    await hub.sample()
    assert (await sub.next_event()).kind == mh.TICK

    mt5.fail_positions = True
    await asyncio.sleep(0.07)  # 越过 STALE 限流窗口
    await hub.sample()

    event = await sub.next_event()
    assert event.kind == mh.STALE
    assert sub.empty_hits == 0  # 读取失败不能计入空仓确认


async def test_positions_cleared_needs_confirm_rounds():
    mt5 = FakeMT5()
    mt5.add(ticket=1, magic=MAGIC)
    hub = _hub(mt5, empty_confirm=2)
    sub = hub.subscribe(symbol="XAUUSD", magic=MAGIC, direction="BUY")

    await hub.sample()
    assert (await sub.next_event()).kind == mh.TICK

    mt5.clear()
    await hub.sample()
    assert await _next_or_none(sub) is None  # 第一轮空仓不作数

    await hub.sample()
    assert (await sub.next_event()).kind == mh.GONE


async def test_never_seen_positions_never_reports_gone():
    """首单还没反映到终端时不能误判收口，漏报交给服务端快照对账兜底。"""
    mt5 = FakeMT5()
    hub = _hub(mt5, empty_confirm=1)
    sub = hub.subscribe(symbol="XAUUSD", magic=MAGIC, direction="BUY")

    for _ in range(5):
        await hub.sample()

    assert await _next_or_none(sub) is None
    assert sub.seen is False


async def test_one_sample_serves_all_subscribers():
    mt5 = FakeMT5()
    mt5.add(ticket=1, magic=MAGIC)
    mt5.add(ticket=2, magic=OTHER_MAGIC)
    hub = _hub(mt5)
    first = hub.subscribe(symbol="XAUUSD", magic=MAGIC, direction="BUY")
    second = hub.subscribe(symbol="XAUUSD", magic=OTHER_MAGIC, direction="BUY")

    await hub.sample()

    # MT5 调用次数与任务数无关：一轮各一次
    assert mt5.positions_calls == 1
    assert mt5.quotes_calls == 1
    assert (await first.next_event()).positions[0]["ticket"] == 1
    assert (await second.next_event()).positions[0]["ticket"] == 2


async def test_point_is_cached_across_samples():
    mt5 = FakeMT5()
    mt5.add(ticket=1, magic=MAGIC)
    hub = _hub(mt5)
    hub.subscribe(symbol="XAUUSD", magic=MAGIC, direction="BUY")

    await hub.sample()
    mt5.point = 0.0  # 缓存命中后不应再取值
    await hub.sample()

    assert hub._points["XAUUSD"] == 0.01


async def test_unsubscribed_sub_gets_no_events():
    mt5 = FakeMT5()
    mt5.add(ticket=1, magic=MAGIC)
    hub = _hub(mt5)
    sub = hub.subscribe(symbol="XAUUSD", magic=MAGIC, direction="BUY")
    hub.unsubscribe(sub)

    await hub.sample()

    assert sub.closed is True
    assert mt5.positions_calls == 0  # 没有订阅者时完全不碰 MT5


async def test_pending_event_is_conflated_to_latest():
    sub = mh.Subscription(symbol="XAUUSD", magic=MAGIC, direction="BUY")
    sub.offer(mh.MarketEvent(kind=mh.TICK, symbol="XAUUSD", magic=MAGIC, price=1.0))
    sub.offer(mh.MarketEvent(kind=mh.TICK, symbol="XAUUSD", magic=MAGIC, price=2.0))

    assert (await sub.next_event()).price == 2.0
    assert await _next_or_none(sub) is None


async def test_gone_is_not_overwritten_by_stale():
    sub = mh.Subscription(symbol="XAUUSD", magic=MAGIC, direction="BUY")
    sub.offer(mh.MarketEvent(kind=mh.GONE, symbol="XAUUSD", magic=MAGIC))
    sub.offer(mh.MarketEvent(kind=mh.STALE, symbol="XAUUSD", magic=MAGIC))

    assert (await sub.next_event()).kind == mh.GONE


async def test_wake_unblocks_waiter():
    sub = mh.Subscription(symbol="XAUUSD", magic=MAGIC, direction="BUY")
    waiter = asyncio.create_task(sub.next_event())
    await asyncio.sleep(0)
    sub.wake()

    assert (await waiter).kind == mh.WAKE


async def test_close_returns_none_to_waiter():
    sub = mh.Subscription(symbol="XAUUSD", magic=MAGIC, direction="BUY")
    waiter = asyncio.create_task(sub.next_event())
    await asyncio.sleep(0)
    sub.close()

    assert await waiter is None


async def test_loop_idles_without_subscribers():
    mt5 = FakeMT5()
    hub = _hub(mt5, interval=0.001, idle_interval=0.005)
    hub.start()
    await asyncio.sleep(0.03)

    assert mt5.positions_calls == 0
    await hub.close()


async def test_loop_starts_sampling_after_subscribe():
    mt5 = FakeMT5()
    mt5.add(ticket=1, magic=MAGIC)
    hub = _hub(mt5, interval=0.001, idle_interval=0.05)
    hub.start()
    sub = hub.subscribe(symbol="XAUUSD", magic=MAGIC, direction="BUY")

    event = await asyncio.wait_for(sub.next_event(), timeout=2.0)

    assert event.kind == mh.TICK
    await hub.close()


async def test_loop_survives_sample_error():
    mt5 = FakeMT5()
    mt5.add(ticket=1, magic=MAGIC)
    hub = _hub(mt5, interval=0.001, idle_interval=0.05)
    hub.start()
    sub = hub.subscribe(symbol="XAUUSD", magic=MAGIC, direction="BUY")
    mt5.fail_positions = True
    await asyncio.sleep(0.02)
    mt5.fail_positions = False

    event = await asyncio.wait_for(sub.next_event(), timeout=2.0)

    assert event.kind in (mh.STALE, mh.TICK)
    assert hub._task is not None and not hub._task.done()
    await hub.close()


# --------------------------- ATR / 波幅指标 ---------------------------
async def test_bar_metric_computes_atr_and_range():
    mt5 = FakeMT5()
    hub = _hub(mt5)

    assert await hub.bar_metric("XAUUSD", "M5", "atr") == 2.0
    assert await hub.bar_metric("XAUUSD", "M5", "range") == 2.0


async def test_bar_metric_is_cached_within_the_bar():
    """采样每轮都会问指标，但同一根 K 线内不该重复读 K 线。"""
    mt5 = FakeMT5()
    hub = _hub(mt5)

    for _ in range(5):
        assert await hub.bar_metric("XAUUSD", "M5", "atr") == 2.0

    assert mt5.bars_calls == 1


async def test_bar_metric_caches_per_timeframe_and_metric():
    mt5 = FakeMT5()
    hub = _hub(mt5)

    await hub.bar_metric("XAUUSD", "M5", "atr")
    await hub.bar_metric("XAUUSD", "M15", "atr")     # 换周期要重新读
    await hub.bar_metric("XAUUSD", "M5", "range")    # 换指标也要重新读
    await hub.bar_metric("XAUUSD", "M5", "atr")      # 命中缓存

    assert mt5.bars_calls == 3


async def test_bar_metric_failure_is_not_cached():
    """读不到就返回 0 让判定跳过，但不能把 0 缓存住，否则该档位会长期失效。"""
    mt5 = FakeMT5()
    mt5.fail_bars = True
    hub = _hub(mt5)

    assert await hub.bar_metric("XAUUSD", "M5", "atr") == 0.0
    mt5.fail_bars = False
    assert await hub.bar_metric("XAUUSD", "M5", "atr") == 2.0


async def test_hold_when_empty_emits_price_ticks_instead_of_gone():
    """网格空仓是常态：hold_when_empty=True 时不发 GONE，继续派发价格事件。"""
    mt5 = FakeMT5()
    hub = _hub(mt5, empty_confirm=1)
    sub = hub.subscribe(
        symbol="XAUUSD", magic=MAGIC, direction="BUY", hold_when_empty=True,
    )

    await hub.sample()
    event = await sub.next_event()
    assert event.kind == mh.TICK
    assert event.positions == ()
    assert event.price == 2330.0

    mt5.quote = {"bid": 2331.0, "ask": 2331.2, "mid": 2331.1, "change": 0.0}
    await hub.sample()
    event = await sub.next_event()
    assert event.kind == mh.TICK
    assert event.price == 2331.0


async def test_hold_when_empty_false_still_emits_gone():
    """默认行为不变：见过持仓后清空仍发 GONE。"""
    mt5 = FakeMT5()
    mt5.add(ticket=1, magic=MAGIC)
    hub = _hub(mt5, empty_confirm=1)
    sub = hub.subscribe(
        symbol="XAUUSD", magic=MAGIC, direction="BUY", hold_when_empty=False,
    )

    await hub.sample()
    assert (await sub.next_event()).kind == mh.TICK

    mt5.clear()
    await hub.sample()
    assert (await sub.next_event()).kind == mh.GONE


# ---------------------------------------------------------------------------
# 挂单可见性：限价开仓在成交前只有挂单，误判收口会留下孤儿单
# ---------------------------------------------------------------------------

async def test_orders_not_read_unless_tracked():
    """市价链路一笔挂单都不会有，每轮多调一次 orders_get 纯属浪费。"""
    mt5 = FakeMT5()
    mt5.add(ticket=1, magic=MAGIC)
    hub = _hub(mt5)
    hub.subscribe(symbol="XAUUSD", magic=MAGIC, direction="BUY")

    await hub.sample()
    assert mt5.orders_calls == 0


async def test_tracked_subscription_reads_orders_once_per_round():
    mt5 = FakeMT5()
    mt5.add_order(ticket=51, magic=MAGIC)
    hub = _hub(mt5)
    hub.subscribe(symbol="XAUUSD", magic=MAGIC, direction="BUY", track_orders=True)
    hub.subscribe(symbol="XAUUSD", magic=OTHER_MAGIC, direction="BUY", track_orders=True)

    await hub.sample()
    assert mt5.orders_calls == 1  # 一轮一次，覆盖全部订阅者


async def test_resting_order_blocks_gone():
    """挂单还在盘上就说明任务在途：此时判收口会让挂单变成没人监控的孤儿单。"""
    mt5 = FakeMT5()
    mt5.add_order(ticket=51, magic=MAGIC)
    hub = _hub(mt5, empty_confirm=1)
    sub = hub.subscribe(symbol="XAUUSD", magic=MAGIC, direction="BUY", track_orders=True)

    for _ in range(5):
        await hub.sample()

    event = await sub.next_event()
    assert event.kind == mh.TICK
    assert event.positions == ()
    assert len(event.orders) == 1
    assert sub.empty_hits == 0


async def test_gone_after_order_cancelled_without_fill():
    """挂单被撤又没成交：见过挂单就算见过痕迹，此后空仓要能正常收口，否则占位泄漏。"""
    mt5 = FakeMT5()
    mt5.add_order(ticket=51, magic=MAGIC)
    hub = _hub(mt5, empty_confirm=1)
    sub = hub.subscribe(symbol="XAUUSD", magic=MAGIC, direction="BUY", track_orders=True)

    await hub.sample()
    assert (await sub.next_event()).kind == mh.TICK
    assert sub.seen is True

    mt5.clear_orders()
    await hub.sample()
    assert (await sub.next_event()).kind == mh.GONE


async def test_order_fill_changes_signature_and_emits_tick():
    """挂单成交后票号从挂单挪到持仓，签名必须变，否则订阅方看不到进场。"""
    mt5 = FakeMT5()
    mt5.add_order(ticket=51, magic=MAGIC)
    hub = _hub(mt5, empty_confirm=1)
    sub = hub.subscribe(symbol="XAUUSD", magic=MAGIC, direction="BUY", track_orders=True)

    await hub.sample()
    assert (await sub.next_event()).kind == mh.TICK

    mt5.clear_orders()
    mt5.add(ticket=51, magic=MAGIC)
    await hub.sample()
    event = await sub.next_event()
    assert event.kind == mh.TICK
    assert len(event.positions) == 1
    assert event.orders == ()


async def test_order_read_failure_emits_stale_not_gone():
    """读不到挂单不等于挂单没了：整轮退化为 STALE，绝不据此收口。"""
    mt5 = FakeMT5()
    mt5.add(ticket=1, magic=MAGIC)
    hub = _hub(mt5, interval=0.05, idle_interval=0.05, empty_confirm=1)
    sub = hub.subscribe(symbol="XAUUSD", magic=MAGIC, direction="BUY", track_orders=True)

    await hub.sample()
    assert (await sub.next_event()).kind == mh.TICK

    mt5.fail_orders = True
    mt5.clear()
    await asyncio.sleep(0.07)  # 越过 STALE 限流窗口
    await hub.sample()
    assert (await sub.next_event()).kind == mh.STALE
    assert sub.empty_hits == 0


async def test_other_magic_orders_do_not_keep_task_alive():
    """别的策略的挂单不能替本任务续命，否则收口永远等不到。"""
    mt5 = FakeMT5()
    mt5.add(ticket=1, magic=MAGIC)
    mt5.add_order(ticket=51, magic=OTHER_MAGIC)
    hub = _hub(mt5, empty_confirm=1)
    sub = hub.subscribe(symbol="XAUUSD", magic=MAGIC, direction="BUY", track_orders=True)

    await hub.sample()
    assert (await sub.next_event()).kind == mh.TICK

    mt5.clear()
    await hub.sample()
    assert (await sub.next_event()).kind == mh.GONE


async def test_missing_pending_orders_api_degrades_to_empty():
    """旧版客户端没有挂单能力：等价于没有挂单，不能整条链路报错。"""

    class Legacy(FakeMT5):
        pending_orders = None  # type: ignore[assignment]

    mt5 = Legacy()
    mt5.add(ticket=1, magic=MAGIC)
    hub = _hub(mt5, empty_confirm=1)
    sub = hub.subscribe(symbol="XAUUSD", magic=MAGIC, direction="BUY", track_orders=True)

    await hub.sample()
    assert (await sub.next_event()).kind == mh.TICK
