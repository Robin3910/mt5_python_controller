"""端到端：MT5 终端重启后，节点自动恢复，策略监控不丢。

这条链路串起了三件事：

1. 终端进程没了 → 看门狗发现绑定失效；
2. 用户把 MT5 开回来 → 看门狗重新附着（**不会擅自把终端开起来**）；
3. 重新附着后，策略监控自己读回持仓继续跑 —— 任务不能因为终端重启
   就被判失败、也不能把持仓当成「已平光」而错误收口。

这里跑的是真实链路：真实 MarketHub 采样 + 真实 StrategyRunner +
真实 NodeClient 看门狗，只把 MT5 换成模拟端。
"""
import asyncio

import market_hub as mh
import node_client as nc
from mock_mt5 import MockMT5Client
from strategy_runner import StrategyRunner

MAGIC = 900000002


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


def _fast_watchdog(monkeypatch):
    monkeypatch.setattr(nc.settings, "terminal_watch_interval", 0.01)
    monkeypatch.setattr(nc.settings, "terminal_watch_confirm", 2)
    monkeypatch.setattr(nc.settings, "terminal_reconnect_min", 0.01)
    monkeypatch.setattr(nc.settings, "terminal_reconnect_max", 0.02)


async def test_strategy_survives_terminal_restart(monkeypatch):
    """终端重启 → 节点恢复 → 策略监控继续跑，持仓没丢、任务没被误收口。

    关键点：终端消失期间 hub 读不到账本，只会派发 STALE（"读不到"，
    不是"已平光"）。策略靠这条语义活下来；重新附着后读回持仓继续跑。
    """
    _fast_watchdog(monkeypatch)
    sent: list = []
    mt5 = MockMT5Client()
    mt5.connected = True
    mt5._ipc_ok = True

    hub = mh.MarketHub(mt5, _exec, interval=0.02, idle_interval=0.2, empty_confirm=2)

    async def send(payload: dict) -> None:
        sent.append(payload)

    runner = StrategyRunner(
        task_id=1, magic=MAGIC, group_id="g1", signal_id="s1",
        entry={"action": "BUY", "symbol": "XAUUSD", "volume": 0.1},
        strategy={"template_id": "tpl_1", "rules": [counter_rule()]},
        mt5=mt5, hub=hub, exec_fn=_exec, send_fn=send, report_interval=1.0,
    )

    hub.start()
    runner.start()
    watchdog = asyncio.create_task(nc.NodeClient._terminal_watchdog(
        _StubNode(mt5),
    ))
    try:
        # 首单成交
        assert await _wait_until(
            lambda: len(mt5.positions_by_magic(MAGIC)) == 1
        ), "首单没有成交"

        # 用户关掉 MT5：终端进程没了，IPC 绑定失效
        mt5.terminal_alive = False
        mt5.break_ipc()
        assert await _wait_until(lambda: mt5.ipc_ok is False)

        # 终端关着期间：任务不能被判失败，更不能被判「已平光」而收口
        await asyncio.sleep(0.2)
        assert not _finished(sent), "终端重启期间任务被误收口了"
        assert not runner.done, "终端重启期间任务被判失败了"

        # 用户把 MT5 开回来 → 节点自动重新附着
        mt5.terminal_alive = True
        assert await _wait_until(
            lambda: getattr(mt5, "reconnect_calls", 0) >= 1
        ), "节点没有尝试重连"
        assert await _wait_until(lambda: mt5.ipc_ok is True), "节点没有重新附着上"

        # 恢复后策略继续跑，持仓还在
        await asyncio.sleep(0.2)
        assert not _finished(sent), "恢复后任务被误收口了"
        assert not runner.done, "恢复后任务被判失败了"
        assert len(mt5.positions_by_magic(MAGIC)) == 1, "持仓丢了"
    finally:
        watchdog.cancel()
        runner.cancel()
        await hub.close()


class _StubNode:
    """只借用 NodeClient 的看门狗逻辑，MT5 用模拟端。"""

    def __init__(self, mt5: MockMT5Client) -> None:
        self.mt5 = mt5
        self.loop = asyncio.get_running_loop()
        self._mt5_executor = None
        self._algo_trading_allowed = None

    async def _exec(self, fn, *args):
        return fn(*args)

    def _schedule_algo_refresh(self) -> None:
        return None

    _terminal_watchdog = nc.NodeClient._terminal_watchdog
    _terminal_alive = nc.NodeClient._terminal_alive
    _terminal_process_running = nc.NodeClient._terminal_process_running
    _reconnect_terminal = nc.NodeClient._reconnect_terminal


async def test_task_does_not_finish_just_because_terminal_went_away(monkeypatch):
    """终端短暂不可用期间，tpl_1 不能把任务判成「没持仓」而收口。

    这条最容易出问题：终端重启期间读不到账本，如果被当成「空仓」，
    tpl_1 的「没持仓就结束」规则就会误触发，把还有持仓的任务收掉。
    """
    _fast_watchdog(monkeypatch)
    sent: list = []
    mt5 = MockMT5Client()
    mt5.connected = True
    mt5._ipc_ok = True

    hub = mh.MarketHub(mt5, _exec, interval=0.02, idle_interval=0.2, empty_confirm=2)

    async def send(payload: dict) -> None:
        sent.append(payload)

    runner = StrategyRunner(
        task_id=1, magic=MAGIC, group_id="g1", signal_id="s1",
        entry={"action": "BUY", "symbol": "XAUUSD", "volume": 0.1},
        strategy={"template_id": "tpl_1", "rules": [counter_rule()]},
        mt5=mt5, hub=hub, exec_fn=_exec, send_fn=send, report_interval=1.0,
    )

    hub.start()
    runner.start()
    try:
        assert await _wait_until(
            lambda: len(mt5.positions_by_magic(MAGIC)) == 1
        ), "首单没有成交"

        # 终端没了，读不到账本
        mt5.terminal_alive = False
        mt5.break_ipc()

        # 多跑几轮采样：hub 只能派发 STALE，绝不能派发 GONE
        await asyncio.sleep(0.5)
        assert not _finished(sent), "终端不可用期间任务被误收口了"
        assert not runner.done
    finally:
        runner.cancel()
        await hub.close()
