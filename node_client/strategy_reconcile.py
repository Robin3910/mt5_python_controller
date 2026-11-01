"""热推配置的逐字段 reconcile：把「新快照」拆成每个字段的结论。

热推不是整体替换：有的字段改了立刻生效（HOT），有的只能调大不能调小（上限类），
有的是运行期锁死不允许改（LOCKED）。这个模块负责纯逻辑的分类与结论生成，
不碰 I/O，节点执行器拿着新旧快照调它就能得到一份逐字段回执。

字段级结论状态：
- applied   —— 已采纳，下一拍生效
- frozen    —— 缩容类字段：低于已开笔数，保存前就该被拒（正常路径下不该出现）
- locked    —— 运行期锁死字段被改（正常路径下不该出现，precheck 已拦）
- no_effect —— 改了也不会产生效果（如加仓已收口时改上限）
"""
from __future__ import annotations

from typing import Any, Optional

# 上限类字段：可以调大也可以调小，但新值不能低于该规则已开笔数
LIMIT_FIELDS = ("total_lot_limit", "max_allow_num")

# 运行期锁死：规则类型、模版
LOCKED_RULE_FIELDS = ("type",)
LOCKED_MANUAL_FIELDS = (
    "entry_price",
    "take_profit",
    "volume",
    "volume_locked",
    "stop_loss",
)

# 手动分散仓自身的开关：手动单成交后整块锁死
LOCKED_MANUAL_TOGGLES = ("enabled",)

# 收口后仍然生效的字段（改单类，不产生新仓位）
NO_EFFECT_WHEN_CLOSED = (
    "point",
    "lot_times",
    "extra_lot",
    "batch_levels",
    "batch_enabled",
    "batch_action",
)

# 信号级盈亏控制（策略共享，写在两条加仓规则上）
PL_RATIO_FIELD = "float_pl_ratio"
PL_TIERS_FIELD = "lot_pl_tiers"
MONITOR_TIMES = "times"


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _rule_type(rule: dict) -> int:
    return _as_int((rule or {}).get("type"), 1)


def rule_type_label(rule_type: int) -> str:
    return "逆势加仓" if int(rule_type or 0) == 1 else "顺势加仓"


def _manual_cfg(rule: dict) -> dict:
    cfg = (rule or {}).get("manual_scatter")
    return cfg if isinstance(cfg, dict) else {}


def _changed(old: Any, new: Any) -> bool:
    """两个值是否真的不同（浮点用容差，整数/字符串直接比）。"""
    if isinstance(old, (int, float)) and isinstance(new, (int, float)):
        return abs(float(old) - float(new)) > 1e-9
    return old != new


def _manual_changed(old_rule: dict, new_rule: dict) -> bool:
    """手动分散仓区块有没有被改（含开关）。"""
    old_cfg = _manual_cfg(old_rule)
    new_cfg = _manual_cfg(new_rule)
    for key in set(old_cfg) | set(new_cfg):
        if _changed(old_cfg.get(key), new_cfg.get(key)):
            return True
    return False


def _limit_value(rule: dict, field: str) -> int:
    return _as_int((rule or {}).get(field), 0)


def _pl_cfg(rule: dict) -> dict:
    cfg = (rule or {}).get(PL_RATIO_FIELD)
    return cfg if isinstance(cfg, dict) else {}


