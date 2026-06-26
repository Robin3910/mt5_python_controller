"""Redis 访问层：实时状态、运行期配置、轮询队列、分布式锁。

Redis 在本系统中承担“实时态”职责（在线状态、账户快照、运行期配置、信号去重、
执行锁、轮询进度/队列）；持久化（节点账本、审计、信号历史）在 MySQL（见 persist.py）。
"""
from __future__ import annotations

import json
import time
from typing import Optional

import redis.asyncio as aioredis

from .settings import settings

# ---- Redis key 模板（集中管理，避免散落各处拼字符串）----
K_NODE = "node:{}"               # 节点元数据缓存（JSON）
K_TOKEN = "node:token:{}"        # token 哈希 -> node_id
K_NODES = "nodes"                # 所有 node_id 的集合
K_ONLINE = "node:online:{}"      # 在线标记（带 TTL）
K_ACCOUNT = "node:account:{}"    # 账户快照（JSON）
K_LOT_GLOBAL = "config:lot:global"   # 全局手数配置
K_FILTERS = "config:filters"     # 多区间方向过滤配置
K_DISPATCH = "config:dispatch"   # 分发模式 / 持仓判定范围
K_DEDUP = "dedup:{}"             # 信号去重指纹（带 TTL）
K_EXEC_LOCK = "lock:exec:{}:{}"  # (node, symbol) 执行锁
K_POLL_PENDING = "signal:poll:pending"  # 轮询待处理队列（List）
K_POLL_PROGRESS = "signal:poll:{}"      # 单条轮询信号的进度（JSON）


class RedisStore:
    def __init__(self, client: aioredis.Redis) -> None:
        self.r = client

    @classmethod
    def from_url(cls, url: Optional[str] = None) -> "RedisStore":
        # decode_responses=True：直接拿到 str，省去手动 decode
        return cls(aioredis.from_url(url or settings.redis_url, decode_responses=True))

    async def close(self) -> None:
        try:
            await self.r.aclose()
        except Exception:
            pass

    # ----------------- 节点元数据缓存 -----------------
    async def cache_node(self, node: dict) -> None:
        """写入/更新节点缓存，并登记到节点集合。"""
        nid = node["node_id"]
        await self.r.set(K_NODE.format(nid), json.dumps(node))
        await self.r.sadd(K_NODES, nid)

    async def get_node(self, node_id: str) -> Optional[dict]:
        raw = await self.r.get(K_NODE.format(node_id))
        return json.loads(raw) if raw else None

    async def all_nodes(self) -> list[dict]:
        ids = await self.r.smembers(K_NODES)
        out: list[dict] = []
        for nid in ids:
            n = await self.get_node(nid)
            if n:
                out.append(n)
        return out

    async def delete_node(self, node_id: str) -> None:
        """删除节点时，连带清理 token 映射、账户快照、在线标记。"""
        n = await self.get_node(node_id)
        if n and n.get("token_hash"):
            await self.r.delete(K_TOKEN.format(n["token_hash"]))
        await self.r.delete(K_NODE.format(node_id))
        await self.r.delete(K_ACCOUNT.format(node_id))
        await self.r.delete(K_ONLINE.format(node_id))
        await self.r.srem(K_NODES, node_id)

    # ----------------- token 映射 -----------------
    async def set_token(self, token_hash: str, node_id: str) -> None:
        await self.r.set(K_TOKEN.format(token_hash), node_id)

    async def del_token(self, token_hash: str) -> None:
        await self.r.delete(K_TOKEN.format(token_hash))

    async def node_by_token(self, token_hash: str) -> Optional[str]:
        """WS 握手时用 token 哈希反查节点（O(1)）。"""
        return await self.r.get(K_TOKEN.format(token_hash))

    # ----------------- 在线状态 / 心跳 -----------------
    async def touch_online(self, node_id: str) -> None:
        """续期在线标记；超过 ONLINE_TTL 未续期则自动判定离线。"""
        await self.r.set(K_ONLINE.format(node_id), "1", ex=settings.online_ttl)

    async def set_offline(self, node_id: str) -> None:
        await self.r.delete(K_ONLINE.format(node_id))

    async def is_online(self, node_id: str) -> bool:
        return bool(await self.r.exists(K_ONLINE.format(node_id)))

    # ----------------- 账户快照 -----------------
    async def save_account(self, node_id: str, snapshot: dict) -> None:
        await self.r.set(K_ACCOUNT.format(node_id), json.dumps(snapshot))

    async def get_account(self, node_id: str) -> Optional[dict]:
        raw = await self.r.get(K_ACCOUNT.format(node_id))
        return json.loads(raw) if raw else None

    # ----------------- 运行期配置 -----------------
    async def get_lot_global(self) -> dict:
        raw = await self.r.get(K_LOT_GLOBAL)
        return json.loads(raw) if raw else {"enabled": False, "value": 0.1}

    async def set_lot_global(self, cfg: dict) -> None:
        await self.r.set(K_LOT_GLOBAL, json.dumps(cfg))

    async def get_filters(self) -> dict:
        raw = await self.r.get(K_FILTERS)
        return json.loads(raw) if raw else {}

    async def set_filters(self, cfg: dict) -> None:
        await self.r.set(K_FILTERS, json.dumps(cfg))

    async def get_dispatch(self) -> dict:
        raw = await self.r.get(K_DISPATCH)
        if raw:
            return json.loads(raw)
        # 未配置时回退到环境默认
        return {"mode": settings.dispatch_mode, "position_scope": settings.position_scope}

    async def set_dispatch(self, cfg: dict) -> None:
        await self.r.set(K_DISPATCH, json.dumps(cfg))

    # ----------------- 幂等 / 锁 -----------------
    async def seen_signal(self, fingerprint: str) -> bool:
        """信号去重：用 SET NX EX 实现窗口内幂等；返回 True 表示是重复信号。"""
        ok = await self.r.set(
            K_DEDUP.format(fingerprint), "1", nx=True, ex=settings.dedup_window
        )
        return not bool(ok)

    async def acquire_exec_lock(self, node_id: str, symbol: str, ttl_ms: int = 10000) -> bool:
        """获取 (节点, 品种) 执行锁；带 TTL，异常情况下可自动释放。"""
        return bool(
            await self.r.set(K_EXEC_LOCK.format(node_id, symbol), "1", nx=True, px=ttl_ms)
        )

    async def release_exec_lock(self, node_id: str, symbol: str) -> None:
        await self.r.delete(K_EXEC_LOCK.format(node_id, symbol))

    # ----------------- 轮询队列 -----------------
    async def poll_enqueue(self, signal_id: str) -> None:
        await self.r.lpush(K_POLL_PENDING, signal_id)

    async def poll_dequeue(self) -> Optional[str]:
        # LPUSH + RPOP 构成 FIFO
        return await self.r.rpop(K_POLL_PENDING)

    async def save_poll_progress(self, signal_id: str, progress: dict) -> None:
        # 进度保留 1 天，便于排查与续跑
        await self.r.set(K_POLL_PROGRESS.format(signal_id), json.dumps(progress), ex=86400)

    async def get_poll_progress(self, signal_id: str) -> Optional[dict]:
        raw = await self.r.get(K_POLL_PROGRESS.format(signal_id))
        return json.loads(raw) if raw else None
