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
        "trailing_up": False, "trailing_max": 0,
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


def test_plan_grid_rejects_collapsed_levels():
    """区间太窄时多条网格线会被四舍五入到同一价位，一次穿越能连开好几格。"""
    plan = gt.plan_grid(
        _cfg(price_lower=100.0, price_upper=100.01, grid_count=10),
        signal_action="BUY", spec=_spec(),
    )
    assert not plan.ok
    assert "精度" in plan.reject


def test_plan_grid_rejects_total_lot_below_one_grid():
    """一格都开不出来的配置直接拒绝，否则任务只会空跑着占用节点。"""
    plan = gt.plan_grid(
        _cfg(lot_per_grid=0.1, total_lot_limit=0.05), signal_action="BUY", spec=_spec(),
    )
    assert not plan.ok
    assert "总手数上限" in plan.reject


def test_resolve_side():
    assert gt.resolve_side(_cfg(grid_side="long"), "SELL") == "BUY"
    assert gt.resolve_side(_cfg(grid_side="short"), "BUY") == "SELL"
    assert gt.resolve_side(_cfg(grid_side="follow"), "SELL") == "SELL"
    assert gt.resolve_side(_cfg(grid_side="follow"), "BUY") == "BUY"


def test_prefill_indices_long():
    plan = gt.plan_grid(_cfg(grid_count=4), signal_action="BUY", spec=_spec())
    # levels: 100, 102.5, 105, 107.5, 110；现价 106 是市价成本，
    # 只有卖出线仍高于 106 的格能盈利兑现：格 2（卖 107.5）、格 3（卖 110）
    assert gt.prefill_indices(plan, 106.0) == [2, 3]


def test_prefill_indices_short():
    plan = gt.plan_grid(
        _cfg(grid_side="short", grid_count=4), signal_action="SELL", spec=_spec(),
    )
    # 现价 106 市价开空，只有平仓线仍低于 106 的格能盈利兑现
    assert gt.prefill_indices(plan, 106.0) == [0, 1, 2]


def test_prefill_never_locks_in_a_loss():
    """预填的每一格，退出线都必须在现价的盈利侧。"""
    price = 106.0
    long_plan = gt.plan_grid(_cfg(grid_count=4), signal_action="BUY", spec=_spec())
    for i in gt.prefill_indices(long_plan, price):
        assert long_plan.levels[i + 1] > price

    short_plan = gt.plan_grid(
        _cfg(grid_side="short", grid_count=4), signal_action="SELL", spec=_spec(),
    )
    for i in gt.prefill_indices(short_plan, price):
        assert short_plan.levels[i] < price


def test_capped_prefill():
    cfg = _cfg(grid_count=4, lot_per_grid=0.1, total_lot_limit=0.2)
    plan = gt.plan_grid(cfg, signal_action="BUY", spec=_spec())
    indices = gt.prefill_indices(plan, 101.0)
    assert indices == [0, 1, 2, 3]
    # 0.2 / 0.1 = 2 格；多头留卖出线离现价最近的两格
    assert gt.capped_prefill(plan, cfg, indices) == [0, 1]

    short_cfg = _cfg(
        grid_side="short", grid_count=4, lot_per_grid=0.1, total_lot_limit=0.2,
    )
    short_plan = gt.plan_grid(short_cfg, signal_action="SELL", spec=_spec())
    short_indices = gt.prefill_indices(short_plan, 109.0)
    assert short_indices == [0, 1, 2, 3]
    # 空头留平仓线离现价最近的两格
    assert gt.capped_prefill(short_plan, short_cfg, short_indices) == [2, 3]


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
    plan = gt.plan_grid(
        _cfg(stop_lower=95.0, stop_upper=120.0), signal_action="BUY", spec=_spec(),
    )
    assert "止损" in (gt.terminate_reason(plan, 94.0) or "")
    assert "止盈" in (gt.terminate_reason(plan, 121.0) or "")
    assert gt.terminate_reason(plan, 105.0) is None

    # 空头网格的止损止盈方向相反
    short = gt.plan_grid(
        _cfg(grid_side="short", stop_lower=95.0, stop_upper=120.0),
        signal_action="SELL", spec=_spec(),
    )
    assert "止损" in (gt.terminate_reason(short, 121.0) or "")
    assert "止盈" in (gt.terminate_reason(short, 94.0) or "")


def test_trigger_reached_is_direction_agnostic():
    cfg = _cfg(trigger_price=105.0)
    # 首个报价只用来确定所处一侧，正好落在触发价上才算到价
    assert gt.trigger_reached(cfg, 106.0, prev_price=0.0) is False
    assert gt.trigger_reached(cfg, 104.0, prev_price=0.0) is False
    assert gt.trigger_reached(cfg, 105.0, prev_price=0.0) is True
    # 之后从任一侧触及都算：上涨触及、回落触及
    assert gt.trigger_reached(cfg, 105.0, prev_price=104.0) is True
    assert gt.trigger_reached(cfg, 104.5, prev_price=106.0) is True
    # 同侧移动不算
    assert gt.trigger_reached(cfg, 106.5, prev_price=106.0) is False
    assert gt.trigger_reached(_cfg(trigger_price=0), 1.0) is True


