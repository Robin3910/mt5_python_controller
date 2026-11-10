"""【AI 交易】Tab 的 API：AI 机器人广场 + AI 跟单。

与【带单】Tab（follow_api）**完全对称**，但走独立的路由与审计分类：

| 维度 | 【带单】Tab | 【AI 交易】Tab |
|---|---|---|
| 路由 | `/api/follow/*` | `/api/ai-follow/*` |
| 审计 category | `follow` | `ai_follow` |
| 市场白名单 | `tpl_2` + `tpl_3` | `tpl_1`（AI 智能加仓策略） |
| 机器人准入 | `follow_leader` | `ai_strategy` **且** `follow_leader` |
| 分组开关 | 无 | `accept_ai_follow`（默认关） |

**内部完全复用** follow_service 的关系读写与 follow_rules 的判定 ——
两个市场共用同一张 `group_follower_node` 表、同一套分发链路，
靠「分组绑定的策略模版」天然互斥。

为什么不复用 /api/follow/* 的路由：
① 审计 category 可区分；② 未来 AI 跟单要独立限流 / 统计时切开更容易；
③ 两个 tab 的 unselectable_reason 文案完全不同。

方向性约定：
- 能否**当机器人主人**对外接单 → 需要 `ai_strategy` 且 `follow_leader`；
- 能否**跟**别人的 AI 机器人 → 只需要能进【AI 交易】菜单（assignable，默认人人有）。
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from . import (
    ai_application_store,
    ai_follow_nodes,
    ai_robot_detail,
    deal_stats,
    follow_rules,
    follow_service,
    group_persist,
    permissions,
    persist,
    rbac_service,
    user_service,
)
from .connections import manager
from .ai_channel_models import AiUiEvent
from .ai_follow_settings import public_settings
from .db import SessionLocal
from .deps import client_ip, get_group_dispatcher, get_store, require_menu
from .models import (
    AiJoinRequest,
    AiJoinResponse,
    AiLeaveRequest,
    AiLeaveResponse,
    AiMeResponse,
    AiMyGroup,
    AiMyRobot,
    AiMyRobotDetailResponse,
    AiFollowNodesResponse,
    AiFollowNodesRequest,
    AiFollowNodeStateRequest,
    AiPauseRequest,
    AiPauseResponse,
    AiRobotDetailResponse,
    AiRobotGroupDetail,
    AiRobotListResponse,
    AiRobotSummary,
)
from .permissions import MENU_AI_TRADE, Principal
from .redis_store import RedisStore

router = APIRouter(prefix="/api/ai-follow", tags=["ai_follow"])


class _Stub:
    """机器人主人已被删除时的占位（审计用，不泄露存在性）。"""

    def __init__(self, user_id: int):
        self.id = user_id
        self.username = f"user#{user_id}"


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------
def _node_brief(node: Optional[dict]) -> Optional[dict]:
    if not node:
        return None
    return {
        "node_id": node.get("node_id"),
        "name": node.get("name"),
        "mt5_login": node.get("mt5_login"),
        "enabled": bool(node.get("enabled", True)),
        "is_primary": bool(node.get("is_primary")),
        "online": manager.is_node_online(str(node.get("node_id"))),
    }


def _primary_of(all_nodes: list[dict], user_id: Optional[int]) -> Optional[dict]:
    """某用户的主节点。

    委托 `follow_rules.primary_node_of` —— 与带单广场共用同一实现，
    两边口径本就相同（都是主节点口径），各写一遍必然漂移。
    """
    return follow_rules.primary_node_of(all_nodes, user_id)


def _viewable_follow_nodes(all_nodes: list[dict], principal: Principal) -> list[dict]:
    """AI 跟单允许主节点，归属与开通状态仍须有效。"""
    return [
        n for n in all_nodes
        if permissions.same_owner(n.get("owner_user_id"), permissions.owner_for_new(principal))
        and n.get("enabled", True)
        and n.get("approval_status", "approved") == "approved"
    ]


def _count_online(group: dict) -> int:
    """分组内在线节点数（owner 成员 + 跟单成员）。"""
    return sum(1 for m in (group.get("members") or []) if m.get("online"))


async def _ai_host_ids_with_capability(store: RedisStore, users: list) -> set[int]:
    """能当 AI 机器人主人的用户 ID（ai_strategy 且 follow_leader，且账号启用）。

    镜像 follow_api._leader_ids_with_capability：逐个 load_principal，
    管理员由 can_capability 内部的 is_admin 分支放行。
    """
    out: set[int] = set()
    for u in users:
        if not u.is_active:
            continue
        principal = await rbac_service.load_principal(store, u.username)
        if principal and permissions.can_host_ai_robot(principal):
            out.add(u.id)
    return out


def _rule_value(strategy: Optional[dict], key: str):
    """取策略规则里的某个值（AI 的逆势 / 顺势两条规则，取第一条有该值的）。

    规则列表里 type=1 是逆势、type=2 是顺势；跟单员看到的是机器人的
    风险量级，所以取「两条规则里更保守的那个」由调用方决定，这里只做取值。
    """
    if not strategy:
        return None
    for rule in (strategy.get("rules") or []):
        if not isinstance(rule, dict):
            continue
        if key in rule and rule.get(key) is not None:
            return rule.get(key)
    return None


def _risk_summary(strategy: Optional[dict]) -> dict:
    """AI 策略的风险参数摘要（供广场卡片与详情展示）。

    跟单员**不控手数**（决策），所以必须公开机器人的风险上限 ——
    否则就是盲跟。这是「不控手数」的对价。
    """
    return {
        "risk_amount": _rule_value(strategy, "risk_amount"),
        "max_allow_num": _rule_value(strategy, "max_allow_num"),
        "total_lot_limit": _rule_value(strategy, "total_lot_limit"),
        "lot_times": _rule_value(strategy, "lot_times"),
        "point": _rule_value(strategy, "point"),
    }


def _ai_groups_of(
    all_groups: list[dict],
    tmpl_map: dict[str, str],
    leader_user_id: int,
    primary_node_id: Optional[str],
    *,
    only_open: bool = True,
) -> list[dict]:
    """某机器人名下可对外开放的 AI 分组（tpl_1 + 已开放 AI 跟单）。"""
    groups = follow_rules.followable_groups(
        all_groups, leader_user_id, primary_node_id,
        market=follow_rules.AI_MARKET_TEMPLATE_IDS,
        strategy_templates_map=tmpl_map,
    )
    if not only_open:
        return groups
    return [g for g in groups if g.get("accept_ai_follow")]


def _group_robot_summary(user, primary: Optional[dict], group: dict, strategy: Optional[dict], p: Principal) -> dict:
    strategy = strategy or {}
    name = strategy.get("name")
    risk = _risk_summary(strategy)
    return {
        "leader_user_id": user.id,
        "group_id": group["group_id"],
        "strategy_id": group.get("strategy_id"),
        "username": user.username,
        "display_name": user.display_name,
        "description": strategy.get("remark") or getattr(user, "leader_intro", None),
        "primary_node": _node_brief(primary),
        "ai_group_count": 1,
        "strategy_name": name,
        "strategy_names": [name] if name else [],
        "editable_strategy_id": (
            strategy.get("strategy_id")
            if permissions.same_owner(strategy.get("owner_user_id"), permissions.owner_for_new(p))
            and follow_rules.template_in_market(strategy.get("template_id"), follow_rules.AI_MARKET_TEMPLATE_IDS)
            else None
        ),
        "max_allow_num": risk.get("max_allow_num"),
        "total_lot_limit": risk.get("total_lot_limit"),
        "ai_follow_paused": bool(getattr(user, "ai_follow_paused", False)),
    }


# ---------------------------------------------------------------------------
# 机器人广场
# ---------------------------------------------------------------------------
@router.get("/robots", response_model=AiRobotListResponse)
async def list_ai_robots(
    q: str = "",
    window: str = "30d",
    store: RedisStore = Depends(get_store),
    p: Principal = Depends(require_menu(MENU_AI_TRADE)),
) -> AiRobotListResponse:
    """每个已开放的 AI 策略分组一条记录，计数与主节点绩效均按分组隔离。"""
    users = await user_service.list_users()
    hosts = await _ai_host_ids_with_capability(store, users)
    if not hosts:
        return AiRobotListResponse(robots=[])

    all_groups = await store.all_groups()
    all_nodes = await store.all_nodes()
    strategies = await follow_service._strategies_of(all_groups, store)
    tmpl_map = follow_service._template_ids_of(strategies)

    mine_by_group: dict[str, list[dict]] = {}
    for f in await follow_service.list_followings(p.user_id):
        mine_by_group.setdefault(f["group_id"], []).append(f)
    followers_by_group: dict[str, set[str]] = {}
    for f in await follow_service.list_for_leaders(hosts):
        followers_by_group.setdefault(f["group_id"], set()).add(f["node_id"])
    groups_by_id = {g["group_id"]: g for g in all_groups}
    win = window if window in deal_stats.WINDOWS else deal_stats.DEFAULT_WINDOW
    by_id = {u.id: u for u in users}
    needle = q.strip().lower()
    candidates: list[tuple] = []
    for uid in hosts:
        u = by_id.get(uid)
        if not u:
            continue
        primary = _primary_of(all_nodes, uid)
        if not primary:
            continue
        groups = _ai_groups_of(all_groups, tmpl_map, uid, primary.get("node_id"))
        for group in sorted(groups, key=follow_rules.leader_group_sort_key):
            strategy = strategies.get(group.get("strategy_id") or "")
            haystack = " ".join(str(value or "") for value in (
                u.username, u.display_name, primary.get("name"), primary.get("mt5_login"),
                group.get("name"), (strategy or {}).get("name"),
            )).lower()
            if not needle or needle in haystack:
                candidates.append((u, primary, group, strategy))
    scopes = await ai_robot_detail.group_node_magics(
        [primary["node_id"] for user, primary, group, strategy in candidates],
        [group["group_id"] for user, primary, group, strategy in candidates],
    )
    out: list[dict] = []
    for user, primary, group, strategy in candidates:
        gid = group["group_id"]
        my_group_count, my_node_count = follow_rules.my_follow_counts_in_market(
            mine_by_group.get(gid, []), groups_by_id, follow_rules.AI_MARKET_TEMPLATE_IDS, tmpl_map,
        )
        perf = await deal_stats.leader_batch(
            [primary["node_id"]], None if win == "all" else deal_stats.window_start(win),
            node_magics=scopes[gid],
        )
        follower_count = len(followers_by_group.get(gid, set()))
        out.append({
            **_group_robot_summary(user, primary, group, strategy, p),
            "follower_node_count": follower_count,
            "my_follow_group_count": my_group_count,
            "my_follow_node_count": my_node_count,
            "marketable": True,
            "performance": perf,
            "perf_window": win,
            "follower_count": follower_count,
        })
    out.sort(key=lambda item: (item["my_follow_group_count"] == 0, item["username"], item["group_id"]))
    return AiRobotListResponse(robots=out)


# ---------------------------------------------------------------------------
# 机器人详情
# ---------------------------------------------------------------------------
@router.get("/robots/{leader_user_id}", response_model=AiRobotDetailResponse)
async def get_ai_robot_detail(
    leader_user_id: int,
    window: str = "30d",
    group_id: Optional[str] = None,
    store: RedisStore = Depends(get_store),
    p: Principal = Depends(require_menu(MENU_AI_TRADE)),
) -> AiRobotDetailResponse:
    """机器人详情：可开放的 AI 分组（含我的已跟标记）+ 我可用于跟单的节点。"""
    user = await user_service.get_user_by_id(leader_user_id)
    if not user or not user.is_active:
        raise HTTPException(status_code=404, detail="机器人主人不存在或已停用")
    principal = await rbac_service.load_principal(store, user.username)
    if not principal or not permissions.can_host_ai_robot(principal):
        raise HTTPException(status_code=404, detail="该用户暂未开放 AI 跟单")

    all_nodes = await store.all_nodes()
    primary = _primary_of(all_nodes, leader_user_id)
    if not primary:
        raise HTTPException(status_code=404, detail="该机器人主人尚无主节点")

    all_groups = await store.all_groups()
    strategies = await follow_service._strategies_of(all_groups, store)
    tmpl_map = follow_service._template_ids_of(strategies)
    groups = _ai_groups_of(all_groups, tmpl_map, leader_user_id, primary.get("node_id"))
    if group_id is not None:
        groups = [group for group in groups if group["group_id"] == group_id]
        if not groups:
            raise HTTPException(status_code=404, detail="该 AI 策略分组不存在或未开放跟单")
    signal_counts = await group_persist.count_by_group()
    active_counts = await group_persist.count_active_by_group()

    mine_by_group: dict[str, list[str]] = {}
    my_followings_for_leader: list[dict] = []
    for f in await follow_service.list_followings(p.user_id):
        if int(f["leader_user_id"]) == leader_user_id:
            mine_by_group.setdefault(f["group_id"], []).append(f["node_id"])
            my_followings_for_leader.append(f)
    # 「我已跟」计数：**只算 AI 市场**（tpl_1）的跟单关系。
    # 带单市场（tpl_2 开仓策略 / tpl_3 网格）的关系不算 ——
    # 跟了开仓策略分组 ≠ 跟了 AI 机器人（该用户报的现象）。
    my_group_count, my_node_count = follow_rules.my_follow_counts_in_market(
        [item for item in my_followings_for_leader if group_id is None or item["group_id"] == group_id],
        {g["group_id"]: g for g in all_groups},
        follow_rules.AI_MARKET_TEMPLATE_IDS,
        tmpl_map,
    )

    detail_groups: list[dict] = []
    # 暂停 AI 跟单：分组置灰 + 给出原因，让跟单员**看得出**这里已经收口，
    # 而不是点了「确认跟单」才吃到一句 400。
    # 写侧仍会再拦一次（写侧拦截才是权威，前端置灰只是提示）。
    leader_paused = bool(getattr(user, "ai_follow_paused", False))
    for g in sorted(groups, key=follow_rules.leader_group_sort_key):
        gid = g["group_id"]
        strategy = strategies.get(g.get("strategy_id") or "")
        reason = None
        if not g.get("enabled", True):
            reason = "分组已禁用"
        elif strategy is None:
            reason = "已停止接收新信号（策略已停用）"
        elif leader_paused:
            reason = follow_rules.PAUSED_AI_REASON
        risk = _risk_summary(strategy)
        detail_groups.append({
            "group_id": gid,
            "name": g.get("name"),
            "symbol": (strategy or {}).get("symbol") or g.get("strategy_symbol"),
            "strategy_id": g.get("strategy_id"),
            "strategy_name": (strategy or {}).get("name"),
            "strategy_template": (strategy or {}).get("template_name"),
            "enabled": bool(g.get("enabled", True)),
            "member_count": len(g.get("members") or []),
            "follower_count": len(g.get("followers") or []),
            "online_count": _count_online(g),
            "active_task_count": active_counts.get(gid, 0),
            "signal_count": signal_counts.get(gid, 0),
            "my_follow_node_ids": mine_by_group.get(gid, []),
            "selectable": reason is None,
            "unselectable_reason": reason,
            **risk,
            # AI 智能加仓策略不支持信号并发
            "signal_concurrent_enabled": False,
            "concurrent_unavailable_reason": (
                "AI 智能加仓策略不支持信号并发，同一节点同时只跑一单"
            ),
        })

    risk_all = [_risk_summary(strategies.get(g.get("strategy_id") or "")) for g in groups]
    max_allow = [r["max_allow_num"] for r in risk_all if r["max_allow_num"] is not None]
    lot_limits = [r["total_lot_limit"] for r in risk_all if r["total_lot_limit"] is not None]

    # 绩效：主节点口径，与列表同一份缓存（口径完全一致）。
    # 详情是单个人，缓存刚建立时可能还没有 —— 允许就地回源算一次兜底。
    win = window if window in deal_stats.WINDOWS else deal_stats.DEFAULT_WINDOW
    perf: dict = {}
    node_magics = None
    if group_id is not None:
        scopes = await ai_robot_detail.group_node_magics([primary["node_id"]], [group_id])
        node_magics = scopes[group_id]
        perf = await deal_stats.leader_batch(
            [primary["node_id"]], None if win == "all" else deal_stats.window_start(win),
            node_magics=node_magics,
        )
    else:
        cached = await store.get_robot_stats(leader_user_id) or {}
        if win in cached:
            perf = dict(cached[win])
        else:
            perf = await deal_stats.leader_batch([str(primary.get("node_id"))], deal_stats.window_start(win), None)

    follower_count = len({
        f["node_id"] for f in await follow_service.list_for_leaders({leader_user_id})
        if group_id is None or f["group_id"] == group_id
    })
    # 策略名称：与列表接口同一口径（按分组顺序去重，取第一个作主标题）
    strategy_names: list[str] = []
    for g in groups:
        sname = (strategies.get(g.get("strategy_id") or "") or {}).get("name")
        if sname and sname not in strategy_names:
            strategy_names.append(str(sname))

    # 近 7 日每日收益（柱状图数据源）。
    # 与跟单详情同一个 `daily_net_profit`、同一个主节点，口径天然一致 ——
    # 两个市场各算一次必然漂移（「同一节点在跟单页与 AI 页收益不同」）。
    # 同样不回源缓存：日序列与四窗口汇总结构不同，塞进 K_ROBOT_STATS 会污染缓存语义。
    daily_profit = await deal_stats.daily_net_profit(
        [str(primary.get("node_id"))], days=7, node_magics=node_magics,
    )

    return AiRobotDetailResponse(
        robot={
            "leader_user_id": leader_user_id,
            "username": user.username,
            "display_name": user.display_name,
            "primary_node": _node_brief(primary),
            "ai_group_count": len(groups),
            "strategy_name": strategy_names[0] if strategy_names else None,
            "strategy_names": strategy_names,
            "follower_node_count": follower_count,
            "my_follow_group_count": my_group_count,
            "my_follow_node_count": my_node_count,
            "max_allow_num": max(max_allow) if max_allow else None,
            "total_lot_limit": max(lot_limits) if lot_limits else None,
            "marketable": True,
            "performance": perf,
            "perf_window": win,
            "follower_count": follower_count,
            # 暂停 AI 跟单状态（详情页据此显示「暂停 / 恢复 AI 跟单」按钮）
            "ai_follow_paused": bool(getattr(user, "ai_follow_paused", False)),
            **(_group_robot_summary(user, primary, groups[0], strategies.get(groups[0].get("strategy_id") or ""), p) if len(groups) == 1 else {}),
        },
        groups=detail_groups,
        my_nodes=[{
            **_node_brief(n),
            "online": n["node_id"] in manager.nodes,
            "balance": (await store.get_account(n["node_id"]) or {}).get("balance"),
            "owner_name": p.username,
            "selectable": not any(n["node_id"] == member.get("node_id") for group in groups for member in group.get("members", [])),
            "unselectable_reason": "已是该机器人分组成员" if any(n["node_id"] == member.get("node_id") for group in groups for member in group.get("members", [])) else None,
        } for n in _viewable_follow_nodes(all_nodes, p)],
        daily_profit=daily_profit,
    )


# ---------------------------------------------------------------------------
# 我的 AI 跟单
# ---------------------------------------------------------------------------
@router.get("/me", response_model=AiMeResponse)
async def list_my_ai_followings(
    store: RedisStore = Depends(get_store),
    p: Principal = Depends(require_menu(MENU_AI_TRADE)),
) -> AiMeResponse:
    """我的机器人：合并自有主节点上的 AI 策略与既有 AI 跟单。"""
    followings = await follow_service.list_followings(p.user_id, include_stopped=True)
    pairs = {(item["group_id"], item["node_id"]) for item in followings}
    followings += [item for item in await ai_application_store.legacy_relations(p.user_id)
                   if (item["group_id"], item["node_id"]) not in pairs]
    all_groups = {g["group_id"]: g for g in await store.all_groups()}
    all_nodes = await store.all_nodes()
    active_counts = await group_persist.count_active_by_group()
    strategies = await follow_service._strategies_of(list(all_groups.values()), store)
    tmpl_map = follow_service._template_ids_of(strategies)
    owner = permissions.owner_for_new(p)
    own_primary = _primary_of(all_nodes, owner)
    owned_group_ids = {
        gid for gid, group in all_groups.items()
        if permissions.same_owner(group.get("owner_user_id"), owner)
        and follow_rules.group_contains_primary(
            group, own_primary.get("node_id") if own_primary else None,
        )
        and follow_rules.template_in_market(
            tmpl_map.get(str(group.get("strategy_id") or "")),
            follow_rules.AI_MARKET_TEMPLATE_IDS,
        )
    }

    by_leader: dict[int, list[dict]] = {}
    for f in followings:
        by_leader.setdefault(int(f["leader_user_id"]), []).append(f)

    # 市场隔离：AI tab 只管 **AI 市场**（tpl_1）的分组。
    # 带单市场（tpl_2/tpl_3）的分组归【带单】tab —— 在这里显示会让跟单员以为
    # 自己在跟 AI 机器人，实际却在跟网格分组（而且移出会移错组）。
    # 模版未知的分组（未绑策略 / 策略被删除）保持可见，与既有口径一致。
    ai_group_ids: set[str] = {
        gid for gid, g in all_groups.items()
        if not follow_rules.leave_market_skip_reason(
            tmpl_map.get(str(g.get("strategy_id") or "")),
            follow_rules.AI_MARKET_TEMPLATE_IDS,
        )
    }
    by_leader = {
        lid: [f for f in items if f["group_id"] in ai_group_ids]
        for lid, items in by_leader.items()
    }
    by_leader = {lid: items for lid, items in by_leader.items() if items}
    following_group_count = len({item["group_id"] for items in by_leader.values() for item in items})
    if owned_group_ids:
        by_leader.setdefault(p.user_id, [])
    if not by_leader:
        return AiMeResponse(
            following=[], summary={"following_count": 0, "follow_node_count": 0},
            applications=await ai_application_store.list_mine(p.user_id),
        )

    following_out: list[dict] = []
    for lid, items in sorted(by_leader.items()):
        user = await user_service.get_user_by_id(lid)
        if not user:
            continue
        principal = await rbac_service.load_principal(store, user.username)
        has_cap = bool(principal) and permissions.can_host_ai_robot(principal)
        is_owner = lid == p.user_id and bool(owned_group_ids)
        primary = own_primary if is_owner else _primary_of(all_nodes, lid)

        by_group: dict[str, list[dict]] = {}
        for it in items:
            by_group.setdefault(it["group_id"], []).append(it)
        if is_owner:
            for gid in sorted(owned_group_ids):
                by_group.setdefault(gid, [])

        for gid, fs in by_group.items():
            g = all_groups.get(gid)
            if not g:
                continue  # 分组已被删除（理论上 delete_group 已级联），跳过
            reason = follow_rules.marketable_reason(
                g, has_cap, primary.get("node_id") if primary else None,
                template_id=tmpl_map.get(str(g.get("strategy_id") or "")),
                market=follow_rules.AI_MARKET_TEMPLATE_IDS,
                # 主人暂停 AI 跟单 -> 存量关系保留但标不可再收新信号
                paused=bool(getattr(user, "ai_follow_paused", False)),
                ai=True,
            )
            # AI 特有：分组未开放 AI 跟单时，存量关系保留但标不可跟
            if reason is None and not g.get("accept_ai_follow"):
                reason = "该机器人已关闭 AI 跟单，不再接收新信号"
            risk = _risk_summary(strategies.get(g.get("strategy_id") or ""))
            group_out = {
                "group_id": gid,
                "group_name": g.get("name"),
                "symbol": (strategies.get(g.get("strategy_id") or "") or {}).get("symbol") or g.get("strategy_symbol"),
                "strategy_id": g.get("strategy_id"),
                "strategy_name": (strategies.get(g.get("strategy_id") or "") or {}).get("name"),
                "marketable": reason is None,
                "unmarketable_reason": reason,
                "active_task_count": active_counts.get(gid, 0),
                "batch_used": 0,
                "batch_limit": int(risk.get("max_allow_num") or 0),
                "my_nodes": [
                    {
                        **_node_brief(next(
                            (n for n in all_nodes if n.get("node_id") == f["node_id"]), None
                        )),
                        "joined_at": f.get("joined_at"),
                        "follow_state": "paused" if f.get("leader_paused") and f.get("follow_state") != "stopped" else f.get("follow_state") or "running",
                        "leader_paused": bool(f.get("leader_paused")),
                        "settings": public_settings(f.get("settings")),
                    }
                    for f in sorted(fs, key=lambda x: x.get("joined_at") or 0)
                ],
            }
            my_group_count, my_node_count = follow_rules.my_follow_counts_in_market(
                fs, all_groups, follow_rules.AI_MARKET_TEMPLATE_IDS, tmpl_map,
            )
            following_out.append({
                "is_owner": lid == p.user_id and gid in owned_group_ids,
                "robot": {
                    **_group_robot_summary(user, primary, g, strategies.get(g.get("strategy_id") or ""), p),
                    "my_follow_group_count": my_group_count,
                    "my_follow_node_count": my_node_count,
                    "marketable": has_cap,
                },
                "groups": [group_out],
                "review_status": next((f["review_status"] for f in fs if f.get("review_status")), None),
            })
    return AiMeResponse(
        following=following_out,
        applications=await ai_application_store.list_mine(p.user_id),
        summary={
            "following_count": following_group_count,
            # 计数也只算 AI 市场的关系（与上面过滤后的口径一致）
            "follow_node_count": sum(len(items) for items in by_leader.values()),
        },
    )


# ---------------------------------------------------------------------------
# AI 跟单子节点统计与控制
# ---------------------------------------------------------------------------
@router.get("/nodes", response_model=AiFollowNodesResponse)
async def list_ai_follow_nodes(
    store: RedisStore = Depends(get_store),
    p: Principal = Depends(require_menu(MENU_AI_TRADE)),
):
    return await ai_follow_nodes.node_stats(store, p)


@router.post("/nodes/state")
async def set_ai_follow_node_state(
    body: AiFollowNodeStateRequest, request: Request,
    store: RedisStore = Depends(get_store),
    p: Principal = Depends(require_menu(MENU_AI_TRADE)),
):
    return await ai_follow_nodes.set_node_state(
        store, p, body.node_ids, body.state, client_ip(request), group_id=body.group_id,
    )


@router.post("/nodes/close")
async def close_ai_follow_nodes(
    body: AiFollowNodesRequest, request: Request,
    store: RedisStore = Depends(get_store),
    dispatcher=Depends(get_group_dispatcher),
    p: Principal = Depends(require_menu(MENU_AI_TRADE)),
):
    return await ai_follow_nodes.close_nodes(store, p, body.node_ids, request, dispatcher, client_ip(request))


# ---------------------------------------------------------------------------
# 我的机器人详情
# ---------------------------------------------------------------------------
@router.get("/me/{leader_user_id}", response_model=AiMyRobotDetailResponse)
async def get_my_ai_robot_detail(
    leader_user_id: int,
    group_id: Optional[str] = None,
    store: RedisStore = Depends(get_store),
    p: Principal = Depends(require_menu(MENU_AI_TRADE)),
) -> AiMyRobotDetailResponse:
    mine = await list_my_ai_followings(store=store, p=p)
    entries = [item for item in mine.following if item.robot.leader_user_id == leader_user_id
               and (group_id is None or item.robot.group_id == group_id)]
    if not entries:
        raise HTTPException(status_code=404, detail="机器人不存在或不属于我的机器人")
    if len(entries) > 1:
        raise HTTPException(status_code=400, detail="该主人有多个机器人，请指定 group_id")
    entry = entries[0]
    data = await ai_robot_detail.build_detail(store, p, entry)
    return AiMyRobotDetailResponse(**entry.model_dump(), **data)


# ---------------------------------------------------------------------------
# 加入 AI 跟单
# ---------------------------------------------------------------------------
@router.post("/join", response_model=AiJoinResponse)
async def join_ai_follow(
    body: AiJoinRequest,
    request: Request,
    store: RedisStore = Depends(get_store),
    p: Principal = Depends(require_menu(MENU_AI_TRADE)),
) -> AiJoinResponse:
    """提交 AI 接入申请，审核通过前不建立跟单关系。"""
    try:
        return AiJoinResponse(**await ai_application_store.submit(
            store, p, body.leader_user_id, body.group_ids, body.node_ids, ip=client_ip(request),
            settings=body.settings.model_dump(),
        ))
    except HTTPException as exc:
        from .node_deletion import failed
        await failed(p, "ai_follow_request_create", body.leader_user_id, exc, client_ip(request))
        if exc.status_code == 409:
            raise HTTPException(400, exc.detail) from exc
        raise


@router.post("/ui-events")
async def ui_event(body: AiUiEvent, request: Request,
                   p: Principal = Depends(require_menu(MENU_AI_TRADE))):
    async with SessionLocal() as session:
        ai_application_store.audit(session, p, f"ai_ui_{body.action}", body.group_id,
                                  params=body.model_dump(), ip=client_ip(request))
        await session.commit()
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# 暂停 / 恢复 AI 跟单
# ---------------------------------------------------------------------------
@router.post("/pause", response_model=AiPauseResponse)
async def set_ai_follow_paused(
    body: AiPauseRequest,
    request: Request,
    store: RedisStore = Depends(get_store),
    p: Principal = Depends(require_menu(MENU_AI_TRADE)),
) -> AiPauseResponse:
    """暂停 / 恢复【AI 交易】市场的整组新开仓与对外接单。

    语义：
    ① 新的客户无法再加入该机器人主人名下的 AI 分组（写侧拦截 + 展示侧置灰）；
    ② 已有跟单关系与在跑任务**不动** —— 等跟单员自行移出，本接口不碰
       任何跟单数据、不平任何仓；
    ③ 主人名下 AI 分组的所有节点均不再接收新开仓，包括没有跟单成员的分组。
       已有任务继续运行，CLOSE / 平仓仍可执行。

    只作用于【AI 交易】市场（AI 智能加仓分组）。带单市场（趋势 / 网格分组）
    有独立的开关（/api/follow/pause），两边互不影响 —— 同一个主人可能同时
    做普通带单与 AI 机器人，停一边不该连累另一边。

    权限：只能操作自己（管理员可代操作他人）。必须仍持有机器人主人资格。
    """
    if body.leader_user_id != p.user_id and not p.is_admin:
        raise HTTPException(status_code=403, detail="只能暂停自己的 AI 跟单")

    user = await user_service.get_user_by_id(body.leader_user_id)
    if not user or not user.is_active:
        raise HTTPException(status_code=404, detail="机器人主人不存在或已停用")

    principal = await rbac_service.load_principal(store, user.username)
    if not principal or not permissions.can_host_ai_robot(principal):
        raise HTTPException(status_code=400, detail="该用户暂未开放 AI 跟单，无法暂停")

    before = bool(getattr(user, "ai_follow_paused", False))
    if before == body.paused:
        # 幂等：状态没变就不写库（避免审计日志刷屏）
        return AiPauseResponse(
            leader_user_id=user.id,
            ai_follow_paused=before,
            paused_at=_paused_at(user.ai_follow_paused_at) if before else None,
        )

    updated = await user_service.set_ai_follow_paused(user.id, body.paused)
    if updated is None:
        raise HTTPException(status_code=404, detail="机器人主人不存在")

    await persist.audit(
        p.username,
        "pause_ai_follow" if body.paused else "resume_ai_follow",
        user.username,
        {"leader_user_id": user.id, "paused": body.paused},
        "ok", client_ip(request), category="ai_follow",
        before={"ai_follow_paused": before},
        after={"ai_follow_paused": body.paused},
    )
    return AiPauseResponse(
        leader_user_id=updated.id,
        ai_follow_paused=bool(updated.ai_follow_paused),
        paused_at=_paused_at(updated.ai_follow_paused_at) if updated.ai_follow_paused else None,
    )


def _paused_at(value: object) -> Optional[float]:
    """暂停时刻 -> Unix 秒（供前端展示「暂停于 …」）。"""
    if value is None:
        return None
    ts = getattr(value, "timestamp", None)
    try:
        return float(ts()) if callable(ts) else None
    except (TypeError, ValueError, OSError):
        return None


# ---------------------------------------------------------------------------
# 移出 AI 跟单
# ---------------------------------------------------------------------------
@router.post("/leave", response_model=AiLeaveResponse)
async def leave_ai_follow(
    body: AiLeaveRequest,
    request: Request,
    store: RedisStore = Depends(get_store),
    p: Principal = Depends(require_menu(MENU_AI_TRADE)),
) -> AiLeaveResponse:
    """把当前用户的若干节点移出某个 AI 机器人的若干分组。

    不要求机器人主人仍有能力 —— 跟单员必须始终能自行清理自己的节点。

    close_tasks 默认 False：**在跑任务不停止，继续按策略运行至结束**
    （任务执行由 node_client 负责，后端只解除跟单关系、不再下发新信号）。

    **市场隔离**：只允许移出 **AI 市场**的分组。带单市场（tpl_2/tpl_3）的分组
    归【带单】tab 管 —— 在这里删掉会连带清掉跟单员在带单侧的关系。
    两个市场共用同一张关系表，所以校验必须在**写入侧**。
    跨市场的分组会被跳过（关系不动）并在 skipped 里回报原因。
    """
    from .follow_api import _stop_following_subtasks

    removed: list[dict] = []
    closing: list[dict] = []
    forced: list[dict] = []
    skipped: list[dict] = []
    stopped = 0

    # 一次取全部分组 + 模版快照，循环内不逐个回源查 Redis
    all_groups = {g["group_id"]: g for g in await store.all_groups()}
    strategies = await follow_service._strategies_of(list(all_groups.values()), store)
    tmpl_map = follow_service._template_ids_of(strategies)

    for gid in body.group_ids:
        gid = str(gid or "").strip()
        if not gid:
            continue
        group = all_groups.get(gid) or await store.get_group(gid)
        if not group:
            continue
        # 市场校验：带单市场分组不在 AI 市场 -> 跳过，关系保持不变
        skip_reason = follow_rules.leave_market_skip_reason(
            tmpl_map.get(str(group.get("strategy_id") or "")),
            follow_rules.AI_MARKET_TEMPLATE_IDS,
        )
        if skip_reason:
            skipped.append({
                "group_id": gid,
                "group_name": group.get("name"),
                "reason": skip_reason,
            })
            continue
        node_ids = [
            nid for nid in (body.node_ids or [])
            if follow_service.follows_node(group, nid, p.user_id)
        ]
        if not node_ids:
            continue
        if body.close_tasks:
            for nid in node_ids:
                outcome = await _stop_following_subtasks(store, gid, nid)
                if outcome["status"] == "closing":
                    closing.append(outcome)
                elif outcome["status"] == "forced":
                    forced.append(outcome)
                if outcome["status"] != "none":
                    stopped += 1
        await follow_service.remove_following(
            store, group_id=gid, node_ids=node_ids, follower_user_id=p.user_id,
        )
        removed.append({"group_id": gid, "node_ids": node_ids})

    warning = None
    if stopped and body.close_tasks is False:
        warning = "存在进行中的任务，移出后不会停止，将继续按策略运行至结束"

    user = await user_service.get_user_by_id(body.leader_user_id)
    await persist.audit(
        p.username, "leave_ai_follow", (user or _Stub(body.leader_user_id)).username,
        {
            "leader_user_id": body.leader_user_id,
            "group_ids": [r["group_id"] for r in removed],
            "node_ids": body.node_ids,
            "close_tasks": body.close_tasks,
            "stopped_subtasks": stopped,
            "skipped_group_ids": [s["group_id"] for s in skipped],
        },
        "ok", client_ip(request), category="ai_follow",
        before=None,
        after={"removed": removed, "closing": closing, "forced": forced, "skipped": skipped},
    )
    return AiLeaveResponse(
        removed=removed, closing=closing, forced=forced, warning=warning,
        skipped=skipped,
    )
