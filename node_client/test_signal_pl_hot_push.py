"""信号盈亏控制热推：止损重算 + 越价保留旧值（方案 A + 2b）。

覆盖：
- 止损配置指纹：热推改比例后必须重算止损（与仓位构成无关）
- 越价逐侧判定：旧值有效则保留旧值，旧值也越价则市价平仓
- 手动分散仓在场时止损仍然按配置算（止盈仍听手动仓权威）
"""
import asyncio

import market_hub as mh
from mock_mt5 import MockMT5Client

import signal_pl as sp
from strategy_runner import StrategyRunner

MAGIC = 900000001


class FakeHub:
    def __init__(self) -> None:
        self.sub: mh.Subscription | None = None

    def subscribe(self, *, symbol, magic, direction, hold_when_empty=False,
                  track_orders=False) -> mh.Subscription:
        self.sub = mh.Subscription(
            symbol=symbol, magic=magic, direction=direction,
            hold_when_empty=bool(hold_when_empty),
            track_orders=bool(track_orders),
        )
        return self.sub

    def unsubscribe(self, sub) -> None:
        self.unsubscribed = True
        sub.close()

    async def bar_metric(self, symbol: str, timeframe: str, metric: str) -> float:
        return 0.0

    async def bar_range_pair(
        self, symbol: str, timeframe: str, *, refresh: float = 5.0,
    ) -> tuple[float, float]:
        return (0.0, 0.0)


def _rule(**over) -> dict:
    rule = {
        "type": 1, "status": 1, "action": "all",
        "point": 100.0, "lot_times": 1.0, "extra_lot": 0.0,
        "max_allow_num": 3, "batch_enabled": False, "batch_levels": [],
        "float_pl_ratio": {
            "enabled": False, "ratio": -20.0, "monitor_mode": "loop", "max_times": 1,
        },
        "lot_pl_tiers": {
            "enabled": False, "batch_count": 2, "close_action": "all",
            "tiers": [
                {"min_lot": 0.1, "pl_amount": 50.0},
                {"min_lot": 0.5, "pl_amount": 100.0},
            ],
        },
    }
    rule.update(over)
    return rule


def _runner(sent: list, *, mt5=None, strategy=None) -> tuple[StrategyRunner, FakeHub]:
    hub = FakeHub()

    async def send(payload: dict) -> None:
        sent.append(payload)

    async def _exec(fn, *args):
        return fn(*args)

    runner = StrategyRunner(
        task_id=1, magic=MAGIC, group_id="g1", signal_id="s1",
        entry={"action": "BUY", "symbol": "XAUUSD", "volume": 0.1},
        strategy=strategy or {"rules": [_rule()]},
        mt5=mt5 or MockMT5Client(),
        hub=hub, exec_fn=_exec, send_fn=send, report_interval=1.0,
    )
    return runner, hub


async def _settle(times: int = 6) -> None:
    for _ in range(times):
        await asyncio.sleep(0)


def _of_type(sent: list, mtype: str) -> list[dict]:
    return [m["data"] for m in sent if m["type"] == mtype]


def _progress(sent: list, event: str) -> list[dict]:
    return [d for d in _of_type(sent, "strategy_progress") if d["event"] == event]


def _tick(hub: FakeHub, positions, price: float, *, bid=None, ask=None) -> None:
    hub.sub.offer(mh.MarketEvent(
        kind=mh.TICK, symbol="XAUUSD", magic=MAGIC,
        positions=tuple(positions), price=price, point=0.01,
        bid=price if bid is None else bid,
        ask=price if ask is None else ask,
    ))


def _float_pl_rule(*, enabled=True, ratio=-20.0) -> dict:
    return _rule(float_pl_ratio={
        "enabled": enabled, "ratio": ratio, "monitor_mode": "loop", "max_times": 1,
    })


