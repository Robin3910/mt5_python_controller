"""模版1 信号级盈亏控制纯函数测试。"""
import signal_pl as sp


def _cfg(**over) -> dict:
    cfg = sp.default_config()
    for key, val in over.items():
        if key in cfg and isinstance(val, dict):
            cfg[key] = {**cfg[key], **val}
        else:
            cfg[key] = val
    return cfg


def test_extract_defaults_when_missing():
    cfg = sp.extract_config([{"type": 2, "status": 1, "action": "all"}])
    assert cfg["float_pl_ratio"]["enabled"] is False
    assert cfg["float_pl_ratio"]["ratio"] == -20.0
    assert "remaining_times" in cfg["float_pl_ratio"]
    assert cfg["lot_pl_tiers"]["enabled"] is False
    assert cfg["lot_pl_tiers"]["batch_count"] == 2


def test_extract_prefers_enabled_addon_rule():
    cfg = sp.extract_config([
        {
            "type": 1, "status": 0,
            "float_pl_ratio": {"enabled": True, "ratio": -10},
        },
        {
            "type": 2, "status": 1,
            "float_pl_ratio": {"enabled": True, "ratio": -30},
        },
    ])
    assert cfg["float_pl_ratio"]["ratio"] == -30.0


def test_signal_ratio_uses_signal_profit_over_balance():
    assert sp.signal_floating_pl_ratio(10000, -2000) == -20.0
    assert sp.signal_floating_pl_ratio(10000, 500) == 5.0
    assert sp.signal_floating_pl_ratio(0, -10) is None


def test_float_pl_triggers_on_signal_profit_not_account_equity():
    """同账户其它仓位的浮盈不应改变本信号盈亏比。"""
    cfg = _cfg(float_pl_ratio={"enabled": True, "ratio": -20})
    # 本信号亏 2000 / 余额 10000 = -20%；若误用账户净值会是 +30%
    hit = sp.should_trigger_float_pl(
        cfg, balance=10000, signal_profit=-2000, has_positions=True,
    )
    assert hit is not None
    assert hit["current_ratio"] == -20.0
    assert hit["close_action"] == "all"

    # 阈值未达
    miss = sp.should_trigger_float_pl(
        cfg, balance=10000, signal_profit=-1999, has_positions=True,
    )
    assert miss is None


def test_lot_pl_uses_batch1_absolute_amount_and_weighted_open():
    """两笔多单按手数加权；批次2的金额不参与，负数金额取绝对值。"""
    cfg = _cfg(lot_pl_tiers={
        "enabled": True,
        "batch_count": 2,
        "close_action": "all",
        "tiers": [
            {"min_lot": 0.1, "pl_amount": -30},
            {"min_lot": 0.5, "pl_amount": 999},
        ],
    })
    assert sp.lot_pl_amount(cfg) == 30.0
    positions = [
        {"type": "BUY", "ticket": 1, "volume": 0.1, "price_open": 2300},
        {"type": "BUY", "ticket": 2, "volume": 0.2, "price_open": 2310},
    ]
    # W = (0.1*2300 + 0.2*2310) / 0.3 = 2306.666...
    # D = 30 / ((1/0.01) * 0.3) = 1
    plans = sp.plan_common_stops(
        positions, tp_amount=sp.lot_pl_amount(cfg),
        tick_size=0.01, tick_value=1.0, digits=2,
        bid=2306.0,
    )
    assert len(plans) == 1
    plan = plans[0]
    assert plan["side"] == "BUY"
    assert plan["tp"] == 2307.67
    assert plan["sl"] is None
    assert plan["crossed"] is None
    assert plan["amount"] == 30.0
    both = sp.plan_common_stops(
        positions, tp_amount=30, sl_amount=30,
        tick_size=0.01, tick_value=1.0, digits=2, bid=2306.0,
    )
    assert both[0]["sl"] == 2305.67


def test_take_profit_waits_for_min_lot():
    positions = [{"type": "BUY", "ticket": 1, "volume": 0.1, "price_open": 2300}]
    below = sp.plan_common_stops(
        positions, tp_amount=10, sl_amount=30, tp_min_lot=0.2,
        tick_size=0.01, tick_value=1.0, digits=2, bid=2300,
    )
    assert below[0]["tp"] is None
    assert below[0]["sl"] == 2297.0
    reached = sp.plan_common_stops(
        [{"type": "BUY", "ticket": 1, "volume": 0.2, "price_open": 2300}],
        tp_amount=10, tp_min_lot=0.2,
        tick_size=0.01, tick_value=1.0, digits=2, bid=2300,
    )
    assert reached[0]["tp"] == 2300.5


def test_lot_pl_sell_stops_are_mirrored():
    positions = [{"type": "SELL", "ticket": 1, "volume": 1.0, "price_open": 1.1}]
    plans = sp.plan_common_stops(
        positions, tp_amount=10, sl_amount=10,
        tick_size=0.00001, tick_value=1.0, digits=5,
        ask=1.1,
    )
    assert plans[0]["tp"] == 1.0999
    assert plans[0]["sl"] == 1.1001


