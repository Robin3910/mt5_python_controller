"""分组管理 API。

分组是 strategy 信号（Webhook 的 model=strategy）的分发单元：支持新建 / 编辑 /
启停、维护成员节点、设置分组级分发模式（sync / poll，不区分币种），并提供该分组
已处理信号的明细查询（主任务 + 各节点处理过程）。

普通用户只能看 / 改自己创建的分组，成员只能选本人节点、策略只能绑本人策略；
写操作与平仓需要「分组管理」菜单。
"""
from fastapi import APIRouter, Depends, HTTPException, Request

from . import group_persist, group_rules, group_service, permissions, persist, rbac_service
from .connections import manager
from .deps import (
    client_ip,
    get_group_dispatcher,
    get_principal,
    get_store,
    owned_group_or_404,
    require_menu,
)
from .group_dispatcher import GroupDispatcher
from .limit_watch import normalize_keyword
from .models import (
    GROUP_DISPATCH_MODES,
    GroupCreate,
    GroupNodeRef,
    GroupOut,
    GroupTaskEventRecord,
    GroupUpdate,
    PaginatedGroupSignals,
    PaginatedLimitWatchLogs,
)
from .permissions import MENU_GROUPS, Principal
from .redis_store import RedisStore

router = APIRouter(prefix="/api/groups", tags=["groups"])


def _group_audit_snapshot(d: dict | None) -> dict | None:
    """分组审计快照：只保留配置相关字段。"""
    if not d:
        return None
    return {
        "group_id": d.get("group_id"),
        "name": d.get("name"),
        "enabled": d.get("enabled", True),
        "dispatch_mode": d.get("dispatch_mode"),
        "signal_concurrent_enabled": bool(d.get("signal_concurrent_enabled", False)),
        "trend_risk_enabled": bool(d.get("trend_risk_enabled", False)),
        "limit_watch_enabled": bool(d.get("limit_watch_enabled", False)),
        "limit_watch_keyword": normalize_keyword(d.get("limit_watch_keyword")),
        "limit_watch_node_ids": group_rules.limit_watch_report_ids(d),
        "strategy_id": d.get("strategy_id"),
        "remark": d.get("remark"),
        "node_ids": group_rules.member_ids(d),
        "owner_user_id": d.get("owner_user_id"),
    }


def _validate_dispatch_mode(mode: str | None) -> None:
    if mode is not None and mode not in GROUP_DISPATCH_MODES:
        raise HTTPException(
            status_code=400,
            detail=f"分发模式非法：{mode}（应为 {' / '.join(GROUP_DISPATCH_MODES)}）",
        )


async def _to_group_out(
    store: RedisStore, d: dict, signal_count: int = 0, active_task_count: int = 0,
    limit_watch_logs: list | None = None, owner_names: dict[int, str] | None = None,
) -> GroupOut:
    """把缓存里的分组 dict 组装成对外的 GroupOut（合并成员节点的在线状态）。"""
    nodes: list[GroupNodeRef] = []
    online = 0
    for member in d.get("members") or []:
        node_id = member.get("node_id")
        node = await store.get_node(node_id) or {}
        status = "online" if manager.is_node_online(node_id) else "offline"
        enabled = bool(node.get("enabled", True)) if node else False
        if node and enabled and status == "online":
            online += 1
        nodes.append(
            GroupNodeRef(
                node_id=node_id,
                name=node.get("name") or node_id,
                mt5_login=node.get("mt5_login"),
                enabled=enabled,
                status=status,
                sort_order=member.get("sort_order", 0),
                limit_watch_report=bool(member.get("limit_watch_report")),
            )
        )
    strategy_id = d.get("strategy_id") or None
    strategy_name = None
    if strategy_id:
        sty = await store.get_strategy(strategy_id) or {}
        strategy_name = sty.get("name") or strategy_id
    logs: list = []
    if d.get("limit_watch_enabled"):
        if limit_watch_logs is not None:
            logs = limit_watch_logs
        else:
            gid = d.get("group_id")
            if gid:
                latest = await persist.latest_limit_watch_logs([gid])
                logs = latest.get(gid, [])
    owner = permissions.normalize_owner(d.get("owner_user_id"))
    return GroupOut(
        group_id=d["group_id"],
        name=d["name"],
        enabled=d.get("enabled", True),
        dispatch_mode=d.get("dispatch_mode", "sync"),
        signal_concurrent_enabled=bool(d.get("signal_concurrent_enabled", False)),
        trend_risk_enabled=bool(d.get("trend_risk_enabled", False)),
        limit_watch_enabled=bool(d.get("limit_watch_enabled", False)),
        limit_watch_keyword=normalize_keyword(d.get("limit_watch_keyword")),
        strategy_id=strategy_id,
        strategy_name=strategy_name,
        remark=d.get("remark"),
        created_at=d.get("created_at", 0),
        nodes=nodes,
        node_count=len(nodes),
        online_node_count=online,
        signal_count=signal_count,
        active_task_count=active_task_count,
        limit_watch_logs=logs,
        owner_user_id=owner,
        owner_username=(owner_names or {}).get(owner) if owner is not None else None,
    )


