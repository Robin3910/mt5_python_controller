"""断联时的「假空仓」防护：终端与券商断连那一瞬，任务不能被提前收口。

要挡住的是这条真实事故（线上日志）：
    21:22:02 task 800697 no position left (2 consecutive empty rounds), finishing: positions_cleared
    21:22:02 task 800697 finished: done (positions_cleared) realized_profit=-2.9
    21:22:03 MT5 link lost: terminal not connected to trade server (3 hits)

任务收口发生在**链路状态机判离线之前**：hub 采样 0.5 秒一轮、连续 2 轮空仓
就派发 GONE（约 1 秒），而链路去抖要连续 3 次读到断开才认离线（约 3 秒）。
断联时 `positions_get` 会**成功**返回空集合 —— 终端此刻没有在线账户，
「没有持仓」只是「读不到在线账户」的假象。把它当真空仓，任务就提前收口，
盘上还活着的仓位变成没人管的孤儿仓。

这里验证四层防护：
1. MarketHub：断联时读到空仓 → 按「读不到」处理（STALE），不派发 GONE；
2. StrategyRunner（模版1）：即使收到空仓也先确认账本可信，不可信就不收口；
3. NodeClient 快照：断联时空账本 → 降级快照 books_ok=False，服务端跳过对账；
4. 恢复后：链路回来、持仓还在，任务照常继续跑。
"""
import asyncio
import json

import pytest

import market_hub as mh
import mock_mt5
import mt5_client
import node_client as nc
from mock_mt5 import MockMT5Client
from strategy_runner import StrategyRunner

MAGIC = 900000001


def counter_rule(**over) -> dict:
    rule = {
        "type": 1, "status": 1, "action": "all",
        "point": 100.0, "lot_times": 1.0, "extra_lot": 0.0,
        "max_allow_num": 3, "batch_enabled": False, "batch_levels": [],
    }
    rule.update(over)
    return rule


async def _exec(fn, *args):
    return fn(*args)


async def _wait_until(predicate, timeout: float = 5.0, step: float = 0.02) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(step)
    return predicate()


def _finished(sent: list) -> list[dict]:
    return [m["data"] for m in sent if m["type"] == "strategy_finished"]


def _build(strategy: dict, mt5: MockMT5Client, sent: list):
    hub = mh.MarketHub(mt5, _exec, interval=0.02, idle_interval=0.2, empty_confirm=2)

    async def send(payload: dict) -> None:
        sent.append(payload)

    runner = StrategyRunner(
        task_id=1, magic=MAGIC, group_id="g1", signal_id="s1",
        entry={"action": "BUY", "symbol": "XAUUSD", "volume": 0.1},
        strategy=strategy,
        mt5=mt5, hub=hub, exec_fn=_exec, send_fn=send, report_interval=1.0,
    )
    return runner, hub


# ---------------------------------------------------------------------------
# 1. MarketHub：断联时读到空仓不能派发 GONE
# ---------------------------------------------------------------------------


async def test_hub_does_not_dispatch_gone_while_link_down():
    """断联时的空仓是假象：hub 必须按「读不到」处理，绝不派发 GONE。"""
    mt5 = MockMT5Client()
    mt5.connected = True
    mt5.link_connected = True
    hub = mh.MarketHub(mt5, _exec, interval=0.02, idle_interval=0.2, empty_confirm=2)

    sub = hub.subscribe(symbol="XAUUSD", magic=MAGIC, direction="BUY")
    sub.finish_on_empty = True  # 模版1 口径：没持仓就结束任务
    hub.start()
    try:
        await hub.sample()
        mt5.link_connected = False
        for _ in range(3):
            await hub.sample()

        event = await asyncio.wait_for(sub.next_event(), timeout=2.0)
        assert event is not None
        assert event.kind != mh.GONE, "断联时不能派发 GONE"
        assert event.kind == mh.STALE
    finally:
        await hub.close()


async def test_hub_dispatches_gone_after_link_recovers_with_empty_book():
    """链路恢复后账本可信：真的空仓就照常派发 GONE（别把防护做成永不发）。"""
    mt5 = MockMT5Client()
    mt5.connected = True
    mt5.link_connected = True
    hub = mh.MarketHub(mt5, _exec, interval=0.02, idle_interval=0.2, empty_confirm=2)

    sub = hub.subscribe(symbol="XAUUSD", magic=MAGIC, direction="BUY")
    sub.finish_on_empty = True  # 模版1 口径：没持仓就结束任务
    hub.start()
    try:
        await hub.sample()
        mt5.link_connected = False
        for _ in range(3):
            await hub.sample()
        assert await _wait_until(
            lambda: sub._latest is not None and sub._latest.kind == mh.STALE
        ), "断联时应该发 STALE"

        # 链路恢复，账本可信且确实没有持仓 → 连续 2 轮空仓 → GONE
        mt5.link_connected = True
        for _ in range(4):
            await hub.sample()

        assert await _wait_until(
            lambda: sub._latest is not None and sub._latest.kind == mh.GONE
        ), "链路恢复后真的空仓应该派发 GONE"
    finally:
        await hub.close()


# ---------------------------------------------------------------------------
# 2. StrategyRunner（模版1）：断联时不能收口
# ---------------------------------------------------------------------------


async def test_tpl1_does_not_finish_while_link_down():
    """断联期间任务不能收口：盘上还活着的仓位不能变成孤儿仓。"""
    sent: list = []
    mt5 = MockMT5Client()
    mt5.connected = True
    mt5.link_connected = True
    runner, hub = _build(
        {"template_id": "tpl_1", "rules": [counter_rule()]}, mt5, sent,
    )
    hub.start()
    runner.start()
    try:
        assert await _wait_until(
            lambda: len(mt5.positions_by_magic(MAGIC)) == 1
        ), "首单没有成交"

        # 断联：持仓在 MT5 里原封不动，只是读不到
        mt5.link_connected = False
        await asyncio.sleep(0.6)

        assert not _finished(sent), "断联时任务不该收口"
        assert not runner.done
    finally:
        runner.cancel()
        await hub.close()


