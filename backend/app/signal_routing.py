"""信号中心的**路由中枢**：把待路由的信号下发给管理员指定的分组。

与自动分发（`group_dispatcher.dispatch` 的 direct 路径）的分工：

- **自动分发**：TV 告警 / 手动触发 → 按规则挑分组 → 立刻开仓。
- **路由中枢**：管理员在信号中心里选中信号 + 选中分组 → 复用同一条信号的参数
  → 再下发一次。

**路由不新建信号**：复用原信号的参数（symbol / action / sl / tp / volume），
允许覆盖 volume / sl / tp。所以一条信号可能对应**多个** `group_signal_task`
（第一次自动分发的 + 若干次人工路由的）—— 统计时按 task 聚合，不按 signal 去重。

**幂等**：同一 `signal_id` + 同一 `group_id` 已有**未收口**任务 → 拒绝（409），
并把已有任务的 task_id 一起返回，让前端能直接跳过去看。
为什么只查「未收口」：已收口的任务是历史，重跑一次是合法的（同一信号再开一仓）。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy import select

from . import group_rules, parser, signal_meta
from .db import SessionLocal
from .orm import GroupSignalTask, SignalHistory

logger = logging.getLogger(__name__)


class RouteConflict(Exception):
    """同一信号重复路由到同一分组（已有未收口任务）。"""

    def __init__(self, message: str, *, task_ids: Optional[list[int]] = None) -> None:
        super().__init__(message)
        self.task_ids = task_ids or []


class RouteInputError(Exception):
    """路由入参不合法（信号不存在 / 不可路由 / 参数缺失）。"""


async def _load_signal(signal_id: str) -> SignalHistory:
    async with SessionLocal() as s:
        sig = (
            await s.execute(
                select(SignalHistory).where(SignalHistory.signal_id == signal_id)
            )
        ).scalar_one_or_none()
    if sig is None:
        raise RouteInputError(f"信号不存在：{signal_id}")
    return sig


def _rebuild_signal(sig: SignalHistory, signal_id: str) -> parser.TradingSignal:
    """从信号历史行还原出一个可再次下发的 `TradingSignal`。

    优先用原始载荷重新解析 —— 因为**限价开仓的 `entry_price` 没有单独入库**，
    只在原始载荷里。解析失败再退回按列拼装（此时退化为市价单）。

    原始载荷是纯文本告警时解析不出来 —— 那种信号本来也没有结构化参数可用，
    退回按列拼装即可。
    """
    if sig.raw_payload:
        try:
            parsed = parser.parse(sig.raw_payload)
        except Exception:  # noqa: BLE001
            parsed = None
        if parsed is not None:
            return parsed

    action = (sig.action or "BUY").upper()
    if action not in ("BUY", "SELL", "CLOSE"):
        raise RouteInputError(f"信号 {signal_id} 的动作不可路由：{sig.action}")
    if not sig.symbol:
        raise RouteInputError(f"信号 {signal_id} 缺少品种，无法路由")
    return parser.TradingSignal(
        action=action,
        symbol=sig.symbol,
        volume=float(sig.volume or 0.0),
        stop_loss=sig.sl,
        take_profit=sig.tp,
        comment=sig.comment or "",
        timeframe=sig.timeframe,
        signal_type=sig.signal_type,
    )


async def _active_task_ids(signal_id: str, group_ids: list[str]) -> dict[str, int]:
    """查这些分组下该信号**未收口**的主任务：{group_id: task_id}。"""
    if not group_ids:
        return {}
    try:
        async with SessionLocal() as s:
            rows = (
                await s.execute(
                    select(GroupSignalTask.group_id, GroupSignalTask.task_id).where(
                        GroupSignalTask.signal_id == signal_id,
                        GroupSignalTask.group_id.in_(group_ids),
                        GroupSignalTask.status.in_(group_rules.TASK_ACTIVE),
                    )
                )
            ).all()
        return {str(gid): int(tid) for gid, tid in rows}
    except Exception as e:  # noqa: BLE001
        logger.warning("signal_routing._active_task_ids failed: %s", e)
        return {}


async def route_signal(
    signal_id: str,
    *,
    group_ids: list[str],
    dispatcher: Any,
    source_ip: Optional[str] = None,
    volume: Optional[float] = None,
    stop_loss: Optional[float] = None,
    take_profit: Optional[float] = None,
    note: Optional[str] = None,
) -> dict:
    """把一条信号路由到指定的分组。

    返回 {"signal_id":..., "groups":..., "targets":..., "tasks":[...]}；
    重复路由抛 `RouteConflict`，入参问题抛 `RouteInputError`。
    """
    targets = [str(g).strip() for g in (group_ids or []) if str(g or "").strip()]
    if not targets:
        raise RouteInputError("未指定目标分组")
    # 去重但保序
    seen: set[str] = set()
    unique: list[str] = []
    for g in targets:
        if g not in seen:
            seen.add(g)
            unique.append(g)

    sig = await _load_signal(signal_id)
    if (sig.action or "").upper() == "CLOSE":
        # CLOSE 是终止指令，不能「路由到分组开仓」
        raise RouteInputError("平仓信号不能路由到分组开仓")

    conflict = await _active_task_ids(signal_id, unique)
    if conflict:
        names = "、".join(
            f"{gid}（任务 #{tid}）" for gid, tid in sorted(conflict.items())
        )
        raise RouteConflict(
            f"这些分组已有该信号的进行中任务：{names}",
            task_ids=sorted(conflict.values()),
        )

    signal = _rebuild_signal(sig, signal_id)
    # 允许覆盖参数：不同分组手数不同；AI 加仓按风险金额反推手数，sl 是关键输入
    if volume is not None:
        signal.volume = float(volume)
    if stop_loss is not None:
        signal.stop_loss = float(stop_loss)
    if take_profit is not None:
        signal.take_profit = float(take_profit)

    result = await dispatcher.dispatch(
        signal, signal_id,
        source_ip=source_ip,
        raw_payload=sig.raw_payload,
        source=sig.source or "tradingview",
        route_mode=signal_meta.ROUTE_MODE_CENTER,
        group_ids=unique,
    )
    if result.get("mode") == "rejected":
        raise RouteInputError(str(result.get("reason") or "路由失败"))

    await _mark_routed(signal_id, unique, note)
    result["note"] = note
    return result


async def _mark_routed(
    signal_id: str, group_ids: list[str], note: Optional[str] = None,
) -> None:
    """把信号标记成「已中心下发」。best-effort：写失败不影响下发结果。"""
    try:
        async with SessionLocal() as s:
            await s.execute(
                SignalHistory.__table__.update()
                .where(SignalHistory.signal_id == signal_id)
                .values(
                    route_state=signal_meta.ROUTE_ROUTED,
                    route_mode=signal_meta.ROUTE_MODE_CENTER,
                )
            )
            await s.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("signal_routing._mark_routed failed: %s", e)


async def drop_signal(signal_id: str, *, note: Optional[str] = None) -> dict:
    """丢弃一条待路由信号：只改路由状态，不动已建的任务。

    已直连分发过的信号也能丢弃 —— 那只是「不再路由到 center 分组」，
    已经在跑的仓位不受影响（要平仓请走 CLOSE 信号）。
    """
    sig = await _load_signal(signal_id)
    try:
        async with SessionLocal() as s:
            await s.execute(
                SignalHistory.__table__.update()
                .where(SignalHistory.signal_id == signal_id)
                .values(
                    route_state=signal_meta.ROUTE_DROPPED,
                    route_mode=signal_meta.ROUTE_MODE_CENTER,
                )
            )
            await s.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("signal_routing.drop_signal failed: %s", e)
        raise
    return {
        "signal_id": signal_id,
        "route_state": signal_meta.ROUTE_DROPPED,
        "note": note,
        "previous_status": sig.status,
    }


async def route_many(
    signal_ids: list[str],
    *,
    group_ids: list[str],
    dispatcher: Any,
    source_ip: Optional[str] = None,
    volume: Optional[float] = None,
    stop_loss: Optional[float] = None,
    take_profit: Optional[float] = None,
    note: Optional[str] = None,
) -> dict:
    """批量路由：逐条路由，返回逐条结果。

    **不做事务**：一条失败不该拖垮整批 —— 每条的结果都单独汇报，
    前端据此展示「成功 N 条 / 冲突 M 条 / 失败 K 条」。
    """
    ok: list[dict] = []
    conflicts: list[dict] = []
    failed: list[dict] = []
    for sid in signal_ids:
        try:
            res = await route_signal(
                sid, group_ids=group_ids, dispatcher=dispatcher,
                source_ip=source_ip, volume=volume,
                stop_loss=stop_loss, take_profit=take_profit, note=note,
            )
            ok.append({"signal_id": sid, "groups": res.get("groups", 0),
                       "targets": res.get("targets", 0)})
        except RouteConflict as e:
            conflicts.append({"signal_id": sid, "reason": str(e),
                              "task_ids": e.task_ids})
        except RouteInputError as e:
            failed.append({"signal_id": sid, "reason": str(e)})
        except Exception as e:  # noqa: BLE001
            logger.warning("signal_routing.route_many failed for %s: %s", sid, e)
            failed.append({"signal_id": sid, "reason": str(e)})
    return {
        "ok": ok,
        "conflicts": conflicts,
        "failed": failed,
        "summary": {
            "ok": len(ok), "conflicts": len(conflicts), "failed": len(failed),
        },
    }
