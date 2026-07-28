"""策略模版定义（当前仅内置「策略模版1」）。

模版本身不可通过 API 新建；新建策略时选择模版，会把模版默认规则复制到策略实例，
之后可按实例独立调整（不影响模版）。

结构说明：
- BatchLevel：分批加仓档位（持仓笔数区间内的点数 / 倍数）
- CounterTrendRule：逆势加仓（独立模型）
- TrendFollowRule：顺势加仓（独立模型）
- TemplateRuleSet：模版规则集，逆势 / 顺势分字段存放

对外序列化仍为 rules 列表（带 type），兼容现有 API / 落库格式。
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

# 规则 type
RULE_TYPE_COUNTER = 1  # 逆势加仓
RULE_TYPE_TREND = 2    # 顺势加仓

RULE_ACTIONS = ("all", "buy", "sell")
BATCH_CALC_TYPES = ("point",)  # 点数；预留扩展

TEMPLATE_1_ID = "tpl_1"
TEMPLATE_1_NAME = "策略模版1"


# ---------------------------------------------------------------------------
# 结构模型
# ---------------------------------------------------------------------------

@dataclass
class BatchLevel:
    """分批加仓档位：在持仓笔数 [pos_from, pos_to] 内使用本组参数。"""
    pos_from: int = 2
    pos_to: int = 4
    calc_type: str = "point"  # 点数
    point: float = 100.0
    lot_times: float = 1.1
    extra_lot: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CounterTrendRule:
    """逆势加仓规则（独立模型，对齐 MTcommander「逆势」）。"""
    status: int = 1
    action: str = "all"
    point: float = 100.0
    lot_times: float = 1.1
    extra_lot: float = 0.0
    max_allow_num: int = 3
    # 分批加仓（对齐「逆势分批加」）
    batch_enabled: bool = True
    batch_action: str = "all"  # 分批独立监控方向
    batch_count: int = 3
    total_lot_limit: float = 10.0
    batch_levels: list[BatchLevel] = field(default_factory=list)

    @property
    def type(self) -> int:
        return RULE_TYPE_COUNTER

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "status": self.status,
            "action": self.action,
            "point": self.point,
            "lot_times": self.lot_times,
            "extra_lot": self.extra_lot,
            "max_allow_num": self.max_allow_num,
            "batch_enabled": self.batch_enabled,
            "batch_action": self.batch_action,
            "batch_count": self.batch_count,
            "total_lot_limit": self.total_lot_limit,
            "batch_levels": [lv.to_dict() for lv in self.batch_levels],
        }


@dataclass
class TrendFollowRule:
    """顺势加仓规则（独立模型，对齐 MTcommander「顺势」）。"""
    status: int = 1
    action: str = "all"
    point: float = 100.0
    lot_times: float = 0.8
    extra_lot: float = 0.0
    max_allow_num: int = 10
    # 分批加仓（结构与逆势对齐，截图未启用则默认关闭）
    batch_enabled: bool = False
    batch_action: str = "all"
    batch_count: int = 0
    total_lot_limit: float = 0.0
    batch_levels: list[BatchLevel] = field(default_factory=list)

    @property
    def type(self) -> int:
        return RULE_TYPE_TREND

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "status": self.status,
            "action": self.action,
            "point": self.point,
            "lot_times": self.lot_times,
            "extra_lot": self.extra_lot,
            "max_allow_num": self.max_allow_num,
            "batch_enabled": self.batch_enabled,
            "batch_action": self.batch_action,
            "batch_count": self.batch_count,
            "total_lot_limit": self.total_lot_limit,
            "batch_levels": [lv.to_dict() for lv in self.batch_levels],
        }


@dataclass
class TemplateRuleSet:
    """模版规则集：逆势 / 顺势独立存放。"""
    counter: CounterTrendRule
    trend: TrendFollowRule

    def to_rules(self) -> list[dict[str, Any]]:
        """序列化为 API / 落库用的 rules 列表。"""
        return [self.counter.to_dict(), self.trend.to_dict()]


def _default_counter_batch_levels() -> list[BatchLevel]:
    """MTcommander 风格默认分批档位。"""
    return [
        BatchLevel(pos_from=2, pos_to=4, calc_type="point", point=100.0, lot_times=1.1, extra_lot=0.0),
        BatchLevel(pos_from=5, pos_to=7, calc_type="point", point=200.0, lot_times=1.2, extra_lot=0.0),
        BatchLevel(pos_from=8, pos_to=10, calc_type="point", point=300.0, lot_times=1.3, extra_lot=0.0),
    ]


def default_counter_rule() -> CounterTrendRule:
    """策略模版1 · 逆势默认参数（对齐 MTcommander 截图）。"""
    return CounterTrendRule(
        status=1,
        action="all",
        point=100.0,
        lot_times=1.1,
        extra_lot=0.0,
        max_allow_num=3,
        batch_enabled=True,
        batch_action="all",
        batch_count=3,
        total_lot_limit=10.0,
        batch_levels=_default_counter_batch_levels(),
    )


def default_trend_rule() -> TrendFollowRule:
    """策略模版1 · 顺势默认参数（对齐 MTcommander 截图）。"""
    return TrendFollowRule(
        status=1,
        action="all",
        point=100.0,
        lot_times=0.8,
        extra_lot=0.0,
        max_allow_num=10,
        batch_enabled=False,
        batch_action="all",
        batch_count=0,
        total_lot_limit=0.0,
        batch_levels=[],
    )


def default_template_1_rules() -> TemplateRuleSet:
    return TemplateRuleSet(
        counter=default_counter_rule(),
        trend=default_trend_rule(),
    )


# ---------------------------------------------------------------------------
# 模版注册表
# ---------------------------------------------------------------------------

_TPL1_RULES = default_template_1_rules()

STRATEGY_TEMPLATES: dict[str, dict] = {
    TEMPLATE_1_ID: {
        "template_id": TEMPLATE_1_ID,
        "name": TEMPLATE_1_NAME,
        "description": (
            "含逆势加仓与顺势加仓两条独立规则："
            "逆势支持分批加仓档位（点数 / 倍数随持仓加深）；"
            "实际手数 = lot_times × 基础手数 + extra_lot。"
        ),
        # 模版内部按独立模型存放；对外仍暴露 rules 列表
        "rule_set": _TPL1_RULES,
        "rules": _TPL1_RULES.to_rules(),
    },
}


def list_templates() -> list[dict]:
    """返回所有可用策略模版（含默认规则副本）。"""
    out: list[dict] = []
    for t in STRATEGY_TEMPLATES.values():
        item = {
            "template_id": t["template_id"],
            "name": t["name"],
            "description": t.get("description", ""),
            "rules": deepcopy(t["rules"]),
        }
        out.append(item)
    return out


def get_template(template_id: str) -> Optional[dict]:
    t = STRATEGY_TEMPLATES.get(template_id)
    if not t:
        return None
    return {
        "template_id": t["template_id"],
        "name": t["name"],
        "description": t.get("description", ""),
        "rules": deepcopy(t["rules"]),
    }


# ---------------------------------------------------------------------------
# 规范化
# ---------------------------------------------------------------------------

def _as_int(value: object, default: int) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _as_float(value: object, default: float) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def normalize_batch_level(raw: dict) -> dict[str, Any]:
    calc_type = str(raw.get("calc_type") or "point").strip().lower()
    if calc_type not in BATCH_CALC_TYPES:
        calc_type = "point"
    pos_from = max(0, _as_int(raw.get("pos_from", 1), 1))
    pos_to = max(pos_from, _as_int(raw.get("pos_to", pos_from), pos_from))
    return {
        "pos_from": pos_from,
        "pos_to": pos_to,
        "calc_type": calc_type,
        "point": max(0.0, _as_float(raw.get("point", 100), 100.0)),
        "lot_times": max(0.0, _as_float(raw.get("lot_times", 1.0), 1.0)),
        "extra_lot": max(0.0, _as_float(raw.get("extra_lot", 0.0), 0.0)),
    }


def _normalize_batch_levels(raw: object) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    return [normalize_batch_level(x) for x in raw if isinstance(x, dict)]


def normalize_rule(raw: dict) -> dict[str, Any]:
    """规范化单条规则；非法字段回落默认值。按 type 走对应独立模型默认。"""
    try:
        rule_type = int(raw.get("type", RULE_TYPE_COUNTER))
    except (TypeError, ValueError):
        rule_type = RULE_TYPE_COUNTER
    if rule_type not in (RULE_TYPE_COUNTER, RULE_TYPE_TREND):
        rule_type = RULE_TYPE_COUNTER

    defaults = (
        default_counter_rule().to_dict()
        if rule_type == RULE_TYPE_COUNTER
        else default_trend_rule().to_dict()
    )

    try:
        status = 1 if int(raw.get("status", defaults["status"])) else 0
    except (TypeError, ValueError):
        status = int(defaults["status"])

    action = str(raw.get("action") or defaults["action"]).strip().lower()
    if action not in RULE_ACTIONS:
        action = defaults["action"]

    batch_action = str(raw.get("batch_action") or defaults.get("batch_action") or action).strip().lower()
    if batch_action not in RULE_ACTIONS:
        batch_action = defaults.get("batch_action") or "all"

    batch_enabled = bool(raw.get("batch_enabled", defaults["batch_enabled"]))
    batch_count = max(0, _as_int(raw.get("batch_count", defaults["batch_count"]), defaults["batch_count"]))
    total_lot_limit = max(
        0.0,
        _as_float(raw.get("total_lot_limit", defaults["total_lot_limit"]), defaults["total_lot_limit"]),
    )
    batch_levels = _normalize_batch_levels(raw.get("batch_levels", defaults["batch_levels"]))

    return {
        "type": rule_type,
        "status": status,
        "action": action,
        "point": max(0.0, _as_float(raw.get("point", defaults["point"]), defaults["point"])),
        "lot_times": max(0.0, _as_float(raw.get("lot_times", defaults["lot_times"]), defaults["lot_times"])),
        "extra_lot": max(0.0, _as_float(raw.get("extra_lot", defaults["extra_lot"]), defaults["extra_lot"])),
        "max_allow_num": max(0, _as_int(raw.get("max_allow_num", defaults["max_allow_num"]), defaults["max_allow_num"])),
        "batch_enabled": batch_enabled,
        "batch_action": batch_action,
        "batch_count": batch_count,
        "total_lot_limit": total_lot_limit,
        "batch_levels": batch_levels,
    }


def normalize_rules(rules: object) -> list[dict[str, Any]]:
    if not isinstance(rules, list):
        return []
    return [normalize_rule(r) for r in rules if isinstance(r, dict)]


def rules_to_rule_set(rules: object) -> TemplateRuleSet:
    """从 rules 列表还原为逆势 / 顺势独立模型。缺失的一侧用默认填充。"""
    counter = default_counter_rule()
    trend = default_trend_rule()
    if isinstance(rules, list):
        for raw in rules:
            if not isinstance(raw, dict):
                continue
            normalized = normalize_rule(raw)
            levels = [
                BatchLevel(
                    pos_from=lv["pos_from"],
                    pos_to=lv["pos_to"],
                    calc_type=lv["calc_type"],
                    point=lv["point"],
                    lot_times=lv["lot_times"],
                    extra_lot=lv["extra_lot"],
                )
                for lv in normalized["batch_levels"]
            ]
            if normalized["type"] == RULE_TYPE_COUNTER:
                counter = CounterTrendRule(
                    status=normalized["status"],
                    action=normalized["action"],
                    point=normalized["point"],
                    lot_times=normalized["lot_times"],
                    extra_lot=normalized["extra_lot"],
                    max_allow_num=normalized["max_allow_num"],
                    batch_enabled=normalized["batch_enabled"],
                    batch_action=normalized["batch_action"],
                    batch_count=normalized["batch_count"],
                    total_lot_limit=normalized["total_lot_limit"],
                    batch_levels=levels,
                )
            elif normalized["type"] == RULE_TYPE_TREND:
                trend = TrendFollowRule(
                    status=normalized["status"],
                    action=normalized["action"],
                    point=normalized["point"],
                    lot_times=normalized["lot_times"],
                    extra_lot=normalized["extra_lot"],
                    max_allow_num=normalized["max_allow_num"],
                    batch_enabled=normalized["batch_enabled"],
                    batch_action=normalized["batch_action"],
                    batch_count=normalized["batch_count"],
                    total_lot_limit=normalized["total_lot_limit"],
                    batch_levels=levels,
                )
    return TemplateRuleSet(counter=counter, trend=trend)
