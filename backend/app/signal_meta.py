"""信号的**元信息**注册表：周期 / 信号类型 / 入场原因。

与 `signal_codes.py` 同一范式：**编码是数据，文字是展示**。
统计侧一律按 `code` 聚合，永远不碰中文标签。

这个模块只做三件事：
1. **归一**：把 TV 告警里的各种写法（``{{interval}}`` / ``60`` / ``H1``）收敛成统一词表；
2. **判周期依赖**：哪些入场原因真的依赖周期，哪些不依赖（点数 / 指定价）；
3. **给标签**：给人读的中文名。

**铁律（与 `signal_codes` 一致）**：
- 缺周期 / 缺类型**绝不拒收信号** —— 它们只是统计维度，非法值一律存 NULL；
- 未知类型**保留原值（小写）**而不是丢成 unknown —— TV 随时可能新增结构类型，
  丢了就再也统计不到；
- 本模块**不参与任何交易判断**：写失败、写漏、写错都只影响统计。

---

## 一、周期

### 1.1 归一表

TV 的 ``{{interval}}`` 给的是「分钟数」或 ``D`` / ``W`` / ``M``；策略档位里写的是
``M15`` / ``H4`` / ``D1``。两套写法都要能收敛到同一个词表。

归一表见模块内的 ``_TIMEFRAME_MAP``（TV 分钟数、TV 日周月、已是内部词表三类）。

**内部词表**（顺序即展示顺序，短周期在前）：

```python
TIMEFRAMES: tuple[str, ...] = (
    "M1", "M3", "M5", "M15", "M30", "M45",
    "H1", "H2", "H3", "H4", "H6", "H8", "H12",
    "D1", "W1", "MN",
)
```

### 1.2 归一函数

```python
def normalize_timeframe(value: object) -> Optional[str]:
    # 把任意写法的周期收敛成内部词表；认不出返回 None（存 NULL，不拒收）。
    ...
```

**行为约定**：
- 空 / None → `None`
- 大小写不敏感（`m15` → `M15`）
- 认不出的值（如 `M7`）→ `None`，**不抛异常**
- 归一后仍认不出 → `None`

---

## 二、信号类型（首仓入场原因）

TV 告警里的结构类型。**未知类型保留原值**（小写），这样 TV 新增结构类型时
不需要改代码就能统计。

```python
SIGNAL_TYPE_OB   = "ob"      # 订单块
SIGNAL_TYPE_FVG  = "fvg"     # 公允价值缺口
SIGNAL_TYPE_PDZ  = "pdz"     # 溢价折让区
SIGNAL_TYPE_FIBO = "fibo"    # 斐波那契
```

标签表 `SIGNAL_TYPE_LABELS`；`signal_type_label(code)` 未知时回落到**原值本身**
（而不是「其它」）—— 因为原值是有意义的信息。

---

## 三、入场原因（每一次入场）

「入场原因」与「信号类型」**不是一回事**，不要合并：

| 概念 | 范围 | 来源 |
|---|---|---|
| 信号类型 | **首仓**这一条信号 | TV 告警 |
| 入场原因 | **每一次**入场（首仓 + 每一档加仓） | 策略档位配置 / TV 告警 |

```python
ENTRY_FIRST  = "first"    # 首仓
ENTRY_POINT  = "point"    # 固定点数加仓
ENTRY_PRICE  = "price"    # 指定价加仓
ENTRY_ATR    = "atr"      # ATR 间距
ENTRY_RANGE  = "range"    # 波幅间距
ENTRY_FVG    = "fvg"
ENTRY_OB     = "ob"
ENTRY_PDZ    = "pdz"
ENTRY_FIBO   = "fibo"
ENTRY_MANUAL = "manual"   # 手动触发（无档位）
```

### 3.1 周期依赖（§一.1 的核心）

**只有依赖周期的入场原因才写 `timeframe`**；点数与指定价**恒为 NULL**。

```python
# 不依赖周期的入场原因：它们的「周期」字段配了也不读
TIMEFRAME_INDEPENDENT_REASONS: frozenset[str] = frozenset({
    ENTRY_POINT, ENTRY_PRICE,
})

# 依赖周期的原因：ATR / 波幅要读 K 线；SMC 四类靠周期匹配 TV 推送的价位
TIMEFRAME_DEPENDENT_REASONS: frozenset[str] = frozenset({
    ENTRY_ATR, ENTRY_RANGE, ENTRY_FVG, ENTRY_OB, ENTRY_PDZ, ENTRY_FIBO,
})
```

### 3.2 ``leg_timeframe`` 的写入规则

```python
def leg_timeframe(reason: str, timeframe: object) -> Optional[str]:
    # 一次入场实际生效的周期：
    #   - 原因不依赖周期（point / price）→ 恒为 None；
    #   - 原因依赖周期但周期缺失 / 非法 → None；
    #   - 否则返回归一后的周期。
    ...
```

---

## 四、路由

```python
ROUTE_PENDING = "pending"    # 已入信号中心，等待路由
ROUTE_AUTO    = "auto"       # 直连模式已被分组自动消费
ROUTE_ROUTED  = "routed"     # 被信号中心选中下发过（人工介入过）
ROUTE_DROPPED = "dropped"    # 被信号中心的筛选规则丢弃

ROUTE_MODE_DIRECT = "direct"  # 分组直连（TV / 手动直接触发）
ROUTE_MODE_CENTER = "center"  # 信号中心下发
```

触发来源（`group_signal_task.trigger_source`）：

```python
TRIGGER_TV_WEBHOOK   = "tv_webhook"
TRIGGER_MANUAL       = "manual"
TRIGGER_LIMIT_WATCH  = "limit_watch"
TRIGGER_CENTER       = "center"
```

---

## 五、辅助

`safe_route_state` / `safe_route_mode` / `safe_trigger_source`：
把写入点传来的值收敛成合法值，非法值一律归 `None`（**绝不抛异常**）。

`timeframe_label` / `signal_type_label` / `entry_reason_label`：人读标签。
"""
from __future__ import annotations

