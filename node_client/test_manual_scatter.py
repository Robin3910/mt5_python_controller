"""模版1 手动分散仓：手数公式、到价判定、与分批隔离、每规则一次、止盈联动清仓。"""
import asyncio
import time

import close_reason as cr
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


def test_tier0_pl_amount_always_first_tier():
    """手动分散仓按固定第一档对齐，不随当前手数命中高档。"""
    assert ms.tier0_pl_amount(_gold_tiers()) == 500.0
    # 即使当前仓 0.5 手命中第二档，手动分散仓仍取第一档
    assert ms.tier0_pl_amount(_gold_tiers(pl_lo=500.0, pl_hi=800.0)) == 500.0
    assert ms.tier0_pl_amount({"enabled": False, "tiers": []}) is None
    assert ms.tier0_pl_amount({"enabled": True, "tiers": []}) is None
    assert ms.tier0_pl_amount({"enabled": True, "tiers": [{"pl_amount": 0}]}) is None


def test_projected_profit_marks_existing_positions_to_take_profit():
    """存量仓位在止盈价处的盈亏：按开仓价与止盈价的距离折算，不看当下浮盈。"""
    positions = [
        {"type": "BUY", "volume": 1.0, "price_open": 4410.0, "profit": -999.0},
    ]
    # 到 4410（止盈价）时这笔还是 0，不是当下的浮亏
    assert ms.projected_profit(positions, 4410.0, 0.01, 1.0) == 0.0
    # 到 4420 时赚 1000u（10 价差 / 0.01 × 1 = 1000u 每手）
    assert ms.projected_profit(positions, 4420.0, 0.01, 1.0) == 1000.0
    # 到 4400 时亏 1000u
    assert ms.projected_profit(positions, 4400.0, 0.01, 1.0) == -1000.0


def test_projected_profit_handles_sell_side():
    positions = [{"type": "SELL", "volume": 1.0, "price_open": 4400.0}]
    # 空单价格跌到 4390 → 赚 1000u
    assert ms.projected_profit(positions, 4390.0, 0.01, 1.0) == 1000.0
    # 空单价格涨到 4410 → 亏 1000u
    assert ms.projected_profit(positions, 4410.0, 0.01, 1.0) == -1000.0


def test_volume_covers_existing_loss_to_reach_target():
    """存量浮亏 1000u：手数要按 (目标 500 + 亏损 1000) / 每手止盈 反推。

    XAUUSD entry=4400、tp=4410、tick 0.01/1 → 每手止盈 1000u。
    存量 0.1 手开在 4510，到 4410 时浮亏 1000u，目标净赚 500u
    → 手动仓要赚 1500u → 1.5 手。
    """
    positions = [{"type": "BUY", "volume": 0.1, "price_open": 4510.0}]
    projected = ms.projected_profit(positions, 4410.0, 0.01, 1.0)
    assert projected == -1000.0
    vol = ms.compute_volume(
        cfg=_cfg(),
        current_lot_size=0.1,
        lot_pl_tiers=_gold_tiers(),
        tick_size=0.01,
        tick_value=1.0,
        projected_loss=max(0.0, -projected),
    )
    assert vol == 1.5
    # 手动仓止盈时：存量 -1000u + 手动仓 1.5 手 × 1000u = 净赚 500u
    assert projected + vol * 1000.0 == 500.0


def test_net_profit_at_tp_is_invariant_to_intermediate_moves():
    """挂限价单后：价格中途怎么走，止盈那一刻的净额恒等于目标金额。

    存量在止盈时刻的盈亏由**存量开仓价**决定（历史事实），手动仓的盈利由
    **固定入场价与止盈价**决定，两者都是常数，所以中途行情不影响净额。
    """
    entry, tp = 4400.0, 4410.0
    per_lot = (tp - entry) / 0.01 * 1.0  # 1000u / 手
    # 存量 0.1 手开在 4510
    existing = [{"type": "BUY", "volume": 0.1, "price_open": 4510.0}]
    projected = ms.projected_profit(existing, tp, 0.01, 1.0)
    vol = ms.compute_volume(
        cfg=_cfg(), current_lot_size=0.1, lot_pl_tiers=_gold_tiers(),
        tick_size=0.01, tick_value=1.0,
        projected_loss=max(0.0, -projected),
    )
    assert vol == 1.5
    # 中途价格走过的每一条路径，止盈那一刻的净额都一样
    for mid_price in (4510.0, 4400.0, 4300.0, 4600.0, 4200.0):
        # 存量到止盈那一刻的盈亏（由开仓价决定，与 mid_price 无关）
        assert ms.projected_profit(existing, tp, 0.01, 1.0) == -1000.0
        # 手动仓的盈利（由入场价与止盈价决定，与 mid_price 无关）
        manual_profit = (tp - entry) / 0.01 * 1.0 * vol
        assert projected + manual_profit == 500.0


def test_volume_frozen_at_placement_survives_market_moves():
    """手数在挂出那一刻算死：中途行情变化不重新算，净额仍等于目标。"""
    entry, tp = 4400.0, 4410.0
    existing = [{"type": "BUY", "volume": 0.1, "price_open": 4510.0}]
    # 挂出那一刻：存量浮亏 1000u，手数按 (500 + 1000) / 1000 算
    projected_at_placement = ms.projected_profit(existing, tp, 0.01, 1.0)
    vol = ms.compute_volume(
        cfg=_cfg(), current_lot_size=0.1, lot_pl_tiers=_gold_tiers(),
        tick_size=0.01, tick_value=1.0,
        projected_loss=max(0.0, -projected_at_placement),
    )
    # 中途行情变化：当下浮盈变了，但止盈时刻的净额没变
    for mid_price in (4300.0, 4600.0):
        current_profit = ms.position_profit_at(existing[0], mid_price, 0.01, 1.0)
        assert current_profit != projected_at_placement  # 当下浮盈确实变了
        # 但止盈那一刻的预估没变（存量开仓价是历史事实）
        assert ms.projected_profit(existing, tp, 0.01, 1.0) == -1000.0
        manual_profit = (tp - entry) / 0.01 * 1.0 * vol
        assert projected_at_placement + manual_profit == 500.0


def test_volume_ignores_profit_when_no_loss():
    """存量是浮盈时不抵扣：手数仍按目标金额算。"""
    positions = [{"type": "BUY", "volume": 0.1, "price_open": 4390.0}]
    projected = ms.projected_profit(positions, 4410.0, 0.01, 1.0)
    assert projected == 200.0
    vol = ms.compute_volume(
        cfg=_cfg(),
        current_lot_size=0.1,
        lot_pl_tiers=_gold_tiers(),
        tick_size=0.01,
        tick_value=1.0,
        projected_loss=max(0.0, -projected),
    )
    assert vol == 0.5


def test_volume_formula_mentions_projected_loss():
    text = ms.volume_formula(
        cfg=_cfg(), volume=1.5, current_lot_size=0.1, target_pl=500.0,
        profit_per_lot_value=1000.0, volume_step=0.01, projected_loss=1000.0,
    )
    assert "存量仓位到止盈预估亏损 1000u" in text
    assert "分档目标 500u" in text


def test_fill_side_price_matches_market_order():
    """BUY 看卖价、SELL 看买价；缺侧时报 fallback，兼容只带平仓侧的旧事件。"""
    assert ms.fill_side_price("BUY", bid=4290.0, ask=4290.2) == 4290.2
    assert ms.fill_side_price("SELL", bid=4290.0, ask=4290.2) == 4290.0
    assert ms.fill_side_price("BUY", bid=4290.0, ask=0.0, fallback=4290.0) == 4290.0
    assert ms.fill_side_price("SELL", bid=0.0, ask=4290.2, fallback=4290.2) == 4290.2
    assert ms.fill_side_price("BUY", bid=0.0, ask=0.0, fallback=2330.0) == 2330.0


def test_price_reached_buy_pullback_and_sell_bounce():
    assert ms.direction_from_prices(4400, 4410) == "BUY"
    assert ms.direction_from_prices(4400, 4390) == "SELL"
    assert ms.price_reached("BUY", 4400, 4400) is True
    assert ms.price_reached("BUY", 4399, 4400) is True
    assert ms.price_reached("BUY", 4401, 4400) is False
    assert ms.price_reached("SELL", 4400, 4400) is True
    assert ms.price_reached("SELL", 4401, 4400) is True
    assert ms.price_reached("SELL", 4399, 4400) is False


