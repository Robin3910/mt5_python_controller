"""Golden tests locking parser behavior to the reference repo `mt5_python_connector`.

These assert the *actual code behavior* of the reference implementation (not the
README), so the ported parser stays byte-for-byte consistent.

本文件是 `/webhook` 入参的「全场景」解析用例集，逐条锁定 WEBHOOK.md 中描述的
真实行为（含各类坑位/边界），与 test_webhook.py（HTTP 层）配套。
"""
import pytest

from app.parser import TradingSignal, TradingViewParser

parser = TradingViewParser()


def test_json_buy_basic():
    sig = parser.parse({"action": "buy", "symbol": "EURUSD", "volume": 0.1})
    assert sig is not None
    assert sig.action == "BUY"
    assert sig.symbol == "EURUSD"
    assert sig.volume == 0.1
    assert sig.order_type == "market"
    assert sig.allow_position is False


def test_json_sell_with_sl_tp_comment():
    sig = parser.parse(
        {
            "action": "sell",
            "symbol": "XAUUSD",
            "volume": 0.2,
            "sl": 2300.0,
            "tp": 2400.0,
            "comment": "pine",
        }
    )
    assert sig.action == "SELL"
    assert sig.symbol == "XAUUSD"
    assert sig.volume == 0.2
    assert sig.stop_loss == 2300.0
    assert sig.take_profit == 2400.0
    assert sig.comment == "pine"


def test_close_defaults_volume():
    sig = parser.parse({"action": "close", "symbol": "EURUSD"})
    assert sig.action == "CLOSE"
    assert sig.volume == 0.1  # DEFAULT_LOT


def test_volume_capped_at_max_lot():
    sig = parser.parse({"action": "buy", "symbol": "EURUSD", "volume": 5})
    assert sig.volume == 1.0  # MAX_LOT_SIZE


def test_volume_non_positive_falls_back_to_default():
    sig = parser.parse({"action": "buy", "symbol": "EURUSD", "volume": 0})
    assert sig.volume == 0.1


def test_chinese_keywords():
    assert parser.parse({"action": "做多", "symbol": "EURUSD"}).action == "BUY"
    assert parser.parse({"action": "做空", "symbol": "EURUSD"}).action == "SELL"
    assert parser.parse({"action": "平仓", "symbol": "EURUSD"}).action == "CLOSE"


def test_close_has_priority_over_buy():
    sig = parser.parse({"action": "close buy", "symbol": "EURUSD"})
    assert sig.action == "CLOSE"


def test_allow_position_flag():
    assert parser.parse({"action": "buy", "symbol": "EURUSD", "allow_position": 1}).allow_position is True
    assert parser.parse({"action": "buy", "symbol": "EURUSD", "allow_position": 0}).allow_position is False


def test_symbol_alias_ticker():
    sig = parser.parse({"action": "buy", "ticker": "GBPUSD"})
    assert sig.symbol == "GBPUSD"


def test_plain_text_string():
    sig = parser.parse("buy EURUSD")
    assert sig.action == "BUY"
    assert sig.symbol == "EURUSD"
    assert sig.volume == 0.1


def test_plain_text_symbol_first():
    sig = parser.parse("EURUSD sell")
    assert sig.action == "SELL"
    assert sig.symbol == "EURUSD"


def test_text_field_wrapper():
    sig = parser.parse({"text": "buy EURUSD"})
    assert sig.action == "BUY"
    assert sig.symbol == "EURUSD"


def test_unparseable_returns_none():
    assert parser.parse({"foo": "bar"}) is None
    assert parser.parse("hello world") is None


def test_json_missing_symbol_returns_none():
    # action present but no symbol field and no text fields -> None (repo behavior)
    assert parser.parse({"action": "buy"}) is None


def test_repo_quirk_signal_string_without_symbol_field():
    # README claims {"signal": "buy EURUSD 0.1"} works, but the reference *code*
    # cannot extract a symbol from the 'signal' string (only from symbol/ticker/s/sym
    # fields), and there is no text/message/body field -> returns None.
    assert parser.parse({"signal": "buy EURUSD 0.1"}) is None


def test_validate_signal():
    ok, err = parser.validate_signal(
        TradingSignal(action="BUY", symbol="EURUSD", volume=0.1)
    )
    assert ok is True and err is None

    ok, err = parser.validate_signal(
        TradingSignal(action="BUY", symbol="EURUSD", volume=0)
    )
    assert ok is False and "volume" in err.lower()


