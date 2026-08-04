"""strategy 分组分发的纯规则——不含任何 I/O，可单独做单元测试。

与 `rules.py`（normal 链路，按币种配置）完全隔离：

- 不读中控台 `config:filters`，不做币种准入 / 多区间方向过滤 / 持仓过滤；
- 不读节点按币种配置（follow_sync / follow_poll / lot_mode / poll_order）；
- 分发模式来自分组自身（sync / poll），与币种无关；
- 手数直接采用信号手数（仅做单笔上限保护）。

判定“有效节点”的口径固定为：节点已启用 且 当前在线。

并发控制的粒度在**节点**上：真正的执行单元是「节点子任务」，各自持有独立魔术号；
分组主任务只汇总一条信号在该分组内的分发情况，不参与互斥判定。
"""
from __future__ import annotations

from typing import Callable, Optional

from . import strategy_templates
from .config import Config
from .models import GROUP_DISPATCH_MODES, SIGNAL_MODEL_NORMAL, SIGNAL_MODELS


def normalize_signal_model(value: object) -> Optional[str]:
    """规范化 Webhook 的 model 字段。

    空 / 缺失 -> normal（保持当前项目默认行为）；normal / strategy 原样返回（大小写不敏感）；
    其它取值返回 None，表示是非法枚举值，应由调用方拒收。
    """
    if value is None:
        return SIGNAL_MODEL_NORMAL
    text = str(value).strip().lower()
    if not text:
        return SIGNAL_MODEL_NORMAL
    return text if text in SIGNAL_MODELS else None


def normalize_dispatch_mode(value: object) -> str:
    """规范化分组分发模式；非法值回落到 sync。"""
    mode = str(value or "").strip().lower()
    return mode if mode in GROUP_DISPATCH_MODES else "sync"


def resolve_volume(signal_volume: float) -> float:
    """分组链路的手数：直接用信号手数，仅做单笔上限保护。"""
    try:
        vol = float(signal_volume)
    except (TypeError, ValueError):
        vol = 0.0
    return min(max(vol, 0.0), Config.MAX_LOT_SIZE)


def normalize_symbol_key(symbol: object) -> str:
    """品种归一化：去掉非字母数字并大写，便于跨券商比较。"""
    text = str(symbol or "").upper()
    return "".join(ch for ch in text if ch.isalnum())


def symbol_match(strategy_symbol: object, signal_symbol: object) -> bool:
    """策略绑定品种与信号品种是否同一标的。

    不同券商对同一标的会加后缀（XAUUSD / XAUUSDm / XAUUSD.pro），因此归一化后
    只要互为前缀即视为匹配，与节点侧 resolve_symbol 的宽松口径保持一致。
    """
    a = normalize_symbol_key(strategy_symbol)
    b = normalize_symbol_key(signal_symbol)
    if not a or not b:
        return False
    return a.startswith(b) or b.startswith(a)


def subtask_magic(dispatch_id: int) -> int:
    """子任务号 -> MT5 魔术号（基数 + 子任务号）。

    魔术号绑在「节点子任务」而不是分组主任务上：每个节点独立维护自己的执行单元，
    所以一个魔术号全局唯一地对应一次节点执行，可由 MT5 订单直接反查到子任务。
    """
    return Config.NODE_TASK_MAGIC_BASE + int(dispatch_id)


def subtask_id_from_magic(magic: object) -> Optional[int]:
    """MT5 魔术号 -> 子任务号；不在 strategy 魔术号区间内则返回 None。"""
    try:
        value = int(magic)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    dispatch_id = value - Config.NODE_TASK_MAGIC_BASE
    return dispatch_id if dispatch_id > 0 else None


def member_ids(group: dict) -> list[str]:
    """分组成员的 node_id 列表，按组内顺序（sort_order 升序）排列。"""
    members = group.get("members") or []
    ordered = sorted(
        (m for m in members if isinstance(m, dict) and m.get("node_id")),
        key=lambda m: (int(m.get("sort_order") or 0), str(m.get("node_id"))),
    )
    return [str(m["node_id"]) for m in ordered]


def effective_node_ids(group: dict, nodemap: dict[str, dict], online_ids: set[str]) -> list[str]:
    """分组内的“有效节点”：节点存在 + 已启用 + 当前在线，保持组内顺序。"""
    return [
        nid for nid in member_ids(group)
        if (node := nodemap.get(nid)) is not None
        and node.get("enabled", True)
        and nid in online_ids
    ]


def group_skip_reason(group: dict, effective: list[str]) -> Optional[str]:
    """分组不参与本次分发的原因；可参与时返回 None。"""
    if not group.get("enabled", True):
        return f"分组已禁用：{group.get('name') or group.get('group_id')}"
    if not member_ids(group):
        return f"分组无成员节点：{group.get('name') or group.get('group_id')}"
    if not effective:
        return f"分组无有效节点：{group.get('name') or group.get('group_id')}（成员均未启用或不在线）"
    return None


def reconcile_rotation(order: list[str], participants: list[str]) -> list[str]:
    """把持久化的组内轮转顺序与当前成员对齐。

    保留仍在组内的节点的既有相对顺序（维持轮转位置），丢弃已移出分组的节点，
    并把新加入的节点按组内顺序追加到队尾。
    """
    participant_set = set(participants)
    merged = [nid for nid in order if nid in participant_set]
    seen = set(merged)
    for nid in participants:
        if nid not in seen:
            merged.append(nid)
            seen.add(nid)
    return merged


