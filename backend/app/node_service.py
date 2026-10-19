"""节点持久化：MySQL/SQLite 为权威，Redis 作缓存 + mt5_login 反查映射。

自 v0.2 起鉴权令牌改为全局共享（见 system_settings），节点本身不再持有 token；
节点的业务唯一键升级为 mt5_login。

所有写操作都遵循“先写库、再刷新缓存”的顺序，保证重启后能从库里恢复全部状态。
"""
import logging
import time
from datetime import datetime
from typing import Optional

from sqlalchemy import func, select, text

from . import group_persist, group_service
from .db import SessionLocal
from . import risk_control
from .models import NodeCreate, NodeUpdate
from .orm import Node, NodeCredential
from .redis_store import RedisStore
from .rules import validate_node_global_lot_mode
from .security import make_node_id

logger = logging.getLogger(__name__)

# 阻塞前置检查的硬上限：单次响应最多返回的明细条数，防止 N 条在跑任务把响应撑爆
PRIMARY_BLOCKING_DETAIL_LIMIT = 100


class PrimarySwitchBlocked(Exception):
    """主节点切换被阻塞：旧主节点上仍有 in-flight 子任务。

    - code: 给前端 / 审计的机器可读错误码
    - node_id: 被阻塞的旧主节点 id
    - node_name: 同上，节点显示名
    - blocking_tasks: 阻塞明细（精简字段，limit = PRIMARY_BLOCKING_DETAIL_LIMIT）
    - next_node_id: 即将上任的新主节点 id（取消主节点时为 None）
    """

    def __init__(
        self, *, code: str, message: str, node_id: str, node_name: str,
        next_node_id: Optional[str], blocking_tasks: list[dict],
    ) -> None:
        super().__init__(message)
        self.code = code
        self.node_id = node_id
        self.node_name = node_name
        self.next_node_id = next_node_id
        self.blocking_tasks = blocking_tasks


async def precheck_active_blocking(
    store: RedisStore,
    *,
    target_node_id: str,
    next_node_id: Optional[str],
) -> None:
    """阶段 0：检查旧主节点上是否仍有非终态子任务。命中阻塞即抛 PrimarySwitchBlocked。

    阻塞判据（与 group_rules.SUBTASK_HOLDS_LOCK 对齐）：
      GroupTaskDispatch 中 node_id == target_node_id 且
      status NOT IN ("done", "failed", "skipped", "offline") → 阻塞

    放行条件（唯一）：
      旧主节点无任何 in-flight 子任务 → 放行。
      任何其它情况（包括管理员运维请求）一律不放行。
    """
    # 1) 拉活动子任务（与 group_dispatcher 的活跃口径一致）
    active = await group_persist.active_subtasks_for_node(target_node_id)
    if not active:
        return

    # 2) 组装明细：精简字段，避免把内部列全抛出去
    detail: list[dict] = [
        {
            "dispatch_id": s.get("dispatch_id"),
            "task_id": s.get("task_id"),
            "signal_id": s.get("signal_id"),
            "group_id": s.get("group_id"),
            "symbol": s.get("symbol"),
            "magic": s.get("magic"),
            "status": s.get("status"),
        }
        for s in active[:PRIMARY_BLOCKING_DETAIL_LIMIT]
    ]

    # 3) 节点名（缓存里查；没有就回退到 id）
    node = await store.get_node(target_node_id) or {}
    node_name = node.get("name") or target_node_id

    # 4) 抛阻塞异常（路由层会原样映射成 409 + detail）
    raise PrimarySwitchBlocked(
        code="primary_switch_blocked",
        message=(
            f"旧主节点 {node_name} 仍有 {len(active)} 条在跑子任务，"
            "请先到任务页收口后再切换主节点"
        ),
        node_id=target_node_id,
        node_name=node_name,
        next_node_id=next_node_id,
        blocking_tasks=detail,
    )


async def list_active_tasks_for_node(node_id: str) -> list[dict]:
    """前端轻量预查：返回某节点当前 in-flight 子任务精简列表。

    与 precheck_active_blocking 同源（group_persist.active_subtasks_for_node），
    区别是这里不抛异常；用于切换主节点前在确认框里展示「旧主节点有 N 条在跑」。
    """
    active = await group_persist.active_subtasks_for_node(node_id)
    return [
        {
            "dispatch_id": s.get("dispatch_id"),
            "task_id": s.get("task_id"),
            "signal_id": s.get("signal_id"),
            "group_id": s.get("group_id"),
            "symbol": s.get("symbol"),
            "magic": s.get("magic"),
            "status": s.get("status"),
        }
        for s in active[:PRIMARY_BLOCKING_DETAIL_LIMIT]
    ]


