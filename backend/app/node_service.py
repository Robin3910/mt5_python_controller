"""节点持久化：MySQL/SQLite 为权威，Redis 作缓存 + mt5_login 反查映射。

自 v0.2 起鉴权令牌改为全局共享（见 system_settings），节点本身不再持有 token；
节点的业务唯一键升级为 mt5_login。

所有写操作都遵循“先写库、再刷新缓存”的顺序，保证重启后能从库里恢复全部状态。
"""
import time
from datetime import datetime
from typing import Optional

from sqlalchemy import func, select, text

from . import group_service
from .db import SessionLocal
from . import risk_control
from .models import NodeCreate, NodeUpdate
from .orm import Node, NodeCredential
from .redis_store import RedisStore
from .rules import validate_node_global_lot_mode
from .security import make_node_id


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
            if needs_strict_audit and audit_principal is not None:
                from .dashboard_audit import begin_operation
                audit_row = await begin_operation(s, audit_principal, "assign_node", target=node_id,
                                                  params={"node_id": node_id}, ip=audit_ip, result="ok")
                audit_row.before_json = {"owner_user_id": previous_owner}
                audit_row.after_json = {"owner_user_id": owner_user_id, "approval_status": row.approval_status,
                                        "enabled": row.enabled}
        await s.commit()
        await s.refresh(row)
        d = node_row_to_dict(row)
    await store.cache_node(d)
    return d


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
