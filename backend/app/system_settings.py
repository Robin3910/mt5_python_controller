"""系统级 key/value 配置（持久化）。

当前职责：全局节点接入令牌 NODE_TOKEN —— 所有节点共享同一令牌（明文存 DB，
Redis 作缓存，便于管理员在「账户设置」页查看/复制/重置）。
"""
from __future__ import annotations

import time
from typing import Optional

from sqlalchemy import select

from .db import SessionLocal
from .orm import SystemSetting
from .redis_store import RedisStore
from .security import gen_token

KEY_NODE_TOKEN = "node_token"


async def _get_setting(key: str) -> Optional[SystemSetting]:
    async with SessionLocal() as s:
        row = await s.get(SystemSetting, key)
        return row


async def _upsert_setting(key: str, value: str) -> SystemSetting:
    async with SessionLocal() as s:
        row = await s.get(SystemSetting, key)
        if row is None:
            row = SystemSetting(key=key, value=value)
            s.add(row)
        else:
            row.value = value
        await s.commit()
        await s.refresh(row)
        return row


async def get_node_token(store: RedisStore) -> tuple[str, float]:
    """读取当前全局节点令牌，返回 (token, updated_at_epoch)。

    优先读 Redis 缓存；未命中再回源到 MySQL/SQLite，并刷新缓存。
    """
    cached = await store.get_node_token()
    if cached:
        row = await _get_setting(KEY_NODE_TOKEN)
        updated_at = row.updated_at.timestamp() if (row and row.updated_at) else 0.0
        return cached, updated_at
    row = await _get_setting(KEY_NODE_TOKEN)
    if row is None:
        return "", 0.0
    await store.set_node_token(row.value)
    return row.value, (row.updated_at.timestamp() if row.updated_at else time.time())


async def rotate_node_token(store: RedisStore) -> tuple[str, float]:
    """生成新的全局节点令牌，写库 + 刷新缓存，返回 (token, updated_at_epoch)。"""
    token = gen_token()
    row = await _upsert_setting(KEY_NODE_TOKEN, token)
    await store.set_node_token(token)
    return token, (row.updated_at.timestamp() if row.updated_at else time.time())


async def ensure_node_token(store: RedisStore) -> str:
    """启动期幂等：若全局令牌不存在则生成一个；返回当前令牌（用于日志记录）。"""
    row = await _get_setting(KEY_NODE_TOKEN)
    if row and row.value:
        await store.set_node_token(row.value)
        return row.value
    token = gen_token()
    await _upsert_setting(KEY_NODE_TOKEN, token)
    await store.set_node_token(token)
    return token