async def _collect_victim_node_ids(
    session, *, node_id: str, is_primary: bool,
) -> list[str]:
    """收集本次主节点切换会被踢下台 / 被取消的旧主节点 id 列表。

    - 设为 True：同 owner 下当前 is_primary=True 但不是 node_id 自身的节点（被替换）
    - 设为 False：node_id 自身（如果当前 is_primary=True）
    - 状态未变（已同态）→ 返回空 list
    """
    row = await session.get(Node, node_id)
    if not row:
        return []
    owner_user_id: Optional[int] = row.owner_user_id
    if is_primary:
        # 即将晋升为新主：找 owner 下其它当前仍是主节点的节点（被替换）
        stmt = select(Node.node_id).where(Node.is_primary.is_(True))
        if owner_user_id is None:
            stmt = stmt.where(Node.owner_user_id.is_(None))
        else:
            stmt = stmt.where(Node.owner_user_id == owner_user_id)
        stmt = stmt.where(Node.node_id != node_id)
        return [r[0] for r in (await session.execute(stmt)).all()]
    # 即将取消主节点
    if row.is_primary:
        return [node_id]
    return []





def node_row_to_dict(row: Node) -> dict:
    """ORM 行 -> Redis 缓存用的 dict（created_at 转成 epoch 便于排序）。"""
    return {
        "node_id": row.node_id,
        "name": row.name,
        "enabled": row.enabled,
        "lot_mode": row.lot_mode,
        "lot": row.lot,
        "follow_sync": row.follow_sync,
        "follow_poll": row.follow_poll,
        "poll_order": row.poll_order,
        "filters": row.filters_json,
        "risk": risk_control.normalize_risk(row.risk_json),
        "mt5_login": row.mt5_login,
        "mt5_server": row.mt5_server,
        "client_version": row.client_version,
        "client_version_at": (
            row.client_version_at.timestamp() if row.client_version_at else None
        ),
        "owner_user_id": row.owner_user_id,
        "approval_status": row.approval_status,
        "requested_by_user_id": row.requested_by_user_id,
        "admin_enable_requested": bool(row.admin_enable_requested),
        "is_primary": bool(row.is_primary),
        "created_at": row.created_at.timestamp() if row.created_at else time.time(),
    }


async def warm_cache(store: RedisStore) -> int:
    """启动时以库为准刷新 Redis，并丢掉库里已经没有的缓存节点。"""
    async with SessionLocal() as s:
        rows = (await s.execute(select(Node))).scalars().all()
    alive = set()
    for row in rows:
        alive.add(row.node_id)
        await store.cache_node(node_row_to_dict(row))
    for cached in await store.all_nodes():
        nid = cached.get("node_id")
        if nid and nid not in alive:
            await store.delete_node(nid)
            await group_service.remove_node_from_all_groups(store, nid)
    return len(alive)


# 节点自动注册时，每个品种的按币种默认条目（与节点详情表单默认一致）
DEFAULT_NODE_SYMBOL_RULE = {
    "follow_sync": True,
    "follow_poll": True,
    "lot_mode": "fixed",
    "lot": 0.01,
    "poll_order": 0,
}

# 节点自动注册时使用的默认配置（启用状态与手动新建不同：自动入库默认禁用，需管理员启用后才可接入）
AUTO_NODE_DEFAULTS = {
    "enabled": False,
    "lot_mode": "fixed",
    "lot": 0.01,
    "poll_order": 0,
}


def default_node_filters_from_global(global_filters: dict) -> dict:
    """从中控台「多区间方向过滤」已有品种，生成节点按币种默认配置。"""
    out: dict = {}
    for sym, rule in (global_filters or {}).items():
        if not isinstance(rule, dict):
            continue
        key = str(sym).strip().upper()
        if not key:
            continue
        out[key] = dict(DEFAULT_NODE_SYMBOL_RULE)
    return out


def _default_name(seq: int, mt5_login: int) -> str:
    """默认显示名：`{序号}-{mt5_login}`，序号按节点位置从 1 起递增。"""
    return f"{seq}-{mt5_login}"


