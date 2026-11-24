"""AI 策略（tpl_1）开仓准入闸门。

**定位**：中控台在 `config:filters` 的每个品种条目下有一块独立的 `ai_entry`
子块，用来限定「AI 智能加仓策略（`tpl_1`）分组」**首单开仓信号**的准入：

- 方向不对（该品种不允许做多 / 做空）→ 丢弃；
- 判定价不在任何允许区间内 → 丢弃；
- **取不到判定价** → 直接拦截（拿不到价就放行 = 这份配置形同虚设）。

**与 normal 链路的关系**：normal 链路（`dispatcher.py`）本来就有一套
「区间 + 允许方向」过滤，读的是同一份 `config:filters` 里的普通 `intervals`；
strategy 链路（含 tpl_1）被显式设计成**不读**那份配置。本模块补上的就是
strategy 链路缺失的这一层，且：

- 区间裁决**复用** `rules.interval_decision` —— 两条链路对「命中哪个区间 /
  边界是否闭合 / 区间外默认动作」的口径是同一份实现，不会日后各改各的漂移；
- `ai_entry` 与普通字段**互不影响**：普通 `intervals` 继续只作用 normal 链路。

**不做什么**（明确写进注释，防止后人误加）：

- 不拦 CLOSE —— 平仓走 `_dispatch_close` 的独立路径，天然不经过这里；
- 不终止已在跑的任务 —— 本闸门只作用于「新信号 → 新建主任务」；
- 不覆盖 tpl_1 的**加仓** —— 加仓发生在节点进程内，须下沉节点侧，
  在中控台做会形成「两套价格、两个真相」；
- 不拦 tpl_2 / tpl_3 —— 调用方需先检查 `strategy.template_id == "tpl_1"`。

本模块**不含交易动作**：不写 DB、不发指令、不抢并发锁。
"""
from __future__ import annotations

import logging
from typing import Optional

from . import group_rules, rules

logger = logging.getLogger(__name__)

# AI 智能加仓策略模版 ID（tpl_1）。只有绑定该模版的分组才走本闸门。
AI_TEMPLATE_ID = "tpl_1"

# 拦截原因统一前缀 —— 让运营在信号中心一眼看出「这是 AI 开仓准入拦的」。
REASON_PREFIX = "AI 开仓准入"

# 合法方向
_DIRECTIONS = ("BUY", "SELL")


def default_ai_entry() -> dict:
    """未配置时的默认结构：**不启用**（存量配置零行为变化）。"""
    return {
        "enabled": False,
        "allow_buy": True,
        "allow_sell": True,
        "default_action": "block",
        "intervals": [],
    }


def _normalize_interval(raw: object) -> Optional[dict]:
    """归一化单条区间；非法项返回 None（调用方丢弃该条，不影响其它区间）。"""
    if not isinstance(raw, dict):
        return None
    try:
        low = float(raw.get("low"))
        high = float(raw.get("high"))
    except (TypeError, ValueError):
        return None
    if low != low or high != high:  # NaN
        return None
    if low >= high:
        return None
    allow_raw = raw.get("allow")
    if not isinstance(allow_raw, (list, tuple)):
        return None
    allow = []
    for a in allow_raw:
        up = str(a or "").strip().upper()
        if up in _DIRECTIONS and up not in allow:
            allow.append(up)
    if not allow:
        return None
    return {"low": low, "high": high, "allow": allow}


def normalize_ai_entry(raw: object) -> dict:
    """把中控台 `ai_entry` 子块归一化成判定可直接使用的结构。

    归一化放在**服务端**（不依赖前端）—— 这份配置直接决定真实交易。

    容错口径：

    - `ai_entry` 缺失 / 非字典 → 未启用（放行）；
    - `enabled` 缺失 → `False`；
    - `default_action` 非法 → `block`（保守）；
    - `allow_buy` / `allow_sell` 缺失 → `True`；
    - 单条区间非法（`low >= high` / 非数字 / `allow` 为空）→ **该条被丢弃**，
      不影响其它区间，也不让整个配置失效。
    """
    cfg = default_ai_entry()
    if not isinstance(raw, dict):
        return cfg
    cfg["enabled"] = raw.get("enabled") is True
    cfg["allow_buy"] = raw.get("allow_buy") is not False
    cfg["allow_sell"] = raw.get("allow_sell") is not False
    cfg["default_action"] = "pass" if raw.get("default_action") == "pass" else "block"
    intervals_raw = raw.get("intervals")
    intervals: list[dict] = []
    if isinstance(intervals_raw, (list, tuple)):
        for iv in intervals_raw:
            norm = _normalize_interval(iv)
            if norm is not None:
                intervals.append(norm)
    cfg["intervals"] = intervals
    return cfg