def test_lot_pl_buy_side_ignores_sell_and_crossed_take_profit():
    cfg = _cfg(lot_pl_tiers={
        "enabled": True,
        "close_action": "buy",
        "batch_count": 1,
        "tiers": [{"min_lot": 0.01, "pl_amount": 10}],
    })
    positions = [
        {"type": "SELL", "volume": 1.0, "price_open": 2300, "ticket": 1},
        {"type": "BUY", "volume": 0.1, "price_open": 2330, "ticket": 2},
    ]
    scoped = sp.filter_lot_side(positions, sp.lot_pl_close_action(cfg))
    assert [p["ticket"] for p in scoped] == [2]
    plans = sp.plan_common_stops(
        scoped, tp_amount=10, tick_size=0.01, tick_value=1.0, digits=2,
        bid=2331.0,
    )
    # D = 10 / (100 * 0.1) = 1，止盈 2331，买价已经到了；没给止损金额就不写止损
    assert plans[0]["tp"] == 2331.0
    assert plans[0]["sl"] is None
    assert plans[0]["crossed"] == "tp"


def test_lot_pl_does_not_market_close_inside_find_triggered_rule():
    cfg = _cfg(lot_pl_tiers={
        "enabled": True,
        "close_action": "all",
        "tiers": [{"min_lot": 0.1, "pl_amount": 10}],
    })
    positions = [{"type": "BUY", "volume": 1.0, "profit": 500, "price_open": 2300}]
    assert sp.find_triggered_rule(
        cfg, balance=10000, signal_profit=500, positions=positions,
    ) is None


def test_book_has_open_ignores_closes():
    first = ((1, 0.1, "BUY"),)
    assert sp.book_has_open((), first) is True
    assert sp.book_has_open(first, ((1, 0.1, "BUY"), (2, 0.1, "BUY"))) is True
    assert sp.book_has_open(first, ((1, 0.2, "BUY"),)) is True
    assert sp.book_has_open(
        ((1, 0.1, "BUY"), (2, 0.1, "BUY")), ((1, 0.1, "BUY"),),
    ) is False
    assert sp.book_has_open(first, ((1, 0.05, "BUY"),)) is False


def test_float_pl_ratio_becomes_loss_amount():
    """盈亏比只折成亏损金额，正负号都按绝对值。未启用不折。"""
    cfg = _cfg(float_pl_ratio={"enabled": True, "ratio": -20})
    assert sp.float_pl_loss_amount(cfg, 10000) == 2000.0
    assert sp.float_pl_loss_amount(
        _cfg(float_pl_ratio={"enabled": True, "ratio": 20}), 10000,
    ) == 2000.0
    assert sp.float_pl_loss_amount(
        _cfg(float_pl_ratio={"enabled": False, "ratio": -20}), 10000,
    ) is None


def test_opening_stops_include_the_new_fill():
    """新单按预计成交价并进加权价，下单时就能带上止损止盈。"""
    held = [{"type": "BUY", "volume": 0.1, "price_open": 2300.0}]
    sl, tp = sp.opening_stops(
        held, side="BUY", volume=0.1, fill_price=2310.0,
        tp_amount=10, sl_amount=20, tp_min_lot=0.1,
        tick_size=0.01, tick_value=1.0, digits=2,
        bid=2305.0, ask=2310.0,
    )
    # W = (0.1*2300 + 0.1*2310) / 0.2 = 2305
    # D_sl = 20 / 20 = 1，D_tp = 10 / 20 = 0.5
    assert sl == 2304.0
    assert tp == 2305.5
    # 空单：止盈在加权价之下，止损在上，且相对卖价都在合法一侧
    sell_sl, sell_tp = sp.opening_stops(
        [], side="SELL", volume=0.1, fill_price=2300.0,
        tp_amount=10, sl_amount=20,
        tick_size=0.01, tick_value=1.0, digits=2,
        bid=2300.0, ask=2300.1,
    )
    assert sell_sl == 2302.0
    assert sell_tp == 2299.0


def test_opening_stops_skip_unsendable_side():
    """现价已经越过、或近于 stops_level 的那一侧不挂，避免整张市价单被拒。"""
    common = dict(
        side="BUY", volume=0.1, fill_price=2300.0,
        tp_amount=10, sl_amount=2000, tp_min_lot=0.0,
        tick_size=0.01, tick_value=1.0, digits=2,
        bid=2300.0, ask=2300.0,
    )
    # D_tp = 10 / 10 = 1，止盈 2301；现价已在 2302，越过止盈
    sl, tp = sp.opening_stops([], bid=2302.0, ask=2302.0, **{
        k: v for k, v in common.items() if k not in ("bid", "ask")
    })
    assert sl == 2100.0
    assert tp is None

    # 止盈距现价 100 点，低于券商要求的 150 点，这一侧不挂；止损距离足够，仍挂
    near = sp.opening_stops(
        [], side="BUY", volume=0.1, fill_price=2300.0,
        tp_amount=10, sl_amount=2000, tp_min_lot=0.0,
        tick_size=0.01, tick_value=1.0, digits=2,
        bid=2300.0, ask=2300.0, stops_level=150, point=0.01,
    )
    assert near == (2100.0, None)


def test_opening_stops_respect_side_and_min_lot():
    assert sp.opening_stops(
        [], side="BUY", volume=0.1, fill_price=2300.0,
        tp_amount=10, sl_amount=20, close_action="sell",
        tick_size=0.01, tick_value=1.0, digits=2, bid=2300.0,
    ) is None
    sl, tp = sp.opening_stops(
        [], side="BUY", volume=0.1, fill_price=2300.0,
        tp_amount=10, sl_amount=20, tp_min_lot=0.2,
        tick_size=0.01, tick_value=1.0, digits=2, bid=2300.0,
    )
    assert sl == 2298.0
    assert tp is None
    assert sp.opening_stops(
        [], side="BUY", volume=0.1, fill_price=0,
        tp_amount=10, sl_amount=20,
        tick_size=0.01, tick_value=1.0, digits=2,
    ) is None
