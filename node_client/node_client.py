"""MT5 节点客户端：WebSocket 连接 / 鉴权 / 心跳 / 账户上报 / 执行命令。

运行：python node_client.py
配置见 .env（参考 .env.example）。请将本程序放在 MT5 安装目录；
若终端已登录则自动复用账号，否则启动时输入账号/密码/服务器。
设置 MT5_MOCK=true 可在无终端时用模拟器联调。

设计要点：
- MetaTrader5 的调用是阻塞式且非线程安全的，统一丢到专用单线程执行器排队，
  既不阻塞事件循环，也保证同一时刻只有一路在操作终端；
- 断线自动重连（指数退避）；
- 三个并发任务：账户上报 / 心跳 / 接收命令，任一结束即重建连接；
- 策略托管任务由 MarketHub 统一采样并派发事件驱动（见 market_hub.py），
  采样协程与连接无关、常驻整个进程生命周期；
- 启动时绑定的 MT5 登录号与终端实时 account_info.login 不一致时主动停交易并断线。
"""
import asyncio
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor

import websockets

import account_risk
from config import get_settings
from market_hub import MarketHub
from mt5_prompt import prompt_mt5_credentials
from strategy_runner import StrategyRunner

settings = get_settings()
logging.basicConfig(
    level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("node")


class LoginMismatchError(RuntimeError):
    """终端当前登录号与启动时绑定账号不符。"""


def make_client(
    mt5_login: int,
    mt5_password: str,
    mt5_server: str,
    mt5_path: str,
    *,
    reuse_terminal_session: bool = False,
):
    """按配置选择真实 MT5 客户端或模拟客户端。"""
    if settings.mt5_mock:
        from mock_mt5 import MockMT5Client

        return MockMT5Client(
            mt5_login, mt5_password, mt5_server,
            mt5_path, settings.default_slippage, settings.default_magic,
        )
    from mt5_client import MT5Client

    return MT5Client(
        mt5_login, mt5_password, mt5_server,
        mt5_path, settings.default_slippage, settings.default_magic,
        reuse_terminal_session=reuse_terminal_session,
    )


class NodeClient:
    def __init__(
        self,
        *,
        mt5_login: int = 90000001,
        mt5_password: str = "",
        mt5_server: str = "MockServer",
        mt5_path: str = "",
        reuse_terminal_session: bool = False,
    ) -> None:
        self.expected_mt5_login = int(mt5_login)
        self.mt5 = make_client(
            mt5_login, mt5_password, mt5_server, mt5_path,
            reuse_terminal_session=reuse_terminal_session,
        )
        self.loop: asyncio.AbstractEventLoop | None = None
        # MT5 的 Python API 非线程安全：所有阻塞调用固定在这一个线程里排队，
        # 否则风控清仓与策略收口会并发对同一批持仓发单，后到的那笔必被拒
        self._mt5_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mt5")
        self._stop = False
        # 中控台全局 filters 品种（auth_ok / watch_symbols 下发），与本地 WATCH_SYMBOLS 合并取价
        self.hub_symbols: set[str] = set()
        # 账户级风控配置（auth_ok / risk_config 下发），节点本地监控执行
        self.risk: dict = account_risk.default_risk()
        # 触发后短暂冷却，避免循环模式下同一波行情连续清仓
        self._risk_cooldown_until: float = 0.0
        # 浮盈亏保护状态机：item_id -> 是否已进入保护
        self._protect_armed: dict[str, bool] = {}
        # 策略托管任务：task_id -> StrategyRunner，断线时取消、重连后由服务端下发恢复
        self.runners: dict[int, StrategyRunner] = {}
        # 策略监控的事件源：全节点共用一个采样器，MT5 调用次数与任务数无关
        self.hub: MarketHub | None = None

    async def run(self) -> None:
        """主入口：先连 MT5，再进入“连接-鉴权-服务”的自动重连循环。"""
        self.loop = asyncio.get_running_loop()
        await self._connect_mt5()
        self._ensure_hub()
        try:
            await self._connect_loop()
        finally:
            if self.hub is not None:
                await self.hub.close()
                self.hub = None
            # 不等在途调用：退出路径上别把事件循环卡在 MT5 的阻塞请求里
            self._mt5_executor.shutdown(wait=False)

    async def _connect_loop(self) -> None:
        backoff = settings.reconnect_min
        while not self._stop:
            try:
                async with websockets.connect(
                    settings.manager_ws_url, ping_interval=20, ping_timeout=20, max_queue=128
                ) as ws:
                    if not await self._authenticate(ws):
                        # 鉴权/登录被拒绝：退避后重试（如重复登录，待对端下线后可接入）
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, settings.reconnect_max)
                        continue
                    backoff = settings.reconnect_min  # 鉴权成功，重置退避
                    await self._serve(ws)
            except LoginMismatchError as e:
                logger.error("%s", e)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, settings.reconnect_max)
            except Exception as e:  # noqa: BLE001
                logger.warning("ws connection error: %s", e)
            if self._stop:
                break
            # 断线后指数退避重连
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, settings.reconnect_max)

    def _ensure_hub(self) -> MarketHub:
        """取（必要时创建）策略监控事件源；采样协程幂等启动。"""
        if self.hub is None:
            self.hub = MarketHub(
                self.mt5, self._exec,
                interval=settings.strategy_sample_interval,
                idle_interval=settings.strategy_idle_interval,
                empty_confirm=settings.strategy_empty_confirm,
            )
        self.hub.start()
        return self.hub

    # ---------------------- MT5 辅助 ----------------------
    async def _exec(self, fn, *args):
        """把阻塞式 MT5 调用放到专用单线程执行：不阻塞事件循环，且彼此串行。"""
        return await self.loop.run_in_executor(self._mt5_executor, lambda: fn(*args))

    async def _connect_mt5(self) -> None:
        try:
            ok = await self._exec(self.mt5.connect)
            if not ok:
                logger.error("MT5 connect failed (will keep reporting empty until available)")
        except Exception as e:  # noqa: BLE001
            logger.error("MT5 connect error: %s", e)

    def _check_login(self, acct: dict) -> None:
        """终端实时登录号须与启动绑定账号一致；缺失则跳过（避免暂空误杀）。"""
        raw = (acct or {}).get("login")
        if raw is None or raw == "":
            return
        try:
            got = int(raw)
        except (TypeError, ValueError):
            return
        if got <= 0:
            return
        if got != self.expected_mt5_login:
            raise LoginMismatchError(
                f"MT5 登录号不符：终端当前={got}，启动绑定={self.expected_mt5_login}；"
                "请切回正确账号或重启节点客户端"
            )

    def apply_hub_symbols(self, symbols) -> None:
        """接收中控台下发的观察品种列表。"""
        self.hub_symbols = {
            str(s).strip().upper() for s in (symbols or []) if str(s).strip()
        }
        logger.info("hub watch symbols (%d): %s", len(self.hub_symbols), sorted(self.hub_symbols))

    def apply_risk_config(self, risk) -> None:
        """接收账户级风控配置（登录 auth_ok 或保存后 risk_config）。"""
        previous = self.risk
        self.risk = account_risk.normalize_risk(risk if isinstance(risk, dict) else None)
        # 保护进度只在条目消失、被关闭或保护方向反转时清除。重连会重新收到一次
        # 配置，整体清零会让已进入保护的品种前功尽弃。
        self._protect_armed = account_risk.prune_armed_map(
            self.risk, self._protect_armed, previous=previous,
        )
        spo = len((self.risk.get(account_risk.RULE_SYMBOL_PL_ORDERS) or {}).get("items") or [])
        spp = len((self.risk.get(account_risk.RULE_SYMBOL_PL_PROTECT) or {}).get("items") or [])
        lpt = self.risk.get(account_risk.RULE_LOT_PL_TIERS) or {}
        logger.info(
            "hub risk config: spo=%d spp=%d lot_tiers enabled=%s batches=%s",
            spo, spp, lpt.get("enabled"), lpt.get("batch_count"),
        )

    def effective_watchlist(self, positions: list | None = None) -> list[str]:
        """本地 WATCH_SYMBOLS ∪ 中控台品种 ∪ 当前持仓品种。"""
        out: set[str] = {s.strip().upper() for s in settings.watchlist if s.strip()}
        out |= self.hub_symbols
        for p in positions or []:
            sym = str((p or {}).get("symbol") or "").strip().upper()
            if sym:
                out.add(sym)
        return sorted(out)

    async def _snapshot(self) -> dict:
        """采集一次账户快照（账户信息 + 持仓 + 观察列表报价）。"""
        try:
            positions = await self._exec(self.mt5.positions)
            quotes = await self._exec(self.mt5.quotes, self.effective_watchlist(positions))
            return {
                "account": await self._exec(self.mt5.account_info),
                "positions": positions,
                "quotes": quotes,
                "prices": {sym: q["mid"] for sym, q in quotes.items()},
            }
        except Exception as e:  # noqa: BLE001
            logger.debug("snapshot error: %s", e)
            return {"account": {}, "positions": [], "prices": {}, "quotes": {}}

    # ----------------------- 协议 ------------------------
    async def _authenticate(self, ws) -> bool:
        """首包发送 auth（token + MT5 登录号），等待 auth_ok；成功后再上报 hello。"""
        acct = await self._exec(self.mt5.account_info)
        try:
            self._check_login(acct)
        except LoginMismatchError as e:
            logger.error("%s", e)
            return False
        # 身份以启动绑定账号为准，避免终端已漂移时挂到错误节点
        login = self.expected_mt5_login
        await ws.send(json.dumps({
            "type": "auth",
            "data": {"token": settings.node_token, "mt5_login": login},
        }))
        logger.info("user_info: %s", acct)
        try:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=settings.auth_timeout))
        except Exception as e:  # noqa: BLE001
            logger.error("auth handshake failed: %s", e)
            return False
        if msg.get("type") == "auth_ok":
            data = msg.get("data") or {}
            logger.info("authenticated as node %s", data.get("node_id"))
            self.apply_hub_symbols(data.get("watch_symbols"))
            self.apply_risk_config(data.get("risk"))
            acct = await self._exec(self.mt5.account_info)
            try:
                self._check_login(acct)
            except LoginMismatchError as e:
                logger.error("%s", e)
                return False
            await ws.send(json.dumps(
                {"type": "hello", "data": {"login": acct.get("login"), "server": acct.get("server")}}
            ))
            return True
        # 鉴权失败：解析并显示后端给出的拒绝原因
        data = msg.get("data") or {}
        reason = data.get("reason") or msg.get("type") or "unknown"
        reason_text = {
            "invalid_token": "全局节点令牌无效；请到管理后台「账户设置 → 节点令牌」查看/重置后更新本机 .env",
            "already_online": "该 MT5 账户已有在线连接，本次登录被拒绝（同一账户同一时刻只允许一个在线）",
            "disabled": "节点已被管理员禁用，无法接入",
            "missing_mt5_login": "鉴权包缺少 MT5 账户登录号（请确认启动时输入的 MT5 账号正确且已成功登录终端）",
            "auto_register_failed": "节点自动注册失败，请联系管理员排查后台日志",
            "mt5_login_mismatch": "终端当前 MT5 账号与节点绑定账号不符，连接被拒绝",
        }.get(reason, data.get("message") or reason)
        logger.error("登录被拒绝：%s", reason_text)
        return False

    async def _serve(self, ws) -> None:
        """并发跑三个任务；任一退出(通常是断线)即取消其余，触发外层重连。"""
        tasks = [
            asyncio.create_task(self._reporter(ws)),
            asyncio.create_task(self._heartbeat(ws)),
            asyncio.create_task(self._receiver(ws)),
        ]
        try:
            done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                if t.cancelled():
                    continue
                exc = t.exception()
                if isinstance(exc, LoginMismatchError):
                    raise exc
        finally:
            for t in tasks:
                t.cancel()
            self._cancel_runners()

    async def _reporter(self, ws) -> None:
        """定时上报账户快照；顺带检查账户级风控；发现换号则抛错结束会话。"""
        while True:
            snap = await self._snapshot()
            self._check_login(snap.get("account") or {})
            await ws.send(json.dumps({"type": "account", "data": snap}))
            await self._check_account_risk(ws, snap)
            await asyncio.sleep(settings.account_report_interval)

    async def _heartbeat(self, ws) -> None:
        """定时心跳，维持服务端在线标记。"""
        while True:
            await ws.send(json.dumps({"type": "heartbeat", "data": {}}))
            await asyncio.sleep(settings.heartbeat_interval)

    async def _receiver(self, ws) -> None:
        """接收服务端下发的命令。"""
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            await self._handle(ws, msg)

    async def _handle(self, ws, msg: dict) -> None:
        """按命令类型分派：open / close / pong / watch_symbols；处理服务端登录号拒绝。"""
        mtype = msg.get("type")
        if mtype == "auth_fail":
            data = msg.get("data") or {}
            reason = data.get("reason") or "unknown"
            if reason == "mt5_login_mismatch":
                raise LoginMismatchError(
                    data.get("message")
                    or "终端当前 MT5 账号与节点绑定账号不符，连接被拒绝"
                )
            logger.error("服务端拒绝：%s", data.get("message") or reason)
            raise LoginMismatchError(data.get("message") or reason)

        if mtype == "watch_symbols":
            self.apply_hub_symbols((msg.get("data") or {}).get("symbols"))
            return
        if mtype == "risk_config":
            self.apply_risk_config((msg.get("data") or {}).get("risk"))
            return
        if mtype in ("pong", "ping"):
            return  # 心跳应答 / 探活，忽略

        cmd = msg.get("cmd")
        if cmd == "open":
            await self._do_open(ws, msg)
        elif cmd == "close":
            await self._do_close(ws, msg)
        elif cmd == "strategy_start":
            await self._do_strategy_start(ws, msg, resume=False)
        elif cmd == "strategy_resume":
            await self._do_strategy_start(ws, msg, resume=True)
        elif cmd == "strategy_stop":
            await self._do_strategy_stop(ws, msg)
        else:
            logger.debug("ignored message: %s", msg)

    # ---------------------- 账户级风控 ----------------------
    async def _execute_risk_close(self, hit: dict, positions: list) -> dict:
        """按命中规则的动作执行平仓。"""
        action = hit.get("close_action") or "account_all"
        # 账户级规则与分档的「全部」都是整账户清仓
        if action == "account_all" or (hit.get("scope_all_symbols") and action == "all"):
            return await self._exec(self.mt5.close_all)
        targets = account_risk.select_close_targets(
            positions,
            close_action=action,
            # 分档按账户侧向平仓，不限定品种
            symbol=None if hit.get("scope_all_symbols") else hit.get("symbol"),
        )
        if not targets:
            # 判定阶段已排除这种情况；真发生说明快照与实际持仓不一致，
            # 按失败处理让下一轮重新判定，不能白扣次数把规则关掉
            return {"success": False, "closed": 0, "action": "CLOSE",
                    "error": "没有符合该平仓动作的持仓"}
        return await self._exec(self.mt5.close_positions, targets)

    @staticmethod
    def _close_action_label(action: str | None) -> str:
        return {
            "account_all": "清仓全部",
            "all": "品种全平",
            "buy": "多单平仓",
            "sell": "空单平仓",
            "hedge": "锁单平仓",
        }.get(action or "", action or "平仓")

    async def _check_account_risk(self, ws, snap: dict) -> None:
        """账户级风控检查入口。

        跑在账户上报循环里，任何异常都只记日志：抛出去会连带结束上报任务，
        进而触发整条 WS 会话重连。
        """
        try:
            await self._run_account_risk(ws, snap)
        except Exception as e:  # noqa: BLE001
            logger.exception("account risk check failed: %s", e)

    async def _run_account_risk(self, ws, snap: dict) -> None:
        """检查各条风控规则；触达则按动作平仓并上报 risk_event。"""
        acct = snap.get("account") or {}
        positions = snap.get("positions") or []
        hit, new_armed, armed_events = account_risk.find_triggered_rule(
            self.risk,
            balance=acct.get("balance", 0),
            equity=acct.get("equity", 0),
            positions=positions,
            armed_map=self._protect_armed,
        )
        self._protect_armed = new_armed

        for ev in armed_events:
            await ws.send(json.dumps({
                "type": "risk_event",
                "data": {**ev, "success": True, "closed": 0},
            }))
            logger.info("account risk armed: %s", ev.get("message"))

        if not hit:
            return

        # 冷却只挡「执行平仓」，上面的保护状态机照常推进，
        # 否则刚触发过的这几轮里保护进度会整体停滞、错过收窄时机
        now = time.time()
        if now < self._risk_cooldown_until:
            return

        # 账户级全平或大范围平仓时停策略监控，避免继续加仓；
        # 结束原因写清规则名与触发参数，供后台「开单原因」展示。
        stop_reason = account_risk.describe_trigger(hit)
        stop_detail = account_risk.trigger_detail(hit)
        if hit.get("close_action") in ("account_all", "all") or hit.get("scope_all_symbols"):
            for runner in list(self.runners.values()):
                if not runner.done:
                    runner.request_stop(stop_reason, detail=stop_detail)

        res = await self._execute_risk_close(hit, positions)
        success = bool(res.get("success"))
        new_risk, state = account_risk.apply_trigger_to_risk(
            self.risk, hit["rule"], hit.get("item_id"),
        )
        if success and not state.get("matched", True):
            # 配置在本轮判定与回写之间被改过：次数没能扣减，靠冷却避免连续触发
            logger.warning(
                "risk rule %s item %s vanished before state write-back",
                hit["rule"], hit.get("item_id"),
            )
        if success:
            self.risk = new_risk
            # 保护规则触发后解除该条目 armed
            if hit.get("item_id"):
                self._protect_armed.pop(str(hit["item_id"]), None)
            self._risk_cooldown_until = now + max(5.0, float(settings.account_report_interval) * 2)

        remaining = state["remaining_times"] if success else hit.get("remaining_times", 0)
        disabled = bool(state["disabled"]) if success else False
        action_label = self._close_action_label(hit.get("close_action"))
        msg_parts = [
            hit.get("message_core") or hit["rule"],
            f"已{action_label}" if success else f"平仓失败：{res.get('error') or 'unknown'}",
        ]
        if success and disabled:
            msg_parts.append("指定次数已耗尽，规则已关闭")
        elif success and hit.get("monitor_mode") == account_risk.MONITOR_TIMES:
            msg_parts.append(f"剩余次数 {remaining}")

        event = {
            "rule": hit["rule"],
            "item_id": hit.get("item_id"),
            "event": "triggered",
            "action": hit.get("close_action") or hit.get("action"),
            "symbol": hit.get("symbol"),
            "success": success,
            "closed": int(res.get("closed") or 0),
            "monitor_mode": hit.get("monitor_mode"),
            "remaining_times": remaining,
            "disabled": disabled,
            "equity": hit.get("equity"),
            "message": "；".join(msg_parts),
            "error": res.get("error"),
        }
        if hit["rule"] == account_risk.RULE_FLOAT_PL_RATIO:
            event.update({
                "ratio_threshold": hit.get("threshold"),
                "current_ratio": round(hit["current_ratio"], 4) if hit.get("current_ratio") is not None else None,
                "floating_pl": round(hit["floating_pl"], 4) if hit.get("floating_pl") is not None else None,
                "balance": hit.get("balance"),
            })
        elif hit["rule"] == account_risk.RULE_EQUITY_MIN:
            event["amount_threshold"] = hit.get("amount")
        elif hit["rule"] == account_risk.RULE_SYMBOL_PL_ORDERS:
            event.update({
                "pl_amount": hit.get("pl_amount"),
                "current_pl": hit.get("current_pl"),
                "order_count": hit.get("order_count"),
            })
        elif hit["rule"] == account_risk.RULE_SYMBOL_PL_PROTECT:
            event.update({
                "trigger_amount": hit.get("trigger_amount"),
                "narrow_amount": hit.get("narrow_amount"),
                "current_pl": hit.get("current_pl"),
            })
        elif hit["rule"] == account_risk.RULE_LOT_PL_TIERS:
            event.update({
                "tier_index": hit.get("tier_index"),
                "min_lot": hit.get("min_lot"),
                "pl_amount": hit.get("pl_amount"),
                "current_lot": hit.get("current_lot"),
                "current_pl": hit.get("current_pl"),
            })
        if success:
            event["risk"] = self.risk
        await ws.send(json.dumps({"type": "risk_event", "data": event}))
        logger.warning("account risk: %s", event["message"])

    # ---------------------- 策略托管 ----------------------
    async def _do_strategy_start(self, ws, msg: dict, *, resume: bool) -> None:
        """启动（或恢复）一个分组策略任务的常驻监控。"""
        task_id = msg.get("task_id")
        magic = msg.get("magic")
        if not task_id or not magic:
            logger.warning("strategy_start missing task_id/magic: %s", msg)
            return
        task_id = int(task_id)
        existing = self.runners.get(task_id)
        if existing and not existing.done:
            logger.info("task %s already running, ignore duplicate start", task_id)
            return

        async def send(payload: dict) -> None:
            await ws.send(json.dumps(payload))

        runner = StrategyRunner(
            task_id=task_id,
            magic=int(magic),
            group_id=msg.get("group_id") or "",
            signal_id=msg.get("signal_id") or "",
            entry=msg.get("entry") or {},
            strategy=msg.get("strategy") or {},
            mt5=self.mt5,
            hub=self._ensure_hub(),
            exec_fn=self._exec,
            send_fn=send,
            report_interval=msg.get("report_interval") or 5,
        )
        self.runners[task_id] = runner
        runner.start(resume=resume)
        logger.info(
            "strategy task %s %s (magic=%s symbol=%s)",
            task_id, "resumed" if resume else "started", magic, (msg.get("entry") or {}).get("symbol"),
        )

    async def _do_strategy_stop(self, ws, msg: dict) -> None:
        """终止指令：平掉该任务魔术号的全部持仓并结束监控。"""
        try:
            task_id = int(msg.get("task_id"))
        except (TypeError, ValueError):
            logger.warning("strategy_stop with invalid task_id: %s", msg)
            return
        runner = self.runners.get(task_id)
        if runner:
            runner.request_stop(msg.get("reason") or "stop_command")
            logger.info("strategy task %s stop requested", task_id)
            return
        # 本地没有监控：节点重启过，或服务端补发了离线期间的终止指令。
        # 此时按魔术号直接平掉残留持仓，否则这批仓位再也没人负责收口。
        await self._close_orphan_strategy(ws, task_id, msg)

    async def _close_orphan_strategy(self, ws, task_id: int, msg: dict) -> None:
        """无本地监控时按魔术号平仓，并回报结果供服务端收口。"""
        try:
            magic = int(msg.get("magic"))
        except (TypeError, ValueError):
            logger.info("strategy_stop for unknown task %s without magic, ignored", task_id)
            return
        try:
            res = await self._exec(self.mt5.close_by_magic, magic)
        except Exception as e:  # noqa: BLE001
            logger.warning("strategy_stop close by magic %s failed: %s", magic, e)
            return
        closed = int(res.get("closed") or 0)
        logger.warning(
            "strategy task %s has no local runner, closed %d position(s) by magic %s",
            task_id, closed, magic,
        )
        realized = 0.0
        try:
            if hasattr(self.mt5, "realized_profit_by_magic"):
                # 孤儿收口没有精确任务起点，取最近一天成交汇总
                realized = float(
                    await self._exec(
                        self.mt5.realized_profit_by_magic, magic, time.time() - 86400,
                    )
                    or 0.0
                )
        except Exception:  # noqa: BLE001
            logger.debug("orphan realized_profit lookup failed", exc_info=True)
            realized = float(res.get("profit") or 0.0)
        # 不带累计单量字段：服务端已有历史统计，这里回传 0 会把它覆盖掉
        await ws.send(json.dumps({
            "type": "strategy_finished",
            "data": {
                "task_id": task_id,
                "magic": magic,
                "group_id": msg.get("group_id") or "",
                "signal_id": msg.get("signal_id") or "",
                "symbol": msg.get("symbol"),
                "status": "done" if res.get("success") else "failed",
                "reason": msg.get("reason") or "stop_command",
                "realized_profit": round(realized, 2),
            },
        }))

    def _cancel_runners(self) -> None:
        """断线时取消本地监控循环；MT5 持仓保留，等重连后由服务端下发恢复。"""
        for runner in self.runners.values():
            runner.cancel()
        self.runners.clear()

    async def _do_open(self, ws, msg: dict) -> None:
        """执行开仓并回报结果（带 signal_id/symbol 供服务端关联与释放锁）。"""
        acct = await self._exec(self.mt5.account_info)
        try:
            self._check_login(acct)
        except LoginMismatchError as e:
            await ws.send(json.dumps({
                "type": "trade_result",
                "data": {
                    "success": False,
                    "error": str(e),
                    "signal_id": msg.get("signal_id"),
                    "symbol": msg.get("symbol"),
                    "action": msg.get("action"),
                },
            }))
            raise

        res = await self._exec(
            self.mt5.place_market_order,
            msg["symbol"], msg["action"], msg["volume"],
            msg.get("stop_loss"), msg.get("take_profit"),
            msg.get("comment", ""), msg.get("magic"),
        )
        res["signal_id"] = msg.get("signal_id")
        res.setdefault("symbol", msg.get("symbol"))
        await ws.send(json.dumps({"type": "trade_result", "data": res}))
        logger.info("open result: %s", res)

    async def _do_close(self, ws, msg: dict) -> None:
        """执行平仓（按订单/按品种/全平）并回报结果。"""
        target = msg.get("close_target", "all")
        if target == "ticket" and msg.get("close_ticket"):
            res = await self._exec(self.mt5.close_ticket, msg["close_ticket"])
        elif target == "symbol" and msg.get("close_symbol"):
            res = await self._exec(self.mt5.close_symbol, msg["close_symbol"])
        else:
            res = await self._exec(self.mt5.close_all)
        res["signal_id"] = msg.get("signal_id")
        res.setdefault("action", "CLOSE")
        res.setdefault("symbol", msg.get("close_symbol"))
        res["detail"] = self._close_detail(msg, res)
        await ws.send(json.dumps({"type": "trade_result", "data": res}))
        logger.info("close result: %s", res)

    @staticmethod
    def _close_detail(msg: dict, res: dict) -> str:
        target = msg.get("close_target", "all")
        ticket = res.get("ticket") or msg.get("close_ticket")
        symbol = res.get("symbol") or msg.get("close_symbol")
        closed = int(res.get("closed") or (1 if res.get("success") and target == "ticket" else 0))
        if target == "ticket" and ticket:
            parts = [f"订单 #{ticket}"]
            if symbol:
                parts.append(symbol)
            if res.get("volume"):
                parts.append(f"{res['volume']} 手")
            return " · ".join(parts)
        if target == "symbol" and symbol:
            return f"品种 {symbol}" + (f" · {closed} 笔" if closed > 1 else "")
        if target == "all":
            return f"全平 {closed} 笔"
        return ""


async def main() -> None:
    try:
        creds = prompt_mt5_credentials()
    except FileNotFoundError as e:
        logger.error("%s", e)
        raise SystemExit(1) from e
    except Exception as e:
        from mt5_client import MT5Error

        if isinstance(e, MT5Error):
            logger.error("%s", e)
            raise SystemExit(1) from e
        raise
    await NodeClient(**creds).run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("node stopped")
