"""自动路由规则：**管理员配置的持久规则**，决定信号自动下发到哪些分组。

与「信号自带的定向字段」必须分清（这是本模块存在的理由）：

- `template_ids` / `group_ids` 是 **TV 告警自带**的（谁推的告警谁指定），
  单次有效、不持久化；
- 本模块的规则是**管理员在信号中心里配置的**，对之后进来的每一条信号生效。

## 匹配语义

**过滤条件一律「空 = 不限制」**（与 `template_ids` / `group_ids` 一致）：

| 条件 | 空 = | 含义 |
|---|---|---|
| `signal_types` | 所有类型都收 | 类型可写类型码（ob）或业务分类（结构分析） |
| `timeframes` | 所有周期都收 | 归一后比较（M15 / H1 / …） |
| `directions` | 所有方向都收 | BUY / SELL |
| `group_ids` | **无效规则** | 没有目标分组的规则没有意义，保存时直接拒 |

## 两种条件形态

扁平的 `signal_types` × `timeframes` 是**笛卡尔积** —— 两个条件各自独立成立，
表达不了「M1 只收 OB，M5 只收 FVG」。所以另有一张**映射形态**：

    `type_by_timeframe` = `{周期码: [类型码, ...]}`（非空时扁平条件不参与匹配）

| map | 含义 |
|---|---|
| `{}` | 走扁平形态（存量规则） |
| `{"M1": ["ob"]}` | 只收 M1 的 OB，其它周期一律不收（**白名单**） |
| `{"M1": ["ob"], "M5": []}` | M1 只收 OB；M5 不限类型；其它周期不收 |

**空数组 ≠ 没有这个 key**：map 里**没有**某周期 = 该周期不收；
map 里**有**某周期但值是 `[]` = 该周期**不限类型**。

## 命中即占位

多条规则可能命中同一个分组（例如 R1「类型=ob → g1,g2」与 R2「周期=M15 → g1」，
一条 ob+M15 的信号会两次命中 g1）。**不去重会重复开仓**。

所以规则按 `priority` 升序处理，**一个分组被某条规则占用后，从后续规则的
候选集里移除** —— priority 小的规则先挑走分组，参数覆盖也跟随第一条命中的规则。

## 与告警自带定向的关系：取交集

告警自带 `group_ids` 与规则命中集**都是「收窄」语义**（都只缩小范围），
交集是唯一不违反任一方意图的组合：

- 告警指定 `group_ids=['g1']` + 规则命中 `g2` → 结果为空集（两个条件都要满足）；
- 告警不带定向 + 规则命中 `g2` → 结果为 `g2`。

实现上很自然：`_candidate_groups` 已经用 `group_rules.group_targeted` 做了
告警定向过滤，规则匹配加在**它之后**就是天然的交集。

## 品种维度刻意不做

`group_rules.symbol_match` 已经要求「策略绑定品种 == 信号品种」——
品种维度被**策略绑定**隐式决定了。规则再配一遍是重复配置，且必然与策略绑定
打架（规则说收 BTC、策略绑的是 XAU → 永远不命中 → 用户困惑）。
所以本模块**不提供**品种条件。
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Optional

from sqlalchemy import select

from . import signal_meta
from .db import SessionLocal
from .orm import SignalRouteRule

logger = logging.getLogger(__name__)

# 规则缓存：{only_enabled: (缓存时刻, 规则列表)}
#
# 为什么缓存而不是每次查 DB：自动路由在**每条信号**的**分发热路径**上跑，
# 规则数量级是几十条 —— 查一次 DB 不慢，但没必要每条信号都查。
# TTL 5 秒 + 写操作主动清缓存：既保证「改完规则最多 5 秒生效」，
# 又保证本进程内的写操作是**即时**生效的。
_RULES_CACHE_TTL = 5.0
_RULES_CACHE: dict[bool, tuple[float, list[dict]]] = {}


# ---------------------------------------------------------------------------
# 规则归一 / 校验
# ---------------------------------------------------------------------------

def _clean_list(value: Any) -> list[str]:
    """把任意入参收敛成「去重、保序、大小写归一」的字符串列表。"""
    if value is None:
        return []
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = [value]
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item is None or isinstance(item, bool):
            continue
        text = str(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def normalize_signal_types(value: Any) -> list[str]:
    """信号类型条件归一：类型码小写；业务分类原样保留（匹配时才展开）。

    业务分类（结构分析 / 突破信号 / …）**不能**在这里展开成类型码 ——
    展开后就分不清「用户选了结构分析」和「用户只选了 ob」，
    规则回显时用户会看到自己没配过的码。
    """
    out: list[str] = []
    seen: set[str] = set()
    for item in _clean_list(value):
        code = str(item).strip().lower()
        if not code or code in seen:
            continue
        seen.add(code)
        out.append(code)
    return out


def normalize_timeframes(value: Any) -> list[str]:
    """周期条件归一：走 `signal_meta.normalize_timeframe`（认不出 = 丢弃）。

    认不出的周期直接丢掉而不是留着 —— 留着一个永远匹配不上的值，
    用户会以为规则生效了但一条都没命中。
    """
    out: list[str] = []
    seen: set[str] = set()
    for item in _clean_list(value):
        tf = signal_meta.normalize_timeframe(item)
        if not tf or tf in seen:
            continue
        seen.add(tf)
        out.append(tf)
    return out


def normalize_type_by_timeframe(value: Any) -> dict[str, list[str]]:
    """周期 → 类型 白名单映射归一。

    返回 `{周期码: [类型码, ...]}`：
    - key：走 `normalize_timeframe`，认不出（None）→ **整个 key 丢弃**
      （留着一个永远匹配不上的周期，用户会以为配好了但一条都没命中）；
    - value：类型码小写去重保序；非列表按单元素列表处理；
    - 非 dict 入参 → 空 dict（= 走旧的扁平条件）。

    **空数组是有意义的**：它表示「该周期不限类型」（收该周期全部类型），
    与「map 里没有这个周期」（该周期不收）是两回事，不能混为一谈。
    """
    if not isinstance(value, dict):
        return {}
    out: dict[str, list[str]] = {}
    for raw_tf, raw_types in value.items():
        tf = signal_meta.normalize_timeframe(raw_tf)
        if not tf:
            continue
        types = normalize_signal_types(raw_types)
        out[tf] = types
    return out


def normalize_directions(value: Any) -> list[str]:
    """方向条件归一：统一大写，只保留 BUY / SELL。"""
    out: list[str] = []
    seen: set[str] = set()
    for item in _clean_list(value):
        code = str(item).strip().upper()
        if code not in ("BUY", "SELL") or code in seen:
            continue
        seen.add(code)
        out.append(code)
    return out


def normalize_group_ids(value: Any) -> list[str]:
    """目标分组归一：去重保序，小写。

    分组 ID 全局唯一且不含大写（`node_group.group_id` 是 String(32)），
    但历史上有过手工填的分组，所以统一小写比较。
    """
    out: list[str] = []
    seen: set[str] = set()
    for item in _clean_list(value):
        gid = str(item).strip().lower()
        if not gid or gid in seen:
            continue
        seen.add(gid)
        out.append(gid)
    return out


class RuleInputError(Exception):
    """规则入参不合法（没有目标分组 / 名称为空等）。"""


def validate_rule_payload(payload: dict) -> dict:
    """校验并归一一条规则；返回可直接落库的 dict。

    **目标分组为空 → 报错**：没有目标分组的规则没有意义，
    静默存下来只会让人以为配好了。
    """
    name = str(payload.get("name") or "").strip()
    if not name:
        raise RuleInputError("规则名称不能为空")
    if len(name) > 64:
        raise RuleInputError("规则名称过长（最多 64 字）")

    group_ids = normalize_group_ids(payload.get("group_ids"))
    if not group_ids:
        raise RuleInputError("请至少选择一个目标分组")

    # 周期 → 类型 映射（新形态）。非空时**扁平的类型 / 周期条件不再参与匹配**，
    # 这里主动清空 —— 两个语义同时存在会让用户困惑「为什么 M5 还是收 OB」。
    type_map = normalize_type_by_timeframe(payload.get("type_by_timeframe"))
    signal_types = [] if type_map else normalize_signal_types(payload.get("signal_types"))
    timeframes = [] if type_map else normalize_timeframes(payload.get("timeframes"))

    priority = payload.get("priority")
    try:
        priority = int(priority) if priority is not None else 100
    except (TypeError, ValueError):
        priority = 100

    return {
        "name": name,
        "enabled": bool(payload.get("enabled", True)),
        "signal_types": signal_types,
        "timeframes": timeframes,
        "type_by_timeframe": type_map,
        "directions": normalize_directions(payload.get("directions")),
        "group_ids": group_ids,
        # 参数覆盖：当前未启用，但结构预留（见 ORM 的注释）
        "volume": payload.get("volume"),
        "sl": payload.get("sl"),
        "tp": payload.get("tp"),
        "priority": priority,
    }


# ---------------------------------------------------------------------------
# 规则读写
# ---------------------------------------------------------------------------

def rule_row_to_dict(row: SignalRouteRule) -> dict:
    """ORM 行 -> 对外 dict（数组列统一成 list，不给 None）。"""
    def _lst(value: Any) -> list:
        return list(value) if isinstance(value, (list, tuple)) else []

    return {
        "rule_id": row.rule_id,
        "name": row.name,
        "enabled": bool(row.enabled),
        "signal_types": _lst(row.signal_types),
        "timeframes": _lst(row.timeframes),
        "type_by_timeframe": dict(row.type_by_timeframe or {}),
        "directions": _lst(row.directions),
        "group_ids": _lst(row.group_ids),
        "volume": row.volume,
        "sl": row.sl,
        "tp": row.tp,
        "priority": row.priority if row.priority is not None else 100,
        "owner_user_id": row.owner_user_id,
        "created_at": row.created_at.timestamp() if row.created_at else None,
        "updated_at": row.updated_at.timestamp() if row.updated_at else None,
    }


async def list_rules(*, only_enabled: bool = False) -> list[dict]:
    """列出规则；按 priority 升序（小的先处理）。

    这是**热路径**（每条信号都要读），所以做**进程内短 TTL 缓存** ——
    规则数量级是几十条，读一次 DB 完全够用，但没必要每条信号都查一次。

    TTL 选 5 秒：规则改完最多 5 秒生效（用户能接受），
    同时把「改完规则立刻测一条信号」的重启成本降到几乎为零。
    **写操作（create / update / delete）会主动清缓存**，所以在本进程内是即时的。
    """
    now = time.monotonic()
    cached = _RULES_CACHE.get(only_enabled)
    if cached is not None and now - cached[0] < _RULES_CACHE_TTL:
        return cached[1]

    async with SessionLocal() as s:
        stmt = select(SignalRouteRule).order_by(
            SignalRouteRule.priority.asc(),
            SignalRouteRule.created_at.asc(),
        )
        if only_enabled:
            stmt = stmt.where(SignalRouteRule.enabled.is_(True))
        rows = (await s.execute(stmt)).scalars().all()
    out = [rule_row_to_dict(r) for r in rows]
    _RULES_CACHE[only_enabled] = (now, out)
    return out


def invalidate_rules_cache() -> None:
    """清空规则缓存（写操作后调用）。"""
    _RULES_CACHE.clear()


async def get_rule(rule_id: str) -> Optional[dict]:
    async with SessionLocal() as s:
        row = (
            await s.execute(
                select(SignalRouteRule).where(SignalRouteRule.rule_id == rule_id)
            )
        ).scalar_one_or_none()
    return rule_row_to_dict(row) if row else None


async def create_rule(payload: dict, *, owner_user_id: Optional[int] = None) -> dict:
    """新建规则。"""
    data = validate_rule_payload(payload)
    rule_id = f"rr_{uuid.uuid4().hex[:16]}"
    async with SessionLocal() as s:
        row = SignalRouteRule(
            rule_id=rule_id,
            name=data["name"],
            enabled=data["enabled"],
            signal_types=data["signal_types"] or None,
            timeframes=data["timeframes"] or None,
            type_by_timeframe=data["type_by_timeframe"] or None,
            directions=data["directions"] or None,
            group_ids=data["group_ids"],
            volume=data["volume"],
            sl=data["sl"],
            tp=data["tp"],
            priority=data["priority"],
            owner_user_id=owner_user_id,
        )
        s.add(row)
        await s.commit()
        await s.refresh(row)
        result = rule_row_to_dict(row)
    invalidate_rules_cache()
    return result


async def update_rule(rule_id: str, payload: dict) -> Optional[dict]:
    """更新规则；规则不存在返回 None。

    **入参里的空列表 = 清空该条件**（= 不限制），不是「不改」——
    前端传全量字段覆盖，后端按全量语义处理，避免「删不掉一个条件」。
    """
    data = validate_rule_payload(payload)
    async with SessionLocal() as s:
        row = (
            await s.execute(
                select(SignalRouteRule).where(SignalRouteRule.rule_id == rule_id)
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        row.name = data["name"]
        row.enabled = data["enabled"]
        row.signal_types = data["signal_types"] or None
        row.timeframes = data["timeframes"] or None
        row.type_by_timeframe = data["type_by_timeframe"] or None
        row.directions = data["directions"] or None
        row.group_ids = data["group_ids"]
        row.volume = data["volume"]
        row.sl = data["sl"]
        row.tp = data["tp"]
        row.priority = data["priority"]
        await s.commit()
        await s.refresh(row)
        result = rule_row_to_dict(row)
    invalidate_rules_cache()
    return result


async def delete_rule(rule_id: str) -> bool:
    """删除规则；返回是否真的删掉了。"""
    async with SessionLocal() as s:
        row = (
            await s.execute(
                select(SignalRouteRule).where(SignalRouteRule.rule_id == rule_id)
            )
        ).scalar_one_or_none()
        if row is None:
            return False
        await s.delete(row)
        await s.commit()
    invalidate_rules_cache()
    return True


# ---------------------------------------------------------------------------
# 匹配
# ---------------------------------------------------------------------------

def _expand_types(codes: list[str]) -> set[str]:
    """把规则里的类型条件展开成「可匹配的类型码集合」。

    业务分类（结构分析 / 突破信号 / …）展开成它包含的类型码；
    未登记的码按原样保留（遵循「未知类型保留原值」原则）。
    """
    out: set[str] = set()
    for code in codes:
        expanded = signal_meta.types_in_group(code)
        if expanded:
            out |= set(expanded)
        else:
            out.add(code)
    return out


def _match_type_map(type_map: dict[str, list[str]], signal: dict) -> bool:
    """周期 → 类型 白名单映射的匹配。

    **白名单语义**：
    - map 里**没有**这个周期 → 该周期**不收**（不是「不限类型」）；
    - map 里**有**这个周期但值是空数组 → 该周期**不限类型**（收该周期的全部类型）；
    - 值非空 → 信号类型必须落在其中（类型缺失 = 不命中）。

    这两种「没有类型限制」的区别是白名单的核心，混淆会让用户配出反直觉的规则。
    """
    tf = signal_meta.normalize_timeframe(signal.get("timeframe"))
    if not tf or tf not in type_map:
        # 周期不在白名单里 → 不命中（含信号周期缺失的情况）
        return False
    allowed = type_map.get(tf) or []
    if not allowed:
        # 该周期不限类型 → 类型条件不再收窄
        return True
    st = signal_meta.normalize_signal_type(signal.get("signal_type"))
    # 信号类型缺失（未标注）→ 不命中：规则明确限定了类型，
    # 而这条信号的类型是「没有」而不是「匹配」。
    return bool(st) and st in _expand_types(allowed)


def rule_matches(rule: dict, signal: dict) -> bool:
    """规则是否命中这条信号。

    `signal` 是归一后的信号维度：
    `{"signal_type": ..., "timeframe": ..., "action": ...}`。

    空条件 = 不限制（与 template_ids / group_ids 的语义一致）。

    **两种条件形态**：`type_by_timeframe` 非空时走映射形态
    （周期 → 类型白名单），此时扁平的 `signal_types` / `timeframes`
    不再参与匹配；否则走旧的扁平形态。
    """
    type_map = rule.get("type_by_timeframe") or {}
    if type_map:
        if not _match_type_map(type_map, signal):
            return False
    else:
        types = rule.get("signal_types") or []
        if types:
            st = signal_meta.normalize_signal_type(signal.get("signal_type"))
            # 信号类型缺失（未标注）→ 不命中：规则明确限定了类型，
            # 而这条信号的类型是「没有」而不是「匹配」。
            if not st or st not in _expand_types(types):
                return False

        tfs = rule.get("timeframes") or []
        if tfs:
            tf = signal_meta.normalize_timeframe(signal.get("timeframe"))
            if not tf or tf not in tfs:
                return False

    dirs = rule.get("directions") or []
    if dirs:
        action = str(signal.get("action") or "").strip().upper()
        if action not in dirs:
            return False

    return True


def match_groups(
    rules: list[dict], signal: dict, candidate_group_ids: set[str],
) -> tuple[list[str], dict[str, str]]:
    """按规则挑出「这个信号要发给哪些 auto 分组」。

    返回 `(命中的分组 ID 列表, 分组 -> 命中它的规则 ID)`。

    **命中即占位**：一个分组被某条规则占用后，从后续规则的候选集里移除，
    所以同一个分组不会被下发两次。

    **按 priority 升序处理**（小的先挑走分组）：排序在这里做而不是依赖调用方，
    这样纯函数在任何调用路径下语义都一致 —— 传进来的顺序不影响结果。

    `candidate_group_ids` 是**准入已通过**的 auto 分组集合
    （`_candidate_groups` 已经做完启用状态 / 策略 / 品种 / 模版校验）——
    规则只回答「要不要它」，不回答「它能不能」。
    """
    hit: list[str] = []
    owner: dict[str, str] = {}
    remaining = {gid for gid in candidate_group_ids}

    # 按优先级升序（数值小的先挑走分组）；同优先级时保持传入顺序（稳定排序）
    ordered = sorted(
        rules,
        key=lambda r: (int(r.get("priority") or 100), ),
    )
    for rule in ordered:
        if not remaining:
            break
        if not rule.get("enabled", True):
            continue
        if not rule_matches(rule, signal):
            continue
        for gid in rule.get("group_ids") or []:
            if gid not in remaining:
                continue
            remaining.discard(gid)
            hit.append(gid)
            owner[gid] = rule.get("rule_id", "")
    return hit, owner
