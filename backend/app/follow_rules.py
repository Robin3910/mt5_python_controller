"""带单（跟单）的纯规则判定：谁是带单员、哪些分组可以被跟、节点能不能跟。

不做任何 I/O：调用方（API / 分发层）拿到数据后调这里判断，便于单测。
与 group_rules 的分工：group_rules 管「分组内有哪些节点参与分发」，
这里管「这次跟单操作是否合法」以及「带单员 / 可跟单分组的口径」。

三条方向性约定（改动前请先读）：
- 跟单（把节点加入别人的分组）**默认人人可做**，不需要任何能力；
- 带单（被别人跟）需要 `follow_leader` 角色能力；
- 带单源永远是带单员的**主节点**，且分组必须包含该主节点才算带单源分组。
"""
from __future__ import annotations

from typing import Optional

from . import group_rules
from . import strategy_templates
from .permissions import CAP_FOLLOW_LEADER, normalize_owner


# ---------------------------------------------------------------------------
# 跟单市场（模版白名单）
# ---------------------------------------------------------------------------
# 「跟单市场」= 允许被跟单的策略模版集合。两个市场**互斥**：
#
#   带单市场（FOLLOW）  = 趋势策略(tpl_2) + 网格策略(tpl_3)  -> 【带单】Tab
#   AI 交易市场（AI）    = AI 智能加仓策略(tpl_1)              -> 【AI 交易】Tab
#
# 一个分组只绑定一个策略（node_group.strategy_id 唯一），所以一个分组只可能
# 属于一个市场 —— 市场互斥由数据结构天然保证，不需要额外的标记字段。
#
# 白名单而非黑名单：未来新增模版默认**不**外露，需显式加入才进入某个市场。
FOLLOW_MARKET_TEMPLATE_IDS: frozenset[str] = frozenset({
    strategy_templates.TEMPLATE_2_ID,   # tpl_2 趋势策略（市价 / 现价开仓）
    strategy_templates.TEMPLATE_3_ID,   # tpl_3 网格策略
})

AI_MARKET_TEMPLATE_IDS: frozenset[str] = frozenset({
    strategy_templates.TEMPLATE_1_ID,   # tpl_1 AI 智能加仓策略
})


def template_in_market(template_id: object | None, market: frozenset[str]) -> bool:
    """该策略模版是否属于指定跟单市场；白名单命中才算。

    template_id 为空（未知模版）时返回 False —— 调用方据此「模版已知但不在市场」
    与「模版未知」两种情况要分开处理，见 followable_groups 的注释。
    """
    if not template_id:
        return False
    return str(template_id).strip().lower() in market


def template_out_of_market(template_id: object | None, market: frozenset[str]) -> bool:
    """该模版**已知**且不在指定市场内。

    与 template_in_market 相反的方向，用于「拒绝」判定。关键区别：
    **模版未知（空）不算 out_of_market** —— 未绑定策略的分组、策略被删除的分组
    都不据此判死，避免把「跟单员在跟一个还没配策略的分组」这种既有场景打掉。
    """
    if not template_id:
        return False
    return not template_in_market(template_id, market)


def market_for_template(template_id: object | None) -> frozenset[str]:
    """按分组绑定的模版决定它属于哪个跟单市场。

    返回空集表示「两个市场都不收」（未知模版 / 未绑策略）—— 调用方据此
    保守处理（跟单节点不接收新信号）。
    """
    tid = str(template_id or "").strip().lower()
    if tid == strategy_templates.TEMPLATE_1_ID:
        return AI_MARKET_TEMPLATE_IDS
    if tid in (strategy_templates.TEMPLATE_2_ID, strategy_templates.TEMPLATE_3_ID):
        return FOLLOW_MARKET_TEMPLATE_IDS
    return frozenset()


def market_reject_reason(template_id: object | None, market: frozenset[str]) -> Optional[str]:
    """该模版不属于指定市场时的人读原因（详情页置灰提示用）。"""
    if template_in_market(template_id, market):
        return None
    tid = str(template_id or "").strip().lower()
    label = strategy_templates.live_template_name(tid, None) or tid or "未知策略"
    if tid == strategy_templates.TEMPLATE_1_ID:
        return f"「{label}」不在普通跟单市场，请在【AI 交易】页跟单"
    if not tid:
        return "该分组未绑定可用策略，暂不开放跟单"
    return f"「{label}」暂不开放跟单"


def leave_market_skip_reason(
    template_id: object | None, market: frozenset[str],
) -> Optional[str]:
    """移出跟单时的市场校验：该分组是否属于**别的**跟单市场。

    返回 None = 允许从本市场移出；返回字符串 = 跨市场，应跳过且**不动这条关系**。

    两个市场共用同一张关系表，而「移出」是按 (group_id, node_id) 删关系的。
    若不校验市场，在【带单】tab 移出就会连带删掉【AI 交易】市场的关系 ——
    跟单员在另一侧还自以为在跟，属于跨市场误删。

    只在模版**已知**时判定：模版未知（未绑策略 / 策略被删除）既不属于任何市场，
    允许从任一市场移出 —— 跟单员必须始终能自行清理自己的节点。
    """
    tid = str(template_id or "").strip().lower()
    if not tid:
        return None
    if template_in_market(tid, market):
        return None
    label = strategy_templates.live_template_name(tid, None) or tid
    if tid == strategy_templates.TEMPLATE_1_ID:
        return f"「{label}」属于【AI 交易】市场，请在【AI 交易】页移出"
    return f"「{label}」属于【带单】市场，请在【带单】页移出"


