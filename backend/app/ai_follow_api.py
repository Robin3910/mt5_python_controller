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
    deal_stats,
    follow_rules,
    follow_service,
    group_persist,
    permissions,
    persist,
    rbac_service,
    user_service,
)
from .deps import client_ip, get_store, require_menu
from .models import (
    AiJoinRequest,
    AiJoinResponse,
    AiLeaveRequest,
    AiLeaveResponse,
    AiMeResponse,
    AiMyGroup,
    AiMyRobot,
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
        "online": bool(node.get("online", False)),
    }


def _primary_of(all_nodes: list[dict], user_id: int) -> Optional[dict]:
    """某用户的主节点。

    委托 `follow_rules.primary_node_of` —— 与带单广场共用同一实现，
    两边口径本就相同（都是主节点口径），各写一遍必然漂移。
    """
    return follow_rules.primary_node_of(all_nodes, user_id)


def _viewable_follow_nodes(all_nodes: list[dict], principal: Principal) -> list[dict]:
    """当前用户可用于跟单的节点：自己的、已启用、**非主节点**。

    主节点必须排除 —— 它同时是带单源（AI 机器人的载体），保持独立才有
    「机器人 / 跟单」的方向性。
    """
    return [
        n for n in all_nodes
        if permissions.normalize_owner(n.get("owner_user_id")) == principal.user_id
        and n.get("enabled", True)
        and not n.get("is_primary")
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
    """AI 机器人广场：持 ai_strategy + follow_leader + 有主节点 + 至少一个已开放 AI 跟单的 tpl_1 分组。

    节点 / 分组 / 跟单关系都**批量预取一次**再在内存里筛，避免 N+1。

    绩效口径 = **主节点**的成交聚合（AI 机器人的展示单位就是主节点）。
    只读 deal_agg 预聚合的 Redis 缓存，**未命中不回源查库** —— 与带单广场
    同一套策略，保证列表接口延迟恒定。
    """
    users = await user_service.list_users()
    hosts = await _ai_host_ids_with_capability(store, users)
    if not hosts:
        return AiRobotListResponse(robots=[])

    all_groups = await store.all_groups()
    all_nodes = await store.all_nodes()
    strategies = await follow_service._strategies_of(all_groups, store)
    tmpl_map = follow_service._template_ids_of(strategies)

    mine_by_leader: dict[int, list[dict]] = {}
    for f in await follow_service.list_followings(p.user_id):
        mine_by_leader.setdefault(int(f["leader_user_id"]), []).append(f)
    followers_by_leader: dict[int, set[str]] = {}
    for f in await follow_service.list_for_leaders(hosts):
        followers_by_leader.setdefault(int(f["leader_user_id"]), set()).add(f["node_id"])

    # 绩效：一次 MGET 拿全部机器人的主节点预聚合结果（未命中就是 None）
    win = window if window in deal_stats.WINDOWS else deal_stats.DEFAULT_WINDOW
    cached: dict[int, dict] = {}
    for uid in hosts:
        entry = await store.get_robot_stats(uid)
        if entry and win in entry:
            cached[uid] = entry[win]

    by_id = {u.id: u for u in users}
    needle = q.strip().lower()
    out: list[dict] = []
    for uid in hosts:
        u = by_id.get(uid)
        if not u:
            continue
        primary = _primary_of(all_nodes, uid)
        if not primary:
            continue
        groups = _ai_groups_of(all_groups, tmpl_map, uid, primary.get("node_id"))
        if not groups:
            continue          # 没有已开放 AI 跟单的分组 -> 不进广场
        if needle:
            haystack = " ".join(str(x) for x in [
                u.username, u.display_name or "",
                primary.get("name") or "", primary.get("mt5_login") or "",
            ]).lower()
            if needle not in haystack:
                continue
        mine = mine_by_leader.get(uid, [])
        # 风险上限取各分组里的最大值，让用户一眼看到风险量级
        risk = [
            _risk_summary(strategies.get(g.get("strategy_id") or "")) for g in groups
        ]
        max_allow = [r["max_allow_num"] for r in risk if r["max_allow_num"] is not None]
        lot_limits = [r["total_lot_limit"] for r in risk if r["total_lot_limit"] is not None]
        # 策略名称（列表标题用）：按分组顺序去重，取第一个作为主标题。
        # 分组未绑策略 / 策略已删除时该名字为空 —— 此时列表不显示标题文字，
        # 前端会回落到用户名，不展示空白标题。
        strategy_names: list[str] = []
        for g in groups:
            sname = (strategies.get(g.get("strategy_id") or "") or {}).get("name")
            if sname and sname not in strategy_names:
                strategy_names.append(str(sname))
        # 绩效：主节点口径的预聚合（缓存未命中 -> 空统计，前端显示「暂无数据」）
        perf = dict(cached.get(uid) or {})
        out.append({
            "leader_user_id": uid,
            "username": u.username,
            "display_name": u.display_name,
            "primary_node": _node_brief(primary),
            "ai_group_count": len(groups),
            "strategy_name": strategy_names[0] if strategy_names else None,
            "strategy_names": strategy_names,
            "follower_node_count": len(followers_by_leader.get(uid, set())),
            "my_follow_group_count": len({f["group_id"] for f in mine}),
            "my_follow_node_count": len(mine),
            "max_allow_num": max(max_allow) if max_allow else None,
            "total_lot_limit": max(lot_limits) if lot_limits else None,
            "marketable": True,
            "performance": perf,
            "perf_window": win,
            # 跟单人数取预聚合里的值，回落到跟单关系表实时统计
            "follower_count": len(followers_by_leader.get(uid, set())),
        })
    # 已跟过的排前面，其余按用户名
    out.sort(key=lambda x: (x["my_follow_group_count"] == 0, x["username"]))
    return AiRobotListResponse(robots=out)


# ---------------------------------------------------------------------------
# 机器人详情
# ---------------------------------------------------------------------------
@router.get("/robots/{leader_user_id}", response_model=AiRobotDetailResponse)
async def get_ai_robot_detail(
    leader_user_id: int,
    window: str = "30d",
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
    signal_counts = await group_persist.count_by_group()
    active_counts = await group_persist.count_active_by_group()

    mine_by_group: dict[str, list[str]] = {}
    for f in await follow_service.list_followings(p.user_id):
        if int(f["leader_user_id"]) == leader_user_id:
            mine_by_group.setdefault(f["group_id"], []).append(f["node_id"])

    detail_groups: list[dict] = []
    for g in sorted(groups, key=follow_rules.leader_group_sort_key):
        gid = g["group_id"]
        strategy = strategies.get(g.get("strategy_id") or "")
        reason = None
        if not g.get("enabled", True):
            reason = "分组已禁用"
        elif strategy is None:
            reason = "已停止接收新信号（策略已停用）"
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

    mine = mine_by_group
    risk_all = [_risk_summary(strategies.get(g.get("strategy_id") or "")) for g in groups]
    max_allow = [r["max_allow_num"] for r in risk_all if r["max_allow_num"] is not None]
    lot_limits = [r["total_lot_limit"] for r in risk_all if r["total_lot_limit"] is not None]

    # 绩效：主节点口径，与列表同一份缓存（口径完全一致）。
    # 详情是单个人，缓存刚建立时可能还没有 —— 允许就地回源算一次兜底。
    win = window if window in deal_stats.WINDOWS else deal_stats.DEFAULT_WINDOW
    perf: dict = {}
    cached = await store.get_robot_stats(leader_user_id) or {}
    if win in cached:
        perf = dict(cached[win])
    else:
        perf = await deal_stats.leader_batch([str(primary.get("node_id"))], deal_stats.window_start(win), None)

    follower_count = len({
        f["node_id"] for f in await follow_service.list_for_leaders({leader_user_id})
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
        [str(primary.get("node_id"))], days=7,
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
            "my_follow_group_count": len(mine),
            "my_follow_node_count": sum(len(v) for v in mine.values()),
            "max_allow_num": max(max_allow) if max_allow else None,
            "total_lot_limit": max(lot_limits) if lot_limits else None,
            "marketable": True,
            "performance": perf,
            "perf_window": win,
            "follower_count": follower_count,
        },
        groups=detail_groups,
        my_nodes=[_node_brief(n) for n in _viewable_follow_nodes(all_nodes, p)],
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
    """我的 AI 跟单：按机器人主人分组；主人撤销能力时标 unmarketable 但仍可自行移出。"""
    followings = await follow_service.list_followings(p.user_id)
    if not followings:
        return AiMeResponse(
            following=[], summary={"following_count": 0, "follow_node_count": 0},
        )
    all_groups = {g["group_id"]: g for g in await store.all_groups()}
    all_nodes = await store.all_nodes()
    active_counts = await group_persist.count_active_by_group()
    strategies = await follow_service._strategies_of(list(all_groups.values()), store)
    tmpl_map = follow_service._template_ids_of(strategies)

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
    if not by_leader:
        return AiMeResponse(
            following=[], summary={"following_count": 0, "follow_node_count": 0},
        )

    following_out: list[dict] = []
    for lid, items in sorted(by_leader.items()):
        user = await user_service.get_user_by_id(lid)
        if not user:
            continue
        principal = await rbac_service.load_principal(store, user.username)
        has_cap = bool(principal) and permissions.can_host_ai_robot(principal)
        primary = _primary_of(all_nodes, lid)

        by_group: dict[str, list[dict]] = {}
        for it in items:
            by_group.setdefault(it["group_id"], []).append(it)

        groups_out: list[dict] = []
        for gid, fs in by_group.items():
            g = all_groups.get(gid)
            if not g:
                continue  # 分组已被删除（理论上 delete_group 已级联），跳过
            reason = follow_rules.marketable_reason(
                g, has_cap, primary.get("node_id") if primary else None,
                template_id=tmpl_map.get(str(g.get("strategy_id") or "")),
                market=follow_rules.AI_MARKET_TEMPLATE_IDS,
            )
            # AI 特有：分组未开放 AI 跟单时，存量关系保留但标不可跟
            if reason is None and not g.get("accept_ai_follow"):
                reason = "该机器人已关闭 AI 跟单，不再接收新信号"
            risk = _risk_summary(strategies.get(g.get("strategy_id") or ""))
            groups_out.append({
                "group_id": gid,
                "group_name": g.get("name"),
                "symbol": g.get("strategy_symbol") or risk.get("symbol"),
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
                    }
                    for f in sorted(fs, key=lambda x: x.get("joined_at") or 0)
                ],
            })
        following_out.append({
            "robot": {
                "leader_user_id": lid,
                "username": user.username,
                "display_name": user.display_name,
                "primary_node": _node_brief(primary),
                "ai_group_count": len(by_group),
                # 策略名称：与广场列表同一口径（按分组顺序去重，取第一个作主标题）
                "strategy_name": (groups_out[0].get("strategy_name") if groups_out else None),
                "strategy_names": [
                    n for n in dict.fromkeys(
                        str(g.get("strategy_name")) for g in groups_out if g.get("strategy_name")
                    )
                ],
                "follower_node_count": 0,
                "my_follow_group_count": len(by_group),
                "my_follow_node_count": len(items),
                "marketable": has_cap,
            },
            "groups": groups_out,
        })
    return AiMeResponse(
        following=following_out,
        summary={
            "following_count": len(by_leader),
            # 计数也只算 AI 市场的关系（与上面过滤后的口径一致）
            "follow_node_count": sum(len(items) for items in by_leader.values()),
        },
    )


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
    """把当前用户的若干节点加入某个 AI 机器人的若干分组。

    校验顺序固定：机器人主人有效性 -> 节点归属/主节点 -> 分组可跟单性。
    越权与不存在一律 4xx，不透露对方名下有什么分组。

    **市场隔离**：只接受 tpl_1 分组；且该分组必须已开放 AI 跟单
    （accept_ai_follow）—— 开关校验在**写入侧**，不能只在展示侧过滤，
    否则用户可绕过 UI 直接调 API 加入未开放的分组。
    """
    user = await user_service.get_user_by_id(body.leader_user_id)
    if not user or not user.is_active:
        raise HTTPException(status_code=404, detail="机器人主人不存在或已停用")
    principal = await rbac_service.load_principal(store, user.username)
    if not principal or not permissions.can_host_ai_robot(principal):
        raise HTTPException(status_code=404, detail="该用户暂未开放 AI 跟单")

    all_nodes = await store.all_nodes()
    leader_primary = _primary_of(all_nodes, body.leader_user_id)
    if not leader_primary:
        raise HTTPException(status_code=404, detail="该机器人主人尚无主节点")

    # 节点：必须是我的、启用、非主节点
    my_follow_nodes = {
        n["node_id"]: n for n in _viewable_follow_nodes(all_nodes, p)
    }
    node_ids: list[str] = []
    skipped: list[dict] = []
    for nid in body.node_ids:
        nid = str(nid or "").strip()
        if not nid or nid in node_ids:
            continue
        if nid in my_follow_nodes:
            node_ids.append(nid)
        else:
            reason = follow_rules.node_join_reject_reason(
                next((n for n in all_nodes if n.get("node_id") == nid), None),
                existing_group=None,
                group_id="",
            )
            skipped.append({"node_id": nid, "reason": reason or "节点不属于你"})
    if not node_ids:
        raise HTTPException(status_code=400, detail="没有可用于跟单的节点（主节点不可跟单）")

    # 分组：必须是该主人名下、含其主节点、启用、tpl_1、且已开放 AI 跟单
    all_groups = await store.all_groups()
    strategies = await follow_service._strategies_of(all_groups, store)
    tmpl_map = follow_service._template_ids_of(strategies)
    followable = {
        g["group_id"]: g for g in _ai_groups_of(
            all_groups, tmpl_map, body.leader_user_id, leader_primary.get("node_id"),
        )
    }
    joined: list[dict] = []
    for gid in body.group_ids:
        gid = str(gid or "").strip()
        g = followable.get(gid)
        if not g:
            continue  # 不可跟的分组静默跳过，joined 里不出现
        if g.get("strategy_id") and g["strategy_id"] not in strategies:
            continue
        added: list[str] = []
        already: list[str] = []
        for nid in node_ids:
            reason = follow_rules.node_join_reject_reason(
                my_follow_nodes[nid], existing_group=g, group_id=gid,
            )
            if reason:
                skipped.append({"node_id": nid, "group_id": gid, "reason": reason})
                continue
            ok = await follow_service.add_following(
                store,
                group_id=gid, node_id=nid,
                follower_user_id=p.user_id,
                leader_user_id=body.leader_user_id,
                leader_primary_node_id=leader_primary.get("node_id"),
            )
            (added if ok else already).append(nid)
            g.setdefault("followers", []).append({"node_id": nid})
        if added or already:
            joined.append({
                "group_id": gid,
                "group_name": g.get("name"),
                "node_ids": added,
                "already_node_ids": already,
            })

    if not joined:
        raise HTTPException(status_code=400, detail="没有可加入的分组（可能已全部跟过或已失效）")

    await persist.audit(
        p.username, "join_ai_follow", user.username,
        {
            "leader_user_id": body.leader_user_id,
            "leader_primary_node_id": leader_primary.get("node_id"),
            "group_ids": [j["group_id"] for j in joined],
            "node_ids": node_ids,
        },
        "ok", client_ip(request), category="ai_follow",
        before=None,
        after={"joined": joined, "skipped": skipped},
    )
    return AiJoinResponse(
        joined=joined,
        skipped=skipped,
        leader_primary_node_id=leader_primary.get("node_id"),
    )


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
