"""热推配置的逐字段 reconcile：把「新快照」拆成每个字段的结论。

热推不是整体替换，而是**逐字段采纳 + 逐字段说明**：
- 字段值一律采纳用户填的新值（配置无条件热推，保存路径上没有拦截）；
- 「加不加 / 怎么加」由节点自身状态决定，结论写在每条结论的 `exec` 里。

这个模块负责纯逻辑的分类与结论生成，不碰 I/O，节点执行器拿着新旧快照
调它就能得到一份逐字段回执。

字段级结论状态（**采纳与否**）：
- applied   —— 已采纳，下一拍生效
- no_effect —— 已采纳，但节点当前状态决定它不再产生新的动作
- locked    —— 已采纳为新值，但该字段被运行期状态锁死（手动仓已成交）

字段级结论里的 `exec`（**节点会不会执行**）：
- will_add         —— 节点会按此继续加仓
- no_more_adds     —— 本规则不再新增批次（已加 ≥ 上限，或该规则已收口）
- waiting_tp_sl    —— 本规则不再加仓，现有仓位等待止盈止损
- locked_by_manual —— 该规则手动分散仓已成交，节点不执行这部分
"""
from __future__ import annotations

from typing import Any, Optional

# 上限类字段：可以调大也可以调小，任何值都允许保存并热推
LIMIT_FIELDS = ("total_lot_limit", "max_allow_num")

# 节点执行结论（写在每条字段结论的 exec 里）
EXEC_WILL_ADD = "will_add"
EXEC_NO_MORE_ADDS = "no_more_adds"
EXEC_WAITING_TP_SL = "waiting_tp_sl"
EXEC_LOCKED_BY_MANUAL = "locked_by_manual"

# 运行期锁死：指定价档位已成交，它的入场价 / 自有止损 / 止盈价
# 已经落在已开的仓位上，改配置不改已开仓位。
EXEC_LOCKED_BY_PRICE_LEVEL = "locked_by_price_level"

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


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _rule_type(rule: dict) -> int:
    return _as_int((rule or {}).get("type"), 1)


def rule_type_label(rule_type: int) -> str:
    return "逆势加仓" if int(rule_type or 0) == 1 else "顺势加仓"


def _manual_cfg(rule: dict) -> dict:
    cfg = (rule or {}).get("manual_scatter")
    return cfg if isinstance(cfg, dict) else {}


def _manual_enabled(rule: dict) -> bool:
    """这条规则的手动分散仓当前是否「有且启用」。

    「没有 manual_scatter 整块」与「有但 enabled=false」对节点来说是同一件事：
    这条规则不再有手动单。所以判断口径统一成「配置里有没有生效的手动单」。
    """
    cfg = _manual_cfg(rule)
    if not cfg:
        return False
    return bool(cfg.get("enabled"))


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


def _pl_tiers_cfg(rule: dict) -> dict:
    cfg = (rule or {}).get(PL_TIERS_FIELD)
    return cfg if isinstance(cfg, dict) else {}