def lookup_ai_entry(filters_cfg: Optional[dict], symbol: str) -> dict:
    """从中控台 filters 里取出该品种的 `ai_entry` 子块（未配置 → 未启用）。"""
    if not filters_cfg or not symbol:
        return default_ai_entry()
    sf = rules.lookup_symbol_config(filters_cfg, symbol)
    if not isinstance(sf, dict):
        return default_ai_entry()
    return normalize_ai_entry(sf.get("ai_entry"))


def evaluate(
    action: str,
    symbol: str,
    price: Optional[float],
    ai_entry_cfg: Optional[dict],
) -> tuple[bool, Optional[str]]:
    """AI 开仓准入判定，返回 (是否放行, 拦截原因)。

    判定顺序（短路）：

    1. 未启用 → 放行；
    2. action 不是 BUY/SELL（CLOSE）→ 放行；
    3. 方向总开关不允许本次方向 → 拦截；
    4. **无判定价 → 拦截**（拿不到价就无法判定区间）；
    5. 命中某区间但该区间不允许本次方向 → 拦截（文案写明命中区间）；
    6. 不在任何区间 → `default_action` = `block` 拦截 / `pass` 放行。

    注意第 4 步**只在启用时才会走到** —— 不启用时缺价完全不影响任何信号，
    否则会让所有没配价的上报信号被误杀。
    """
    cfg = ai_entry_cfg if isinstance(ai_entry_cfg, dict) else default_ai_entry()
    if not cfg.get("enabled"):
        return True, None
    if action not in _DIRECTIONS:
        return True, None
    if action == "BUY" and cfg.get("allow_buy") is False:
        return False, f"{REASON_PREFIX}：方向总开关：该品种已禁止接收做多(BUY)信号"
    if action == "SELL" and cfg.get("allow_sell") is False:
        return False, f"{REASON_PREFIX}：方向总开关：该品种已禁止接收做空(SELL)信号"
    if price is None:
        sym = rules.base_symbol(symbol) or symbol
        return False, (
            f"{REASON_PREFIX}：{sym}无可用价格（节点未上报报价且无持仓价），"
            f"无法判定区间，{action}被拦截"
        )
    ok, reason = rules.interval_decision(
        action, price, cfg.get("intervals") or [], cfg.get("default_action", "block"),
        reason_prefix=REASON_PREFIX,
    )
    if ok:
        return True, None
    return False, reason


def evaluate_with_config(
    action: str,
    symbol: str,
    price: Optional[float],
    filters_cfg: Optional[dict],
) -> tuple[bool, Optional[str]]:
    """便捷入口：直接从中控台 filters 里取该品种的 `ai_entry` 再判定。"""
    return evaluate(action, symbol, price, lookup_ai_entry(filters_cfg, symbol))


def resolve_price(
    signal: object,
    group: dict,
    nodemap: dict,
    accounts: dict,
    online_ids: Optional[set] = None,
) -> tuple[Optional[float], str]:
    """取该分组的**参考判定价**，返回 (价格, 来源说明)。

    取价优先级：

    1. **信号自带价** —— `entry_price` / `limit_price`（> 0 才算）。
       零 IO、最准（TradingView 告警带价时最准）。
    2. **分组 owner 成员的账户快照报价** —— 优先**主节点**（`is_primary`，
       报价最有代表性）；其次第一个**在线** owner 成员；再次第一个启用成员。
       跟单成员的报价**不参与**（跟单员的券商报价不代表带单方的市场）。
    3. 都取不到 → `(None, "无可用价格")` → 上层按「缺价直接拦截」处理。

    `accounts` 是 `node_id -> 账户快照` 的映射（由调用方批量读取，避免 N 次 IO）。
    `online_ids` 是当前在线节点集合（在线状态在 Redis / 连接管理器里，不在节点字典里）。
    """
    price = _signal_price(signal)
    if price is not None:
        return price, "信号自带价"

    symbol = signal_symbol(signal)
    online = set(online_ids or ())
    for node_id, desc in _reference_nodes(group, nodemap, online):
        account = (accounts or {}).get(node_id)
        if not account:
            continue
        p = rules.pick_price(account, symbol)
        if p is not None:
            return float(p), f"{desc}报价快照"
    return None, "无可用价格"


def signal_symbol(signal: object) -> str:
    """取信号品种（兼容对象 / dict 两种形态）。"""
    if isinstance(signal, dict):
        return str(signal.get("symbol") or "")
    return str(getattr(signal, "symbol", "") or "")