from typing import Optional

# ---------------------------------------------------------------------------
# 周期
# ---------------------------------------------------------------------------

# TV 的 {{interval}} 与内部词表之间的归一表
_TIMEFRAME_MAP: dict[str, str] = {
    # TV 分钟数
    "1": "M1", "3": "M3", "5": "M5", "15": "M15", "30": "M30", "45": "M45",
    "60": "H1", "120": "H2", "180": "H3", "240": "H4", "360": "H6", "480": "H8",
    "720": "H12",
    # TV 日 / 周 / 月
    "1D": "D1", "D": "D1", "1W": "W1", "W": "W1", "1M": "MN", "M": "MN",
    # 已是内部词表
    "M1": "M1", "M3": "M3", "M5": "M5", "M15": "M15", "M30": "M30", "M45": "M45",
    "H1": "H1", "H2": "H2", "H3": "H3", "H4": "H4", "H6": "H6", "H8": "H8",
    "H12": "H12", "D1": "D1", "W1": "W1", "MN": "MN",
}

# 内部词表（顺序即展示顺序）
TIMEFRAMES: tuple[str, ...] = (
    "M1", "M3", "M5", "M15", "M30", "M45",
    "H1", "H2", "H3", "H4", "H6", "H8", "H12",
    "D1", "W1", "MN",
)

# 周期的人读标签
TIMEFRAME_LABELS: dict[str, str] = {
    "M1": "1 分钟", "M3": "3 分钟", "M5": "5 分钟", "M15": "15 分钟",
    "M30": "30 分钟", "M45": "45 分钟",
    "H1": "1 小时", "H2": "2 小时", "H3": "3 小时", "H4": "4 小时",
    "H6": "6 小时", "H8": "8 小时", "H12": "12 小时",
    "D1": "日线", "W1": "周线", "MN": "月线",
}


