"""模版1 手动分散仓：手数公式、到价判定、与分批隔离、每规则一次。"""
import asyncio

import market_hub as mh
import manual_scatter as ms
from mock_mt5 import MockMT5Client
from strategy_rules import PositionCtx, evaluate
from strategy_runner import StrategyRunner

MAGIC = 900000001


def _gold_tiers(pl_lo=500.0, pl_hi=800.0):
    return {
        "enabled": True,
        "tiers": [
            {"min_lot": 0.1, "pl_amount": pl_lo},
            {"min_lot": 0.5, "pl_amount": pl_hi},
        ],
    }


def _cfg(**over):
    cfg = {
        "enabled": True,
        "entry_price": 4400.0,
        "take_profit": 4410.0,
        "stop_loss": 0.0,
        "volume": 0.0,
        "volume_locked": False,
    }
    cfg.update(over)
    return cfg


def test_describe_open_explains_locked_and_formula_volume():
    watch = {
        "rule_type": 2, "direction": "BUY", "comment": "M2",
        "cfg": _cfg(volume=0.4, volume_locked=True),
    }
    locked = ms.describe_open(
        watch, 0.4, target_pl=100.0, current_lot_size=0.79,
        profit_per_lot_value=2900.0, volume_step=0.01,
    )
    assert "手填锁定 0.4" in locked
    assert "不按分档反推" in locked
    assert "分档目标" not in locked

    formula = ms.describe_open(
        {"rule_type": 2, "direction": "BUY", "cfg": _cfg()},
        0.5, target_pl=500.0, current_lot_size=0.1,
        profit_per_lot_value=1000.0, volume_step=0.01,
    )
    assert "分档目标 500u" in formula
    assert "每手止盈 1000u" in formula
    assert "当前仓 0.1 手命中该档" in formula
    detail = ms.open_detail(
        watch, 0.4, target_pl=100.0, current_lot_size=0.79,
        profit_per_lot_value=2900.0,
    )
    assert detail["volume_locked"] is True
    assert detail["target_pl"] is None
    assert "手填锁定" in detail["volume_formula"]


def test_gold_example_4400_4410_500u_is_half_lot():
    """XAUUSD 价差 10、tick 0.01/1 → 每手 1000u；目标 500u → 0.5 手。"""
    vol = ms.compute_volume(
        cfg=_cfg(),
        current_lot_size=0.1,
        lot_pl_tiers=_gold_tiers(),
        tick_size=0.01,
        tick_value=1.0,
    )
    assert vol == 0.5


def test_match_target_pl_picks_highest_qualifying_tier():
    assert ms.match_target_pl(_gold_tiers(), 0.1) == 500.0
    assert ms.match_target_pl(_gold_tiers(), 0.5) == 800.0
    assert ms.match_target_pl(_gold_tiers(), 0.09) is None
    assert ms.match_target_pl({"enabled": False, "tiers": [{"min_lot": 0.1, "pl_amount": 500}]}, 1.0) is None


def test_ceil_volume_rounds_up_to_step():
    vol = ms.compute_volume(
        cfg=_cfg(),
        current_lot_size=0.1,
        lot_pl_tiers=_gold_tiers(pl_lo=501.0),
        tick_size=0.01,
        tick_value=1.0,
    )
    # 501 / 1000 = 0.501 → 上取整到 0.51
    assert vol == 0.51


def test_locked_volume_skips_formula():
    vol = ms.compute_volume(
        cfg=_cfg(volume=0.2, volume_locked=True),
        current_lot_size=0.1,
        lot_pl_tiers=_gold_tiers(),
        tick_size=0.01,
        tick_value=1.0,
    )
    assert vol == 0.2


def test_no_target_without_volume_returns_none():
    assert ms.compute_volume(
        cfg=_cfg(),
        current_lot_size=0.1,
        lot_pl_tiers={"enabled": False, "tiers": []},
        tick_size=0.01,
        tick_value=1.0,
    ) is None


def test_price_reached_buy_and_sell_including_already_past():
    assert ms.direction_from_prices(4400, 4410) == "BUY"
    assert ms.direction_from_prices(4400, 4390) == "SELL"
    assert ms.price_reached("BUY", 4400, 4400) is True
    assert ms.price_reached("BUY", 4401, 4400) is True
    assert ms.price_reached("BUY", 4399, 4400) is False
    assert ms.price_reached("SELL", 4400, 4400) is True
    assert ms.price_reached("SELL", 4399, 4400) is True
    assert ms.price_reached("SELL", 4401, 4400) is False


