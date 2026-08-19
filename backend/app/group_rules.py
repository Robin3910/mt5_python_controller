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

from . import strategy_templates, trend_indicators
from .config import Config
from .models import GROUP_DISPATCH_MODES, SIGNAL_MODEL_NORMAL, SIGNAL_MODELS

_TREND_VERDICT_LABELS = {
    trend_indicators.TREND_BULLISH: "多头",
    trend_indicators.TREND_BEARISH: "空头",
    trend_indicators.TREND_NEUTRAL: "中性",
    trend_indicators.TREND_UNKNOWN: "数据不足",
}


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


def unknown_template_ids(template_ids: Optional[list[str]]) -> list[str]:
    """信号 template_ids 里不存在于模版注册表的项，供入口层拒收明显的配置错误。"""
    return [
        tid for tid in (template_ids or [])
        if tid not in strategy_templates.STRATEGY_TEMPLATES
    ]


def template_reject_reason(
    strategy: dict, template_ids: Optional[list[str]],
) -> Optional[str]:
    """策略模版定向：信号指定 template_ids 时，只有绑定这些模版的策略才接收。

    template_ids 缺省 / 为空表示不限制；可接收时返回 None，否则返回人读的落选原因。
    """
    if not template_ids:
        return None
    template_id = str(strategy.get("template_id") or "").strip().lower()
    if template_id in template_ids:
        return None
    label = strategy.get("template_name") or template_id or "未知模版"
    return f"策略模版 {label} 不在信号指定的 template_ids（{'、'.join(template_ids)}）内"


def group_targeted(group: dict, group_ids: Optional[list[str]]) -> bool:
    """分组定向：信号是否点名了这个分组；group_ids 为空表示不限制（全部视为被点名）。"""
    if not group_ids:
        return True
    return str(group.get("group_id") or "").strip().lower() in group_ids


def missing_group_ids(groups: list[dict], group_ids: Optional[list[str]]) -> list[str]:
    """信号点名了、但当前并不存在的分组 ID，供落选说明指出是哪几个写错或已删除。"""
    if not group_ids:
        return []
    known = {str(g.get("group_id") or "").strip().lower() for g in groups}
    return [gid for gid in group_ids if gid not in known]


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


# 主任务 skip_reason 与 GroupSignalTask.skip_reason 列宽对齐
_TASK_SKIP_REASON_MAX = 255
_BUSY_REASON = "目标节点在本分组内均有进行中的任务"
_UNAVAILABLE_REASON = "下发失败：目标节点连接均不可用"
_TREND_ALL_REASON = "本组有效节点均被趋势风控拦截"


def _clip_skip_reason(text: str) -> str:
    text = str(text or "").strip()
    if len(text) <= _TASK_SKIP_REASON_MAX:
        return text
    return text[: _TASK_SKIP_REASON_MAX - 1] + "…"


def _no_target_kind(outcome: dict) -> str:
    """把零下发节点结果分成 trend / busy / skipped_other / unavailable。"""
    reason = str(outcome.get("reason") or "").strip()
    status = str(outcome.get("status") or "").strip()
    if reason.startswith("趋势风控"):
        return "trend"
    if "进行中的子任务" in reason:
        return "busy"
    if status == "skipped":
        return "skipped_other"
    return "unavailable"


def summarize_no_target(outcomes: list[dict]) -> tuple[str, str]:
    """一个节点都没发出去时的 (状态, 原因)。

    必须看各节点 `reason`：`skipped` 既可能是组内互斥，也可能是趋势风控。
    全忙仍用「均有进行中的任务」；全趋势抄节点文案（多条不同则归并）；
    混因用分号拼接，避免把趋势拦截写成节点忙或连接失败。
    """
    items = [o for o in outcomes if isinstance(o, dict)]
    if not items:
        return "failed", _UNAVAILABLE_REASON

    kinds = [_no_target_kind(o) for o in items]
    unique: list[str] = []
    seen: set[str] = set()
    for o in items:
        reason = str(o.get("reason") or "").strip()
        if reason and reason not in seen:
            seen.add(reason)
            unique.append(reason)

    skip_kinds = {"trend", "busy", "skipped_other"}
    all_skipped = all(k in skip_kinds for k in kinds)
    status = "skipped" if all_skipped else "failed"
    kind_set = set(kinds)

    if kind_set == {"busy"}:
        return "skipped", _BUSY_REASON
    if kind_set == {"trend"}:
        if len(unique) == 1:
            return "skipped", _clip_skip_reason(unique[0])
        return "skipped", _TREND_ALL_REASON
    if kind_set == {"unavailable"}:
        if len(unique) == 1:
            return "failed", _clip_skip_reason(unique[0])
        return "failed", _UNAVAILABLE_REASON

    if len(unique) == 1:
        return status, _clip_skip_reason(unique[0])
    if unique:
        return status, _clip_skip_reason("；".join(unique))
    return status, _BUSY_REASON if all_skipped else _UNAVAILABLE_REASON