def normalize_timeframe(value: object) -> Optional[str]:
    """把任意写法的周期收敛成内部词表；认不出返回 None。

    **绝不抛异常**：周期只是统计维度，认不出就存 NULL，绝不影响信号受理。
    """
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip().upper()
    if not text:
        return None
    hit = _TIMEFRAME_MAP.get(text)
    if hit:
        return hit
    # 兜底：纯数字按分钟数再试一次（TV 有时给 "15" 也可能给 "15m"）
    if text.endswith("M") and text[:-1].isdigit():
        return _TIMEFRAME_MAP.get(text[:-1])
    return None


def timeframe_label(value: object) -> str:
    """周期的人读标签；未标注时返回「未标注」。"""
    tf = normalize_timeframe(value)
    if not tf:
        return "未标注"
    return TIMEFRAME_LABELS.get(tf, tf)


# ---------------------------------------------------------------------------
# 信号类型（首仓）
# ---------------------------------------------------------------------------

SIGNAL_TYPE_OB = "ob"
SIGNAL_TYPE_FVG = "fvg"
SIGNAL_TYPE_PDZ = "pdz"
SIGNAL_TYPE_FIBO = "fibo"

SIGNAL_TYPES: tuple[str, ...] = (
    SIGNAL_TYPE_OB, SIGNAL_TYPE_FVG, SIGNAL_TYPE_PDZ, SIGNAL_TYPE_FIBO,
)

SIGNAL_TYPE_LABELS: dict[str, str] = {
    SIGNAL_TYPE_OB: "订单块 OB",
    SIGNAL_TYPE_FVG: "公允价值缺口 FVG",
    SIGNAL_TYPE_PDZ: "溢价折让区 PDZ",
    SIGNAL_TYPE_FIBO: "斐波那契 Fibo",
}

# 信号类型 → **业务分类**（原型的 6 个分类）。
#
# 这是**展示层**的归类：结构分析类的三个码（ob / fvg / pdz）在原型里同属
# 「结构分析」一栏，但它们在统计上仍是三个独立的类型码（分桶仍按码分）。
# 未登记的码归「其它」并保留原码 —— 遵循「未知类型保留原值」原则。
SIGNAL_TYPE_GROUPS: dict[str, str] = {
    SIGNAL_TYPE_OB: "结构分析",
    SIGNAL_TYPE_FVG: "结构分析",
    SIGNAL_TYPE_PDZ: "结构分析",
    SIGNAL_TYPE_FIBO: "斐波那契回踩",
    "bos": "突破信号",
    "swing": "趋势跟踪",
    "choch": "反转信号",
    "mm": "量价背离",
}

GROUP_OTHER = "其它"


def types_in_group(group: object) -> list[str]:
    """业务分类 → 它包含的**类型码**列表。

    「结构分析」这一类下面有 ob / fvg / pdz 三个码 —— 按分类筛选时
    必须展开成 `IN (...)`，只取一个码会把另外两个漏掉。

    未登记的分类返回空列表（调用方据此不过滤，而不是筛成空集）。
    """
    name = str(group or "").strip()
    if not name:
        return []
    return [code for code, g in SIGNAL_TYPE_GROUPS.items() if g == name]


def normalize_signal_type(value: object) -> Optional[str]:
    """信号类型归一。

    **未知类型保留原值（小写）而不是丢成 unknown** —— TV 随时可能新增结构类型，
    丢了就再也统计不到。新增类型只需在 `SIGNAL_TYPE_LABELS` 补个中文标签。
    """
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    if text in SIGNAL_TYPE_LABELS:
        return text
    # 别名归一（TV 侧可能用全称）
    alias = {
        "orderblock": SIGNAL_TYPE_OB,
        "order_block": SIGNAL_TYPE_OB,
        "fairvaluegap": SIGNAL_TYPE_FVG,
        "fair_value_gap": SIGNAL_TYPE_FVG,
        "premiumdiscount": SIGNAL_TYPE_PDZ,
        "premium_discount": SIGNAL_TYPE_PDZ,
        "fibonacci": SIGNAL_TYPE_FIBO,
    }
    return alias.get(text, text)


def signal_type_label(value: object) -> str:
    """信号类型的人读标签；未知类型回落到原值本身（原值是有意义的信息）。"""
    code = normalize_signal_type(value)
    if not code:
        return "未标注"
    return SIGNAL_TYPE_LABELS.get(code, code)


