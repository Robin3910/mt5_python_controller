"""节点命令的「下发 + 按 req_id 等回包」公共层。

抽出前只有 `limit_watch` 私有一套 req_id 配对（`_pending_cancels`）；新增的
「人工删挂单」需要同一种「发命令 → 等回包 → 失败不抛」的语义。
两套实现会各自漂移，所以统一收口到这里，`limit_watch` 反过来复用本模块。

与 `market_probe` 同形（req_id 配对 + future 唤醒），差别只在于本模块是通用的。

约定：
- 回包一律带 `req_id`，由 `ws_gateway` 调 `resolve()` 唤醒对应 future；
- 失败（离线 / 超时 / 回包无法解析）返回 `success=False` 的 dict，**不抛异常**，
  让调用方按业务决定是记失败日志还是静默跳过。
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Optional

from .connections import manager

logger = logging.getLogger(__name__)

# 节点回包默认超时：与 limit_watch 撤单口径一致（8s）
DEFAULT_TIMEOUT = 8.0

# req_id -> 等待回包的 future
_pending: dict[str, asyncio.Future] = {}


def new_req_id() -> str:
    """生成一个 req_id（调用方需把它放进下发命令里）。"""
    return uuid.uuid4().hex[:16]


def resolve(req_id: object, payload: dict) -> None:
    """唤醒等待该 req_id 的请求方（由 ws_gateway 的回包分支调用）。"""
    fut = _pending.pop(str(req_id or ""), None)
    if fut is not None and not fut.done():
        fut.set_result(payload if isinstance(payload, dict) else {})


def discard(req_id: str) -> None:
    """丢弃等待项（超时 / 异常路径清理，避免 future 泄漏）。"""
    _pending.pop(str(req_id), None)


def pending_count() -> int:
    """在途等待数（测试与排障用）。"""
    return len(_pending)


def reset_waiters() -> None:
    """测试用：清空在途等待。"""
    _pending.clear()


async def request(
    node_id: str,
    cmd: dict[str, Any],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    req_id: Optional[str] = None,
    offline_error: str = "节点当前离线，无法执行",
    timeout_error: str = "节点未在超时时间内确认",
    bad_payload_error: str = "回包无法解析",
) -> dict:
    """向节点下发命令并等待对应回包。

    返回节点回包 dict；失败返回 `{"success": False, "error": ...}`，**不抛**。
    `cmd` 里若已带 req_id 则沿用，否则自动生成并注入返回值的 `_req_id` 字段。
    """
    rid = str(req_id or cmd.get("req_id") or new_req_id())
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    _pending[rid] = fut
    try:
        sent = await manager.send_to_node(node_id, {**cmd, "req_id": rid})
        if not sent:
            return {"success": False, "req_id": rid, "error": offline_error}
        try:
            payload = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(
                "node_rpc %s %s timeout after %.1fs (req_id=%s)",
                node_id, cmd.get("cmd"), timeout, rid,
            )
            return {"success": False, "req_id": rid, "error": timeout_error}
        if not isinstance(payload, dict):
            return {"success": False, "req_id": rid, "error": bad_payload_error}
        return payload
    finally:
        discard(rid)