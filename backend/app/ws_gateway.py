"""节点 WebSocket 网关：首包鉴权、心跳、账户上报、成交回报。

鉴权采用“连接建立后首包必须是 auth”的方式（而非把 token 放到 URL 上），
避免 token 出现在日志/代理访问记录中。详见文档 6.3。

自 v0.2 起：所有节点共享全局 NODE_TOKEN（见 system_settings），节点身份由 mt5_login
唯一标识；若 mt5_login 不在库中则按默认配置自动注册（见 node_service.auto_register）。
"""
import asyncio
import logging
import time

from fastapi import APIRouter, WebSocket
from starlette.websockets import WebSocketDisconnect

from . import (
    group_dispatcher,
    group_persist,
    group_rules,
    market_probe,
    mt5_identity,
    node_service,
    persist,
    results,
    risk_control,
    rules,
    system_settings,
)
from .connections import manager
from .security import compare_secret
from .settings import settings
from .state import state

logger = logging.getLogger(__name__)
router = APIRouter()


class LoginMismatchError(Exception):
    """会话中检测到终端账号与节点绑定不符，应结束该连接。"""


def _parse_mt5_login(data: dict) -> int | None:
    """从 auth 数据解析 MT5 登录号（正整数）。"""
    return mt5_identity.parse_login(data.get("mt5_login"))


async def _reject_login_mismatch(
    node_id: str, ws: WebSocket, expected, reported,
) -> None:
    """通知节点与管理端后关闭连接，并抛出 LoginMismatchError。"""
    reason = mt5_identity.login_mismatch_reason(expected, reported)
    logger.warning(
        "node %s mt5_login mismatch: bound=%s reported=%s",
        node_id, expected, reported,
    )
    await ws.send_json({
        "type": "auth_fail",
        "data": {
            "reason": "mt5_login_mismatch",
            "message": reason or "MT5 登录号与节点绑定不符",
            "expected": mt5_identity.parse_login(expected),
            "reported": mt5_identity.parse_login(reported),
        },
    })
    await manager.broadcast_admin({
        "type": "node_rejected",
        "data": {
            "node_id": node_id,
            "reason": "mt5_login_mismatch",
            "expected": mt5_identity.parse_login(expected),
            "reported": mt5_identity.parse_login(reported),
        },
    })
    try:
        await ws.close(code=4401)
    except Exception:  # noqa: BLE001
        pass
    raise LoginMismatchError(reason or "mt5_login_mismatch")


async def _enforce_login_match(node_id: str, ws: WebSocket, reported) -> None:
    """若上报 login 与节点绑定冲突则拒绝并断开。"""
    store = state.store
    node = await store.get_node(node_id) if store else None
    if not node:
        return
    expected = node.get("mt5_login")
    if mt5_identity.is_mt5_login_mismatch(expected, reported):
        await _reject_login_mismatch(node_id, ws, expected, reported)