# ----------------------------------------------------------------------
# 方案 A：止损配置指纹
# ----------------------------------------------------------------------
async def test_hot_push_ratio_recalculates_stop_without_book_change():
    """热推改比例：仓位构成没变，止损也要按新比例重算。"""
    sent: list = []
    mt5 = MockMT5Client()
    mt5.balance = 10000.0
    runner, hub = _runner(
        sent, mt5=mt5, strategy={"rules": [_float_pl_rule(ratio=-20.0)]},
    )
    runner.start()
    await _settle()
    held = [p for p in mt5._positions if int(p.get("magic") or 0) == MAGIC]
    open_px = held[0]["price_open"]
    _tick(hub, held, open_px)
    await _settle()
    assert mt5.positions_by_magic(MAGIC)[0]["sl"] == open_px - 200

    # 热推：比例从 -20 调到 -40（余额 10000 → 亏损金额 4000 → 距离翻倍）
    runner.apply_strategy_detailed({
        "template_id": "tpl_1",
        "rules": [_float_pl_rule(ratio=-40.0)],
    })
    _tick(hub, [p for p in mt5._positions if int(p.get("magic") or 0) == MAGIC], open_px)
    await _settle()

    pos = mt5.positions_by_magic(MAGIC)[0]
    assert pos["sl"] == open_px - 400, "改了比例后止损必须重算"
    runner.cancel()


async def test_hot_push_unrelated_field_keeps_stop():
    """热推改与止损无关的字段：止损价不动。"""
    sent: list = []
    mt5 = MockMT5Client()
    mt5.balance = 10000.0
    runner, hub = _runner(
        sent, mt5=mt5, strategy={"rules": [_float_pl_rule(ratio=-20.0)]},
    )
    runner.start()
    await _settle()
    held = [p for p in mt5._positions if int(p.get("magic") or 0) == MAGIC]
    open_px = held[0]["price_open"]
    _tick(hub, held, open_px)
    await _settle()
    before = mt5.positions_by_magic(MAGIC)[0]["sl"]

    runner.apply_strategy_detailed({
        "template_id": "tpl_1",
        "rules": [_float_pl_rule(ratio=-20.0)],
    })
    _tick(hub, [p for p in mt5._positions if int(p.get("magic") or 0) == MAGIC], open_px)
    await _settle()

    assert mt5.positions_by_magic(MAGIC)[0]["sl"] == before, "与止损无关的改动不该动止损"
    runner.cancel()


async def test_hot_push_keeps_stop_when_disabling_ratio():
    """关闭盈亏比：已有持仓上的止损价保留（不摘单）。"""
    sent: list = []
    mt5 = MockMT5Client()
    mt5.balance = 10000.0
    runner, hub = _runner(
        sent, mt5=mt5, strategy={"rules": [_float_pl_rule(ratio=-20.0)]},
    )
    runner.start()
    await _settle()
    held = [p for p in mt5._positions if int(p.get("magic") or 0) == MAGIC]
    open_px = held[0]["price_open"]
    _tick(hub, held, open_px)
    await _settle()
    before = mt5.positions_by_magic(MAGIC)[0]["sl"]
    assert before != 0

    runner.apply_strategy_detailed({
        "template_id": "tpl_1",
        "rules": [_float_pl_rule(enabled=False, ratio=-20.0)],
    })
    _tick(hub, [p for p in mt5._positions if int(p.get("magic") or 0) == MAGIC], open_px)
    await _settle()

    assert mt5.positions_by_magic(MAGIC)[0]["sl"] == before, "关开关不该摘掉已有止损"
    runner.cancel()