def test_comment_is_m_prefix_not_r3b():
    assert ms.comment_for(1) == "M1"
    assert ms.comment_for(2) == "M2"
    assert ms.parse_rule_type("M1") == 1
    assert ms.parse_rule_type("M2 extra") == 2
    assert ms.parse_rule_type("R3B") is None
    assert ms.parse_rule_type("M10") is None
    assert not str(ms.comment_for(1)).startswith("R3B")
    assert not str(ms.comment_for(2)).startswith("R3B")


def test_exclude_manual_does_not_change_batch_position_count():
    first = {"ticket": 1, "comment": "S1", "volume": 0.1, "price_open": 2330, "time": 1}
    manual = {"ticket": 2, "comment": "M2", "volume": 0.5, "price_open": 4400, "time": 2}
    filtered = ms.exclude_manual([first, manual])
    assert len(filtered) == 1
    assert filtered[0]["ticket"] == 1
    ctx = PositionCtx(
        direction="BUY",
        position_count=len(filtered),
        base_volume=0.1,
        base_price=2330.0,
        counter_base_price=2330.0,
        price=4400.0,
        point=0.01,
        add_count=0,
    )
    rules = [{
        "type": 2, "status": 1, "action": "all",
        "batch_enabled": True, "batch_action": "all",
        "total_lot_limit": 10,
        "batch_levels": [{
            "pos_from": 3, "pos_to": 3, "calc_type": "point",
            "point": 1, "lot_times": 1, "extra_lot": 0,
        }],
    }]
    # 过滤后持仓 1 笔 → 下一笔是第 2 笔，档位 3~3 不命中
    assert evaluate(rules, ctx) is None
    # 若把手动仓算进去，position_count=2 → 下一笔第 3 笔会命中
    ctx_wrong = PositionCtx(**{**ctx.__dict__, "position_count": 2})
    assert evaluate(rules, ctx_wrong) is not None


def test_extract_watches_skips_disabled_rule_and_one_per_type():
    rules = [
        {"type": 1, "status": 0, "manual_scatter": _cfg()},
        {"type": 2, "status": 1, "manual_scatter": _cfg(), "lot_pl_tiers": _gold_tiers()},
        {"type": 2, "status": 1, "manual_scatter": _cfg(entry_price=1, take_profit=2)},
    ]
    watches = ms.extract_watches(rules)
    assert len(watches) == 1
    assert watches[0]["rule_type"] == 2
    assert watches[0]["comment"] == "M2"


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
        sub.close()

    async def bar_metric(self, symbol: str, timeframe: str, metric: str) -> float:
        return 0.0


def _trend_rule(**over) -> dict:
    rule = {
        "type": 2, "status": 1, "action": "all",
        "point": 1e9, "lot_times": 1.0, "extra_lot": 0.0,
        "max_allow_num": 3, "batch_enabled": False, "batch_levels": [],
        "lot_pl_tiers": _gold_tiers(),
        "manual_scatter": _cfg(),
    }
    rule.update(over)
    return rule


def _runner(sent: list, *, mt5=None, strategy=None, runtime=None) -> tuple[StrategyRunner, FakeHub]:
    hub = FakeHub()

    async def send(payload: dict) -> None:
        sent.append(payload)

    async def _exec(fn, *args):
        return fn(*args)

    runner = StrategyRunner(
        task_id=1, magic=MAGIC, group_id="g1", signal_id="s1",
        entry={"action": "BUY", "symbol": "XAUUSD", "volume": 0.1},
        strategy=strategy or {"template_id": "tpl_1", "rules": [_trend_rule()]},
        mt5=mt5 or MockMT5Client(),
        hub=hub, exec_fn=_exec, send_fn=send, report_interval=1.0,
        runtime=runtime,
    )
    return runner, hub


async def _settle(times: int = 5) -> None:
    for _ in range(times):
        await asyncio.sleep(0)


def _progress(sent: list, event: str) -> list[dict]:
    return [m["data"] for m in sent if m["type"] == "strategy_progress" and m["data"].get("event") == event]


