"""尽力而为(best-effort)的持久化：写 MySQL/SQLite，永不阻塞交易主链路。

所有函数都吞掉异常并只记日志——即便数据库短暂不可用，也不能影响信号分发与下单。
"""
import logging
from dataclasses import asdict
from datetime import datetime
from typing import Optional

from sqlalchemy import func, or_, select, update

from .db import SessionLocal
from .orm import AuditLog, SignalDispatch, SignalHistory

logger = logging.getLogger(__name__)


async def record_signal(signal_id, signal, source_ip=None, parsed_ok=True,
                        dispatch_mode=None, status="dispatching",
                        raw_payload: Optional[str] = None,
                        source: str = "tradingview") -> None:
    """落库一条信号历史。

    source：信号来源，tradingview（外部 Webhook）/ manual（中控台手动触发）。
    """
    payload_str = raw_payload
    if payload_str is None and signal is not None:
        payload_str = str(asdict(signal))
    try:
        async with SessionLocal() as s:
            s.add(
                SignalHistory(
                    signal_id=signal_id,
                    source_ip=source_ip,
                    raw_payload=payload_str,
                    action=getattr(signal, "action", None),
                    symbol=getattr(signal, "symbol", None),
                    volume=getattr(signal, "volume", None),
                    sl=getattr(signal, "stop_loss", None),
                    tp=getattr(signal, "take_profit", None),
                    comment=getattr(signal, "comment", None),
                    parsed_ok=parsed_ok,
                    dispatch_mode=dispatch_mode,
                    status=status,
                    source=source,
                )
            )
            await s.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("record_signal failed: %s", e)


async def record_dispatch(signal_id, node_id, decided_vol, gate_result,
                          skip_reason, status) -> None:
    """落库一条分发明细（pending / skipped），终态时尝试收口信号整体状态。"""
    try:
        async with SessionLocal() as s:
            s.add(
                SignalDispatch(
                    signal_id=signal_id,
                    node_id=node_id,
                    decided_vol=decided_vol,
                    gate_result=gate_result,
                    skip_reason=skip_reason,
                    status=status,
                    dispatched_at=datetime.now(),
                )
            )
            if status in _DISPATCH_TERMINAL:
                await s.flush()
                await _refresh_signal_status(s, signal_id)
            await s.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("record_dispatch failed: %s", e)


# 分发明细终态；仍在途的不计入整体收口
_DISPATCH_TERMINAL = frozenset({"done", "failed", "skipped"})
_DISPATCH_PENDING = frozenset({"pending", "sent"})


def _aggregate_signal_status(dispatch_statuses: list[str]) -> Optional[str]:
    """根据各节点分发明细汇总信号整体状态。

    - 仍有 pending/sent → 继续 dispatching（返回 None 表示暂不改）
    - 既有成功又有失败 → partial
    - 任一成功（无失败）→ done
    - 全部失败 → failed
    - 全部跳过 → done（处理已结束，只是无人成交）
    """
    if not dispatch_statuses:
        return None
    if any(st in _DISPATCH_PENDING for st in dispatch_statuses):
        return None
    has_done = any(st == "done" for st in dispatch_statuses)
    has_failed = any(st == "failed" for st in dispatch_statuses)
    if has_done and has_failed:
        return "partial"
    if has_done:
        return "done"
    if has_failed:
        return "failed"
    if all(st in _DISPATCH_TERMINAL for st in dispatch_statuses):
        return "done"
    return None


async def _refresh_signal_status(session, signal_id: str) -> None:
    """按当前分发明细收口 SignalHistory.status（仅在全部明细终态时更新）。"""
    rows = (
        await session.execute(
            select(SignalDispatch.status).where(SignalDispatch.signal_id == signal_id)
        )
    ).scalars().all()
    new_status = _aggregate_signal_status(list(rows))
    if not new_status:
        return
    await session.execute(
        update(SignalHistory)
        .where(SignalHistory.signal_id == signal_id)
        .values(status=new_status)
    )


async def update_signal_status(signal_id: str, status: str) -> None:
    """显式更新信号整体状态（如轮询无人领取 → failed）。"""
    try:
        async with SessionLocal() as s:
            await s.execute(
                update(SignalHistory)
                .where(SignalHistory.signal_id == signal_id)
                .values(status=status)
            )
            await s.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("update_signal_status failed: %s", e)


