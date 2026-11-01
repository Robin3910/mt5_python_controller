"""查证：波幅逆势限价成交后，已开仓位是否被补上止损止盈。"""
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
    mt5.auto_fill_pending = False
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
    hub.pairs["M5"] = (0.30, 0.05)
    tick(hub, mt5.positions_by_magic(MAGIC), open_px)
    await settle()

    pend = mt5.pending_orders_by_magic(MAGIC)
    print(f"pending: price={pend[0]['price_open']} sl={pend[0]['sl']} tp={pend[0]['tp']}")

    # 价格压到挂单价下方，限价成交
    fill_px = pend[0]["price_open"]
    mt5.prices_map["XAUUSD"] = fill_px - 0.5
    tick(hub, mt5.positions_by_magic(MAGIC), fill_px - 0.5)
    await settle()
    await settle()

    for p in mt5.positions_by_magic(MAGIC):
        print(f"pos ticket={p['ticket']} price={p['price_open']} vol={p['volume']} "
              f"sl={p['sl']} tp={p['tp']} comment={p['comment']}")
    pend = mt5.pending_orders_by_magic(MAGIC)
    print(f"pending after fill: {len(pend)}")
    for o in pend:
        print(f"  ticket={o['ticket']} price={o['price_open']} sl={o['sl']} tp={o['tp']}")

    runner.cancel()


asyncio.run(main())