async def test_runner_opens_market_once_with_m2_comment():
    sent: list = []
    mt5 = MockMT5Client()
    runner, hub = _runner(sent, mt5=mt5)
    runner.start()
    await _settle()

    mt5.prices_map["XAUUSD"] = 4400.0
    held = mt5.positions_by_magic(MAGIC)
    hub.sub.offer(mh.MarketEvent(
        kind=mh.TICK, symbol="XAUUSD", magic=MAGIC,
        positions=tuple(held), price=4400.0, point=0.01,
    ))
    await _settle()

    manuals = [p for p in mt5.positions_by_magic(MAGIC) if str(p.get("comment") or "").startswith("M")]
    assert len(manuals) == 1
    assert manuals[0]["comment"] == "M2"
    assert manuals[0]["volume"] == 0.5
    assert manuals[0]["tp"] == 4410.0
    assert not str(manuals[0]["comment"]).startswith("R3B")
    assert runner.add_count == 0
    assert runner.total_orders == 1
    assert runner.total_volume == 0.6
    ev = _progress(sent, "add_manual")
    assert ev and ev[0]["detail"]["kind"] == "manual_scatter"
    assert "分档目标" in (ev[0].get("message") or "")
    assert "每手止盈" in (ev[0].get("message") or "")
    assert "手数 =" in (ev[0]["detail"].get("volume_formula") or "")
    assert ev[0].get("runtime", {}).get("manual_fired") == [2]

    hub.sub.offer(mh.MarketEvent(
        kind=mh.TICK, symbol="XAUUSD", magic=MAGIC,
        positions=tuple(mt5.positions_by_magic(MAGIC)), price=4400.0, point=0.01,
    ))
    await _settle()
    assert len([p for p in mt5.positions_by_magic(MAGIC) if str(p.get("comment") or "").startswith("M")]) == 1
    runner.cancel()


async def test_runner_already_past_entry_opens_immediately():
    sent: list = []
    mt5 = MockMT5Client()
    mt5.prices_map["XAUUSD"] = 4415.0
    runner, hub = _runner(sent, mt5=mt5)
    runner.start()
    await _settle()
    held = mt5.positions_by_magic(MAGIC)
    hub.sub.offer(mh.MarketEvent(
        kind=mh.TICK, symbol="XAUUSD", magic=MAGIC,
        positions=tuple(held), price=4415.0, point=0.01,
    ))
    await _settle()
    manuals = [p for p in mt5.positions_by_magic(MAGIC) if p.get("comment") == "M2"]
    assert len(manuals) == 1
    runner.cancel()


async def test_manual_positions_do_not_inflate_batch_position_count():
    sent: list = []
    mt5 = MockMT5Client()
    rule = _trend_rule(
        batch_enabled=True,
        batch_action="all",
        total_lot_limit=10,
        batch_levels=[{
            "pos_from": 3, "pos_to": 3, "calc_type": "point",
            "point": 1, "lot_times": 1, "extra_lot": 0,
        }],
    )
    runner, hub = _runner(sent, mt5=mt5, strategy={"template_id": "tpl_1", "rules": [rule]})
    runner.start()
    await _settle()
    mt5.prices_map["XAUUSD"] = 4400.0
    hub.sub.offer(mh.MarketEvent(
        kind=mh.TICK, symbol="XAUUSD", magic=MAGIC,
        positions=tuple(mt5.positions_by_magic(MAGIC)), price=4400.0, point=0.01,
    ))
    await _settle()
    assert runner.add_count == 0
    hub.sub.offer(mh.MarketEvent(
        kind=mh.TICK, symbol="XAUUSD", magic=MAGIC,
        positions=tuple(mt5.positions_by_magic(MAGIC)), price=4400.0, point=0.01,
    ))
    await _settle()
    assert runner.add_count == 0
    assert len(mt5.positions_by_magic(MAGIC)) == 2
    runner.cancel()


def test_apply_strategy_refuses_grid_and_keeps_fired():
    sent: list = []
    runner, _hub = _runner(sent)
    runner._manual_fired.add(2)
    grid = {
        "type": 4, "status": 1, "action": "all",
        "price_lower": 2300, "price_upper": 2400, "grid_count": 10,
        "grid_mode": "arithmetic", "grid_side": "long", "lot_per_grid": 0.01,
    }
    assert runner.apply_strategy({"rules": [grid]}) is False
    assert runner._manual_fired == {2}
    ok = runner.apply_strategy({
        "template_id": "tpl_1",
        "rules": [_trend_rule(manual_scatter=_cfg(entry_price=4500, take_profit=4510))],
    })
    assert ok is True
    assert runner._manual_fired == {2}
    watches = ms.extract_watches(runner.strategy.get("rules"))
    assert watches[0]["cfg"]["entry_price"] == 4500