def _pl_ratio_fields(old_rule: dict, new_rule: dict) -> list[dict]:
    """信号盈亏比的变更结论（逐字段）。

    早期版本在这里提前 return：同时改开关 + 比例 + 次数时只报一条，
    客户看不到另外两个字段的结论。这里改成返回列表，逐字段都给说法。
    """
    old_cfg = _pl_cfg(old_rule)
    new_cfg = _pl_cfg(new_rule)
    out: list[dict] = []

    old_enabled = bool(old_cfg.get("enabled"))
    new_enabled = bool(new_cfg.get("enabled"))
    if old_enabled != new_enabled:
        if new_enabled:
            out.append({
                "field": f"rules[{PL_RATIO_FIELD}].enabled",
                "status": "applied",
                "old": old_enabled,
                "new": new_enabled,
                "message": "信号盈亏比已启用；下一拍按新比例重算共同止损",
            })
        else:
            out.append({
                "field": f"rules[{PL_RATIO_FIELD}].enabled",
                "status": "applied",
                "old": old_enabled,
                "new": new_enabled,
                "message": (
                    "信号盈亏比已关闭；已有持仓上的止损价保持不变（不会自动摘除）"
                ),
            })

    old_mode = str(old_cfg.get("monitor_mode") or "loop")
    new_mode = str(new_cfg.get("monitor_mode") or "loop")
    old_times = _as_int(old_cfg.get("max_times"), 0)
    new_times = _as_int(new_cfg.get("max_times"), 0)
    times_changed = old_times != new_times

    if times_changed:
        if new_mode == MONITOR_TIMES:
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
        else:
            message = (
                f"信号盈亏比次数上限从 {old_times} 次调整为 {new_times} 次"
            )
        out.append({
            "field": f"rules[{PL_RATIO_FIELD}].max_times",
            "status": "applied",
            "old": old_times,
            "new": new_times,
            "message": message,
        })

    old_ratio = old_cfg.get("ratio")
    new_ratio = new_cfg.get("ratio")
    if _changed(old_ratio, new_ratio):
        out.append({
            "field": f"rules[{PL_RATIO_FIELD}].ratio",
            "status": "applied",
            "old": old_ratio,
            "new": new_ratio,
            "message": (
                f"信号盈亏比阈值从 {old_ratio} 调整为 {new_ratio}；"
                f"下一拍按新比例重算共同止损"
            ),
        })
    return out


def _pl_tiers_fields(old_rule: dict, new_rule: dict) -> list[dict]:
    """信号分档手数盈亏的变更结论（逐字段）。

    批次 1 的盈亏金额是**真正生效**的那个；批次 2+ 当前节点不参与共同止盈
    计算，这里如实标注，免得客户以为改了就能用。
    """
    old_cfg = _pl_tiers_cfg(old_rule)
    new_cfg = _pl_tiers_cfg(new_rule)
    out: list[dict] = []

    old_enabled = bool(old_cfg.get("enabled"))
    new_enabled = bool(new_cfg.get("enabled"))
    if old_enabled != new_enabled:
        if new_enabled:
            out.append({
                "field": f"rules[{PL_TIERS_FIELD}].enabled",
                "status": "applied",
                "old": old_enabled,
                "new": new_enabled,
                "message": "分档手数盈亏已启用；下一拍按批次1金额重算共同止盈",
            })
        else:
            out.append({
                "field": f"rules[{PL_TIERS_FIELD}].enabled",
                "status": "applied",
                "old": old_enabled,
                "new": new_enabled,
                "message": (
                    "分档手数盈亏已关闭；已有持仓上的止盈价保持不变（不会自动摘除）"
                ),
            })

    old_action = str(old_cfg.get("close_action") or "all")
    new_action = str(new_cfg.get("close_action") or "all")
    if _changed(old_action, new_action):
        out.append({
            "field": f"rules[{PL_TIERS_FIELD}].close_action",
            "status": "applied",
            "old": old_action,
            "new": new_action,
            "message": (
                f"分档手数盈亏的平仓方向从 {old_action} 调整为 {new_action}；"
                f"下一拍按新方向改写止盈"
            ),
        })

    old_tiers = [t for t in (old_cfg.get("tiers") or []) if isinstance(t, dict)]
    new_tiers = [t for t in (new_cfg.get("tiers") or []) if isinstance(t, dict)]
    for idx, new_tier in enumerate(new_tiers):
        old_tier = old_tiers[idx] if idx < len(old_tiers) else {}
        if _changed(old_tier.get("min_lot"), new_tier.get("min_lot")):
            out.append({
                "field": f"rules[{PL_TIERS_FIELD}].tiers[{idx}].min_lot",
                "status": "applied",
                "old": old_tier.get("min_lot"),
                "new": new_tier.get("min_lot"),
                "message": (
                    f"批次{idx + 1}的总手数门槛从 {old_tier.get('min_lot')} 调整为 "
                    f"{new_tier.get('min_lot')}；影响该方向达到多少手数才写止盈"
                ),
            })
        if _changed(old_tier.get("pl_amount"), new_tier.get("pl_amount")):
            message = (
                f"批次{idx + 1}的盈亏金额从 {old_tier.get('pl_amount')} 调整为 "
                f"{new_tier.get('pl_amount')}"
            )
            if idx == 0:
                message += (
                    "；这是实际生效的共同止盈金额，下一拍重算止盈价；"
                    "在途的手动分散仓挂单会按新金额重算手数后重挂"
                )
            else:
                message += "；当前版本只有批次1参与共同止盈计算，该批次暂不生效"
            out.append({
                "field": f"rules[{PL_TIERS_FIELD}].tiers[{idx}].pl_amount",
                "status": "applied",
                "old": old_tier.get("pl_amount"),
                "new": new_tier.get("pl_amount"),
                "message": message,
            })
    return out