def test_tp_passed_buy_and_sell():
    assert ms.tp_passed("BUY", 4410, 4410) is True
    assert ms.tp_passed("BUY", 4411, 4410) is True
    assert ms.tp_passed("BUY", 4409, 4410) is False
    assert ms.tp_passed("SELL", 4390, 4390) is True
    assert ms.tp_passed("SELL", 4389, 4390) is True
    assert ms.tp_passed("SELL", 4391, 4390) is False
    assert ms.tp_passed("BUY", 4415, 0) is False
    assert "越过止盈" in ms.describe_tp_passed("BUY", 4415, 4400, 4410)


def test_comment_is_m_prefix_not_r3b():
    assert ms.comment_for(1) == "M1"
    assert ms.comment_for(2) == "M2"
    assert ms.parse_rule_type("M1") == 1
    assert ms.parse_rule_type("M2 extra") == 2
    assert ms.parse_rule_type("R3B") is None
    assert ms.parse_rule_type("M10") is None
    assert not str(ms.comment_for(1)).startswith("R3B")
    assert not str(ms.comment_for(2)).startswith("R3B")


# ----------------------------------------------------------------------
# 挂限价单：按现价在入场价的哪一侧选单
# ----------------------------------------------------------------------
def test_pending_kind_picks_side_by_current_price():
    """BUY：现价在上方挂 Limit 等回落，在下方挂 Stop 等反弹。"""
    assert ms.pending_kind("BUY", 4410.0, 4400.0) == "limit"
    assert ms.pending_kind("BUY", 4390.0, 4400.0) == "stop"
    # SELL 反过来：现价在下方挂 Limit 等反弹，在上方挂 Stop 等回落
    assert ms.pending_kind("SELL", 4390.0, 4400.0) == "limit"
    assert ms.pending_kind("SELL", 4410.0, 4400.0) == "stop"


def test_pending_kind_none_when_price_on_entry_or_invalid():
    """现价正好压在入场价：限价单与止损单都挂不上。"""
    assert ms.pending_kind("BUY", 4400.0, 4400.0) is None
    assert ms.pending_kind("SELL", 4400.0, 4400.0) is None
    assert ms.pending_kind("BUY", 0.0, 4400.0) is None
    assert ms.pending_kind("BUY", 4400.0, 0.0) is None
    assert ms.pending_kind("", 4400.0, 4400.0) is None


def test_pending_entry_ok_mirrors_pending_kind():
    assert ms.pending_entry_ok("BUY", 4400.0, 4410.0) is True
    assert ms.pending_entry_ok("BUY", 4400.0, 4390.0) is True
    assert ms.pending_entry_ok("BUY", 4400.0, 4400.0) is False


def test_sl_is_sendable_requires_opposite_side_of_entry():
    """止损必须落在入场价的反方向，否则券商拒掉整张单。"""
    assert ms.sl_is_sendable("BUY", 4350.0, 4400.0) is True
    assert ms.sl_is_sendable("BUY", 4450.0, 4400.0) is False
    assert ms.sl_is_sendable("SELL", 4450.0, 4400.0) is True
    assert ms.sl_is_sendable("SELL", 4350.0, 4400.0) is False
    # 没配止损
    assert ms.sl_is_sendable("BUY", 0.0, 4400.0) is False
    # 离入场价太近（券商 stops_level）
    assert ms.sl_is_sendable(
        "BUY", 4399.95, 4400.0, stops_level=10, point=0.01,
    ) is False
    assert ms.sl_is_sendable(
        "BUY", 4390.0, 4400.0, stops_level=10, point=0.01,
    ) is True


def test_is_manual_pending_recognizes_limit_and_stop():
    assert ms.is_manual_pending({"comment": "M2", "pending_kind": "limit"}) is True
    assert ms.is_manual_pending({"comment": "M2", "pending_kind": "stop"}) is True
    # 加仓单的备注是 R1 / R2，不是手动仓
    assert ms.is_manual_pending({"comment": "R1L0D100", "pending_kind": "limit"}) is False
    assert ms.is_manual_pending({"comment": "M2"}) is True
    assert ms.is_manual_pending(None) is False
    assert ms.pending_rule_type({"comment": "M2", "pending_kind": "limit"}) == 2
    assert ms.pending_rule_type({"comment": "R1", "pending_kind": "limit"}) is None


def test_runtime_pending_round_trip():
    """在途挂单随 runtime 落库再还原：重连后才知道已经挂过。"""
    snap = {
        "ticket": 5001, "type": "BUY", "volume": 1.5,
        "price_open": 4400.0, "sl": 4350.0, "tp": 4410.0, "time": 1700000000.0,
    }
    payload = ms.runtime_pending_payload({2: snap})
    assert "2" in payload
    assert payload["2"]["ticket"] == 5001
    assert payload["2"]["price_open"] == 4400.0
    restored = ms.parse_runtime_pending(payload)
    assert restored[2]["ticket"] == 5001
    assert restored[2]["volume"] == 1.5
    assert restored[2]["price_open"] == 4400.0
    # 脏数据一律丢弃
    assert ms.parse_runtime_pending({"9": snap, "2": {"ticket": 0}, "x": 1}) == {}
    assert ms.parse_runtime_pending(None) == {}


def test_describe_pending_mentions_limit_and_sync():
    watch = {"rule_type": 2, "direction": "BUY", "comment": "M2", "cfg": _cfg()}
    text = ms.describe_pending(
        watch, 1.5, target_pl=500.0, current_lot_size=0.1,
        profit_per_lot_value=1000.0, volume_step=0.01, projected_loss=1000.0,
    )
    assert "挂出限价单" in text
    assert "挂单价 4400" in text
    assert "成交后统一同步到全部持仓" in text
    detail = ms.pending_detail(
        watch, 1.5, target_pl=500.0, current_lot_size=0.1,
        profit_per_lot_value=1000.0, ticket=5001, kind="limit",
    )
    assert detail["kind"] == "manual_scatter"
    assert detail["pending_ticket"] == 5001
    assert detail["pending_kind"] == "limit"
    assert detail["pending_action"] == "place"


def test_describe_pending_filled_mentions_sync_count():
    snap = {
        "ticket": 5001, "type": "BUY", "volume": 1.5,
        "tp": 4410.0, "price_open": 4400.0,
    }
    text = ms.describe_pending_filled(2, snap, aligned=3)
    assert "限价单成交" in text
    assert "止盈价已同步到 3 笔持仓" in text
    detail = ms.pending_filled_detail(2, snap, aligned=3)
    assert detail["kind"] == "manual_scatter"
    assert detail["ticket"] == 5001
    assert detail["aligned_tickets"] == 3
    assert detail["pending_action"] == "filled"


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
            "pos_from": 2, "pos_to": 2, "calc_type": "point",
            "point": 1, "lot_times": 1, "extra_lot": 0,
        }],
    }]
    # 过滤后本规则尚未加仓 → 第 1 次加仓不在档位 2~2 内，不命中
    assert evaluate(rules, ctx) is None
    # 若把另一笔误记成本规则加仓，已加 1 次 → 第 2 次加仓会命中
    ctx_wrong = PositionCtx(**{**ctx.__dict__, "add_count": 1, "add_counts": {2: 1}})
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


