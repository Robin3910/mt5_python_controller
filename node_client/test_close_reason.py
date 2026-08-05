"""平仓原因汇总的纯逻辑单测。"""
import close_reason as cr


def _deal(reason: int, *, entry: int = 1) -> dict:
    return {"entry": entry, "reason": reason, "volume": 0.02, "profit": -1.0}


def test_all_stop_loss():
    counts = cr.count_exit_reasons([_deal(cr.REASON_SL)] * 11)
    assert counts == {"sl": 11, "tp": 0, "so": 0, "manual": 0, "expert": 0, "other": 0, "total": 11}
    assert cr.describe_close_reason(counts) == "止损打掉（11 笔）"


def test_all_take_profit():
    counts = cr.count_exit_reasons([_deal(cr.REASON_TP)] * 3)
    assert cr.describe_close_reason(counts) == "止盈兑完（3 笔）"


def test_ladder_tp_then_sl():
    deals = [_deal(cr.REASON_TP)] * 4 + [_deal(cr.REASON_SL)] * 7
    message, detail = cr.summarize_exit_deals(deals)
    assert message == "止盈 4 笔后止损收口（止损 7 笔）"
    assert detail["kind"] == "close_reason"
    assert detail["tp"] == 4 and detail["sl"] == 7


def test_manual_and_expert():
    assert cr.describe_close_reason(cr.count_exit_reasons([_deal(cr.REASON_CLIENT)] * 2)) == "人工平仓（2 笔）"
    assert cr.describe_close_reason(cr.count_exit_reasons([_deal(cr.REASON_EXPERT)])) == "程序平仓（1 笔）"


def test_stop_out():
    assert cr.describe_close_reason(cr.count_exit_reasons([_deal(cr.REASON_SO)])) == "强制平仓 Stop Out（1 笔）"


def test_ignores_entry_deals():
    deals = [_deal(cr.REASON_SL, entry=0), _deal(cr.REASON_SL, entry=1)]
    counts = cr.count_exit_reasons(deals)
    assert counts["total"] == 1
    assert counts["sl"] == 1


def test_empty_falls_back_to_default():
    message, detail = cr.summarize_exit_deals([])
    assert message == cr.DEFAULT_CLEAR_REASON
    assert detail is None


def test_mixed_other_reasons():
    deals = [_deal(cr.REASON_TP), _deal(cr.REASON_CLIENT), _deal(cr.REASON_EXPERT)]
    assert "混合出场" in cr.describe_close_reason(cr.count_exit_reasons(deals))
