"""以损定量趋势单（策略模版2）的纯逻辑单测。"""
import pytest

from risk_sizing import (
    ENTRY_BREAKOUT,
    ENTRY_PULLBACK,
    RULE_TYPE_RISK_SIZED,
    RiskSizedConfig,
    SymbolSpec,
    anchor_to_fill,
    batch_comment,
    batch_detail,
    breakeven_move,
    describe_batch,
    describe_plan,
    next_batch,
    pick_risk_sized_rule,
    plan_detail,
    plan_entries,
    weighted_avg_price,
)

# 黄金类规格：一手一 point(0.01) 值 1 美元，即一手一美元价差值 100 美元
GOLD = SymbolSpec(
    point=0.01, digits=2, tick_size=0.01, tick_value=1.0,
    volume_min=0.01, volume_step=0.01, volume_max=100.0,
)


def rule(**over) -> dict:
    base = {
        "type": RULE_TYPE_RISK_SIZED,
        "status": 1,
        "action": "all",
        "risk_amount": 300.0,
        "rr_ratio": 2.5,
        "base_ratio": 30.0,
        "add_batches": 2,
        "entry_direction": ENTRY_PULLBACK,
        "batch_gap_points": 100.0,
        "max_total_lot": 0.0,
        "breakeven_enabled": False,
        "breakeven_times": 1.0,
    }
    base.update(over)
    return base


def cfg(**over) -> RiskSizedConfig:
    return RiskSizedConfig.from_rule(rule(**over))


# ---------------------------------------------------------------------------
# 规则识别
# ---------------------------------------------------------------------------

def test_pick_rule_only_matches_enabled_risk_sized():
    assert pick_risk_sized_rule([rule()]) is not None
    assert pick_risk_sized_rule([rule(status=0)]) is None
    assert pick_risk_sized_rule([{"type": 1, "status": 1}]) is None
    assert pick_risk_sized_rule(None) is None
    assert pick_risk_sized_rule([{"type": 1, "status": 1}, rule()]) is not None


def test_config_forces_full_base_when_not_batching():
    c = cfg(add_batches=0, base_ratio=30)
    assert c.base_ratio == 100.0


def test_config_falls_back_on_illegal_entry_direction():
    assert cfg(entry_direction="sideways").entry_direction == ENTRY_PULLBACK


# ---------------------------------------------------------------------------
# 手数反推
# ---------------------------------------------------------------------------

def test_total_lot_comes_from_risk_over_stop_distance():
    # 止损距离 3 美元 = 300 点，一手亏 300 美元；风险 300 -> 总手数 1.0
    plan = plan_entries(cfg(), direction="BUY", entry_price=2400.0, stop_loss=2397.0, spec=GOLD)
    assert plan.ok
    assert plan.sl_points == pytest.approx(300.0)
    assert plan.loss_per_lot == pytest.approx(300.0)
    assert plan.total_lot == pytest.approx(1.0)
    assert plan.risk_used == pytest.approx(300.0)


def test_total_lot_scales_with_risk_amount():
    a = plan_entries(cfg(risk_amount=300), direction="BUY", entry_price=2400.0,
                     stop_loss=2397.0, spec=GOLD)
    b = plan_entries(cfg(risk_amount=600), direction="BUY", entry_price=2400.0,
                     stop_loss=2397.0, spec=GOLD)
    assert b.total_lot == pytest.approx(a.total_lot * 2)


def test_wider_stop_gives_smaller_lot_at_same_risk():
    tight = plan_entries(cfg(), direction="BUY", entry_price=2400.0, stop_loss=2397.0, spec=GOLD)
    wide = plan_entries(cfg(), direction="BUY", entry_price=2400.0, stop_loss=2394.0, spec=GOLD)
    assert wide.total_lot < tight.total_lot
    # 风险金额固定，两种止损的实际敞口都不超过设定值
    assert wide.risk_used <= 300.0
    assert tight.risk_used <= 300.0