# =====================================================================
# 全场景补充用例（WEBHOOK.md §4~§12 的真实行为）
# =====================================================================

# ---------------------------- 动作 action ----------------------------
@pytest.mark.parametrize("field", ["action", "signal", "direction", "cmd"])
def test_action_field_aliases(field):
    """action 可来自 action / signal / direction / cmd 任一字段。"""
    sig = parser.parse({field: "buy", "symbol": "EURUSD"})
    assert sig is not None and sig.action == "BUY"


@pytest.mark.parametrize(
    "value,expected",
    [
        ("buy", "BUY"), ("BUY", "BUY"), ("long", "BUY"), ("做多", "BUY"),
        ("买入", "BUY"), ("多", "BUY"),
        ("sell", "SELL"), ("Short", "SELL"), ("做空", "SELL"), ("卖出", "SELL"),
        ("空", "SELL"),
        ("close", "CLOSE"), ("EXIT", "CLOSE"), ("平仓", "CLOSE"), ("平", "CLOSE"),
        ("close_all", "CLOSE"),
    ],
)
def test_action_keyword_mapping(value, expected):
    """动作关键字（大小写不敏感、中英文）映射到 BUY/SELL/CLOSE。"""
    assert parser.parse({"action": value, "symbol": "EURUSD"}).action == expected


def test_action_priority_close_over_buy_over_sell():
    """优先级 CLOSE > BUY > SELL（子串匹配）。"""
    assert parser.parse({"action": "close buy", "symbol": "EURUSD"}).action == "CLOSE"
    assert parser.parse({"action": "sell buy", "symbol": "EURUSD"}).action == "BUY"
    assert parser.parse({"action": "exit sell", "symbol": "EURUSD"}).action == "CLOSE"


def test_action_unknown_keyword_unparseable():
    """动作字段存在但无任何关键字 -> 取不到 action -> None。"""
    assert parser.parse({"action": "hello", "symbol": "EURUSD"}) is None


# ---------------------------- 品种 symbol ----------------------------
@pytest.mark.parametrize("field", ["symbol", "ticker", "s", "sym"])
def test_symbol_field_aliases(field):
    """symbol 可来自 symbol / ticker / s / sym 任一字段。"""
    assert parser.parse({"action": "buy", field: "GBPUSD"}).symbol == "GBPUSD"


def test_symbol_mapping_identity_table():
    """映射表内所有品种均为恒等映射。"""
    from app.config import Config

    for tv in Config.SYMBOLS:
        assert parser.parse({"action": "buy", "symbol": tv}).symbol == Config.SYMBOLS[tv]


def test_symbol_passthrough_unknown_uppercased_trimmed():
    """未命中映射表的品种原样透传，并做 upper()+strip()。"""
    assert parser.parse({"action": "buy", "symbol": "btcusd"}).symbol == "BTCUSD"
    assert parser.parse({"action": "buy", "symbol": "  eurusd  "}).symbol == "EURUSD"


@pytest.mark.parametrize("bad", ["", "none", "NONE", None])
def test_symbol_empty_or_none_unparseable(bad):
    """symbol 为空 / "none" / "NONE" / 缺失 -> 取不到品种 -> None。"""
    assert parser.parse({"action": "buy", "symbol": bad}) is None


def test_symbol_missing_field_unparseable():
    assert parser.parse({"action": "buy"}) is None


# ---------------------------- 手数 volume ----------------------------
@pytest.mark.parametrize("field", ["volume", "lotsize", "lot", "v", "q"])
def test_volume_field_aliases(field):
    assert parser.parse({"action": "buy", "symbol": "EURUSD", field: 0.3}).volume == 0.3


def test_volume_string_number_ok():
    assert parser.parse({"action": "buy", "symbol": "EURUSD", "volume": "0.25"}).volume == 0.25


def test_volume_default_when_missing():
    assert parser.parse({"action": "buy", "symbol": "EURUSD"}).volume == 0.1  # DEFAULT_LOT


@pytest.mark.parametrize("bad", [0, -1, "abc", "", None])
def test_volume_invalid_falls_back_to_default(bad):
    """≤0 / 非法 / 空 -> 回退 DEFAULT_LOT。"""
    assert parser.parse({"action": "buy", "symbol": "EURUSD", "volume": bad}).volume == 0.1


def test_volume_capped_at_max():
    assert parser.parse({"action": "buy", "symbol": "EURUSD", "volume": 99}).volume == 1.0  # MAX_LOT_SIZE