# ---------------------------------------------------------------------------
# 能力
# ---------------------------------------------------------------------------
def has_leader_capability(capabilities: object) -> bool:
    """按能力码列表判断是否可带单（列表投影场景，管理员由调用方另行放行）。"""
    if isinstance(capabilities, str):
        capabilities = [capabilities]
    try:
        return CAP_FOLLOW_LEADER in set(capabilities or [])
    except TypeError:
        return False


# ---------------------------------------------------------------------------
# 带单源
# ---------------------------------------------------------------------------
def primary_node_of(nodes: list[dict], user_id: int) -> Optional[dict]:
    """某用户的主节点**记录**；没有主节点返回 None。

    主节点按所有者唯一（`node_service.set_primary` 保证同 owner 至多一个），
    所以这里取第一个命中即可。这是「谁是主节点」的唯一实现：
    带单广场、AI 机器人广场、绩效统计三处共用，避免各写一遍
    `next(n for n in ... if n.get("is_primary"))` 日后口径漂移。

    刻意**不看 enabled**：调用方（列表接口）另行用
    `leader_primary_node_id` 做「是否对外接单」的准入判定，这里只回答
    「哪个是主节点」。绩效统计要的是主节点的历史成交，哪怕它当前被禁用。
    """
    target = normalize_owner(user_id)
    for n in nodes or []:
        if not isinstance(n, dict):
            continue
        if normalize_owner(n.get("owner_user_id")) != target:
            continue
        if n.get("is_primary"):
            return n
    return None


def leader_primary_node_id(nodes: list[dict], leader_user_id: int) -> Optional[str]:
    """带单员的主节点 ID（带单源）；没有主节点返回 None。

    没有主节点就没有带单源 —— 交易页的触发入口就是主节点，没有它无法对外发信号。
    """
    for n in nodes or []:
        if not isinstance(n, dict):
            continue
        if normalize_owner(n.get("owner_user_id")) != normalize_owner(leader_user_id):
            continue
        if n.get("is_primary") and n.get("enabled", True):
            return str(n.get("node_id"))
    return None


def group_contains_primary(group: dict, primary_node_id: Optional[str]) -> bool:
    """分组是否包含带单员的主节点 —— 这是「带单源分组」的唯一判据。

    跟单员加入的是**主节点所在的分组**，所以带单员名下那些不含主节点的手工分组
    不进入跟单市场（它们本来也不是带单员对外发信号的入口）。
    """
    if not primary_node_id:
        return False
    target = str(primary_node_id).strip().lower()
    return any(
        str(m.get("node_id") or "").strip().lower() == target
        for m in (group.get("members") or [])
        if isinstance(m, dict)
    )


def is_leader_source_group(group: dict, leader_user_id: int, primary_node_id: Optional[str]) -> bool:
    """该分组是否是某带单员的带单源分组（可被跟单）。"""
    if normalize_owner(group.get("owner_user_id")) != normalize_owner(leader_user_id):
        return False
    return group_contains_primary(group, primary_node_id)


def followable_groups(
    groups: list[dict], leader_user_id: int, primary_node_id: Optional[str],
    *,
    market: frozenset[str] = FOLLOW_MARKET_TEMPLATE_IDS,
    strategy_templates_map: Optional[dict[str, str]] = None,
) -> list[dict]:
    """带单员名下所有可被跟单的分组（启用 + 含主节点 + 模版在市场白名单内）。

    market：跟单市场的模版白名单。默认 = 普通带单市场（tpl_2 + tpl_3）；
        传 AI_MARKET_TEMPLATE_IDS 即得【AI 交易】市场的可跟分组。
    strategy_templates_map：{strategy_id: template_id} 快照，由调用方批量预取
        （避免在循环里逐个查 Redis）。为 None 时**跳过模版判定**（仅供内部 / 测试用）。

    这是带单市场 / AI 交易市场共用的**唯一**判定入口：列表、详情、join 三处
    都走这里，改一个函数三处一起生效。
    """
    if not primary_node_id:
        return []
    out: list[dict] = []
    for g in groups or []:
        if not isinstance(g, dict):
            continue
        if not g.get("enabled", True):
            continue
        if not is_leader_source_group(g, leader_user_id, primary_node_id):
            continue
        sid = str(g.get("strategy_id") or "").strip()
        if not sid:
            # 未绑策略的分组：保持既有可跟单行为（不因市场判定被拒）。
            # 它本来也不会被分发 —— _candidate_groups 见到未绑策略直接跳过。
            out.append(g)
            continue
        if strategy_templates_map is not None:
            tid = strategy_templates_map.get(sid)
            # 只在模版**已知**且不属于本市场时才拒绝。模版未知（策略被删除）
            # 保持既有行为，不在跟单侧判死 —— 分发侧另有保守处理。
            if template_out_of_market(tid, market):
                continue
        out.append(g)
    return out