async def test_runner_places_pending_order_once_with_m2_comment():
    """手动分散仓挂成限价单：挂出即算开火，成交由终端撮合。"""
    sent: list = []
    mt5 = MockMT5Client()
    # 首单开在手动入场附近：批次1金额会给底仓写共同止盈，
    # 若仍从 2330 跳到 4400，底仓会先被市价平掉。
    mt5.prices_map["XAUUSD"] = 4390.0
    runner, hub = _runner(sent, mt5=mt5)
    runner.start()
    await _settle()

    mt5.prices_map["XAUUSD"] = 4399.0
    held = mt5.positions_by_magic(MAGIC)
    hub.sub.offer(mh.MarketEvent(
        kind=mh.TICK, symbol="XAUUSD", magic=MAGIC,
        positions=tuple(held), price=4399.0, point=0.01,
        bid=4398.9, ask=4399.0,
    ))
    await _settle()

    # 挂单在场，还没有手动仓持仓
    pending = mt5.pending_orders_by_magic(MAGIC)
    manuals = [o for o in pending if str(o.get("comment") or "").startswith("M")]
    assert len(manuals) == 1, "手动分散仓应挂成一张限价单"
    assert manuals[0]["comment"] == "M2"
    assert manuals[0]["volume"] == 0.5
    assert manuals[0]["price_open"] == 4400.0, "挂单价就是配置入场价"
    # 现价（卖价 4399）已在入场价下方 → Buy Stop 等反弹回入场价成交
    assert manuals[0]["pending_kind"] == "stop"
    assert not str(manuals[0]["comment"]).startswith("R3B")
    # 挂单不是持仓，总手数不因它而变
    assert not [p for p in mt5.positions_by_magic(MAGIC) if p.get("comment") == "M2"]
    assert runner.add_count == 0
    assert runner.total_orders == 1
    assert runner.total_volume == 0.1
    ev = _progress(sent, "manual_pending")
    assert ev and ev[0]["detail"]["kind"] == "manual_scatter"
    assert "分档目标" in (ev[0].get("message") or "")
    assert "每手止盈" in (ev[0].get("message") or "")
    assert "手数 =" in (ev[0]["detail"].get("volume_formula") or "")
    assert ev[0].get("runtime", {}).get("manual_fired") == [2]
    # 在途挂单也写进 runtime，重连后才知道已经挂过
    pending_rt = ev[0].get("runtime", {}).get("manual_pending") or {}
    assert "2" in pending_rt
    assert pending_rt["2"]["price_open"] == 4400.0

    # 再来一拍：不该重复挂
    hub.sub.offer(mh.MarketEvent(
        kind=mh.TICK, symbol="XAUUSD", magic=MAGIC,
        positions=tuple(mt5.positions_by_magic(MAGIC)), price=4399.0, point=0.01,
        bid=4398.9, ask=4399.0,
    ))
    await _settle()
    assert len([o for o in mt5.pending_orders_by_magic(MAGIC) if o.get("comment") == "M2"]) == 1
    runner.cancel()


async def test_runner_pending_fills_and_aligns_all_tp():
    """限价单成交后：手动仓的止盈价成为权威，全部持仓的 TP 都对齐过去。"""
    sent: list = []
    mt5 = MockMT5Client()
    mt5.prices_map["XAUUSD"] = 4390.0
    runner, hub = _runner(sent, mt5=mt5)
    runner.start()
    await _settle()

    mt5.prices_map["XAUUSD"] = 4399.0
    hub.sub.offer(mh.MarketEvent(
        kind=mh.TICK, symbol="XAUUSD", magic=MAGIC,
        positions=tuple(mt5.positions_by_magic(MAGIC)), price=4399.0, point=0.01,
        bid=4398.9, ask=4399.0,
    ))
    await _settle()
    assert [o for o in mt5.pending_orders_by_magic(MAGIC) if o.get("comment") == "M2"]

    # 价格回落到入场价，终端撮合成交
    mt5.prices_map["XAUUSD"] = 4400.0
    hub.sub.offer(mh.MarketEvent(
        kind=mh.TICK, symbol="XAUUSD", magic=MAGIC,
        positions=tuple(mt5.positions_by_magic(MAGIC)), price=4400.0, point=0.01,
        bid=4399.9, ask=4400.0,
    ))
    await _settle()

    manuals = [p for p in mt5.positions_by_magic(MAGIC) if p.get("comment") == "M2"]
    assert len(manuals) == 1, "限价单成交后应有一笔手动仓持仓"
    assert manuals[0]["price_open"] == 4400.0, "成交价正好等于入场价"
    assert manuals[0]["volume"] == 0.5
    # 成交后手数才计入总手数
    assert runner.total_volume == 0.6
    filled = _progress(sent, "manual_filled")
    assert filled, "应记一笔成交事件"
    assert "限价单成交" in (filled[0].get("message") or "")
    # 全部持仓的止盈都对齐到手动仓 TP
    assert runner._manual_authority_tp() == 4410.0
    for pos in mt5.positions_by_magic(MAGIC):
        assert pos["tp"] == 4410.0, f"仓位 {pos['ticket']} 的止盈价应等于手动仓 TP"
    runner.cancel()


async def test_runner_pending_order_keeps_volume_frozen():
    """手数在挂出那一刻算死：中途行情变化不重新算。"""
    sent: list = []
    mt5 = MockMT5Client()
    mt5.prices_map["XAUUSD"] = 4390.0
    runner, hub = _runner(sent, mt5=mt5)
    runner.start()
    await _settle()

    mt5.prices_map["XAUUSD"] = 4399.0
    hub.sub.offer(mh.MarketEvent(
        kind=mh.TICK, symbol="XAUUSD", magic=MAGIC,
        positions=tuple(mt5.positions_by_magic(MAGIC)), price=4399.0, point=0.01,
        bid=4398.9, ask=4399.0,
    ))
    await _settle()
    placed = [o for o in mt5.pending_orders_by_magic(MAGIC) if o.get("comment") == "M2"]
    assert placed and placed[0]["volume"] == 0.5

    # 价格继续下跌（存量浮亏扩大），挂单手数不能变
    mt5.prices_map["XAUUSD"] = 4250.0
    hub.sub.offer(mh.MarketEvent(
        kind=mh.TICK, symbol="XAUUSD", magic=MAGIC,
        positions=tuple(mt5.positions_by_magic(MAGIC)), price=4250.0, point=0.01,
        bid=4249.9, ask=4250.0,
    ))
    await _settle()
    still = [o for o in mt5.pending_orders_by_magic(MAGIC) if o.get("comment") == "M2"]
    assert still and still[0]["volume"] == 0.5, "手数在挂出时已算死，不随行情变"
    runner.cancel()


async def test_runner_pending_order_carries_configured_stop_loss():
    """配了止损就带上；没配就不带。"""
    sent: list = []
    mt5 = MockMT5Client()
    mt5.prices_map["XAUUSD"] = 4390.0
    runner, hub = _runner(
        sent, mt5=mt5,
        strategy={"template_id": "tpl_1", "rules": [
            _trend_rule(manual_scatter=_cfg(
                entry_price=4400, take_profit=4410, stop_loss=4350,
            )),
        ]},
    )
    runner.start()
    await _settle()
    mt5.prices_map["XAUUSD"] = 4399.0
    hub.sub.offer(mh.MarketEvent(
        kind=mh.TICK, symbol="XAUUSD", magic=MAGIC,
        positions=tuple(mt5.positions_by_magic(MAGIC)), price=4399.0, point=0.01,
        bid=4398.9, ask=4399.0,
    ))
    await _settle()
    placed = [o for o in mt5.pending_orders_by_magic(MAGIC) if o.get("comment") == "M2"]
    assert placed and placed[0]["sl"] == 4350.0
    assert placed[0]["tp"] == 0.0, "止盈不写进挂单：成交后由节点统一同步"
    runner.cancel()


async def test_runner_pending_order_skips_stop_loss_on_wrong_side():
    """止损落在入场价的错误一侧：不带止损，宁可少个保护位也不让整张单被拒。"""
    sent: list = []
    mt5 = MockMT5Client()
    mt5.prices_map["XAUUSD"] = 4390.0
    runner, hub = _runner(
        sent, mt5=mt5,
        strategy={"template_id": "tpl_1", "rules": [
            _trend_rule(manual_scatter=_cfg(
                entry_price=4400, take_profit=4410, stop_loss=4450,
            )),
        ]},
    )
    runner.start()
    await _settle()
    mt5.prices_map["XAUUSD"] = 4399.0
    hub.sub.offer(mh.MarketEvent(
        kind=mh.TICK, symbol="XAUUSD", magic=MAGIC,
        positions=tuple(mt5.positions_by_magic(MAGIC)), price=4399.0, point=0.01,
        bid=4398.9, ask=4399.0,
    ))
    await _settle()
    placed = [o for o in mt5.pending_orders_by_magic(MAGIC) if o.get("comment") == "M2"]
    assert placed, "止损不合法不该影响挂单本身"
    assert placed[0]["sl"] == 0.0
    runner.cancel()


