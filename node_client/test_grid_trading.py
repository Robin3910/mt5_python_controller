"""网格交易纯逻辑单测。"""
import grid_trading as gt


def _cfg(**over) -> gt.GridConfig:
    raw = {
        "type": 4, "status": 1,
        "price_lower": 100.0, "price_upper": 110.0,
        "grid_count": 10, "grid_mode": "arithmetic",
        "grid_side": "long", "lot_per_grid": 0.01,
        "total_lot_limit": 0.0, "trigger_price": 0.0,
        "stop_lower": 0.0, "stop_upper": 0.0,
        "close_on_stop": True, "prefill_enabled": True,
    }
    raw.update(over)
    return gt.GridConfig.from_rule(raw)


def _spec(**over) -> gt.SymbolSpec:
    raw = dict(point=0.01, digits=2, volume_min=0.01, volume_step=0.01, volume_max=100.0)
    raw.update(over)
    return gt.SymbolSpec(**raw)


def test_build_levels_arithmetic():
    levels = gt.build_levels(_cfg(grid_count=4), digits=2)
    assert levels == [100.0, 102.5, 105.0, 107.5, 110.0]


def test_build_levels_geometric():
    levels = gt.build_levels(
        _cfg(price_lower=100.0, price_upper=121.0, grid_count=2, grid_mode="geometric"),
        digits=4,
    )
    assert levels[0] == 100.0
    assert levels[-1] == 121.0
    assert abs(levels[1] - 110.0) < 0.01


def test_plan_grid_ok():
    plan = gt.plan_grid(_cfg(), signal_action="BUY", spec=_spec())
    assert plan.ok
    assert plan.side == "BUY"
    assert plan.grid_count == 10
    assert plan.lot_per_grid == 0.01
    assert plan.triggered is True


def test_plan_grid_rejects_bad_range():
    cfg = _cfg()
    cfg.price_lower = 110
    cfg.price_upper = 100
    plan = gt.plan_grid(cfg, signal_action="BUY", spec=_spec())
    assert not plan.ok
    assert "上限" in plan.reject


def test_resolve_side():
    assert gt.resolve_side(_cfg(grid_side="long"), "SELL") == "BUY"
    assert gt.resolve_side(_cfg(grid_side="short"), "BUY") == "SELL"
    assert gt.resolve_side(_cfg(grid_side="follow"), "SELL") == "SELL"
    assert gt.resolve_side(_cfg(grid_side="follow"), "BUY") == "BUY"


def test_prefill_indices_long():
    plan = gt.plan_grid(_cfg(grid_count=4), signal_action="BUY", spec=_spec())
    # levels: 100, 102.5, 105, 107.5, 110；现价 106 → 买线 < 106 的格：0,1,2
    assert gt.prefill_indices(plan, 106.0) == [0, 1, 2]


def test_capped_prefill():
    plan = gt.plan_grid(
        _cfg(grid_count=4, lot_per_grid=0.1, total_lot_limit=0.2),
        signal_action="BUY", spec=_spec(),
    )
    indices = gt.prefill_indices(plan, 106.0)
    capped = gt.capped_prefill(plan, _cfg(lot_per_grid=0.1, total_lot_limit=0.2), indices)
    assert len(capped) == 2
    assert capped == indices[-2:]


def test_crossings_down_then_up_no_repeat():
    plan = gt.plan_grid(_cfg(grid_count=4), signal_action="BUY", spec=_spec())
    # levels: 100, 102.5, 105, 107.5, 110
    # 下跌用 price<=level<prev：起点价本身不算穿越，避免同价反复触发
    downs = gt.crossings(plan, 105.0, 102.0)
    assert [c.level_index for c in downs] == [1]          # 只穿 102.5
    assert all(c.direction == "down" for c in downs)

    # 上涨用 prev<level<=price
    ups = gt.crossings(plan, 102.0, 105.0)
    assert [c.level_index for c in ups] == [1, 2]         # 102.5 与 105
    assert all(c.direction == "up" for c in ups)

    assert gt.crossings(plan, 105.0, 105.0) == []


def test_buy_sell_level_for_crossing_long():
    plan = gt.plan_grid(_cfg(grid_count=4), signal_action="BUY", spec=_spec())
    down = gt.Crossing(level_index=2, direction="down", level_price=105.0)
    assert gt.buy_level_for_crossing(plan, down) == 2
    plan.holdings[2] = 1001
    assert gt.buy_level_for_crossing(plan, down) is None

    up = gt.Crossing(level_index=3, direction="up", level_price=107.5)
    assert gt.sell_level_for_crossing(plan, up) == 2
    plan.holdings.pop(2)
    assert gt.sell_level_for_crossing(plan, up) is None


def test_terminate_reason():
    cfg = _cfg(stop_lower=95.0, stop_upper=120.0)
    assert gt.terminate_reason(cfg, 94.0, side="BUY") is not None
    assert "止损" in (gt.terminate_reason(cfg, 94.0, side="BUY") or "")
    assert "止盈" in (gt.terminate_reason(cfg, 121.0, side="BUY") or "")
    assert gt.terminate_reason(cfg, 105.0, side="BUY") is None


def test_trigger_reached():
    cfg = _cfg(trigger_price=105.0)
    assert gt.trigger_reached(cfg, 106.0, prev_price=0.0) is True
    assert gt.trigger_reached(cfg, 104.0, prev_price=0.0) is False
    assert gt.trigger_reached(cfg, 105.0, prev_price=104.0) is True
    assert gt.trigger_reached(_cfg(trigger_price=0), 1.0) is True


def test_grid_comment_roundtrip():
    assert gt.grid_comment(12) == "G4L12"
    assert gt.parse_grid_comment("G4L12") == 12
    assert gt.parse_grid_comment("other") is None


def test_rebuild_holdings_from_comment_and_price():
    plan = gt.plan_grid(_cfg(grid_count=4), signal_action="BUY", spec=_spec())
    positions = [
        {"ticket": 11, "comment": "G4L1", "price_open": 102.5},
        {"ticket": 22, "comment": "junk", "price_open": 105.0},
    ]
    gt.rebuild_holdings(plan, positions)
    assert plan.holdings[1] == 11
    assert plan.holdings[2] == 22


def test_pick_grid_rule():
    assert gt.pick_grid_rule([{"type": 4, "status": 1}]) is not None
    assert gt.pick_grid_rule([{"type": 4, "status": 0}]) is None
    assert gt.pick_grid_rule([{"type": 3, "status": 1}]) is None
