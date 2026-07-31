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
K_NODE_BY_LOGIN = "node:by_login:{}"  # mt5_login -> node_id（用于 WS 握手快速反查）
K_NODES = "nodes"                # 所有 node_id 的集合
K_ONLINE = "node:online:{}"      # 在线标记（带 TTL）
K_ACCOUNT = "node:account:{}"    # 账户快照（JSON）
K_FILTERS = "config:filters"     # 多区间方向过滤配置
K_NODE_TOKEN = "config:node_token"   # 全局节点接入令牌（明文）
K_DEDUP = "dedup:{}"             # 信号去重指纹（带 TTL）
K_EXEC_LOCK = "lock:exec:{}:{}"  # (node, symbol) 执行锁
K_POLL_PENDING = "signal:poll:pending"  # 轮询待处理队列（List）
K_POLL_PROGRESS = "signal:poll:{}"      # 单条轮询信号的进度（JSON）
K_POLL_ROTATION = "signal:poll:rotation:{}"  # 按品种的轮转顺序（JSON list[node_id]）
# ---- strategy 分组链路（与上面按币种的 key 命名空间完全分离）----
K_GROUP = "group:{}"                    # 分组元数据缓存（JSON，含成员 node_id 列表）
K_GROUPS = "groups"                     # 所有 group_id 的集合
K_GROUP_ROTATION = "group:poll:rotation:{}"  # 分组内轮询轮转顺序（JSON list[node_id]）
# 节点级互斥：(分组, 节点) -> 进行中的子任务号。同一分组内一个节点只允许一个策略
# 任务；不同分组各自独立，同一节点可以同时承接多个分组的任务。
K_GROUP_NODE_BUSY = "group:node:busy:{}:{}"
K_STRATEGY = "strategy:{}"              # 策略实例缓存（JSON）
K_STRATEGIES = "strategies"             # 所有 strategy_id 的集合


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
        """写入/更新节点缓存，并登记到节点集合 + mt5_login 反查索引。"""
        nid = node["node_id"]
        await self.r.set(K_NODE.format(nid), json.dumps(node))
        await self.r.sadd(K_NODES, nid)
        login = node.get("mt5_login")
        if login:
            await self.r.set(K_NODE_BY_LOGIN.format(int(login)), nid)

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
        """删除节点时，连带清理 mt5_login 反查索引、账户快照、在线标记。"""
        n = await self.get_node(node_id)
        if n and n.get("mt5_login"):
            await self.r.delete(K_NODE_BY_LOGIN.format(int(n["mt5_login"])))
        await self.r.delete(K_NODE.format(node_id))
        await self.r.delete(K_ACCOUNT.format(node_id))
        await self.r.delete(K_ONLINE.format(node_id))
        await self.r.srem(K_NODES, node_id)

    # ----------------- mt5_login 反查 -----------------
    async def node_by_mt5_login(self, mt5_login: int) -> Optional[str]:
        """WS 握手用 MT5 登录号反查节点 ID（O(1)）。"""
        return await self.r.get(K_NODE_BY_LOGIN.format(int(mt5_login)))

    # ----------------- 全局节点令牌（缓存） -----------------
    async def get_node_token(self) -> Optional[str]:
        return await self.r.get(K_NODE_TOKEN)

    async def set_node_token(self, token: str) -> None:
        await self.r.set(K_NODE_TOKEN, token)

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
    async def get_filters(self) -> dict:
        raw = await self.r.get(K_FILTERS)
        return json.loads(raw) if raw else {}

    async def set_filters(self, cfg: dict) -> None:
        await self.r.set(K_FILTERS, json.dumps(cfg))

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

    # ----------------- 轮询轮转顺序（按品种） -----------------
    async def get_poll_rotation(self, symbol: str) -> list[str]:
        """读取某品种的轮转顺序（node_id 有序列表）；不存在则返回空列表。"""
        raw = await self.r.get(K_POLL_ROTATION.format(symbol))
        return json.loads(raw) if raw else []

    async def save_poll_rotation(self, symbol: str, order: list[str]) -> None:
        """持久化某品种的轮转顺序（领取成功后把消费节点移到队尾，重启后仍延续轮转）。"""
        await self.r.set(K_POLL_ROTATION.format(symbol), json.dumps(order))

    # ----------------- 分组（strategy 链路） -----------------
    async def cache_group(self, group: dict) -> None:
        """写入/更新分组缓存，并登记到分组集合。"""
        gid = group["group_id"]
        await self.r.set(K_GROUP.format(gid), json.dumps(group))
        await self.r.sadd(K_GROUPS, gid)

    async def get_group(self, group_id: str) -> Optional[dict]:
        raw = await self.r.get(K_GROUP.format(group_id))
        return json.loads(raw) if raw else None

    async def all_groups(self) -> list[dict]:
        ids = await self.r.smembers(K_GROUPS)
        out: list[dict] = []
        for gid in ids:
            g = await self.get_group(gid)
            if g:
                out.append(g)
        return out

    async def delete_group(self, group_id: str) -> None:
        """删除分组时连带清理其轮转顺序与组内各节点的互斥占位。"""
        await self.r.delete(K_GROUP.format(group_id))
        await self.r.delete(K_GROUP_ROTATION.format(group_id))
        await self.clear_group_node_busy(group_id)
        await self.r.srem(K_GROUPS, group_id)

    # ---- 组内节点互斥（同一分组内一个节点只跑一个策略任务，跨分组各自独立）----
    async def acquire_group_node_busy(
        self, group_id: str, node_id: str, marker: str, ttl: int,
    ) -> bool:
        """抢占组内节点占位。marker 先写占位串，拿到子任务号后再覆盖。

        TTL 是兜底：节点永久离线时避免该位置被永久锁死，到期后自动放行。
        """
        return bool(
            await self.r.set(
                K_GROUP_NODE_BUSY.format(group_id, node_id), marker, nx=True, ex=ttl,
            )
        )

    async def set_group_node_busy(
        self, group_id: str, node_id: str, marker: str, ttl: int,
    ) -> None:
        """覆盖占位内容（抢占成功后写入真实子任务号），保持原 TTL 语义。"""
        await self.r.set(K_GROUP_NODE_BUSY.format(group_id, node_id), marker, ex=ttl)

    async def get_group_node_busy(self, group_id: str, node_id: str) -> Optional[str]:
        return await self.r.get(K_GROUP_NODE_BUSY.format(group_id, node_id))

    async def release_group_node_busy(self, group_id: str, node_id: str) -> None:
        await self.r.delete(K_GROUP_NODE_BUSY.format(group_id, node_id))

    async def clear_group_node_busy(self, group_id: str) -> None:
        """清掉某分组下全部节点占位（分组被删除时）。"""
        keys = [
            key async for key in self.r.scan_iter(
                match=K_GROUP_NODE_BUSY.format(group_id, "*")
            )
        ]
        if keys:
            await self.r.delete(*keys)

    async def clear_trade_runtime(self) -> int:
        """清空交易相关 Redis 运行态（轮询队列/进度、组内占位、去重、执行锁）。

        节点 / 分组 / 策略 / 过滤配置与在线状态保留。返回删除的 key 数量。
        """
        patterns = (
            K_POLL_PENDING,
            "signal:poll:*",
            "group:node:busy:*",
            "dedup:*",
            "lock:exec:*",
        )
        deleted = 0
        seen: set[str] = set()
        for pattern in patterns:
            if "*" in pattern:
                keys = [key async for key in self.r.scan_iter(match=pattern)]
            else:
                keys = [pattern] if await self.r.exists(pattern) else []
            fresh = [k for k in keys if k not in seen]
            if not fresh:
                continue
            seen.update(fresh)
            deleted += int(await self.r.delete(*fresh) or 0)
        return deleted

    async def get_group_rotation(self, group_id: str) -> list[str]:
        """读取分组内的轮转顺序（node_id 有序列表）；不存在则返回空列表。"""
        raw = await self.r.get(K_GROUP_ROTATION.format(group_id))
        return json.loads(raw) if raw else []

    async def save_group_rotation(self, group_id: str, order: list[str]) -> None:
        """持久化分组轮转顺序（领取成功后把消费节点移到队尾，重启后仍延续轮转）。"""
        await self.r.set(K_GROUP_ROTATION.format(group_id), json.dumps(order))

    # ----------------- 策略实例 -----------------
    async def cache_strategy(self, strategy: dict) -> None:
        sid = strategy["strategy_id"]
        await self.r.set(K_STRATEGY.format(sid), json.dumps(strategy))
        await self.r.sadd(K_STRATEGIES, sid)

    async def get_strategy(self, strategy_id: str) -> Optional[dict]:
        raw = await self.r.get(K_STRATEGY.format(strategy_id))
        return json.loads(raw) if raw else None

    async def all_strategies(self) -> list[dict]:
        ids = await self.r.smembers(K_STRATEGIES)
        out: list[dict] = []
        for sid in ids:
            s = await self.get_strategy(sid)
            if s:
                out.append(s)
        return out

    async def delete_strategy(self, strategy_id: str) -> None:
        await self.r.delete(K_STRATEGY.format(strategy_id))
        await self.r.srem(K_STRATEGIES, strategy_id)
