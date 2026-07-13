"""纯分发规则——不含任何 I/O，可单独做单元测试。

对应技术方案文档第 9 章的决策逻辑：
- 手数计算（全局 / 固定 / 信号）          -> 9.4
- 持仓过滤（按品种 / 按账户）             -> 9.3（与参考仓库一致）
- 多区间方向过滤                          -> 9.2
"""
from __future__ import annotations

import re
from typing import Optional

from .config import Config
from .settings import settings


def base_symbol(s: str) -> str:
    """去掉券商后缀/标点，便于品种比较（如 XAUUSD.m -> XAUUSD）。"""
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def symbol_match(a: str, b: str) -> bool:
    """判断两个品种是否“同一品种”，兼容券商后缀差异。"""
    ba, bb = base_symbol(a), base_symbol(b)
    if not ba or not bb:
        return False
    # 完全相等，或一方是另一方的前缀（覆盖 XAUUSD 与 XAUUSD.m 这类情况）
    return ba == bb or ba.startswith(bb) or bb.startswith(ba)


def _node_symbol_rule(node: dict, symbol: str) -> Optional[dict]:
    """读取节点 filters 中某品种的配置。"""
    node_filters = node.get("filters") or {}
    if isinstance(node_filters, dict):
        return _lookup_symbol_config(node_filters, symbol)
    return None


def node_has_symbol_config(node: dict, symbol: str) -> bool:
    """节点 filters 中是否已为该品种配置按币种条目（非空 dict 且能匹配到 symbol）。"""
    node_filters = node.get("filters") or {}
    if not isinstance(node_filters, dict) or not node_filters:
        return False
    return _lookup_symbol_config(node_filters, symbol) is not None


def node_symbol_not_configured_reason(symbol: str) -> str:
    """节点未配置该品种时的拒收说明（落库 skip_reason / 节点信号日志）。"""
    sym = base_symbol(symbol) or symbol
    return f"节点未配置：{sym}未在节点按币种配置中，拒收"


def resolve_volume(node: dict, signal_volume: float, global_filters: dict, symbol: str) -> float:
    """9.4——按节点该品种的手数策略决定实际手数，并以 MAX_LOT_SIZE 封顶。"""
    sf = _node_symbol_rule(node, symbol)
    mode = (sf.get("lot_mode") if sf else None) or node.get("lot_mode", "global")
    fixed_lot = sf.get("lot") if sf and sf.get("lot") is not None else node.get("lot")
    if mode == "fixed" and fixed_lot is not None:
        vol = float(fixed_lot)
    elif mode == "global":
        gf = _lookup_symbol_config(global_filters or {}, symbol)
        if gf and gf.get("lot_enabled"):
            vol = float(gf.get("lot", Config.DEFAULT_LOT))
        else:
            vol = float(signal_volume)
    else:
        vol = float(signal_volume)
    return min(max(vol, 0.0), Config.MAX_LOT_SIZE)


def node_poll_order(node: dict, symbol: str) -> int:
    """节点在某品种轮询分发中的顺序（越小越先）。"""
    sf = _node_symbol_rule(node, symbol)
    if sf and sf.get("poll_order") is not None:
        try:
            return int(sf["poll_order"])
        except (TypeError, ValueError):
            pass
    return int(node.get("poll_order", 0))


def position_gate(
    action: str,
    allow_position: bool,
    positions: list[dict],
    symbol: str,
    scope: str = "symbol",
) -> tuple[bool, Optional[str]]:
    """9.3——持仓过滤，返回 (是否放行, 拦截原因)。

    规则：
    - CLOSE 永远放行（平仓不受持仓限制）；
    - allow_position=True 时强制放行（覆盖过滤）；
    - scope=symbol：仅当“同品种”无持仓才放行；
    - scope=account：账户存在任意持仓即拦截。

    被拦截时返回的 reason 是一句中文说明，明确写出是哪条持仓规则
    （按品种 / 按账户）以及当前持仓数，直接展示在后台「跳过原因」。
    """
    if action not in ("BUY", "SELL"):
        return True, None
    if allow_position:
        return True, None
    held = positions or []
    if scope == "symbol":
        held = [p for p in held if symbol_match(p.get("symbol", ""), symbol)]
    if len(held) == 0:
        return True, None
    if scope == "symbol":
        return False, f"持仓过滤：已持有同品种{symbol}仓位({len(held)}笔)，按品种过滤跳过"
    return False, f"持仓过滤：账户已有持仓({len(held)}笔)，按账户过滤跳过"