@router.get("", response_model=list[GroupOut])
async def list_groups(
    q: str | None = None,
    store: RedisStore = Depends(get_store),
    p: Principal = Depends(get_principal),
):
    """分组列表（按创建时间排序）；可选 q 按分组名称模糊搜索。普通用户只看本人分组。"""
    groups = permissions.visible(p, await store.all_groups())
    if q and (term := q.strip()):
        needle = term.lower()
        groups = [g for g in groups if needle in (g.get("name") or "").lower()]
    # created_at 只到秒，同秒创建的按名称兜底，避免列表顺序在刷新之间跳动
    groups.sort(key=lambda g: (g.get("created_at", 0), g.get("name") or ""))
    counts = await group_persist.count_by_group()
    active_counts = await group_persist.count_active_by_group()
    watch_ids = [
        g["group_id"] for g in groups
        if g.get("limit_watch_enabled") and g.get("group_id")
    ]
    latest_logs = await persist.latest_limit_watch_logs(watch_ids) if watch_ids else {}
    names = await rbac_service.owner_names_for(p)
    return [
        await _to_group_out(
            store, g, counts.get(g["group_id"], 0), active_counts.get(g["group_id"], 0),
            limit_watch_logs=latest_logs.get(g["group_id"], []) if g.get("limit_watch_enabled") else [],
            owner_names=names,
        )
        for g in groups
    ]


@router.post("", response_model=GroupOut, status_code=201)
async def create_group(
    body: GroupCreate,
    request: Request,
    store: RedisStore = Depends(get_store),
    p: Principal = Depends(require_menu(MENU_GROUPS)),
):
    """新建分组：归创建者所有（管理员建的归管理员名下），名称在同一所有者内唯一。"""
    _validate_dispatch_mode(body.dispatch_mode)
    owner = permissions.owner_for_new(p)
    name = body.name.strip()
    if await group_service.name_exists(name, owner_user_id=owner):
        raise HTTPException(status_code=409, detail=f"分组名称已存在：{name}")
    try:
        d = await group_service.create_group(store, body, owner_user_id=owner)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await persist.audit(
        p.username, "create_group", d["group_id"], None, "ok", client_ip(request),
        category="console", before=None, after=_group_audit_snapshot(d),
    )
    return await _to_group_out(store, d, owner_names=await rbac_service.owner_names_for(p))


@router.get("/{group_id}", response_model=GroupOut)
async def get_group(
    group_id: str,
    store: RedisStore = Depends(get_store),
    p: Principal = Depends(get_principal),
):
    d = await owned_group_or_404(store, p, group_id)
    counts = await group_persist.count_by_group()
    active_counts = await group_persist.count_active_by_group()
    return await _to_group_out(
        store, d, counts.get(group_id, 0), active_counts.get(group_id, 0),
        owner_names=await rbac_service.owner_names_for(p),
    )


@router.get("/{group_id}/limit-watch-logs", response_model=PaginatedLimitWatchLogs)
async def group_limit_watch_logs(
    group_id: str,
    page: int = 1,
    page_size: int = 50,
    store: RedisStore = Depends(get_store),
    p: Principal = Depends(get_principal),
):
    """分组限价监听日志分页（落库；列表页经后台 WS 实时追加）。"""
    await owned_group_or_404(store, p, group_id)
    return await persist.list_limit_watch_logs(group_id, page, page_size)


@router.get("/{group_id}/signals", response_model=PaginatedGroupSignals)
async def group_signals(
    group_id: str,
    page: int = 1,
    page_size: int = 20,
    status: str | None = None,
    signal_id: str | None = None,
    store: RedisStore = Depends(get_store),
    p: Principal = Depends(get_principal),
):
    """分组信号明细分页：主任务（信号 + 下发数据）与各节点的处理过程。

    status=active 时仅返回进行中主任务（pending/dispatching/running）。
    signal_id 非空时按信号编号精确过滤（监听日志「已转成策略信号」跳转）。
    """
    if status is not None and status != "active":
        raise HTTPException(status_code=400, detail="status 仅支持 active")
    await owned_group_or_404(store, p, group_id)
    nodes = await store.all_nodes()
    node_names = {n["node_id"]: n.get("name") or n["node_id"] for n in nodes}
    sid = (signal_id or "").strip() or None
    return await group_persist.recent_group_signals(
        group_id, page, page_size, node_names, status=status, signal_id=sid,
    )


