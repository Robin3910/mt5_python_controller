"""节点行情探针：按需向在线节点索取一段 K 线与当前报价。

账户上报只带观察品种的报价、且不含 K 线，所以「看任意品种任意周期」的只读视图
（当前是趋势面板）需要一条请求-响应通道：下发 `market_probe` 命令并等节点回
`market_probe_result`，用 req_id 配对。这套等待与成交回报的 signal_id 等待
（results.py）彼此独立，互不影响分发链路的语义。

同一 (节点, 品种, 周期, 根数) 的结果在极短窗口内复用，收敛三种重复取数：
面板定时轮询、多名管理员同时看同一节点、以及面板上调参数（只影响本地计算，
不需要重新问节点）。窗口远小于轮询间隔，因此不会牺牲报价的实时性。
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid

from .connections import manager

logger = logging.getLogger(__name__)

# 结果复用窗口。取值须明显小于面板轮询间隔，否则会拖慢报价刷新
CACHE_SECONDS = 2.0
# 等节点回包的超时。节点侧是一次本地终端查询，正常在百毫秒级
RPC_TIMEOUT = 8.0
MAX_CACHE_ENTRIES = 256

# req_id -> 等待中的 future
_pending: dict[str, asyncio.Future] = {}
# (node_id, symbol, timeframe, count) -> (过期时刻, 结果)
_cache: dict[tuple, tuple[float, dict]] = {}
# 同一键在途的请求，多个等待者共享一次终端查询
_inflight: dict[tuple, asyncio.Task] = {}


class ProbeError(RuntimeError):
    """探针取数失败（节点离线 / 超时 / 节点侧读不到该品种）。消息可直接展示给管理员。"""


def build_command(req_id: str, symbol: str, timeframe: str, count: int) -> dict:
    return {
        "cmd": "market_probe",
        "req_id": req_id,
        "symbol": symbol,
        "timeframe": timeframe,
        "count": count,
    }


def resolve(req_id: object, payload: dict) -> None:
    """收到节点回包时唤醒对应等待者（由 ws_gateway 调用）。"""
    fut = _pending.pop(str(req_id or ""), None)
    if fut is not None and not fut.done():
        fut.set_result(payload or {})


def discard(req_id: str) -> None:
    _pending.pop(req_id, None)


def _prune(now: float) -> None:
    for key in [k for k, (expire_at, _) in _cache.items() if expire_at <= now]:
        _cache.pop(key, None)
    while len(_cache) > MAX_CACHE_ENTRIES:
        _cache.pop(next(iter(_cache)), None)


async def _request(node_id: str, symbol: str, timeframe: str, count: int) -> dict:
    """发一次探针命令并等回包；失败一律抛 ProbeError。"""
    req_id = uuid.uuid4().hex[:16]
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    _pending[req_id] = fut
    try:
        if not await manager.send_to_node(node_id, build_command(req_id, symbol, timeframe, count)):
            raise ProbeError("节点当前离线，无法读取行情")
        try:
            payload = await asyncio.wait_for(fut, timeout=RPC_TIMEOUT)
        except asyncio.TimeoutError:
            raise ProbeError("节点未在超时时间内返回行情")
    finally:
        _pending.pop(req_id, None)

    if payload.get("error"):
        raise ProbeError(str(payload["error"]))
    bars = payload.get("bars")
    if not isinstance(bars, list) or not bars:
        raise ProbeError(f"{symbol} {timeframe} 无可用 K 线")
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "bars": bars,
        "quote": payload.get("quote") if isinstance(payload.get("quote"), dict) else {},
        "fetched_at": time.time(),
    }


def _drop_inflight(key: tuple, task: asyncio.Task) -> None:
    if _inflight.get(key) is task:
        _inflight.pop(key, None)


async def fetch(node_id: str, symbol: str, timeframe: str, count: int) -> dict:
    """取 K 线与报价，带结果复用与在途合并。

    多个等待者共享同一个在途任务，且用 shield 保护：某个请求方中途断开被取消时，
    终端查询不会被连带取消，其余等待者照常拿到结果。
    """
    key = (node_id, symbol, timeframe, count)
    now = time.time()
    hit = _cache.get(key)
    if hit and hit[0] > now:
        return {**hit[1], "cached": True}

    task = _inflight.get(key)
    if task is None or task.done():
        task = asyncio.create_task(_request(node_id, symbol, timeframe, count))
        _inflight[key] = task
        task.add_done_callback(lambda t, k=key: _drop_inflight(k, t))
    payload = await asyncio.shield(task)

    _cache[key] = (time.time() + CACHE_SECONDS, payload)
    _prune(now)
    return {**payload, "cached": False}


def quote_price(quote: dict | None) -> float:
    """从报价里取用于比对 EMA 的价位：优先中间价，缺失时用买卖价折中。"""
    src = quote if isinstance(quote, dict) else {}
    for field in ("mid", "bid", "ask", "last"):
        try:
            value = float(src.get(field) or 0.0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 0.0


def reset() -> None:
    """清空缓存与在途状态（测试用）。"""
    _cache.clear()
    _pending.clear()
    _inflight.clear()
