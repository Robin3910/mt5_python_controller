"""分组持久化：MySQL/SQLite 为权威，Redis 作缓存（与节点账本同样的读写顺序）。

分组是 strategy 信号（model=strategy）的分发单元：分组自带 sync/poll 分发模式，
成员节点通过 node_group_member 关联，一个节点可同时属于多个分组。

所有写操作都遵循“先写库、再刷新缓存”，保证重启后能从库里恢复全部状态。
"""
from __future__ import annotations

import time
from typing import Optional

from sqlalchemy import delete, select

from .db import SessionLocal
from .group_rules import normalize_dispatch_mode
from .models import GroupCreate, GroupUpdate
from .orm import NodeGroup, NodeGroupMember
from .redis_store import RedisStore
from .security import make_group_id


def group_row_to_dict(row: NodeGroup, members: list[NodeGroupMember]) -> dict:
    """ORM 行 -> Redis 缓存用的 dict（created_at 转成 epoch 便于排序）。"""
    ordered = sorted(members, key=lambda m: (m.sort_order or 0, m.node_id))
    return {
        "group_id": row.group_id,
        "name": row.name,
        "enabled": row.enabled,
        "dispatch_mode": row.dispatch_mode,
        "remark": row.remark,
        "created_at": row.created_at.timestamp() if row.created_at else time.time(),
        "members": [
            {"node_id": m.node_id, "sort_order": m.sort_order or 0} for m in ordered
        ],
    }


async def _load_group(session, group_id: str) -> Optional[dict]:
    row = await session.get(NodeGroup, group_id)
    if not row:
        return None
    members = (
        await session.execute(
            select(NodeGroupMember).where(NodeGroupMember.group_id == group_id)
        )
    ).scalars().all()
    return group_row_to_dict(row, list(members))


async def warm_cache(store: RedisStore) -> int:
    """启动时把库里的分组（含成员）全部预热进 Redis。"""
    async with SessionLocal() as s:
        rows = (await s.execute(select(NodeGroup))).scalars().all()
        members = (await s.execute(select(NodeGroupMember))).scalars().all()
    by_group: dict[str, list[NodeGroupMember]] = {}
    for m in members:
        by_group.setdefault(m.group_id, []).append(m)
    for row in rows:
        await store.cache_group(group_row_to_dict(row, by_group.get(row.group_id, [])))
    return len(rows)


def _dedup_node_ids(node_ids: Optional[list[str]]) -> list[str]:
    """去重并保持传入顺序（顺序即组内轮询顺序）。"""
    out: list[str] = []
    seen: set[str] = set()
    for nid in node_ids or []:
        key = str(nid).strip()
        if key and key not in seen:
            out.append(key)
            seen.add(key)
    return out


async def validate_node_ids(store: RedisStore, node_ids: list[str]) -> Optional[str]:
    """校验成员节点均存在；返回错误说明或 None。"""
    for nid in node_ids:
        if not await store.get_node(nid):
            return f"节点不存在：{nid}"
    return None


async def _replace_members(session, group_id: str, node_ids: list[str]) -> None:
    """整体替换分组成员（传入顺序即组内轮询顺序）。"""
    await session.execute(
        delete(NodeGroupMember).where(NodeGroupMember.group_id == group_id)
    )
    for idx, nid in enumerate(node_ids):
        session.add(NodeGroupMember(group_id=group_id, node_id=nid, sort_order=idx))


async def name_exists(name: str, exclude_group_id: Optional[str] = None) -> bool:
    """分组名称是否已被占用（分组名用于日志与信号明细展示，要求唯一）。"""
    async with SessionLocal() as s:
        stmt = select(NodeGroup.group_id).where(NodeGroup.name == name)
        if exclude_group_id:
            stmt = stmt.where(NodeGroup.group_id != exclude_group_id)
        return (await s.execute(stmt)).first() is not None


async def create_group(store: RedisStore, payload: GroupCreate) -> dict:
    """创建分组：写库 + 刷新缓存。"""
    node_ids = _dedup_node_ids(payload.node_ids)
    err = await validate_node_ids(store, node_ids)
    if err:
        raise ValueError(err)
    group_id = make_group_id()
    async with SessionLocal() as s:
        s.add(
            NodeGroup(
                group_id=group_id,
                name=payload.name.strip(),
                enabled=payload.enabled,
                dispatch_mode=normalize_dispatch_mode(payload.dispatch_mode),
                remark=(payload.remark or "").strip() or None,
            )
        )
        await _replace_members(s, group_id, node_ids)
        await s.commit()
        d = await _load_group(s, group_id)
    await store.cache_group(d)
    return d


async def update_group(store: RedisStore, group_id: str, patch: GroupUpdate) -> Optional[dict]:
    """更新分组：仅写入提供的字段；node_ids 传入即整体替换成员。"""
    node_ids: Optional[list[str]] = None
    if patch.node_ids is not None:
        node_ids = _dedup_node_ids(patch.node_ids)
        err = await validate_node_ids(store, node_ids)
        if err:
            raise ValueError(err)
    async with SessionLocal() as s:
        row = await s.get(NodeGroup, group_id)
        if not row:
            return None
        if patch.name is not None and patch.name.strip():
            row.name = patch.name.strip()
        if patch.enabled is not None:
            row.enabled = patch.enabled
        if patch.dispatch_mode is not None:
            row.dispatch_mode = normalize_dispatch_mode(patch.dispatch_mode)
        if patch.remark is not None:
            row.remark = patch.remark.strip() or None
        if node_ids is not None:
            await _replace_members(s, group_id, node_ids)
        await s.commit()
        d = await _load_group(s, group_id)
    await store.cache_group(d)
    return d


async def delete_group(store: RedisStore, group_id: str) -> bool:
    """删除分组：先删库（含成员关联），再清理 Redis 缓存与轮转顺序。"""
    async with SessionLocal() as s:
        row = await s.get(NodeGroup, group_id)
        if not row:
            return False
        await s.execute(delete(NodeGroupMember).where(NodeGroupMember.group_id == group_id))
        await s.delete(row)
        await s.commit()
    await store.delete_group(group_id)
    return True


async def remove_node_from_all_groups(store: RedisStore, node_id: str) -> list[str]:
    """节点被删除时，把它从所有分组成员中摘掉；返回受影响的分组 ID。"""
    async with SessionLocal() as s:
        rows = (
            await s.execute(
                select(NodeGroupMember.group_id).where(NodeGroupMember.node_id == node_id)
            )
        ).scalars().all()
        affected = sorted(set(rows))
        if not affected:
            return []
        await s.execute(delete(NodeGroupMember).where(NodeGroupMember.node_id == node_id))
        await s.commit()
        for gid in affected:
            d = await _load_group(s, gid)
            if d:
                await store.cache_group(d)
    return affected