def _pl_ratio_field(old_rule: dict, new_rule: dict) -> Optional[dict]:
    """信号盈亏比的变更结论。"""
    old_cfg = _pl_cfg(old_rule)
    new_cfg = _pl_cfg(new_rule)
    ratio_changed = _changed(old_cfg.get("ratio"), new_cfg.get("ratio"))
    old_mode = str(old_cfg.get("monitor_mode") or "loop")
    new_mode = str(new_cfg.get("monitor_mode") or "loop")
    old_times = _as_int(old_cfg.get("max_times"), 0)
    new_times = _as_int(new_cfg.get("max_times"), 0)
    times_changed = old_times != new_times

    if times_changed and new_mode == MONITOR_TIMES:
        if old_mode == MONITOR_TIMES and new_times > old_times:
            message = (
                f"信号盈亏比次数上限从 {old_times} 次调大到 {new_times} 次；"
                f"本任务已消耗的次数不复活，剩余次数按新上限顺延"
            )
        else:
            message = (
                f"信号盈亏比次数上限从 {old_times} 次调整为 {new_times} 次；"
                f"本任务已消耗的次数不复活"
            )
        return {
            "field": "rules[float_pl_ratio].max_times",
            "status": "applied",
            "old": old_times,
            "new": new_times,
            "message": message,
        }

    if ratio_changed:
        return {
            "field": "rules[float_pl_ratio].ratio",
            "status": "applied",
            "old": old_cfg.get("ratio"),
            "new": new_cfg.get("ratio"),
            "message": (
                f"信号盈亏比阈值从 {old_cfg.get('ratio')} 调整为 "
                f"{new_cfg.get('ratio')}；下一拍改单生效"
            ),
        }
    return None


def reconcile_rules(
    old_rules: list, new_rules: list,
    *, added_by_rule: Optional[dict] = None,
    closed_rules: Optional[set] = None,
    manual_locked: Optional[set] = None,
) -> tuple[list[dict], list[dict]]:
    """逐条规则、逐字段比对。

    返回 (结论列表, 生效的规则列表)：
    结论列表给人看；生效的规则列表里，锁定字段保留旧值——被锁的字段不该
    影响其它字段的生效。

    added_by_rule：各规则已开笔数（用于上限下限判定）
    closed_rules  ：加仓已收口的规则类型（手动分散仓已开）
    manual_locked ：手动分散仓**已成交**的规则类型（这类配置彻底锁死；
                   挂单在盘上（PENDING）的不锁，客户可以改参数，节点会
                   撤旧单重算重挂）
    """
    added_by_rule = added_by_rule or {}
    closed_rules = closed_rules or set()
    manual_locked = manual_locked or set()
    out: list[dict] = []

    old_list = [r for r in (old_rules or []) if isinstance(r, dict)]
    new_list = [r for r in (new_rules or []) if isinstance(r, dict)]

    # 规则按类型配对（逆势 / 顺势各一条），避免下标错位
    old_by_type = {_rule_type(r): r for r in old_list}
    new_by_type = {_rule_type(r): r for r in new_list}

    effective: list[dict] = []

    for rule_type, new_rule in new_by_type.items():
        old_rule = old_by_type.get(rule_type)
        path = f"rules[type={rule_type}]"
        label = rule_type_label(rule_type)
        applied_rule = dict(new_rule)

        if old_rule is None:
            out.append({
                "field": path,
                "status": "applied",
                "message": f"新增{label}规则",
            })
            effective.append(applied_rule)
            continue

        # 规则类型本身不允许变（配对已保证，这里防御性兜底）
        if _rule_type(old_rule) != _rule_type(new_rule):
            out.append({
                "field": f"{path}.type",
                "status": "locked",
                "message": f"{label}规则的类型不允许在运行期修改",
            })
            applied_rule["type"] = old_rule.get("type")

        # 上限类字段
        for field in LIMIT_FIELDS:
            old_v = _limit_value(old_rule, field)
            new_v = _limit_value(new_rule, field)
            if not _changed(old_v, new_v):
                continue
            added = _as_int(added_by_rule.get(rule_type), 0)
            if rule_type in closed_rules:
                out.append({
                    "field": f"{path}.{field}",
                    "status": "no_effect",
                    "old": old_v,
                    "new": new_v,
                    "message": (
                        f"{label}的手动分散仓已开仓，本任务加仓已收口，"
                        f"调整{field}不会新增批次"
                    ),
                })
                # 加仓已收口：改上限不产生效果，但保留新值（下次新建任务时生效）
            elif new_v and added and new_v < added:
                out.append({
                    "field": f"{path}.{field}",
                    "status": "frozen",
                    "old": old_v,
                    "new": new_v,
                    "message": (
                        f"{label}已开 {added} 笔，新上限 {new_v} 笔低于已开笔数，"
                        f"本任务不再加仓；已开仓位保持不动"
                    ),
                })
                # 低于已开笔数：保留旧上限，避免把任务推进非法状态
                applied_rule[field] = old_v
            else:
                extra = ""
                if new_v and added:
                    extra = f"，本任务还能再开 {max(0, new_v - added)} 笔"
                out.append({
                    "field": f"{path}.{field}",
                    "status": "applied",
                    "old": old_v,
                    "new": new_v,
                    "message": (
                        f"{label}上限已从 {old_v} 调整为 {new_v}{extra}；"
                        f"已开仓位保持不动"
                    ),
                })

        # 手动分散仓：已成交就整块锁死，保留旧配置。
        # 挂单在盘上（PENDING）不锁——客户可以改，节点会撤旧单重算重挂。
        if rule_type in manual_locked and _manual_changed(old_rule, new_rule):
            out.append({
                "field": f"{path}.manual_scatter",
                "status": "locked",
                "message": (
                    f"{label}手动分散仓已成交，配置锁定，不影响已开仓位"
                ),
            })
            applied_rule["manual_scatter"] = dict(_manual_cfg(old_rule))

        # 信号级盈亏控制：次数型监控在热推时沿用运行态已消耗的次数，
        # 这里只把「次数上限被调大」这件事说清楚，避免用户以为次数被复活了。
        pl_field = _pl_ratio_field(old_rule, new_rule)
        if pl_field is not None:
            out.append(pl_field)

        effective.append(applied_rule)

    # 旧有新无的规则被删了：锁定，保留旧规则
    for rule_type, old_rule in old_by_type.items():
        if rule_type in new_by_type:
            continue
        out.append({
            "field": f"rules[type={rule_type}]",
            "status": "locked",
            "message": (
                f"{rule_type_label(rule_type)}规则不允许在运行期删除"
            ),
        })
        effective.append(dict(old_rule))

    return out, effective