@router.get(
    "/{group_id}/dispatches/{dispatch_id}/events",
    response_model=list[GroupTaskEventRecord],
)
async def group_dispatch_events(
    group_id: str,
    dispatch_id: int,
    store: RedisStore = Depends(get_store),
    p: Principal = Depends(get_principal),
):
    """节点策略子任务的关联订单/事件流：开仓、加仓、平仓等。"""
    await owned_group_or_404(store, p, group_id)
    items = await group_persist.list_dispatch_events(group_id, dispatch_id)
    if items is None:
        raise HTTPException(status_code=404, detail="dispatch not found")
    if items:
        node_id = items[0].get("node_id")
        magic = items[0].get("magic")
        acct = await store.get_account(str(node_id)) if node_id else None
        items = group_rules.overlay_live_event_profits(
            items,
            (acct or {}).get("positions") or [],
            int(magic) if magic is not None else None,
        )
    return items


@router.post("/{group_id}/dispatches/{dispatch_id}/close")
async def close_group_dispatch(
    group_id: str,
    dispatch_id: int,
    request: Request,
    store: RedisStore = Depends(get_store),
    group_dispatcher: GroupDispatcher = Depends(get_group_dispatcher),
    p: Principal = Depends(require_menu(MENU_GROUPS)),
):
    """手动终止单个节点策略子任务：下发 strategy_stop，平掉该魔术号持仓并结束监控。

    与 Webhook CLOSE（按品种命中全部分组全部子任务）不同，这里只作用于指定子任务。
    """
    await owned_group_or_404(store, p, group_id)
    try:
        outcome = await group_dispatcher.close_subtask(group_id, dispatch_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    result = "ok" if outcome.get("status") == "closing" else "offline"
    await persist.audit(
        p.username, "close_group_dispatch", group_id,
        {"dispatch_id": dispatch_id, "node_id": outcome.get("node_id")},
        result, client_ip(request),
        category="console", before=None, after=outcome,
    )
    return outcome


@router.post("/{group_id}/close")
async def close_group(
    group_id: str,
    request: Request,
    store: RedisStore = Depends(get_store),
    group_dispatcher: GroupDispatcher = Depends(get_group_dispatcher),
    p: Principal = Depends(require_menu(MENU_GROUPS)),
):
    """分组一键平仓：终止该分组全部未收口子任务，平掉各自魔术号持仓并结束监控。

    只作用于本分组，不做账户级全平，因此同一节点上其它分组的任务与持仓不受影响。
    与 Webhook CLOSE 同一条 strategy_stop 路径；CLOSE 还按品种筛选，这里不限品种。
    """
    await owned_group_or_404(store, p, group_id)
    try:
        outcome = await group_dispatcher.close_group(group_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    status = outcome.get("status")
    if status == "closing":
        result = "ok"
    elif status == "skipped":
        result = "skipped"
    else:
        result = "offline"
    await persist.audit(
        p.username, "close_group", group_id, None, result, client_ip(request),
        category="console", before=None, after=outcome,
    )
    return outcome


@router.patch("/{group_id}", response_model=GroupOut)
async def update_group(
    group_id: str,
    body: GroupUpdate,
    request: Request,
    store: RedisStore = Depends(get_store),
    p: Principal = Depends(require_menu(MENU_GROUPS)),
):
    """更新分组（名称 / 启用状态 / 分发模式 / 信号并发 / 趋势风控 / 限价监听 / 成员上报开关 / 绑定策略 / 备注 / 成员节点）。"""
    _validate_dispatch_mode(body.dispatch_mode)
    current = await owned_group_or_404(store, p, group_id)
    if body.name is not None and (name := body.name.strip()):
        if await group_service.name_exists(
            name, exclude_group_id=group_id, owner_user_id=current.get("owner_user_id"),
        ):
            raise HTTPException(status_code=409, detail=f"分组名称已存在：{name}")
    before = _group_audit_snapshot(current)
    try:
        d = await group_service.update_group(store, group_id, body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not d:
        raise HTTPException(status_code=404, detail="group not found")
    await persist.audit(
        p.username, "update_group", group_id, body.model_dump(exclude_none=True), "ok",
        client_ip(request),
        category="console", before=before, after=_group_audit_snapshot(d),
    )
    return await _to_group_out(store, d, owner_names=await rbac_service.owner_names_for(p))


@router.delete("/{group_id}")
async def delete_group(
    group_id: str,
    request: Request,
    store: RedisStore = Depends(get_store),
    p: Principal = Depends(require_menu(MENU_GROUPS)),
):
    before = _group_audit_snapshot(await owned_group_or_404(store, p, group_id))
    ok = await group_service.delete_group(store, group_id)
    if not ok:
        raise HTTPException(status_code=404, detail="group not found")
    await persist.audit(
        p.username, "delete_group", group_id, None, "ok", client_ip(request),
        category="console", before=before, after=None,
    )
    return {"status": "deleted", "group_id": group_id}
