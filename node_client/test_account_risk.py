"""账户级风控纯函数单测。"""
import account_risk as ar


def test_floating_pl_ratio():
    assert ar.floating_pl_ratio(10000, 8000) == -20.0
    assert ar.floating_pl_ratio(10000, 12000) == 20.0


def test_should_trigger_loss_side():
    risk = {
        "float_pl_ratio": {
            "enabled": True, "ratio": -20, "action": "close_all",
            "monitor_mode": "loop", "max_times": 1, "remaining_times": 0,
        }
    }
    hit = ar.should_trigger_float_pl(
        risk, balance=10000, equity=7900, has_positions=True,
    )
    assert hit is not None
    assert hit["close_action"] == "account_all"


def test_symbol_pl_orders_trigger():
    positions = [
        {"symbol": "XAUUSD", "type": "BUY", "volume": 0.1, "profit": 120},
        {"symbol": "XAUUSD", "type": "BUY", "volume": 0.1, "profit": 10},
    ]
    risk = {
        "symbol_pl_orders": {
            "items": [{
                "id": "a1",
                "enabled": True,
                "symbol": "XAUUSD",
                "pl_amount": 100,
                "order_op": "gte",
                "order_count": 2,
                "close_action": "buy",
                "monitor_mode": "loop",
                "max_times": 1,
                "remaining_times": 0,
            }]
        }
    }
    hit = ar.should_trigger_symbol_pl_orders(risk, positions=positions)
    assert hit is not None
    assert hit["rule"] == ar.RULE_SYMBOL_PL_ORDERS
    assert hit["close_action"] == "buy"
    assert hit["order_count"] == 2


def test_symbol_pl_orders_order_op_blocks():
    positions = [{"symbol": "XAUUSD", "type": "BUY", "volume": 0.1, "profit": 200}]
    risk = {
        "symbol_pl_orders": {
            "items": [{
                "id": "a1", "enabled": True, "symbol": "XAUUSD",
                "pl_amount": 100, "order_op": "gte", "order_count": 2,
                "close_action": "all", "monitor_mode": "loop",
                "max_times": 1, "remaining_times": 0,
            }]
        }
    }
    assert ar.should_trigger_symbol_pl_orders(risk, positions=positions) is None


def test_no_trigger_when_action_has_no_matching_side():
    """动作是多单平仓但该品种只有空单：不触发，否则会空平一次白扣次数。"""
    positions = [{"symbol": "XAUUSD", "type": "SELL", "volume": 0.1, "profit": -150}]
    risk = {
        "symbol_pl_orders": {
            "items": [{
                "id": "s1", "enabled": True, "symbol": "XAUUSD",
                "pl_amount": -100, "order_op": "any", "order_count": 0,
                "close_action": "buy", "monitor_mode": "times",
                "max_times": 1, "remaining_times": 1,
            }]
        }
    }
    assert ar.should_trigger_symbol_pl_orders(risk, positions=positions) is None

    positions.append({"symbol": "XAUUSD", "type": "BUY", "volume": 0.1, "profit": -20})
    hit = ar.should_trigger_symbol_pl_orders(risk, positions=positions)
    assert hit is not None
    assert hit["close_count"] == 1


def test_hedge_requires_both_sides():
    positions = [
        {"symbol": "XAUUSD", "type": "BUY", "volume": 0.1, "profit": -30},
        {"symbol": "EURUSD", "type": "SELL", "volume": 0.1, "profit": -30},
    ]
    risk = {
        "symbol_pl_orders": {
            "items": [{
                "id": "h1", "enabled": True, "symbol": "XAUUSD",
                "pl_amount": -20, "order_op": "any", "order_count": 0,
                "close_action": "hedge", "monitor_mode": "loop",
                "max_times": 1, "remaining_times": 0,
            }]
        }
    }
    assert ar.should_trigger_symbol_pl_orders(risk, positions=positions) is None
    positions.append({"symbol": "XAUUSD", "type": "SELL", "volume": 0.1, "profit": -25})
    hit = ar.should_trigger_symbol_pl_orders(risk, positions=positions)
    assert hit is not None


