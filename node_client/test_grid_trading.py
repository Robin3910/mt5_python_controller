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
        "close_on_stop": True,
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


def test_config_ignores_config_period_assist_fields():
    """assist_* 只是后台配置期的试算留痕，执行层必须原样忽略。"""
    plain = _cfg()
    assisted = _cfg(
        assist_enabled=True, assist_timeframe="H4",
        assist_atr_mult=1.5, assist_spacing=2.5, assist_max_loss=800.0,
    )
    assert assisted == plain

    spec = _spec()
    assert gt.plan_grid(assisted, signal_action="BUY", spec=spec).levels == (
        gt.plan_grid(plain, signal_action="BUY", spec=spec).levels
    )


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


def test_entry_and_exit_prices_long():
    """多头：入场价在下侧，出场价在上侧，两条线之间就是这一格吃的差价。"""
    plan = gt.plan_grid(_cfg(grid_count=4), signal_action="BUY", spec=_spec())
    # levels: 100, 102.5, 105, 107.5, 110
    assert gt.entry_price(plan, 0) == 100.0
    assert gt.exit_price(plan, 0) == 102.5
    assert gt.entry_price(plan, 3) == 107.5
    assert gt.exit_price(plan, 3) == 110.0


def test_entry_and_exit_prices_short():
    """空头：出场价在下侧，入场价在上侧。"""
    plan = gt.plan_grid(
        _cfg(grid_side="short", grid_count=4), signal_action="SELL", spec=_spec(),
    )
    assert gt.entry_price(plan, 0) == 102.5
    assert gt.exit_price(plan, 0) == 100.0
    assert gt.entry_price(plan, 3) == 110.0
    assert gt.exit_price(plan, 3) == 107.5


def test_exit_price_always_on_profit_side():
    """出场价恒在入场价的盈利侧——否则那一格一成交就是浮亏。"""
    for side, action in (("long", "BUY"), ("short", "SELL")):
        plan = gt.plan_grid(
            _cfg(grid_side=side, grid_count=4), signal_action=action, spec=_spec(),
        )
        for i in range(plan.grid_count):
            ep, xp = gt.entry_price(plan, i), gt.exit_price(plan, i)
            if plan.is_long:
                assert xp > ep
            else:
                assert xp < ep


def test_placeable_levels_long_only_below_price():
    """多头 BUY LIMIT 必须挂在现价下方；挂在上方会被券商直接拒单。"""
    plan = gt.plan_grid(_cfg(grid_count=4), signal_action="BUY", spec=_spec())
    # levels: 100, 102.5, 105, 107.5, 110；现价 106
    assert gt.placeable_levels(plan, 106.0) == [0, 1, 2]
    for i in gt.placeable_levels(plan, 106.0):
        assert gt.entry_price(plan, i) < 106.0


def test_placeable_levels_short_only_above_price():
    """空头 SELL LIMIT 必须挂在现价上方。"""
    plan = gt.plan_grid(
        _cfg(grid_side="short", grid_count=4), signal_action="SELL", spec=_spec(),
    )
    assert gt.placeable_levels(plan, 106.0) == [2, 3]
    for i in gt.placeable_levels(plan, 106.0):
        assert gt.entry_price(plan, i) > 106.0


def test_placeable_levels_needs_price():
    """没有现价就挂不了单：方向判不出来。"""
    plan = gt.plan_grid(_cfg(grid_count=4), signal_action="BUY", spec=_spec())
    assert gt.placeable_levels(plan, 0.0) == []


def test_capped_levels_counts_pending_and_holdings():
    """总手数上限把「在途挂单 + 已成交持仓」一起算进去。"""
    cfg = _cfg(grid_count=4, lot_per_grid=0.1, total_lot_limit=0.3)
    plan = gt.plan_grid(cfg, signal_action="BUY", spec=_spec())
    assert gt.capped_levels(plan, cfg, [0, 1, 2, 3]) == [0, 1, 2]
    # 两格成交了（在途 0 + 持仓 2 格 = 0.2 手），只剩 0.1 手额度 → 还能挂 1 格
    plan.holdings = {0: 1001, 1: 1002}
    assert gt.capped_levels(plan, cfg, [2, 3]) == [2]
    assert gt.can_place_more(plan, cfg) is True
    # 三格在手（0.3 手）→ 额度用尽，一格都挂不了
    plan.holdings = {0: 1001, 1: 1002, 2: 1003}
    assert gt.capped_levels(plan, cfg, [3]) == []
    assert gt.can_place_more(plan, cfg) is False


def test_tp_is_sendable_checks_direction_and_distance():
    """出场价方向反了、或离入场价太近，都不能写进挂单（券商会拒整张单）。"""
    plan = gt.plan_grid(_cfg(grid_count=4), signal_action="BUY", spec=_spec())
    assert gt.tp_is_sendable(plan, 0) is True
    # point=2.5、stops_level=2 → 出场价要离入场价至少 5.0
    assert gt.tp_is_sendable(plan, 0, stops_level=2, point=2.5) is False
    assert gt.tp_is_sendable(plan, 0, stops_level=1, point=2.5) is True


def test_tp_is_sendable_rejects_wrong_side():
    """出场价落到亏损侧就不能挂——这一格没有可兑现的差价。"""
    plan = gt.plan_grid(_cfg(grid_count=4), signal_action="BUY", spec=_spec())
    plan.levels = [100.0, 99.0, 105.0, 107.5, 110.0]
    # 格 0 的出场价 99.0 低于入场价 100.0 → 不合法
    assert gt.tp_is_sendable(plan, 0) is False