def _price_level_cfg(rule: dict) -> list[dict]:
    """规则里的指定价档位（calc_type=price 且填了入场价）。"""
    levels = (rule or {}).get("batch_levels")
    out: list[dict] = []
    for idx, lv in enumerate(levels or []):
        if not isinstance(lv, dict):
            continue
        if str(lv.get("calc_type") or "").strip().lower() != "price":
            continue
        if _as_float(lv.get("price")) <= 0:
            continue
        out.append({"index": idx, "level": lv})
    return out


def _price_level_fields(
    old_rule: dict, new_rule: dict,
    *, price_locked: set, path: str, label: str,
) -> list[dict]:
    """指定价档位的变更结论（逐档位、逐字段）。

    两种结论：
    - **在途未成交** → `applied`：节点会撤掉旧挂单、按新参数重挂；
    - **已成交** → `locked`：这一档的入场价 / 自有止损 / 止盈价都已
      锁定在已开的仓位上，改配置不影响已开仓位（也不该改）。

    `price_locked` 是「已成交的指定价档位」集合：(规则类型, 档位下标)。
    """
    out: list[dict] = []
    old_levels = {item["index"]: item["level"] for item in _price_level_cfg(old_rule)}
    new_levels = {item["index"]: item["level"] for item in _price_level_cfg(new_rule)}
    rule_type = _rule_type(new_rule)
    for idx in sorted(set(old_levels) | set(new_levels)):
        old_lv = old_levels.get(idx, {})
        new_lv = new_levels.get(idx, {})
        locked = (rule_type, idx) in price_locked
        for field, text in (
            ("price", "指定价"),
            ("price_tp", "止盈价"),
            ("price_sl", "自有止损"),
        ):
            old_v = _as_float(old_lv.get(field))
            new_v = _as_float(new_lv.get(field))
            if not _changed(old_v, new_v):
                continue
            field_path = f"{path}.batch_levels[{idx}].{field}"
            if locked:
                out.append({
                    "field": field_path,
                    "status": "locked",
                    "exec": EXEC_LOCKED_BY_PRICE_LEVEL,
                    "old": old_v,
                    "new": new_v,
                    "message": (
                        f"{label}档位#{idx + 1}的{text}已随成交锁定在已开仓位上"
                        f"（{old_v}），本次调整（{new_v}）不影响已开仓位"
                    ),
                })
            else:
                out.append({
                    "field": field_path,
                    "status": "applied",
                    "old": old_v,
                    "new": new_v,
                    "message": (
                        f"{label}档位#{idx + 1}的{text}从 {old_v} 调整为 {new_v}；"
                        "节点会撤掉盘上的挂单并按新参数重挂"
                    ),
                })
    return out