# ----------------------------------------------------------------------
# 方案 2b：越价保留旧值
# ----------------------------------------------------------------------
async def test_hot_push_crossed_stop_keeps_old_value_and_warns():
    """新止损价落在现价错误一侧、旧止损价仍有效 → 保留旧值，不强平，发告警。

    多单的止损在开仓价之下：把比例调小（止损变紧）后价格继续下跌，
    现价就越过了新的止损价——但旧止损价还没被越过。
    """
    sent: list = []
    mt5 = MockMT5Client()
    mt5.balance = 10000.0
    runner, hub = _runner(
        sent, mt5=mt5, strategy={"rules": [_float_pl_rule(ratio=-20.0)]},
    )
    runner.start()
    await _settle()
    held = [p for p in mt5._positions if int(p.get("magic") or 0) == MAGIC]
    open_px = held[0]["price_open"]
    _tick(hub, held, open_px)
    await _settle()
    old_sl = mt5.positions_by_magic(MAGIC)[0]["sl"]
    assert old_sl == open_px - 200

    # 比例调到 -1 → 止损距离 10 → 新止损价 open_px - 10
    runner.apply_strategy_detailed({
        "template_id": "tpl_1",
        "rules": [_float_pl_rule(ratio=-1.0)],
    })
    # 价格跌到 open_px - 20：已越过新止损价，没越过旧止损价
    _tick(hub, [p for p in mt5._positions if int(p.get("magic") or 0) == MAGIC], open_px - 20)
    await _settle()

    pos = mt5.positions_by_magic(MAGIC)[0]
    assert pos["sl"] == old_sl, "旧止损价仍有效，应保留"
    assert len(mt5.positions_by_magic(MAGIC)) == 1, "不该被市价平掉"

    kept = [d for d in _progress(sent, "signal_pl") if d.get("detail", {}).get("status") == "kept"]
    assert kept, "应发出保留旧值的告警"
    assert kept[0]["detail"]["kept_side"] == "sl"
    assert kept[0]["detail"]["market"]
    runner.cancel()


async def test_hot_push_crossed_stop_with_old_also_crossed_closes():
    """旧止损价也已被现价越过 → 价格事件，市价平仓（回归现状）。"""
    sent: list = []
    mt5 = MockMT5Client()
    mt5.balance = 10000.0
    runner, hub = _runner(
        sent, mt5=mt5, strategy={"rules": [_float_pl_rule(ratio=-20.0)]},
    )
    runner.start()
    await _settle()
    held = [p for p in mt5._positions if int(p.get("magic") or 0) == MAGIC]
    open_px = held[0]["price_open"]
    _tick(hub, held, open_px)
    await _settle()
    assert mt5.positions_by_magic(MAGIC)[0]["sl"] == open_px - 200

    runner.apply_strategy_detailed({
        "template_id": "tpl_1",
        "rules": [_float_pl_rule(ratio=-1.0)],
    })
    # 价格跌到 open_px - 250：旧止损价 open_px - 200 也被越过了
    _tick(hub, [p for p in mt5._positions if int(p.get("magic") or 0) == MAGIC], open_px - 250)
    await _settle()

    assert mt5.positions_by_magic(MAGIC) == [], "旧止损已被越过，应市价平仓"
    runner.cancel()


async def test_kept_stays_kept_on_repeat_sync():
    """保留旧值后再次进入同步循环：仍然保留，不误强平。"""
    sent: list = []
    mt5 = MockMT5Client()
    mt5.balance = 10000.0
    runner, hub = _runner(
        sent, mt5=mt5, strategy={"rules": [_float_pl_rule(ratio=-20.0)]},
    )
    runner.start()
    await _settle()
    held = [p for p in mt5._positions if int(p.get("magic") or 0) == MAGIC]
    open_px = held[0]["price_open"]
    _tick(hub, held, open_px)
    await _settle()
    old_sl = mt5.positions_by_magic(MAGIC)[0]["sl"]

    runner.apply_strategy_detailed({
        "template_id": "tpl_1",
        "rules": [_float_pl_rule(ratio=-1.0)],
    })
    for _ in range(3):
        _tick(hub, [p for p in mt5._positions if int(p.get("magic") or 0) == MAGIC], open_px - 20)
        await _settle()
        assert len(mt5.positions_by_magic(MAGIC)) == 1, "重复同步不该误平仓"

    assert mt5.positions_by_magic(MAGIC)[0]["sl"] == old_sl
    runner.cancel()


