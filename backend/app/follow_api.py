"""带单（跟单）API。

- 带单员列表 / 详情 / 我的跟单（只读，鉴权 = require_menu(MENU_FOLLOW)）
- 加入 / 移出跟单（鉴权同上；**不需要** follow_leader 能力 —— 跟单方向人人可做）

方向性约定（改动前请先读）：
- 我能否**带单**给他人 → 需要 follow_leader 角色能力；
- 我能否**跟**别人的单 → 只需要能进【带单】菜单（assignable，默认人人有）；
- 带单源永远是带单员的**主节点**，且分组必须包含该主节点才算带单源分组。

数据来源：分组 / 节点读 Redis 缓存，跟单关系读库，带单员能力按用户名现算
（`load_principal` 带缓存），避免管理员刚撤销能力却仍对外可见。
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
    FollowDetailOut,
    FollowJoinRequest,
    FollowJoinResponse,
    FollowLeaveRequest,
    FollowLeaveResponse,
    FollowLeaderListResponse,
    FollowMeResponse,
)
from .permissions import MENU_FOLLOW, Principal
from .redis_store import RedisStore

router = APIRouter(prefix="/api/follow", tags=["follow"])


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------
def _node_brief(node: Optional[dict]) -> Optional[dict]:
    """节点精简展示（带单员摘要 / 我的可跟单节点）。

    只给跟单员判断「这单靠不靠谱」所需的最小信息，不暴露对方账户细节。
    """
    if not node:
        return None
    return {
        "node_id": node.get("node_id"),
        "name": node.get("name"),
        "mt5_login": node.get("mt5_login"),
        "status": node.get("status"),
        "enabled": bool(node.get("enabled", True)),
        "is_primary": bool(node.get("is_primary")),
    }


def _owned_nodes(all_nodes: list[dict], user_id: int) -> list[dict]:
    return [
        n for n in all_nodes
        if permissions.normalize_owner(n.get("owner_user_id")) == user_id
    ]


def _primary_of(all_nodes: list[dict], user_id: int) -> Optional[dict]:
    """某用户的主节点（带单源）。没有主节点 = 不对外接单的资格。"""
    return next((n for n in _owned_nodes(all_nodes, user_id) if n.get("is_primary")), None)


def _viewable_follow_nodes(all_nodes: list[dict], principal: Principal) -> list[dict]:
    """当前用户可用于跟单的节点：自己的、已启用、**非主节点**。

    主节点必须排除 —— 它同时是带单源，保持独立才有「带单 / 跟单」的方向性。
    服务端在此拦一次，join 时再按 nodes 表兜一次（防旧客户端）。
    """
    return [
        n for n in all_nodes
        if permissions.normalize_owner(n.get("owner_user_id")) == principal.user_id
        and n.get("enabled", True)
        and not n.get("is_primary")
    ]


def _count_online(group: dict) -> int:
    """分组中在线的节点数（含 owner 成员 + 跟单成员），用于粗判「这单响不响应」。"""
    members = (group.get("members") or []) + (group.get("followers") or [])
    return sum(1 for m in members if isinstance(m, dict) and m.get("status") == "online")


async def _strategies_of(groups: list[dict], store: RedisStore) -> dict[str, dict]:
    """分组绑定的策略快照 {strategy_id: strategy}（只取这些分组用到的）。

    读缓存，缺失的策略按「已停用」处理（调用方据此把分组标为不可选），
    不为一次列表请求去逐个回源查库。
    """
    return await follow_service._strategies_of(groups, store)


def _template_ids_of(strategies: dict[str, dict]) -> dict[str, str]:
    """{strategy_id: strategy} -> {strategy_id: template_id}（跟单市场判定用）。

    缺失的策略（已删除）不会出现在入参里，调用方据此把分组判为不可跟（保守）。
    """
    return follow_service._template_ids_of(strategies)


async def _leader_ids_with_capability(
    store: RedisStore, users: list,
) -> set[int]:
    """持有 follow_leader 能力且启用的用户 ID（含自己，用于带单界面自跟单）。"""
    out: set[int] = set()
    for u in users:
        if not u.is_active:
            continue
        principal = await rbac_service.load_principal(store, u.username)
        if principal and permissions.can_be_leader(principal):
            out.add(u.id)
    return out


# ---------------------------------------------------------------------------
# 带单员列表
# ---------------------------------------------------------------------------
@router.get("/leaders", response_model=FollowLeaderListResponse)
async def list_follow_leaders(
    q: str = "",
    window: str = "30d",
    store: RedisStore = Depends(get_store),
    p: Principal = Depends(require_menu(MENU_FOLLOW)),
) -> FollowLeaderListResponse:
    """带单员列表：持 follow_leader 能力 + 有主节点 + 至少一个可跟单分组（含自己，用于自跟单）。

    节点 / 分组 / 我的跟单关系都**批量预取一次**再在内存里筛，
    避免「每个用户查一次 Redis」的 N+1。

    绩效来自后台预聚合的 Redis 缓存（deal_agg）。**未命中不回源查库** ——
    带单广场一次要展示 N 个带单员，回源就是 N+1 查询，接口延迟会随人数线性劣化。
    未命中时返回 `data_available=False`，前端显示「暂无数据」而不是 0。
    """
    users = await user_service.list_users()
    leaders = await _leader_ids_with_capability(store, users)
    if not leaders:
        return FollowLeaderListResponse(leaders=[])

    all_groups = await store.all_groups()
    all_nodes = await store.all_nodes()
    # 跟单市场判定所需的模版快照：一次批量读 Redis，不在循环里逐个查
    strategies = await _strategies_of(all_groups, store)
    tmpl_map = _template_ids_of(strategies)
    mine_by_leader: dict[int, list[dict]] = {}
    for f in await follow_service.list_followings(p.user_id):
        mine_by_leader.setdefault(int(f["leader_user_id"]), []).append(f)
    # 每个带单员被多少节点跟随（按 leader 聚合全量跟单关系）
    followers_by_leader: dict[int, set[str]] = {}
    for f in await follow_service.list_for_leaders(leaders):
        followers_by_leader.setdefault(int(f["leader_user_id"]), set()).add(f["node_id"])

    # 绩效：一次 MGET 拿全部带单员的预聚合结果（未命中就是 None）
    win = window if window in deal_stats.WINDOWS else deal_stats.DEFAULT_WINDOW
    cached: dict[int, dict] = {}
    for uid in leaders:
        entry = await store.get_leader_stats(uid)
        if entry and win in entry:
            cached[uid] = entry[win]

    by_id = {u.id: u for u in users}
    needle = q.strip().lower()
    out: list[dict] = []
    for uid in leaders:
        u = by_id.get(uid)
        if not u:
            continue
        primary = _primary_of(all_nodes, uid)
        if not primary:
            continue
        followable = follow_rules.followable_groups(
            all_groups, uid, primary.get("node_id"),
            market=follow_rules.FOLLOW_MARKET_TEMPLATE_IDS,
            strategy_templates_map=tmpl_map,
        )
        if not followable:
            continue
        if needle:
            haystack = " ".join(str(x) for x in [
                u.username, u.display_name or "",
                primary.get("name") or "", primary.get("mt5_login") or "",
            ]).lower()
            if needle not in haystack:
                continue
        mine = mine_by_leader.get(uid, [])
        # 缓存结构：{window: {**summary, follower_count}} —— 与 deal_agg 写入的一致
        perf = dict(cached.get(uid) or {})
        out.append({
            "leader_user_id": uid,
            "username": u.username,
            "display_name": u.display_name,
            "primary_node": _node_brief(primary),
            "followable_group_count": len(followable),
            "follower_node_count": len(followers_by_leader.get(uid, set())),
            "my_follow_group_count": len({f["group_id"] for f in mine}),
            "my_follow_node_count": len(mine),
            "marketable": True,
            # 绩效：聚合公开可见（带单广场是给潜在跟单者看的）
            "performance": {
                k: v for k, v in perf.items() if k != "follower_count"
            },
            "perf_window": win,
            "follower_count": int(perf.get("follower_count") or 0)
            or len(followers_by_leader.get(uid, set())),
            "perf_available": bool(perf),
        })
    # 已跟过的排前面，其余按用户名
    out.sort(key=lambda x: (x["my_follow_group_count"] == 0, x["username"]))
    return FollowLeaderListResponse(leaders=out)


# ---------------------------------------------------------------------------
# 带单员详情
# ---------------------------------------------------------------------------
@router.get("/leaders/{leader_user_id}", response_model=FollowDetailOut)
async def get_leader_detail(
    leader_user_id: int,
    window: str = "30d",
    store: RedisStore = Depends(get_store),
    p: Principal = Depends(require_menu(MENU_FOLLOW)),
) -> FollowDetailOut:
    """带单员详情：可跟单分组（含我的已跟标记）+ 我可用于跟单的节点 + 绩效。"""
    user = await user_service.get_user_by_id(leader_user_id)
    if not user or not user.is_active:
        raise HTTPException(status_code=404, detail="带单员不存在或已停用")
    principal = await rbac_service.load_principal(store, user.username)
    if not principal or not permissions.can_be_leader(principal):
        raise HTTPException(status_code=404, detail="该用户暂未开放带单")

    all_nodes = await store.all_nodes()
    primary = _primary_of(all_nodes, leader_user_id)
    if not primary:
        raise HTTPException(status_code=404, detail="该带单员尚无主节点")

    all_groups = await store.all_groups()
    # 跟单市场判定所需的模版快照：一次批量读 Redis
    all_strategies = await _strategies_of(all_groups, store)
    tmpl_map = _template_ids_of(all_strategies)
    # 市场内（可跟）：趋势 / 网格
    followable = follow_rules.followable_groups(
        all_groups, leader_user_id, primary.get("node_id"),
        market=follow_rules.FOLLOW_MARKET_TEMPLATE_IDS,
        strategy_templates_map=tmpl_map,
    )
    # 市场外（AI 加仓等）：仅提示，不可选 —— 支撑「存量关系人工清理」需要看得见
    followable_ids = {g["group_id"] for g in followable}
    hidden = [
        g for g in follow_rules.followable_groups(
            all_groups, leader_user_id, primary.get("node_id"),
            market=follow_rules.AI_MARKET_TEMPLATE_IDS,
            strategy_templates_map=tmpl_map,
        )
        if g["group_id"] not in followable_ids
    ]
    strategies = all_strategies
    signal_counts = await group_persist.count_by_group()
    active_counts = await group_persist.count_active_by_group()

    mine_by_group: dict[str, list[str]] = {}
    for f in await follow_service.list_followings(p.user_id):
        if int(f["leader_user_id"]) == leader_user_id:
            mine_by_group.setdefault(f["group_id"], []).append(f["node_id"])

    detail_groups = []
    for g in sorted(followable, key=follow_rules.leader_group_sort_key):
        gid = g["group_id"]
        strategy = strategies.get(g.get("strategy_id") or "")
        reason = None if g.get("enabled", True) else "分组已禁用"
        if strategy is None and g.get("strategy_id"):
            reason = "已停止接收新信号（策略已停用）"
        detail_groups.append({
            "group_id": gid,
            "name": g.get("name"),
            "strategy_name": (strategy or {}).get("name"),
            "strategy_template": (strategy or {}).get("template_name"),
            "dispatch_mode": g.get("dispatch_mode", "sync"),
            "signal_concurrent_enabled": bool(g.get("signal_concurrent_enabled")),
            "trend_risk_enabled": bool(g.get("trend_risk_enabled")),
            "enabled": bool(g.get("enabled", True)),
            "member_count": len(g.get("members") or []),
            "follower_count": len(g.get("followers") or []),
            "online_count": _count_online(g),
            "active_task_count": active_counts.get(gid, 0),
            "signal_count": signal_counts.get(gid, 0),
            "my_follow_node_ids": mine_by_group.get(gid, []),
            "selectable": reason is None,
            "unselectable_reason": reason,
        })

    # 市场外分组（AI 加仓等）：置灰展示 + 原因，供「存量关系人工清理」看得见
    hidden_groups = []
    for g in sorted(hidden, key=follow_rules.leader_group_sort_key):
        gid = g["group_id"]
        strategy = strategies.get(g.get("strategy_id") or "")
        hidden_groups.append({
            "group_id": gid,
            "name": g.get("name"),
            "strategy_name": (strategy or {}).get("name"),
            "strategy_template": (strategy or {}).get("template_name"),
            "dispatch_mode": g.get("dispatch_mode", "sync"),
            "signal_concurrent_enabled": bool(g.get("signal_concurrent_enabled")),
            "trend_risk_enabled": bool(g.get("trend_risk_enabled")),
            "enabled": bool(g.get("enabled", True)),
            "member_count": len(g.get("members") or []),
            "follower_count": len(g.get("followers") or []),
            "online_count": _count_online(g),
            "active_task_count": active_counts.get(gid, 0),
            "signal_count": signal_counts.get(gid, 0),
            "my_follow_node_ids": mine_by_group.get(gid, []),
            "selectable": False,
            "unselectable_reason": follow_rules.market_reject_reason(
                (strategy or {}).get("template_id"),
                follow_rules.FOLLOW_MARKET_TEMPLATE_IDS,
            ) or "该分组不在普通跟单市场",
        })

    mine = mine_by_group
    # 绩效：读后台预聚合的 Redis 缓存（与列表同一份，口径完全一致）。
    # 详情是单个人，允许回源查库兜底（缓存刚建立时可能还没有）。
    win = window if window in deal_stats.WINDOWS else deal_stats.DEFAULT_WINDOW
    perf: dict = {}
    cached = await store.get_leader_stats(leader_user_id) or {}
    if win in cached:
        perf = dict(cached[win])
    else:
        # 缓存未命中：就地算一次（详情是单人，不构成 N+1）
        leader_node_ids = [
            str(n.get("node_id"))
            for n in all_nodes
            if permissions.normalize_owner(n.get("owner_user_id")) == leader_user_id
            and n.get("node_id")
        ]
        perf = await deal_stats.leader_batch(
            leader_node_ids, deal_stats.window_start(win), None
        )
        perf = dict(perf)
    follower_count = int(perf.pop("follower_count", 0) or 0) or len({
        f["node_id"] for f in await follow_service.list_for_leaders({leader_user_id})
    })
    perf["follower_count"] = follower_count
    return FollowDetailOut(
        leader={
            "leader_user_id": leader_user_id,
            "username": user.username,
            "display_name": user.display_name,
            "primary_node": _node_brief(primary),
            "followable_group_count": len(followable),
            "follower_node_count": len({
                f["node_id"] for f in await follow_service.list_for_leaders({leader_user_id})
            }),
            "my_follow_group_count": len(mine),
            "my_follow_node_count": sum(len(v) for v in mine.values()),
            "marketable": True,
            "performance": {k: v for k, v in perf.items() if k != "follower_count"},
            "perf_window": win,
            "follower_count": follower_count,
            "perf_available": True,
        },
        groups=detail_groups,
        hidden_groups=hidden_groups,
        my_nodes=[_node_brief(n) for n in _viewable_follow_nodes(all_nodes, p)],
    )


# ---------------------------------------------------------------------------
# 我的跟单
# ---------------------------------------------------------------------------
@router.get("/me", response_model=FollowMeResponse)
async def list_my_followings(
    store: RedisStore = Depends(get_store),
    p: Principal = Depends(require_menu(MENU_FOLLOW)),
) -> FollowMeResponse:
    """我的跟单：按带单员分组；带单员已撤销能力时标 unmarketable 但仍可自行移出。"""
    followings = await follow_service.list_followings(p.user_id)
    if not followings:
        return FollowMeResponse(
            following=[], summary={"following_count": 0, "follow_node_count": 0},
        )
    all_groups = {g["group_id"]: g for g in await store.all_groups()}
    all_nodes = await store.all_nodes()
    active_counts = await group_persist.count_active_by_group()
    # 跟单市场判定所需的模版快照（存量 AI 分组的关系仍在，只是标不可跟）
    strategies = await _strategies_of(list(all_groups.values()), store)
    tmpl_map = _template_ids_of(strategies)

    by_leader: dict[int, list[dict]] = {}
    for f in followings:
        by_leader.setdefault(int(f["leader_user_id"]), []).append(f)

    following_out: list[dict] = []
    for lid, items in sorted(by_leader.items()):
        user = await user_service.get_user_by_id(lid)
        if not user:
            continue
        principal = await rbac_service.load_principal(store, user.username)
        has_cap = bool(principal) and permissions.can_be_leader(principal)
        primary = _primary_of(all_nodes, lid)

        by_group: dict[str, list[dict]] = {}
        for it in items:
            by_group.setdefault(it["group_id"], []).append(it)

        groups_out = []
        for gid, fs in by_group.items():
            g = all_groups.get(gid)
            if not g:
                continue  # 分组已被删除（理论上 delete_group 已级联），跳过
            reason = follow_rules.marketable_reason(
                g, has_cap, primary.get("node_id") if primary else None,
                template_id=tmpl_map.get(str(g.get("strategy_id") or "")),
                market=follow_rules.FOLLOW_MARKET_TEMPLATE_IDS,
            )
            node_ids = {f["node_id"] for f in fs}
            groups_out.append({
                "group_id": gid,
                "group_name": g.get("name"),
                "symbol": g.get("strategy_symbol"),
                "strategy_name": g.get("strategy_name"),
                "marketable": reason is None,
                "unmarketable_reason": reason,
                "active_task_count": active_counts.get(gid, 0),
                "my_nodes": [
                    {
                        **_node_brief(next(
                            (n for n in all_nodes if n.get("node_id") == f["node_id"]), None
                        )),
                        "joined_at": f.get("joined_at"),
                    }
                    for f in sorted(fs, key=lambda x: x.get("joined_at") or 0)
                    if f["node_id"] in node_ids
                ],
            })
        following_out.append({
            "leader": {
                "leader_user_id": lid,
                "username": user.username,
                "display_name": user.display_name,
                "primary_node": _node_brief(primary),
                "followable_group_count": 0,
                "follower_node_count": 0,
                "my_follow_group_count": len(by_group),
                "my_follow_node_count": len(items),
                "marketable": has_cap,
            },
            "groups": groups_out,
        })
    return FollowMeResponse(
        following=following_out,
        summary={
            "following_count": len(by_leader),
            "follow_node_count": len(followings),
        },
    )


# ---------------------------------------------------------------------------
# 加入跟单
# ---------------------------------------------------------------------------
@router.post("/join", response_model=FollowJoinResponse)
async def join_follow(
    body: FollowJoinRequest,
    request: Request,
    store: RedisStore = Depends(get_store),
    p: Principal = Depends(require_menu(MENU_FOLLOW)),
) -> FollowJoinResponse:
    """把当前用户的若干节点加入带单员的若干分组。

    校验顺序固定：带单员有效性 -> 节点归属/主节点 -> 分组可跟单性。
    越权与不存在一律 4xx，不透露对方名下有什么分组。
    """
    user = await user_service.get_user_by_id(body.leader_user_id)
    if not user or not user.is_active:
        raise HTTPException(status_code=404, detail="带单员不存在或已停用")
    principal = await rbac_service.load_principal(store, user.username)
    if not principal or not permissions.can_be_leader(principal):
        raise HTTPException(status_code=404, detail="该用户暂未开放带单")

    all_nodes = await store.all_nodes()
    leader_primary = _primary_of(all_nodes, body.leader_user_id)
    if not leader_primary:
        raise HTTPException(status_code=404, detail="该带单员尚无主节点")

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

    # 分组：必须是该带单员名下、含其主节点、启用、策略启用、且模版在**带单市场**内。
    # 这是唯一的写入路径 —— 收窄到带单市场后，AI 分组就写不进跟单关系了。
    all_groups = await store.all_groups()
    all_strategies = await _strategies_of(all_groups, store)
    tmpl_map = _template_ids_of(all_strategies)
    followable = {
        g["group_id"]: g for g in follow_rules.followable_groups(
            all_groups, body.leader_user_id, leader_primary.get("node_id"),
            market=follow_rules.FOLLOW_MARKET_TEMPLATE_IDS,
            strategy_templates_map=tmpl_map,
        )
    }
    strategies = all_strategies
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
            if reason == follow_rules.NODE_JOIN_REJECT["duplicate"]:
                already.append(nid)
                continue
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
        p.username, "join_follow", user.username,
        {
            "leader_user_id": body.leader_user_id,
            "leader_primary_node_id": leader_primary.get("node_id"),
            "group_ids": [j["group_id"] for j in joined],
            "node_ids": node_ids,
        },
        "ok", client_ip(request), category="follow",
        before=None,
        after={"joined": joined, "skipped": skipped},
    )
    return FollowJoinResponse(
        joined=joined,
        skipped=skipped,
        leader_primary_node_id=leader_primary.get("node_id"),
    )


# ---------------------------------------------------------------------------
# 移出跟单
# ---------------------------------------------------------------------------
@router.post("/leave", response_model=FollowLeaveResponse)
async def leave_follow(
    body: FollowLeaveRequest,
    request: Request,
    store: RedisStore = Depends(get_store),
    p: Principal = Depends(require_menu(MENU_FOLLOW)),
) -> FollowLeaveResponse:
    """把当前用户的若干节点移出带单员的若干分组。

    不要求带单员仍有 follow_leader 能力 —— 跟单员必须始终能自行清理自己的节点。
    close_tasks=True 时按「节点在该分组的子任务」逐个终止（复用现有 stop 路径）。

    **市场隔离**：只允许移出**带单市场**的分组。AI 分组（tpl_1）虽然因存量
    关系需要在【我的跟单】里可见（便于人工清理），但它的关系归【AI 交易】
    市场管 —— 在这里删掉会让跟单员在另一侧还自以为在跟。两个市场共用同一张
    关系表，所以这个校验必须在**写入侧**，不能只靠前端不显示。
    跨市场的分组会被跳过（关系不动）并在 skipped 里回报原因。
    """
    removed: list[dict] = []
    closing: list[dict] = []
    forced: list[dict] = []
    skipped: list[dict] = []
    stopped = 0

    # 一次取全部分组 + 模版快照，循环内不逐个回源查 Redis
    all_groups = {g["group_id"]: g for g in await store.all_groups()}
    strategies = await _strategies_of(list(all_groups.values()), store)
    tmpl_map = _template_ids_of(strategies)

    for gid in body.group_ids:
        gid = str(gid or "").strip()
        if not gid:
            continue
        group = all_groups.get(gid) or await store.get_group(gid)
        if not group:
            continue
        # 市场校验：AI 分组不在带单市场 -> 跳过，关系保持不变
        skip_reason = follow_rules.leave_market_skip_reason(
            tmpl_map.get(str(group.get("strategy_id") or "")),
            follow_rules.FOLLOW_MARKET_TEMPLATE_IDS,
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
        p.username, "leave_follow", (user or _Stub(body.leader_user_id)).username,
        {
            "leader_user_id": body.leader_user_id,
            "group_ids": [r["group_id"] for r in removed],
            "node_ids": body.node_ids,
            "close_tasks": body.close_tasks,
            "stopped_subtasks": stopped,
            "skipped_group_ids": [s["group_id"] for s in skipped],
        },
        "ok", client_ip(request), category="follow",
        before=None,
        after={"removed": removed, "closing": closing, "forced": forced, "skipped": skipped},
    )
    return FollowLeaveResponse(
        removed=removed, closing=closing, forced=forced, warning=warning,
        skipped=skipped,
    )


class _Stub:
    """用户已删除时的兜底（删除用户时已清过跟单，这里只是审计 target 兜底）。"""

    def __init__(self, user_id: int) -> None:
        self.username = f"user#{user_id}"


async def _stop_following_subtasks(store: RedisStore, group_id: str, node_id: str) -> dict:
    """终止某节点在某分组的在跑子任务；返回 closing / forced / none 三态之一。

    复用 group_dispatcher.close_subtask（在线下发 strategy_stop 标 closing，
    离线强制收口并把平仓入队等重连补发），不另写一套强平逻辑 —— 平仓口径
    必须与手动 CLOSE 完全一致，否则会出现「移出跟单平不掉仓」的口径分裂。
    """
    from .group_dispatcher import group_dispatcher

    outcome: dict = {
        "node_id": node_id, "group_id": group_id, "status": "none",
        "dispatch_ids": [], "magic": None,
    }
    subs = [
        s for s in await group_persist.active_subtasks(group_id)
        if s.get("node_id") == node_id
    ]
    for sub in subs:
        try:
            r = await group_dispatcher(store).close_subtask(
                group_id, int(sub["dispatch_id"]), reason="leave_follow",
            )
        except ValueError:
            # 已被并发收口（终态），跳过
            continue
        status = r.get("status")
        if status in ("closing", "forced"):
            outcome["status"] = "forced" if status == "forced" else outcome["status"]
            outcome["dispatch_ids"].append(int(sub["dispatch_id"]))
            outcome["magic"] = sub.get("magic") or outcome["magic"]
    if outcome["dispatch_ids"] and outcome["status"] == "none":
        outcome["status"] = "closing"
    return outcome