async def _next_node_seq(session) -> int:
    """下一节点序号 = 当前节点数 + 1（列表按创建顺序，新节点位于末尾）。"""
    count = (await session.execute(select(func.count()).select_from(Node))).scalar_one()
    return int(count) + 1


async def find_by_mt5_login(mt5_login: int) -> Optional[dict]:
    """按 MT5 登录号在库中查找节点（不走缓存，用于 WS 握手时的权威判定）。"""
    async with SessionLocal() as s:
        row = (
            await s.execute(select(Node).where(Node.mt5_login == int(mt5_login)))
        ).scalar_one_or_none()
        return node_row_to_dict(row) if row else None


async def create_node(
    store: RedisStore, payload: NodeCreate, *, enabled: bool = True,
) -> dict:
    """创建节点：写库 + 刷新缓存。mt5_login 重复会触发 IntegrityError，由路由层捕获。

    enabled 默认 True（管理端手动新建）；自动注册传入 AUTO_NODE_DEFAULTS["enabled"]=False。
    """
    if payload.filters is not None:
        err = validate_node_global_lot_mode(
            payload.filters, await store.get_filters()
        )
        if err:
            raise ValueError(err)
    explicit_name = (payload.name or "").strip()
    node_id = make_node_id()
    async with SessionLocal() as s:
        name = explicit_name or _default_name(
            await _next_node_seq(s), payload.mt5_login,
        )
        row = Node(
            node_id=node_id,
            name=name,
            enabled=enabled,
            lot_mode=AUTO_NODE_DEFAULTS["lot_mode"],
            lot=AUTO_NODE_DEFAULTS["lot"],
            poll_order=AUTO_NODE_DEFAULTS["poll_order"],
            filters_json=payload.filters,
            mt5_login=payload.mt5_login,
        )
        s.add(row)
        await s.commit()
        await s.refresh(row)
        d = node_row_to_dict(row)
    await store.cache_node(d)
    return d


async def auto_register(store: RedisStore, mt5_login: int) -> dict:
    """node_client 登录时按 mt5_login 自动注册节点（默认配置见 AUTO_NODE_DEFAULTS）。

    按币种配置：从中控台多区间方向过滤已有品种自动生成（参与 sync/poll、固定手数 0.01、轮询序 0）。
    默认 enabled=False：入库后须管理员启用才可接入；本次连接会在鉴权阶段被拒（4403）。
    若已存在则直接返回现有节点（幂等）；不存在则用默认配置入库。
    """
    existing = await find_by_mt5_login(mt5_login)
    if existing:
        return existing
    global_filters = await store.get_filters()
    filters = default_node_filters_from_global(global_filters)
    payload = NodeCreate(
        name=None,  # 留空由 create_node 按「序号-mt5_login」生成
        mt5_login=mt5_login,
        filters=filters or None,
    )
    return await create_node(
        store, payload, enabled=AUTO_NODE_DEFAULTS["enabled"],
    )


async def _locked_node(session, node_id: str):
    """串行化启用意图与归属更改，避免同时提交后两个条件齐备却未开通。"""
    if session.bind.dialect.name == "sqlite":
        await session.execute(text("BEGIN IMMEDIATE"))
    return await session.scalar(select(Node).where(Node.node_id == node_id).with_for_update())