async def update_dispatch_result(signal_id, node_id, status, result: Optional[dict] = None) -> None:
    """根据节点回报更新分发明细的最终结果，并尝试收口信号整体状态。"""
    result = result or {}
    try:
        async with SessionLocal() as s:
            await s.execute(
                update(SignalDispatch)
                .where(
                    SignalDispatch.signal_id == signal_id,
                    SignalDispatch.node_id == node_id,
                )
                .values(
                    status=status,
                    retcode=result.get("retcode"),
                    order_ticket=result.get("order") or result.get("ticket"),
                    deal=result.get("deal"),
                    price=result.get("price"),
                    error=result.get("error"),
                    finished_at=datetime.now(),
                )
            )
            hist_vals: dict = {}
            if result.get("symbol"):
                hist_vals["symbol"] = result.get("symbol")
            if result.get("volume") is not None:
                hist_vals["volume"] = result.get("volume")
            if result.get("detail"):
                hist_vals["comment"] = result.get("detail")
            if hist_vals:
                await s.execute(
                    update(SignalHistory)
                    .where(SignalHistory.signal_id == signal_id)
                    .values(**hist_vals)
                )
            await _refresh_signal_status(s, signal_id)
            await s.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("update_dispatch_result failed: %s", e)


async def record_manual_close(signal_id: str, node_id: str, target: str,
                              symbol: Optional[str] = None, ticket: Optional[int] = None) -> None:
    """落库一条手动平仓记录，供详情页成交回报展示。"""
    labels = {
        "all": "手动全平",
        "symbol": f"手动平品种 {symbol or ''}".strip(),
        "ticket": f"手动平订单 #{ticket}" if ticket else "手动平仓",
    }
    try:
        async with SessionLocal() as s:
            s.add(
                SignalHistory(
                    signal_id=signal_id,
                    action="CLOSE",
                    symbol=symbol,
                    parsed_ok=True,
                    dispatch_mode="manual",
                    status="dispatching",
                    comment=labels.get(target, "手动平仓"),
                )
            )
            s.add(
                SignalDispatch(
                    signal_id=signal_id,
                    node_id=node_id,
                    gate_result="passed",
                    status="sent",
                    dispatched_at=datetime.now(),
                )
            )
            await s.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("record_manual_close failed: %s", e)


def _dispatch_rows(rows) -> list[dict]:
    """把 ORM 查询结果序列化为 API 明细 dict。"""
    return [
        {
            "id": d.id,
            # —— 信号原始数据 ——
            "signal_id": d.signal_id,
            "symbol": sig.symbol if sig else None,
            "action": sig.action if sig else None,
            "volume": sig.volume if sig else None,
            "sl": sig.sl if sig else None,
            "tp": sig.tp if sig else None,
            "comment": sig.comment if sig else None,
            "source_ip": sig.source_ip if sig else None,
            "parsed_ok": sig.parsed_ok if sig else None,
            "dispatch_mode": sig.dispatch_mode if sig else None,
            "signal_status": sig.status if sig else None,
            "received_at": sig.received_at.timestamp() if sig and sig.received_at else None,
            "raw_payload": sig.raw_payload if sig else None,
            # —— 本节点处理情况 ——
            "decided_vol": d.decided_vol,
            "gate_result": d.gate_result,
            "skip_reason": d.skip_reason,
            "status": d.status,
            "retcode": d.retcode,
            "order": d.order_ticket,
            "deal": d.deal,
            "price": d.price,
            "error": d.error,
            "dispatched_at": d.dispatched_at.timestamp() if d.dispatched_at else None,
            "finished_at": d.finished_at.timestamp() if d.finished_at else None,
        }
        for d, sig in rows
    ]


def _webhook_signal_filter():
    """排除手动平仓等非 Webhook 来源的信号历史。"""
    return or_(SignalHistory.dispatch_mode.is_(None), SignalHistory.dispatch_mode != "manual")


def _signal_event_row(sig: SignalHistory, dispatches: list[dict]) -> dict:
    return {
        "signal_id": sig.signal_id,
        "received_at": sig.received_at.timestamp() if sig.received_at else None,
        "source_ip": sig.source_ip,
        "raw_payload": sig.raw_payload,
        "action": sig.action,
        "symbol": sig.symbol,
        "volume": sig.volume,
        "sl": sig.sl,
        "tp": sig.tp,
        "comment": sig.comment,
        "parsed_ok": sig.parsed_ok,
        "dispatch_mode": sig.dispatch_mode,
        "status": sig.status,
        "source": sig.source,
        "dispatches": dispatches,
    }


def _signal_dispatch_row(d: SignalDispatch, node_name: Optional[str]) -> dict:
    return {
        "id": d.id,
        "node_id": d.node_id,
        "node_name": node_name,
        "decided_vol": d.decided_vol,
        "gate_result": d.gate_result,
        "skip_reason": d.skip_reason,
        "status": d.status,
        "retcode": d.retcode,
        "order": d.order_ticket,
        "deal": d.deal,
        "price": d.price,
        "error": d.error,
        "dispatched_at": d.dispatched_at.timestamp() if d.dispatched_at else None,
        "finished_at": d.finished_at.timestamp() if d.finished_at else None,
    }


