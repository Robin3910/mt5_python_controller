"""尽力而为(best-effort)的持久化：写 MySQL/SQLite，永不阻塞交易主链路。

所有函数都吞掉异常并只记日志——即便数据库短暂不可用，也不能影响信号分发与下单。
"""
import logging
from dataclasses import asdict
from datetime import datetime
from typing import Optional

from sqlalchemy import func, select, update

from .db import SessionLocal
from .orm import AuditLog, SignalDispatch, SignalHistory

logger = logging.getLogger(__name__)


async def record_signal(signal_id, signal, source_ip=None, parsed_ok=True,
                        dispatch_mode=None, status="dispatching") -> None:
    """落库一条信号历史。"""
    try:
        async with SessionLocal() as s:
            s.add(
                SignalHistory(
                    signal_id=signal_id,
                    source_ip=source_ip,
                    raw_payload=str(asdict(signal)) if signal else None,
                    action=getattr(signal, "action", None),
                    symbol=getattr(signal, "symbol", None),
                    volume=getattr(signal, "volume", None),
                    sl=getattr(signal, "stop_loss", None),
                    tp=getattr(signal, "take_profit", None),
                    comment=getattr(signal, "comment", None),
                    parsed_ok=parsed_ok,
                    dispatch_mode=dispatch_mode,
                    status=status,
                )
            )
            await s.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("record_signal failed: %s", e)


async def record_dispatch(signal_id, node_id, decided_vol, gate_result,
                          skip_reason, status) -> None:
    """落库一条分发明细（pending / skipped）。"""
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
            await s.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("record_dispatch failed: %s", e)


async def update_dispatch_result(signal_id, node_id, status, result: Optional[dict] = None) -> None:
    """根据节点回报更新分发明细的最终结果。"""
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


async def audit(operator, action, target=None, params=None, result="ok", ip=None) -> None:
    """写一条操作审计。"""
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
                )
            )
            await s.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("audit failed: %s", e)
