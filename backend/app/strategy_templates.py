"""策略模版定义（当前仅内置「策略模版1」）。

模版本身不可通过 API 新建；新建策略时选择模版，会把模版默认规则复制到策略实例，
之后可按实例独立调整（不影响模版）。

规则字段说明（每条规则）：
- type：1=逆势加仓，2=顺势加仓
- status：0=关闭，1=启用
- action：监控方向 all | buy | sell（默认 all）
- point：以监控方向最近订单为基准，价格偏离达到 point * Point() 后触发
- lot_times：加仓倍率（默认 1）
- extra_lot：额外手数（默认 0）
- max_allow_num：最大允许加仓次数
实际加仓手数 = lot_times * 基础订单手数 + extra_lot
"""
from __future__ import annotations

from copy import deepcopy
from typing import Optional

# 规则 type
RULE_TYPE_COUNTER = 1  # 逆势加仓
RULE_TYPE_TREND = 2    # 顺势加仓

RULE_ACTIONS = ("all", "buy", "sell")

TEMPLATE_1_ID = "tpl_1"
TEMPLATE_1_NAME = "策略模版1"


def _default_rule(rule_type: int) -> dict:
    return {
        "type": rule_type,
        "status": 1,
        "action": "all",
        "point": 100,
        "lot_times": 1.0,
        "extra_lot": 0.0,
        "max_allow_num": 5,
    }


# 模版注册表：后续新增模版只需往这里追加
STRATEGY_TEMPLATES: dict[str, dict] = {
    TEMPLATE_1_ID: {
        "template_id": TEMPLATE_1_ID,
        "name": TEMPLATE_1_NAME,
        "description": (
            "含逆势加仓与顺势加仓两条规则："
            "按监控方向取最近订单为基准，价格偏离达到 point×Point() 后加仓；"
            "实际手数 = lot_times × 基础手数 + extra_lot。"
        ),
        "rules": [
            _default_rule(RULE_TYPE_COUNTER),
            _default_rule(RULE_TYPE_TREND),
        ],
    },
}


def list_templates() -> list[dict]:
    """返回所有可用策略模版（含默认规则副本）。"""
    return [deepcopy(t) for t in STRATEGY_TEMPLATES.values()]


def get_template(template_id: str) -> Optional[dict]:
    t = STRATEGY_TEMPLATES.get(template_id)
    return deepcopy(t) if t else None


def normalize_rule(raw: dict) -> dict:
    """规范化单条规则；非法字段回落默认值。"""
    try:
        rule_type = int(raw.get("type", RULE_TYPE_COUNTER))
    except (TypeError, ValueError):
        rule_type = RULE_TYPE_COUNTER
    if rule_type not in (RULE_TYPE_COUNTER, RULE_TYPE_TREND):
        rule_type = RULE_TYPE_COUNTER

    try:
        status = 1 if int(raw.get("status", 1)) else 0
    except (TypeError, ValueError):
        status = 1

    action = str(raw.get("action") or "all").strip().lower()
    if action not in RULE_ACTIONS:
        action = "all"

    def _num(key: str, default: float) -> float:
        try:
            return float(raw.get(key, default))
        except (TypeError, ValueError):
            return default

    try:
        max_allow = int(raw.get("max_allow_num", 5))
    except (TypeError, ValueError):
        max_allow = 5
    max_allow = max(0, max_allow)

    return {
        "type": rule_type,
        "status": status,
        "action": action,
        "point": max(0.0, _num("point", 100)),
        "lot_times": max(0.0, _num("lot_times", 1.0)),
        "extra_lot": max(0.0, _num("extra_lot", 0.0)),
        "max_allow_num": max_allow,
    }


def normalize_rules(rules: object) -> list[dict]:
    if not isinstance(rules, list):
        return []
    return [normalize_rule(r) for r in rules if isinstance(r, dict)]
