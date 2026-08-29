"""分组限价挂单监听：把节点 MT5 上手动挂的「带关键字限价单」转成 strategy 信号。

只作用于绑定趋势策略（模版2）且开关打开的分组。识别与校验是纯函数；撤单、
分发、审计在编排层。复用 `process_signal`（与中控台手动触发同构），用
`group_ids` 点名全部命中分组。
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, Optional

from fastapi import HTTPException

from . import group_rules, persist, strategy_templates
from .connections import manager
from .models import SIGNAL_MODEL_STRATEGY
from .state import state

logger = logging.getLogger(__name__)

DEFAULT_KEYWORD = "limit"
KEYWORD_MAX_LEN = 32
SOURCE = "limit_watch"
AUDIT_ACTION = "limit_watch_signal"
AUDIT_OPERATOR = "system"
CANCEL_TIMEOUT = 8.0
# 撤单+分发过程中锁 ticket，避免下一轮快照重复触发
LOCK_TTL_SEC = 60
# 成功处理后记住 ticket，防止撤单滞后导致快照里还看得见
DONE_TTL_SEC = 86400
# 同一张不合格单的拒绝日志去重窗口
REJECT_TTL_SEC = 300

# req_id -> 等待撤单回包的 future
_pending_cancels: dict[str, asyncio.Future] = {}


def normalize_keyword(raw: object) -> str:
    """分组关键字：去空白，空则回落到默认 `limit`，超长截断。"""
    text = str(raw or "").strip()
    if not text:
        return DEFAULT_KEYWORD
    return text[:KEYWORD_MAX_LEN]


def is_trend_strategy(strategy: object) -> bool:
    """绑定策略是否为模版2（趋势策略）。"""
    if not isinstance(strategy, dict):
        return False
    return str(strategy.get("template_id") or "").strip().lower() == (
        strategy_templates.TEMPLATE_2_ID
    )


def keyword_matches(comment: object, keyword: object) -> bool:
    """订单注释是否包含该分组关键字（大小写不敏感）。"""
    needle = normalize_keyword(keyword)
    hay = str(comment or "")
    return needle.lower() in hay.lower() if needle else False


def _as_int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _as_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def order_ticket(order: object) -> int:
    if not isinstance(order, dict):
        return 0
    return _as_int(order.get("ticket"), 0)


def order_snapshot(order: object) -> dict:
    """审计 / raw_payload 用的挂单快照，只留对账字段。"""
    if not isinstance(order, dict):
        return {}
    return {
        "ticket": order_ticket(order),
        "symbol": order.get("symbol"),
        "type": order.get("type"),
        "pending_kind": order.get("pending_kind"),
        "volume": _as_float(order.get("volume")),
        "price_open": _as_float(order.get("price_open")),
        "sl": _as_float(order.get("sl")),
        "tp": _as_float(order.get("tp")),
        "magic": _as_int(order.get("magic")),
        "comment": str(order.get("comment") or ""),
        "time": order.get("time"),
    }


def order_fingerprint(order: object) -> str:
    snap = order_snapshot(order)
    return "|".join(
        str(snap.get(k) or "")
        for k in ("symbol", "type", "volume", "price_open", "sl", "tp", "comment")
    )


def is_limit_kind(order: object) -> bool:
    if not isinstance(order, dict):
        return False
    return str(order.get("pending_kind") or "").strip().lower() == "limit"


def is_manual_pending(order: object) -> bool:
    """只认 MT5 手工单（magic=0）。策略托管单自带魔术号，不能当触发单撤掉。"""
    if not isinstance(order, dict):
        return False
    return _as_int(order.get("magic"), 0) == 0


def order_incomplete_reason(order: object) -> Optional[str]:
    """限价触发单本身是否参数完整；与策略开仓准入分开，缺了直接拒绝、不撤单。"""
    if not isinstance(order, dict):
        return "不是有效的挂单"
    if not is_limit_kind(order):
        return "只接受限价挂单（BUY LIMIT / SELL LIMIT）"
    action = str(order.get("type") or "").strip().upper()
    if action not in ("BUY", "SELL"):
        return f"限价单方向非法：{order.get('type')}（应为 BUY / SELL）"
    if not str(order.get("symbol") or "").strip():
        return "限价单缺少品种"
    if _as_float(order.get("volume")) <= 0:
        return "限价单缺少手数"
    if _as_float(order.get("price_open")) <= 0:
        return "限价单缺少挂单价"
    if _as_float(order.get("sl")) <= 0:
        return "限价单缺少止损价（sl）"
    return None


def build_signal_payload(order: dict, group_ids: list[str]) -> dict:
    """把合格挂单组装成 Webhook / 手动触发同构的 strategy 信号体。"""
    action = str(order.get("type") or "").strip().upper()
    data: dict[str, Any] = {
        "action": action,
        "symbol": str(order.get("symbol") or "").strip(),
        "volume": _as_float(order.get("volume")),
        "model": SIGNAL_MODEL_STRATEGY,
        "sl": _as_float(order.get("sl")),
        "limit_price": _as_float(order.get("price_open")),
        "group_ids": list(group_ids),
    }
    tp = _as_float(order.get("tp"))
    if tp > 0:
        data["tp"] = tp
    comment = str(order.get("comment") or "").strip()
    if comment:
        data["comment"] = comment
    return data


def node_has_watch(groups: list[dict], node_id: str) -> bool:
    """该节点是否属于任一已开监听的分组（快路径，避免无开关时扫挂单）。"""
    nid = str(node_id or "")
    if not nid:
        return False
    for group in groups or []:
        if not group.get("limit_watch_enabled"):
            continue
        if not group.get("enabled", True):
            continue
        if nid in group_rules.member_ids(group):
            return True
    return False


def select_watch_targets(
    groups: list[dict],
    strategies: dict[str, dict],
    node_id: str,
    order: dict,
) -> tuple[list[dict], list[tuple[dict, str]]]:
    """从该节点所属分组里挑出应接收本张触发单的分组。

    入选：分组启用 + 监听开 + 节点是成员 + 注释匹配该组关键字 + 绑定启用中的
    趋势策略 + 品种匹配 + 通过 `entry_reject_reason`。
    返回 (可下发分组, 关键字已命中但被准入拦住的 (分组, 原因))。
    关键字都没碰上的分组不出现在任一侧。
    """
    nid = str(node_id or "")
    passing: list[dict] = []
    blocked: list[tuple[dict, str]] = []
    for group in groups or []:
        if nid not in group_rules.member_ids(group):
            continue
        if not group.get("limit_watch_enabled"):
            continue
        if not keyword_matches(order.get("comment"), group.get("limit_watch_keyword")):
            continue
        if not group.get("enabled", True):
            blocked.append((group, f"分组已禁用：{group.get('name') or group.get('group_id')}"))
            continue
        sid = str(group.get("strategy_id") or "").strip()
        strategy = strategies.get(sid) if sid else None
        if not is_trend_strategy(strategy):
            blocked.append((group, "限价单监听仅适用于绑定趋势策略的分组"))
            continue
        if not (strategy or {}).get("enabled", True):
            blocked.append((
                group,
                f"策略已禁用：{(strategy or {}).get('name') or sid}",
            ))
            continue
        if not group_rules.symbol_match((strategy or {}).get("symbol"), order.get("symbol")):
            blocked.append((
                group,
                f"策略品种 {(strategy or {}).get('symbol')} 与挂单品种 {order.get('symbol')} 不一致",
            ))
            continue
        reason = group_rules.entry_reject_reason(
            strategy or {},
            order.get("sl"),
            signal_action=order.get("type"),
            signal_entry_price=order.get("price_open"),
        )
        if reason:
            blocked.append((group, reason))
            continue
        passing.append(group)
    return passing, blocked


def _blocked_reason(blocked: list[tuple[dict, str]]) -> str:
    parts: list[str] = []
    for group, reason in blocked:
        name = group.get("name") or group.get("group_id")
        parts.append(f"{name}：{reason}")
    return "；".join(parts) if parts else "没有可接收的分组"


# ------------------------------------------------------------------
# 撤单 RPC（仿 market_probe：req_id 配对）
# ------------------------------------------------------------------
def build_cancel_command(req_id: str, ticket: int) -> dict:
    return {"cmd": "cancel_pending", "req_id": req_id, "ticket": int(ticket)}


def resolve_cancel(req_id: object, payload: dict) -> None:
    fut = _pending_cancels.pop(str(req_id or ""), None)
    if fut is not None and not fut.done():
        fut.set_result(payload or {})


def discard_cancel(req_id: str) -> None:
    _pending_cancels.pop(req_id, None)


def reset_cancel_waiters() -> None:
    """测试用：清空在途撤单等待。"""
    _pending_cancels.clear()


async def cancel_pending_on_node(node_id: str, ticket: int) -> dict:
    """向节点下发撤挂单并等待回包；失败返回 success=False，不抛。"""
    req_id = uuid.uuid4().hex[:16]
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    _pending_cancels[req_id] = fut
    try:
        sent = await manager.send_to_node(node_id, build_cancel_command(req_id, ticket))
        if not sent:
            return {"success": False, "ticket": ticket, "error": "节点当前离线，无法撤销挂单"}
        try:
            payload = await asyncio.wait_for(fut, timeout=CANCEL_TIMEOUT)
        except asyncio.TimeoutError:
            return {"success": False, "ticket": ticket, "error": "节点未在超时时间内确认撤单"}
        if not isinstance(payload, dict):
            return {"success": False, "ticket": ticket, "error": "撤单回包无法解析"}
        return payload
    finally:
        discard_cancel(req_id)


# ------------------------------------------------------------------
# 编排：账户快照里扫挂单
# ------------------------------------------------------------------
async def handle_account_orders(node_id: str, orders: list | None) -> None:
    """扫描节点上报的挂单；命中则校验、撤单、按手动触发同构分发给命中分组。"""
    store = state.store
    if store is None or not node_id:
        return
    groups = await store.all_groups()
    if not node_has_watch(groups, node_id):
        return
    strategies = {s["strategy_id"]: s for s in await store.all_strategies() if s.get("strategy_id")}
    for order in orders or []:
        if not isinstance(order, dict):
            continue
        try:
            await _handle_one(node_id, order, groups, strategies)
        except Exception:  # noqa: BLE001
            logger.exception(
                "limit_watch node %s ticket %s failed", node_id, order_ticket(order),
            )


async def _handle_one(
    node_id: str,
    order: dict,
    groups: list[dict],
    strategies: dict[str, dict],
) -> None:
    ticket = order_ticket(order)
    if ticket <= 0:
        return
    if not is_limit_kind(order) or not is_manual_pending(order):
        return

    passing, blocked = select_watch_targets(groups, strategies, node_id, order)
    if not passing and not blocked:
        return

    store = state.store
    if store is None:
        return
    if await store.is_limit_watch_done(node_id, ticket):
        return

    snap = order_snapshot(order)
    incomplete = order_incomplete_reason(order)
    if incomplete:
        await _reject(store, node_id, order, incomplete, passing + [g for g, _ in blocked])
        return
    if not passing:
        await _reject(store, node_id, order, _blocked_reason(blocked), [g for g, _ in blocked])
        return

    if not await store.try_lock_limit_watch(node_id, ticket, ttl=LOCK_TTL_SEC):
        return

    group_ids = [str(g["group_id"]) for g in passing if g.get("group_id")]
    group_names = [str(g.get("name") or g.get("group_id")) for g in passing]
    logger.info(
        "limit_watch node %s ticket %s matched %s, cancelling then dispatch",
        node_id, ticket, ",".join(group_ids),
    )
    try:
        cancel = await cancel_pending_on_node(node_id, ticket)
        if not cancel.get("success"):
            err = cancel.get("error") or "撤单失败"
            logger.warning(
                "limit_watch cancel failed node %s ticket %s: %s", node_id, ticket, err,
            )
            await _audit(
                node_id, snap, "cancel_failed",
                after={"cancel": cancel, "group_ids": group_ids, "reason": err},
            )
            await store.release_limit_watch_lock(node_id, ticket)
            return

        payload = build_signal_payload(order, group_ids)
        raw = json.dumps({"order": snap, "node_id": node_id, "group_ids": group_ids}, ensure_ascii=False)
        result = await _dispatch(payload, raw_payload=raw, dedup_key=f"{node_id}:{ticket}")
        await store.mark_limit_watch_done(node_id, ticket, ttl=DONE_TTL_SEC)
        status = str(result.get("status") or "ok")
        logger.info(
            "limit_watch dispatched node %s ticket %s signal=%s status=%s groups=%s",
            node_id, ticket, result.get("signal_id"), status, ",".join(group_ids),
        )
        await _audit(
            node_id, snap, status if status in ("accepted", "rejected", "duplicate") else "ok",
            after={
                "signal": payload,
                "result": {
                    "status": result.get("status"),
                    "signal_id": result.get("signal_id"),
                    "model": result.get("model"),
                    "mode": result.get("mode"),
                    "groups": result.get("groups"),
                    "targets": result.get("targets"),
                    "reason": result.get("reason"),
                },
                "cancel": {"success": True, "ticket": ticket},
                "group_ids": group_ids,
                "group_names": group_names,
            },
        )
    except Exception:  # noqa: BLE001
        await store.release_limit_watch_lock(node_id, ticket)
        raise


async def _dispatch(payload: dict, *, raw_payload: str, dedup_key: str) -> dict:
    from .webhook import process_signal

    dispatcher = state.dispatcher
    group_dispatcher = state.group_dispatcher
    store = state.store
    if dispatcher is None or group_dispatcher is None or store is None:
        raise RuntimeError("service not ready")
    try:
        return await process_signal(
            payload,
            source_ip=None,
            source=SOURCE,
            store=store,
            dispatcher=dispatcher,
            group_dispatcher=group_dispatcher,
            raw_payload=raw_payload,
            dedup_key=dedup_key,
        )
    except HTTPException as e:
        detail = e.detail
        logger.warning("limit_watch process_signal rejected: %s", detail)
        return {"status": "rejected", "reason": str(detail)}


async def _reject(
    store, node_id: str, order: dict, reason: str, groups: list[dict],
) -> None:
    ticket = order_ticket(order)
    fp = f"{reason}|{order_fingerprint(order)}"
    if not await store.note_limit_watch_reject(node_id, ticket, fp, ttl=REJECT_TTL_SEC):
        return
    logger.warning("limit_watch reject node %s ticket %s: %s", node_id, ticket, reason)
    await _audit(
        node_id, order_snapshot(order), "rejected",
        after={
            "reason": reason,
            "group_ids": [g.get("group_id") for g in groups],
            "group_names": [g.get("name") or g.get("group_id") for g in groups],
        },
    )


async def _audit(node_id: str, before: dict, result: str, *, after: dict) -> None:
    await persist.audit(
        AUDIT_OPERATOR, AUDIT_ACTION, node_id, before, result, None,
        category="console", before=before, after=after,
    )
