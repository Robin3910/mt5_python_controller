"""带单持久化：跟单关系的增删查，以及节点 / 用户生命周期变化时的连带清理。

与 group_service 分工：group_service 管分组自身与 owner 成员；这里只管
group_follower_node 这一张跟单关系表。所有写操作遵循「先写库、再刷新分组缓存」，
保证重启后能从库里恢复（warm_cache 已把 followers 一并预热）。

关联节点生命周期时**必须**调用本模块的清理函数，否则会留下永远不会被分发的脏关系：
- group_service.delete_group      -> 分组没了，级联删（已内置在删除事务里）
- group_service.remove_node_from_all_groups -> 节点被删 / 归属转移
- node_service.set_primary        -> 跟单节点升主节点后不再是可跟单节点
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import delete, func, select

from .db import SessionLocal
from .group_service import _load_group
from .orm import GroupFollowerNode
from .redis_store import RedisStore

logger = logging.getLogger(__name__)


def _row_to_dict(row: GroupFollowerNode) -> dict:
    return {
        "id": row.id,
        "group_id": row.group_id,
        "node_id": row.node_id,
        "follower_user_id": row.follower_user_id,
        "leader_user_id": row.leader_user_id,
        "leader_primary_node_id": row.leader_primary_node_id,
        "sort_order": row.sort_order or 0,
        "joined_at": row.joined_at.timestamp() if row.joined_at else None,
    }


async def _refresh_groups(store: RedisStore, group_ids: set[str]) -> None:
    """把指定分组的缓存重新读库刷新（跟单关系变了，缓存里的 followers 必须跟上）。"""
    if not group_ids:
        return
    async with SessionLocal() as s:
        for gid in group_ids:
            d = await _load_group(s, gid)
            if d:
                await store.cache_group(d)


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------
async def list_followings(follower_user_id: int) -> list[dict]:
    """某跟单员的全部跟单关系（按加入时间正序）。"""
    async with SessionLocal() as s:
        rows = (
            await s.execute(
                select(GroupFollowerNode)
                .where(GroupFollowerNode.follower_user_id == int(follower_user_id))
                .order_by(GroupFollowerNode.joined_at.asc(), GroupFollowerNode.id.asc())
            )
        ).scalars().all()
    return [_row_to_dict(r) for r in rows]


async def list_for_groups(group_ids: list[str]) -> list[dict]:
    """若干分组的全部跟单关系（带单员侧展示「被谁跟随」用）。"""
    ids = [str(g).strip() for g in (group_ids or []) if str(g or "").strip()]
    if not ids:
        return []
    async with SessionLocal() as s:
        rows = (
            await s.execute(
                select(GroupFollowerNode)
                .where(GroupFollowerNode.group_id.in_(ids))
                .order_by(GroupFollowerNode.joined_at.asc(), GroupFollowerNode.id.asc())
            )
        ).scalars().all()
    return [_row_to_dict(r) for r in rows]


async def list_for_leaders(leader_user_ids: set[int] | list[int]) -> list[dict]:
    """若干带单员名下的全部跟单关系（统计「被多少节点跟随」用）。"""
    ids = [int(i) for i in (leader_user_ids or []) if i]
    if not ids:
        return []
    async with SessionLocal() as s:
        rows = (
            await s.execute(
                select(GroupFollowerNode)
                .where(GroupFollowerNode.leader_user_id.in_(ids))
                .order_by(GroupFollowerNode.joined_at.asc(), GroupFollowerNode.id.asc())
            )
        ).scalars().all()
    return [_row_to_dict(r) for r in rows]


def follows_node(group: Optional[dict], node_id: str, follower_user_id: int) -> bool:
    """分组缓存里该用户是否真的把某节点放进来跟单（移出前的归属兜底）。

    只认 follower_user_id 一致的记录 —— 同一分组下别人的跟单节点不该被我动。
    """
    target = str(node_id or "").strip()
    if not target or not group:
        return False
    for f in (group.get("followers") or []):
        if not isinstance(f, dict):
            continue
        if str(f.get("node_id") or "").strip() != target:
            continue
        if int(f.get("follower_user_id") or 0) == int(follower_user_id):
            return True
    return False


async def count_by_group() -> dict[str, int]:
    """各分组的跟单人数（分组列表的「跟单 N」列）。"""
    try:
        async with SessionLocal() as s:
            rows = (
                await s.execute(
                    select(GroupFollowerNode.group_id, func.count())
                    .group_by(GroupFollowerNode.group_id)
                )
            ).all()
        return {gid: int(cnt) for gid, cnt in rows}
    except Exception as e:  # noqa: BLE001
        logger.warning("follow count_by_group failed: %s", e)
        return {}


async def following_group_count(follower_user_id: int) -> int:
    """某跟单员已跟的分组数（前端提示用）。"""
    async with SessionLocal() as s:
        return int(
            await s.scalar(
                select(func.count(func.distinct(GroupFollowerNode.group_id))).where(
                    GroupFollowerNode.follower_user_id == int(follower_user_id),
                )
            )
            or 0
        )


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------
async def add_following(
    store: RedisStore,
    *,
    group_id: str,
    node_id: str,
    follower_user_id: int,
    leader_user_id: int,
    leader_primary_node_id: Optional[str],
) -> bool:
    """加入一个节点的跟单关系；返回 True 表示新加入，False 表示已存在（幂等）。

    一个分组一个事务，失败即整条不写入，避免留下半条关系。
    """
    async with SessionLocal() as s:
        exists = (
            await s.execute(
                select(GroupFollowerNode).where(
                    GroupFollowerNode.group_id == group_id,
                    GroupFollowerNode.node_id == node_id,
                )
            )
        ).scalar_one_or_none()
        if exists:
            return False
        s.add(GroupFollowerNode(
            group_id=group_id,
            node_id=node_id,
            follower_user_id=int(follower_user_id),
            leader_user_id=int(leader_user_id),
            leader_primary_node_id=leader_primary_node_id,
            sort_order=0,
        ))
        await s.commit()
    await _refresh_groups(store, {group_id})
    return True


async def adopt_followers_for_group(
    store: RedisStore,
    *,
    target_group: dict,
    source_group_ids: list[str],
) -> tuple[list[dict], list[dict]]:
    """把来源分组里已存在的跟单节点写入目标分组；返回 (adopted, skipped)。

    用于「带单员新建分组 + 策略时，把跟过自己其它分组的节点一并带进来」——
    跨策略类型 / 品种都生效（分组名按 策略类型-用户名-MT5号-品种 生成）。

    三条口径：
    - **只搬运已存在的跟单关系**，绝不引入「本来就没跟我」的节点；
    - 同一节点跟了多个来源分组时按 node_id 去重，follower_user_id 取首次出现的那条；
    - 逐条走 add_following，已存在时返回 False 进 skipped，天然幂等。

    leader_user_id / leader_primary_node_id **一律从源关系取**，不作入参：源关系里
    已存了这两个字段（写入时快照），复用既避免管理员名下分组（owner 为 None）
    触发 int(None) 崩溃，也保证与源关系完全一致。
    """
    target_gid = str((target_group or {}).get("group_id") or "").strip()
    src_ids = [
        str(g).strip() for g in (source_group_ids or [])
        if str(g or "").strip() and str(g).strip() != target_gid
    ]
    if not target_gid or not src_ids:
        return [], []

    # 按 node_id 去重，保留首次出现的来源分组，便于回显「继承自哪个分组」
    seen: dict[str, dict] = {}
    for f in await list_for_groups(src_ids):
        nid = str(f.get("node_id") or "").strip()
        if nid and nid not in seen:
            seen[nid] = f

    adopted: list[dict] = []
    skipped: list[dict] = []
    for nid, src in seen.items():
        follower_uid = src.get("follower_user_id")
        leader_uid = src.get("leader_user_id")
        if not follower_uid or not leader_uid:
            skipped.append({"node_id": nid, "reason": "跟单关系缺少归属信息"})
            continue
        ok = await add_following(
            store,
            group_id=target_gid,
            node_id=nid,
            follower_user_id=int(follower_uid),
            leader_user_id=int(leader_uid),
            leader_primary_node_id=src.get("leader_primary_node_id"),
        )
        row = {
            "node_id": nid,
            "follower_user_id": int(follower_uid),
            "from_group_id": src.get("group_id"),
        }
        (adopted if ok else skipped).append(row)
    return adopted, skipped


async def remove_following(
    store: RedisStore,
    *,
    group_id: str,
    node_ids: list[str],
    follower_user_id: Optional[int] = None,
) -> list[str]:
    """移出跟单；返回本次请求涉及的 node_ids（已按 ids 去空白保序）。

    follower_user_id 为空表示不限跟单员（节点生命周期清理时用）。
    删除本身是幂等的：不存在的关系不会报错，重复调用安全。
    """
    ids = [str(n).strip() for n in (node_ids or []) if str(n or "").strip()]
    if not ids:
        return []
    async with SessionLocal() as s:
        stmt = delete(GroupFollowerNode).where(
            GroupFollowerNode.group_id == group_id,
            GroupFollowerNode.node_id.in_(ids),
        )
        if follower_user_id is not None:
            stmt = stmt.where(
                GroupFollowerNode.follower_user_id == int(follower_user_id)
            )
        await s.execute(stmt)
        await s.commit()
    await _refresh_groups(store, {group_id})
    return ids


# ---------------------------------------------------------------------------
# 节点 / 用户生命周期连带清理（最容易漏的地方）
# ---------------------------------------------------------------------------
async def remove_node_followings(store: RedisStore, node_id: str) -> list[str]:
    """把一个节点从所有跟单关系中摘出；返回受影响的分组 ID。

    调用时机：节点被删除、节点归属转移（assign_nodes）、节点升为主节点。
    """
    gid = str(node_id or "").strip()
    if not gid:
        return []
    async with SessionLocal() as s:
        affected = [
            r for (r,) in (
                await s.execute(
                    select(GroupFollowerNode.group_id).where(
                        GroupFollowerNode.node_id == gid,
                    ).distinct()
                )
            ).all()
        ]
        if not affected:
            return []
        await s.execute(
            delete(GroupFollowerNode).where(GroupFollowerNode.node_id == gid)
        )
        await s.commit()
    await _refresh_groups(store, set(affected))
    return affected


async def remove_user_followings(
    store: RedisStore, follower_user_id: int,
) -> list[str]:
    """清空某跟单员的全部跟单关系（用户被删除时用）。"""
    uid = int(follower_user_id)
    async with SessionLocal() as s:
        affected = [
            r for (r,) in (
                await s.execute(
                    select(GroupFollowerNode.group_id).where(
                        GroupFollowerNode.follower_user_id == uid,
                    ).distinct()
                )
            ).all()
        ]
        if not affected:
            return []
        await s.execute(
            delete(GroupFollowerNode).where(GroupFollowerNode.follower_user_id == uid)
        )
        await s.commit()
    await _refresh_groups(store, set(affected))
    return affected