def test_lot_is_floored_to_volume_step_and_never_exceeds_risk():
    # 止损 3.33 美元 -> 一手亏 333，300/333 = 0.9009 -> 向下取整到 0.9
    plan = plan_entries(cfg(), direction="BUY", entry_price=2400.0, stop_loss=2396.67, spec=GOLD)
    assert plan.total_lot == pytest.approx(0.9)
    assert plan.risk_used <= 300.0


def test_max_total_lot_caps_the_result():
    plan = plan_entries(cfg(max_total_lot=0.5), direction="BUY", entry_price=2400.0,
                        stop_loss=2397.0, spec=GOLD)
    assert plan.total_lot == pytest.approx(0.5)


def test_volume_max_caps_the_result():
    spec = SymbolSpec(point=0.01, digits=2, tick_size=0.01, tick_value=1.0,
                      volume_min=0.01, volume_step=0.01, volume_max=0.3)
    plan = plan_entries(cfg(), direction="BUY", entry_price=2400.0, stop_loss=2397.0, spec=spec)
    assert plan.total_lot == pytest.approx(0.3)


def test_sell_direction_uses_stop_above_entry():
    plan = plan_entries(cfg(), direction="SELL", entry_price=2400.0, stop_loss=2403.0, spec=GOLD)
    assert plan.ok
    assert plan.total_lot == pytest.approx(1.0)
    assert plan.take_profit == pytest.approx(2392.5)  # 2400 - 3 × 2.5


# ---------------------------------------------------------------------------
# 拒绝开仓的情形
# ---------------------------------------------------------------------------

def test_reject_without_stop_loss():
    plan = plan_entries(cfg(), direction="BUY", entry_price=2400.0, stop_loss=0.0, spec=GOLD)
    assert not plan.ok
    assert "止损价" in plan.reject


def test_reject_when_stop_on_wrong_side():
    plan = plan_entries(cfg(), direction="BUY", entry_price=2400.0, stop_loss=2403.0, spec=GOLD)
    assert not plan.ok
    assert "不利方向" in plan.reject


def test_reject_when_risk_amount_zero():
    plan = plan_entries(cfg(risk_amount=0), direction="BUY", entry_price=2400.0,
                        stop_loss=2397.0, spec=GOLD)
    assert not plan.ok
    assert "风险金额" in plan.reject


def test_reject_when_spec_incomplete():
    plan = plan_entries(cfg(), direction="BUY", entry_price=2400.0, stop_loss=2397.0,
                        spec=SymbolSpec())
    assert not plan.ok
    assert "品种规格" in plan.reject


def test_reject_when_lot_below_minimum():
    # 风险 1 美元、止损 300 点：算得 0.003 手，低于最小 0.01
    plan = plan_entries(cfg(risk_amount=1), direction="BUY", entry_price=2400.0,
                        stop_loss=2397.0, spec=GOLD)
    assert not plan.ok
    assert "最小手数" in plan.reject


def test_reject_when_action_mismatches_signal():
    plan = plan_entries(cfg(action="sell"), direction="BUY", entry_price=2400.0,
                        stop_loss=2397.0, spec=GOLD)
    assert not plan.ok
    assert "监控方向" in plan.reject


# ---------------------------------------------------------------------------
# 分批切分
# ---------------------------------------------------------------------------

def test_batches_sum_to_total_lot():
    plan = plan_entries(cfg(), direction="BUY", entry_price=2400.0, stop_loss=2397.0, spec=GOLD)
    assert len(plan.batches) == 3           # 底仓 + 2 批
    assert sum(b.volume for b in plan.batches) == pytest.approx(plan.total_lot)


def test_base_volume_follows_base_ratio():
    plan = plan_entries(cfg(base_ratio=30), direction="BUY", entry_price=2400.0,
                        stop_loss=2397.0, spec=GOLD)
    assert plan.base_volume == pytest.approx(0.3)   # 1.0 手的 30%


def test_no_batching_puts_everything_in_base():
    plan = plan_entries(cfg(add_batches=0), direction="BUY", entry_price=2400.0,
                        stop_loss=2397.0, spec=GOLD)
    assert len(plan.batches) == 1
    assert plan.base_volume == pytest.approx(plan.total_lot)


