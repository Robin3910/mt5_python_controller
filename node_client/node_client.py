"""MT5 节点客户端：WebSocket 连接 / 鉴权 / 心跳 / 账户上报 / 执行命令。

运行：python node_client.py
配置见 .env（参考 .env.example）；MT5 账号/密码/服务器/路径在启动时手动输入。
设置 MT5_MOCK=true 可在无终端时用模拟器联调。

设计要点：
- MetaTrader5 的调用是阻塞式的，统一丢到线程池(run_in_executor)，不阻塞事件循环；
- 断线自动重连（指数退避）；
- 三个并发任务：账户上报 / 心跳 / 接收命令，任一结束即重建连接；
- 启动时绑定的 MT5 登录号与终端实时 account_info.login 不一致时主动停交易并断线。
"""
import asyncio
import json
import logging

import websockets

from config import get_settings
from mt5_prompt import prompt_mt5_credentials

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
    )


class NodeClient:
    def __init__(
        self,
        *,
        mt5_login: int = 90000001,
        mt5_password: str = "",
        mt5_server: str = "MockServer",
        mt5_path: str = "",
    ) -> None:
        self.expected_mt5_login = int(mt5_login)
        self.mt5 = make_client(mt5_login, mt5_password, mt5_server, mt5_path)
        self.loop: asyncio.AbstractEventLoop | None = None
        self._stop = False

    async def run(self) -> None:
        """主入口：先连 MT5，再进入“连接-鉴权-服务”的自动重连循环。"""
        self.loop = asyncio.get_running_loop()
        await self._connect_mt5()
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

    # ---------------------- MT5 辅助 ----------------------
    async def _exec(self, fn, *args):
        """把阻塞式 MT5 调用放到线程池执行，避免阻塞事件循环。"""
        return await self.loop.run_in_executor(None, lambda: fn(*args))

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

    async def _snapshot(self) -> dict:
        """采集一次账户快照（账户信息 + 持仓 + 观察列表报价）。"""
        try:
            quotes = await self._exec(self.mt5.quotes, settings.watchlist)
            return {
                "account": await self._exec(self.mt5.account_info),
                "positions": await self._exec(self.mt5.positions),
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
            logger.info("authenticated as node %s", (msg.get("data") or {}).get("node_id"))
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

    async def _reporter(self, ws) -> None:
        """定时上报账户快照；发现换号则抛错结束会话。"""
        while True:
            snap = await self._snapshot()
            self._check_login(snap.get("account") or {})
            await ws.send(json.dumps({"type": "account", "data": snap}))
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
        """按命令类型分派：open / close / pong；处理服务端登录号拒绝。"""
        if msg.get("type") == "auth_fail":
            data = msg.get("data") or {}
            reason = data.get("reason") or "unknown"
            if reason == "mt5_login_mismatch":
                raise LoginMismatchError(
                    data.get("message")
                    or "终端当前 MT5 账号与节点绑定账号不符，连接被拒绝"
                )
            logger.error("服务端拒绝：%s", data.get("message") or reason)
            raise LoginMismatchError(data.get("message") or reason)

        cmd = msg.get("cmd")
        if cmd == "open":
            await self._do_open(ws, msg)
        elif cmd == "close":
            await self._do_close(ws, msg)
        elif msg.get("type") == "pong":
            pass  # 心跳应答，忽略
        else:
            logger.debug("ignored message: %s", msg)

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
    creds = prompt_mt5_credentials()
    await NodeClient(**creds).run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("node stopped")