def test_grid_comment_roundtrip():
    assert gt.grid_comment(12) == "G4L12"
    assert gt.parse_grid_comment("G4L12") == 12
    assert gt.parse_grid_comment("other") is None
    # 空头追跌会把绝对格位推成负数
    assert gt.grid_comment(-3) == "G4L-3"
    assert gt.parse_grid_comment("G4L-3") == -3


# ---------------------------------------------------------------------------
# 向上追踪
# ---------------------------------------------------------------------------

def _trailing_plan(**over) -> tuple[gt.GridConfig, gt.GridPlan]:
    """区间 100~110 切 4 格（间距 2.5）的多头追踪网格。"""
    cfg = _cfg(grid_count=4, trailing_up=True, **over)
    action = "SELL" if cfg.grid_side == gt.GRID_SIDE_SHORT else "BUY"
    return cfg, gt.plan_grid(cfg, signal_action=action, spec=_spec())


def test_trailing_shift_skipped_when_disabled_or_inside_range():
    cfg, plan = _trailing_plan()
    assert gt.trailing_shift(plan, cfg, 105.0, digits=2) is None   # 区间内

    off_cfg = _cfg(grid_count=4)
    off_plan = gt.plan_grid(off_cfg, signal_action="BUY", spec=_spec())
    assert gt.trailing_shift(off_plan, off_cfg, 200.0, digits=2) is None


def test_trailing_shift_up_one_grid():
    cfg, plan = _trailing_plan()
    shift = gt.trailing_shift(plan, cfg, 111.0, digits=2)
    assert shift is not None
    assert shift.steps == 1
    assert shift.direction == "up"
    assert shift.levels == [102.5, 105.0, 107.5, 110.0, 112.5]

    gt.apply_shift(plan, shift)
    assert plan.shift_count == 1
    assert plan.levels[-1] == 112.5
    assert plan.grid_count == 4


def test_trailing_shift_catches_up_on_gap():
    """一次跳空跨多格时循环平移到区间重新盖住现价。"""
    cfg, plan = _trailing_plan()
    shift = gt.trailing_shift(plan, cfg, 116.0, digits=2)
    assert shift.steps == 3
    assert shift.levels == [107.5, 110.0, 112.5, 115.0, 117.5]


def test_trailing_shift_respects_max():
    cfg, plan = _trailing_plan(trailing_max=2)
    shift = gt.trailing_shift(plan, cfg, 500.0, digits=2)
    assert shift.steps == 2
    gt.apply_shift(plan, shift)
    # 额度耗尽后价格再高也不再平移
    assert gt.trailing_shift(plan, cfg, 500.0, digits=2) is None


def test_trailing_shift_moves_stops_together():
    cfg, plan = _trailing_plan(stop_lower=95.0, stop_upper=120.0)
    shift = gt.trailing_shift(plan, cfg, 111.0, digits=2)
    assert shift.stop_lower == 97.5
    assert shift.stop_upper == 122.5

    gt.apply_shift(plan, shift)
    # 止损跟着上移，等于锁住了平移这一格的利润
    assert gt.terminate_reason(plan, 96.0) is not None
    assert gt.terminate_reason(plan, 98.5) is None


def test_trailing_shift_remaps_holdings_and_drops_outliers():
    cfg, plan = _trailing_plan()
    plan.holdings = {0: 1001, 2: 1003}
    shift = gt.trailing_shift(plan, cfg, 111.0, digits=2)
    assert shift.holdings == {1: 1003}       # 格位 2 上移后变成 1
    assert shift.dropped == [(0, 1001)]      # 最低格被挤出网格，需兑现


def test_trailing_shift_short_moves_down():
    cfg, plan = _trailing_plan(grid_side="short")
    assert plan.side == "SELL"
    shift = gt.trailing_shift(plan, cfg, 99.0, digits=2)
    assert shift.steps == -1
    assert shift.direction == "down"
    assert shift.levels == [97.5, 100.0, 102.5, 105.0, 107.5]


def test_trailing_shift_geometric_keeps_ratio():
    cfg = _cfg(
        price_lower=100.0, price_upper=121.0, grid_count=2,
        grid_mode="geometric", trailing_up=True,
    )
    plan = gt.plan_grid(cfg, signal_action="BUY", spec=_spec(digits=4))
    shift = gt.trailing_shift(plan, cfg, 122.0, digits=4)
    assert shift.steps == 1
    # 公比 1.1：[100, 110, 121] → [110, 121, 133.1]
    assert abs(shift.levels[0] - 110.0) < 0.01
    assert abs(shift.levels[-1] - 133.1) < 0.01


def test_absolute_index_survives_shift():
    cfg, plan = _trailing_plan()
    gt.apply_shift(plan, gt.trailing_shift(plan, cfg, 111.0, digits=2))
    assert plan.absolute_index(0) == 1       # 平移后的第 0 格就是原来的第 1 格
    assert plan.relative_index(1) == 0
    assert plan.relative_index(0) is None    # 原第 0 格已被挤出


def test_rebuild_holdings_after_shift_reports_orphans():
    cfg, plan = _trailing_plan()
    gt.apply_shift(plan, gt.trailing_shift(plan, cfg, 111.0, digits=2))
    orphans = gt.rebuild_holdings(plan, [
        {"ticket": 11, "comment": "G4L2", "price_open": 105.0},
        {"ticket": 22, "comment": "G4L0", "price_open": 100.0},
    ])
    assert plan.holdings == {1: 11}
    assert orphans == [22]


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