async def test_kept_records_old_price_as_level():
    """保留旧值后：落价记录的是持仓上的实际价，不会按被拒的新价去平仓。"""
    sent: list = []
    mt5 = MockMT5Client()
    mt5.balance = 10000.0
    runner, hub = _runner(
        sent, mt5=mt5, strategy={"rules": [_float_pl_rule(ratio=-20.0)]},
    )
    runner.start()
    await _settle()
    held = [p for p in mt5._positions if int(p.get("magic") or 0) == MAGIC]
    open_px = held[0]["price_open"]
    _tick(hub, held, open_px)
    await _settle()
    old_sl = mt5.positions_by_magic(MAGIC)[0]["sl"]

    runner.apply_strategy_detailed({
        "template_id": "tpl_1",
        "rules": [_float_pl_rule(ratio=-1.0)],
    })
    _tick(hub, [p for p in mt5._positions if int(p.get("magic") or 0) == MAGIC], open_px - 20)
    await _settle()

    levels = runner._lot_sltp_levels.get("BUY")
    assert levels, "应记下落价"
    assert levels[0] == old_sl, "落价应记持仓上的实际旧价，不是被拒的新价"
    runner.cancel()


# ----------------------------------------------------------------------
# 手动分散仓在场
# ----------------------------------------------------------------------
async def test_manual_tp_authority_still_writes_stop_on_hot_push():
    """手动仓在场：止盈听手动仓权威，止损仍按热推后的新比例算。"""
    sent: list = []
    mt5 = MockMT5Client()
    mt5.balance = 10000.0
    runner, hub = _runner(
        sent, mt5=mt5, strategy={"rules": [_float_pl_rule(ratio=-20.0)]},
    )
    runner.start()
    await _settle()
    held = [p for p in mt5._positions if int(p.get("magic") or 0) == MAGIC]
    open_px = held[0]["price_open"]
    _tick(hub, held, open_px)
    await _settle()
    assert mt5.positions_by_magic(MAGIC)[0]["sl"] == open_px - 200

    # 手动仓成交 → 止盈权威在场
    runner._manual_tp_authority = {1: open_px + 50}
    runner.apply_strategy_detailed({
        "template_id": "tpl_1",
        "rules": [_float_pl_rule(ratio=-40.0)],
    })
    _tick(hub, [p for p in mt5._positions if int(p.get("magic") or 0) == MAGIC], open_px)
    await _settle()

    pos = mt5.positions_by_magic(MAGIC)[0]
    assert pos["sl"] == open_px - 400, "手动仓在场时止损仍应按新比例重算"
    runner.cancel()


# ----------------------------------------------------------------------
# 纯函数层
# ----------------------------------------------------------------------
def test_crossed_sides_reports_both():
    # 多单：止损在现价之上（价格已跌穿）算越价，止盈在现价之下（价格已涨过）算越价
    got = sp.crossed_sides("BUY", 4420.0, 4400.0, 4410.0)
    assert got == {"tp", "sl"}
    assert sp.crossed_sides("BUY", 4400.0, 4420.0, 4410.0) == set()
    # 空单反过来
    assert sp.crossed_sides("SELL", 4400.0, 4420.0, 4410.0) == {"tp", "sl"}


def test_plan_common_stops_carries_market():
    plan = sp.plan_common_stops(
        [{"type": "BUY", "volume": 0.1, "price_open": 4400.0}],
        tp_tiers={
            "enabled": True,
            "tiers": [{"min_lot": 0.0, "pl_amount": 10.0}],
        },
        tick_size=0.01, tick_value=1.0, digits=2,
        bid=4400.0, ask=4400.0, fallback_price=4400.0,
    )[0]
    assert plan["market"] == 4400.0
    assert plan["crossed_sides"] == []


def test_stop_side_decision_keep_vs_close():
    pos = [{"type": "BUY", "volume": 0.1, "price_open": 4400.0, "sl": 4390.0, "tp": 4420.0}]
    # 新值没越价 → 写
    assert sp.stop_side_decision(
        "BUY", "sl", new_value=4395.0, positions=pos, market=4400.0,
    )["action"] == "write"
    # 新值越价（止损落在现价之上）、旧值没越价 → 保留旧值
    dec = sp.stop_side_decision(
        "BUY", "sl", new_value=4450.0, positions=pos, market=4400.0,
    )
    assert dec["action"] == "keep"
    assert dec["old_value"] == 4390.0
    # 旧值也越价 → 平仓
    dec = sp.stop_side_decision(
        "BUY", "sl", new_value=4450.0, positions=pos, market=4350.0,
    )
    assert dec["action"] == "close"
    # 没有旧值 → 平仓
    dec = sp.stop_side_decision(
        "BUY", "sl", new_value=4450.0, positions=[], market=4400.0,
    )
    assert dec["action"] == "close"