@router.websocket("/ws/node")
async def node_ws(ws: WebSocket):
    await ws.accept()
    store = state.store

    # ---- 首包必须是 auth，并设超时，避免未鉴权的空连接占用资源（6.3）----
    try:
        msg = await asyncio.wait_for(
            ws.receive_json(), timeout=settings.auth_first_packet_timeout
        )
    except Exception:
        await ws.close(code=4400)  # 握手超时 / 格式错误
        return

    if not isinstance(msg, dict) or msg.get("type") != "auth":
        await ws.close(code=4401)
        return

    data = msg.get("data") or {}
    token = data.get("token", "")

    # ---- 1. 校验全局节点令牌（所有节点共享同一 NODE_TOKEN）----
    expected_token, _ = await system_settings.get_node_token(store) if store else ("", 0)
    if not expected_token or not compare_secret(token, expected_token):
        await ws.send_json({"type": "auth_fail", "data": {"reason": "invalid_token"}})
        await ws.close(code=4401)
        return

    # ---- 2. 解析 MT5 登录号 ----
    client_login = _parse_mt5_login(data)
    if client_login is None:
        await ws.send_json({
            "type": "auth_fail",
            "data": {
                "reason": "missing_mt5_login",
                "message": "鉴权包缺少 MT5 账户登录号",
            },
        })
        await ws.close(code=4401)
        return

    # ---- 3. 按 mt5_login 查找节点；不存在则自动注册（默认配置见 node_service）----
    node_id = await store.node_by_mt5_login(client_login) if store else None
    node = await store.get_node(node_id) if node_id else None

    if not node:
        # 兜底走 DB 直查（避免缓存未同步时误判为不存在）
        node = await node_service.find_by_mt5_login(client_login)

    if not node:
        try:
            node = await node_service.auto_register(store, client_login)
        except Exception as e:  # noqa: BLE001
            logger.exception("auto register failed for mt5_login=%s: %s", client_login, e)
            await ws.send_json({
                "type": "auth_fail",
                "data": {"reason": "auto_register_failed", "message": "节点自动注册失败"},
            })
            await ws.close(code=4500)
            return
        await manager.broadcast_admin({
            "type": "node_registered",
            "data": {"node_id": node["node_id"], "mt5_login": client_login, "name": node["name"]},
        })
        logger.info(
            "auto-registered node %s for mt5_login=%s with defaults",
            node["node_id"], client_login,
        )

    node_id = node["node_id"]
    if not node.get("enabled", True):
        await ws.send_json({"type": "auth_fail", "data": {"reason": "disabled", "message": "节点已被禁用，无法接入"}})
        await ws.close(code=4403)  # 节点被禁用
        return

    # 同一节点同一时刻只允许一个在线：已有存活连接时拒绝本次登录并说明原因
    if manager.is_node_online(node_id) and await manager.is_connection_alive(node_id):
        logger.warning("node %s duplicate login rejected (already online) from %s", node_id, ws.client)
        await ws.send_json({
            "type": "auth_fail",
            "data": {
                "reason": "already_online",
                "message": "该节点已有在线连接，同一节点同一时刻只允许一个在线",
            },
        })
        await manager.broadcast_admin(
            {"type": "node_rejected", "data": {"node_id": node_id, "reason": "already_online"}}
        )
        await ws.close(code=4409)  # 4409：重复连接被拒绝
        return

    # 客户端版本随鉴权首包上报（协议见 技术实现方案.md 的 auth.client_version）。
    # 旧版本节点不带该字段，一律按「未知版本」放行，绝不因此拒绝接入。
    await node_service.report_client_version(store, node_id, data.get("client_version") or "")

    # 先登记连接再回 auth_ok，确保节点收到确认时即可被路由（消除竞态）
    await manager.register_node(node_id, ws)
    await store.touch_online(node_id)
    watch_symbols = rules.filter_watch_symbols(await store.get_filters())
    await ws.send_json(
        {
            "type": "auth_ok",
            "data": {
                "node_id": node_id,
                "heartbeat": settings.heartbeat_interval,
                "watch_symbols": watch_symbols,
                # 登录成功即同步账户级风控，节点据此本地监控执行
                "risk": risk_control.normalize_risk(node.get("risk")),
            },
        }
    )
    await manager.broadcast_admin({"type": "node_status", "data": {"node_id": node_id, "status": "online"}})
    await _flush_pending_stops(node_id, ws)
    await _resume_strategy_tasks(node_id, ws)

    try:
        await _session(node_id, ws)
    except (WebSocketDisconnect, LoginMismatchError):
        pass
    except Exception:  # noqa: BLE001
        logger.exception("node session error: %s", node_id)
    finally:
        # 无论何种原因断开，都要清理连接与在线标记，并通知后台
        manager.unregister_node(node_id, ws)
        await store.set_offline(node_id)
        await manager.broadcast_admin(
            {"type": "node_status", "data": {"node_id": node_id, "status": "offline"}}
        )