def _signal_price(signal: object) -> Optional[float]:
    """取信号自带的入场价 / 限价（<= 0 视为没有）。"""
    if isinstance(signal, dict):
        raw = signal.get("entry_price")
        if raw is None:
            raw = signal.get("limit_price")
    else:
        raw = getattr(signal, "entry_price", None)
        if raw is None:
            raw = getattr(signal, "limit_price", None)
    try:
        if raw is None:
            return None
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value != value or value <= 0:  # NaN 或非正
        return None
    return value


def reference_node_ids(
    group: dict, nodemap: dict, online_ids: Optional[set] = None,
) -> list[tuple[str, str]]:
    """该分组的参考报价节点（有序）—— 对外暴露，供调用方批量取账户快照。

    顺序：主节点 → 第一个在线 owner 成员 → 其余启用的 owner 成员。
    只看 owner 成员，不看跟单成员。
    """
    return _reference_nodes(group, nodemap, online_ids)


def _reference_nodes(
    group: dict, nodemap: dict, online_ids: Optional[set] = None,
) -> list[tuple[str, str]]:
    """该分组的参考报价节点（有序），返回 [(node_id, 人类可读描述)]。

    顺序：主节点 → 第一个在线 owner 成员 → 其余启用的 owner 成员。
    只看 owner 成员，不看跟单成员。
    """
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    online = set(online_ids or ())
    members = group_rules.member_ids(group or {})

    def _add(node_id: str, desc: str) -> None:
        if node_id and node_id not in seen:
            seen.add(node_id)
            out.append((node_id, desc))

    # 1) 主节点（带单源身份，报价最有代表性）
    for node_id in members:
        node = nodemap.get(node_id)
        if node and node.get("is_primary"):
            _add(node_id, "主节点")
            break

    # 2) 第一个在线的 owner 成员
    for node_id in members:
        node = nodemap.get(node_id)
        if node and node.get("enabled", True) and node_id in online:
            _add(node_id, "在线成员节点")
            break

    # 3) 其余启用的 owner 成员（兜底）
    for node_id in members:
        node = nodemap.get(node_id)
        if node and node.get("enabled", True):
            _add(node_id, "成员节点")
            break

    return out


def validate_ai_entry_config(filters_cfg: Optional[dict]) -> Optional[str]:
    """校验中控台 filters 里的 `ai_entry` 子块，返回错误文案（合法则 None）。

    这份配置直接决定真实交易，**不能只靠前端兜底** —— 服务端必须拒收非法值。

    校验范围（只管 `ai_entry`，不扩大到普通 `intervals`，避免扩大改动面）：

    - `ai_entry` 必须是对象；
    - `default_action` 只能是 `block` / `pass`；
    - 每条区间：`low` / `high` 必须是有限数且 `low < high`；
      `allow` 必须是数组、取值只能是 `BUY` / `SELL`、且不能为空。
    """
    if not isinstance(filters_cfg, dict):
        return None
    for symbol, rule in filters_cfg.items():
        if not isinstance(rule, dict):
            continue
        raw = rule.get("ai_entry")
        if raw is None:
            continue
        key = str(symbol).strip().upper() or "?"
        if not isinstance(raw, dict):
            return f"{key}：AI 开仓准入配置格式非法（应为对象），无法保存"
        da = raw.get("default_action", "block")
        if da not in ("block", "pass"):
            return (
                f"{key}：AI 开仓准入的默认动作非法"
                f"（应为 block / pass，当前为 {da}），无法保存"
            )
        intervals = raw.get("intervals")
        if intervals is None:
            continue
        if not isinstance(intervals, (list, tuple)):
            return f"{key}：AI 开仓准入的区间列表格式非法，无法保存"
        for idx, iv in enumerate(intervals):
            label = f"{key} AI 开仓准入 第 {idx + 1} 条区间"
            if not isinstance(iv, dict):
                return f"{label}：格式非法（应为对象），无法保存"
            try:
                low = float(iv.get("low"))
                high = float(iv.get("high"))
            except (TypeError, ValueError):
                return f"{label}：价格上下限必须为有效数字"
            if low != low or high != high or low == float("inf") or high == float("inf"):
                return f"{label}：价格上下限必须为有效数字"
            if low >= high:
                return f"{label}：价格下限必须小于上限"
            allow = iv.get("allow")
            if not isinstance(allow, (list, tuple)) or not allow:
                return f"{label}：请至少勾选一个允许方向（做多/做空）"
            for a in allow:
                if str(a or "").strip().upper() not in _DIRECTIONS:
                    return f"{label}：允许方向非法（只能是做多/做空）"
    return None