def trend_verdict_label(verdict: str) -> str:
    """趋势结论的中文标签（供 skip_reason / 日志展示）。"""
    return _TREND_VERDICT_LABELS.get(str(verdict or "").strip().lower(), str(verdict or "未知"))


def trend_risk_reject_reason(
    action: str,
    verdict: str,
    *,
    ready: bool,
    score: float | None = None,
) -> Optional[str]:
    """趋势风控门禁：顺势才放行；返回拦截原因，放行时返回 None。

    口径（分组开关开启时）：
    - BUY 仅多头放行，SELL 仅空头放行；
    - 中性 / 数据不足 / ready=false 一律拦截；
    - CLOSE 与其它非开仓动作不参与判定（返回 None）。
    """
    act = str(action or "").strip().upper()
    if act not in ("BUY", "SELL"):
        return None

    score_part = ""
    if score is not None:
        try:
            score_part = f"（得分 {float(score):+.1f}）"
        except (TypeError, ValueError):
            score_part = ""

    if not ready:
        return f"趋势风控：信号 {act}，当前趋势为数据不足{score_part}，已拦截"

    v = str(verdict or "").strip().lower()
    if v == trend_indicators.TREND_UNKNOWN or not v:
        return f"趋势风控：信号 {act}，当前趋势为数据不足{score_part}，已拦截"

    label = trend_verdict_label(v)
    if act == "BUY" and v == trend_indicators.TREND_BULLISH:
        return None
    if act == "SELL" and v == trend_indicators.TREND_BEARISH:
        return None
    return f"趋势风控：信号 {act}，当前趋势为{label}{score_part}，已拦截"


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
# 节点已停止交易、但魔术号下仍有持仓：
# stop_failed 平仓没平干净 / detached 按配置保留持仓 / faulted 执行器异常退出。
# 这些都不是终态——持仓还在就必须继续持有占位，不能让下一条信号叠上来。
SUBTASK_STUCK = ("stop_failed", "detached", "faulted")
# 子任务终态：不再变化，可参与主任务收口，并触发节点占位释放
SUBTASK_TERMINAL = ("done", "failed", "skipped", "offline")
# 子任务占位态：持有节点互斥占位的状态集合（在途 + 运行中 + 残仓待处理）
SUBTASK_HOLDS_LOCK = SUBTASK_PENDING + SUBTASK_RUNNING + SUBTASK_STUCK

# 主任务未收口状态：仅用于展示与断线恢复筛选，不再承担互斥职责
TASK_ACTIVE = ("pending", "dispatching", "running")


def can_apply_phase(current: str, phase: str) -> bool:
    """节点上报的运行阶段能否覆盖当前子任务状态。

    状态只允许前向流动：已进入终止流程（closing）或已停手待处理残仓（STUCK）的
    子任务，不能被一条迟到的运行心跳改回 running，否则终止意图会凭空丢掉。
    """
    if phase not in SUBTASK_RUNNING:
        return False
    if current in SUBTASK_TERMINAL or current in SUBTASK_STUCK:
        return False
    if current == "closing" and phase != "closing":
        return False
    return True


