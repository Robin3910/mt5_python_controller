"""策略执行器单测：事件驱动下的首单、加仓、收口与恢复。

用真实 Subscription 配一个只提供订阅契约的事件源替身，事件由测试直接投递，
既覆盖事件合并逻辑，又不必等采样定时器。
"""
import asyncio

import market_hub as mh
from mock_mt5 import MockMT5Client
from strategy_runner import StrategyRunner

MAGIC = 900000001


class FakeHub:
    """事件源替身：只实现 subscribe / unsubscribe 契约。"""

    def __init__(self) -> None:
        self.sub: mh.Subscription | None = None
        self.unsubscribed = False

    def subscribe(self, *, symbol, magic, direction) -> mh.Subscription:
        self.sub = mh.Subscription(symbol=symbol, magic=magic, direction=direction)
        return self.sub

    def unsubscribe(self, sub) -> None:
        self.unsubscribed = True
        sub.close()


def counter_rule(**over) -> dict:
    rule = {
        "type": 1, "status": 1, "action": "all",
        "point": 100.0, "lot_times": 1.0, "extra_lot": 0.0,
        "max_allow_num": 3, "batch_enabled": False, "batch_levels": [],
    }
    rule.update(over)
    return rule


def _runner(sent: list, *, mt5=None, strategy=None) -> tuple[StrategyRunner, FakeHub]:
    hub = FakeHub()

    async def send(payload: dict) -> None:
        sent.append(payload)

    async def _exec(fn, *args):
        return fn(*args)

    runner = StrategyRunner(
        task_id=1, magic=MAGIC, group_id="g1", signal_id="s1",
        entry={"action": "BUY", "symbol": "XAUUSD", "volume": 0.1},
        strategy=strategy or {"rules": [counter_rule()]},
        mt5=mt5 or MockMT5Client(),
        hub=hub, exec_fn=_exec, send_fn=send, report_interval=1.0,
    )
    return runner, hub


async def _settle(times: int = 5) -> None:
    """把控制权让给 runner 协程，直到它重新等在事件上。"""
    for _ in range(times):
        await asyncio.sleep(0)


def _of_type(sent: list, mtype: str) -> list[dict]:
    return [m["data"] for m in sent if m["type"] == mtype]


def _progress(sent: list, event: str) -> list[dict]:
    return [d for d in _of_type(sent, "strategy_progress") if d["event"] == event]


async def test_first_order_reports_trade_result_and_open():
    sent: list = []
    runner, hub = _runner(sent)
    runner.start()
    await _settle()

    trade = _of_type(sent, "trade_result")
    assert trade and trade[0]["success"] is True
    assert trade[0]["magic"] == MAGIC
    assert trade[0]["task_id"] == 1

    opened = _progress(sent, "open")
    assert opened and opened[0]["phase"] == "opened"
    assert opened[0]["total_orders"] == 1
    assert hub.sub is not None
    runner.cancel()


async def test_first_order_failure_ends_without_finished():
    class FailingMT5(MockMT5Client):
        def place_market_order(self, *a, **kw):
            return {"success": False, "error": "no money"}

    sent: list = []
    runner, hub = _runner(sent, mt5=FailingMT5())
    runner.start()
    await _settle()

    trade = _of_type(sent, "trade_result")
    assert trade and trade[0]["success"] is False
    assert not _of_type(sent, "strategy_finished")  # 首单失败靠 trade_result 收口
    assert runner.done
    assert hub.unsubscribed is True


async def test_gone_event_finishes_task():
    sent: list = []
    runner, hub = _runner(sent)
    runner.start()
    await _settle()

    hub.sub.offer(mh.MarketEvent(kind=mh.GONE, symbol="XAUUSD", magic=MAGIC))
    await _settle()

    finished = _of_type(sent, "strategy_finished")
    assert finished and finished[0]["status"] == "done"
    assert finished[0]["reason"] == "positions_cleared"
    assert runner.done
    assert hub.unsubscribed is True


async def test_stale_event_does_not_finish_task():
    """读不到持仓时绝不能判定任务完成。"""
    sent: list = []
    runner, hub = _runner(sent)
    runner.start()
    await _settle()

    hub.sub.offer(mh.MarketEvent(kind=mh.STALE, symbol="XAUUSD", magic=MAGIC))
    await _settle()

    assert not _of_type(sent, "strategy_finished")
    assert not runner.done
    runner.cancel()


async def test_tick_beyond_threshold_adds_position():
    sent: list = []
    mt5 = MockMT5Client()
    runner, hub = _runner(sent, mt5=mt5)
    runner.start()
    await _settle()

    held = mt5.positions_by_magic(MAGIC)
    assert len(held) == 1
    # 逆势：多单朝不利方向走 100 点（point=0.01 -> 价格跌 1.0）
    hub.sub.offer(mh.MarketEvent(
        kind=mh.TICK, symbol="XAUUSD", magic=MAGIC,
        positions=tuple(held), price=held[0]["price_open"] - 1.0, point=0.01,
    ))
    await _settle()

    assert runner.add_count == 1
    assert len(mt5.positions_by_magic(MAGIC)) == 2
    assert _progress(sent, "add_counter")
    runner.cancel()