def _lookup_symbol_config(filters_cfg: dict, symbol: str) -> Optional[dict]:
    """按品种 / base_symbol / 券商后缀兼容查找配置项。"""
    if not filters_cfg or not symbol:
        return None
    sf = filters_cfg.get(symbol) or filters_cfg.get(base_symbol(symbol))
    if isinstance(sf, dict):
        return sf
    for key, rule in filters_cfg.items():
        if isinstance(rule, dict) and symbol_match(str(key), symbol):
            return rule
    return None


def resolve_dispatch_config(
    symbol: str, global_filters: dict,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """按币种解析分发模式与持仓判定范围。

    返回 (mode, scope, reject_reason)。reject_reason 非空时表示应拒收信号
   （未登记，或中控台已取消「启用」——含开仓与 Webhook 平仓；手动平仓不受影响）。
    """
    sf = _lookup_symbol_config(global_filters or {}, symbol)
    sym = base_symbol(symbol) or symbol
    if not sf:
        return None, None, f"品种未配置：{sym}未在中控台配置，信号拒收"
    if sf.get("enabled", True) is False:
        return None, None, f"品种已禁用：{sym}在中控台未启用，信号拒收"
    mode = sf.get("dispatch_mode", settings.dispatch_mode)
    scope = sf.get("position_scope", settings.position_scope)
    if mode not in ("sync", "poll"):
        mode = "sync"
    if scope not in ("symbol", "account"):
        scope = "symbol"
    return mode, scope, None


def node_participates(node: dict, symbol: str, mode: str, global_filters: dict) -> bool:
    """节点是否参与该币种在指定分发模式下的分发（读节点 filters 中的 per-symbol 开关）。"""
    node_filters = node.get("filters") or {}
    sf = _lookup_symbol_config(node_filters, symbol) if isinstance(node_filters, dict) else None
    if mode == "sync":
        return sf.get("follow_sync", True) if sf else True
    if mode == "poll":
        return sf.get("follow_poll", True) if sf else True
    return True


def poll_participant_ids(nodes: list[dict], symbol: str, global_filters: dict) -> list[str]:
    """9.6 轮询轮转的“参与节点”静态资格集合，返回按轮转初始顺序排好的 node_id 列表。

    资格 = 已启用 + 已配置该品种（按币种条目）+ 参与 poll（follow_poll）。
    排序 = poll_order 升序，其次 created_at 升序（与队列初始顺序一致）。

    注意：不含在线判断——在线是动态状态，由 worker 在“领取”时对队首实时判定，
    离线节点仍保留在轮转顺序中以维持其轮转位置。
    """
    parts = [
        n for n in nodes
        if n.get("enabled", True)
        and node_has_symbol_config(n, symbol)
        and node_participates(n, symbol, "poll", global_filters)
    ]
    parts.sort(key=lambda n: (node_poll_order(n, symbol), n.get("created_at", 0)))
    return [n["node_id"] for n in parts]


def effective_filters(node: dict, global_filters: dict) -> dict:
    """9.2——节点级过滤覆盖全局；同品种字段级合并，节点未配置的品种回退全局规则。"""
    merged: dict = {}
    for sym, rule in (global_filters or {}).items():
        if isinstance(rule, dict):
            merged[sym] = dict(rule)
    node_filters = node.get("filters") or {}
    if isinstance(node_filters, dict):
        for sym, nr in node_filters.items():
            if not isinstance(nr, dict):
                continue
            key = str(sym).strip().upper()
            if not key:
                continue
            base = merged.get(key) or merged.get(base_symbol(key)) or {}
            if not isinstance(base, dict):
                base = {}
            merged[key] = {**base, **nr}
    return merged


def pick_price(account: Optional[dict], symbol: str) -> Optional[float]:
    """从节点最近一次账户快照里，取该品种“尽量准确”的参考价。

    优先级：实时报价表 prices > 该品种已有持仓的 price_current；都没有则返回 None。
    """
    if not account:
        return None
    prices = account.get("prices") or {}
    for k, v in prices.items():
        if symbol_match(k, symbol):
            return float(v)
    # 兜底：用该品种持仓的当前价
    for p in account.get("positions") or []:
        if symbol_match(p.get("symbol", ""), symbol) and p.get("price_current"):
            return float(p["price_current"])
    return None


def _fmt_num(x: object) -> str:
    """把价格/区间端点格式化为紧凑字符串（去掉多余的 0，且不使用科学计数法）。"""
    try:
        f = float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return str(x)
    s = f"{f:.6f}".rstrip("0").rstrip(".")
    return s or "0"


def interval_filter(
    action: str,
    symbol: str,
    price: Optional[float],
    filters_cfg: dict,
) -> tuple[bool, Optional[str]]:
    """9.2——多区间方向过滤，返回 (是否放行, 拦截原因)。CLOSE 不过滤。

    逻辑：
    - 该品种未配置 -> 放行（准入拒收由 resolve_dispatch_config 负责；
      中控台取消「启用」亦在该处拒收，含 CLOSE）；
    - 无可用价格 -> 放行（避免在拿不到价时误拦截）；
    - 命中某区间：方向在该区间 allow 列表内则放行，否则拦截；
    - 不在任何区间：按 default_action（block 拦截 / pass 放行）。

    被拦截时返回的 reason 是一句可直接展示给运营的中文说明，明确写出
    “不符合哪条规则”（命中的区间、该区间允许的方向、或默认动作），
    会落库到分发明细的 skip_reason 并显示在后台「跳过原因」。
    """
    if action not in ("BUY", "SELL"):
        return True, None
    sf = _lookup_symbol_config(filters_cfg, symbol)
    if not sf:
        return True, None
    if action == "BUY" and sf.get("allow_buy", True) is False:
        return False, f"方向总开关：该品种已禁止接收做多(BUY)信号"
    if action == "SELL" and sf.get("allow_sell", True) is False:
        return False, f"方向总开关：该品种已禁止接收做空(SELL)信号"
    if price is None:
        return True, None
    for iv in sf.get("intervals", []):
        if float(iv["low"]) <= price <= float(iv["high"]):
            allow = [a.upper() for a in iv.get("allow", [])]
            if action in allow:
                return True, None
            allowed_txt = "/".join(allow) if allow else "无"
            return False, (
                f"区间方向过滤：价格{_fmt_num(price)}命中区间"
                f"[{_fmt_num(iv['low'])},{_fmt_num(iv['high'])}]，"
                f"该区间仅允许{allowed_txt}，{action}被拦截"
            )
    # 落在所有配置区间之外
    if sf.get("default_action", "block") == "pass":
        return True, None
    return False, f"区间默认过滤：价格{_fmt_num(price)}不在任何配置区间内，默认动作拦截(block)"


def validate_node_global_lot_mode(node_filters: dict, global_filters: dict) -> Optional[str]:
    """节点 filters 中 lot_mode=global 时，中控台对应品种须已启用全局手数。"""
    if not node_filters or not isinstance(node_filters, dict):
        return None
    for sym, rule in node_filters.items():
        if not isinstance(rule, dict) or rule.get("lot_mode") != "global":
            continue
        key = base_symbol(str(sym)) or str(sym).strip().upper()
        if not key:
            continue
        gf = _lookup_symbol_config(global_filters or {}, key)
        if not gf or not gf.get("lot_enabled"):
            return (
                f"{key}：手数策略为「跟随中控台」，但中控台该品种未启用全局手数，无法保存"
            )
    return None