# 子任务在途状态：命令已投递但策略尚未收口
SUBTASK_PENDING = ("pending", "sent")
# 子任务运行态：首单已成交，节点正在按策略监控与加仓
SUBTASK_RUNNING = ("opened", "running", "closing")
# 子任务终态：不再变化，可参与主任务收口，并触发节点占位释放
SUBTASK_TERMINAL = ("done", "failed", "skipped", "offline")
# 子任务占位态：持有节点互斥占位的状态集合（在途 + 运行中）
SUBTASK_HOLDS_LOCK = SUBTASK_PENDING + SUBTASK_RUNNING

# 主任务未收口状态：仅用于展示与断线恢复筛选，不再承担互斥职责
TASK_ACTIVE = ("pending", "dispatching", "running")


def aggregate_task_status(dispatch_statuses: list[str]) -> str:
    """按各子任务状态汇总主任务状态。

    - 无子任务 -> skipped（没有任何节点被下发）
    - 仍有 pending/sent -> dispatching（还没确认首单）
    - 有 opened/running/closing -> running（策略在跑）
    - 既有成功又有失败 -> partial
    - 任一成功（无失败）-> done
    - 全部失败 -> failed
    - 全部跳过 -> skipped
    """
    if not dispatch_statuses:
        return "skipped"
    if any(st in SUBTASK_RUNNING for st in dispatch_statuses):
        return "running"
    if any(st in SUBTASK_PENDING for st in dispatch_statuses):
        return "dispatching"
    has_done = any(st == "done" for st in dispatch_statuses)
    has_failed = any(st in ("failed", "offline") for st in dispatch_statuses)
    if has_done and has_failed:
        return "partial"
    if has_done:
        return "done"
    if has_failed:
        return "failed"
    return "skipped"


def strategy_rules_snapshot(strategy: Optional[dict]) -> Optional[dict]:
    """把绑定策略压成随任务下发的快照，运行期不再受策略后续编辑影响。"""
    if not strategy:
        return None
    return {
        "strategy_id": strategy.get("strategy_id"),
        "name": strategy.get("name"),
        "symbol": strategy.get("symbol"),
        "template_id": strategy.get("template_id"),
        "rules": [dict(r) for r in (strategy.get("rules") or []) if isinstance(r, dict)],
    }


def _reject_risk_sized(rule: dict, *, signal_stop_loss: object,
                       signal_action: object = None) -> Optional[str]:
    """以损定量趋势单准入：手数由风险金额 ÷ 止损距离反推，没有止损价就算不出手数。"""
    del signal_action  # 本路径不看信号方向
    try:
        stop_loss = float(signal_stop_loss)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        stop_loss = 0.0
    if stop_loss <= 0:
        return "以损定量趋势单需要信号携带止损价（sl），本信号未提供"
    if float(rule.get("risk_amount") or 0) <= 0:
        return "以损定量趋势单的风险金额需大于 0"
    return None


def _reject_grid(rule: dict, *, signal_stop_loss: object,
                 signal_action: object = None) -> Optional[str]:
    """网格交易准入：区间合法、每格手数 > 0、方向与信号匹配。"""
    del signal_stop_loss  # 网格不依赖信号止损
    try:
        lower = float(rule.get("price_lower") or 0)
        upper = float(rule.get("price_upper") or 0)
    except (TypeError, ValueError):
        lower, upper = 0.0, 0.0
    if lower <= 0 or upper <= 0 or upper <= lower:
        return "网格交易需要合法的价格区间（上限须大于下限，且均大于 0）"
    if float(rule.get("lot_per_grid") or 0) <= 0:
        return "网格交易的每格手数需大于 0"
    side = str(rule.get("grid_side") or strategy_templates.GRID_SIDE_LONG).strip().lower()
    action = str(signal_action or "").strip().upper()
    if side == strategy_templates.GRID_SIDE_LONG and action == "SELL":
        return "网格方向为只做多（long），不接受 SELL 信号"
    if side == strategy_templates.GRID_SIDE_SHORT and action == "BUY":
        return "网格方向为只做空（short），不接受 BUY 信号"
    return None


# 各 type 的开仓准入实现。新增模版时在此登记，entry_reject_reason 无需再改。
_ENTRY_REJECTORS: dict[int, Callable[..., Optional[str]]] = {
    strategy_templates.RULE_TYPE_RISK_SIZED: _reject_risk_sized,
    strategy_templates.RULE_TYPE_GRID: _reject_grid,
}


def entry_reject_reason(
    strategy: dict,
    signal_stop_loss: object,
    signal_action: object = None,
) -> Optional[str]:
    """开仓前的策略级准入：策略跑不起来时给出人读的原因；可开仓返回 None。

    与其让节点收到命令后再失败一次，不如在分发前挡住，落选原因直接写进信号记录。
    按启用中规则的 type 分派到对应校验；未知 type 不拦截。
    """
    rules = strategy.get("rules")
    if not isinstance(rules, list):
        return None
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        if not int(rule.get("status") or 0):
            continue
        rule_type = int(rule.get("type") or 0)
        rejector = _ENTRY_REJECTORS.get(rule_type)
        if rejector is None:
            continue
        reason = rejector(
            rule, signal_stop_loss=signal_stop_loss, signal_action=signal_action,
        )
        if reason:
            return reason
    return None