def test_stop_side_decision_allow_keep_false_always_closes():
    """allow_keep=False（加仓场景）：新值越价一律平掉，不走 2b。"""
    pos = [{"type": "BUY", "volume": 0.1, "price_open": 4400.0, "sl": 4390.0, "tp": 4420.0}]
    dec = sp.stop_side_decision(
        "BUY", "tp", new_value=4300.0, positions=pos, market=4400.0, allow_keep=False,
    )
    assert dec["action"] == "close"


# ----------------------------------------------------------------------
# 加仓路径：重算出的价越价仍然平仓（2b 只针对配置改动）
# ----------------------------------------------------------------------
async def test_addon_recomputed_tp_crossed_still_closes():
    """加仓后重算出的止盈落在现价之下 → 市价平仓（既有口径不变）。"""
    sent: list = []
    mt5 = MockMT5Client()
    mt5.balance = 10000.0
    runner, hub = _runner(
        sent, mt5=mt5, strategy={"rules": [_addon_pl_rule(lot_pl_tiers=_tiers())]},
    )
    runner.start()
    await _settle()
    held = [p for p in mt5._positions if int(p.get("magic") or 0) == MAGIC]
    open_px = held[0]["price_open"]
    _tick(hub, held, open_px)
    await _settle()

    # 加一手，价格已经涨过重算出来的止盈
    mt5.place_market_order("XAUUSD", "BUY", 0.1, magic=MAGIC)
    _tick(hub, [p for p in mt5._positions if int(p.get("magic") or 0) == MAGIC], open_px + 5)
    await _settle()

    assert mt5.positions_by_magic(MAGIC) == [], "加仓后的越价应平仓"
    runner.cancel()


# ----------------------------------------------------------------------
# 止盈配置热推
# ----------------------------------------------------------------------
def _tiers(**over) -> dict:
    tiers = {
        "enabled": True, "batch_count": 2, "close_action": "all",
        "tiers": [
            {"min_lot": 0.1, "pl_amount": 10.0},
            {"min_lot": 0.5, "pl_amount": 20.0},
        ],
    }
    tiers.update(over)
    return tiers


def _addon_pl_rule(**over) -> dict:
    rule = _rule()
    rule.update(over)
    rule.setdefault("lot_pl_tiers", _tiers())
    return rule


async def test_hot_push_tiers_amount_recalculates_tp():
    """热推改批次1金额：共同止盈按新金额重算。"""
    sent: list = []
    mt5 = MockMT5Client()
    runner, hub = _runner(
        sent, mt5=mt5,
        strategy={"rules": [_addon_pl_rule(lot_pl_tiers=_tiers())]},
    )
    runner.start()
    await _settle()
    held = [p for p in mt5._positions if int(p.get("magic") or 0) == MAGIC]
    open_px = held[0]["price_open"]
    _tick(hub, held, open_px)
    await _settle()
    # D = 10 / ((1/0.01) * 0.1) = 1
    assert mt5.positions_by_magic(MAGIC)[0]["tp"] == open_px + 1

    runner.apply_strategy_detailed({
        "template_id": "tpl_1",
        "rules": [_addon_pl_rule(lot_pl_tiers=_tiers(tiers=[
            {"min_lot": 0.1, "pl_amount": 20.0},
            {"min_lot": 0.5, "pl_amount": 20.0},
        ]))],
    })
    _tick(hub, [p for p in mt5._positions if int(p.get("magic") or 0) == MAGIC], open_px)
    await _settle()

    # D = 20 / ((1/0.01) * 0.1) = 2
    assert mt5.positions_by_magic(MAGIC)[0]["tp"] == open_px + 2, "改了金额要重算止盈"
    runner.cancel()