def test_protect_state_machine_loss():
    positions = [{"symbol": "XAUUSD", "type": "BUY", "volume": 0.1, "profit": -120}]
    risk = {
        "symbol_pl_protect": {
            "items": [{
                "id": "p1", "enabled": True, "symbol": "XAUUSD",
                "trigger_amount": -100, "narrow_amount": -50,
                "monitor_mode": "loop", "max_times": 1, "remaining_times": 0,
            }]
        }
    }
    armed, hit, events = ar.evaluate_protect_items(risk, positions=positions, armed_map={})
    assert hit is None
    assert armed["p1"] is True
    assert len(events) == 1

    positions[0]["profit"] = -40
    armed2, hit2, events2 = ar.evaluate_protect_items(
        risk, positions=positions, armed_map=armed,
    )
    assert hit2 is not None
    assert hit2["rule"] == ar.RULE_SYMBOL_PL_PROTECT
    assert events2 == []


def test_protect_profit_side():
    positions = [{"symbol": "XAUUSD", "type": "BUY", "volume": 0.1, "profit": 120}]
    risk = {
        "symbol_pl_protect": {
            "items": [{
                "id": "p2", "enabled": True, "symbol": "XAUUSD",
                "trigger_amount": 100, "narrow_amount": 50,
                "monitor_mode": "loop", "max_times": 1, "remaining_times": 0,
            }]
        }
    }
    armed, hit, _ = ar.evaluate_protect_items(risk, positions=positions, armed_map={})
    assert hit is None and armed["p2"]
    positions[0]["profit"] = 40
    _, hit2, _ = ar.evaluate_protect_items(risk, positions=positions, armed_map=armed)
    assert hit2 is not None


def test_lot_pl_tiers():
    positions = [
        {"symbol": "XAUUSD", "type": "BUY", "volume": 0.6, "profit": 120},
    ]
    risk = {
        "lot_pl_tiers": {
            "enabled": True,
            "batch_count": 2,
            "close_action": "buy",
            "tiers": [
                {"min_lot": 0.1, "pl_amount": 50},
                {"min_lot": 0.5, "pl_amount": 100},
            ],
        }
    }
    hit = ar.should_trigger_lot_pl_tiers(risk, positions=positions)
    assert hit is not None
    assert hit["tier_index"] == 1
    assert hit["close_action"] == "buy"


def test_apply_trigger_list_item_times():
    risk = {
        "symbol_pl_orders": {
            "items": [{
                "id": "x1", "enabled": True, "symbol": "XAUUSD",
                "pl_amount": 10, "order_op": "any", "order_count": 0,
                "close_action": "all", "monitor_mode": "times",
                "max_times": 1, "remaining_times": 1,
            }]
        }
    }
    cfg, state = ar.apply_trigger_to_risk(risk, ar.RULE_SYMBOL_PL_ORDERS, "x1")
    assert state["disabled"] is True
    assert cfg["symbol_pl_orders"]["items"][0]["enabled"] is False


def _protect_risk(trigger=-100, narrow=-50, enabled=True):
    return {
        "symbol_pl_protect": {
            "items": [{
                "id": "p1", "enabled": enabled, "symbol": "XAUUSD",
                "trigger_amount": trigger, "narrow_amount": narrow,
                "monitor_mode": "loop", "max_times": 1, "remaining_times": 0,
            }]
        }
    }


def test_prune_armed_map_keeps_progress_on_reconnect():
    """重连会重新下发同一份配置，保护进度不该被清掉。"""
    risk = _protect_risk()
    assert ar.prune_armed_map(risk, {"p1": True}, previous=risk) == {"p1": True}


