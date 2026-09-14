"""以损定量趋势单（策略模版2）的纯逻辑单测。"""
import pytest

from risk_sizing import (
    RULE_TYPE_RISK_SIZED,
    Batch,
    RiskSizedConfig,
    SymbolSpec,
    align_stop_to_tick,
    anchor_to_fill,
    batch_comment,
    batch_detail,
    breakeven_move,
    describe_batch,
    describe_plan,
    filled_orders_from_positions,
    next_batch,
    parse_batch_comment,
    pending_batches,
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

# BTC 类规格：合约规模 1，一手一美元价差值 1 美元（用于复现 MTcommander 实盘样本）
BTC = SymbolSpec(
    point=0.01, digits=2, tick_size=0.01, tick_value=0.01,
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
        "max_total_lot": 0.0,
        "breakeven_enabled": False,
        "breakeven_times": 1.0,
        "breakeven_mode": "once",
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


def test_config_forces_full_base_when_not_distributing():
    c = cfg(add_batches=0, base_ratio=30)
    assert c.base_ratio == 100.0


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
    assert wide.risk_used <= 300.0
    assert tight.risk_used <= 300.0


def test_lot_is_floored_to_volume_step_and_never_exceeds_risk():
    plan = plan_entries(cfg(), direction="BUY", entry_price=2400.0, stop_loss=2396.67, spec=GOLD)
    assert plan.planned_lot == pytest.approx(0.9)
    assert plan.total_lot <= plan.planned_lot
    assert plan.risk_used <= 300.0


def test_max_total_lot_caps_the_result():
    plan = plan_entries(cfg(max_total_lot=0.5), direction="BUY", entry_price=2400.0,
                        stop_loss=2397.0, spec=GOLD)
    assert plan.planned_lot == pytest.approx(0.5)


def test_volume_max_caps_the_result():
    spec = SymbolSpec(point=0.01, digits=2, tick_size=0.01, tick_value=1.0,
                      volume_min=0.01, volume_step=0.01, volume_max=0.3)
    plan = plan_entries(cfg(), direction="BUY", entry_price=2400.0, stop_loss=2397.0, spec=spec)
    assert plan.planned_lot == pytest.approx(0.3)


def test_sell_direction_uses_stop_above_entry():
    plan = plan_entries(cfg(), direction="SELL", entry_price=2400.0, stop_loss=2403.0, spec=GOLD)
    assert plan.ok
    assert plan.total_lot == pytest.approx(1.0)
    assert plan.take_profit == pytest.approx(2392.5)  # 2400 - 3 × 2.5


# ---------------------------------------------------------------------------
# 限价开仓：全部挂在信号入场价，止损距离与市价同一口径
# ---------------------------------------------------------------------------

def limit_cfg(**over) -> RiskSizedConfig:
    return cfg(entry_mode="limit", **over)


def limit_plan(**over):
    """底仓挂 2400、止损 2397 的多单限价计划（止损距离 3 美元 = 300 点）。"""
    params = {"direction": "BUY", "entry_price": 2410.0, "stop_loss": 2397.0,
              "spec": GOLD, "limit_price": 2400.0}
    conf = over.pop("cfg", None) or limit_cfg(**over)
    return plan_entries(conf, **params)


def test_limit_entry_anchors_on_signal_price_not_market():
    """挂单按挂单价成交，开仓价与止损基准都用信号入场价，不看现价。"""
    plan = limit_plan()
    assert plan.ok
    assert plan.is_limit_entry
    assert plan.entry_price == pytest.approx(2400.0)
    assert plan.risk_price == pytest.approx(2400.0)
    assert plan.spread == pytest.approx(0.0)  # 挂单没有点差概念
    assert plan.sl_distance == pytest.approx(3.0)


def test_limit_orders_stack_at_entry_price():
    """底仓与分散仓全部挂在信号入场价，不朝止损方向铺开。"""
    plan = limit_plan(add_batches=2)
    prices = [b.price for b in plan.batches]
    assert prices == pytest.approx([2400.0, 2400.0, 2400.0])
    assert plan.ladder_step == pytest.approx(0.0)


def test_limit_sell_also_stacks_at_entry_price():
    plan = plan_entries(limit_cfg(add_batches=2), direction="SELL", entry_price=2390.0,
                        stop_loss=2403.0, spec=GOLD, limit_price=2400.0)
    assert [b.price for b in plan.batches] == pytest.approx([2400.0, 2400.0, 2400.0])
    assert all(b.sl_distance == pytest.approx(3.0) for b in plan.batches)


def test_limit_batches_share_the_same_stop_distance():
    """同价入场后各档离止损一样远，手数因此与市价同一口径。"""
    plan = limit_plan(add_batches=2)
    assert all(b.sl_distance == pytest.approx(3.0) for b in plan.batches)


def test_limit_uses_single_stop_distance_for_sizing():
    """每手亏损按入场价到止损价的单值距离算：3 美元 = 300 点，一手亏 300。"""
    plan = limit_plan(add_batches=2, base_ratio=30)
    assert plan.loss_per_lot == pytest.approx(300.0)
    assert plan.planned_lot == pytest.approx(1.0)  # floor(300 / 300) 到 0.01


def test_limit_lot_matches_market_at_same_stop_distance():
    """入场同价后，限价与市价的止损距离、手数、风险一致。"""
    market = plan_entries(cfg(add_batches=2), direction="BUY", entry_price=2400.0,
                          stop_loss=2397.0, spec=GOLD)
    limit = limit_plan(add_batches=2)
    assert limit.sl_distance == pytest.approx(market.sl_distance)
    assert limit.loss_per_lot == pytest.approx(market.loss_per_lot)
    assert limit.total_lot == pytest.approx(market.total_lot)
    assert limit.risk_used == pytest.approx(market.risk_used)


def test_limit_worst_case_loss_stays_within_risk_amount():
    """全部档位都成交后打到止损，总亏损不超过风险金额——以损定量的立身之本。"""
    plan = limit_plan(add_batches=2)
    worst = sum(b.volume * b.sl_distance for b in plan.batches) / GOLD.tick_size * GOLD.tick_value
    assert worst == pytest.approx(plan.risk_used)
    assert worst <= 300.0 + 1e-9


@pytest.mark.parametrize("batches", [0, 1, 3, 7, 20])
@pytest.mark.parametrize("base_ratio", [10, 30, 50, 90])
def test_limit_never_exceeds_risk_budget(batches, base_ratio):
    """各种仓位分布下都不能超预算：最小手数托底会让实际分布偏离配置比例。"""
    plan = limit_plan(add_batches=batches, base_ratio=base_ratio)
    if not plan.ok:
        return  # 切不出满足预算的分布时会拒绝，这也是正确行为
    worst = sum(b.volume * b.sl_distance for b in plan.batches) / GOLD.tick_size * GOLD.tick_value
    assert worst <= 300.0 + 1e-6


def test_limit_partial_fill_loses_less_than_budget():
    """只成交部分档位时手数更少，亏损小于全仓风险金额。"""
    plan = limit_plan(add_batches=2)
    base_only = plan.batches[0].volume * plan.batches[0].sl_distance / GOLD.tick_size
    assert base_only * GOLD.tick_value < plan.risk_used


def test_limit_requires_entry_price():
    plan = plan_entries(limit_cfg(), direction="BUY", entry_price=2410.0,
                        stop_loss=2397.0, spec=GOLD)
    assert not plan.ok
    assert "入场价" in plan.reject


def test_limit_rejects_entry_on_wrong_side_of_stop():
    """挂在止损之外的单一成交就已越过止损，等于开仓即止损。"""
    plan = plan_entries(limit_cfg(), direction="BUY", entry_price=2410.0,
                        stop_loss=2405.0, spec=GOLD, limit_price=2400.0)
    assert not plan.ok
    assert "不利方向" in plan.reject


def test_limit_take_profit_still_anchors_on_base_price():
    """阶梯止盈仍以入场价为锚：同价入场后与市价模式的止盈价位一致。"""
    plan = limit_plan(add_batches=2)
    assert plan.take_profit == pytest.approx(2400.0 + 3.0 * 2.5)
    assert plan.batches[0].take_profit == 0.0
    assert [b.take_profit for b in plan.batches[1:]] == pytest.approx([2403.75, 2407.5])


def test_limit_batches_marked_as_pending():
    plan = limit_plan(add_batches=2)
    assert all(b.is_pending for b in plan.batches)


def test_market_batches_have_no_limit_price():
    """市价模式一个挂单价都不该有，否则会误走挂单下单路径。"""
    plan = plan_entries(cfg(add_batches=2), direction="BUY", entry_price=2400.0,
                        stop_loss=2397.0, spec=GOLD)
    assert all(not b.is_pending for b in plan.batches)
    assert plan.ladder_step == 0.0


def test_anchor_to_fill_is_noop_for_limit():
    """限价成交价就是挂单价，拿终端回报再算一遍只会引入取整噪声。"""
    plan = limit_plan(add_batches=2)
    before = [b.take_profit for b in plan.batches]
    anchor_to_fill(plan, limit_cfg(add_batches=2), 2399.87, GOLD)
    assert plan.entry_price == pytest.approx(2400.0)
    assert [b.take_profit for b in plan.batches] == pytest.approx(before)


def test_market_mode_sizing_unchanged_by_limit_support():
    """限价改造不得动市价口径：单值止损距离下手数与风险与老行为一致。"""
    plan = plan_entries(cfg(add_batches=2), direction="BUY", entry_price=2400.0,
                        stop_loss=2397.0, spec=GOLD)
    assert plan.loss_per_lot == pytest.approx(300.0)
    assert plan.total_lot == pytest.approx(1.0)
    assert plan.risk_used == pytest.approx(300.0)
    assert all(b.sl_distance == pytest.approx(3.0) for b in plan.batches)


# ---------------------------------------------------------------------------
# 止损距离的基准价：止损触发侧（BUY=bid / SELL=ask）
# ---------------------------------------------------------------------------

def test_risk_price_measures_stop_distance_from_the_trigger_side():
    """多单在 ask 成交、止损由 bid 触发：距离按 bid 算，阶梯锚在 ask。"""
    plan = plan_entries(cfg(), direction="BUY", entry_price=2400.06,
                        stop_loss=2397.0, spec=GOLD, risk_price=2399.94)
    assert plan.spread == pytest.approx(0.12)
    assert plan.sl_distance == pytest.approx(2.94)
    assert plan.take_profit == pytest.approx(2400.06 + 2.94 * 2.5)


def test_sell_risk_price_sits_above_entry():
    plan = plan_entries(cfg(), direction="SELL", entry_price=2399.94,
                        stop_loss=2403.0, spec=GOLD, risk_price=2400.06)
    assert plan.sl_distance == pytest.approx(2.94)
    assert plan.take_profit == pytest.approx(2399.94 - 2.94 * 2.5)


def test_risk_price_falls_back_to_entry_price():
    plan = plan_entries(cfg(), direction="BUY", entry_price=2400.0, stop_loss=2397.0, spec=GOLD)
    assert plan.risk_price == pytest.approx(2400.0)
    assert plan.spread == 0.0


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
# 底仓 + 分散仓切分
# ---------------------------------------------------------------------------

def test_batches_sum_to_total_lot():
    plan = plan_entries(cfg(), direction="BUY", entry_price=2400.0, stop_loss=2397.0, spec=GOLD)
    assert len(plan.batches) == 3           # 底仓 + 2 分散
    assert sum(b.volume for b in plan.batches) == pytest.approx(plan.total_lot)
    assert plan.total_lot <= plan.planned_lot


def test_distribute_lots_are_equal_and_remainder_is_dropped():
    """分散仓严格等手数；除不尽的余量宁可不开，也不并到末笔上。"""
    plan = plan_entries(cfg(max_total_lot=0.5), direction="BUY", entry_price=2400.0,
                        stop_loss=2397.0, spec=GOLD)
    assert plan.planned_lot == pytest.approx(0.5)
    assert plan.base_volume == pytest.approx(0.15)      # ⌊0.5 × 30%⌋
    assert [b.volume for b in plan.batches[1:]] == [0.17, 0.17]
    assert plan.total_lot == pytest.approx(0.49)
    assert plan.dropped_lot == pytest.approx(0.01)


def test_base_volume_follows_base_ratio():
    plan = plan_entries(cfg(base_ratio=30), direction="BUY", entry_price=2400.0,
                        stop_loss=2397.0, spec=GOLD)
    assert plan.base_volume == pytest.approx(0.3)   # 1.0 手的 30%


def test_no_distribute_puts_everything_in_base():
    plan = plan_entries(cfg(add_batches=0), direction="BUY", entry_price=2400.0,
                        stop_loss=2397.0, spec=GOLD)
    assert len(plan.batches) == 1
    assert plan.base_volume == pytest.approx(plan.total_lot)


def test_distribute_count_shrinks_when_rest_cannot_fill_every_order():
    # 总手数 0.03、底仓 0.01，剩余 0.02 只够切 2 单而不是 5 单
    plan = plan_entries(cfg(risk_amount=9, base_ratio=30, add_batches=5),
                        direction="BUY", entry_price=2400.0, stop_loss=2397.0, spec=GOLD)
    assert plan.total_lot == pytest.approx(0.03)
    assert plan.add_batch_count == 2
    assert sum(b.volume for b in plan.batches) == pytest.approx(0.03)


def test_base_has_no_take_profit_distribute_climbs_the_ladder():
    """对齐截图：底仓 TP=0，分散仓按等分阶梯逐档挂止盈。"""
    plan = plan_entries(cfg(), direction="BUY", entry_price=2400.0, stop_loss=2397.0, spec=GOLD)
    assert plan.batches[0].take_profit == 0.0
    assert plan.batches[0].is_base
    assert [b.take_profit for b in plan.batches[1:]] == pytest.approx([2403.75, 2407.5])
    assert plan.tp_step == pytest.approx(3.75)


def test_ten_distribute_orders_match_screenshot_shape():
    plan = plan_entries(cfg(add_batches=10), direction="BUY", entry_price=2400.0,
                        stop_loss=2397.0, spec=GOLD)
    assert plan.order_count == 11
    assert plan.add_batch_count == 10
    assert plan.base_volume == pytest.approx(0.3)
    assert plan.distribute_volume == pytest.approx(0.7)


# ---------------------------------------------------------------------------
# 止盈与成交价重挂
# ---------------------------------------------------------------------------

def test_full_risk_reward_ratio_lands_on_the_last_rung():
    plan = plan_entries(cfg(rr_ratio=2.5), direction="BUY", entry_price=2400.0,
                        stop_loss=2397.0, spec=GOLD)
    assert plan.take_profit == pytest.approx(2407.5)             # 2400 + 3 × 2.5
    assert plan.batches[-1].take_profit == pytest.approx(2407.5)


def test_ladder_is_evenly_spaced_and_strictly_increasing():
    plan = plan_entries(cfg(risk_amount=3000, add_batches=5), direction="BUY",
                        entry_price=2400.0, stop_loss=2397.0, spec=GOLD)
    tps = [b.take_profit for b in plan.batches[1:]]
    assert len(tps) == 5
    assert tps == pytest.approx([2401.5, 2403.0, 2404.5, 2406.0, 2407.5])
    assert plan.tp_step == pytest.approx(1.5)                    # 3 × 2.5 ÷ 5


def test_ladder_divides_by_the_actual_distribute_count():
    """单数被最小手数削减时，最远一档仍要吃满配置的盈亏比。"""
    plan = plan_entries(cfg(risk_amount=9, base_ratio=30, add_batches=5),
                        direction="BUY", entry_price=2400.0, stop_loss=2397.0, spec=GOLD)
    assert plan.add_batch_count == 2
    assert [b.take_profit for b in plan.batches[1:]] == pytest.approx([2403.75, 2407.5])


def test_sell_ladder_steps_downwards():
    plan = plan_entries(cfg(), direction="SELL", entry_price=2400.0,
                        stop_loss=2403.0, spec=GOLD)
    assert [b.take_profit for b in plan.batches[1:]] == pytest.approx([2396.25, 2392.5])


def test_zero_ratio_means_no_take_profit():
    plan = plan_entries(cfg(rr_ratio=0), direction="BUY", entry_price=2400.0,
                        stop_loss=2397.0, spec=GOLD)
    assert plan.take_profit == 0.0
    assert plan.tp_step == 0.0
    assert all(b.take_profit == 0.0 for b in plan.batches)


def test_anchor_to_fill_shifts_the_ladder_but_keeps_lots_and_base_tp_zero():
    c = cfg()
    plan = plan_entries(c, direction="BUY", entry_price=2400.0, stop_loss=2397.0, spec=GOLD)
    lots = [b.volume for b in plan.batches]
    anchor_to_fill(plan, c, 2400.5, GOLD)
    assert [b.volume for b in plan.batches] == lots
    assert plan.entry_price == pytest.approx(2400.5)
    assert plan.sl_distance == pytest.approx(3.5)
    assert plan.take_profit == pytest.approx(2409.25)   # 2400.5 + 3.5 × 2.5
    assert plan.batches[0].take_profit == 0.0
    assert plan.batches[1].take_profit == pytest.approx(2404.875, abs=0.01)
    assert plan.batches[2].take_profit == pytest.approx(2409.25)


def test_anchor_to_fill_carries_the_spread_along():
    """重锚时点差随开仓价平移，止损距离仍按触发侧算。"""
    c = cfg()
    plan = plan_entries(c, direction="BUY", entry_price=2400.06, stop_loss=2397.0,
                        spec=GOLD, risk_price=2399.94)
    anchor_to_fill(plan, c, 2400.5, GOLD)
    assert plan.risk_price == pytest.approx(2400.38)    # 2400.5 - 0.12
    assert plan.sl_distance == pytest.approx(3.38)
    assert plan.take_profit == pytest.approx(2400.5 + 3.38 * 2.5)


def test_anchor_to_fill_is_noop_without_fill_price():
    c = cfg()
    plan = plan_entries(c, direction="BUY", entry_price=2400.0, stop_loss=2397.0, spec=GOLD)
    anchor_to_fill(plan, c, 0.0, GOLD)
    assert plan.entry_price == pytest.approx(2400.0)


# ---------------------------------------------------------------------------
# MTcommander 实盘样本复现（BTCUSD，风险 100 / 盈亏比 2.5 / 底仓 30% / 分散 10 单）
# ---------------------------------------------------------------------------

def test_reproduces_mtcommander_sample_one():
    """截图样本：bid 64427.40 / ask 64439.40，止损 64270.60 -> 0.18 + 0.04 × 10。"""
    c = cfg(risk_amount=100, rr_ratio=2.5, base_ratio=30, add_batches=10)
    plan = plan_entries(c, direction="BUY", entry_price=64439.40, stop_loss=64270.60,
                        spec=BTC, risk_price=64427.40)
    assert plan.sl_distance == pytest.approx(156.80)
    assert plan.planned_lot == pytest.approx(0.63)
    assert plan.base_volume == pytest.approx(0.18)
    assert [b.volume for b in plan.batches[1:]] == [0.04] * 10
    assert plan.total_lot == pytest.approx(0.58)
    assert plan.dropped_lot == pytest.approx(0.05)
    assert plan.tp_step == pytest.approx(39.2)
    assert [b.take_profit for b in plan.batches[1:]] == pytest.approx([
        64478.60, 64517.80, 64557.00, 64596.20, 64635.40,
        64674.60, 64713.80, 64753.00, 64792.20, 64831.40,
    ])


def test_reproduces_mtcommander_sample_two():
    """截图样本：bid 64274.20 / ask 64286.20，止损 63986.99 -> 0.1 + 0.02 × 10。"""
    c = cfg(risk_amount=100, rr_ratio=2.5, base_ratio=30, add_batches=10)
    plan = plan_entries(c, direction="BUY", entry_price=64286.20, stop_loss=63986.99,
                        spec=BTC, risk_price=64274.20)
    assert plan.planned_lot == pytest.approx(0.34)
    assert plan.base_volume == pytest.approx(0.10)
    assert [b.volume for b in plan.batches[1:]] == [0.02] * 10
    assert plan.total_lot == pytest.approx(0.30)
    # 这组报价是从截图的止盈阶梯反解出来的（本身已被券商取整到分），
    # 加上 MT5 四舍五入、Python 银行家舍入，个别档位允许差两分
    assert [b.take_profit for b in plan.batches[1:]] == pytest.approx([
        64358.00, 64429.81, 64501.61, 64573.41, 64645.21,
        64717.02, 64788.82, 64860.62, 64932.42, 65004.23,
    ], abs=0.02)


# ---------------------------------------------------------------------------
# 分散仓待开判定（全部市价，不看触发价）
# ---------------------------------------------------------------------------

def test_next_batch_returns_first_distribute_immediately():
    c = cfg()
    plan = plan_entries(c, direction="BUY", entry_price=2400.0, stop_loss=2397.0, spec=GOLD)
    batch = next_batch(plan, c, filled=1, price=9999.0)
    assert batch is not None and batch.index == 1


def test_pending_batches_lists_all_unopened_distribute():
    c = cfg()
    plan = plan_entries(c, direction="BUY", entry_price=2400.0, stop_loss=2397.0, spec=GOLD)
    assert [b.index for b in pending_batches(plan, filled=1)] == [1, 2]
    assert pending_batches(plan, filled=3) == []


def test_next_batch_returns_none_when_all_filled():
    c = cfg()
    plan = plan_entries(c, direction="BUY", entry_price=2400.0, stop_loss=2397.0, spec=GOLD)
    assert next_batch(plan, c, filled=3, price=2390.0) is None


def test_next_batch_needs_base_filled_first():
    c = cfg()
    plan = plan_entries(c, direction="BUY", entry_price=2400.0, stop_loss=2397.0, spec=GOLD)
    assert next_batch(plan, c, filled=0, price=2398.0) is None


def test_batch_comment_roundtrip():
    assert batch_comment(Batch(index=1, volume=0.1)) == "R3B1"
    assert batch_comment(Batch(index=12, volume=0.1)) == "R3B12"
    assert parse_batch_comment("R3B12") == 12
    assert parse_batch_comment("S1") is None
    assert parse_batch_comment("R3B") is None


def test_filled_orders_from_positions_uses_max_batch_not_count():
    """中间档止盈离场后，仍按最大 R3B 序号推断已开完，避免重开。"""
    positions = [
        {"comment": "S1", "volume": 0.3},
        {"comment": "R3B2", "volume": 0.35},
    ]
    assert filled_orders_from_positions(positions) == 3
    assert filled_orders_from_positions([{"comment": "S1"}]) == 1
    assert filled_orders_from_positions([
        {"comment": "S1"}, {"comment": "R3B1"},
    ]) == 2
    assert filled_orders_from_positions([]) == 0


# ---------------------------------------------------------------------------
# 保本触发
# ---------------------------------------------------------------------------

def positions(*pairs, sl: float = 2397.0) -> list[dict]:
    return [
        {"ticket": 100 + i, "volume": v, "price_open": p, "sl": sl, "tp": 0.0}
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
    move = breakeven_move(c, plan, positions=pos, price=2403.0, digits=2, tick_size=0.01)
    assert move is not None
    assert move.stop_loss == pytest.approx(2399.3)


def test_align_stop_to_tick_rounds_away_from_market():
    assert align_stop_to_tick(77831.24, tick_size=0.1, digits=2, direction="BUY") == pytest.approx(77831.2)
    assert align_stop_to_tick(77831.24, tick_size=0.1, digits=2, direction="SELL") == pytest.approx(77831.3)
    assert align_stop_to_tick(2400.0, tick_size=0.01, digits=2, direction="BUY") == pytest.approx(2400.0)
    assert align_stop_to_tick(77831.24, tick_size=0.0, digits=2, direction="SELL") == pytest.approx(77831.24)


def test_breakeven_stop_snaps_to_tick():
    """加权均价落到半个 tick 时，空单向上、多单向下对齐。"""
    c = cfg(breakeven_enabled=True, breakeven_times=1.0)
    # 0.3*2400.07 + 0.7*2399.01 = 2399.328 → digits=2 为 2399.33
    pos = positions((0.3, 2400.07), (0.7, 2399.01), sl=2403.0)
    sell = plan_entries(c, direction="SELL", entry_price=2400.0, stop_loss=2403.0, spec=GOLD)
    sell_move = breakeven_move(
        c, sell, positions=pos, price=2396.0, digits=2, tick_size=0.1,
    )
    assert sell_move is not None
    assert sell_move.avg_price == pytest.approx(2399.33)
    assert sell_move.stop_loss == pytest.approx(2399.4)
    assert "按跳动取整" in sell_move.describe(2)

    buy = plan_entries(c, direction="BUY", entry_price=2400.0, stop_loss=2397.0, spec=GOLD)
    buy_pos = positions((0.3, 2400.07), (0.7, 2399.01), sl=2397.0)
    buy_move = breakeven_move(
        c, buy, positions=buy_pos, price=2403.0, digits=2, tick_size=0.1,
    )
    assert buy_move is not None
    assert buy_move.stop_loss == pytest.approx(2399.3)


def test_breakeven_times_scales_the_threshold():
    c = cfg(breakeven_enabled=True, breakeven_times=2.0)
    plan = plan_entries(c, direction="BUY", entry_price=2400.0, stop_loss=2397.0, spec=GOLD)
    pos = positions((0.3, 2400.0))
    assert breakeven_move(c, plan, positions=pos, price=2405.0, digits=2) is None
    assert breakeven_move(c, plan, positions=pos, price=2406.0, digits=2) is not None


def test_sell_breakeven_triggers_on_the_way_down():
    c = cfg(breakeven_enabled=True, breakeven_times=1.0)
    plan = plan_entries(c, direction="SELL", entry_price=2400.0, stop_loss=2403.0, spec=GOLD)
    pos = positions((0.3, 2400.0), sl=2403.0)
    assert breakeven_move(c, plan, positions=pos, price=2398.0, digits=2) is None
    assert breakeven_move(c, plan, positions=pos, price=2397.0, digits=2) is not None


def test_breakeven_skips_when_stop_already_at_average():
    c = cfg(breakeven_enabled=True, breakeven_times=1.0, breakeven_mode="loop")
    plan = plan_entries(c, direction="BUY", entry_price=2400.0, stop_loss=2397.0, spec=GOLD)
    pos = positions((0.3, 2400.0), sl=2400.0)  # 已保本
    assert breakeven_move(c, plan, positions=pos, price=2406.0, digits=2) is None


def test_breakeven_mode_defaults_to_once_and_accepts_loop():
    assert cfg().breakeven_mode == "once"
    assert cfg(breakeven_mode="loop").breakeven_is_loop
    assert cfg(breakeven_mode="weird").breakeven_mode == "once"


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
    assert "分散仓" in text and "TP=0" in text


def test_describe_limit_plan_stacks_at_entry_price():
    c = limit_cfg(add_batches=2)
    plan = limit_plan(cfg=c)
    text = describe_plan(plan, c, GOLD)
    assert "限价开仓" in text
    assert "限价 @2400" in text
    assert "阶梯限价" not in text
    assert "加权止损" not in text


def test_plan_detail_exposes_every_input():
    c = cfg()
    plan = plan_entries(c, direction="BUY", entry_price=2400.0, stop_loss=2397.0, spec=GOLD)
    d = plan_detail(plan, c, GOLD)
    assert d["kind"] == "risk_sized_plan"
    assert d["total_lot"] == pytest.approx(1.0)
    assert d["risk_amount"] == pytest.approx(300.0)
    assert d["lot_formula"] == "300 ÷ 300 = 1"
    assert d["order_count"] == 3
    assert d["batches"][0]["take_profit"] is None
    assert d["batches"][0]["role"] == "base"
    assert d["batches"][1]["role"] == "distribute"


def test_batch_description_and_detail():
    c = cfg()
    plan = plan_entries(c, direction="BUY", entry_price=2400.0, stop_loss=2397.0, spec=GOLD)
    batch = plan.batches[1]
    text = describe_batch(plan, c, batch, GOLD, 2400.1)
    assert "分散仓" in text
    d = batch_detail(plan, c, batch, GOLD, 2400.1)
    assert d["kind"] == "risk_sized_distribute"
    assert d["batch_index"] == 1
    assert d["stop_loss"] == pytest.approx(2397.0)


def test_breakeven_description_and_detail():
    c = cfg(breakeven_enabled=True)
    plan = plan_entries(c, direction="BUY", entry_price=2400.0, stop_loss=2397.0, spec=GOLD)
    move = breakeven_move(c, plan, positions=positions((0.3, 2400.0)), price=2403.0, digits=2)
    assert move is not None
    assert "保本触发" in move.describe(2)
    assert move.detail()["kind"] == "breakeven"
    assert "按跳动取整" not in move.describe(2)


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
