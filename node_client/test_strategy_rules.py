"""策略加仓判定的单元测试（纯逻辑，无需 MT5）。"""
from strategy_rules import (
    RULE_TYPE_COUNTER,
    RULE_TYPE_TREND,
    PositionCtx,
    action_matches,
    deviation_points,
    evaluate,
    evaluate_rule,
    pick_batch_level,
)

POINT = 0.01  # 黄金类品种


def counter_rule(**kw):
    rule = {
        "type": RULE_TYPE_COUNTER,
        "status": 1,
        "action": "all",
        "point": 100,
        "lot_times": 1.1,
        "extra_lot": 0.0,
        "max_allow_num": 3,
        "batch_enabled": False,
        "batch_action": "all",
        "batch_count": 0,
        "total_lot_limit": 0,
        "batch_levels": [],
    }
    rule.update(kw)
    return rule


def batched_counter_rule(**kw):
    overrides = {
        "batch_enabled": True,
        "batch_count": 3,
        "total_lot_limit": 10,
        "batch_levels": [
            {"pos_from": 2, "pos_to": 4, "calc_type": "point", "point": 100, "lot_times": 1.1, "extra_lot": 0},
            {"pos_from": 5, "pos_to": 7, "calc_type": "point", "point": 200, "lot_times": 1.2, "extra_lot": 0},
            {"pos_from": 8, "pos_to": 10, "calc_type": "point", "point": 300, "lot_times": 1.3, "extra_lot": 0},
        ],
    }
    overrides.update(kw)
    return counter_rule(**overrides)


def ctx(**kw):
    base = {
        "direction": "BUY",
        "position_count": 1,
        "base_volume": 0.1,
        "base_price": 2400.0,
        "price": 2400.0,
        "point": POINT,
        "add_count": 0,
    }
    base.update(kw)
    return PositionCtx(**base)


# --------------------------- 方向匹配 ---------------------------
def test_action_matches():
    assert action_matches("all", "BUY")
    assert action_matches("buy", "BUY")
    assert not action_matches("sell", "BUY")
    assert action_matches(None, "SELL")


# --------------------------- 偏离计算 ---------------------------
def test_counter_deviation_is_adverse_direction():
    # 多单：价格下跌才是逆势方向
    assert deviation_points(RULE_TYPE_COUNTER, "BUY", 2400.0, 2399.0, POINT) == 100
    assert deviation_points(RULE_TYPE_COUNTER, "BUY", 2400.0, 2401.0, POINT) == -100
    # 空单：价格上涨才是逆势方向
    assert deviation_points(RULE_TYPE_COUNTER, "SELL", 2400.0, 2401.0, POINT) == 100


def test_trend_deviation_is_favourable_direction():
    assert deviation_points(RULE_TYPE_TREND, "BUY", 2400.0, 2401.0, POINT) == 100
    assert deviation_points(RULE_TYPE_TREND, "SELL", 2400.0, 2399.0, POINT) == 100


# --------------------------- 基础加仓 ---------------------------
def test_no_add_before_threshold():
    assert evaluate_rule(counter_rule(), ctx(price=2399.5)) is None


def test_add_when_threshold_reached():
    d = evaluate_rule(counter_rule(), ctx(price=2399.0))
    assert d is not None
    assert d.action == "BUY"
    assert d.volume == 0.11          # 1.1 × 0.1
    assert d.event_type == "add_counter"


def test_extra_lot_added_on_top_of_multiplier():
    d = evaluate_rule(counter_rule(extra_lot=0.05), ctx(price=2399.0))
    assert d.volume == 0.16          # 1.1 × 0.1 + 0.05


def test_disabled_rule_never_triggers():
    assert evaluate_rule(counter_rule(status=0), ctx(price=2390.0)) is None


def test_max_allow_num_caps_adds():
    rule = counter_rule(max_allow_num=3)
    assert evaluate_rule(rule, ctx(price=2399.0, add_count=2)) is not None
    assert evaluate_rule(rule, ctx(price=2399.0, add_count=3)) is None


def test_rule_action_filters_direction():
    rule = counter_rule(action="sell")
    assert evaluate_rule(rule, ctx(price=2399.0, direction="BUY")) is None
    # 空单方向反过来才是逆势
    assert evaluate_rule(rule, ctx(price=2401.0, direction="SELL")) is not None


# --------------------------- 分批档位 ---------------------------
def test_pick_batch_level_by_next_position_no():
    rule = batched_counter_rule()
    assert pick_batch_level(rule, 2)[1] == 0
    assert pick_batch_level(rule, 5)[1] == 1
    assert pick_batch_level(rule, 10)[1] == 2
    assert pick_batch_level(rule, 11)[1] is None


def test_batch_level_params_apply_per_tier():
    rule = batched_counter_rule()
    # 持仓 1 笔 -> 下一笔是第 2 笔，命中档位 1（100 点 / 1.1 倍）
    d = evaluate_rule(rule, ctx(position_count=1, price=2399.0))
    assert d.volume == 0.11
    assert d.level_index == 0

    # 持仓 4 笔 -> 下一笔是第 5 笔，命中档位 2（200 点 / 1.2 倍）
    assert evaluate_rule(rule, ctx(position_count=4, price=2399.0)) is None  # 100 点不够
    d2 = evaluate_rule(rule, ctx(position_count=4, price=2398.0))
    assert d2.volume == 0.12
    assert d2.level_index == 1

    # 持仓 7 笔 -> 第 8 笔，命中档位 3（300 点 / 1.3 倍）
    d3 = evaluate_rule(rule, ctx(position_count=7, price=2397.0))
    assert d3.volume == 0.13
    assert d3.level_index == 2


def test_total_lot_limit_caps_position_count():
    rule = batched_counter_rule()
    # 已有 10 笔（= total_lot_limit），不再加仓
    assert evaluate_rule(rule, ctx(position_count=10, price=2390.0)) is None


def test_batch_enabled_but_out_of_levels_does_not_fallback():
    """开了分批却超出所有档位区间时不应回落到基础参数继续加。"""
    rule = batched_counter_rule(total_lot_limit=0)
    assert evaluate_rule(rule, ctx(position_count=11, price=2390.0)) is None


def test_batch_action_can_differ_from_base_action():
    rule = batched_counter_rule(action="all", batch_action="sell")
    assert evaluate_rule(rule, ctx(position_count=1, price=2399.0, direction="BUY")) is None
    assert evaluate_rule(
        rule, ctx(position_count=1, price=2401.0, direction="SELL")
    ) is not None


# --------------------------- 多规则协同 ---------------------------
def test_counter_evaluated_before_trend():
    counter = counter_rule()
    trend = {
        "type": RULE_TYPE_TREND, "status": 1, "action": "all",
        "point": 100, "lot_times": 0.8, "extra_lot": 0.0, "max_allow_num": 10,
        "batch_enabled": False, "batch_levels": [],
    }
    # 价格下跌 100 点：只命中逆势
    d = evaluate([trend, counter], ctx(price=2399.0))
    assert d.rule_type == RULE_TYPE_COUNTER

    # 价格上涨 100 点：只命中顺势
    d2 = evaluate([trend, counter], ctx(price=2401.0))
    assert d2.rule_type == RULE_TYPE_TREND
    assert d2.volume == 0.08
    assert d2.event_type == "add_trend"


def test_no_rules_means_no_add():
    assert evaluate([], ctx(price=2390.0)) is None


def test_zero_point_is_ignored():
    """取不到 Point() 时不做判定，避免除零或误触发。"""
    assert evaluate_rule(counter_rule(), ctx(price=2399.0, point=0)) is None