def test_prune_armed_map_keeps_progress_when_amount_edited():
    """只调金额时按新目标继续判定，进度保留。"""
    old = _protect_risk(trigger=-100, narrow=-50)
    new = _protect_risk(trigger=-100, narrow=-30)
    assert ar.prune_armed_map(new, {"p1": True}, previous=old) == {"p1": True}


def test_prune_armed_map_drops_on_disable_or_direction_flip():
    old = _protect_risk(trigger=-100, narrow=-50)
    assert ar.prune_armed_map(_protect_risk(enabled=False), {"p1": True}, previous=old) == {}
    flipped = _protect_risk(trigger=100, narrow=50)
    assert ar.prune_armed_map(flipped, {"p1": True}, previous=old) == {}
    assert ar.prune_armed_map({}, {"p1": True}, previous=old) == {}


def test_apply_trigger_reports_unmatched_item():
    """条目在回写前被改掉：明确报告未命中，而不是静默不扣次数。"""
    risk = {
        "symbol_pl_orders": {
            "items": [{
                "id": "keep", "enabled": True, "symbol": "XAUUSD",
                "pl_amount": 10, "order_op": "any", "order_count": 0,
                "close_action": "all", "monitor_mode": "times",
                "max_times": 2, "remaining_times": 2,
            }]
        }
    }
    _, state = ar.apply_trigger_to_risk(risk, ar.RULE_SYMBOL_PL_ORDERS, "gone")
    assert state["matched"] is False

    _, state2 = ar.apply_trigger_to_risk(risk, ar.RULE_SYMBOL_PL_ORDERS, "keep")
    assert state2["matched"] is True
    assert state2["remaining_times"] == 1


def test_select_close_targets():
    positions = [
        {"symbol": "XAUUSD", "type": "BUY", "ticket": 1},
        {"symbol": "XAUUSD", "type": "SELL", "ticket": 2},
        {"symbol": "EURUSD", "type": "BUY", "ticket": 3},
    ]
    buys = ar.select_close_targets(positions, close_action="buy", symbol="XAUUSD")
    assert len(buys) == 1 and buys[0]["ticket"] == 1
    hedge = ar.select_close_targets(positions, close_action="hedge", symbol="XAUUSD")
    assert len(hedge) == 2
    all_buy = ar.select_close_targets(positions, close_action="buy")
    assert len(all_buy) == 2


def test_describe_trigger_includes_rule_and_params():
    hit = ar.should_trigger_lot_pl_tiers(
        {
            "lot_pl_tiers": {
                "enabled": True,
                "batch_count": 2,
                "close_action": "all",
                "tiers": [
                    {"min_lot": 0.1, "pl_amount": 50},
                    {"min_lot": 0.5, "pl_amount": 20},
                ],
            }
        },
        positions=[
            {"symbol": "XAUUSD", "type": "BUY", "volume": 0.6, "profit": 26.4},
        ],
    )
    assert hit is not None
    text = ar.describe_trigger(hit)
    assert "手数分档盈亏" in text
    assert "0.6" in text and "0.5" in text
    assert "26.40" in text or "26.4" in text
    assert "账户全部平仓" in text or "全部平仓" in text

    detail = ar.trigger_detail(hit)
    assert detail["kind"] == "account_risk"
    assert detail["rule"] == ar.RULE_LOT_PL_TIERS
    assert detail["min_lot"] == 0.5
    assert detail["pl_amount"] == 20
    assert detail["current_lot"] == 0.6


def test_describe_trigger_float_pl_ratio():
    hit = {
        "rule": ar.RULE_FLOAT_PL_RATIO,
        "close_action": "account_all",
        "threshold": -20,
        "current_ratio": -21.5,
        "message_core": "浮盈亏比 -21.50% 达到阈值 -20%",
    }
    text = ar.describe_trigger(hit)
    assert text.startswith("账户风控·浮盈亏比例：")
    assert "-21.50%" in text and "-20%" in text
    detail = ar.trigger_detail(hit)
    assert detail["ratio_threshold"] == -20
    assert detail["current_ratio"] == -21.5