def is_displayable_leader(
    leader: dict, groups: list[dict], current_user_id: int,
) -> bool:
    """该用户是否应出现在【带单】列表里（含自己，用于自跟单场景）。

    实际列表过滤由 follow_api._leader_ids_with_capability 驱动，此函数为
    未来精细化过滤（如屏蔽特定用户）留的后门。

    四个条件全满足才展示：持有 follow_leader 能力、账号启用、有主节点、
    至少有一个可跟单分组（含自己，用于自跟单场景）。
    """
    if not isinstance(leader, dict):
        return False
    if not leader.get("is_active", True):
        return False
    if not has_leader_capability(leader.get("capabilities")):
        return False
    primary = leader.get("primary_node_id")
    if not primary:
        return False
    return bool(followable_groups(groups, leader.get("user_id"), primary))


# ---------------------------------------------------------------------------
# 节点准入（加入跟单时的校验）
# ---------------------------------------------------------------------------
NODE_JOIN_REJECT = {
    "primary": "主节点不能用于跟单（主节点需保持独立）",
    "disabled": "节点已停用",
    "duplicate": "已在此分组跟单",
    "foreign": "节点不属于你",
}


def node_join_reject_reason(
    node: Optional[dict], *, existing_group: Optional[dict], group_id: str,
) -> Optional[str]:
    """该节点能否加入本分组跟单；可加入返回 None，否则给人读原因。

    刻意不返回「对方分组是否存在」这类信息 —— 越权与不存在一律同一个原因，
    避免通过详情接口探测带单员名下有什么分组。
    """
    if not node:
        return NODE_JOIN_REJECT["foreign"]
    if node.get("is_primary"):
        return NODE_JOIN_REJECT["primary"]
    if not node.get("enabled", True):
        return NODE_JOIN_REJECT["disabled"]
    gid = str(group_id or "").strip().lower()
    if any(
        str(f.get("node_id") or "").strip().lower() == str(node.get("node_id")).strip().lower()
        for f in ((existing_group or {}).get("followers") or [])
        if isinstance(f, dict)
    ):
        return NODE_JOIN_REJECT["duplicate"]
    return None


# ---------------------------------------------------------------------------
# 分组市场状态（带单员侧变化时给跟单员看的原因）
# ---------------------------------------------------------------------------
def marketable_reason(
    group: dict, leader_has_capability: bool, primary_node_id: Optional[str],
    template_id: object | None = None,
    market: frozenset[str] = FOLLOW_MARKET_TEMPLATE_IDS,
) -> Optional[str]:
    """
    该分组当前是否还在指定市场；下架时返回人读原因，可跟单返回 None。

    三种下架：带单员被撤销能力、分组不再包含其当前主节点（切换主节点后旧分组不再对外接单）、
    分组绑定的模版不在该市场内（如 AI 智能加仓策略不在普通带单市场）。
    跟单员仍可自行移出，这里只负责解释。

    判断顺序：能力 / 主节点在前（更根本），模版市场其次，分组禁用最后。
    """
    if not leader_has_capability:
        return "该带单员已关闭带单，本分组不再下发新信号"
    if not group_contains_primary(group, primary_node_id):
        return "该带单员已更换主节点，本分组不再对外接单"
    if template_out_of_market(template_id, market):
        return market_reject_reason(template_id, market)
    if not group.get("enabled", True):
        return "分组已禁用"
    return None


# ---------------------------------------------------------------------------
# 分发口径（与 group_rules 配合）
# ---------------------------------------------------------------------------
def group_still_leads(group: dict, leader_has_capability: bool, primary_node_id: Optional[str]) -> bool:
    """分组此刻是否仍在带单：带单员有能力 + 分组仍含其主节点。

    分发时据此决定是否把跟单成员纳入目标节点；返回 False 时退回 owner 口径。
    """
    if not leader_has_capability:
        return False
    return group_contains_primary(group, primary_node_id)


def dispatch_targets(
    group: dict, nodemap: dict[str, dict], online_ids: set[str], *,
    leader_has_capability: bool, primary_node_id: Optional[str],
) -> list[str]:
    """本分组本次应下发的节点。

    带单员不再带单（撤销能力 / 换了主节点）时，跟单成员一律不接收新信号，
    退回 owner 成员口径；已在跑的任务不受影响，由各自自然结束。
    """
    if not group_still_leads(group, leader_has_capability, primary_node_id):
        return group_rules.owner_effective_node_ids(group, nodemap, online_ids)
    return group_rules.effective_node_ids(group, nodemap, online_ids)


def leader_group_sort_key(group: dict) -> tuple:
    """带单员列表 / 我的跟单的稳定排序：按创建时间，再按分组 ID。"""
    return (float(group.get("created_at") or 0), str(group.get("group_id") or ""))