def test_batch_count_shrinks_when_rest_cannot_fill_every_batch():
    # 总手数 0.03、底仓 0.01，剩余 0.02 只够切 2 批而不是 5 批
    plan = plan_entries(cfg(risk_amount=9, base_ratio=30, add_batches=5),
                        direction="BUY", entry_price=2400.0, stop_loss=2397.0, spec=GOLD)
    assert plan.total_lot == pytest.approx(0.03)
    assert plan.add_batch_count == 2
    assert sum(b.volume for b in plan.batches) == pytest.approx(0.03)


def test_pullback_triggers_go_against_position_and_stay_inside_stop():
    plan = plan_entries(cfg(), direction="BUY", entry_price=2400.0, stop_loss=2397.0, spec=GOLD)
    triggers = [b.trigger_price for b in plan.batches[1:]]
    assert triggers == [2399.0, 2398.0]          # 每批回撤 100 点
    assert all(t > plan.stop_loss for t in triggers)


def test_pullback_gap_is_capped_inside_the_stop():
    # 配置 500 点间距、止损只有 300 点：压缩到 300/(2+1)=100 点
    plan = plan_entries(cfg(batch_gap_points=500), direction="BUY", entry_price=2400.0,
                        stop_loss=2397.0, spec=GOLD)
    assert plan.gap_capped
    assert plan.gap_points == pytest.approx(100.0)
    assert all(b.trigger_price > plan.stop_loss for b in plan.batches[1:])


def test_breakout_triggers_go_with_position_and_stay_inside_target():
    plan = plan_entries(cfg(entry_direction=ENTRY_BREAKOUT), direction="BUY",
                        entry_price=2400.0, stop_loss=2397.0, spec=GOLD)
    triggers = [b.trigger_price for b in plan.batches[1:]]
    assert triggers == [2401.0, 2402.0]
    assert all(t < plan.take_profit for t in triggers)


def test_sell_pullback_triggers_go_up():
    plan = plan_entries(cfg(), direction="SELL", entry_price=2400.0, stop_loss=2403.0, spec=GOLD)
    assert [b.trigger_price for b in plan.batches[1:]] == [2401.0, 2402.0]


# ---------------------------------------------------------------------------
# 止盈与成交价重挂
# ---------------------------------------------------------------------------

def test_take_profit_follows_risk_reward_ratio():
    plan = plan_entries(cfg(rr_ratio=2.5), direction="BUY", entry_price=2400.0,
                        stop_loss=2397.0, spec=GOLD)
    assert plan.take_profit == pytest.approx(2407.5)   # 2400 + 3 × 2.5


def test_zero_ratio_means_no_take_profit():
    plan = plan_entries(cfg(rr_ratio=0), direction="BUY", entry_price=2400.0,
                        stop_loss=2397.0, spec=GOLD)
    assert plan.take_profit == 0.0


def test_anchor_to_fill_shifts_prices_but_keeps_lots():
    c = cfg()
    plan = plan_entries(c, direction="BUY", entry_price=2400.0, stop_loss=2397.0, spec=GOLD)
    lots = [b.volume for b in plan.batches]
    anchor_to_fill(plan, c, 2400.5, GOLD)
    assert [b.volume for b in plan.batches] == lots
    assert plan.entry_price == pytest.approx(2400.5)
    assert plan.sl_distance == pytest.approx(3.5)
    assert plan.take_profit == pytest.approx(2409.25)   # 2400.5 + 3.5 × 2.5
    assert [b.trigger_price for b in plan.batches[1:]] == [2399.5, 2398.5]


def test_anchor_to_fill_is_noop_without_fill_price():
    c = cfg()
    plan = plan_entries(c, direction="BUY", entry_price=2400.0, stop_loss=2397.0, spec=GOLD)
    anchor_to_fill(plan, c, 0.0, GOLD)
    assert plan.entry_price == pytest.approx(2400.0)


# ---------------------------------------------------------------------------
# 补仓判定
# ---------------------------------------------------------------------------