# ---------------------------- 止损/止盈 sl/tp ----------------------------
@pytest.mark.parametrize("field", ["sl", "stoploss", "stop_loss", "stop"])
def test_stop_loss_aliases(field):
    assert parser.parse({"action": "buy", "symbol": "EURUSD", field: 1.05}).stop_loss == 1.05


@pytest.mark.parametrize("field", ["tp", "takeprofit", "take_profit", "target"])
def test_take_profit_aliases(field):
    assert parser.parse({"action": "buy", "symbol": "EURUSD", field: 1.25}).take_profit == 1.25


@pytest.mark.parametrize("bad", [0, "0", "", "abc", None])
def test_sl_tp_zero_or_invalid_become_none(bad):
    sig = parser.parse({"action": "buy", "symbol": "EURUSD", "sl": bad, "tp": bad})
    assert sig.stop_loss is None and sig.take_profit is None


# ---------------------------- order_type / comment ----------------------------
def test_order_type_default_market_json():
    assert parser.parse({"action": "buy", "symbol": "EURUSD"}).order_type == "market"


@pytest.mark.parametrize("field", ["type", "ordertype"])
def test_order_type_aliases_lowercased(field):
    assert parser.parse({"action": "buy", "symbol": "EURUSD", field: "LIMIT"}).order_type == "limit"


def test_comment_passthrough_and_default_empty():
    assert parser.parse({"action": "buy", "symbol": "EURUSD", "comment": "pine"}).comment == "pine"
    assert parser.parse({"action": "buy", "symbol": "EURUSD"}).comment == ""


# ---------------------------- allow_position（精确规则）----------------------------
@pytest.mark.parametrize(
    "value,expected",
    [
        (1, True), ("1", True), (2, True), ("2", True), (-1, True), ("-1", True),
        (1.5, True), (True, True),
        (0, False), ("0", False), (False, False), (0.4, False),
        ("true", False), ("yes", False), ("1.5", False), (None, False),
    ],
)
def test_allow_position_int_nonzero_rule(value, expected):
    """allow_position 真实规则：int(value) 成功且非零才为 True。"""
    sig = parser.parse({"action": "buy", "symbol": "EURUSD", "allow_position": value})
    assert sig.allow_position is expected


@pytest.mark.parametrize("field", ["allow_position", "allowposition", "position_allowed"])
def test_allow_position_field_aliases(field):
    assert parser.parse({"action": "buy", "symbol": "EURUSD", field: 1}).allow_position is True


def test_allow_position_default_false():
    assert parser.parse({"action": "buy", "symbol": "EURUSD"}).allow_position is False


# ---------------------------- 纯文本（格式二）----------------------------
def test_text_action_symbol_either_order():
    assert parser.parse("buy EURUSD").action == "BUY"
    assert parser.parse("EURUSD sell").action == "SELL"


def test_text_close_priority():
    sig = parser.parse("close buy XAUUSD")
    assert sig.action == "CLOSE" and sig.symbol == "XAUUSD"


@pytest.mark.parametrize("text", ["VOLUME=0.3 buy EURUSD", "buy EURUSD LOT=0.3", "buy EURUSD 0.3 LOT"])
def test_text_volume_keywords(text):
    assert parser.parse(text).volume == 0.3


def test_text_volume_default_without_keyword():
    """裸数字（无 VOLUME/LOT 关键字）不当作手数。"""
    assert parser.parse("buy EURUSD 0.3").volume == 0.1


def test_text_volume_capped():
    assert parser.parse("buy EURUSD LOT=99").volume == 1.0


@pytest.mark.parametrize("text", ["buy EURUSD SL=1.05", "buy EURUSD STOP LOSS=1.05"])
def test_text_stop_loss(text):
    assert parser.parse(text).stop_loss == 1.05


@pytest.mark.parametrize("text", ["buy EURUSD TP=1.25", "buy EURUSD TAKE PROFIT=1.25", "buy EURUSD TARGET=1.25"])
def test_text_take_profit(text):
    assert parser.parse(text).take_profit == 1.25


def test_text_full_line():
    sig = parser.parse("close XAUUSD VOLUME=0.2 SL=2300 TP=2400")
    assert (sig.action, sig.symbol, sig.volume, sig.stop_loss, sig.take_profit) == (
        "CLOSE", "XAUUSD", 0.2, 2300.0, 2400.0,
    )