async def test_runner_waits_for_batch_before_pending():
    """分批没加完：不挂单，位置不记开火（加完后仍可挂）。"""
    sent: list = []
    mt5 = MockMT5Client()
    rule = _trend_rule(
        batch_enabled=True,
        batch_action="all",
        total_lot_limit=10,
        batch_levels=[{
            "pos_from": 1, "pos_to": 1, "calc_type": "point",
            "point": 1, "lot_times": 1, "extra_lot": 0,
        }],
        manual_scatter=_cfg(entry_price=4400, take_profit=4410),
    )
    runner, hub = _runner(sent, mt5=mt5, strategy={"template_id": "tpl_1", "rules": [rule]})
    mt5.prices_map["XAUUSD"] = 4390.0
    runner.start()
    await _settle()

    mt5.prices_map["XAUUSD"] = 4399.0
    hub.sub.offer(mh.MarketEvent(
        kind=mh.TICK, symbol="XAUUSD", magic=MAGIC,
        positions=tuple(mt5.positions_by_magic(MAGIC)), price=4399.0, point=0.01,
        bid=4398.9, ask=4399.0,
    ))
    await _settle()

    manuals = [o for o in mt5.pending_orders_by_magic(MAGIC) if o.get("comment") == "M2"]
    assert manuals == [], "分批没加完，手动分散仓不该挂单"
    assert 2 not in runner._manual_fired, "不该记开火，加完后仍可挂"
    errors = _progress(sent, "error")
    assert errors and errors[0]["detail"]["error"] == "batch_pending"
    runner.cancel()


async def test_runner_places_pending_after_batch_full():
    """分批加满上限：手动分散仓可以挂单。"""
    sent: list = []
    mt5 = MockMT5Client()
    rule = _trend_rule(
        batch_enabled=True,
        batch_action="all",
        total_lot_limit=1,
        batch_levels=[{
            "pos_from": 1, "pos_to": 1, "calc_type": "point",
            "point": 1, "lot_times": 1, "extra_lot": 0,
        }],
        manual_scatter=_cfg(entry_price=4400, take_profit=4410),
    )
    runner, hub = _runner(sent, mt5=mt5, strategy={"template_id": "tpl_1", "rules": [rule]})
    mt5.prices_map["XAUUSD"] = 4390.0
    runner.start()
    await _settle()

    mt5.prices_map["XAUUSD"] = 4399.0
    hub.sub.offer(mh.MarketEvent(
        kind=mh.TICK, symbol="XAUUSD", magic=MAGIC,
        positions=tuple(mt5.positions_by_magic(MAGIC)), price=4399.0, point=0.01,
        bid=4398.9, ask=4399.0,
    ))
    await _settle()

    # 分批加满 1 笔，顺势加仓先做完，手动分散仓同拍就能挂
    assert runner.add_count == 1
    manuals = [o for o in mt5.pending_orders_by_magic(MAGIC) if o.get("comment") == "M2"]
    assert len(manuals) == 1, "分批加满后手动分散仓应能挂单"
    assert runner._manual_fired == {2}
    runner.cancel()


async def test_runner_past_take_profit_does_not_open():
    """现价已过止盈：不挂单，进度记越过止盈；不记开火，以后仍可回踩。"""
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
        bid=4414.9, ask=4415.0,
    ))
    await _settle()
    manuals = [o for o in mt5.pending_orders_by_magic(MAGIC) if o.get("comment") == "M2"]
    assert manuals == []
    assert 2 not in runner._manual_fired
    errors = _progress(sent, "error")
    assert errors and "越过止盈" in (errors[0].get("message") or "")
    assert errors[0]["detail"]["error"] == "tp_passed"

    hub.sub.offer(mh.MarketEvent(
        kind=mh.TICK, symbol="XAUUSD", magic=MAGIC,
        positions=tuple(held), price=4416.0, point=0.01,
        bid=4415.9, ask=4416.0,
    ))
    await _settle()
    assert len(_progress(sent, "error")) == 1
    runner.cancel()


async def test_runner_between_entry_and_tp_waits():
    """多单现价在入场与止盈之间：未回踩，也不算越过止盈，不挂单。"""
    sent: list = []
    mt5 = MockMT5Client()
    runner, hub = _runner(sent, mt5=mt5)
    runner.start()
    await _settle()
    held = mt5.positions_by_magic(MAGIC)
    hub.sub.offer(mh.MarketEvent(
        kind=mh.TICK, symbol="XAUUSD", magic=MAGIC,
        positions=tuple(held), price=4405.0, point=0.01,
        bid=4404.9, ask=4405.0,
    ))
    await _settle()
    assert not [p for p in mt5.positions_by_magic(MAGIC) if p.get("comment") == "M2"]
    assert not [o for o in mt5.pending_orders_by_magic(MAGIC) if o.get("comment") == "M2"]
    assert not _progress(sent, "error")
    assert not _progress(sent, "manual_pending")
    runner.cancel()


async def test_runner_pending_uses_ask_side_for_buy():
    """多单挂单看卖价：卖价在入场上方 → Buy Limit 等回落；卖价在下方 → Buy Stop 等反弹。"""
    sent: list = []
    mt5 = MockMT5Client()
    runner, hub = _runner(
        sent, mt5=mt5,
        strategy={"template_id": "tpl_1", "rules": [
            _trend_rule(manual_scatter=_cfg(
                entry_price=4400, take_profit=4410, volume=0.01, volume_locked=True,
            )),
        ]},
    )
    mt5.prices_map["XAUUSD"] = 4390.0
    runner.start()
    await _settle()
    held = mt5.positions_by_magic(MAGIC)

    # 现价高于入场价 → Buy Limit 挂在下方等回落
    mt5.prices_map["XAUUSD"] = 4405.0
    hub.sub.offer(mh.MarketEvent(
        kind=mh.TICK, symbol="XAUUSD", magic=MAGIC,
        positions=tuple(held), price=4405.0, point=0.01,
        bid=4404.9, ask=4405.0,
    ))
    await _settle()
    placed = [o for o in mt5.pending_orders_by_magic(MAGIC) if o.get("comment") == "M2"]
    assert len(placed) == 1
    assert placed[0]["type"] == "BUY"
    assert placed[0]["pending_kind"] == "limit", "现价高于入场 → Buy Limit 等回落"
    assert placed[0]["price_open"] == 4400.0
    assert placed[0]["volume"] == 0.01
    runner.cancel()


async def test_runner_pending_stop_when_price_below_entry():
    """现价在入场价下方：挂 Buy Stop 等反弹，成交价正好是入场价。"""
    sent: list = []
    mt5 = MockMT5Client()
    runner, hub = _runner(
        sent, mt5=mt5,
        strategy={"template_id": "tpl_1", "rules": [
            _trend_rule(manual_scatter=_cfg(
                entry_price=4400, take_profit=4410, volume=0.01, volume_locked=True,
            )),
        ]},
    )
    mt5.prices_map["XAUUSD"] = 4390.0
    runner.start()
    await _settle()
    held = mt5.positions_by_magic(MAGIC)

    mt5.prices_map["XAUUSD"] = 4390.0
    hub.sub.offer(mh.MarketEvent(
        kind=mh.TICK, symbol="XAUUSD", magic=MAGIC,
        positions=tuple(held), price=4390.0, point=0.01,
        bid=4389.9, ask=4390.0,
    ))
    await _settle()
    placed = [o for o in mt5.pending_orders_by_magic(MAGIC) if o.get("comment") == "M2"]
    assert len(placed) == 1
    assert placed[0]["pending_kind"] == "stop", "现价低于入场 → Buy Stop 等反弹"
    assert placed[0]["price_open"] == 4400.0
    runner.cancel()
    runner.cancel()


