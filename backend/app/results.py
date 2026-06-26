"""跨模块的“成交回报等待”注册表。

轮询模式下，分发器发出开仓命令后会在这里登记一个 future 并等待；节点回报到达
ws_gateway 时再 resolve 对应 future，从而把“异步回报”变成“可 await 的结果”。
"""
import asyncio
from typing import Any

# key = "{signal_id}:{node_id}" -> 等待中的 future
_futures: dict[str, asyncio.Future] = {}


def key(signal_id: str, node_id: str) -> str:
    return f"{signal_id}:{node_id}"


def register(signal_id: str, node_id: str) -> asyncio.Future:
    """登记一个等待回报的 future（在当前事件循环上创建）。"""
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    _futures[key(signal_id, node_id)] = fut
    return fut


def resolve(signal_id: str, node_id: str, result: Any) -> None:
    """收到回报时唤醒对应 future。"""
    fut = _futures.pop(key(signal_id, node_id), None)
    if fut is not None and not fut.done():
        fut.set_result(result)


def discard(signal_id: str, node_id: str) -> None:
    """放弃等待（超时/发送失败时清理）。"""
    _futures.pop(key(signal_id, node_id), None)
