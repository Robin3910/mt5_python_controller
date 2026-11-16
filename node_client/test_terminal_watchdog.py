"""终端看门狗：MT5 被关掉再打开后，节点自动重新附着。

要解决的问题：用户直接关掉 MT5 再打开，`MetaTrader5` 这个 C 扩展绑定的是
**当初那个终端进程**，进程没了之后所有 IPC 调用都失效。节点必须重新
`initialize` 才能附着到新起的进程上 —— 以前只能重启 node_client。

这里覆盖：
- 终端进程消失 → 连续确认几轮后开始重连；
- 终端还没起来 → 重连失败，按退避继续重试；
- 终端起来 → 重连成功，恢复到正常状态；
- 单次 IPC 抖动 → 不触发重连（不能因为一次读不到就重连）；
- 终端在、只是券商断链 → 不触发重连（那是链路问题，不是进程问题）。
"""
import asyncio

import node_client as nc


def _node():
    n = nc.NodeClient()
    n.loop = asyncio.get_running_loop()
    return n


async def _connected_node():
    """已附着到终端的节点（模拟端默认是未连接状态）。"""
    n = _node()
    await n._exec(n.mt5.connect)
    return n


async def _wait_until(predicate, timeout: float = 3.0, step: float = 0.01) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(step)
    return predicate()


def _fast_settings(monkeypatch, **over):
    """把看门狗的间隔调到毫秒级，测试不用等真实的 2 秒。"""
    monkeypatch.setattr(nc.settings, "terminal_watch_interval", 0.01)
    monkeypatch.setattr(nc.settings, "terminal_watch_confirm", 2)
    monkeypatch.setattr(nc.settings, "terminal_reconnect_min", 0.01)
    monkeypatch.setattr(nc.settings, "terminal_reconnect_max", 0.02)
    for key, value in over.items():
        monkeypatch.setattr(nc.settings, key, value)


async def test_watchdog_reconnects_after_terminal_restarts(monkeypatch):
    """终端重启 → 节点自动重新附着，不需要重启 node_client。

    模拟真实的重启时序：终端进程短暂消失（用户正在重启 MT5），
    然后新进程起来。节点在新进程起来后自动跟上。
    """
    _fast_settings(monkeypatch)
    n = await _connected_node()

    task = asyncio.create_task(n._terminal_watchdog())
    try:
        # 用户关掉 MT5 再打开：中间有一小段进程不存在的窗口
        n.mt5.terminal_alive = False
        await asyncio.sleep(0.05)

        # 新进程起来了
        n.mt5.terminal_alive = True
        assert await _wait_until(
            lambda: getattr(n.mt5, "reconnect_calls", 0) >= 1
        ), "看门狗没有尝试重连"
        assert await _wait_until(lambda: n.mt5.ipc_ok is True), "没有重新附着上"
    finally:
        task.cancel()


async def test_watchdog_does_not_relaunch_closed_terminal(monkeypatch):
    """用户把 MT5 关着 → 节点绝不擅自把它开回来。

    这是最重要的安全约束：`mt5.initialize()` 在终端没跑时会自动启动它。
    用户手动关掉 MT5 通常就是想停止交易，节点擅自重开会让策略立刻恢复
    下单 —— 最不能接受的行为。
    """
    _fast_settings(monkeypatch)
    n = await _connected_node()
    # 用户把 MT5 关了，并且没打算再开
    n.mt5.terminal_alive = False

    task = asyncio.create_task(n._terminal_watchdog())
    try:
        await asyncio.sleep(0.15)
        assert getattr(n.mt5, "reconnect_calls", 0) == 0, (
            "终端被用户关掉时不该尝试重连（initialize 会把终端拉起来）"
        )
    finally:
        task.cancel()


async def test_watchdog_reconnects_when_terminal_comes_back(monkeypatch):
    """用户把 MT5 关掉，之后又自己打开 → 节点这时才重新附着。"""
    _fast_settings(monkeypatch)
    n = await _connected_node()
    n.mt5.terminal_alive = False

    task = asyncio.create_task(n._terminal_watchdog())
    try:
        # 终端关着：不动
        await asyncio.sleep(0.1)
        assert getattr(n.mt5, "reconnect_calls", 0) == 0

        # 用户把 MT5 开回来了 → 自动跟上
        n.mt5.terminal_alive = True
        assert await _wait_until(
            lambda: getattr(n.mt5, "reconnect_calls", 0) >= 1
        ), "没有尝试重连"
        assert await _wait_until(lambda: n.mt5.ipc_ok is True), "没有重新附着上"
    finally:
        task.cancel()


async def test_watchdog_retries_until_terminal_is_ready(monkeypatch):
    """终端在、但还没就绪 → 重连失败，按退避继续重试，不放弃。

    这里终端**进程是在跑的**（用户已经把 MT5 开回来了），只是刚启动、
    还没连上券商。这种情况 initialize 不会去启动终端，只是暂时附不上，
    所以重试是对的。
    """
    _fast_settings(monkeypatch)
    n = await _connected_node()
    # 终端进程在跑，但绑定断了（刚启动还没就绪）
    n.mt5.break_ipc()
    n.mt5.terminal_attach_fails = True

    task = asyncio.create_task(n._terminal_watchdog())
    try:
        assert await _wait_until(
            lambda: getattr(n.mt5, "reconnect_calls", 0) >= 2
        ), "没有持续重试"

        # 终端就绪 → 终于连上
        n.mt5.terminal_attach_fails = False
        assert await _wait_until(lambda: n.mt5.ipc_ok is True)
    finally:
        task.cancel()