async def _session(node_id: str, ws: WebSocket) -> None:
    """已鉴权连接的消息主循环：按 type 分派处理。"""
    store = state.store
    while True:
        msg = await ws.receive_json()
        if not isinstance(msg, dict):
            continue
        mtype = msg.get("type")
        data = msg.get("data") or {}

        if mtype in ("heartbeat", "ping"):
            # 心跳：续期在线 TTL；若心跳里捎带了账户快照也一并保存
            await store.touch_online(node_id)
            if data.get("account") or data.get("positions"):
                await _save_account(node_id, ws, data)
            await ws.send_json({"type": "pong", "data": {"ts": time.time()}})

        elif mtype == "account":
            # 账户快照上报
            await store.touch_online(node_id)
            await _save_account(node_id, ws, data)

        elif mtype == "trade_result":
            # 成交回报
            await _on_trade_result(node_id, data)

        elif mtype == "strategy_progress":
            # 策略托管运行期上报（加仓 / 快照）
            await _on_strategy_progress(node_id, data)

        elif mtype == "strategy_finished":
            # 策略托管结束：该魔术号的持仓已全部平掉
            await _on_strategy_finished(node_id, data)

        elif mtype == "hello":
            # 节点上线自报 MT5 登录信息（含登录号一致性校验）
            await _update_node_mt5(node_id, ws, data)

        elif mtype == "risk_event":
            # 账户级风控触发 / 状态回写（如次数耗尽关闭开关）
            await _on_risk_event(node_id, data)

        elif mtype == "market_probe_result":
            # 只读行情探针回包（趋势面板），按 req_id 唤醒等待中的请求
            market_probe.resolve(data.get("req_id"), data)

        elif mtype == "cancel_pending_result":
            from . import limit_watch
            limit_watch.resolve_cancel(data.get("req_id"), data)

        else:
            logger.debug("node %s unknown msg type=%s", node_id, mtype)


async def _save_account(node_id: str, ws: WebSocket, data: dict) -> None:
    """把节点上报的账户/持仓/报价整理成标准快照，存 Redis 并推送后台。

    若上报 login 与节点绑定不符，拒绝入库并断开连接。
    """
    reported = mt5_identity.extract_reported_login(data)
    await _enforce_login_match(node_id, ws, reported)

    store = state.store
    acct = dict(data.get("account") or {})
    snapshot = {
        "node_id": node_id,
        "login": acct.get("login") or data.get("login"),
        "server": acct.get("server") or data.get("server"),
        "balance": acct.get("balance", 0),
        "equity": acct.get("equity", 0),
        "margin": acct.get("margin", 0),
        "free_margin": acct.get("free_margin", acct.get("margin_free", 0)),
        "leverage": acct.get("leverage", 0),
        "positions": data.get("positions", []),
        # 未成交挂单：限价开仓的任务在成交前只有它，对账必须看得见
        "orders": data.get("orders", []),
        "prices": data.get("prices", {}),  # 供区间过滤取价
        "quotes": data.get("quotes", {}),
        "updated_at": time.time(),
        # 券商 MT5 服务器相对 UTC 的偏移（秒）；用于把后台 UTC 时间显示成终端订单时间
        "server_time_offset": data.get("server_time_offset"),
    }
    await store.save_account(node_id, snapshot)
    await _reconcile_strategy_tasks(
        node_id, snapshot.get("positions") or [], snapshot.get("orders") or [],
    )
    await manager.broadcast_admin({"type": "account", "data": snapshot})
    asyncio.create_task(_run_limit_watch(node_id, snapshot.get("orders") or []))


async def _run_limit_watch(node_id: str, orders: list) -> None:
    """限价挂单监听不得挡住账户快照入库。"""
    try:
        from . import limit_watch
        await limit_watch.handle_account_orders(node_id, orders)
    except Exception:  # noqa: BLE001
        logger.exception("limit_watch failed for node %s", node_id)


def _magics_of(rows: list) -> set[int]:
    out: set[int] = set()
    for row in rows:
        try:
            out.add(int((row or {}).get("magic") or 0))
        except (TypeError, ValueError):
            continue
    return out