async def update_node(store: RedisStore, node_id: str, patch: NodeUpdate, *, admin_enable_intent: bool = False,
                      audit_principal=None, audit_ip: str | None = None) -> Optional[dict]:
    """更新节点：仅写入“非空”字段；成功后刷新缓存。"""
    if patch.filters is not None:
        err = validate_node_global_lot_mode(
            patch.filters, await store.get_filters()
        )
        if err:
            raise ValueError(err)
    risk_norm: dict | None = None
    if patch.risk is not None:
        risk_norm = risk_control.normalize_risk(patch.risk)
        err = risk_control.validate_risk(risk_norm)
        if err:
            raise ValueError(err)
    async with SessionLocal() as s:
        row = await _locked_node(s, node_id)
        if not row:
            return None
        approval_change = row.approval_status == "pending" and patch.enabled is not None
        approval_before = {"approval_status": row.approval_status, "enabled": row.enabled,
                           "admin_enable_requested": row.admin_enable_requested}
        for f in ("name",):
            v = getattr(patch, f, None)
            if v is not None:
                setattr(row, f, v)
        if patch.enabled is not None:
            if row.approval_status == "pending":
                if not admin_enable_intent:
                    raise ValueError("新增节点须由管理员审核开通")
                row.admin_enable_requested = patch.enabled
                row.enabled = False
                from .node_credentials import activate_if_ready
                await activate_if_ready(s, row)
            else:
                row.enabled = patch.enabled
        if patch.filters is not None:
            row.filters_json = patch.filters
        if risk_norm is not None:
            row.risk_json = risk_norm
        if approval_change and audit_principal is not None:
            from .dashboard_audit import begin_operation
            audit_row = await begin_operation(s, audit_principal, "approval_intent", target=node_id,
                                              params={"enabled": bool(patch.enabled)}, ip=audit_ip, result="ok")
            audit_row.before_json = approval_before
            audit_row.after_json = {"approval_status": row.approval_status, "enabled": row.enabled,
                                    "admin_enable_requested": row.admin_enable_requested}
        await s.commit()
        await s.refresh(row)
        d = node_row_to_dict(row)
    await store.cache_node(d)
    return d


async def set_owner(
    store: RedisStore, node_id: str, owner_user_id: Optional[int], *, audit_principal=None,
    audit_ip: str | None = None,
) -> Optional[dict]:
    """改节点归属（None = 回到管理员名下）；前置校验（活动子任务、分组成员）由调用方完成。"""
    async with SessionLocal() as s:
        row = await _locked_node(s, node_id)
        if not row:
            return None
        owner_changed = row.owner_user_id != owner_user_id
        previous_owner = row.owner_user_id
        if owner_changed:
            from .node_credentials import activate_if_ready, revoke_on_transfer
            credential = await s.get(NodeCredential, node_id)
            needs_strict_audit = row.approval_status == "pending" or bool(
                credential and (credential.token_sha256 or not credential.legacy_allowed))
            await revoke_on_transfer(s, node_id, owner_user_id)
            row.owner_user_id = owner_user_id
            await activate_if_ready(s, row)
            # 跨用户转交时即时清掉主节点标记：主节点是「按所有者」维度唯一，
            # 改主人后老标记对新主人没有意义也违反唯一约束。
            if row.is_primary:
                row.is_primary = False
            # 「账号第一个节点自动升级为主节点」：当节点被分配给一个具体用户，
            # 且该用户此前名下没有任何主节点时，把这个新分配的节点自动升为主。
            # 仅在 owner_user_id 变到非空（真正的"分配给用户"，而非回收回管理员）
            # 且该用户维度下无现存主节点时触发；这样既不会顶替已有主节点，
            # 也避免了"从管理员名下转管理员名下"（owner_user_id=None）的边界情况。
            if (
                owner_user_id is not None
                and not await _owner_has_primary(s, owner_user_id, exclude_node_id=node_id)
            ):
                row.is_primary = True
            if needs_strict_audit and audit_principal is not None:
                from .dashboard_audit import begin_operation
                audit_row = await begin_operation(s, audit_principal, "assign_node", target=node_id,
                                                  params={"node_id": node_id}, ip=audit_ip, result="ok")
                audit_row.before_json = {"owner_user_id": previous_owner}
                audit_row.after_json = {"owner_user_id": owner_user_id, "approval_status": row.approval_status,
                                        "enabled": row.enabled, "is_primary": row.is_primary}
        await s.commit()
        await s.refresh(row)
        d = node_row_to_dict(row)
    await store.cache_node(d)
    return d


async def _owner_has_primary(
    session, owner_user_id: Optional[int], *, exclude_node_id: str | None = None,
) -> bool:
    """判断指定所有者名下是否已经存在主节点（排除 exclude_node_id）。

    用于「分配新节点时自动升级为主」前的唯一性预检，避免顶替已有主节点。
    """
    stmt = select(Node.node_id).where(Node.is_primary.is_(True))
    if owner_user_id is None:
        stmt = stmt.where(Node.owner_user_id.is_(None))
    else:
        stmt = stmt.where(Node.owner_user_id == owner_user_id)
    if exclude_node_id:
        stmt = stmt.where(Node.node_id != exclude_node_id)
    return (await session.execute(stmt.limit(1))).first() is not None