async def test_tpl1_keeps_running_after_link_recovers():
    """链路恢复后任务照常继续跑：断联不该把任务带走。"""
    sent: list = []
    mt5 = MockMT5Client()
    mt5.connected = True
    mt5.link_connected = True
    runner, hub = _build(
        {"template_id": "tpl_1", "rules": [counter_rule()]}, mt5, sent,
    )
    hub.start()
    runner.start()
    try:
        assert await _wait_until(
            lambda: len(mt5.positions_by_magic(MAGIC)) == 1
        ), "首单没有成交"

        mt5.link_connected = False
        await asyncio.sleep(0.4)
        mt5.link_connected = True
        await asyncio.sleep(0.4)

        assert not _finished(sent)
        assert not runner.done
        assert len(mt5.positions_by_magic(MAGIC)) == 1
    finally:
        runner.cancel()
        await hub.close()


async def test_tpl1_finishes_when_position_really_closed():
    """反向验证：真被平掉了（链路正常）时照样收口，防护不能把规则改废。"""
    sent: list = []
    mt5 = MockMT5Client()
    mt5.connected = True
    mt5.link_connected = True
    runner, hub = _build(
        {"template_id": "tpl_1", "rules": [counter_rule()]}, mt5, sent,
    )
    hub.start()
    runner.start()
    try:
        assert await _wait_until(
            lambda: len(mt5.positions_by_magic(MAGIC)) == 1
        ), "首单没有成交"

        mt5.clear_by_magic_with_reason(MAGIC, reason=4)

        assert await _wait_until(lambda: bool(_finished(sent))), "任务没有收口"
        assert _finished(sent)[0]["status"] == "done"
    finally:
        await hub.close()


# ---------------------------------------------------------------------------
# 3. NodeClient 快照：断联时空账本必须降级上报
# ---------------------------------------------------------------------------


class FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))


async def test_snapshot_reports_degraded_while_link_down():
    """断联时账本读不到（空集合）→ 降级快照 books_ok=False。

    服务端据此保留上次快照、跳过对账：不会把「读不到」当成「已平光」
    而错误收口任务。
    """
    n = nc.NodeClient()
    n.loop = asyncio.get_running_loop()
    await n._exec(n.mt5.connect)
    n.mt5.link_connected = False

    snap = await n._snapshot()
    assert snap is None, "断联时不得上报空账本"

    degraded = await n._degraded_snapshot()
    assert degraded is not None
    assert degraded["books_ok"] is False
    assert degraded["positions"] == []


async def test_snapshot_reports_books_when_link_online():
    """链路正常时照常上报账本：降级不能变成常态。"""
    n = nc.NodeClient()
    n.loop = asyncio.get_running_loop()
    await n._exec(n.mt5.connect)
    n.mt5.link_connected = True

    snap = await n._snapshot()
    assert snap is not None
    assert snap["books_ok"] is True


async def test_reporter_sends_degraded_snapshot_when_link_down():
    """断联时账本读成空集合 → 上报降级快照（books_ok=False），不上报空账本。

    服务端据此保留上次快照、跳过对账：不会把「读不到」当成「已平光」
    而错误收口任务。链路状态照常送达，服务端知道节点断联了。
    """
    n = nc.NodeClient()
    n.loop = asyncio.get_running_loop()
    await n._exec(n.mt5.connect)
    n.mt5.link_connected = False
    ws = FakeWS()

    async def stop(_delay):
        raise asyncio.CancelledError

    orig_sleep = nc.asyncio.sleep
    nc.asyncio.sleep = stop
    try:
        with pytest.raises(asyncio.CancelledError):
            await n._reporter(ws)
    finally:
        nc.asyncio.sleep = orig_sleep

    accounts = [m for m in ws.sent if m["type"] == "account"]
    assert len(accounts) == 1
    data = accounts[0]["data"]
    assert data["books_ok"] is False
    assert data["positions"] == []
    assert data["orders"] == []


# ---------------------------------------------------------------------------
# 4. 客户端侧：books_trustworthy 的口径
# ---------------------------------------------------------------------------


def test_books_trustworthy_follows_link_state():
    """账本可信 = 终端与券商连通；读不到链路状态也算不可信。"""
    mt5 = MockMT5Client()
    mt5.connected = True
    mt5.link_connected = True
    assert mt5.books_trustworthy() is True

    mt5.link_connected = False
    assert mt5.books_trustworthy() is False

    mt5.link_connected = None
    assert mt5.books_trustworthy() is False


def test_real_client_books_trustworthy_requires_terminal_connected():
    """真实客户端：现读 terminal_info().connected，不走去抖。

    去抖口径要连续 3 次读到断开才认离线（约 3 秒），而误收口只要
    连续 2 轮空仓（约 1 秒）——等去抖判离线时任务早就收掉了。
    """
    client = mt5_client.MT5Client.__new__(mt5_client.MT5Client)
    client.connected = True

    class _Info:
        def __init__(self, connected):
            self.connected = connected

    info = _Info(True)
    client_module = mt5_client
    original = client_module.mt5
    try:
        client_module.mt5 = type(
            "_MT5", (), {"terminal_info": lambda self: info},
        )()
        assert client.books_trustworthy() is True
        info = _Info(False)
        assert client.books_trustworthy() is False
        info = None
        assert client.books_trustworthy() is False
    finally:
        client_module.mt5 = original