def signal_type_group(value: object) -> str:
    """信号类型 → 业务分类（展示层归类）。

    未登记的类型归「其它」——**不丢原码**（分类只是展示，原码仍可用）。
    """
    code = normalize_signal_type(value)
    if not code:
        return "未标注"
    return SIGNAL_TYPE_GROUPS.get(code, GROUP_OTHER)


def action_label(value: object) -> str:
    """多空方向的人读标签。"""
    code = str(value or "").strip().upper()
    if code == "BUY":
        return "多"
    if code == "SELL":
        return "空"
    if code == "CLOSE":
        return "平"
    return "未标注"


def signal_name(action: object, signal_type: object, timeframe: object = None) -> str:
    """可读的信号名（前端拼装，库里没有这个字段）。

    形如「看涨 · 订单块 OB 回踩」；缺失的部分直接省略，不留空占位。
    """
    parts: list[str] = []
    code = str(action or "").strip().upper()
    if code == "BUY":
        parts.append("看涨")
    elif code == "SELL":
        parts.append("看跌")
    elif code == "CLOSE":
        parts.append("平仓")
    st = normalize_signal_type(signal_type)
    if st:
        label = SIGNAL_TYPE_LABELS.get(st, st)
        parts.append(f"{label} 回踩")
    return " · ".join(parts) if parts else "未标注信号"


# ---------------------------------------------------------------------------
# 入场原因（每一次入场）
# ---------------------------------------------------------------------------

ENTRY_FIRST = "first"
ENTRY_POINT = "point"
ENTRY_PRICE = "price"
ENTRY_ATR = "atr"
ENTRY_RANGE = "range"
ENTRY_FVG = "fvg"
ENTRY_OB = "ob"
ENTRY_PDZ = "pdz"
ENTRY_FIBO = "fibo"
ENTRY_MANUAL = "manual"

ENTRY_REASONS: tuple[str, ...] = (
    ENTRY_FIRST, ENTRY_POINT, ENTRY_PRICE, ENTRY_ATR, ENTRY_RANGE,
    ENTRY_FVG, ENTRY_OB, ENTRY_PDZ, ENTRY_FIBO, ENTRY_MANUAL,
)

ENTRY_REASON_LABELS: dict[str, str] = {
    ENTRY_FIRST: "首仓",
    ENTRY_POINT: "点数加仓",
    ENTRY_PRICE: "指定价加仓",
    ENTRY_ATR: "ATR 加仓",
    ENTRY_RANGE: "波幅加仓",
    ENTRY_FVG: "FVG 加仓",
    ENTRY_OB: "OB 加仓",
    ENTRY_PDZ: "PDZ 加仓",
    ENTRY_FIBO: "Fibo 加仓",
    ENTRY_MANUAL: "手动触发",
}

# 不依赖周期的入场原因：它们的「周期」字段配了也不读
TIMEFRAME_INDEPENDENT_REASONS: frozenset[str] = frozenset({
    ENTRY_POINT, ENTRY_PRICE,
})

# 依赖周期的原因：ATR / 波幅要读 K 线；SMC 四类靠周期匹配 TV 推送的价位
TIMEFRAME_DEPENDENT_REASONS: frozenset[str] = frozenset({
    ENTRY_ATR, ENTRY_RANGE, ENTRY_FVG, ENTRY_OB, ENTRY_PDZ, ENTRY_FIBO,
})


def normalize_entry_reason(value: object) -> str:
    """入场原因归一；认不出保留原值（小写），空值返回空串。"""
    if value is None or isinstance(value, bool):
        return ""
    text = str(value).strip().lower()
    return text


def entry_reason_label(value: object) -> str:
    """入场原因的人读标签；未知原因回落到原值本身。"""
    code = normalize_entry_reason(value)
    if not code:
        return "未标注"
    return ENTRY_REASON_LABELS.get(code, code)


