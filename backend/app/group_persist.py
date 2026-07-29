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
from .orm import GroupSignalTask, GroupTaskDispatch, GroupTaskEvent, SignalHistory

logger = logging.getLogger(__name__)

# 子任务终态；仍在途的不参与主任务收口
_TERMINAL = frozenset(group_rules.SUBTASK_TERMINAL)


async def create_task(
    *,
    signal_id: str,
    group: dict,
    signal,
    dispatch_mode: str,
    source_ip: Optional[str] = None,
    raw_payload: Optional[str] = None,
    strategy: Optional[dict] = None,
    strategy_snapshot: Optional[dict] = None,
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
                strategy_id=(strategy or {}).get("strategy_id"),
                strategy_name=(strategy or {}).get("name"),
                strategy_snapshot_json=strategy_snapshot,
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


async def active_task(group_id: str) -> Optional[dict]:
    """分组当前进行中的主任务（互斥判定的权威来源）；没有则返回 None。"""
    try:
        async with SessionLocal() as s:
            row = (
                await s.execute(
                    select(GroupSignalTask)
                    .where(
                        GroupSignalTask.group_id == group_id,
                        GroupSignalTask.status.in_(group_rules.TASK_ACTIVE),
                    )
                    .order_by(GroupSignalTask.task_id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if not row:
                return None
            return {
                "task_id": row.task_id,
                "magic": row.magic,
                "group_id": row.group_id,
                "signal_id": row.signal_id,
                "status": row.status,
                "symbol": row.symbol,
                "action": row.action,
                "volume": row.volume,
                "dispatch_mode": row.dispatch_mode,
                "strategy_snapshot": row.strategy_snapshot_json,
            }
    except Exception as e:  # noqa: BLE001
        logger.warning("active_task failed: %s", e)
        return None


async def active_group_ids() -> dict[str, int]:
    """所有存在进行中任务的分组 -> 任务号（服务端重启后重建互斥占位）。"""
    try:
        async with SessionLocal() as s:
            rows = (
                await s.execute(
                    select(GroupSignalTask.group_id, GroupSignalTask.task_id)
                    .where(GroupSignalTask.status.in_(group_rules.TASK_ACTIVE))
                    .order_by(GroupSignalTask.task_id.asc())
                )
            ).all()
            return {gid: tid for gid, tid in rows}
    except Exception as e:  # noqa: BLE001
        logger.warning("active_group_ids failed: %s", e)
        return {}


async def task_status(task_id: int) -> Optional[str]:
    """主任务当前状态。"""
    try:
        async with SessionLocal() as s:
            return (
                await s.execute(
                    select(GroupSignalTask.status).where(
                        GroupSignalTask.task_id == task_id
                    )
                )
            ).scalar_one_or_none()
    except Exception as e:  # noqa: BLE001
        logger.warning("task_status failed: %s", e)
        return None


async def task_group_id(task_id: int) -> Optional[str]:
    """主任务所属分组（释放互斥占位时用）。"""
    try:
        async with SessionLocal() as s:
            return (
                await s.execute(
                    select(GroupSignalTask.group_id).where(
                        GroupSignalTask.task_id == task_id
                    )
                )
            ).scalar_one_or_none()
    except Exception as e:  # noqa: BLE001
        logger.warning("task_group_id failed: %s", e)
        return None


async def running_node_ids(task_id: int) -> list[str]:
    """某主任务下仍未收口的子任务节点列表。"""
    try:
        async with SessionLocal() as s:
            rows = (
                await s.execute(
                    select(GroupTaskDispatch.node_id).where(
                        GroupTaskDispatch.task_id == task_id,
                        GroupTaskDispatch.status.notin_(tuple(_TERMINAL)),
                    )
                )
            ).scalars().all()
            return sorted(set(rows))
    except Exception as e:  # noqa: BLE001
        logger.warning("running_node_ids failed: %s", e)
        return []


async def resumable_tasks(node_id: str) -> list[dict]:
    """某节点上仍在运行的策略子任务（重连后据此重建监控）。"""
    try:
        async with SessionLocal() as s:
            rows = (
                await s.execute(
                    select(GroupTaskDispatch, GroupSignalTask)
                    .join(
                        GroupSignalTask,
                        GroupSignalTask.task_id == GroupTaskDispatch.task_id,
                    )
                    .where(
                        GroupTaskDispatch.node_id == node_id,
                        GroupTaskDispatch.status.notin_(tuple(_TERMINAL)),
                        GroupSignalTask.status.in_(group_rules.TASK_ACTIVE),
                    )
                    .order_by(GroupTaskDispatch.task_id.asc())
                )
            ).all()
            return [
                {
                    "task_id": t.task_id,
                    "magic": t.magic,
                    "group_id": t.group_id,
                    "signal_id": t.signal_id,
                    "action": t.action,
                    "symbol": t.symbol,
                    "volume": d.decided_vol if d.decided_vol is not None else t.volume,
                    "strategy_snapshot": t.strategy_snapshot_json,
                }
                for d, t in rows
            ]
    except Exception as e:  # noqa: BLE001
        logger.warning("resumable_tasks failed: %s", e)
        return []


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

    注意：下单成功不等于任务完成。策略托管下首单成交只是进入 opened，
    真正的完成要等 magic 持仓全平后由 finish_subtask 收口；只有下单失败才直接终态。
    """
    success = bool(result.get("success"))
    now = datetime.now()
    try:
        async with SessionLocal() as s:
            stmt = select(GroupTaskDispatch).where(GroupTaskDispatch.node_id == node_id)
            if task_id is not None:
                stmt = stmt.where(GroupTaskDispatch.task_id == task_id)
            elif signal_id:
                stmt = stmt.where(
                    GroupTaskDispatch.signal_id == signal_id,
                    GroupTaskDispatch.status.in_(group_rules.SUBTASK_PENDING),
                )
            else:
                return False
            rows = (await s.execute(stmt.order_by(GroupTaskDispatch.id.asc()))).scalars().all()
            if not rows:
                return False
            for row in rows:
                if row.status in _TERMINAL:
                    continue
                row.retcode = result.get("retcode")
                row.order_ticket = result.get("order") or result.get("ticket")
                row.deal = result.get("deal")
                row.price = result.get("price")
                row.error = result.get("error")
                if success:
                    row.status = "opened"
                    row.opened_at = row.opened_at or now
                    row.total_orders = max(row.total_orders, 1)
                    row.position_count = max(row.position_count, 1)
                    row.last_report_at = now
                else:
                    row.status = "failed"
                    row.finish_reason = "open_failed"
                    row.finished_at = now
            await s.flush()
            for tid in {row.task_id for row in rows}:
                await _refresh_task_status(s, tid)
                await _refresh_task_totals(s, tid)
            for sid in {row.signal_id for row in rows}:
                await _refresh_signal_status(s, sid)
            await s.commit()
            return True
    except Exception as e:  # noqa: BLE001
        logger.warning("group update_dispatch_result failed: %s", e)
        return False


async def mark_subtasks_closing(task_id: int, node_ids: list[str]) -> None:
    """收到终止指令后把在途子任务标为 closing（等节点回报平仓完成）。"""
    if not node_ids:
        return
    try:
        async with SessionLocal() as s:
            await s.execute(
                update(GroupTaskDispatch)
                .where(
                    GroupTaskDispatch.task_id == task_id,
                    GroupTaskDispatch.node_id.in_(node_ids),
                    GroupTaskDispatch.status.notin_(tuple(_TERMINAL)),
                )
                .values(status="closing")
            )
            await s.flush()
            await _refresh_task_status(s, task_id)
            await s.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("mark_subtasks_closing failed: %s", e)


async def record_strategy_progress(
    *, node_id: str, task_id: int, data: dict,
) -> bool:
    """节点策略执行上报：更新子任务实时快照，状态变更事件另外落事件流。

    纯行情心跳（event 为空或 heartbeat）只刷新快照字段，不写事件表，
    避免长周期任务把 group_task_event 写爆。
    """
    event_type = str(data.get("event") or "").strip()
    try:
        async with SessionLocal() as s:
            row = (
                await s.execute(
                    select(GroupTaskDispatch).where(
                        GroupTaskDispatch.task_id == task_id,
                        GroupTaskDispatch.node_id == node_id,
                    )
                )
            ).scalar_one_or_none()
            if not row:
                return False

            now = datetime.now()
            row.last_report_at = now
            phase = str(data.get("phase") or "").strip()
            if phase in group_rules.SUBTASK_RUNNING and row.status not in _TERMINAL:
                row.status = phase
            if _num(data.get("position_count")) is not None:
                row.position_count = int(data["position_count"])
            if _num(data.get("add_count")) is not None:
                row.add_count = int(data["add_count"])
            if _num(data.get("total_orders")) is not None:
                row.total_orders = int(data["total_orders"])
            if _num(data.get("total_volume")) is not None:
                row.total_volume = float(data["total_volume"])
            if _num(data.get("profit")) is not None:
                row.realized_profit = float(data["profit"])
            if event_type == "open" and row.opened_at is None:
                row.opened_at = now
                order = data.get("last_order") or {}
                row.order_ticket = order.get("ticket") or row.order_ticket
                row.price = order.get("price") if order.get("price") is not None else row.price

            if event_type and event_type != "heartbeat":
                order = data.get("last_order") or {}
                s.add(
                    GroupTaskEvent(
                        task_id=task_id,
                        node_id=node_id,
                        magic=row.magic,
                        event_type=event_type[:16],
                        symbol=data.get("symbol"),
                        action=data.get("action"),
                        volume=_num(order.get("volume")) or _num(data.get("volume")),
                        price=_num(order.get("price")),
                        order_ticket=order.get("ticket"),
                        position_count=_int_or_none(data.get("position_count")),
                        total_volume=_num(data.get("total_volume")),
                        profit=_num(data.get("profit")),
                        message=(data.get("message") or None),
                    )
                )

            await s.flush()
            await _refresh_task_status(s, task_id)
            await _refresh_task_totals(s, task_id)
            await s.commit()
            return True
    except Exception as e:  # noqa: BLE001
        logger.warning("record_strategy_progress failed: %s", e)
        return False


async def finish_subtask(
    *, node_id: str, task_id: int, data: dict,
) -> Optional[str]:
    """节点上报策略结束（magic 持仓已全平）。返回收口后的主任务状态。"""
    status = str(data.get("status") or "done").strip().lower()
    if status not in _TERMINAL:
        status = "done"
    try:
        async with SessionLocal() as s:
            row = (
                await s.execute(
                    select(GroupTaskDispatch).where(
                        GroupTaskDispatch.task_id == task_id,
                        GroupTaskDispatch.node_id == node_id,
                    )
                )
            ).scalar_one_or_none()
            if not row:
                return None
            now = datetime.now()
            row.status = status
            row.finish_reason = (data.get("reason") or "positions_cleared")[:64]
            row.finished_at = now
            row.last_report_at = now
            row.position_count = 0
            if _num(data.get("total_orders")) is not None:
                row.total_orders = int(data["total_orders"])
            if _num(data.get("total_volume")) is not None:
                row.total_volume = float(data["total_volume"])
            if _num(data.get("realized_profit")) is not None:
                row.realized_profit = float(data["realized_profit"])
            if data.get("error"):
                row.error = str(data["error"])[:255]

            s.add(
                GroupTaskEvent(
                    task_id=task_id,
                    node_id=node_id,
                    magic=row.magic,
                    event_type="close_all",
                    symbol=data.get("symbol"),
                    position_count=0,
                    total_volume=row.total_volume,
                    profit=row.realized_profit,
                    message=row.finish_reason,
                )
            )

            await s.flush()
            await _refresh_task_status(s, task_id)
            await _refresh_task_totals(s, task_id)
            task_status = (
                await s.execute(
                    select(GroupSignalTask.status).where(GroupSignalTask.task_id == task_id)
                )
            ).scalar_one_or_none()
            await _refresh_signal_status(s, row.signal_id)
            await s.commit()
            return task_status
    except Exception as e:  # noqa: BLE001
        logger.warning("finish_subtask failed: %s", e)
        return None


async def reconcile_node_positions(
    node_id: str, magics: set[int],
) -> list[tuple[int, str]]:
    """账户快照对账：节点上已无持仓的运行中子任务，判定为已平仓并收口。

    这是 strategy_finished 的兜底——上报丢包或节点在监控启动前就平了仓时，
    单靠节点主动上报会让子任务永远停在 running，进而把分组锁死。
    只处理已经开过仓（opened_at 非空）的子任务，避免把刚下发还没成交的误判为完成。
    返回 [(task_id, 主任务状态)]，供调用方决定是否释放分组占位。
    """
    finished: list[tuple[int, str]] = []
    try:
        async with SessionLocal() as s:
            rows = (
                await s.execute(
                    select(GroupTaskDispatch).where(
                        GroupTaskDispatch.node_id == node_id,
                        GroupTaskDispatch.status.in_(group_rules.SUBTASK_RUNNING),
                        GroupTaskDispatch.opened_at.is_not(None),
                    )
                )
            ).scalars().all()
            if not rows:
                return []
            now = datetime.now()
            touched: set[int] = set()
            for row in rows:
                if row.magic is None or int(row.magic) in magics:
                    continue
                row.status = "done"
                row.finish_reason = "reconciled_no_position"
                row.position_count = 0
                row.finished_at = now
                row.last_report_at = now
                s.add(
                    GroupTaskEvent(
                        task_id=row.task_id,
                        node_id=node_id,
                        magic=row.magic,
                        event_type="close_all",
                        position_count=0,
                        message="账户快照中已无该魔术号持仓，自动收口",
                    )
                )
                touched.add(row.task_id)
            if not touched:
                return []
            await s.flush()
            for tid in touched:
                await _refresh_task_status(s, tid)
                await _refresh_task_totals(s, tid)
            task_rows = (
                await s.execute(
                    select(GroupSignalTask.task_id, GroupSignalTask.status,
                           GroupSignalTask.signal_id)
                    .where(GroupSignalTask.task_id.in_(touched))
                )
            ).all()
            for _tid, _status, sid in task_rows:
                await _refresh_signal_status(s, sid)
            await s.commit()
            finished = [(tid, st) for tid, st, _sid in task_rows]
    except Exception as e:  # noqa: BLE001
        logger.warning("reconcile_node_positions failed: %s", e)
        return []
    return finished


async def force_finish_task(task_id: int, *, reason: str) -> Optional[str]:
    """强制收口主任务：把所有在途子任务标记为终态（节点全离线 / 人工终止）。"""
    try:
        async with SessionLocal() as s:
            now = datetime.now()
            await s.execute(
                update(GroupTaskDispatch)
                .where(
                    GroupTaskDispatch.task_id == task_id,
                    GroupTaskDispatch.status.notin_(tuple(_TERMINAL)),
                )
                .values(
                    status="failed", finish_reason=reason[:64],
                    finished_at=now, position_count=0,
                )
            )
            await s.flush()
            await _refresh_task_status(s, task_id)
            await _refresh_task_totals(s, task_id)
            row = (
                await s.execute(
                    select(GroupSignalTask).where(GroupSignalTask.task_id == task_id)
                )
            ).scalar_one_or_none()
            if row:
                row.skip_reason = reason[:255]
                await _refresh_signal_status(s, row.signal_id)
            await s.commit()
            return row.status if row else None
    except Exception as e:  # noqa: BLE001
        logger.warning("force_finish_task failed: %s", e)
        return None


def _num(value: object) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _int_or_none(value: object) -> Optional[int]:
    n = _num(value)
    return int(n) if n is not None else None


async def _refresh_task_totals(session, task_id: int) -> None:
    """把各子任务的下单数 / 手数 / 盈亏汇总到主任务。"""
    row = (
        await session.execute(
            select(
                func.coalesce(func.sum(GroupTaskDispatch.total_orders), 0),
                func.coalesce(func.sum(GroupTaskDispatch.total_volume), 0.0),
                func.coalesce(func.sum(GroupTaskDispatch.realized_profit), 0.0),
                func.min(GroupTaskDispatch.opened_at),
            ).where(GroupTaskDispatch.task_id == task_id)
        )
    ).first()
    if not row:
        return
    orders, volume, profit, opened_at = row
    values: dict = {
        "total_orders": int(orders or 0),
        "total_volume": float(volume or 0.0),
        "realized_profit": float(profit or 0.0),
    }
    if opened_at is not None:
        values["opened_at"] = opened_at
    await session.execute(
        update(GroupSignalTask).where(GroupSignalTask.task_id == task_id).values(**values)
    )


async def _refresh_task_status(session, task_id: int) -> None:
    """按当前子任务状态收口主任务状态。"""
    statuses = (
        await session.execute(
            select(GroupTaskDispatch.status).where(GroupTaskDispatch.task_id == task_id)
        )
    ).scalars().all()
    new_status = group_rules.aggregate_task_status(list(statuses))
    values: dict = {"status": new_status}
    if new_status not in group_rules.TASK_ACTIVE:
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
    if any(st in group_rules.TASK_ACTIVE for st in statuses):
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
        "position_count": d.position_count,
        "add_count": d.add_count,
        "total_orders": d.total_orders,
        "total_volume": d.total_volume,
        "realized_profit": d.realized_profit,
        "finish_reason": d.finish_reason,
        "dispatched_at": d.dispatched_at.timestamp() if d.dispatched_at else None,
        "opened_at": d.opened_at.timestamp() if d.opened_at else None,
        "last_report_at": d.last_report_at.timestamp() if d.last_report_at else None,
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
        "strategy_id": t.strategy_id,
        "strategy_name": t.strategy_name,
        "dispatch_mode": t.dispatch_mode,
        "payload": t.payload_json,
        "node_ids": t.node_ids_json or [],
        "node_count": t.node_count,
        "status": t.status,
        "skip_reason": t.skip_reason,
        "total_orders": t.total_orders,
        "total_volume": t.total_volume,
        "realized_profit": t.realized_profit,
        "opened_at": t.opened_at.timestamp() if t.opened_at else None,
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
