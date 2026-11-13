"""信号元信息注册表（`signal_meta`）的纯函数用例。

这个模块是**统计口径的地基**：周期归一、信号类型归一、入场原因归一，
任何一处归一错了，后面的分桶就全错。所以这里逐条钉死行为，
尤其是三条最容易回归的：

1. **周期归一**：TV 的 `{{interval}}`（分钟数字符串 / `D` / `W` / `M`）
   与内部词表（`M15` / `H4`）必须收敛到**同一个**词表；
2. **未知信号类型保留原值**（不丢成 unknown）—— TV 随时可能新增结构类型；
3. **点数 / 指定价加仓不依赖周期** —— `leg_timeframe` 必须恒返回 None。
"""
import pytest

from app import signal_meta


# ---------------------------------------------------------------------------
# 周期归一
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [
        # TV 的分钟数写法
        ("1", "M1"), ("3", "M3"), ("5", "M5"), ("15", "M15"),
        ("30", "M30"), ("45", "M45"), ("60", "H1"), ("120", "H2"),
        ("180", "H3"), ("240", "H4"), ("360", "H6"), ("720", "H12"),
        # TV 的日周月写法
        ("D", "D1"), ("W", "W1"), ("M", "MN"), ("1D", "D1"),
        # 已经是内部词表
        ("M15", "M15"), ("H1", "H1"), ("D1", "D1"), ("W1", "W1"), ("MN", "MN"),
        # 大小写不敏感
        ("m15", "M15"), ("h1", "H1"), ("d1", "D1"),
        # 带 M 后缀
        ("15M", "M15"),
    ],
)
def test_normalize_timeframe_maps_known_values(raw, expected):
    assert signal_meta.normalize_timeframe(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "  ", "M7", "abc", 7, True, False])
def test_normalize_timeframe_returns_none_for_unknown(raw):
    """认不出的周期一律归 None（存 NULL），**绝不抛异常**。

    周期只是统计维度，认不出就当没标，绝不能因此拒收信号。
    """
    assert signal_meta.normalize_timeframe(raw) is None


def test_timeframe_label_falls_back_to_unmarked():
    assert signal_meta.timeframe_label(None) == "未标注"
    assert signal_meta.timeframe_label("M15") == "15 分钟"
    assert signal_meta.timeframe_label("H4") == "4 小时"


# ---------------------------------------------------------------------------
# 信号类型归一
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("ob", "ob"), ("OB", "ob"), ("fvg", "fvg"), ("pdz", "pdz"),
        ("fibo", "fibo"),
        # 别名归一
        ("orderblock", "ob"), ("order_block", "ob"),
        ("fairvaluegap", "fvg"), ("fair_value_gap", "fvg"),
        ("premium_discount", "pdz"), ("fibonacci", "fibo"),
    ],
)
def test_normalize_signal_type_maps_known_values(raw, expected):
    assert signal_meta.normalize_signal_type(raw) == expected


def test_normalize_signal_type_keeps_unknown_as_lowercase():
    """**未知类型保留原值（小写）而不是丢成 unknown**。

    TV 随时可能新增结构类型，丢了就再也统计不到。
    """
    assert signal_meta.normalize_signal_type("MM") == "mm"
    assert signal_meta.normalize_signal_type("MyNewType") == "mynewtype"


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_normalize_signal_type_returns_none_for_empty(raw):
    assert signal_meta.normalize_signal_type(raw) is None


def test_signal_type_label_falls_back_to_raw_value():
    """未知类型的标签是**原值本身**（原值是有意义的信息），不是「其它」。"""
    assert signal_meta.signal_type_label("ob") == "订单块 OB"
    # 「mm」已从词表移除（缓解块不是首仓信号类型）→ 走未知类型的回落分支
    assert signal_meta.signal_type_label("mm") == "mm"
    assert signal_meta.signal_type_label("brandnew") == "brandnew"
    assert signal_meta.signal_type_label(None) == "未标注"


def test_signal_types_registry_only_has_supported_types():
    """信号类型词表只保留**已支持**的四类。

    BOS / CHoCH / 摆动高低点 / 缓解块 在图表上是结构显示，**不作为首仓信号类型**
    推给后端 —— 词表里留着它们，前端筛选下拉就会出现筛不出任何数据的选项。
    """
    assert signal_meta.SIGNAL_TYPES == ("ob", "fvg", "pdz", "fibo")
    for removed in ("bos", "choch", "swing", "mm"):
        assert removed not in signal_meta.SIGNAL_TYPES
        assert removed not in signal_meta.SIGNAL_TYPE_LABELS
        # 归一时不再映射到别名表 —— 会退回成「未知类型保留原值」
        assert signal_meta.normalize_signal_type(removed) == removed