def test_stop_price_follows_grid_direction():
    """终止价就是止损价：多头取 stop_lower，空头取 stop_upper。"""
    long_plan = gt.plan_grid(
        _cfg(grid_count=4, stop_lower=95.0, stop_upper=120.0),
        signal_action="BUY", spec=_spec(),
    )
    assert gt.stop_price(long_plan) == 95.0
    short_plan = gt.plan_grid(
        _cfg(grid_count=4, grid_side="short", stop_lower=95.0, stop_upper=120.0),
        signal_action="SELL", spec=_spec(),
    )
    assert gt.stop_price(short_plan) == 120.0
    # 没配终止价 → 不写止损
    plain = gt.plan_grid(_cfg(grid_count=4), signal_action="BUY", spec=_spec())
    assert gt.stop_price(plain) == 0.0


def test_sl_is_sendable_checks_direction_and_distance():
    """终止价方向反了、或离入场价太近，都不能写进挂单的止损。"""
    plan = gt.plan_grid(
        _cfg(grid_count=4, stop_lower=95.0), signal_action="BUY", spec=_spec(),
    )
    # 格 0 的入场价 100.0，终止价 95.0 在亏损侧 → 合法
    assert gt.sl_is_sendable(plan, 0) is True
    # point=2.5、stops_level=3 → 终止价要离入场价至少 7.5（实际只有 5.0）
    assert gt.sl_is_sendable(plan, 0, stops_level=3, point=2.5) is False
    assert gt.sl_is_sendable(plan, 0, stops_level=2, point=2.5) is True


def test_sl_is_sendable_rejects_stop_above_entry_for_long():
    """多头的终止价高于入场价就是「止损在盈利侧」，不合法。"""
    plan = gt.plan_grid(
        _cfg(grid_count=4, stop_lower=105.0), signal_action="BUY", spec=_spec(),
    )
    assert gt.sl_is_sendable(plan, 0) is False


def test_sl_is_sendable_requires_configured_stop():
    """没配终止价就没有止损可写。"""
    plan = gt.plan_grid(_cfg(grid_count=4), signal_action="BUY", spec=_spec())
    assert gt.sl_is_sendable(plan, 0) is False


def test_stop_is_sendable_uses_reference_price():
    """改单阶段按现价校验：终止价必须落在现价的正确一侧且足够远。"""
    plan = gt.plan_grid(
        _cfg(grid_count=4, stop_lower=95.0), signal_action="BUY", spec=_spec(),
    )
    # 现价 100.0，终止价 95.0 在下方 → 合法
    assert gt.stop_is_sendable(plan, 100.0) is True
    # 现价已经跌到 94.0（终止价之下），此时改止损会被券商拒
    assert gt.stop_is_sendable(plan, 94.0) is False


def test_level_of_maps_ticket_to_level():
    """持仓票号 → 当前格位。"""
    plan = gt.plan_grid(_cfg(grid_count=4), signal_action="BUY", spec=_spec())
    plan.holdings = {0: 1001, 2: 1003}
    assert plan.level_of(1001) == 0
    assert plan.level_of(1003) == 2
    assert plan.level_of(9999) is None


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


def test_trailing_shift_remaps_pending_and_drops_outliers():
    """平移后挂单也要跟着换格位；被挤出去的单必须撤掉（留在盘上会成交成孤儿）。"""
    cfg, plan = _trailing_plan()
    plan.pending = {0: 2001, 2: 2003}
    shift = gt.trailing_shift(plan, cfg, 111.0, digits=2)
    assert shift.pending == {1: 2003}
    assert shift.dropped_pending == [(0, 2001)]

    gt.apply_shift(plan, shift)
    # 在途挂单一律清空：调用方会撤单后按新格线重挂
    assert plan.pending == {}


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


def test_rebuild_restores_pending_orders():
    """断线恢复：在途挂单靠备注还原成格位映射，恢复后不会重复挂同一格。"""
    plan = gt.plan_grid(_cfg(grid_count=4), signal_action="BUY", spec=_spec())
    orders = [
        {"ticket": 31, "comment": "G4L0", "price_open": 100.0},
        {"ticket": 32, "comment": "G4L2", "price_open": 105.0},
        # 认不出的挂单不认领：宁可当成没有，也不冒重挂出双份手数的险
        {"ticket": 33, "comment": "junk", "price_open": 107.5},
    ]
    gt.rebuild_holdings(plan, [], orders)
    assert plan.pending == {0: 31, 2: 32}
    assert 33 not in plan.pending.values()


def test_rebuild_reports_orphan_pending_after_shift():
    """挂单的格位被平移挤出网格 → 报为孤儿，调用方要撤单。"""
    cfg, plan = _trailing_plan()
    gt.apply_shift(plan, gt.trailing_shift(plan, cfg, 111.0, digits=2))
    orphans = gt.rebuild_holdings(plan, [], [
        {"ticket": 31, "comment": "G4L0", "price_open": 100.0},
    ])
    assert orphans == [31]
    assert plan.pending == {}


def test_pick_grid_rule():
    assert gt.pick_grid_rule([{"type": 4, "status": 1}]) is not None
    assert gt.pick_grid_rule([{"type": 4, "status": 0}]) is None
    assert gt.pick_grid_rule([{"type": 3, "status": 1}]) is None