async def test_tick_within_threshold_does_not_add():
    sent: list = []
    mt5 = MockMT5Client()
    runner, hub = _runner(sent, mt5=mt5)
    runner.start()
    await _settle()

    held = mt5.positions_by_magic(MAGIC)
    hub.sub.offer(mh.MarketEvent(
        kind=mh.TICK, symbol="XAUUSD", magic=MAGIC,
        positions=tuple(held), price=held[0]["price_open"] - 0.2, point=0.01,
    ))
    await _settle()

    assert runner.add_count == 0
    assert len(mt5.positions_by_magic(MAGIC)) == 1
    runner.cancel()


async def test_tick_without_decision_reports_heartbeat():
    sent: list = []
    mt5 = MockMT5Client()
    runner, hub = _runner(sent, mt5=mt5)
    runner.start()
    await _settle()
    sent.clear()
    runner._last_report = 0.0  # 上报间隔下限是 1 秒，这里直接越过节流

    held = mt5.positions_by_magic(MAGIC)
    hub.sub.offer(mh.MarketEvent(
        kind=mh.TICK, symbol="XAUUSD", magic=MAGIC,
        positions=tuple(held), price=held[0]["price_open"], point=0.01,
    ))
    await _settle()

    beats = _progress(sent, "heartbeat")
    assert beats and beats[0]["position_count"] == 1
    runner.cancel()


async def test_resume_skips_first_order_and_seeds_counters():
    sent: list = []
    mt5 = MockMT5Client()
    for _ in range(3):  # 断线前已有首单 + 2 次加仓
        mt5.place_market_order("XAUUSD", "BUY", 0.1, magic=MAGIC)

    runner, hub = _runner(sent, mt5=mt5)
    runner.start(resume=True)
    await _settle()
    assert not _of_type(sent, "trade_result")
    assert _progress(sent, "resume")

    held = mt5.positions_by_magic(MAGIC)
    hub.sub.offer(mh.MarketEvent(
        kind=mh.TICK, symbol="XAUUSD", magic=MAGIC,
        positions=tuple(held), price=held[0]["price_open"], point=0.01,
    ))
    await _settle()

    assert runner.add_count == 2  # 3 笔持仓 = 首单 + 2 次加仓
    assert runner.total_orders == 3
    assert runner.total_volume == 0.3
    runner.cancel()


async def test_resumed_runner_respects_max_allow_num():
    """恢复后计数已重建，达到次数上限就不再加仓。"""
    sent: list = []
    mt5 = MockMT5Client()
    for _ in range(4):  # 首单 + 3 次加仓，已达 max_allow_num=3
        mt5.place_market_order("XAUUSD", "BUY", 0.1, magic=MAGIC)

    runner, hub = _runner(sent, mt5=mt5)
    runner.start(resume=True)
    await _settle()

    held = mt5.positions_by_magic(MAGIC)
    hub.sub.offer(mh.MarketEvent(
        kind=mh.TICK, symbol="XAUUSD", magic=MAGIC,
        positions=tuple(held), price=held[0]["price_open"] - 5.0, point=0.01,
    ))
    await _settle()

    assert runner.add_count == 3
    assert len(mt5.positions_by_magic(MAGIC)) == 4
    runner.cancel()


async def test_stop_request_closes_positions_and_finishes():
    sent: list = []
    mt5 = MockMT5Client()
    runner, hub = _runner(sent, mt5=mt5)
    runner.start()
    await _settle()
    assert len(mt5.positions_by_magic(MAGIC)) == 1

    runner.request_stop("close_signal")
    await _settle()

    assert mt5.positions_by_magic(MAGIC) == []
    finished = _of_type(sent, "strategy_finished")
    assert finished and finished[0]["status"] == "done"
    assert finished[0]["reason"] == "close_signal"
    assert runner.done


async def test_add_failure_reports_error_and_keeps_running():
    class HalfFailingMT5(MockMT5Client):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def place_market_order(self, *a, **kw):
            self.calls += 1
            if self.calls == 1:
                return super().place_market_order(*a, **kw)
            return {"success": False, "error": "requote"}

    sent: list = []
    mt5 = HalfFailingMT5()
    runner, hub = _runner(sent, mt5=mt5)
    runner.start()
    await _settle()

    held = mt5.positions_by_magic(MAGIC)
    hub.sub.offer(mh.MarketEvent(
        kind=mh.TICK, symbol="XAUUSD", magic=MAGIC,
        positions=tuple(held), price=held[0]["price_open"] - 1.0, point=0.01,
    ))
    await _settle()

    errors = _progress(sent, "error")
    assert errors and "requote" in errors[0]["message"]
    assert runner.add_count == 0
    assert not runner.done
    runner.cancel()


async def test_cancel_does_not_report_finished():
    """断线只取消本地循环，持仓还在，不能上报结束。"""
    sent: list = []
    runner, hub = _runner(sent)
    runner.start()
    await _settle()

    runner.cancel()
    await _settle()

    assert not _of_type(sent, "strategy_finished")
    assert runner.done
    assert hub.unsubscribed is True


async def test_subscription_close_exits_monitor_quietly():
    sent: list = []
    runner, hub = _runner(sent)
    runner.start()
    await _settle()

    hub.sub.close()
    await _settle()

    assert not _of_type(sent, "strategy_finished")
    assert runner.done