# ---------------------------------------------------------------------------
# 入场原因归一
# ---------------------------------------------------------------------------
def test_normalize_entry_reason_lowercases_and_strips():
    assert signal_meta.normalize_entry_reason("  ATR ") == "atr"
    assert signal_meta.normalize_entry_reason("Point") == "point"
    assert signal_meta.normalize_entry_reason(None) == ""
    assert signal_meta.normalize_entry_reason(True) == ""


def test_entry_reason_label_falls_back_to_raw_value():
    assert signal_meta.entry_reason_label("first") == "首仓"
    assert signal_meta.entry_reason_label("point") == "点数加仓"
    assert signal_meta.entry_reason_label("price") == "指定价加仓"
    assert signal_meta.entry_reason_label("unknown_reason") == "unknown_reason"
    assert signal_meta.entry_reason_label(None) == "未标注"


# ---------------------------------------------------------------------------
# 入场级周期（point / price 不依赖周期）—— 本轮拍板的核心口径
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("reason", ["point", "price"])
@pytest.mark.parametrize("tf", ["M15", "H1", None, ""])
def test_leg_timeframe_is_none_for_timeframe_independent_reasons(reason, tf):
    """**点数 / 指定价加仓恒为 None（无周期）**。

    它们不读 K 线 —— 配了周期也不读。硬凑一个周期会把「点数间距」
    混进「ATR 间距」的分桶，统计出来的数字没有意义。
    """
    assert signal_meta.leg_timeframe(reason, tf) is None


@pytest.mark.parametrize("reason", ["first", "atr", "range", "ob", "fvg", "pdz", "fibo"])
def test_leg_timeframe_returns_normalized_for_dependent_reasons(reason):
    """依赖周期的原因：周期缺失/非法 → None；否则返回归一后的周期。"""
    assert signal_meta.leg_timeframe(reason, "M15") == "M15"
    assert signal_meta.leg_timeframe(reason, "60") == "H1"
    assert signal_meta.leg_timeframe(reason, None) is None
    assert signal_meta.leg_timeframe(reason, "M7") is None


def test_timeframe_independent_and_dependent_reasons_are_disjoint():
    """两组原因必须互斥 —— 否则「恒为 None」的断言会自相矛盾。"""
    assert signal_meta.TIMEFRAME_INDEPENDENT_REASONS.isdisjoint(
        signal_meta.TIMEFRAME_DEPENDENT_REASONS
    )
    assert signal_meta.TIMEFRAME_INDEPENDENT_REASONS == {"point", "price"}


# ---------------------------------------------------------------------------
# 路由 / 触发来源收敛
# ---------------------------------------------------------------------------
def test_safe_route_state_rejects_unknown():
    assert signal_meta.safe_route_state("pending") == "pending"
    assert signal_meta.safe_route_state("PENDING") == "pending"
    assert signal_meta.safe_route_state("nope") is None
    assert signal_meta.safe_route_state(None) is None


def test_safe_route_mode_rejects_unknown():
    assert signal_meta.safe_route_mode("direct") == "direct"
    assert signal_meta.safe_route_mode("center") == "center"
    assert signal_meta.safe_route_mode("nope") is None


def test_safe_trigger_source_rejects_unknown():
    assert signal_meta.safe_trigger_source("tv_webhook") == "tv_webhook"
    assert signal_meta.safe_trigger_source("nope") is None


def test_normalize_signal_source_defaults_to_direct():
    """存量分组零行为变化：缺省 / 非法一律回落 direct。"""
    assert signal_meta.normalize_signal_source(None) == "direct"
    assert signal_meta.normalize_signal_source("") == "direct"
    assert signal_meta.normalize_signal_source("nope") == "direct"
    assert signal_meta.normalize_signal_source("center") == "center"
    assert signal_meta.normalize_signal_source("CENTER") == "center"


def test_labels_fall_back_without_raising():
    """标签函数对任何输入都不抛异常（前端要直接渲染）。"""
    assert signal_meta.route_state_label("pending") == "待路由"
    assert signal_meta.route_state_label("nope") == "—"
    assert signal_meta.route_mode_label("center") == "信号中心"
    assert signal_meta.route_mode_label(None) == "—"
    assert signal_meta.trigger_source_label("tv_webhook") == "TV 告警"
    assert signal_meta.trigger_source_label("nope") == "—"