async def test_watchdog_ignores_single_ipc_glitch(monkeypatch):
    """单次读不到终端（IPC 抖动）不该触发重连。"""
    _fast_settings(monkeypatch, terminal_watch_confirm=3)
    n = await _connected_node()

    task = asyncio.create_task(n._terminal_watchdog())
    try:
        # 只抖一轮就恢复
        n.mt5.terminal_alive = False
        await asyncio.sleep(0.02)
        n.mt5.terminal_alive = True
        await asyncio.sleep(0.1)

        assert getattr(n.mt5, "reconnect_calls", 0) == 0, "抖动触发了重连"
    finally:
        task.cancel()


async def test_watchdog_ignores_broker_disconnect(monkeypatch):
    """终端在、只是与券商断链 → 不重连（那是链路问题，不是进程问题）。

    重连在这种场景下没有意义：initialize 只会重新绑到同一个进程上，
    还会打断正在跑的策略监控。
    """
    _fast_settings(monkeypatch)
    n = await _connected_node()
    # 链路断开，但终端进程还在
    n.mt5.link_connected = False

    task = asyncio.create_task(n._terminal_watchdog())
    try:
        await asyncio.sleep(0.1)
        assert getattr(n.mt5, "reconnect_calls", 0) == 0, "链路断连触发了重连"
    finally:
        task.cancel()


async def test_watchdog_stays_quiet_while_terminal_is_healthy(monkeypatch):
    """终端正常时不重连，也不刷重连日志。"""
    _fast_settings(monkeypatch)
    n = await _connected_node()

    task = asyncio.create_task(n._terminal_watchdog())
    try:
        await asyncio.sleep(0.1)
        assert getattr(n.mt5, "reconnect_calls", 0) == 0
        assert n.mt5.connected is True
    finally:
        task.cancel()


async def test_terminal_alive_reads_status_alive_flag(monkeypatch):
    """`_terminal_alive` 读 terminal_status 的 alive 字段。"""
    n = await _connected_node()
    n.mt5.terminal_alive = True
    assert await n._terminal_alive() is True

    n.mt5.terminal_alive = False
    assert await n._terminal_alive() is False


async def test_status_snapshot_reports_terminal_state(monkeypatch):
    """状态口要能看到终端看门狗的状态。"""
    _fast_settings(monkeypatch)
    n = await _connected_node()
    assert n.status_snapshot()["mt5_terminal"] == "unknown"

    n._terminal_ok = True
    assert n.status_snapshot()["mt5_terminal"] == "attached"

    n._terminal_ok = False
    n._terminal_waiting = True
    assert n.status_snapshot()["mt5_terminal"] == "waiting_terminal"

    n._terminal_reconnecting = True
    assert n.status_snapshot()["mt5_terminal"] == "reconnecting"


async def test_terminal_alive_false_when_status_read_throws(monkeypatch):
    """读终端状态本身抛异常 → 视为终端没了（由看门狗去重连）。"""
    n = await _connected_node()

    def boom():
        raise RuntimeError("terminal gone")

    monkeypatch.setattr(n.mt5, "terminal_status", boom)
    assert await n._terminal_alive() is False


async def test_reconnect_terminal_reports_failure(monkeypatch):
    """重连失败要返回 False，不能假装成功。"""
    n = await _connected_node()
    monkeypatch.setattr(n.mt5, "reconnect", lambda: False)
    assert await n._reconnect_terminal() is False


async def test_mt5_client_reconnect_resets_link_state(monkeypatch):
    """真实客户端的 reconnect() 要复位链路状态机。

    旧进程的「已离线」判定对新进程没有意义，不复位的话节点要连续读到
    两次在线才会恢复放行。
    """
    import mt5_client as mc

    client = mc.MT5Client(42, "", "S1", path=r"C:\MT5\terminal64.exe")
    client.connected = True
    client._link_state = mc.LINK_OFFLINE
    client._link_offline_hits = mc.LINK_OFFLINE_CONFIRM

    calls: list[str] = []

    class FakeMT5:
        def initialize(self, path):
            calls.append("initialize")
            return True

        def account_info(self):
            return type("AI", (), {"login": 42, "server": "S1"})()

        def shutdown(self):
            calls.append("shutdown")

    monkeypatch.setattr(mc, "mt5", FakeMT5())
    # 复用会话模式：不需要密码，登录号从终端读
    client.reuse_terminal_session = True
    # 终端进程探针：这个测试不该依赖本机是不是真装了 MT5
    monkeypatch.setattr(client, "terminal_process_running", lambda: True)

    assert client.reconnect() is True
    assert calls[0] == "shutdown", "重连必须先拆掉指向旧进程的绑定"
    assert calls[1] == "initialize"
    assert client.connected is True
    assert client._link_state == mc.LINK_ONLINE
    assert client._link_offline_hits == 0


async def test_mt5_client_reconnect_refuses_when_terminal_not_running(monkeypatch):
    """终端进程没在跑 → 拒绝重连，绝不把终端启动起来。

    `mt5.initialize()` 在终端没跑时会自动启动它，而用户手动关掉 MT5
    通常就是想停止交易 —— 擅自重开会让策略立刻恢复下单。
    """
    import mt5_client as mc

    client = mc.MT5Client(42, "", "S1", path=r"C:\MT5\terminal64.exe")
    client.connected = True

    initialized: list[str] = []

    class FakeMT5:
        def initialize(self, path):
            initialized.append(path)
            return True

        def shutdown(self):
            initialized.append("shutdown")

    monkeypatch.setattr(mc, "mt5", FakeMT5())
    monkeypatch.setattr(client, "terminal_process_running", lambda: False)

    assert client.reconnect() is False
    assert "initialize" not in initialized, "终端没在跑时不该 initialize（会把终端启动起来）"