async def recent_webhook_events(page: int = 1, page_size: int = 20,
                                node_names: Optional[dict[str, str]] = None) -> dict:
    """分页读取 Webhook 信号事件（含各节点后续处理明细）。"""
    page = max(1, page)
    page_size = max(1, min(page_size, 100))
    offset = (page - 1) * page_size
    names = node_names or {}
    try:
        async with SessionLocal() as s:
            filt = _webhook_signal_filter()
            total = (
                await s.execute(select(func.count()).select_from(SignalHistory).where(filt))
            ).scalar_one()

            rows = (
                await s.execute(
                    select(SignalHistory)
                    .where(filt)
                    .order_by(SignalHistory.received_at.desc())
                    .offset(offset)
                    .limit(page_size)
                )
            ).scalars().all()

            signal_ids = [r.signal_id for r in rows]
            dispatch_map: dict[str, list[dict]] = {sid: [] for sid in signal_ids}
            if signal_ids:
                disp_rows = (
                    await s.execute(
                        select(SignalDispatch)
                        .where(SignalDispatch.signal_id.in_(signal_ids))
                        .order_by(SignalDispatch.id.asc())
                    )
                ).scalars().all()
                for d in disp_rows:
                    dispatch_map.setdefault(d.signal_id, []).append(
                        _signal_dispatch_row(d, names.get(d.node_id))
                    )

            return {
                "items": [_signal_event_row(sig, dispatch_map.get(sig.signal_id, [])) for sig in rows],
                "total": total,
                "page": page,
                "page_size": page_size,
            }
    except Exception as e:  # noqa: BLE001
        logger.warning("recent_webhook_events failed: %s", e)
        return {"items": [], "total": 0, "page": page, "page_size": page_size}


async def recent_dispatches(node_id: str, page: int = 1, page_size: int = 20) -> dict:
    """分页读取某节点最近的「信号 + 本节点处理」明细（左连 signal_history）。

    返回 items / total / page / page_size，供详情页「信号」与「成交回报」Tab 共用。
    """
    page = max(1, page)
    page_size = max(1, min(page_size, 100))
    offset = (page - 1) * page_size
    try:
        async with SessionLocal() as s:
            total = (
                await s.execute(
                    select(func.count())
                    .select_from(SignalDispatch)
                    .where(SignalDispatch.node_id == node_id)
                )
            ).scalar_one()

            stmt = (
                select(SignalDispatch, SignalHistory)
                .join(
                    SignalHistory,
                    SignalHistory.signal_id == SignalDispatch.signal_id,
                    isouter=True,
                )
                .where(SignalDispatch.node_id == node_id)
                .order_by(SignalDispatch.id.desc())
                .offset(offset)
                .limit(page_size)
            )
            rows = (await s.execute(stmt)).all()
            return {
                "items": _dispatch_rows(rows),
                "total": total,
                "page": page,
                "page_size": page_size,
            }
    except Exception as e:  # noqa: BLE001
        logger.warning("recent_dispatches failed: %s", e)
        return {"items": [], "total": 0, "page": page, "page_size": page_size}


async def audit(
    operator,
    action,
    target=None,
    params=None,
    result="ok",
    ip=None,
    *,
    category: Optional[str] = None,
    before=None,
    after=None,
) -> None:
    """写一条操作审计。

    category：console（中控台）/ node（节点）/ system（其它）
    before / after：操作前后数据快照（dict 或可 JSON 序列化对象）。
    """
    try:
        async with SessionLocal() as s:
            s.add(
                AuditLog(
                    operator=operator,
                    action=action,
                    target=target,
                    params_json=params,
                    result=result,
                    ip=ip,
                    category=category,
                    before_json=before,
                    after_json=after,
                )
            )
            await s.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("audit failed: %s", e)


def _audit_row(row: AuditLog) -> dict:
    return {
        "id": row.id,
        "ts": row.ts.timestamp() if row.ts else None,
        "operator": row.operator,
        "action": row.action,
        "target": row.target,
        "params": row.params_json,
        "result": row.result,
        "ip": row.ip,
        "category": row.category,
        "before": row.before_json,
        "after": row.after_json,
    }


async def recent_audits(
    page: int = 1,
    page_size: int = 20,
    categories: Optional[list[str]] = None,
) -> dict:
    """分页读取操作审计（默认中控台 + 节点）。"""
    page = max(1, page)
    page_size = max(1, min(page_size, 100))
    offset = (page - 1) * page_size
    cats = categories or ["console", "node"]
    try:
        async with SessionLocal() as s:
            filt = AuditLog.category.in_(cats)
            total = (
                await s.execute(select(func.count()).select_from(AuditLog).where(filt))
            ).scalar_one()
            rows = (
                await s.execute(
                    select(AuditLog)
                    .where(filt)
                    .order_by(AuditLog.id.desc())
                    .offset(offset)
                    .limit(page_size)
                )
            ).scalars().all()
            return {
                "items": [_audit_row(r) for r in rows],
                "total": total,
                "page": page,
                "page_size": page_size,
            }
    except Exception as e:  # noqa: BLE001
        logger.warning("recent_audits failed: %s", e)
        return {"items": [], "total": 0, "page": page, "page_size": page_size}
