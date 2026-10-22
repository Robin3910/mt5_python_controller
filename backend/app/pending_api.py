"""远程「删挂单」API：撤掉某节点上全部未成交挂单，**不动持仓**。

与 `close.py` 的平仓是两件事：
- 平仓（全平）= 停策略 + 平持仓 + 附带撤单；
- 本模块 = 停策略 + 全量撤单，**持仓原样保留**。

编排顺序是本模块的技术核心：**必须先停策略再撤单**。
`strategy_runner` 自行维护 `_pending_orders` 计数，发现挂单少了会立刻补回
（网格还会重新挂格），顺序颠倒就会「撤了又长回来」。

详见 docs/superpowers/plans/2026-10-22-cancel-pending-plan.md
"""
import asyncio
import logging
import secrets
import time

from fastapi import APIRouter, Depends, HTTPException, Request

from . import node_rpc, persist
from .connections import manager
from .deps import (
    client_ip,
    get_group_dispatcher,
    get_store,
    owned_node_or_404,
    require_menu,
)
from .group_dispatcher import GroupDispatcher
from .group_persist import active_subtasks_for_node
from .limit_watch import node_has_watch
from .models import CancelPendingRequest, build_cancel_all_command
from .permissions import MENU_DASHBOARD, MENU_NODES, Principal
from .redis_store import RedisStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["pending"])

# 与平仓的 manual_close_all 区分：前端据此显示正确的收口原因（不是「正常完成」）
_STOP_REASON = "manual_cancel_pending"

# 停完策略等它收口的时长。节点侧 close_by_magic 遇瞬时错误会重试最多 3 次，
# 3s 是「够用即止」；超时后继续撤单，反正下一步是全量扫撤，天然兜底。
SETTLE_WAIT_SEC = 3.0
# 复扫前给外部补单（限价监听）留的时间
RESCAN_WAIT_SEC = 2.0
# 复扫最多两轮：外部补单靠重试解决不了，重试第三次只是多花 4 秒
MAX_RESCAN_ROUNDS = 2
# 节点级互斥锁 TTL（覆盖整个编排时长）
LOCK_TTL_SEC = 30
# 人工撤单 RPC 的超时：老版本节点不认识命令、不会回包，只能靠超时收敛
NODE_TIMEOUT_SEC = 8.0


def _cmd_id() -> str:
    """删挂单命令 ID（用于关联回报与审计）。"""
    return "cpx_" + format(int(time.time() * 1000), "x") + secrets.token_hex(2)


