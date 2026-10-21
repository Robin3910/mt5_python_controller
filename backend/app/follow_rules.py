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
from .permissions import CAP_FOLLOW_LEADER, normalize_owner


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
) -> list[dict]:
    """带单员名下所有可被跟单的分组（启用 + 含主节点）。"""
    if not primary_node_id:
        return []
    return [
        g for g in (groups or [])
        if isinstance(g, dict)
        and g.get("enabled", True)
        and is_leader_source_group(g, leader_user_id, primary_node_id)
    ]


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
) -> Optional[str]:
    """该分组当前是否还在带单市场；下架时返回人读原因，可带单返回 None。

    两种下架：带单员被撤销能力、分组不再包含其当前主节点（切换主节点后
    旧分组不再对外接单）。跟单员仍可自行移出，这里只负责解释。
    """
    if not leader_has_capability:
        return "该带单员已关闭带单，本分组不再下发新信号"
    if not group_contains_primary(group, primary_node_id):
        return "该带单员已更换主节点，本分组不再对外接单"
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