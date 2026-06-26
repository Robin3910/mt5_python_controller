"""节点持久化：MySQL/SQLite 为权威，Redis 作缓存 + token 映射。

所有写操作都遵循“先写库、再刷新缓存”的顺序，保证重启后能从库里恢复全部状态。
"""
import time
from typing import Optional

from sqlalchemy import select

from .db import SessionLocal
from .models import NodeCreate, NodeUpdate
from .orm import Node
from .redis_store import RedisStore
from .security import gen_token, hash_token, make_node_id


def node_row_to_dict(row: Node) -> dict:
    """ORM 行 -> Redis 缓存用的 dict（created_at 转成 epoch 便于排序）。"""
    return {
        "node_id": row.node_id,
        "name": row.name,
        "token_hash": row.token_hash,
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
    """启动时把库里的节点全部预热进 Redis（含 token 映射）。"""
    async with SessionLocal() as s:
        rows = (await s.execute(select(Node))).scalars().all()
    for row in rows:
        d = node_row_to_dict(row)
        await store.cache_node(d)
        await store.set_token(row.token_hash, row.node_id)
    return len(rows)


async def create_node(store: RedisStore, payload: NodeCreate) -> tuple[dict, str]:
    """创建节点：生成 token（只此一次返回明文），写库后刷新缓存。"""
    node_id = make_node_id()
    token = gen_token()
    th = hash_token(token)
    async with SessionLocal() as s:
        row = Node(
            node_id=node_id,
            name=payload.name,
            token_hash=th,
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
    await store.set_token(th, node_id)
    return d, token


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


async def rotate_token(store: RedisStore, node_id: str) -> Optional[str]:
    """重置令牌：旧 token 映射立即失效，返回新明文 token。"""
    token = gen_token()
    th = hash_token(token)
    async with SessionLocal() as s:
        row = await s.get(Node, node_id)
        if not row:
            return None
        old_hash = row.token_hash
        row.token_hash = th
        await s.commit()
    await store.del_token(old_hash)
    await store.set_token(th, node_id)
    n = await store.get_node(node_id)
    if n:
        n["token_hash"] = th
        await store.cache_node(n)
    return token


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