async def _wait_subtasks_settled(node_id: str, timeout: float = SETTLE_WAIT_SEC) -> bool:
    """等该节点上的策略子任务收口；返回是否已清空。

    收口判据同 `group_persist` 的终态集合。这里只做「尽力等」：
    等不到就返回 False 让后续撤单兜底，绝不因为等不到而中断整个流程。
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        subs = await active_subtasks_for_node(node_id)
        if not subs:
            return True
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(0.25)


def _summarize_failed(payload: dict) -> list[dict]:
    """把节点回包里的 failed 明细规整成前端可展示的形状。"""
    out: list[dict] = []
    for item in payload.get("failed") or []:
        if not isinstance(item, dict):
            continue
        out.append({
            "ticket": int(item.get("ticket") or 0),
            "symbol": str(item.get("symbol") or ""),
            "error": str(item.get("error") or "撤单失败"),
        })
    return out


async def _cancel_once(node_id: str, scope: str, magics: list[int] | None) -> dict:
    """向节点下发一轮批量撤单（node_rpc 负责 req_id 配对，失败不抛）。"""
    return await node_rpc.request(
        node_id,
        build_cancel_all_command(scope, magics=magics),
        timeout=NODE_TIMEOUT_SEC,
        offline_error="节点当前离线，无法撤销挂单",
        timeout_error="节点未在超时时间内确认（可能客户端版本过低，不支持批量撤挂单）",
    )


@router.post("/nodes/{node_id}/cancel-pending")
async def cancel_pending_on_node_route(
    node_id: str,
    body: CancelPendingRequest,
    request: Request,
    store: RedisStore = Depends(get_store),
    group_dispatcher: GroupDispatcher = Depends(get_group_dispatcher),
    p: Principal = Depends(require_menu(MENU_NODES, MENU_DASHBOARD)),
):
    """删掉该节点的挂单：先停策略（否则策略会补单），再全量撤单，已有没有持仓不受影响。

    撤完复扫一次：若挂单又长回来（多半是限价监听自动触发），停止重试并在结果里说明。
    """
    await owned_node_or_404(store, p, node_id)
    admin = p.username
    ip = client_ip(request)

    if node_id not in manager.nodes:
        raise HTTPException(status_code=409, detail="node offline")

    if not await store.acquire_cancel_pending_lock(node_id, ttl_s=LOCK_TTL_SEC):
        raise HTTPException(status_code=409, detail="该节点正在执行删挂单操作")

    scope = body.scope if body.scope in ("all", "manual", "strategy") else "all"
    prev = await store.get_account(node_id) or {}
    before_orders = list(prev.get("orders") or [])
    groups = await store.all_groups()
    watch_active = node_has_watch(groups, node_id)

    result: dict = {
        "status": "ok", "node_id": node_id, "scope": scope,
        "cancelled": 0, "failed": [], "remaining": 0, "rounds": 0,
    }
    try:
        # [2] 先停策略 —— 顺序不可颠倒，否则 strategy_runner 会把刚撤掉的单补回来
        signal_id = _cmd_id()
        strategies = await group_dispatcher.stop_all_on_node(
            node_id, signal_id=signal_id, reason=_STOP_REASON,
        )
        settled = await _wait_subtasks_settled(node_id)

        # [3]+[4] 撤单 + 复扫：remaining 由节点侧撤完复读挂单簿给出，是权威判据
        await store.mark_cancel_pending_busy(node_id)
        for _ in range(MAX_RESCAN_ROUNDS):
            payload = await _cancel_once(node_id, scope, body.magics)
            result["rounds"] += 1
            if not payload.get("success") and not payload.get("cancelled"):
                # 离线 / 超时 / 节点侧异常：结果未知，不重发（重发只会产生噪音日志）
                result["status"] = "error"
                result["error"] = str(payload.get("error") or "节点未确认撤单")
                await store.clear_cancel_pending_busy(node_id)
                break

            result["cancelled"] += int(payload.get("cancelled") or 0)
            result["failed"].extend(_summarize_failed(payload))
            for item in payload.get("results") or []:
                if isinstance(item, dict) and item.get("success") and item.get("ticket"):
                    await store.note_manual_cancelled_ticket(node_id, int(item["ticket"]))
            result["remaining"] = int(payload.get("remaining") or 0)
            if result["remaining"] == 0:
                break
            if result["rounds"] < MAX_RESCAN_ROUNDS:
                await asyncio.sleep(RESCAN_WAIT_SEC)
        await store.clear_cancel_pending_busy(node_id)

        if result["failed"]:
            result["status"] = "partial"
        elif result["remaining"] > 0:
            # 撤了又长回来同样数量：外部在补单（多半是限价监听），不再重试
            result["status"] = "regenerated"
            result["regenerated"] = result["remaining"]
        result["strategies"] = strategies
        result["settled"] = settled
        result["limit_watch_active"] = watch_active

        # [5] 审计
        await persist.audit(
            admin, "cancel_pending_node", node_id,
            {"scope": scope}, result["status"], ip,
            category="node",
            before={"orders": before_orders},
            after={
                **result,
                "note": (
                    "撤单后系统又生成了挂单，可能是限价监听自动触发"
                    if result["status"] == "regenerated" else None
                ),
            },
        )
        return result
    finally:
        await store.release_cancel_pending_lock(node_id)
        await store.clear_cancel_pending_busy(node_id)