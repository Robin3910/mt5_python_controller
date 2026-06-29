"""节点持久化：MySQL/SQLite 为权威，Redis 作缓存 + mt5_login 反查映射。

自 v0.2 起鉴权令牌改为全局共享（见 system_settings），节点本身不再持有 token；
节点的业务唯一键升级为 mt5_login。

所有写操作都遵循“先写库、再刷新缓存”的顺序，保证重启后能从库里恢复全部状态。
"""
import time
from typing import Optional

from sqlalchemy import select

from .db import SessionLocal
from .models import NodeCreate, NodeUpdate
from .orm import Node
from .redis_store import RedisStore
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
        "mt5_login": row.mt5_login,
        "mt5_server": row.mt5_server,
        "created_at": row.created_at.timestamp() if row.created_at else time.time(),
    }


async def warm_cache(store: RedisStore) -> int:
    """启动时把库里的节点全部预热进 Redis（含 mt5_login 反查索引）。"""
    async with SessionLocal() as s:
        rows = (await s.execute(select(Node))).scalars().all()
    for row in rows:
        await store.cache_node(node_row_to_dict(row))
    return len(rows)


# 节点自动注册时使用的默认配置（与管理后台「+ 新建节点」表单的图示一致）
AUTO_NODE_DEFAULTS = {
    "lot_mode": "fixed",
    "lot": 0.01,
    "follow_sync": True,
    "follow_poll": True,
    "poll_order": 0,
    "filters": None,
}


def _default_name(mt5_login: int) -> str:
    return f"node-{mt5_login}"


async def find_by_mt5_login(mt5_login: int) -> Optional[dict]:
    """按 MT5 登录号在库中查找节点（不走缓存，用于 WS 握手时的权威判定）。"""
    async with SessionLocal() as s:
        row = (
            await s.execute(select(Node).where(Node.mt5_login == int(mt5_login)))
        ).scalar_one_or_none()
        return node_row_to_dict(row) if row else None


async def create_node(store: RedisStore, payload: NodeCreate) -> dict:
    """创建节点：写库 + 刷新缓存。mt5_login 重复会触发 IntegrityError，由路由层捕获。"""
    name = (payload.name or "").strip() or _default_name(payload.mt5_login)
    node_id = make_node_id()
    async with SessionLocal() as s:
        row = Node(
            node_id=node_id,
            name=name,
            enabled=True,
            lot_mode=payload.lot_mode,
            lot=payload.lot,
            follow_sync=payload.follow_sync,
            follow_poll=payload.follow_poll,
            poll_order=payload.poll_order,
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

    若已存在则直接返回现有节点（幂等）；不存在则用默认配置入库。
    """
    existing = await find_by_mt5_login(mt5_login)
    if existing:
        return existing
    payload = NodeCreate(
        name=_default_name(mt5_login),
        mt5_login=mt5_login,
        **AUTO_NODE_DEFAULTS,
    )
    return await create_node(store, payload)


async def update_node(store: RedisStore, node_id: str, patch: NodeUpdate) -> Optional[dict]:
    """更新节点：仅写入“非空”字段；成功后刷新缓存。"""
    async with SessionLocal() as s:
        row = await s.get(Node, node_id)
        if not row:
            return None
        for f in ("name", "enabled", "lot_mode", "lot", "follow_sync", "follow_poll", "poll_order"):
            v = getattr(patch, f, None)
            if v is not None:
                setattr(row, f, v)
        if patch.filters is not None:
            row.filters_json = patch.filters
        await s.commit()
        await s.refresh(row)
        d = node_row_to_dict(row)
    await store.cache_node(d)
    return d


async def delete_node(store: RedisStore, node_id: str) -> bool:
    """删除节点：先删库，再清理 Redis 缓存/快照/在线标记。"""
    async with SessionLocal() as s:
        row = await s.get(Node, node_id)
        if not row:
            return False
        await s.delete(row)
        await s.commit()
    await store.delete_node(node_id)
    return True
