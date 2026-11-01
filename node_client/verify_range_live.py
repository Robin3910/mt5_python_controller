"""手工验证：模拟急跌场景下，逆势限价被实时波幅推远。

不参与 pytest（脚本名不匹配 test_*），用 python verify_range_live.py 直接跑。
"""
import asyncio

from mock_mt5 import MockMT5Client
from strategy_runner import StrategyRunner

MAGIC = 900000001


class Hub:
    """最小 hub 替身：只提供订阅与实时波幅读取。"""

    def __init__(self):
        self.sub = None
        self.pairs = {}
        self.calls = []

    def subscribe(self, *, symbol, magic, direction, hold_when_empty=False,
                  track_orders=False):
        import market_hub as mh
        self.sub = mh.Subscription(
            symbol=symbol, magic=magic, direction=direction,
            hold_when_empty=hold_when_empty, track_orders=track_orders,
        )
        return self.sub

    def unsubscribe(self, sub):
        sub.close()

    async def bar_metric(self, symbol, timeframe, metric):
        return 0.0

    async def bar_range_pair(self, symbol, timeframe, *, refresh=5.0):
        self.calls.append((timeframe, refresh))
        return self.pairs.get(timeframe, (0.0, 0.0))


def tick(hub, positions, price):
    import market_hub as mh
    hub.sub.offer(mh.MarketEvent(
        kind=mh.TICK, symbol="XAUUSD", magic=MAGIC,
        positions=tuple(positions), price=price, point=0.01,
    ))


async def settle(times=6):
    for _ in range(times):
        await asyncio.sleep(0)


async def main():
    sent = []
    mt5 = MockMT5Client()
    hub = Hub()

    strategy = {"rules": [{
        "type": 1, "status": 1, "action": "all",
        "point": 100.0, "lot_times": 1.0, "extra_lot": 0.0,
        "max_allow_num": 3, "batch_enabled": True, "batch_count": 1,
        "total_lot_limit": 4,
        "batch_levels": [{
            "pos_from": 1, "pos_to": 4, "calc_type": "range", "timeframe": "M5",
            "range_include_live": True, "range_live_cap": 1000.0,
            "range_live_refresh": 5,
            "lot_times": 1.0, "extra_lot": 0.0,
        }],
    }]}

    async def send(payload):
        sent.append(payload)

    async def _exec(fn, *args):
        return fn(*args)

    runner = StrategyRunner(
        task_id=1, magic=MAGIC, group_id="g1", signal_id="s1",
        entry={"action": "BUY", "symbol": "XAUUSD", "volume": 0.1},
        strategy=strategy, mt5=mt5, hub=hub, exec_fn=_exec, send_fn=send,
        report_interval=1.0,
    )
    runner.start()
    await settle()

    open_px = mt5.positions_by_magic(MAGIC)[0]["price_open"]
    print(f"open price = {open_px}")

    # T0: flat market, baseline range 30 points (3000 points)
    hub.pairs["M5"] = (0.30, 0.05)
    tick(hub, mt5.positions_by_magic(MAGIC), open_px)
    await settle()
    pend = mt5.pending_orders_by_magic(MAGIC)
    print(f"T0 flat   : limit = {pend[0]['price_open']:.2f} "
          f"(gap {open_px - pend[0]['price_open']:.2f})")

    # T1: crash, this M5 bar has moved 60 points
    hub.pairs["M5"] = (0.30, 60.0)
    tick(hub, mt5.positions_by_magic(MAGIC), open_px)
    await settle()
    pend = mt5.pending_orders_by_magic(MAGIC)
    print(f"T1 crash  : limit = {pend[0]['price_open']:.2f} "
          f"(gap {open_px - pend[0]['price_open']:.2f})")

    # T2: deeper crash, moved 120 points
    # reprice is rate-limited by range_live_refresh (5s) -- wait it out
    await asyncio.sleep(5.2)
    hub.pairs["M5"] = (0.30, 120.0)
    tick(hub, mt5.positions_by_magic(MAGIC), open_px)
    await settle()
    pend = mt5.pending_orders_by_magic(MAGIC)
    print(f"T2 deeper : limit = {pend[0]['price_open']:.2f} "
          f"(gap {open_px - pend[0]['price_open']:.2f})")

    # T3: crash eases off, live range shrinks back -- limit should NOT come back
    # (pull-back uses a larger dead zone and 2x interval)
    await asyncio.sleep(5.2)
    hub.pairs["M5"] = (0.30, 10.0)
    tick(hub, mt5.positions_by_magic(MAGIC), open_px)
    await settle()
    pend = mt5.pending_orders_by_magic(MAGIC)
    print(f"T3 easing : limit = {pend[0]['price_open']:.2f} "
          f"(gap {open_px - pend[0]['price_open']:.2f})")

    print(f"\nlive range reads = {len(hub.calls)}, args = {hub.calls}")

    placed = [d for d in sent if d.get("event") == "cnt_limit"]
    if placed:
        detail = placed[-1].get("detail") or {}
        print("\nlast level calculation breakdown:")
        for k in ("gap_source", "baseline_range", "live_range",
                  "effective_range", "range_live_adapted", "range_live_clamped"):
            print(f"  {k} = {detail.get(k)}")

    runner.cancel()


asyncio.run(main())
