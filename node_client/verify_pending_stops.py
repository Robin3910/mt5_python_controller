"""查证：逆势限价挂单时，MT5 挂单上是否带了止损止盈。

结论要看挂出去那一刻 pending order 的 sl / tp 字段，
以及它是按「预计成交价」还是「现价」算出来的。
"""
import asyncio
import sys

sys.path.insert(0, ".")

import market_hub as mh
from mock_mt5 import MockMT5Client
from strategy_runner import StrategyRunner

MAGIC = 900000001


class FakeHub:
    def __init__(self) -> None:
        self.sub = None

    def subscribe(self, **kw):
        self.sub = mh.Subscription(
            symbol=kw.get("symbol"), magic=kw.get("magic"),
            direction=kw.get("direction"),
        )
        return self.sub

    def unsubscribe(self, sub) -> None:
        pass

    async def bar_metric(self, symbol, timeframe, metric):
        return 0.0

    async def bar_range_pair(self, symbol, timeframe, *, refresh=5.0):
        return (0.0, 0.0)


async def _settle(n=8):
    for _ in range(n):
        await asyncio.sleep(0)


async def main() -> None:
    mt5 = MockMT5Client()
    sent = []

    async def send(payload):
        sent.append(payload)

    async def _exec(fn, *args):
        return fn(*args)

    rule = {
        "type": 1, "status": 1, "action": "all",
        "point": 100.0, "lot_times": 1.0, "extra_lot": 0.0,
        "max_allow_num": 3, "batch_enabled": False, "batch_levels": [],
        # 打开「分档止盈」：批次 1 的 pl_amount = 50 美元
        "lot_pl_tiers": {
            "enabled": True, "batch_count": 1, "close_action": "all",
            "tiers": [{"min_lot": 0.1, "pl_amount": 50.0}],
        },
    }

    runner = StrategyRunner(
        task_id=1, magic=MAGIC, group_id="g1", signal_id="s1",
        entry={"action": "BUY", "symbol": "XAUUSD", "volume": 0.1},
        strategy={"rules": [rule]},
        mt5=mt5, hub=FakeHub(), exec_fn=_exec, send_fn=send,
        report_interval=1.0,
    )
    runner.start()
    await _settle()

    held = mt5.positions_by_magic(MAGIC)
    open_px = held[0]["price_open"]
    print(f"首仓开仓价 = {open_px}")
    print(f"当前价 bid/ask = {mt5.prices_map.get('XAUUSD')}")

    hub = runner._sub
    hub.offer(mh.MarketEvent(
        kind=mh.TICK, symbol="XAUUSD", magic=MAGIC,
        positions=tuple(held), price=open_px, point=0.01,
    ))
    await _settle()

    pend = mt5.pending_orders_by_magic(MAGIC)
    print(f"\n在途挂单数 = {len(pend)}")
    for o in pend:
        print(f"  ticket={o['ticket']} type={o['type']} price={o['price_open']} "
              f"vol={o['volume']} sl={o.get('sl')} tp={o.get('tp')} comment={o.get('comment')}")

    print("\n--- 结论 ---")
    if pend:
        o = pend[0]
        if o.get("sl") or o.get("tp"):
            print("挂单上带了止损止盈")
            print(f"  sl={o.get('sl')}  tp={o.get('tp')}")
            print(f"  挂单价={o['price_open']}")
            if o.get("tp"):
                print(f"  tp - 挂单价 = {round(o['tp'] - o['price_open'], 5)}")
        else:
            print("挂单上没有止损止盈")

    runner.cancel()


if __name__ == "__main__":
    asyncio.run(main())