def reconcile_rules(
    old_rules: list, new_rules: list,
    *, added_by_rule: Optional[dict] = None,
    closed_rules: Optional[set] = None,
    manual_locked: Optional[set] = None,
    manual_pending: Optional[set] = None,
    price_locked: Optional[set] = None,
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
    manual_pending：手动分散仓**挂单在盘上**的规则类型。客户在这期间把手动单
                   删掉时，节点会撤掉盘上那张挂单——这里给出逐字段结论，
                   让客户知道「保存后那张单会被撤掉」。
    price_locked  ：**已成交**的指定价档位（(规则类型, 档位下标) 集合）。
                   这些档位的入场价 / 自有止损 / 止盈价已锁定在已开仓位上，
                   改配置不影响已开仓位；在途未成交的不锁（可改 → 撤单重挂）。
    """
    added_by_rule = added_by_rule or {}
    closed_rules = closed_rules or set()
    manual_locked = manual_locked or set()
    manual_pending = manual_pending or set()
    price_locked = price_locked or set()
    out: list[dict] = []

    old_list = [r for r in (old_rules or []) if isinstance(r, dict)]
    new_list = [r for r in (new_rules or []) if isinstance(r, dict)]

    # 规则按类型配对（逆势 / 顺势各一条），避免下标错位
    old_by_type = {_rule_type(r): r for r in old_list}
    new_by_type = {_rule_type(r): r for r in new_list}

    effective: list[dict] = []
    # 盈亏配置在逆势 / 顺势两条规则上是同一份，字段级结论只报一次
    seen_pl: set[str] = set()

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
        #
        # 配置无条件热推：**字段值一律采纳用户填的新值**，不再保留旧上限。
        # 「还加不加」由节点自己的已开笔数决定——判定侧现成的
        # `rule_adds >= limit` 就会自然停止；本分支只负责把结论说清楚。
        for field in LIMIT_FIELDS:
            old_v = _limit_value(old_rule, field)
            new_v = _limit_value(new_rule, field)
            if not _changed(old_v, new_v):
                continue
            added = _as_int(added_by_rule.get(rule_type), 0)
            applied_rule[field] = new_v
            if rule_type in closed_rules:
                out.append({
                    "field": f"{path}.{field}",
                    "status": "no_effect",
                    "exec": EXEC_NO_MORE_ADDS,
                    "old": old_v,
                    "new": new_v,
                    "message": (
                        f"{label}的手动分散仓已开仓，本任务加仓已收口，"
                        f"调整{field}不会新增批次"
                    ),
                })
            elif new_v <= 0:
                # 0 = 不限
                out.append({
                    "field": f"{path}.{field}",
                    "status": "applied",
                    "exec": EXEC_WILL_ADD,
                    "old": old_v,
                    "new": new_v,
                    "message": (
                        f"{label}上限已从 {old_v} 调整为不限（0）；"
                        f"本任务按规则继续加仓"
                    ),
                })
            elif new_v > added:
                out.append({
                    "field": f"{path}.{field}",
                    "status": "applied",
                    "exec": EXEC_WILL_ADD,
                    "old": old_v,
                    "new": new_v,
                    "message": (
                        f"{label}上限已从 {old_v} 调整为 {new_v}，"
                        f"本任务已加 {added} 笔，还能再开 {new_v - added} 笔"
                    ),
                })
            else:
                # 新上限 ≤ 已开笔数：配置照常采纳，但节点不会再新增批次。
                # 已开仓位保持不动，继续由止盈止损管理。
                out.append({
                    "field": f"{path}.{field}",
                    "status": "no_effect",
                    "exec": EXEC_NO_MORE_ADDS,
                    "old": old_v,
                    "new": new_v,
                    "message": (
                        f"{label}上限 {new_v} 笔 ≤ 已开 {added} 笔，"
                        f"本任务不再新增批次；现有 {added} 笔继续由止盈止损管理"
                    ),
                })

        # 手动分散仓：已成交就整块锁死，保留旧配置。
        # 挂单在盘上（PENDING）不锁——客户可以改，节点会撤旧单重算重挂。
        if rule_type in manual_locked and _manual_changed(old_rule, new_rule):
            out.append({
                "field": f"{path}.manual_scatter",
                "status": "locked",
                "exec": EXEC_LOCKED_BY_MANUAL,
                "message": (
                    f"{label}手动分散仓已成交，配置已保存并热推，"
                    f"但节点不再执行这部分改动；已开仓位不受影响"
                ),
            })
            applied_rule["manual_scatter"] = dict(_manual_cfg(old_rule))
        # 手动分散仓被删掉 / 关掉，而挂单还在盘上：节点会撤掉那张单。
        # 这里必须给出结论——否则回执说「全部生效」，客户完全不知道
        # 盘上那张挂单会被撤（动作是不可逆的，得让人看见）。
        elif (
            rule_type in manual_pending
            and _manual_enabled(old_rule)
            and not _manual_enabled(new_rule)
        ):
            out.append({
                "field": f"{path}.manual_scatter",
                "status": "applied",
                "message": (
                    f"{label}手动分散仓配置已删除：节点会撤掉盘上的挂单，"
                    "本次不再入场；如需继续请重新填写入场价与止盈价"
                ),
            })

        # 信号级盈亏控制：次数型监控在热推时沿用运行态已消耗的次数，
        # 这里只把「次数上限被调大」这件事说清楚，避免用户以为次数被复活了。
        # 盈亏配置写在逆势 / 顺势两条规则上的是同一份，去重后只报一次。
        pl_fields = _pl_ratio_fields(old_rule, new_rule)
        pl_fields.extend(_pl_tiers_fields(old_rule, new_rule))
        for pl_field in pl_fields:
            key = str(pl_field.get("field") or "")
            if key in seen_pl:
                continue
            seen_pl.add(key)
            out.append(pl_field)

        # 指定价档位：在途未成交的可改（撤单重挂），已成交的锁定。
        price_fields = _price_level_fields(
            old_rule, new_rule,
            price_locked=price_locked, path=path, label=label,
        )
        if price_fields:
            out.extend(price_fields)
            # 锁定的档位：生效配置里保留旧值（已开仓位不受影响）
            locked_idx = {
                idx for (rt, idx) in price_locked if rt == rule_type
            }
            if locked_idx:
                old_by_idx = {
                    item["index"]: item["level"] for item in _price_level_cfg(old_rule)
                }
                levels = list(applied_rule.get("batch_levels") or [])
                for idx in locked_idx:
                    if idx >= len(levels) or idx not in old_by_idx:
                        continue
                    old_lv = dict(old_by_idx[idx])
                    new_lv = dict(levels[idx])
                    for field in ("price", "price_tp", "price_sl"):
                        if field in old_lv:
                            new_lv[field] = old_lv[field]
                    levels[idx] = new_lv
                applied_rule["batch_levels"] = levels

        effective.append(applied_rule)

    # 旧有新无的规则被删了：**采纳删除**。
    # 该规则不再参与加仓判定，节点会把它的在途逆势限价单撤掉；
    # 已开仓位照旧由止盈止损管理，**不平仓**。
    for rule_type, old_rule in old_by_type.items():
        if rule_type in new_by_type:
            continue
        added = _as_int(added_by_rule.get(rule_type), 0)
        out.append({
            "field": f"rules[type={rule_type}]",
            "status": "applied",
            "exec": EXEC_WAITING_TP_SL if added else EXEC_NO_MORE_ADDS,
            "message": (
                f"{rule_type_label(rule_type)}规则已移除，不再参与加仓判定；"
                f"已开 {added} 笔仓位继续由止盈止损管理，不平仓"
            ),
        })

    return out, effective


def reconcile_snapshot(
    old_snapshot: dict, new_snapshot: dict,
    *, added_by_rule: Optional[dict] = None,
    closed_rules: Optional[set] = None,
    manual_locked: Optional[set] = None,
    manual_pending: Optional[set] = None,
    price_locked: Optional[set] = None,
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
        manual_pending=manual_pending,
        price_locked=price_locked,
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

    字段级 no_effect / locked 都只是「这个字段按新值采纳了，但节点当前状态
    决定它不再产生动作」，不影响其它字段——所以整体是 partial，不是 rejected。
    整体 rejected 只发生在版本倒退、模式切换这类运行态整体不可用的情况，
    由调用方直接给出。
    """
    items = fields or []
    if not items:
        return "applied"
    if any(f.get("status") in ("no_effect", "locked") for f in items):
        return "partial"
    return "applied"