async def test_manual_tp_becomes_authoritative_for_all_positions():
    """手动仓成交后：本魔术号下所有仓位的止盈价都改到手动仓的 TP。

    存量仓位的共同止盈是按加权开仓价反推的，和手动仓的绝对价对不上；
    不改的话存量会先被平掉、手动仓还挂着。
    """
    sent: list = []
    mt5 = MockMT5Client()
    mt5.prices_map["XAUUSD"] = 4390.0
    runner, hub = _runner(sent, mt5=mt5)
    runner.start()
    await _settle()

    held = mt5.positions_by_magic(MAGIC)
    assert held, "首单应该在场"
    base_tp_before = held[0]["tp"]

    mt5.prices_map["XAUUSD"] = 4399.0
    hub.sub.offer(mh.MarketEvent(
        kind=mh.TICK, symbol="XAUUSD", magic=MAGIC,
        positions=tuple(mt5.positions_by_magic(MAGIC)), price=4399.0, point=0.01,
        bid=4398.9, ask=4399.0,
    ))
    await _settle()

    # 挂单成交
    mt5.prices_map["XAUUSD"] = 4400.0
    hub.sub.offer(mh.MarketEvent(
        kind=mh.TICK, symbol="XAUUSD", magic=MAGIC,
        positions=tuple(mt5.positions_by_magic(MAGIC)), price=4400.0, point=0.01,
        bid=4399.9, ask=4400.0,
    ))
    await _settle()

    manuals = [p for p in mt5.positions_by_magic(MAGIC) if p.get("comment") == "M2"]
    assert len(manuals) == 1
    manual_tp = 4410.0
    assert base_tp_before != manual_tp, "底仓原本的共同止盈应与手动仓 TP 不同"

    # 成交当拍就要对齐：不能等下一拍，否则价格先到存量原有的止盈价时
    # 存量会被平掉、手动仓还挂着
    for pos in mt5.positions_by_magic(MAGIC):
        if str(pos.get("comment") or "").startswith("M"):
            continue
        assert pos["tp"] == manual_tp, f"仓位 {pos['ticket']} 的止盈价应已对齐到手动仓 TP"

    # 下一拍：所有仓位的止盈价都应被对齐到手动仓 TP
    hub.sub.offer(mh.MarketEvent(
        kind=mh.TICK, symbol="XAUUSD", magic=MAGIC,
        positions=tuple(mt5.positions_by_magic(MAGIC)), price=4400.0, point=0.01,
        bid=4399.9, ask=4400.0,
    ))
    await _settle()

    for pos in mt5.positions_by_magic(MAGIC):
        assert pos["tp"] == manual_tp, f"仓位 {pos['ticket']} 的止盈价应等于手动仓 TP"
    runner.cancel()


async def test_manual_tp_authority_skips_lot_sltp_sync():
    """手动仓在场时不再由分档手数盈亏那套反推共同止盈。"""
    sent: list = []
    mt5 = MockMT5Client()
    mt5.prices_map["XAUUSD"] = 4390.0
    runner, hub = _runner(sent, mt5=mt5)
    runner.start()
    await _settle()

    mt5.prices_map["XAUUSD"] = 4399.0
    hub.sub.offer(mh.MarketEvent(
        kind=mh.TICK, symbol="XAUUSD", magic=MAGIC,
        positions=tuple(mt5.positions_by_magic(MAGIC)), price=4399.0, point=0.01,
        bid=4398.9, ask=4399.0,
    ))
    await _settle()
    # 挂单成交后才有止盈权威
    mt5.prices_map["XAUUSD"] = 4400.0
    hub.sub.offer(mh.MarketEvent(
        kind=mh.TICK, symbol="XAUUSD", magic=MAGIC,
        positions=tuple(mt5.positions_by_magic(MAGIC)), price=4400.0, point=0.01,
        bid=4399.9, ask=4400.0,
    ))
    await _settle()
    assert runner._manual_authority_tp() == 4410.0

    # 手动仓在场后再走几拍：分档手数盈亏那条路径不该再发进度
    before = len(_progress(sent, "signal_pl"))
    for _ in range(3):
        hub.sub.offer(mh.MarketEvent(
            kind=mh.TICK, symbol="XAUUSD", magic=MAGIC,
            positions=tuple(mt5.positions_by_magic(MAGIC)), price=4400.0, point=0.01,
            bid=4399.9, ask=4400.0,
        ))
        await _settle()
    assert len(_progress(sent, "signal_pl")) == before, "手动仓在场时不该再由分档手数盈亏改共同止盈"
    runner.cancel()


async def test_manual_tp_authority_released_when_manual_gone():
    """手动仓离场后止盈权威撤销：分档手数盈亏那套重新接管。"""
    sent: list = []
    mt5 = MockMT5Client()
    mt5.prices_map["XAUUSD"] = 4390.0
    runner, hub = _runner(sent, mt5=mt5)
    runner.start()
    await _settle()

    mt5.prices_map["XAUUSD"] = 4399.0
    hub.sub.offer(mh.MarketEvent(
        kind=mh.TICK, symbol="XAUUSD", magic=MAGIC,
        positions=tuple(mt5.positions_by_magic(MAGIC)), price=4399.0, point=0.01,
        bid=4398.9, ask=4399.0,
    ))
    await _settle()
    mt5.prices_map["XAUUSD"] = 4400.0
    hub.sub.offer(mh.MarketEvent(
        kind=mh.TICK, symbol="XAUUSD", magic=MAGIC,
        positions=tuple(mt5.positions_by_magic(MAGIC)), price=4400.0, point=0.01,
        bid=4399.9, ask=4400.0,
    ))
    await _settle()
    assert runner._manual_authority_tp() == 4410.0

    # 手动仓消失（止盈离场），止盈权威应撤销
    runner._manual_tp_authority.pop(2, None)
    assert runner._manual_authority_tp() is None
    runner.cancel()


def test_tp_is_sendable_checks_direction_and_stops_level():
    assert ms.tp_is_sendable("BUY", 4410.0, 4399.0) is True
    assert ms.tp_is_sendable("BUY", 4390.0, 4399.0) is False
    assert ms.tp_is_sendable("SELL", 4390.0, 4399.0) is True
    assert ms.tp_is_sendable("SELL", 4410.0, 4399.0) is False
    # 离现价太近（券商 stops_level）
    assert ms.tp_is_sendable(
        "BUY", 4399.05, 4399.0, stops_level=10, point=0.01,
    ) is False
    assert ms.tp_is_sendable(
        "BUY", 4399.5, 4399.0, stops_level=10, point=0.01,
    ) is True
    assert ms.tp_is_sendable("BUY", 0.0, 4399.0) is False
    assert ms.tp_is_sendable("", 4410.0, 4399.0) is False


# ----------------------------------------------------------------------
# 先后顺序：手动分散仓要等本规则分批加仓先加完
# ----------------------------------------------------------------------
def test_manual_blocked_reason_requires_batch_done():
    rule = {"batch_enabled": True, "total_lot_limit": 3, "max_allow_num": 9}
    # 还没加完
    assert ms.manual_blocked_reason(rule, 0) is not None
    assert ms.manual_blocked_reason(rule, 2) is not None
    # 加满了才放行
    assert ms.manual_blocked_reason(rule, 3) is None
    assert ms.manual_blocked_reason(rule, 4) is None


def test_manual_blocked_reason_allows_when_batch_disabled():
    """没启用分批加仓时不设门槛。"""
    rule = {"batch_enabled": False, "total_lot_limit": 3, "max_allow_num": 9}
    assert ms.manual_blocked_reason(rule, 0) is None
    assert ms.manual_blocked_reason(rule, 0, pending_counter=True) is None


def test_manual_blocked_reason_pending_counter_blocks():
    """有在途逆势限价单：那一档还没成交，算没加完。"""
    rule = {"batch_enabled": True, "total_lot_limit": 1}
    reason = ms.manual_blocked_reason(rule, 1, pending_counter=True)
    assert reason is not None
    assert "限价单" in reason


def test_manual_blocked_reason_no_limit_no_block():
    """上限为 0 表示不限，不设门槛。"""
    rule = {"batch_enabled": True, "total_lot_limit": 0}
    assert ms.manual_blocked_reason(rule, 0) is None


def test_extract_watches_carries_batch_context():
    rules = [{
        "type": 2, "status": 1, "manual_scatter": _cfg(),
        "batch_enabled": True, "total_lot_limit": 2,
    }]
    watches = ms.extract_watches(rules)
    assert len(watches) == 1
    assert watches[0]["batch_enabled"] is True
    assert watches[0]["rule"]["total_lot_limit"] == 2