def test_text_order_type_is_none():
    """文本模式不解析 order_type，保持 None（与 JSON 默认 market 不同）。"""
    assert parser.parse("buy EURUSD").order_type is None


def test_text_no_action_or_symbol_none():
    assert parser.parse("hello world") is None  # 无动作关键字
    assert parser.parse("buy") is None            # 无品种


# ---- 文本模式品种识别坑位（WEBHOOK.md §5 ⚠️）----
def test_text_symbol_slash_form():
    assert parser.parse("EUR/USD buy").symbol == "EURUSD"
    assert parser.parse("buy EUR/USD").symbol == "EURUSD"


def test_text_bare_6_letter_symbol():
    assert parser.parse("buy XAUUSD").symbol == "XAUUSD"


@pytest.mark.parametrize("bare", ["buy GER40", "buy US30", "buy US100", "buy USOIL", "buy UKOIL"])
def test_text_bare_index_commodity_unparseable(bare):
    """裸写指数/5 字母商品（非 6 字母）在文本模式识别不了 -> None。"""
    assert parser.parse(bare) is None


@pytest.mark.parametrize("tag", ["SYMBOL=GER40 buy", "TICKER=GER40 buy", "SYMBOL=USOIL buy", "SYMBOL=UKOIL buy"])
def test_text_tagged_symbol_with_digits_ok(tag):
    """带标签且以 ≥3 字母开头的品种（GER40/USOIL/UKOIL）可识别。"""
    sig = parser.parse(tag)
    assert sig is not None and sig.symbol in {"GER40", "USOIL", "UKOIL"}


@pytest.mark.parametrize("text", ["SYMBOL=US30 buy", "SYMBOL=US100 buy"])
def test_text_us_index_tag_quirk_returns_symbol_word(text):
    """已知坑：US30/US100 标签不匹配，"SYMBOL" 一词被裸 6 字母规则误命中。"""
    sig = parser.parse(text)
    assert sig is not None and sig.symbol == "SYMBOL"


# ---------------------------- JSON 内嵌文本（格式三）----------------------------
@pytest.mark.parametrize("field", ["text", "message", "body"])
def test_json_text_field_fallback(field):
    sig = parser.parse({field: "buy EURUSD"})
    assert sig.action == "BUY" and sig.symbol == "EURUSD"


def test_json_text_field_priority_text_over_message_over_body():
    sig = parser.parse({"text": "buy EURUSD", "message": "sell GBPUSD", "body": "close XAUUSD"})
    assert sig.action == "BUY" and sig.symbol == "EURUSD"


def test_json_fallback_when_structured_fails_text_wins():
    """结构化解析失败（缺 symbol）但带 text 字段时，改用文本内容（JSON 的 action 被忽略）。"""
    sig = parser.parse({"action": "buy", "text": "sell GBPUSD"})
    assert sig.action == "SELL" and sig.symbol == "GBPUSD"


# ---------------------------- 不支持的形态 ----------------------------
@pytest.mark.parametrize("data", [["buy", "EURUSD"], 123, 1.5, True, None])
def test_unsupported_non_str_non_dict_returns_none(data):
    assert parser.parse(data) is None


def test_dict_without_action_or_symbol_returns_none():
    assert parser.parse({"foo": "bar"}) is None
    assert parser.parse({"symbol": "EURUSD"}) is None


# ---------------------------- 校验规则 validate_signal（§11）----------------------------
def test_validate_rejects_missing_action():
    ok, err = parser.validate_signal(TradingSignal(action="", symbol="EURUSD", volume=0.1))
    assert ok is False and "action" in err.lower()


def test_validate_rejects_missing_symbol():
    ok, err = parser.validate_signal(TradingSignal(action="BUY", symbol="", volume=0.1))
    assert ok is False and "symbol" in err.lower()


def test_validate_rejects_negative_sl_tp():
    ok, err = parser.validate_signal(
        TradingSignal(action="BUY", symbol="EURUSD", volume=0.1, stop_loss=-1)
    )
    assert ok is False and "stop loss" in err.lower()

    ok, err = parser.validate_signal(
        TradingSignal(action="BUY", symbol="EURUSD", volume=0.1, take_profit=-1)
    )
    assert ok is False and "take profit" in err.lower()


def test_validate_accepts_none_sl_tp():
    ok, err = parser.validate_signal(
        TradingSignal(action="BUY", symbol="EURUSD", volume=0.1, stop_loss=None, take_profit=None)
    )
    assert ok is True and err is None