async def _reconcile_strategy_tasks(
    node_id: str, positions: list, orders: list | None = None,
) -> None:
    """用账户快照兜底收口：快照里已经没有的魔术号，对应子任务判为已平仓。

    判据是「持仓 ∪ 挂单」：只看持仓的话，限价开仓的任务在挂单成交前会被误判成
    已平仓而提前收口、释放节点占位，那张挂单就成了没人监控的孤儿单。
    """
    magics = _magics_of(positions) | _magics_of(orders or [])
    for done in await group_persist.reconcile_node_positions(node_id, magics):
        logger.info(
            "task %s reconciled from account snapshot of %s", done.get("task_id"), node_id,
        )
        await _release_node_busy(done)


async def _on_trade_result(node_id: str, data: dict) -> None:
    """处理成交回报：唤醒轮询等待者、释放执行锁、落库、推送后台。

    strategy 分组链路的回报只更新 group_task_dispatch；命中后不再走 normal 链路的
    signal_dispatch，两张明细表互不写入。子任务号优先由回报里的 magic 反查（最精确），
    其次按 task_id / signal_id 回退，兼容尚未上报魔术号的旧版节点客户端。
    """
    store = state.store
    signal_id = data.get("signal_id", "")
    symbol = data.get("symbol", "")
    # 轮询模式正等待该回报，唤醒对应 future
    results.resolve(signal_id, node_id, data)
    dispatch_id = group_rules.subtask_id_from_magic(data.get("magic"))
    # normal 链路的执行锁只对 normal 回报有意义；strategy 回报不能去动它，
    # 否则会把同节点同品种正在途的 normal 订单的保护提前解除
    if symbol and dispatch_id is None:
        await store.release_exec_lock(node_id, symbol)

    handled = await group_persist.update_dispatch_result(
        node_id=node_id, result=data, dispatch_id=dispatch_id,
        task_id=data.get("task_id"), signal_id=signal_id,
    )
    if handled is None:
        status = "done" if data.get("success") else "failed"
        await persist.update_dispatch_result(signal_id, node_id, status, data)
    else:
        # 首单失败会让子任务直接收口，此时要放开该节点该品种的占位
        await _release_node_busy(handled)
    await manager.broadcast_admin(
        {"type": "trade_result", "data": {"node_id": node_id, **data}}
    )


async def _on_risk_event(node_id: str, data: dict) -> None:
    """处理账户级风控事件：可选回写配置（次数耗尽关闭），并广播管理端。"""
    store = state.store
    risk_payload = data.get("risk")
    if isinstance(risk_payload, dict):
        try:
            updated = await node_service.apply_risk_state(store, node_id, risk_payload)
            if updated is not None:
                data = {**data, "risk": updated.get("risk")}
        except ValueError as e:
            logger.warning("risk state apply rejected for %s: %s", node_id, e)
    await manager.broadcast_admin(
        {"type": "risk_event", "data": {"node_id": node_id, **data}}
    )


async def _flush_pending_stops(node_id: str, ws: WebSocket) -> None:
    """补发节点离线期间攒下的策略终止指令。

    CLOSE 信号到达时若目标节点不在线，服务端会强制收口并放开占位，但 MT5 里的
    持仓仍在。指令暂存在 Redis，节点一回来就补发，由节点按魔术号平掉残留持仓。
    """
    try:
        commands = await state.store.pop_pending_stops(node_id)
    except Exception:  # noqa: BLE001
        logger.exception("load pending stops failed for %s", node_id)
        return
    for cmd in commands:
        try:
            await ws.send_json(cmd)
            logger.info(
                "node %s pending strategy_stop resent (task=%s magic=%s)",
                node_id, cmd.get("task_id"), cmd.get("magic"),
            )
        except Exception:  # noqa: BLE001
            logger.warning("resend pending stop to %s failed", node_id)
            return


