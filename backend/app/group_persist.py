"""strategy 分组链路的持久化：主任务与各节点明细（best-effort，永不阻塞交易）。

与 `persist.py` 的分工：
- `persist.py` 负责 signal_history（两条链路共用的信号台账）与 signal_dispatch（normal 链路）；
- 本模块只读写 group_signal_task / group_task_dispatch（strategy 链路），
  并在明细收口时同步刷新 signal_history 的整体状态。

主任务号 task_id 由数据库自增生成，`magic = GROUP_TASK_MAGIC_BASE + task_id`，
下发给节点后会作为 MT5 订单魔术号写入，便于从成交单反查主任务。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import func, select, update

from . import group_rules
from .db import SessionLocal
from .orm import GroupSignalTask, GroupTaskDispatch, SignalHistory

logger = logging.getLogger(__name__)

# 明细终态；仍在途的不参与主任务收口
_TERMINAL = frozenset({"done", "failed", "skipped", "offline"})


async def create_task(
    *,
    signal_id: str,
    group: dict,
    signal,
    dispatch_mode: str,
    source_ip: Optional[str] = None,
    raw_payload: Optional[str] = None,
) -> Optional[dict]:
    """在下发节点之前创建一条分组主任务，返回 {task_id, magic}。

    落库失败时返回 None——调用方据此放弃该分组的下发（没有任务号就没有魔术号，
    无法把 MT5 订单关联回主任务，宁可不发也不发一条无法追溯的单）。
    """
    try:
        async with SessionLocal() as s:
            row = GroupSignalTask(
                signal_id=signal_id,
                group_id=group["group_id"],
                group_name=group.get("name"),
                action=getattr(signal, "action", None),
                symbol=getattr(signal, "symbol", None),
                volume=getattr(signal, "volume", None),
                sl=getattr(signal, "stop_loss", None),
                tp=getattr(signal, "take_profit", None),
                comment=getattr(signal, "comment", None),
                source_ip=source_ip,
                raw_payload=raw_payload,
                dispatch_mode=dispatch_mode,
                status="pending",
            )
            s.add(row)
            await s.flush()
            magic = group_rules.task_magic(row.task_id)
            row.magic = magic
            await s.commit()
            return {"task_id": row.task_id, "magic": magic}
    except Exception as e:  # noqa: BLE001
        logger.warning("create_task failed: %s", e)
        return None


async def mark_task_dispatched(
    task_id: int, *, payload: dict, node_ids: list[str], status: str = "dispatching",
) -> None:
    """记录主任务的下发数据与目标节点。"""
    try:
        async with SessionLocal() as s:
            await s.execute(
                update(GroupSignalTask)
                .where(GroupSignalTask.task_id == task_id)
                .values(
                    payload_json=payload,
                    node_ids_json=list(node_ids),
                    node_count=len(node_ids),
                    status=status,
                )
            )
            await s.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("mark_task_dispatched failed: %s", e)


async def mark_task_skipped(task_id: int, reason: Optional[str]) -> None:
    """主任务未产生任何下发（分组无有效节点等）。"""
    try:
        async with SessionLocal() as s:
            await s.execute(
                update(GroupSignalTask)
                .where(GroupSignalTask.task_id == task_id)
                .values(status="skipped", skip_reason=reason, finished_at=datetime.now())
            )
            await s.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("mark_task_skipped failed: %s", e)


async def record_dispatch(
    *,
    task_id: int,
    signal_id: str,
    group_id: str,
    node_id: str,
    magic: Optional[int],
    decided_vol: Optional[float],
    status: str,
    skip_reason: Optional[str] = None,
) -> None:
    """落库一条主任务节点明细，并在终态时收口主任务与信号整体状态。"""
    try:
        async with SessionLocal() as s:
            s.add(
                GroupTaskDispatch(
                    task_id=task_id,
                    signal_id=signal_id,
                    group_id=group_id,
                    node_id=node_id,
                    magic=magic,
                    decided_vol=decided_vol,
                    status=status,
                    skip_reason=skip_reason,
                    dispatched_at=datetime.now(),
                )
            )
            await s.flush()
            await _refresh_task_status(s, task_id)
            await _refresh_signal_status(s, signal_id)
            await s.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("group record_dispatch failed: %s", e)


async def update_dispatch_result(
    *, node_id: str, result: dict, task_id: Optional[int] = None,
    signal_id: Optional[str] = None,
) -> bool:
    """按节点回报更新明细；命中返回 True。

    task_id 优先（回报带 magic 时可精确定位）；否则按 signal_id 回退匹配该节点
    仍在途的明细，兼容尚未上报魔术号的旧版节点客户端。
    """
    status = "done" if result.get("success") else "failed"
    try:
        async with SessionLocal() as s:
            stmt = select(GroupTaskDispatch).where(GroupTaskDispatch.node_id == node_id)
            if task_id is not None:
                stmt = stmt.where(GroupTaskDispatch.task_id == task_id)
            elif signal_id:
                stmt = stmt.where(
                    GroupTaskDispatch.signal_id == signal_id,
                    GroupTaskDispatch.status.in_(("pending", "sent")),
                )
            else:
                return False
            rows = (await s.execute(stmt.order_by(GroupTaskDispatch.id.asc()))).scalars().all()
            if not rows:
                return False
            for row in rows:
                row.status = status
                row.retcode = result.get("retcode")
                row.order_ticket = result.get("order") or result.get("ticket")
                row.deal = result.get("deal")
                row.price = result.get("price")
                row.error = result.get("error")
                row.finished_at = datetime.now()
            await s.flush()
            for tid in {row.task_id for row in rows}:
                await _refresh_task_status(s, tid)
            for sid in {row.signal_id for row in rows}:
                await _refresh_signal_status(s, sid)
            await s.commit()
            return True
    except Exception as e:  # noqa: BLE001
        logger.warning("group update_dispatch_result failed: %s", e)
        return False


async def _refresh_task_status(session, task_id: int) -> None:
    """按当前节点明细收口主任务状态。"""
    statuses = (
        await session.execute(
            select(GroupTaskDispatch.status).where(GroupTaskDispatch.task_id == task_id)
        )
    ).scalars().all()
    new_status = group_rules.aggregate_task_status(list(statuses))
    values: dict = {"status": new_status}
    if new_status != "dispatching":
        values["finished_at"] = datetime.now()
    await session.execute(
        update(GroupSignalTask).where(GroupSignalTask.task_id == task_id).values(**values)
    )


async def _refresh_signal_status(session, signal_id: str) -> None:
    """按该信号下所有分组主任务的状态，收口 signal_history 的整体状态。"""
    statuses = (
        await session.execute(
            select(GroupSignalTask.status).where(GroupSignalTask.signal_id == signal_id)
        )
    ).scalars().all()
    if not statuses:
        return
    if any(st in ("pending", "dispatching") for st in statuses):
        return
    # partial 本身就是“部分成功”，信号整体也应是 partial，不能被计为全成
    has_partial = any(st == "partial" for st in statuses)
    has_done = any(st == "done" for st in statuses)
    has_failed = any(st == "failed" for st in statuses)
    if has_partial or (has_done and has_failed):
        new_status = "partial"
    elif has_done:
        new_status = "done"
    elif has_failed:
        new_status = "failed"
    else:
        new_status = "done"  # 全部 skipped：处理已结束，只是无人成交
    await session.execute(
        update(SignalHistory).where(SignalHistory.signal_id == signal_id).values(status=new_status)
    )


def _dispatch_row(d: GroupTaskDispatch, node_name: Optional[str]) -> dict:
    return {
        "id": d.id,
        "node_id": d.node_id,
        "node_name": node_name,
        "decided_vol": d.decided_vol,
        "status": d.status,
        "skip_reason": d.skip_reason,
        "retcode": d.retcode,
        "order": d.order_ticket,
        "deal": d.deal,
        "price": d.price,
        "error": d.error,
        "magic": d.magic,
        "dispatched_at": d.dispatched_at.timestamp() if d.dispatched_at else None,
        "finished_at": d.finished_at.timestamp() if d.finished_at else None,
    }


def _task_row(t: GroupSignalTask, dispatches: list[dict]) -> dict:
    return {
        "task_id": t.task_id,
        "magic": t.magic,
        "signal_id": t.signal_id,
        "group_id": t.group_id,
        "group_name": t.group_name,
        "created_at": t.created_at.timestamp() if t.created_at else None,
        "action": t.action,
        "symbol": t.symbol,
        "volume": t.volume,
        "sl": t.sl,
        "tp": t.tp,
        "comment": t.comment,
        "source_ip": t.source_ip,
        "raw_payload": t.raw_payload,
        "dispatch_mode": t.dispatch_mode,
        "payload": t.payload_json,
        "node_ids": t.node_ids_json or [],
        "node_count": t.node_count,
        "status": t.status,
        "skip_reason": t.skip_reason,
        "finished_at": t.finished_at.timestamp() if t.finished_at else None,
        "dispatches": dispatches,
    }


async def recent_group_signals(
    group_id: str, page: int = 1, page_size: int = 20,
    node_names: Optional[dict[str, str]] = None,
) -> dict:
    """分页读取某分组的信号主任务（含各节点处理明细）。"""
    page = max(1, page)
    page_size = max(1, min(page_size, 100))
    offset = (page - 1) * page_size
    names = node_names or {}
    try:
        async with SessionLocal() as s:
            total = (
                await s.execute(
                    select(func.count())
                    .select_from(GroupSignalTask)
                    .where(GroupSignalTask.group_id == group_id)
                )
            ).scalar_one()
            rows = (
                await s.execute(
                    select(GroupSignalTask)
                    .where(GroupSignalTask.group_id == group_id)
                    .order_by(GroupSignalTask.task_id.desc())
                    .offset(offset)
                    .limit(page_size)
                )
            ).scalars().all()

            task_ids = [r.task_id for r in rows]
            by_task: dict[int, list[dict]] = {tid: [] for tid in task_ids}
            if task_ids:
                disp_rows = (
                    await s.execute(
                        select(GroupTaskDispatch)
                        .where(GroupTaskDispatch.task_id.in_(task_ids))
                        .order_by(GroupTaskDispatch.id.asc())
                    )
                ).scalars().all()
                for d in disp_rows:
                    by_task.setdefault(d.task_id, []).append(
                        _dispatch_row(d, names.get(d.node_id))
                    )

            return {
                "items": [_task_row(t, by_task.get(t.task_id, [])) for t in rows],
                "total": total,
                "page": page,
                "page_size": page_size,
            }
    except Exception as e:  # noqa: BLE001
        logger.warning("recent_group_signals failed: %s", e)
        return {"items": [], "total": 0, "page": page, "page_size": page_size}


async def count_by_group() -> dict[str, int]:
    """各分组已处理的信号主任务数（供分组列表的「信号」列展示）。"""
    try:
        async with SessionLocal() as s:
            rows = (
                await s.execute(
                    select(GroupSignalTask.group_id, func.count())
                    .group_by(GroupSignalTask.group_id)
                )
            ).all()
            return {gid: int(cnt) for gid, cnt in rows}
    except Exception as e:  # noqa: BLE001
        logger.warning("count_by_group failed: %s", e)
        return {}
