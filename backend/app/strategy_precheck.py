"""策略配置预检：保存前问节点一句「这么改行不行」。

只有节点知道真实持仓、合约规格和现价，所以「改了之后 SL/TP 会不会已经越过现价」
这种判断只能在节点侧做。服务端在 PATCH 落库前向该策略下**在线**节点发一次
`strategy_precheck` 命令，等回包；有节点报出阻断项就把请求 400 掉。

与 market_probe 一样是请求-响应通道，用 req_id 配对，互不影响。

设计取舍：
- 节点离线时**不阻断**：离线节点本来就拿不到实时数据，阻断它等于让整个策略
  改不了。离线节点会在重连后拉最新快照，那时它自己会按新配置执行。
- 任一节点报阻断就阻断：多节点场景下只要有一个节点会进非法状态就该拦下。
- 节点超时**不阻断**：拿不到预检结果不该把合法的保存也拒了（后端还会用
  runtime_hints 做一遍上限下限兜底校验）。
"""
from __future__ import annotations

import asyncio
import logging
import uuid

from .connections import manager

logger = logging.getLogger(__name__)

# 等节点回包的超时。节点侧只是本地算一次，正常在百毫秒级
RPC_TIMEOUT = 5.0

# req_id -> 等待中的 future
_pending: dict[str, asyncio.Future] = {}


class PrecheckError(RuntimeError):
    """预检请求本身失败（节点离线 / 超时）。消息可直接展示给管理员。"""


def build_command(req_id: str, task_id: int, snapshot: dict) -> dict:
    return {
        "cmd": "strategy_precheck",
        "req_id": req_id,
        "task_id": task_id,
        "strategy": snapshot,
    }


def resolve(req_id: object, payload: dict) -> None:
    """收到节点回包时唤醒对应等待者（由 ws_gateway 调用）。"""
    fut = _pending.pop(str(req_id or ""), None)
    if fut is not None and not fut.done():
        fut.set_result(payload or {})


def discard(req_id: str) -> None:
    _pending.pop(req_id, None)


async def request(node_id: str, task_id: int, snapshot: dict) -> dict:
    """向一个节点问一次预检结论。

    返回 {"ok": bool, "blocks": [...], "message": str}；
    节点离线 / 超时抛 PrecheckError。
    """
    req_id = uuid.uuid4().hex[:16]
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    _pending[req_id] = fut
    try:
        sent = await manager.send_to_node(
            node_id, build_command(req_id, task_id, snapshot),
        )
        if not sent:
            raise PrecheckError("节点当前离线，无法预检")
        try:
            payload = await asyncio.wait_for(fut, timeout=RPC_TIMEOUT)
        except asyncio.TimeoutError:
            raise PrecheckError("节点未在超时时间内返回预检结果")
    finally:
        _pending.pop(req_id, None)

    if payload.get("error"):
        raise PrecheckError(str(payload["error"]))
    blocks = payload.get("blocks")
    blocks = blocks if isinstance(blocks, list) else []
    return {
        "ok": bool(payload.get("ok", not blocks)),
        "blocks": blocks,
        "message": str(payload.get("message") or ""),
    }


async def check_subtasks(subtasks: list[dict], snapshot: dict) -> dict:
    """对该策略下的在跑子任务逐个预检，汇总阻断项。

    返回 {"ok": bool, "blocks": [...], "message": str, "checked": int}：
    - 节点离线的子任务跳过（不阻断）
    - 任一节点报阻断 → ok=False
    """
    blocks: list[dict] = []
    checked = 0
    for sub in subtasks or []:
        node_id = str(sub.get("node_id") or "")
        if not node_id:
            continue
        try:
            task_id = int(sub.get("task_id"))
        except (TypeError, ValueError):
            continue
        try:
            result = await request(node_id, task_id, snapshot)
        except PrecheckError as e:
            logger.info("strategy precheck skipped: node %s task %s: %s", node_id, task_id, e)
            continue
        except Exception as e:  # noqa: BLE001
            logger.warning("strategy precheck failed: node %s task %s: %s", node_id, task_id, e)
            continue
        checked += 1
        if not result.get("ok"):
            for block in result.get("blocks") or []:
                item = dict(block)
                item["node_id"] = node_id
                item["task_id"] = task_id
                blocks.append(item)
    return {
        "ok": not blocks,
        "blocks": blocks,
        "message": "；".join(str(b.get("message") or "") for b in blocks),
        "checked": checked,
    }


def reset() -> None:
    """清空等待状态（测试用）。"""
    _pending.clear()