def test_next_batch_waits_until_trigger_price():
    c = cfg()
    plan = plan_entries(c, direction="BUY", entry_price=2400.0, stop_loss=2397.0, spec=GOLD)
    assert next_batch(plan, c, filled=1, price=2399.5) is None
    batch = next_batch(plan, c, filled=1, price=2399.0)
    assert batch is not None and batch.index == 1


def test_next_batch_advances_with_filled_count():
    c = cfg()
    plan = plan_entries(c, direction="BUY", entry_price=2400.0, stop_loss=2397.0, spec=GOLD)
    assert next_batch(plan, c, filled=2, price=2399.0) is None   # 第 2 批要到 2398
    assert next_batch(plan, c, filled=2, price=2398.0).index == 2


def test_next_batch_returns_none_when_all_filled():
    c = cfg()
    plan = plan_entries(c, direction="BUY", entry_price=2400.0, stop_loss=2397.0, spec=GOLD)
    assert next_batch(plan, c, filled=3, price=2390.0) is None


def test_next_batch_needs_base_filled_first():
    c = cfg()
    plan = plan_entries(c, direction="BUY", entry_price=2400.0, stop_loss=2397.0, spec=GOLD)
    assert next_batch(plan, c, filled=0, price=2398.0) is None


def test_breakout_next_batch_triggers_on_the_way_up():
    c = cfg(entry_direction=ENTRY_BREAKOUT)
    plan = plan_entries(c, direction="BUY", entry_price=2400.0, stop_loss=2397.0, spec=GOLD)
    assert next_batch(plan, c, filled=1, price=2400.5) is None
    assert next_batch(plan, c, filled=1, price=2401.0).index == 1


def test_sell_next_batch_triggers_on_the_way_up():
    c = cfg()
    plan = plan_entries(c, direction="SELL", entry_price=2400.0, stop_loss=2403.0, spec=GOLD)
    assert next_batch(plan, c, filled=1, price=2400.5) is None
    assert next_batch(plan, c, filled=1, price=2401.0).index == 1


# ---------------------------------------------------------------------------
# 保本触发
# ---------------------------------------------------------------------------

def positions(*pairs) -> list[dict]:
    return [
        {"ticket": 100 + i, "volume": v, "price_open": p, "sl": 0.0, "tp": 0.0}
        for i, (v, p) in enumerate(pairs)
    ]


def test_weighted_avg_price_uses_volume_weights():
    assert weighted_avg_price(positions((0.3, 2400.0), (0.7, 2399.0))) == pytest.approx(2399.3)
    assert weighted_avg_price([]) == 0.0
    assert weighted_avg_price([{"volume": 0, "price_open": 0}]) == 0.0


def test_breakeven_requires_profit_beyond_stop_distance_times():
    c = cfg(breakeven_enabled=True, breakeven_times=1.0)
    plan = plan_entries(c, direction="BUY", entry_price=2400.0, stop_loss=2397.0, spec=GOLD)
    pos = positions((0.3, 2400.0))
    assert breakeven_move(c, plan, positions=pos, price=2402.9, digits=2) is None
    move = breakeven_move(c, plan, positions=pos, price=2403.0, digits=2)
    assert move is not None
    assert move.stop_loss == pytest.approx(2400.0)


def test_breakeven_moves_stop_to_weighted_average():
    c = cfg(breakeven_enabled=True, breakeven_times=1.0)
    plan = plan_entries(c, direction="BUY", entry_price=2400.0, stop_loss=2397.0, spec=GOLD)
    pos = positions((0.3, 2400.0), (0.7, 2399.0))
    move = breakeven_move(c, plan, positions=pos, price=2403.0, digits=2)
    assert move is not None
    assert move.stop_loss == pytest.approx(2399.3)


def test_breakeven_disabled_returns_none():
    c = cfg(breakeven_enabled=False)
    plan = plan_entries(c, direction="BUY", entry_price=2400.0, stop_loss=2397.0, spec=GOLD)
    assert breakeven_move(c, plan, positions=positions((0.3, 2400.0)),
                          price=2450.0, digits=2) is None


