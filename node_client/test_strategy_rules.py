"""策略加仓判定的单元测试（纯逻辑，无需 MT5）。"""
from strategy_rules import (
    MT5_COMMENT_LIMIT,
    RULE_TYPE_COUNTER,
    RULE_TYPE_TREND,
    PositionCtx,
    action_matches,
    decision_comment,
    decision_detail,
    describe_decision,
    deviation_points,
    evaluate,
    evaluate_rule,
    metric_key,
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


def test_rule_index_points_at_original_position():
    """判定按 type 排序，但记录的必须是规则在原快照中的下标。"""
    trend = {
        "type": RULE_TYPE_TREND, "status": 1, "action": "all",
        "point": 100, "lot_times": 0.8, "extra_lot": 0.0, "max_allow_num": 10,
        "batch_enabled": False, "batch_levels": [],
    }
    # 顺势排在前面，但命中的是排序后被先判定的逆势（原下标 1）
    assert evaluate([trend, counter_rule()], ctx(price=2399.0)).rule_index == 1
    assert evaluate([trend, counter_rule()], ctx(price=2401.0)).rule_index == 0


# --------------------------- 加仓间距计算方式 ---------------------------
def leveled_rule(level: dict, **kw):
    """单档分批规则，档位覆盖第 2 笔，便于单独验证某种计算方式。"""
    base = {"pos_from": 2, "pos_to": 4, "lot_times": 1.0, "extra_lot": 0.0}
    base.update(level)
    return batched_counter_rule(batch_levels=[base], **kw)


def test_price_calc_triggers_when_market_reaches_target():
    """指定价模式：偏离折算成到该价位所需的点数，到价才触发。"""
    rule = leveled_rule({"calc_type": "price", "price": 2399.0})
    assert evaluate_rule(rule, ctx(price=2399.5)) is None      # 还没到价
    d = evaluate_rule(rule, ctx(price=2399.0))
    assert d is not None
    assert d.calc_type == "price"
    assert (d.target_price, d.threshold) == (2399.0, 100)


def test_price_calc_requires_target_on_the_rule_direction():
    """多单逆势要求指定价低于参考价；价位在反方向等于永不成立。"""
    rule = leveled_rule({"calc_type": "price", "price": 2401.0})
    assert evaluate_rule(rule, ctx(price=2390.0)) is None
    # 空单逆势方向相反：价位需高于参考价
    sell = leveled_rule({"calc_type": "price", "price": 2401.0})
    assert evaluate_rule(sell, ctx(price=2401.0, direction="SELL")) is not None


def test_price_calc_without_target_is_skipped():
    rule = leveled_rule({"calc_type": "price", "price": 0})
    assert evaluate_rule(rule, ctx(price=2380.0)) is None


def test_atr_calc_converts_price_distance_to_points():
    """ATR 算出的是价格距离，需换算成点数再与偏离比较。"""
    rule = leveled_rule({"calc_type": "atr", "timeframe": "M5"})
    metrics = {metric_key("atr", "M5"): 1.0}          # 1.0 / 0.01 = 100 点
    assert evaluate_rule(rule, ctx(price=2399.5, bar_metrics=metrics)) is None
    d = evaluate_rule(rule, ctx(price=2399.0, bar_metrics=metrics))
    assert d is not None
    assert (d.calc_type, d.timeframe) == ("atr", "M5")
    assert (d.metric_value, d.threshold) == (1.0, 100)


def test_range_calc_uses_its_own_timeframe_metric():
    rule = leveled_rule({"calc_type": "range", "timeframe": "M15"})
    metrics = {
        metric_key("atr", "M15"): 5.0,               # 同周期的另一指标不应被误用
        metric_key("range", "M15"): 2.0,             # 2.0 / 0.01 = 200 点
    }
    assert evaluate_rule(rule, ctx(price=2399.0, bar_metrics=metrics)) is None
    d = evaluate_rule(rule, ctx(price=2398.0, bar_metrics=metrics))
    assert d is not None
    assert (d.calc_type, d.threshold) == ("range", 200)


def test_bar_calc_without_metric_is_skipped():
    """行情未就绪时不能退化成 0 阈值，否则会立刻满仓。"""
    rule = leveled_rule({"calc_type": "atr", "timeframe": "M5"})
    assert evaluate_rule(rule, ctx(price=2000.0, bar_metrics={})) is None


def test_unknown_calc_type_falls_back_to_point():
    rule = leveled_rule({"calc_type": "unknown", "point": 100})
    d = evaluate_rule(rule, ctx(price=2399.0))
    assert d is not None and d.calc_type == "point"


def test_base_params_always_use_point_calc():
    """非分批的基础参数没有计算方式，恒按点数处理。"""
    d = evaluate_rule(counter_rule(), ctx(price=2399.0))
    assert d.calc_type == "point"


# --------------------------- 开单原因与计算依据 ---------------------------
def test_decision_carries_calculation_context():
    """决策要留存全部计算依据，否则后台无法还原这一单为什么加。"""
    d = evaluate_rule(counter_rule(extra_lot=0.05), ctx(price=2399.0), 3)
    assert d.rule_index == 3
    assert (d.direction, d.base_price, d.price, d.point) == ("BUY", 2400.0, 2399.0, POINT)
    assert (d.base_volume, d.lot_times, d.extra_lot) == (0.1, 1.1, 0.05)
    assert (d.position_count, d.add_count, d.next_position_no) == (1, 0, 2)
    assert (d.limit_kind, d.limit_value) == ("max_allow_num", 3)


def test_batch_decision_records_batch_limit():
    d = evaluate_rule(batched_counter_rule(), ctx(position_count=4, price=2398.0))
    assert d.level_index == 1
    assert (d.limit_kind, d.limit_value) == ("total_lot_limit", 10)


def test_describe_decision_covers_reason_and_formula():
    d = evaluate_rule(counter_rule(), ctx(price=2399.0), 0)
    text = describe_decision(d)
    assert "逆势加仓" in text
    assert "规则#0" in text
    assert "2400" in text and "2399" in text        # 基准价 → 现价
    assert "逆向偏离 100 点" in text and "阈值 100 点" in text
    assert "0.1 × 倍数 1.1 + 追加 0 = 0.11 手" in text
    assert "第 2 笔" in text
    assert "最大加仓次数 3" in text
    assert len(text) <= 255                         # 事件说明列是 VARCHAR(255)


def test_describe_trend_decision_uses_favourable_wording():
    trend = counter_rule(type=RULE_TYPE_TREND)
    text = describe_decision(evaluate_rule(trend, ctx(price=2401.0)))
    assert "顺势加仓" in text
    assert "顺向偏离" in text


def test_decision_detail_exposes_every_parameter():
    d = evaluate_rule(batched_counter_rule(), ctx(position_count=4, price=2398.0))
    detail = decision_detail(d)
    assert detail["kind"] == "add"
    assert detail["rule_type_label"] == "逆势加仓"
    assert detail["batch"] is True and detail["level_index"] == 1
    assert detail["threshold"] == 200 and detail["deviation"] == 200
    assert detail["volume_formula"] == "0.1 × 1.2 + 0 = 0.12"
    assert detail["next_position_no"] == 5
    assert detail["limit_kind"] == "total_lot_limit"


def test_decision_comment_fits_mt5_limit():
    """MT5 备注只有约 31 字符，紧凑编码必须能反查到规则与偏离。"""
    batched = decision_comment(evaluate_rule(batched_counter_rule(), ctx(position_count=4, price=2398.0)))
    assert batched == "R1L1D200"
    assert len(batched) <= MT5_COMMENT_LIMIT

    plain = decision_comment(evaluate_rule(counter_rule(), ctx(price=2399.0)))
    assert plain == "R1D100"      # 未启用分批时不带档位段
