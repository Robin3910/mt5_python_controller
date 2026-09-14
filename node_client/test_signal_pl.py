"""模版1 信号级盈亏控制纯函数测试。"""
import signal_pl as sp


def _cfg(**over) -> dict:
    cfg = sp.default_config()
    for key, val in over.items():
        if key in cfg and isinstance(val, dict):
            cfg[key] = {**cfg[key], **val}
        else:
            cfg[key] = val
    return cfg


def test_extract_defaults_when_missing():
    cfg = sp.extract_config([{"type": 2, "status": 1, "action": "all"}])
    assert cfg["float_pl_ratio"]["enabled"] is False
    assert cfg["float_pl_ratio"]["ratio"] == -20.0
    assert "remaining_times" in cfg["float_pl_ratio"]
    assert cfg["lot_pl_tiers"]["enabled"] is False
    assert cfg["lot_pl_tiers"]["batch_count"] == 2


def test_extract_prefers_enabled_addon_rule():
    cfg = sp.extract_config([
        {
            "type": 1, "status": 0,
            "float_pl_ratio": {"enabled": True, "ratio": -10},
        },
        {
            "type": 2, "status": 1,
            "float_pl_ratio": {"enabled": True, "ratio": -30},
        },
    ])
    assert cfg["float_pl_ratio"]["ratio"] == -30.0


def test_signal_ratio_uses_signal_profit_over_balance():
    assert sp.signal_floating_pl_ratio(10000, -2000) == -20.0
    assert sp.signal_floating_pl_ratio(10000, 500) == 5.0
    assert sp.signal_floating_pl_ratio(0, -10) is None


def test_float_pl_triggers_on_signal_profit_not_account_equity():
    """同账户其它仓位的浮盈不应改变本信号盈亏比。"""
    cfg = _cfg(float_pl_ratio={"enabled": True, "ratio": -20})
    # 本信号亏 2000 / 余额 10000 = -20%；若误用账户净值会是 +30%
    hit = sp.should_trigger_float_pl(
        cfg, balance=10000, signal_profit=-2000, has_positions=True,
    )
    assert hit is not None
    assert hit["current_ratio"] == -20.0
    assert hit["close_action"] == "all"

    # 阈值未达
    miss = sp.should_trigger_float_pl(
        cfg, balance=10000, signal_profit=-1999, has_positions=True,
    )
    assert miss is None


def test_lot_pl_tiers_prefers_higher_batch():
    cfg = _cfg(lot_pl_tiers={
        "enabled": True,
        "batch_count": 2,
        "close_action": "all",
        "tiers": [
            {"min_lot": 0.1, "pl_amount": 50},
            {"min_lot": 0.5, "pl_amount": 100},
        ],
    })
    positions = [{"type": "BUY", "volume": 0.6, "profit": 120, "symbol": "XAUUSD"}]
    hit = sp.should_trigger_lot_pl_tiers(cfg, positions=positions)
    assert hit is not None
    assert hit["tier_index"] == 1
    assert hit["min_lot"] == 0.5


def test_lot_pl_tiers_side_buy_ignores_sell():
    cfg = _cfg(lot_pl_tiers={
        "enabled": True,
        "close_action": "buy",
        "batch_count": 1,
        "tiers": [{"min_lot": 0.1, "pl_amount": 10}],
    })
    positions = [
        {"type": "SELL", "volume": 1.0, "profit": 80, "symbol": "XAUUSD", "ticket": 1},
        {"type": "BUY", "volume": 0.05, "profit": 1, "symbol": "XAUUSD", "ticket": 2},
    ]
    assert sp.should_trigger_lot_pl_tiers(cfg, positions=positions) is None
    positions[1]["volume"] = 0.2
    positions[1]["profit"] = 10
    hit = sp.should_trigger_lot_pl_tiers(cfg, positions=positions)
    assert hit is not None
    targets = sp.select_close_targets(positions, hit)
    assert [p["ticket"] for p in targets] == [2]


def test_times_mode_consumes_remaining():
    cfg = sp.prepare_runtime(_cfg(float_pl_ratio={
        "enabled": True, "ratio": -20, "monitor_mode": "times", "max_times": 2,
    }))
    assert cfg["float_pl_ratio"]["remaining_times"] == 2
    hit = sp.should_trigger_float_pl(
        cfg, balance=10000, signal_profit=-2000, has_positions=True,
    )
    assert hit is not None
    cfg, state = sp.apply_float_pl_trigger(cfg)
    assert state["remaining_times"] == 1
    assert cfg["float_pl_ratio"]["enabled"] is True
    cfg, state = sp.apply_float_pl_trigger(cfg)
    assert state["disabled"] is True
    assert cfg["float_pl_ratio"]["enabled"] is False
    assert sp.should_trigger_float_pl(
        cfg, balance=10000, signal_profit=-2000, has_positions=True,
    ) is None
