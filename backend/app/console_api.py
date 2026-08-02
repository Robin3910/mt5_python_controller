"""中控台手动触发信号 API（需管理员鉴权）。

复用 /webhook 的解析与分发流程（webhook.process_signal），以管理员 JWT 鉴权，
避免把 Webhook token / IP 白名单暴露到浏览器；来源标记为 manual，便于事件页区分。
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from . import group_persist, group_rules, persist
from .connections import manager
from .deps import (
    client_ip,
    get_current_admin,
    get_dispatcher,
    get_group_dispatcher,
    get_store,
)
from .dispatcher import Dispatcher
from .group_dispatcher import GroupDispatcher, build_strategy_stop_command
from .models import (
    PURGE_TRADE_LOGS_CONFIRM,
    SIGNAL_MODEL_STRATEGY,
    SIGNAL_MODELS,
    ManualSignalRequest,
    PurgeTradeLogsRequest,
    PurgeTradeLogsResult,
)
from .redis_store import RedisStore
from .webhook import process_signal

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/console", tags=["console"])

MANUAL_ACTIONS = ("BUY", "SELL", "CLOSE")


def _build_signal_payload(body: ManualSignalRequest) -> dict:
    """把手动触发入参组装成 Webhook 同构的信号体。

    CLOSE 只在 strategy 链路开放：normal 链路的 CLOSE 是「广播所有在线节点平掉该
    品种全部持仓」的全局操作，中控台已有专门的远程平仓入口，不从信号入口重复提供；
    strategy 链路的 CLOSE 只终止分组内进行中的策略任务，作用域明确、风险可控。
    """
    action = (body.action or "").strip().upper()
    if action not in MANUAL_ACTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"动作非法：{body.action}（应为 {' / '.join(MANUAL_ACTIONS)}）",
        )

    model = group_rules.normalize_signal_model(body.model)
    if model is None:
        raise HTTPException(
            status_code=400,
            detail=f"处理模型非法：{body.model}（应为 {' / '.join(SIGNAL_MODELS)}）",
        )
    if action == "CLOSE" and model != SIGNAL_MODEL_STRATEGY:
        raise HTTPException(
            status_code=400,
            detail="CLOSE 仅支持 strategy 模型（按分组终止策略任务）；"
                   "按币种平仓请使用中控台的远程平仓功能",
        )

    # 开仓必须显式给手数，避免静默回落到默认手数下出意料之外的单
    if action != "CLOSE" and not body.volume:
        raise HTTPException(status_code=400, detail="开仓信号必须填写手数")

    data: dict = {"action": action, "symbol": body.symbol, "model": model}
    if body.volume:
        data["volume"] = body.volume
    if body.stop_loss:
        data["sl"] = body.stop_loss
    if body.take_profit:
        data["tp"] = body.take_profit
    if comment := (body.comment or "").strip():
        data["comment"] = comment
    return data


@router.post("/manual-signal")
async def manual_signal(
    body: ManualSignalRequest,
    request: Request,
    store: RedisStore = Depends(get_store),
    dispatcher: Dispatcher = Depends(get_dispatcher),
    group_dispatcher: GroupDispatcher = Depends(get_group_dispatcher),
    admin: str = Depends(get_current_admin),
):
    """手动触发一条信号（BUY / SELL / CLOSE），走与 Webhook 完全一致的分发流程。"""
    data = _build_signal_payload(body)
    ip = client_ip(request)
    result = await process_signal(
        data, source_ip=ip, source="manual", store=store, dispatcher=dispatcher,
        group_dispatcher=group_dispatcher,
    )
    await persist.audit(
        admin, "manual_signal", body.symbol, data, result.get("status", "ok"), ip,
        category="console", before=None, after={
            "request": data,
            "result": {
                "status": result.get("status"),
                "signal_id": result.get("signal_id"),
                "model": result.get("model"),
                "mode": result.get("mode"),
                "groups": result.get("groups"),
                "targets": result.get("targets"),
                "reason": result.get("reason"),
            },
        },
    )
    return result


@router.post("/purge-trade-logs", response_model=PurgeTradeLogsResult)
async def purge_trade_logs(
    body: PurgeTradeLogsRequest,
    request: Request,
    store: RedisStore = Depends(get_store),
    admin: str = Depends(get_current_admin),
):
    """清空全部交易日志表与记录表。

    删除：signal_history / signal_dispatch / group_signal_task /
    group_task_dispatch / group_task_event，并清理 Redis 交易运行态。
    分组、策略、节点配置与操作审计保留。
    """
    if (body.confirm or "").strip() != PURGE_TRADE_LOGS_CONFIRM:
        raise HTTPException(
            status_code=400,
            detail=f"确认词不正确，请输入「{PURGE_TRADE_LOGS_CONFIRM}」",
        )
    # 记录一并删除，节点侧监控不会自己停；先下发终止指令，避免节点继续按
    # 已不存在的任务加仓，MT5 持仓也无人收口
    stopped, unreachable = await _stop_all_running_strategies()
    deleted = await persist.purge_trade_logs()
    redis_cleared = await store.clear_trade_runtime()
    total = sum(deleted.values())
    await persist.audit(
        admin, "purge_trade_logs", None, {"confirm": True}, "ok", client_ip(request),
        category="console", before=None, after={
            "deleted": deleted,
            "redis_cleared": redis_cleared,
            "total_deleted": total,
            "strategies_stopped": stopped,
            "strategies_unreachable": unreachable,
        },
    )
    return PurgeTradeLogsResult(
        deleted=deleted, redis_cleared=redis_cleared, total_deleted=total,
        strategies_stopped=stopped, strategies_unreachable=unreachable,
    )


async def _stop_all_running_strategies() -> tuple[int, int]:
    """给所有仍在跑的策略子任务下发终止指令，返回 (已下发, 节点离线)。"""
    stopped = 0
    unreachable = 0
    for sub in await group_persist.all_active_subtasks():
        node_id = sub.get("node_id")
        if not node_id:
            continue
        cmd = build_strategy_stop_command(
            sub.get("signal_id") or "", sub["task_id"], sub["dispatch_id"],
            sub.get("magic"), sub.get("group_id") or "", sub.get("symbol"),
            reason="purge_trade_logs",
        )
        if await manager.send_to_node(node_id, cmd):
            stopped += 1
        else:
            unreachable += 1
    if stopped or unreachable:
        logger.warning(
            "purge_trade_logs: %d strategy stop sent, %d node(s) offline", stopped, unreachable,
        )
    return stopped, unreachable
