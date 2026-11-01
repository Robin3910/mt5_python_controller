"""查证：波幅逆势限价挂单上是否带止损止盈，以及监控中是否会给挂单补止损止盈。"""
import asyncio

from mock_mt5 import MockMT5Client
from strategy_runner import StrategyRunner

MAGIC = 900000001


class Hub:
    def __init__(self):
        self.sub = None
        self.pairs = {}

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
        return self.pairs.get(timeframe, (0.0, 0.0))


def tick(hub, positions, price):
    import market_hub as mh
    hub.sub.offer(mh.MarketEvent(
        kind=mh.TICK, symbol="XAUUSD", magic=MAGIC,
        positions=tuple(positions), price=price, point=0.01,
    ))


async def settle(times=8):
    for _ in range(times):
        await asyncio.sleep(0)


async def main():
    sent = []
    mt5 = MockMT5Client()
    mt5.balance = 10000.0
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
        "float_pl_ratio": {
            "enabled": True, "ratio": -20, "monitor_mode": "loop", "max_times": 1,
        },
        "lot_pl_tiers": {
            "enabled": True, "batch_count": 1, "close_action": "all",
            "tiers": [{"min_lot": 0.1, "pl_amount": 50.0}],
        },
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
    print(f"open price = {open_px}, first pos sl={mt5.positions_by_magic(MAGIC)[0]['sl']} "
          f"tp={mt5.positions_by_magic(MAGIC)[0]['tp']}")

    hub.pairs["M5"] = (0.30, 0.05)
    tick(hub, mt5.positions_by_magic(MAGIC), open_px)
    await settle()

    pend = mt5.pending_orders_by_magic(MAGIC)
    print(f"pending count = {len(pend)}")
    for o in pend:
        print(f"  ticket={o['ticket']} price={o['price_open']} vol={o['volume']} "
              f"sl={o.get('sl')} tp={o.get('tp')} comment={o.get('comment')}")

    # 监控多拍：看挂单的 sl/tp 是否被改
    for i in range(3):
        await asyncio.sleep(5.2)
        hub.pairs["M5"] = (0.30, 60.0 * (i + 1))
        tick(hub, mt5.positions_by_magic(MAGIC), open_px)
        await settle()
        pend = mt5.pending_orders_by_magic(MAGIC)
        for o in pend:
            print(f"  after tick {i}: price={o['price_open']} sl={o.get('sl')} tp={o.get('tp')}")

    runner.cancel()


asyncio.run(main())