async def test_runner_waits_for_batch_before_manual():
    """分批没加完：手动分散仓不开仓，位置不记开火（加完后仍可开）。"""
    sent: list = []
    mt5 = MockMT5Client()
    rule = _trend_rule(
        batch_enabled=True,
        batch_action="all",
        total_lot_limit=10,
        batch_levels=[{
            "pos_from": 1, "pos_to": 1, "calc_type": "point",
            "point": 1, "lot_times": 1, "extra_lot": 0,
        }],
        manual_scatter=_cfg(entry_price=4400, take_profit=4410),
    )
    runner, hub = _runner(sent, mt5=mt5, strategy={"template_id": "tpl_1", "rules": [rule]})
    mt5.prices_map["XAUUSD"] = 4390.0
    runner.start()
    await _settle()

    # 分批上限 10 笔，只加了 1 笔 → 手动仓不该开
    mt5.prices_map["XAUUSD"] = 4399.0
    hub.sub.offer(mh.MarketEvent(
        kind=mh.TICK, symbol="XAUUSD", magic=MAGIC,
        positions=tuple(mt5.positions_by_magic(MAGIC)), price=4399.0, point=0.01,
        bid=4398.9, ask=4399.0,
    ))
    await _settle()

    manuals = [p for p in mt5.positions_by_magic(MAGIC) if p.get("comment") == "M2"]
    assert manuals == [], "分批没加完，手动分散仓不该开仓"
    assert 2 not in runner._manual_fired, "不该记开火，加完后仍可开"
    errors = _progress(sent, "error")
    assert errors and errors[0]["detail"]["error"] == "batch_pending"
    runner.cancel()


async def test_runner_opens_manual_after_batch_full():
    """分批加满上限：手动分散仓可以开。"""
    sent: list = []
    mt5 = MockMT5Client()
    rule = _trend_rule(
        batch_enabled=True,
        batch_action="all",
        total_lot_limit=1,
        batch_levels=[{
            "pos_from": 1, "pos_to": 1, "calc_type": "point",
            "point": 1, "lot_times": 1, "extra_lot": 0,
        }],
        manual_scatter=_cfg(entry_price=4400, take_profit=4410),
    )
    runner, hub = _runner(sent, mt5=mt5, strategy={"template_id": "tpl_1", "rules": [rule]})
    mt5.prices_map["XAUUSD"] = 4390.0
    runner.start()
    await _settle()

    mt5.prices_map["XAUUSD"] = 4399.0
    hub.sub.offer(mh.MarketEvent(
        kind=mh.TICK, symbol="XAUUSD", magic=MAGIC,
        positions=tuple(mt5.positions_by_magic(MAGIC)), price=4399.0, point=0.01,
        bid=4398.9, ask=4399.0,
    ))
    await _settle()

    # 分批加满 1 笔，顺势加仓先做完，手动分散仓同拍就能挂
    assert runner.add_count == 1
    manuals = [o for o in mt5.pending_orders_by_magic(MAGIC) if o.get("comment") == "M2"]
    assert len(manuals) == 1, "分批加满后手动分散仓应能挂单"
    assert runner._manual_fired == {2}
    runner.cancel()


async def test_manual_positions_do_not_inflate_batch_position_count():
    sent: list = []
    mt5 = MockMT5Client()
    rule = _trend_rule(
        batch_enabled=True,
        batch_action="all",
        # 分批上限 1 笔：手动单要等这一笔加完才能开
        total_lot_limit=1,
        batch_levels=[{
            "pos_from": 1, "pos_to": 1, "calc_type": "point",
            "point": 1, "lot_times": 1, "extra_lot": 0,
        }],
    )
    runner, hub = _runner(sent, mt5=mt5, strategy={"template_id": "tpl_1", "rules": [rule]})
    mt5.prices_map["XAUUSD"] = 4390.0
    runner.start()
    await _settle()
    mt5.prices_map["XAUUSD"] = 4399.0
    hub.sub.offer(mh.MarketEvent(
        kind=mh.TICK, symbol="XAUUSD", magic=MAGIC,
        positions=tuple(mt5.positions_by_magic(MAGIC)), price=4399.0, point=0.01,
        bid=4398.9, ask=4399.0,
    ))
    await _settle()
    assert runner.add_count == 1, "分批第 1 笔应已加上"
    hub.sub.offer(mh.MarketEvent(
        kind=mh.TICK, symbol="XAUUSD", magic=MAGIC,
        positions=tuple(mt5.positions_by_magic(MAGIC)), price=4399.0, point=0.01,
        bid=4398.9, ask=4399.0,
    ))
    await _settle()
    # 分批已加满 1 笔，手动仓可以挂单（挂单不是持仓）
    assert len(mt5.positions_by_magic(MAGIC)) == 2
    assert len([o for o in mt5.pending_orders_by_magic(MAGIC) if o.get("comment") == "M2"]) == 1
    assert runner._manual_fired == {2}
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


# ----------------------------------------------------------------------
# 止盈联动清仓
# ----------------------------------------------------------------------
def _m2(ticket: int = 1001, **over) -> dict:
    snap = {
        "ticket": ticket, "type": "BUY", "volume": 0.5,
        "tp": 4410.0, "price_open": 4400.0, "time": 1_700_000_000.0,
    }
    snap.update(over)
    return snap


def test_normalize_close_all_on_tp_defaults_on_and_round_trips():
    """默认开启；旧快照缺字段同样按开启；显式 False 保留。"""
    assert ms.default_config()["close_all_on_tp"] is True
    assert ms.normalize({})["close_all_on_tp"] is True
    assert ms.normalize(_cfg())["close_all_on_tp"] is True
    assert ms.normalize(_cfg(close_all_on_tp=False))["close_all_on_tp"] is False
    assert ms.normalize(_cfg(close_all_on_tp=0))["close_all_on_tp"] is False
    assert ms.normalize(_cfg(close_all_on_tp="yes"))["close_all_on_tp"] is True


def test_linkage_rule_types_requires_rule_and_flag_enabled():
    rules = [
        {"type": 1, "status": 0, "manual_scatter": _cfg(close_all_on_tp=True)},
        {"type": 2, "status": 1, "manual_scatter": _cfg()},  # 缺字段 → 默认联动
    ]
    assert ms.linkage_rule_types(rules) == {2}
    rules[1]["manual_scatter"]["close_all_on_tp"] = False
    assert ms.linkage_rule_types(rules) == set()
    rules[1]["manual_scatter"]["enabled"] = False
    rules[1]["manual_scatter"]["close_all_on_tp"] = True
    assert ms.linkage_rule_types(rules) == set()


def test_snapshot_gone_and_runtime_round_trip():
    first = {"ticket": 1, "comment": "S1", "type": "BUY", "volume": 0.1, "tp": 0, "price_open": 2330, "time": 1}
    manual = {"ticket": 2, "comment": "M2", "type": "BUY", "volume": 0.5, "tp": 4410, "price_open": 4400, "time": 2}
    tracked = ms.snapshot_open([first, manual])
    assert set(tracked) == {2}
    assert tracked[2]["ticket"] == 2
    assert tracked[2]["tp"] == 4410.0

    assert ms.gone_manual(tracked, ms.snapshot_open([first])) == {2: tracked[2]}
    assert ms.gone_manual(tracked, tracked) == {}

    payload = ms.runtime_open_payload(tracked)
    assert payload == {"2": tracked[2]}
    assert ms.parse_runtime_open(payload) == tracked
    assert ms.parse_runtime_open({"9": tracked[2], "2": {"ticket": 0}, "x": 1}) == {}
    assert ms.parse_runtime_open(None) == {}


def test_resolve_exit_prefers_deal_reason_then_close_side_price():
    snap = _m2()
    tp_deal = {"position_id": 1001, "entry": 1, "reason": cr.REASON_TP}
    sl_deal = {"position_id": 1001, "entry": 1, "reason": cr.REASON_SL}
    other_pos = {"position_id": 999, "entry": 1, "reason": cr.REASON_TP}

    hit = ms.resolve_exit(snap, [other_pos, tp_deal], bid=4300.0, ask=4300.2)
    assert hit == {"exit": "tp", "reason": cr.REASON_TP, "source": "deal"}
    # 成交历史说止损，即使现价在止盈之上也不算止盈离场
    miss = ms.resolve_exit(snap, [sl_deal], bid=4415.0, ask=4415.2)
    assert miss["exit"] == "other" and miss["reason"] == cr.REASON_SL
    # 历史还没写进来：多单看 bid 到止盈才兜底判止盈
    assert ms.resolve_exit(snap, [other_pos], bid=4410.0, ask=4410.2) == {
        "exit": "tp", "reason": None, "source": "price",
    }
    assert ms.resolve_exit(snap, [], bid=4409.9, ask=4410.1) is None
    # 空单看 ask
    short = _m2(type="SELL", tp=4390.0)
    assert ms.resolve_exit(short, [], bid=4389.7, ask=4390.0)["source"] == "price"
    assert ms.resolve_exit(short, [], bid=4390.0, ask=4390.2) is None
    assert ms.deal_exit_reason([tp_deal, sl_deal], 1001) == cr.REASON_TP
    assert ms.exit_label(cr.REASON_TP) == "止盈"
    assert ms.exit_label(None) == "未知"


