"""策略保存前的运行态校验（纯逻辑，可单测）。

前端会做一遍同样的校验（体验优化），但前端校验不可信——请求可以被绕过、
数据也可能来自离线节点。后端在 PATCH 落库前必须独立再校验一次。

目前只有一条硬约束：**上限类字段不能低于该规则已开笔数**。
上限可以调大（扩容），也可以调小（缩减剩余额度），但低于已开笔数会让节点
进入「上限 < 已加」的非法状态，并使手动分散仓的门槛被误解锁。
"""
from __future__ import annotations

from typing import Optional

# 上限类字段（按规则独立计数）
LIMIT_FIELDS = ("total_lot_limit", "max_allow_num")

RULE_TYPE_COUNTER = 1
RULE_TYPE_TREND = 2


def _as_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def rule_type_label(rule_type: int) -> str:
    return "逆势加仓" if int(rule_type or 0) == RULE_TYPE_COUNTER else "顺势加仓"


def _limit_value(rule: dict, field: str) -> int:
    """上限值；0 表示不限制（不参与下限比较）。"""
    return _as_int((rule or {}).get(field), 0)


def check_limit_below_added(
    rules: list, *, max_added_by_rule: Optional[dict] = None,
) -> list[dict]:
    """检查每条规则的上限是否低于该规则已开笔数。

    返回阻断项列表，每项：
    {"field", "code", "rule_type", "rule_type_label", "added", "limit", "message"}

    max_added_by_rule 的键是规则类型（字符串或整数都认）。
    """
    max_added_by_rule = max_added_by_rule or {}
    # 键可能是字符串（JSON 落库）或整数
    normalized: dict[int, int] = {}
    for key, value in max_added_by_rule.items():
        rule_type = _as_int(key, 0)
        n = _as_int(value, 0)
        if rule_type in (RULE_TYPE_COUNTER, RULE_TYPE_TREND) and n > 0:
            normalized[rule_type] = max(normalized.get(rule_type, 0), n)

    blocks: list[dict] = []
    for rule in rules or []:
        if not isinstance(rule, dict):
            continue
        rule_type = _as_int(rule.get("type"), RULE_TYPE_COUNTER)
        added = normalized.get(rule_type, 0)
        if added <= 0:
            continue
        for field in LIMIT_FIELDS:
            limit = _limit_value(rule, field)
            if limit <= 0:
                # 0 = 不限制，不参与下限比较
                continue
            if limit >= added:
                continue
            label = rule_type_label(rule_type)
            blocks.append({
                "field": f"rules[type={rule_type}].{field}",
                "code": "limit_below_added",
                "rule_type": rule_type,
                "rule_type_label": label,
                "added": added,
                "limit": limit,
                "message": (
                    f"{label}已开 {added} 笔，新上限 {limit} 笔低于已开笔数。"
                    f"上限只能缩减剩余额度，不能低于已开笔数，请填写 ≥ {added} 的值。"
                ),
            })
    return blocks


def summarize_blocks(blocks: list[dict]) -> str:
    """把阻断项拼成一句可读的提示。"""
    items = blocks or []
    if not items:
        return ""
    return "；".join(str(b.get("message") or "") for b in items)