async def _resume_strategy_tasks(node_id: str, ws: WebSocket) -> None:
    """节点重连后重建仍未收口的策略监控（不重复下首单）。

    MT5 持仓在节点掉线期间依然存在，若不恢复监控就没人负责加仓与平仓判定，
    该节点该品种也会因为子任务一直不收口而被互斥占位卡住。

    已进入终止流程（closing / stop_requested_at）的子任务改发 strategy_stop，
    避免把「决定要停」的任务又恢复成继续跑。
    """
    try:
        subtasks = await group_persist.resumable_tasks(node_id)
    except Exception:  # noqa: BLE001
        logger.exception("load resumable tasks failed for %s", node_id)
        return
    for subtask in subtasks:
        try:
            status = str(subtask.get("status") or "")
            stop_requested = subtask.get("stop_requested_at") is not None
            if status == "closing" or stop_requested:
                cmd = group_dispatcher.build_strategy_stop_command(
                    subtask.get("signal_id") or f"resume_stop_{subtask.get('dispatch_id')}",
                    subtask.get("task_id"),
                    subtask.get("dispatch_id"),
                    subtask.get("magic"),
                    subtask.get("group_id") or "",
                    subtask.get("symbol"),
                    reason="resume_pending_stop",
                )
                await ws.send_json(cmd)
                logger.info(
                    "node %s pending stop resent on resume (dispatch=%s magic=%s)",
                    node_id, subtask.get("dispatch_id"), subtask.get("magic"),
                )
                continue
            await ws.send_json(group_dispatcher.build_strategy_resume_command(subtask))
            logger.info(
                "node %s resume strategy subtask %s (magic=%s)",
                node_id, subtask.get("dispatch_id"), subtask.get("magic"),
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "send resume for subtask %s to %s failed", subtask.get("dispatch_id"), node_id,
            )
            return


def _as_id(value: object) -> int | None:
    """把回报里的 id 字段安全转成正整数；非法值一律当作缺失。"""
    try:
        num = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return num or None


def _locator(data: dict) -> tuple[int | None, int | None]:
    """从回报里解出定位子任务用的 (子任务号, 主任务号)。

    字段来自节点上报，可能是任意类型；解析失败只当作缺失，不能让整条会话中断。
    """
    dispatch_id = data.get("dispatch_id") or group_rules.subtask_id_from_magic(
        data.get("magic")
    )
    return _as_id(dispatch_id), _as_id(data.get("task_id"))


async def _on_strategy_progress(node_id: str, data: dict) -> None:
    """策略运行期上报：刷新子任务快照，状态变更事件入事件流。"""
    dispatch_id, task_id = _locator(data)
    if dispatch_id is None and task_id is None:
        logger.debug("strategy_progress without locator from %s", node_id)
        return
    await group_persist.record_strategy_progress(
        node_id=node_id, data=data, dispatch_id=dispatch_id, task_id=task_id,
    )
    await manager.broadcast_admin(
        {"type": "strategy_progress", "data": {"node_id": node_id, **data}}
    )


async def _on_strategy_finished(node_id: str, data: dict) -> None:
    """策略结束上报：子任务收口并释放该节点该品种的互斥占位。"""
    dispatch_id, task_id = _locator(data)
    if dispatch_id is None and task_id is None:
        logger.debug("strategy_finished without locator from %s", node_id)
        return
    finished = await group_persist.finish_subtask(
        node_id=node_id, data=data, dispatch_id=dispatch_id, task_id=task_id,
    )
    await _release_node_busy(finished)
    await manager.broadcast_admin(
        {"type": "strategy_finished", "data": {"node_id": node_id, **data}}
    )


async def _release_node_busy(result: dict | None) -> None:
    """释放持久化层回传的组内节点占位，让该节点能接收本分组的下一条策略信号。"""
    store = state.store
    if not store or not result:
        return
    for group_id, node_id in result.get("released", []):
        await store.release_group_node_busy(group_id, node_id)
        logger.info("group %s node %s released", group_id, node_id)


async def _update_node_mt5(node_id: str, ws: WebSocket, data: dict) -> None:
    """处理 hello：校验登录号，并回填 MT5 服务器（登录号以节点绑定为准）。"""
    reported = mt5_identity.extract_reported_login(data)
    await _enforce_login_match(node_id, ws, reported)

    store = state.store
    node = await store.get_node(node_id)
    if not node:
        return
    server = data.get("server")
    if server:
        node["mt5_server"] = server
        await store.cache_node(node)
