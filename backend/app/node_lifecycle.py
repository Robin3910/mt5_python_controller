"""单实例内串行化节点下发、归属变更与不可逆删除。"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from weakref import WeakValueDictionary

_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


@asynccontextmanager
async def locked(*keys: str):
    locks = [_locks.setdefault(key, asyncio.Lock()) for key in sorted(set(keys))]
    acquired = []
    try:
        for lock in locks:
            await lock.acquire()
            acquired.append(lock)
        yield
    finally:
        for lock in reversed(acquired):
            lock.release()


def node_key(node_id: str) -> str:
    return f"node:{node_id}"


def group_key(group_id: str) -> str:
    return f"group:{group_id}"