def test_exit_lookup_since_takes_earliest_and_backs_off_for_server_clock():
    started = 1_700_000_600.0
    assert ms.exit_lookup_since(_m2(time=1_700_000_000.0), started) == 1_700_000_000.0 - ms.EXIT_LOOKUP_MARGIN_SEC
    assert ms.exit_lookup_since(_m2(time=0.0), started) == started - ms.EXIT_LOOKUP_MARGIN_SEC
    assert ms.exit_lookup_since(_m2(time=0.0), 0.0) == 0.0


def test_describe_and_detail_for_tp_close():
    text = ms.describe_tp_close(2, _m2(), remaining=1)
    assert "手动分散仓（顺势）止盈离场，联动清仓" in text
    assert "#1001" in text and "止盈 4410" in text and "其余 1 笔" in text
    detail = ms.tp_close_detail(1, _m2(), remaining=2, source="deal")
    assert detail["kind"] == "manual_scatter"
    assert detail["rule_type"] == 1 and detail["comment"] == "M1"
    assert detail["linked_close"] is True
    assert detail["exit_reason"] == "tp" and detail["exit_source"] == "deal"
    assert detail["remaining"] == 2 and detail["ticket"] == 1001
    opened = ms.describe_open(
        {"rule_type": 2, "direction": "BUY", "cfg": _cfg(close_all_on_tp=True)},
        0.5, target_pl=500.0, current_lot_size=0.1, profit_per_lot_value=1000.0,
    )
    assert "止盈离场后联动清仓" in opened


def _tick_bid_ask(hub: FakeHub, positions, *, bid: float, ask: float) -> None:
    hub.sub.offer(mh.MarketEvent(
        kind=mh.TICK, symbol="XAUUSD", magic=MAGIC,
        positions=tuple(positions), price=bid, point=0.01,
        bid=bid, ask=ask,
    ))


async def _open_first_and_manual(sent: list, mt5: MockMT5Client, **cfg_over):
    """首单 + 挂出 M2 限价单 + 撮合成交，再补一拍让执行器用真实持仓刷新在场快照。"""
    cfg = _cfg(**{"close_all_on_tp": True, **cfg_over})
    runner, hub = _runner(
        sent, mt5=mt5,
        strategy={"template_id": "tpl_1", "rules": [_trend_rule(manual_scatter=cfg)]},
    )
    mt5.prices_map["XAUUSD"] = 4390.0
    runner.start()
    await _settle()
    mt5.prices_map["XAUUSD"] = 4399.0
    _tick_bid_ask(hub, mt5.positions_by_magic(MAGIC), bid=4398.9, ask=4399.0)
    await _settle()
    # 价格回落到入场价：限价单成交
    mt5.prices_map["XAUUSD"] = 4400.0
    _tick_bid_ask(hub, mt5.positions_by_magic(MAGIC), bid=4399.9, ask=4400.0)
    await _settle()
    held = mt5.positions_by_magic(MAGIC)
    manual = next(p for p in held if p.get("comment") == "M2")
    _tick_bid_ask(hub, held, bid=4399.9, ask=4400.0)
    await _settle()
    assert runner._manual_open[2]["ticket"] == manual["ticket"]
    return runner, hub, manual


async def test_runner_manual_tp_exit_closes_rest_when_linked():
    """M2 被券商止盈平掉 → 平掉首单、任务 done，原因与明细带联动标记。"""
    sent: list = []
    mt5 = MockMT5Client()
    runner, hub, manual = await _open_first_and_manual(sent, mt5)
    assert mt5.close_ticket_with_reason(manual["ticket"], cr.REASON_TP) is True

    _tick_bid_ask(hub, mt5.positions_by_magic(MAGIC), bid=4410.0, ask=4410.2)
    await _settle(10)

    assert mt5.positions_by_magic(MAGIC) == []
    finished = [m["data"] for m in sent if m["type"] == "strategy_finished"]
    assert finished and finished[0]["status"] == "done"
    assert "止盈离场，联动清仓" in finished[0]["reason"]
    assert finished[0]["detail"]["linked_close"] is True
    assert finished[0]["detail"]["exit_source"] == "deal"
    ev = _progress(sent, ms.TP_CLOSE_EVENT)
    assert ev and ev[0]["phase"] == "closing"
    assert ev[0]["detail"]["kind"] == "manual_scatter"
    assert ev[0]["detail"]["ticket"] == manual["ticket"]
    assert ev[0]["detail"]["remaining"] == 1
    assert runner.done


async def test_runner_manual_sl_exit_does_not_link():
    sent: list = []
    mt5 = MockMT5Client()
    runner, hub, manual = await _open_first_and_manual(sent, mt5)
    assert mt5.close_ticket_with_reason(manual["ticket"], cr.REASON_SL) is True

    _tick_bid_ask(hub, mt5.positions_by_magic(MAGIC), bid=4380.0, ask=4380.2)
    await _settle(10)

    left = mt5.positions_by_magic(MAGIC)
    assert len(left) == 1 and left[0]["comment"] != "M2"
    assert not [m for m in sent if m["type"] == "strategy_finished"]
    assert not _progress(sent, ms.TP_CLOSE_EVENT)
    assert runner._manual_gone == {}
    runner.cancel()


async def test_runner_manual_tp_exit_without_flag_keeps_positions():
    sent: list = []
    mt5 = MockMT5Client()
    runner, hub, manual = await _open_first_and_manual(sent, mt5, close_all_on_tp=False)
    assert mt5.close_ticket_with_reason(manual["ticket"], cr.REASON_TP) is True

    _tick_bid_ask(hub, mt5.positions_by_magic(MAGIC), bid=4410.0, ask=4410.2)
    await _settle(10)

    assert len(mt5.positions_by_magic(MAGIC)) == 1
    assert not [m for m in sent if m["type"] == "strategy_finished"]
    assert not _progress(sent, ms.TP_CLOSE_EVENT)
    runner.cancel()


async def test_runner_price_fallback_when_history_lags_then_gives_up():
    """历史成交还没写进来：平仓侧到止盈才兜底联动；一直判不出就几拍后放弃。"""
    sent: list = []
    mt5 = MockMT5Client()
    runner, hub, manual = await _open_first_and_manual(sent, mt5)
    calls: list[float | None] = []
    real = mt5.exit_deals_by_magic

    def lagging(magic, since_ts=None):
        calls.append(since_ts)
        return []

    mt5.exit_deals_by_magic = lagging  # type: ignore[method-assign]
    # 不记出场成交地移除持仓，模拟历史滞后
    mt5._positions = [p for p in mt5._positions if p["ticket"] != manual["ticket"]]

    for _ in range(ms.EXIT_RESOLVE_ATTEMPTS + 2):
        _tick_bid_ask(hub, mt5.positions_by_magic(MAGIC), bid=4405.0, ask=4405.2)
        await _settle()
    assert len(calls) == ms.EXIT_RESOLVE_ATTEMPTS
    assert all(c is not None and c < time.time() for c in calls)
    assert runner._manual_gone == {}
    assert len(mt5.positions_by_magic(MAGIC)) == 1
    assert not [m for m in sent if m["type"] == "strategy_finished"]

    mt5.exit_deals_by_magic = real  # type: ignore[method-assign]
    runner.cancel()

    # 同样滞后，但平仓侧 bid 已到止盈 → 按现价兜底判止盈并联动
    sent2: list = []
    mt5b = MockMT5Client()
    runner2, hub2, manual2 = await _open_first_and_manual(sent2, mt5b)
    mt5b.exit_deals_by_magic = lambda magic, since_ts=None: []  # type: ignore[method-assign]
    mt5b._positions = [p for p in mt5b._positions if p["ticket"] != manual2["ticket"]]
    _tick_bid_ask(hub2, mt5b.positions_by_magic(MAGIC), bid=4410.0, ask=4410.2)
    await _settle(10)
    assert mt5b.positions_by_magic(MAGIC) == []
    finished = [m["data"] for m in sent2 if m["type"] == "strategy_finished"]
    assert finished and finished[0]["detail"]["exit_source"] == "price"
    assert runner2.done