def test_breakeven_times_scales_the_threshold():
    c = cfg(breakeven_enabled=True, breakeven_times=2.0)
    plan = plan_entries(c, direction="BUY", entry_price=2400.0, stop_loss=2397.0, spec=GOLD)
    pos = positions((0.3, 2400.0))
    assert breakeven_move(c, plan, positions=pos, price=2405.0, digits=2) is None
    assert breakeven_move(c, plan, positions=pos, price=2406.0, digits=2) is not None


def test_sell_breakeven_triggers_on_the_way_down():
    c = cfg(breakeven_enabled=True, breakeven_times=1.0)
    plan = plan_entries(c, direction="SELL", entry_price=2400.0, stop_loss=2403.0, spec=GOLD)
    pos = positions((0.3, 2400.0))
    assert breakeven_move(c, plan, positions=pos, price=2398.0, digits=2) is None
    assert breakeven_move(c, plan, positions=pos, price=2397.0, digits=2) is not None


# ---------------------------------------------------------------------------
# 说明生成
# ---------------------------------------------------------------------------

def test_describe_plan_states_the_lot_formula():
    c = cfg()
    plan = plan_entries(c, direction="BUY", entry_price=2400.0, stop_loss=2397.0, spec=GOLD)
    text = describe_plan(plan, c, GOLD)
    assert "以损定量" in text
    assert "300" in text and "总手数 1" in text
    assert "底仓 30%" in text


def test_plan_detail_exposes_every_input():
    c = cfg()
    plan = plan_entries(c, direction="BUY", entry_price=2400.0, stop_loss=2397.0, spec=GOLD)
    d = plan_detail(plan, c, GOLD)
    assert d["kind"] == "risk_sized_plan"
    assert d["total_lot"] == pytest.approx(1.0)
    assert d["risk_amount"] == pytest.approx(300.0)
    assert d["lot_formula"] == "300 ÷ 300 = 1"
    assert len(d["batches"]) == 3
    assert d["batches"][0]["trigger_price"] is None      # 底仓市价


def test_batch_description_and_detail():
    c = cfg()
    plan = plan_entries(c, direction="BUY", entry_price=2400.0, stop_loss=2397.0, spec=GOLD)
    batch = plan.batches[1]
    text = describe_batch(plan, c, batch, GOLD, 2398.9)
    assert "补仓" in text and "回撤补仓" in text
    d = batch_detail(plan, c, batch, GOLD, 2398.9)
    assert d["kind"] == "risk_sized_add"
    assert d["batch_index"] == 1
    assert d["stop_loss"] == pytest.approx(2397.0)


def test_breakeven_description_and_detail():
    c = cfg(breakeven_enabled=True)
    plan = plan_entries(c, direction="BUY", entry_price=2400.0, stop_loss=2397.0, spec=GOLD)
    move = breakeven_move(c, plan, positions=positions((0.3, 2400.0)), price=2403.0, digits=2)
    assert move is not None
    assert "保本触发" in move.describe(2)
    assert move.detail()["kind"] == "breakeven"


def test_batch_comment_fits_mt5_limit():
    c = cfg()
    plan = plan_entries(c, direction="BUY", entry_price=2400.0, stop_loss=2397.0, spec=GOLD)
    for batch in plan.batches:
        assert len(batch_comment(batch)) <= 31
    assert batch_comment(plan.batches[1]) == "R3B1"


# ---------------------------------------------------------------------------
# 手数步长
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("step,expected", [(0.01, 2), (0.1, 1), (1.0, 0), (0.001, 3)])
def test_volume_digits_follow_step(step, expected):
    assert SymbolSpec(volume_step=step).volume_digits == expected


def test_floor_lot_never_rounds_up():
    assert GOLD.floor_lot(0.199) == pytest.approx(0.19)
    assert GOLD.floor_lot(0.2) == pytest.approx(0.2)
    assert GOLD.floor_lot(-1) == pytest.approx(0.0)