def aggregate_task_status(dispatch_statuses: list[str]) -> str:
    """按各子任务状态汇总主任务状态。

    - 无子任务 -> skipped（没有任何节点被下发）
    - 仍有 pending/sent -> dispatching（还没确认首单）
    - 有 opened/running/closing 或残仓待处理 -> running（还没了结）
    - 既有成功又有失败 -> partial
    - 任一成功（无失败）-> done
    - 全部失败 -> failed
    - 全部跳过 -> skipped
    """
    if not dispatch_statuses:
        return "skipped"
    if any(st in SUBTASK_RUNNING or st in SUBTASK_STUCK for st in dispatch_statuses):
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
                       signal_action: object = None,
                       signal_entry_price: object = None) -> Optional[str]:
    """以损定量趋势单准入：手数由风险金额 ÷ 止损距离反推，没有止损价就算不出手数。

    限价开仓还要额外有入场价，且必须落在止损价的盈利侧——挂在止损之外的单一旦
    成交就已经越过止损，等于开仓即止损。
    """
    try:
        stop_loss = float(signal_stop_loss)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        stop_loss = 0.0
    if stop_loss <= 0:
        return "以损定量趋势单需要信号携带止损价（sl），本信号未提供"
    if float(rule.get("risk_amount") or 0) <= 0:
        return "以损定量趋势单的风险金额需大于 0"
    if not strategy_templates.is_limit_entry(rule):
        return None  # 市价开仓不看入场价，方向也由节点按现价判定

    try:
        entry_price = float(signal_entry_price)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        entry_price = 0.0
    if entry_price <= 0:
        return "限价开仓需要信号携带入场价（limit_price / price），本信号未提供"
    action = str(signal_action or "").strip().upper()
    if action == "BUY" and entry_price <= stop_loss:
        return f"限价开仓的入场价 {entry_price} 需高于止损价 {stop_loss}"
    if action == "SELL" and entry_price >= stop_loss:
        return f"限价开仓的入场价 {entry_price} 需低于止损价 {stop_loss}"
    return None


def _reject_grid(rule: dict, *, signal_stop_loss: object,
                 signal_action: object = None,
                 signal_entry_price: object = None) -> Optional[str]:
    """网格交易准入：区间合法、每格手数 > 0、方向与信号匹配。"""
    del signal_stop_loss, signal_entry_price  # 网格不依赖信号止损与入场价
    try:
        lower = float(rule.get("price_lower") or 0)
        upper = float(rule.get("price_upper") or 0)
    except (TypeError, ValueError):
        lower, upper = 0.0, 0.0
    if lower <= 0 or upper <= 0 or upper <= lower:
        return "网格交易需要合法的价格区间（上限须大于下限，且均大于 0）"
    if float(rule.get("lot_per_grid") or 0) <= 0:
        return "网格交易的每格手数需大于 0"
    try:
        lot_limit = float(rule.get("total_lot_limit") or 0)
        lot = float(rule.get("lot_per_grid") or 0)
    except (TypeError, ValueError):
        lot_limit, lot = 0.0, 0.0
    if lot_limit > 0 and lot > 0 and lot_limit < lot:
        return "网格交易的总手数上限须不小于每格手数"
    side = str(rule.get("grid_side") or strategy_templates.GRID_SIDE_LONG).strip().lower()
    action = str(signal_action or "").strip().upper()
    if side == strategy_templates.GRID_SIDE_LONG and action == "SELL":
        return "网格方向为只做多（long），不接受 SELL 信号"
    if side == strategy_templates.GRID_SIDE_SHORT and action == "BUY":
        return "网格方向为只做空（short），不接受 BUY 信号"
    return None


def no_active_rule_reason(strategy: dict) -> Optional[str]:
    """策略规则启用态拒收原因；可开仓返回 None。

    - 规则列表为空：视为「仅首单、无加仓规则」的旧口径，不在此拦截；
    - 有规则但全部关闭：拒收；
    - 模版2/3 还必须有对应 type 的启用规则（否则节点无法走对执行路径）。
    """
    rules = strategy.get("rules")
    if not isinstance(rules, list) or not rules:
        return None
    if not any(isinstance(r, dict) and int(r.get("status") or 0) for r in rules):
        return "策略没有启用中的规则"
    template_id = str(strategy.get("template_id") or "").strip().lower()
    if template_id == strategy_templates.TEMPLATE_3_ID:
        if strategy_templates.pick_grid_rule(rules) is None:
            return "网格策略没有启用中的网格规则"
    if template_id == strategy_templates.TEMPLATE_2_ID:
        if strategy_templates.pick_risk_sized_rule(rules) is None:
            return "趋势策略没有启用中的以损定量规则"
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
    signal_entry_price: object = None,
) -> Optional[str]:
    """开仓前的策略级准入：策略跑不起来时给出人读的原因；可开仓返回 None。

    与其让节点收到命令后再失败一次，不如在分发前挡住，落选原因直接写进信号记录。
    先拦启用态 / 模版必备规则，再按启用中规则的 type 分派到对应校验；未知 type 不拦截。
    """
    empty = no_active_rule_reason(strategy)
    if empty:
        return empty
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
            signal_entry_price=signal_entry_price,
        )
        if reason:
            return reason
    return None
