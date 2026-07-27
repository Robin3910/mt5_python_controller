"""MT5 登录号一致性校验（配置账号 vs 终端实际上报）。"""
from app.mt5_identity import is_mt5_login_mismatch, login_mismatch_reason, parse_login


def test_parse_login_accepts_positive_int_and_str():
    assert parse_login(6101) == 6101
    assert parse_login("6101") == 6101
    assert parse_login(None) is None
    assert parse_login("") is None
    assert parse_login(0) is None
    assert parse_login(-1) is None
    assert parse_login("x") is None


def test_mismatch_only_when_both_valid_and_differ():
    assert is_mt5_login_mismatch(6101, 9999) is True
    assert is_mt5_login_mismatch(6101, 6101) is False
    assert is_mt5_login_mismatch(6101, "6101") is False
    assert is_mt5_login_mismatch(6101, None) is False
    assert is_mt5_login_mismatch(6101, "") is False
    assert is_mt5_login_mismatch(None, 9999) is False


def test_login_mismatch_reason_text():
    reason = login_mismatch_reason(6101, 9999)
    assert reason is not None
    assert "6101" in reason and "9999" in reason
    assert login_mismatch_reason(6101, 6101) is None
    assert login_mismatch_reason(6101, None) is None