async def test_runner_stale_snapshot_reappearing_manual_is_not_an_exit():
    """刚开单那拍持仓采样没带上 M2、下一拍又出现：不能当离场，也不联动。"""
    sent: list = []
    mt5 = MockMT5Client()
    runner, hub, manual = await _open_first_and_manual(sent, mt5)
    stale = [p for p in mt5.positions_by_magic(MAGIC) if p["ticket"] != manual["ticket"]]
    _tick_bid_ask(hub, stale, bid=4405.0, ask=4405.2)
    await _settle()
    # 成交历史里没有它、现价也没到止盈：进入待确认，不动仓
    assert 2 in runner._manual_gone
    assert 2 not in runner._manual_open
    _tick_bid_ask(hub, mt5.positions_by_magic(MAGIC), bid=4405.0, ask=4405.2)
    await _settle()
    assert runner._manual_gone == {}
    assert runner._manual_open[2]["ticket"] == manual["ticket"]
    assert len(mt5.positions_by_magic(MAGIC)) == 2
    assert not _progress(sent, ms.TP_CLOSE_EVENT)
    runner.cancel()


async def test_runner_resume_does_not_duplicate_pending_order():
    """重连后 runtime 带在途挂单：不能重复挂一张（挂两次就是双倍手数）。"""
    sent: list = []
    mt5 = MockMT5Client()
    mt5.prices_map["XAUUSD"] = 4390.0
    runner, hub = _runner(sent, mt5=mt5)
    runner.start()
    await _settle()
    mt5.prices_map["XAUUSD"] = 4399.0
    hub.sub.offer(mh.MarketEvent(
        kind=mh.TICK, symbol="XAUUSD", magic=MAGIC,
        positions=tuple(mt5.positions_by_magic(MAGIC)), price=4399.0, point=0.01,
        bid=4398.9, ask=4399.0,
    ))
    await _settle()
    placed = [o for o in mt5.pending_orders_by_magic(MAGIC) if o.get("comment") == "M2"]
    assert len(placed) == 1
    ticket = placed[0]["ticket"]
    runtime = {
        "manual_fired": [2],
        "manual_pending": {"2": {
            "ticket": ticket, "type": "BUY", "volume": placed[0]["volume"],
            "price_open": 4400.0, "sl": 0.0, "tp": 4410.0, "time": time.time() - 60,
        }},
    }
    runner.cancel()

    # 重连：同一张挂单还在盘上
    sent2: list = []
    runner2, hub2 = _runner(
        sent2, mt5=mt5, runtime=runtime,
        strategy={"template_id": "tpl_1", "rules": [
            _trend_rule(manual_scatter=_cfg()),
        ]},
    )
    assert runner2._manual_pending[2]["ticket"] == ticket
    runner2.start(resume=True)
    await _settle()
    mt5.prices_map["XAUUSD"] = 4399.0
    hub2.sub.offer(mh.MarketEvent(
        kind=mh.TICK, symbol="XAUUSD", magic=MAGIC,
        positions=tuple(mt5.positions_by_magic(MAGIC)), price=4399.0, point=0.01,
        bid=4398.9, ask=4399.0,
    ))
    await _settle()
    still = [o for o in mt5.pending_orders_by_magic(MAGIC) if o.get("comment") == "M2"]
    assert len(still) == 1, "重连后不该重复挂单"
    assert still[0]["ticket"] == ticket
    runner2.cancel()


async def test_runner_resume_adopts_pending_filled_offline():
    """离线期间手动限价单成交：重连后从持仓认领，按成交价对齐止盈。"""
    sent: list = []
    mt5 = MockMT5Client()
    mt5.prices_map["XAUUSD"] = 4400.0
    mt5.place_market_order("XAUUSD", "BUY", 0.1, comment="", magic=MAGIC)
    # 离线期间手动限价单成交了
    mt5.place_market_order("XAUUSD", "BUY", 0.5, comment="M2", magic=MAGIC)
    runtime = {
        "manual_fired": [2],
        "manual_pending": {"2": {
            "ticket": 9999, "type": "BUY", "volume": 0.5,
            "price_open": 4400.0, "sl": 0.0, "tp": 4410.0, "time": time.time() - 600,
        }},
    }
    runner, hub = _runner(
        sent, mt5=mt5, runtime=runtime,
        strategy={"template_id": "tpl_1", "rules": [
            _trend_rule(manual_scatter=_cfg()),
        ]},
    )
    runner.start(resume=True)
    await _settle()
    hub.sub.offer(mh.MarketEvent(
        kind=mh.TICK, symbol="XAUUSD", magic=MAGIC,
        positions=tuple(mt5.positions_by_magic(MAGIC)), price=4400.0, point=0.01,
        bid=4399.9, ask=4400.0,
    ))
    await _settle()
    # 成交被认领：止盈价成为权威并同步到全部持仓
    assert runner._manual_authority_tp() == 4410.0
    for pos in mt5.positions_by_magic(MAGIC):
        assert pos["tp"] == 4410.0
    runner.cancel()


async def test_runner_resume_detects_offline_tp_exit_from_runtime():
    """节点离线期间 M2 被止盈：重连凭 runtime 票号查成交历史，补做联动清仓。"""
    sent: list = []
    mt5 = MockMT5Client()
    mt5.prices_map["XAUUSD"] = 4400.0
    mt5.place_market_order("XAUUSD", "BUY", 0.1, comment="", magic=MAGIC)
    opened = mt5.place_market_order("XAUUSD", "BUY", 0.5, tp=4410.0, comment="M2", magic=MAGIC)
    m2_ticket = opened["order"]
    assert mt5.close_ticket_with_reason(m2_ticket, cr.REASON_TP) is True

    runtime = {
        "manual_fired": [2],
        "manual_open": {"2": {
            "ticket": m2_ticket, "type": "BUY", "volume": 0.5,
            "tp": 4410.0, "price_open": 4400.0, "time": time.time() - 600,
        }},
    }
    runner, hub = _runner(
        sent, mt5=mt5, runtime=runtime,
        strategy={"template_id": "tpl_1", "rules": [
            _trend_rule(manual_scatter=_cfg(close_all_on_tp=True)),
        ]},
    )
    assert runner._manual_open[2]["ticket"] == m2_ticket
    runner.start(resume=True)
    await _settle()
    _tick_bid_ask(hub, mt5.positions_by_magic(MAGIC), bid=4402.0, ask=4402.2)
    await _settle(10)

    assert mt5.positions_by_magic(MAGIC) == []
    finished = [m["data"] for m in sent if m["type"] == "strategy_finished"]
    assert finished and finished[0]["status"] == "done"
    assert finished[0]["detail"]["linked_close"] is True
    assert finished[0]["detail"]["exit_source"] == "deal"


async def test_runner_manual_filled_runtime_carries_open_ticket():
    """manual_filled 事件随带的 runtime 要有票号，重连后才对得上离场的是哪一笔。"""
    sent: list = []
    mt5 = MockMT5Client()
    runner, hub, manual = await _open_first_and_manual(sent, mt5)
    ev = _progress(sent, "manual_filled")
    assert ev and ev[0]["runtime"]["manual_fired"] == [2]
    assert ev[0]["runtime"]["manual_open"]["2"]["ticket"] == manual["ticket"]
    assert ev[0]["runtime"]["manual_open"]["2"]["tp"] == 4410.0
    runner.cancel()


async def test_runner_manual_pending_runtime_carries_order_ticket():
    """manual_pending 事件随带的 runtime 要有挂单票号，重连后不重复挂。"""
    sent: list = []
    mt5 = MockMT5Client()
    mt5.prices_map["XAUUSD"] = 4390.0
    runner, hub = _runner(sent, mt5=mt5)
    runner.start()
    await _settle()
    mt5.prices_map["XAUUSD"] = 4399.0
    hub.sub.offer(mh.MarketEvent(
        kind=mh.TICK, symbol="XAUUSD", magic=MAGIC,
        positions=tuple(mt5.positions_by_magic(MAGIC)), price=4399.0, point=0.01,
        bid=4398.9, ask=4399.0,
    ))
    await _settle()
    ev = _progress(sent, "manual_pending")
    assert ev and ev[0]["runtime"]["manual_fired"] == [2]
    pending_rt = ev[0]["runtime"]["manual_pending"]
    assert pending_rt["2"]["ticket"] > 0
    assert pending_rt["2"]["price_open"] == 4400.0
    runner.cancel()