async def _clear_primary_for_owner(
    session, owner_user_id: Optional[int], *, exclude_node_id: str | None = None,
) -> None:
    """清掉同一所有者下其它节点的主节点标记（保留 exclude_node_id）。"""
    stmt = select(Node).where(Node.is_primary.is_(True))
    if owner_user_id is None:
        stmt = stmt.where(Node.owner_user_id.is_(None))
    else:
        stmt = stmt.where(Node.owner_user_id == owner_user_id)
    rows = (await session.execute(stmt)).scalars().all()
    for r in rows:
        if exclude_node_id and r.node_id == exclude_node_id:
            continue
        r.is_primary = False


async def set_primary(
    store: RedisStore, node_id: str, is_primary: bool,
    *, audit_principal=None, audit_ip: str | None = None,
) -> tuple[Optional[dict], list[str]]:
    """设置 / 取消某节点的主节点标记；按 owner 唯一。

    设置为 True 时会把同 owner 下其它节点的主节点标记全部清掉，保证唯一性。
    取消时只清本节点。权限与归属校验由路由层完成（先 owned_node_or_404）。

    阶段 0：被替换 / 被取消的旧主节点上若仍有非终态子任务
    （GroupTaskDispatch.status NOT IN SUBTASK_TERMINAL），整个动作会被
    PrimarySwitchBlocked 异常拦截。事务内、外各做一次校验：事务外拦截绝大多数
    场景，事务内拦截"阶段 0 通过后到 _locked_node 之间"极小窗口期新增的 in-flight。
    """
    # ── 收集 victim ── 阶段 0 和阶段 2 共用；必须在阶段 0 之前抓取，
    # 否则阶段 0 / 事务里把 victim 的 is_primary 清掉后，阶段 2 找不到 victim。
    async with SessionLocal() as s_pre:
        victims = await _collect_victim_node_ids(
            s_pre, node_id=node_id, is_primary=is_primary,
        )

    # ── 阶段 0（事务外预检）──
    for vid in victims:
        # 不抛错就继续；任一 victim 命中阻塞则由异常上抛
        await precheck_active_blocking(
            store,
            target_node_id=vid,
            next_node_id=node_id if is_primary else None,
        )

    async with SessionLocal() as s:
        row = await _locked_node(s, node_id)
        if not row:
            return None, []
        owner_after: Optional[int] = row.owner_user_id
        before = {"is_primary": bool(row.is_primary)}
        if bool(row.is_primary) == bool(is_primary):
            # 状态未变，避免无谓的写入与审计
            return node_row_to_dict(row), []

        # ── 阶段 0（事务内重检）── 双重保险，挡住事务外阶段 0 之后的极小窗口
        for vid in victims:
            await precheck_active_blocking(
                store,
                target_node_id=vid,
                next_node_id=node_id if is_primary else None,
            )

        if is_primary:
            await _clear_primary_for_owner(s, row.owner_user_id, exclude_node_id=node_id)
        row.is_primary = bool(is_primary)
        # 审计：业务写入前先占位，确保与 is_primary 一起提交。
        # 这里不能用 commit 之后的 begin_operation：commit 已结束，写入会留在外层
        # 但因为 begin_operation 内部只 flush、不 commit，会和业务行一起落到下一次提交。
        # 因此先 flush 业务行再写审计，最后统一 commit。
        await s.flush()
        if audit_principal is not None:
            from .dashboard_audit import begin_operation
            audit_row = await begin_operation(
                s, audit_principal,
                "set_primary_node", target=node_id,
                params={"is_primary": bool(is_primary)},
                ip=audit_ip, result="ok",
            )
            audit_row.before_json = before
            audit_row.after_json = {"is_primary": bool(row.is_primary), "owner_user_id": owner_after}
        await s.commit()
        await s.refresh(row)
        d = node_row_to_dict(row)
    # 任一节点的主节点标记变更，都可能影响同 owner 下其它节点的视图，
    # 全部刷一遍缓存（包含受影响节点的 owner），保证下一次列表返回最新状态。
    async with SessionLocal() as s2:
        affected = (
            await s2.execute(
                select(Node).where(
                    Node.owner_user_id.is_(row.owner_user_id)
                    if row.owner_user_id is None
                    else Node.owner_user_id == row.owner_user_id
                )
            )
        ).scalars().all()
    for r in affected:
        await store.cache_node(node_row_to_dict(r))

    # ── 阶段 2：迁移专属分组成员 ──
    # 仅在主节点标记真的翻转时才做迁移（同态 short-circuit 已 return，不会到这里）。
    # 取消主节点（is_primary=False）时只删旧成员、不补新成员。
    # 异常被隔离：失败只记日志，不影响主节点切换的返回值。
    #
    # victims 在阶段 0 之前就已收集；这里直接复用，避开"事务里清掉 victim 后
    # 阶段 2 找不到 victim"的回归。
    try:
        migrated = await migrate_primary_groups(
            store,
            old_primary_id=victims[0] if victims else None,
            new_primary_id=node_id if is_primary else None,
            owner_user_id=row.owner_user_id,
        )
        if migrated:
            logger.info(
                "primary migration: %d groups updated for owner=%s (%s -> %s)",
                len(migrated), row.owner_user_id,
                victims[0] if victims else None, node_id if is_primary else None,
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("primary migration failed (isolated): %s", e)

    return d, migrated


async def _collect_victim_for_migration(
    session, *, node_id: str, is_primary: bool,
) -> Optional[str]:
    """阶段 2 用：取一个 victim 给成员迁移（不需要列表，迁移在循环里取 victims）。

    设主时：取同 owner 下被替换的旧主节点 id（最多 1 个；owner 唯一性保证）；
    取消时：取 node_id 自身（如果它当前就是主节点）。
    非主 → 返回 None。
    """
    row = await session.get(Node, node_id)
    if not row:
        return None
    owner_user_id: Optional[int] = row.owner_user_id
    if is_primary:
        stmt = select(Node.node_id).where(Node.is_primary.is_(True))
        if owner_user_id is None:
            stmt = stmt.where(Node.owner_user_id.is_(None))
        else:
            stmt = stmt.where(Node.owner_user_id == owner_user_id)
        stmt = stmt.where(Node.node_id != node_id).limit(1)
        hit = (await session.execute(stmt)).first()
        return hit[0] if hit else None
    if row.is_primary:
        return node_id
    return None


async def migrate_primary_groups(
    store: RedisStore,
    *,
    old_primary_id: Optional[str],
    new_primary_id: Optional[str],
    owner_user_id: Optional[int],
) -> list[str]:
    """阶段 2：把专属分组里的旧主节点替换成新主节点。

    触发条件（由调用方保证）：
      - 主节点标记已成功翻转（事务 1 已 commit）
      - old_primary_id：被踢下台 / 被取消的旧主节点；无则跳过
      - new_primary_id：即将上任的新主节点（取消主节点时为 None）

    范围：
      - 按 owner_user_id 隔离（与 set_primary 维度一致）
      - 仅处理 _group_name 命名前缀的专属分组（trend_market-* / trend_limit-* / grid-*）
      - 其它手工分组 / 中控台"多区间方向过滤"产生的旧分组 **不** 动

    动作：
      - 新主节点已经存在于成员里 → 仅删除旧主节点成员
      - 新主节点不存在 → 删除旧主节点成员 + 加入新主节点成员（append 到末尾）
      - 取消主节点（new_primary_id is None）→ 仅删除旧主节点成员
      - 趋势策略（template_id=2）且 limit_watch_enabled=True 时，把新主节点成员的
        limit_watch_report 设为 True；其它情况保持 False

    返回：被改动的分组 ID 列表（用于响应头 / 审计）。
    """
    if not old_primary_id:
        # 没 victim = 主节点没换人（首次设主 / 取消主）→ 没东西可迁移
        return []

    # 1) 拉所有 owner 维度下的分组（走 Redis 缓存）
    all_groups = await store.all_groups()
    targets: list[dict] = []
    for g in all_groups:
        if not _is_quick_trigger_group(g.get("name") or ""):
            continue
        if not _same_owner(g.get("owner_user_id"), owner_user_id):
            continue
        members = g.get("members") or []
        if not any(m.get("node_id") == old_primary_id for m in members):
            continue
        targets.append(g)

    if not targets:
        return []

    # 2) 每个命中分组做成员替换（仅成员变动时落地，幂等）
    from .models import GroupUpdate  # 局部 import 避免循环
    from .group_service import _strategy_dict  # 复用查策略的现有助手
    changed: list[str] = []
    for g in targets:
        old_members: list[str] = [m.get("node_id") for m in g.get("members") or [] if m.get("node_id")]
        if old_primary_id not in old_members:
            continue
        new_members = [nid for nid in old_members if nid != old_primary_id]
        if new_primary_id and new_primary_id not in new_members:
            new_members.append(new_primary_id)
        if new_members == old_members:
            # 顺序/内容没变：幂等跳过
            continue
        # limit_watch_report：仅在新成员入局时考虑（v1 与趋势策略联动）
        limit_watch_enabled = bool(g.get("limit_watch_enabled"))
        # group_row_to_dict 没存 template_id，要按 strategy_id 查策略拿到
        sty = await _strategy_dict(store, g.get("strategy_id"))
        is_trend = bool(sty and sty.get("template_id") == "2")  # TEMPLATE_2_ID == 趋势
        report_for_new = bool(limit_watch_enabled and is_trend and new_primary_id)
        try:
            patch_kwargs: dict = {"node_ids": new_members}
            if report_for_new:
                # 仅在新成员入局且需打开 report 时才传；省略该字段让 update_group
                # 用 model_fields_set 判「未传」语义，避免触发空 list → 全员关的副作用
                patch_kwargs["limit_watch_node_ids"] = [new_primary_id]
            await group_service.update_group(
                store, g["group_id"], GroupUpdate(**patch_kwargs),
            )
            changed.append(g["group_id"])
        except ValueError as e:
            # validate_node_ids 拒绝（节点不存在 / 跨 owner）：单条分组跳过，不影响其它
            logger.warning("migrate group %s failed: %s", g.get("group_id"), e)
    return changed


def _is_quick_trigger_group(name: str) -> bool:
    """专属分组命名格式：`{trend_market|trend_limit|grid}-{user}-{login}-{symbol}`。

    只迁移这种命名规则的分组；手工分组不参与主节点切换联动。
    """
    if not name:
        return False
    head = name.split("-", 1)[0]
    return head in ("trend_market", "trend_limit", "grid")


def _same_owner(a: Optional[int], b: Optional[int]) -> bool:
    """两个 owner 是否等价（含 None）。"""
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return int(a) == int(b)


async def apply_risk_state(store: RedisStore, node_id: str, risk: dict) -> Optional[dict]:
    """节点回报触发后回写风控运行态（次数耗尽关闭开关等）。

    只合并「开关 / 剩余次数」，其余字段以库为准：节点回报的是它上次收到的快照，
    整份覆盖会把管理员在这期间保存的新配置冲掉。
    """
    async with SessionLocal() as s:
        row = await s.get(Node, node_id)
        if not row:
            return None
        merged = risk_control.merge_runtime_state(row.risk_json, risk)
        err = risk_control.validate_risk(merged)
        if err:
            raise ValueError(err)
        row.risk_json = merged
        await s.commit()
        await s.refresh(row)
        d = node_row_to_dict(row)
    await store.cache_node(d)
    return d


async def report_client_version(
    store: RedisStore, node_id: str, version: str
) -> Optional[dict]:
    """记录节点鉴权时上报的客户端版本（写库 + 刷新缓存）。

    版本没变就不落库：节点每次断线重连都会上报，否则每次重连都白写一次。
    因此 client_version_at 的语义是「该版本首次上报的时间」。
    """
    v = (version or "").strip()[:32]
    if not v:
        return None
    async with SessionLocal() as s:
        row = await s.get(Node, node_id)
        if row is None or row.client_version == v:
            return None
        row.client_version = v
        row.client_version_at = datetime.now()
        await s.commit()
        await s.refresh(row)
        d = node_row_to_dict(row)
    await store.cache_node(d)
    return d


async def delete_node(store: RedisStore, node_id: str) -> bool:
    """删除节点：先删库，再清理 Redis 缓存/快照/在线标记与分组成员关联。

    列表读的是 Redis。账本行已经不在、缓存还在时也要清掉，否则删除接口返回
    404，页面上的节点却一直还在。
    """
    cached = await store.get_node(node_id)
    async with SessionLocal() as s:
        row = await s.get(Node, node_id)
        credential = await s.get(NodeCredential, node_id)
        if row is None and credential is None and not cached:
            return False
        if credential is not None:
            await s.delete(credential)
        if row is not None:
            await s.delete(row)
        if row is not None or credential is not None:
            await s.commit()
    await store.delete_node(node_id)
    await group_service.remove_node_from_all_groups(store, node_id)
    return True