def leg_timeframe(reason: object, timeframe: object) -> Optional[str]:
    """一次入场**实际生效**的周期。

    - 原因不依赖周期（point / price）→ **恒为 None**
      （配了周期也不读，硬凑一个周期会把「点数间距」混进「ATR 间距」的分桶）；
    - 原因依赖周期但周期缺失 / 非法 → None；
    - 否则返回归一后的周期。
    """
    code = normalize_entry_reason(reason)
    if code in TIMEFRAME_INDEPENDENT_REASONS:
        return None
    return normalize_timeframe(timeframe)


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------

ROUTE_PENDING = "pending"
ROUTE_AUTO = "auto"
ROUTE_ROUTED = "routed"
ROUTE_DROPPED = "dropped"

ROUTE_STATES: tuple[str, ...] = (
    ROUTE_PENDING, ROUTE_AUTO, ROUTE_ROUTED, ROUTE_DROPPED,
)

ROUTE_MODE_DIRECT = "direct"
ROUTE_MODE_CENTER = "center"

ROUTE_MODES: tuple[str, ...] = (ROUTE_MODE_DIRECT, ROUTE_MODE_CENTER)

ROUTE_STATE_LABELS: dict[str, str] = {
    ROUTE_PENDING: "待路由",
    ROUTE_AUTO: "已直连分发",
    ROUTE_ROUTED: "已中心下发",
    ROUTE_DROPPED: "已丢弃",
}

ROUTE_MODE_LABELS: dict[str, str] = {
    ROUTE_MODE_DIRECT: "直连",
    ROUTE_MODE_CENTER: "信号中心",
}

TRIGGER_TV_WEBHOOK = "tv_webhook"
TRIGGER_MANUAL = "manual"
TRIGGER_LIMIT_WATCH = "limit_watch"
TRIGGER_CENTER = "center"

TRIGGER_SOURCES: tuple[str, ...] = (
    TRIGGER_TV_WEBHOOK, TRIGGER_MANUAL, TRIGGER_LIMIT_WATCH, TRIGGER_CENTER,
)

TRIGGER_SOURCE_LABELS: dict[str, str] = {
    TRIGGER_TV_WEBHOOK: "TV 告警",
    TRIGGER_MANUAL: "手动触发",
    TRIGGER_LIMIT_WATCH: "限价监听",
    TRIGGER_CENTER: "信号中心下发",
}


def safe_route_state(value: object) -> Optional[str]:
    """路由状态收敛；非法值归 None（绝不抛异常）。"""
    if not value:
        return None
    code = str(value).strip().lower()
    return code if code in ROUTE_STATES else None


def safe_route_mode(value: object) -> Optional[str]:
    """路由模式收敛；非法值归 None。"""
    if not value:
        return None
    code = str(value).strip().lower()
    return code if code in ROUTE_MODES else None


def safe_trigger_source(value: object) -> Optional[str]:
    """触发来源收敛；非法值归 None。"""
    if not value:
        return None
    code = str(value).strip().lower()
    return code if code in TRIGGER_SOURCES else None


def safe_signal_source(value: object) -> Optional[str]:
    """分组信号来源模式收敛；非法值归 None（调用方按默认 direct 处理）。"""
    if not value:
        return None
    code = str(value).strip().lower()
    return code if code in ROUTE_MODES else None


def normalize_signal_source(value: object) -> str:
    """分组信号来源模式归一；非法 / 缺省一律回落 direct（存量分组零行为变化）。"""
    return safe_signal_source(value) or ROUTE_MODE_DIRECT


def route_state_label(value: object) -> str:
    """路由状态的人读标签。"""
    code = safe_route_state(value)
    if not code:
        return "—"
    return ROUTE_STATE_LABELS.get(code, code)


def route_mode_label(value: object) -> str:
    """路由模式的人读标签。"""
    code = safe_route_mode(value)
    if not code:
        return "—"
    return ROUTE_MODE_LABELS.get(code, code)


def trigger_source_label(value: object) -> str:
    """触发来源的人读标签。"""
    code = safe_trigger_source(value)
    if not code:
        return "—"
    return TRIGGER_SOURCE_LABELS.get(code, code)
