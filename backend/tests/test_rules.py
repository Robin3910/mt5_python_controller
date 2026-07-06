"""Unit tests for pure dispatch rules (lot calc, position gate, interval filter)."""
from app import rules


# ----------------------------- lot -----------------------------
def test_resolve_volume_global_enabled():
    node = {"lot_mode": "global"}
    assert rules.resolve_volume(node, 0.5, {"enabled": True, "value": 0.2}) == 0.2


def test_resolve_volume_global_disabled_uses_signal():
    node = {"lot_mode": "global"}
    assert rules.resolve_volume(node, 0.5, {"enabled": False, "value": 0.2}) == 0.5


def test_resolve_volume_fixed():
    node = {"lot_mode": "fixed", "lot": 0.3}
    assert rules.resolve_volume(node, 0.5, {"enabled": True, "value": 0.2}) == 0.3


def test_resolve_volume_signal_mode():
    node = {"lot_mode": "signal"}
    assert rules.resolve_volume(node, 0.42, {"enabled": True, "value": 0.2}) == 0.42


def test_resolve_volume_capped():
    node = {"lot_mode": "signal"}
    assert rules.resolve_volume(node, 99, {"enabled": False}) == 1.0  # MAX_LOT_SIZE


# ------------------------- position gate ------------------------
def test_gate_symbol_scope_blocks_same_symbol():
    ok, reason = rules.position_gate(
        "BUY", False, [{"symbol": "EURUSD"}], "EURUSD", "symbol"
    )
    assert ok is False
    assert reason is not None and reason.startswith("持仓过滤")
    assert "按品种过滤" in reason and "EURUSD" in reason


def test_gate_symbol_scope_allows_other_symbol():
    ok, _ = rules.position_gate("BUY", False, [{"symbol": "GBPUSD"}], "EURUSD", "symbol")
    assert ok is True


def test_gate_account_scope_blocks_any_position():
    ok, reason = rules.position_gate(
        "BUY", False, [{"symbol": "GBPUSD"}], "EURUSD", "account"
    )
    assert ok is False
    assert reason is not None and reason.startswith("持仓过滤")
    assert "按账户过滤" in reason


def test_gate_allow_position_override():
    ok, _ = rules.position_gate("BUY", True, [{"symbol": "EURUSD"}], "EURUSD", "symbol")
    assert ok is True


def test_gate_close_never_blocked():
    ok, _ = rules.position_gate("CLOSE", False, [{"symbol": "EURUSD"}], "EURUSD", "symbol")
    assert ok is True


def test_gate_broker_suffix_match():
    ok, _ = rules.position_gate("BUY", False, [{"symbol": "XAUUSD.m"}], "XAUUSD", "symbol")
    assert ok is False  # suffix variant still counts as same symbol


# ------------------------ interval filter -----------------------
FILTERS = {
    "EURUSD": {
        "enabled": True,
        "default_action": "block",
        "intervals": [{"low": 1.05, "high": 1.10, "allow": ["BUY"]}],
    }
}


def test_interval_allows_matching_direction():
    ok, _ = rules.interval_filter("BUY", "EURUSD", 1.07, FILTERS)
    assert ok is True


def test_interval_blocks_wrong_direction():
    ok, reason = rules.interval_filter("SELL", "EURUSD", 1.07, FILTERS)
    assert ok is False
    # reason 需明确写出违反的规则：命中区间、仅允许的方向、被拦截的方向
    assert reason is not None and reason.startswith("区间方向过滤")
    assert "[1.05,1.1]" in reason
    assert "仅允许BUY" in reason and "SELL被拦截" in reason


def test_interval_default_block_outside_range():
    ok, reason = rules.interval_filter("BUY", "EURUSD", 1.20, FILTERS)
    assert ok is False
    assert reason is not None and reason.startswith("区间默认过滤")
    assert "默认动作拦截(block)" in reason


def test_interval_default_pass_outside_range():
    cfg = {"EURUSD": {"enabled": True, "default_action": "pass", "intervals": []}}
    ok, _ = rules.interval_filter("BUY", "EURUSD", 1.20, cfg)
    assert ok is True


def test_interval_disabled_passes():
    cfg = {"EURUSD": {"enabled": False, "intervals": []}}
    ok, _ = rules.interval_filter("SELL", "EURUSD", 1.07, cfg)
    assert ok is True


def test_interval_no_price_passes():
    ok, _ = rules.interval_filter("SELL", "EURUSD", None, FILTERS)
    assert ok is True


def test_interval_master_switch_blocks_buy():
    cfg = {
        "EURUSD": {
            "enabled": True,
            "allow_buy": False,
            "allow_sell": True,
            "default_action": "pass",
            "intervals": [{"low": 1.05, "high": 1.10, "allow": ["BUY"]}],
        }
    }
    ok, reason = rules.interval_filter("BUY", "EURUSD", 1.07, cfg)
    assert ok is False
    assert reason is not None and "方向总开关" in reason and "BUY" in reason


def test_interval_master_switch_blocks_sell():
    cfg = {
        "EURUSD": {
            "enabled": True,
            "allow_buy": True,
            "allow_sell": False,
            "default_action": "pass",
            "intervals": [{"low": 1.05, "high": 1.10, "allow": ["SELL"]}],
        }
    }
    ok, reason = rules.interval_filter("SELL", "EURUSD", 1.07, cfg)
    assert ok is False
    assert reason is not None and "方向总开关" in reason and "SELL" in reason


def test_interval_master_switch_defaults_open():
    cfg = {"EURUSD": {"enabled": True, "default_action": "pass", "intervals": []}}
    ok, _ = rules.interval_filter("BUY", "EURUSD", 1.07, cfg)
    assert ok is True
    ok, _ = rules.interval_filter("SELL", "EURUSD", 1.07, cfg)
    assert ok is True
