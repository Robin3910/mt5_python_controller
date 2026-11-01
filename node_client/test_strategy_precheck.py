"""配置保存前预检：SL/TP 越过现价、上限低于已开笔数、手动仓锁定。"""
import strategy_precheck as sp


def _rule(**over) -> dict:
    rule = {
        "type": 1, "status": 1, "action": "all",
        "point": 100, "lot_times": 1.0, "extra_lot": 0.0,
        "max_allow_num": 5, "batch_enabled": False, "batch_levels": [],
        "total_lot_limit": 5,
        "lot_pl_tiers": {
            "enabled": True, "batch_count": 1, "close_action": "all",
            "tiers": [{"min_lot": 0.1, "pl_amount": 100.0}],
        },
        "float_pl_ratio": {
            "enabled": False, "ratio": -20.0, "action": "close_all",
            "monitor_mode": "loop", "max_times": 1,
        },
        "manual_scatter": {"enabled": False},
    }
    rule.update(over)
    return rule


def _pos(**over) -> dict:
    pos = {
        "ticket": 1, "type": "BUY", "volume": 0.1, "price_open": 4400.0,
        "sl": 0.0, "tp": 0.0, "profit": 0.0, "time": 1.0, "comment": "R1D100",
    }
    pos.update(over)
    return pos


# ----------------------------------------------------------------------
# 上限下限
# ----------------------------------------------------------------------
def test_limit_below_added_is_blocked():
    blocks = sp.check_limit_below_added(
        [_rule(total_lot_limit=2)], added_by_rule={1: 3},
    )
    assert len(blocks) == 1
    assert blocks[0]["code"] == "limit_below_added"
    assert blocks[0]["added"] == 3
    assert blocks[0]["limit"] == 2
    assert "低于已开笔数" in blocks[0]["message"]


def test_limit_equal_to_added_is_allowed():
    blocks = sp.check_limit_below_added(
        [_rule(total_lot_limit=3)], added_by_rule={1: 3},
    )
    assert blocks == []


def test_limit_zero_is_unlimited():
    blocks = sp.check_limit_below_added(
        [_rule(total_lot_limit=0)], added_by_rule={1: 3},
    )
    assert blocks == []


def test_limit_check_is_per_rule():
    rules = [_rule(type=1, total_lot_limit=2), _rule(type=2, total_lot_limit=1)]
    blocks = sp.check_limit_below_added(rules, added_by_rule={1: 3, 2: 0})
    assert len(blocks) == 1
    assert blocks[0]["rule_type"] == 1


# ----------------------------------------------------------------------
# 手动仓锁定
# ----------------------------------------------------------------------
def test_manual_locked_is_reported():
    """已成交（FILLED）：阻断保存。"""
    blocks = sp.check_manual_locked([_rule(type=2)], manual_locked={2})
    assert len(blocks) == 1
    assert blocks[0]["code"] == "manual_locked"
    assert blocks[0]["rule_type"] == 2


def test_manual_not_locked_while_pending():
    """挂单在盘上（PENDING）：不阻断，客户可以改参数。"""
    assert sp.check_manual_locked([_rule(type=2)], manual_locked=set()) == []


# ----------------------------------------------------------------------
# SL/TP 越过现价
# ----------------------------------------------------------------------
def test_sltp_crossed_detected():
    """多单开在 4400，止盈算出来在 4410、现价 4412 已越过 → 阻断。"""
    blocks = sp.check_sltp_crossed(
        [_pos(price_open=4400.0)],
        rules=[_rule()],
        tick_size=0.01, tick_value=1.0, digits=2,
        bid=4412.0, ask=4412.0, fallback_price=4412.0,
    )
    assert len(blocks) == 1
    assert blocks[0]["code"] == "sltp_crossed"
    assert blocks[0]["crossed"] == "tp"
    assert "止盈" in blocks[0]["message"]


def test_sltp_not_crossed_is_allowed():
    """现价 4405，止盈 4410，还没越过 → 放行。"""
    blocks = sp.check_sltp_crossed(
        [_pos(price_open=4400.0)],
        rules=[_rule()],
        tick_size=0.01, tick_value=1.0, digits=2,
        bid=4405.0, ask=4405.0, fallback_price=4405.0,
    )
    assert blocks == []


def test_sltp_crossed_needs_positions_and_spec():
    assert sp.check_sltp_crossed(
        [], rules=[_rule()], tick_size=0.01, tick_value=1.0, digits=2,
    ) == []
    # 合约规格无效时算不出价格 → 不阻断
    assert sp.check_sltp_crossed(
        [_pos()], rules=[_rule()], tick_size=0, tick_value=0, digits=2,
        bid=4412.0, ask=4412.0, fallback_price=4412.0,
    ) == []


def test_sltp_crossed_skips_manual_positions():
    """手动仓不参与共同止盈止损计算，也不该被算成 crossed。"""
    manual = _pos(comment="M2")
    blocks = sp.check_sltp_crossed(
        [manual],
        rules=[_rule()],
        tick_size=0.01, tick_value=1.0, digits=2,
        bid=4412.0, ask=4412.0, fallback_price=4412.0,
    )
    assert blocks == []


def test_summarize_joins_messages():
    blocks = [
        {"message": "第一条"},
        {"message": "第二条"},
    ]
    assert sp.summarize(blocks) == "第一条；第二条"
    assert sp.summarize([]) == ""


def test_describe_crossed_for_sl():
    text = sp.describe_crossed({
        "side": "SELL", "crossed": "sl", "tp": 4390.0, "sl": 4410.0,
        "market": 4415.0,
    })
    assert "空单" in text
    assert "止损" in text


# ----------------------------------------------------------------------
# 档位锚定
# ----------------------------------------------------------------------
def _batched_rule(levels: list[dict], **over) -> dict:
    rule = _rule(batch_enabled=True, batch_levels=levels, **over)
    return rule


def test_batch_anchor_accepts_contiguous_levels():
    levels = [
        {"pos_from": 1, "pos_to": 3},
        {"pos_from": 4, "pos_to": 10},
    ]
    blocks = sp.check_batch_anchor([_batched_rule(levels)], added_by_rule={1: 2})
    assert blocks == []


def test_batch_anchor_rejects_overlapping_levels():
    levels = [
        {"pos_from": 1, "pos_to": 5},
        {"pos_from": 3, "pos_to": 10},
    ]
    blocks = sp.check_batch_anchor([_batched_rule(levels)], added_by_rule={1: 2})
    codes = {b["code"] for b in blocks}
    assert "batch_inconsistent" in codes


def test_batch_anchor_rejects_uncovered_next_position():
    """已开 5 笔，但档位只到 4 → 第 6 次加仓没有档位可命中。"""
    levels = [{"pos_from": 1, "pos_to": 4}]
    blocks = sp.check_batch_anchor([_batched_rule(levels)], added_by_rule={1: 5})
    assert len(blocks) == 1
    assert blocks[0]["code"] == "batch_inconsistent"
    assert "没有覆盖" in blocks[0]["message"]


def test_batch_anchor_skipped_when_no_adds():
    levels = [{"pos_from": 1, "pos_to": 4}]
    assert sp.check_batch_anchor([_batched_rule(levels)], added_by_rule={1: 0}) == []


def test_batch_anchor_skipped_when_batch_disabled():
    rule = _rule(batch_enabled=False, batch_levels=[])
    assert sp.check_batch_anchor([rule], added_by_rule={1: 3}) == []