def reconcile_snapshot(
    old_snapshot: dict, new_snapshot: dict,
    *, added_by_rule: Optional[dict] = None,
    closed_rules: Optional[set] = None,
    manual_locked: Optional[set] = None,
) -> tuple[list[dict], dict]:
    """整体快照的逐字段结论。

    返回 (结论列表, 生效的快照)：生效快照里锁定 / 冻结的字段保留旧值。
    """
    old = old_snapshot if isinstance(old_snapshot, dict) else {}
    new = new_snapshot if isinstance(new_snapshot, dict) else {}
    fields, effective_rules = reconcile_rules(
        old.get("rules") or [], new.get("rules") or [],
        added_by_rule=added_by_rule,
        closed_rules=closed_rules,
        manual_locked=manual_locked,
    )

    effective = dict(new)
    effective["rules"] = effective_rules

    # 模版不允许变
    old_tid = str(old.get("template_id") or "").strip().lower()
    new_tid = str(new.get("template_id") or "").strip().lower()
    if old_tid and new_tid and old_tid != new_tid:
        fields.insert(0, {
            "field": "template_id",
            "status": "locked",
            "old": old_tid,
            "new": new_tid,
            "message": "策略模版不允许在运行期修改",
        })
        effective["template_id"] = old.get("template_id")
    return fields, effective


def summarize(fields: list[dict]) -> str:
    """整体结论：全部生效 / 部分生效 / 被拒。

    字段级 locked / frozen / no_effect 都只是「这个字段没按新值生效」，
    不影响其它字段——所以整体是 partial，不是 rejected。
    整体 rejected 只发生在版本倒退、模式切换这类运行态整体不可用的情况，
    由调用方直接给出。
    """
    items = fields or []
    if not items:
        return "applied"
    if any(f.get("status") in ("frozen", "no_effect", "locked") for f in items):
        return "partial"
    return "applied"
