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
    """事件源替身：只实现 subscribe / unsubscribe / bar_metric 契约。"""

    def __init__(self) -> None:
        self.sub: mh.Subscription | None = None
        self.unsubscribed = False
        self.metrics: dict[tuple[str, str], float] = {}
        self.metric_calls: list[tuple[str, str, str]] = []

    def subscribe(self, *, symbol, magic, direction) -> mh.Subscription:
        self.sub = mh.Subscription(symbol=symbol, magic=magic, direction=direction)
        return self.sub

    def unsubscribe(self, sub) -> None:
        self.unsubscribed = True
        sub.close()

    async def bar_metric(self, symbol: str, timeframe: str, metric: str) -> float:
        self.metric_calls.append((symbol, timeframe, metric))
        return self.metrics.get((timeframe, metric), 0.0)


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


async def test_add_reports_reason_detail_and_compact_mt5_comment():
    """加仓单要能还原开单原因：上报带说明与逐项计算依据，MT5 备注带紧凑编码。"""
    sent: list = []
    mt5 = MockMT5Client()
    runner, hub = _runner(sent, mt5=mt5)
    runner.start()
    await _settle()

    held = mt5.positions_by_magic(MAGIC)
    hub.sub.offer(mh.MarketEvent(
        kind=mh.TICK, symbol="XAUUSD", magic=MAGIC,
        positions=tuple(held), price=held[0]["price_open"] - 1.0, point=0.01,
    ))
    await _settle()

    add = _progress(sent, "add_counter")[0]
    assert "逆势加仓" in add["message"]
    assert "偏离 100 点" in add["message"] and "阈值 100 点" in add["message"]
    detail = add["detail"]
    assert detail["kind"] == "add"
    assert detail["threshold"] == 100.0 and detail["deviation"] == 100.0
    assert detail["volume_formula"] == "0.1 × 1 + 0 = 0.1"

    added = mt5.positions_by_magic(MAGIC)[-1]
    assert added["comment"] == "R1D100"
    runner.cancel()


async def test_atr_level_prefetches_metric_and_adds():
    """ATR 档位：判定前先取到行情指标，再按折算出的点数触发加仓。"""
    sent: list = []
    mt5 = MockMT5Client()
    strategy = {"rules": [counter_rule(
        batch_enabled=True, batch_count=1, total_lot_limit=10,
        batch_levels=[{
            "pos_from": 2, "pos_to": 10, "calc_type": "atr", "timeframe": "M5",
            "lot_times": 1.0, "extra_lot": 0.0,
        }],
    )]}
    runner, hub = _runner(sent, mt5=mt5, strategy=strategy)
    hub.metrics[("M5", "atr")] = 1.0          # 1.0 / 0.01 = 100 点
    runner.start()
    await _settle()

    held = mt5.positions_by_magic(MAGIC)
    hub.sub.offer(mh.MarketEvent(
        kind=mh.TICK, symbol="XAUUSD", magic=MAGIC,
        positions=tuple(held), price=held[0]["price_open"] - 1.0, point=0.01,
    ))
    await _settle()

    assert hub.metric_calls == [("XAUUSD", "M5", "atr")]
    assert runner.add_count == 1
    add = _progress(sent, "add_counter")[0]
    assert "ATR(M5,14)" in add["message"]
    assert add["detail"]["calc_type"] == "atr"
    assert mt5.positions_by_magic(MAGIC)[-1]["comment"] == "R1L0A100"
    runner.cancel()


async def test_point_levels_never_read_bars():
    """全是点数档位时不该触碰 K 线接口。"""
    sent: list = []
    mt5 = MockMT5Client()
    runner, hub = _runner(sent, mt5=mt5)
    runner.start()
    await _settle()

    held = mt5.positions_by_magic(MAGIC)
    hub.sub.offer(mh.MarketEvent(
        kind=mh.TICK, symbol="XAUUSD", magic=MAGIC,
        positions=tuple(held), price=held[0]["price_open"] - 1.0, point=0.01,
    ))
    await _settle()

    assert hub.metric_calls == []
    runner.cancel()


async def test_first_order_reports_open_reason_and_detail():
    sent: list = []
    strategy = {
        "strategy_id": "sty_1", "name": "黄金逆势", "template_id": "t1",
        "rules": [counter_rule(), counter_rule(status=0)],
    }
    runner, hub = _runner(sent, strategy=strategy)
    runner.start()
    await _settle()

    opened = _progress(sent, "open")[0]
    assert "策略信号首单：BUY XAUUSD 0.1 手" in opened["message"]
    assert "黄金逆势" in opened["message"]
    detail = opened["detail"]
    assert detail["kind"] == "open"
    assert detail["strategy_name"] == "黄金逆势"
    assert (detail["rule_count"], detail["enabled_rule_count"]) == (2, 1)
    runner.cancel()


async def test_first_order_keeps_signal_comment_in_mt5():
    """信号自带备注要原样进 MT5，不能被任务编码覆盖。"""
    sent: list = []
    mt5 = MockMT5Client()
    runner, _ = _runner(sent, mt5=mt5)
    runner.entry["comment"] = "手动开仓"
    runner.start()
    await _settle()

    assert mt5.positions_by_magic(MAGIC)[0]["comment"] == "手动开仓"
    runner.cancel()


async def test_first_order_falls_back_to_task_comment():
    sent: list = []
    mt5 = MockMT5Client()
    runner, _ = _runner(sent, mt5=mt5)
    runner.start()
    await _settle()

    assert mt5.positions_by_magic(MAGIC)[0]["comment"] == "S1"
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
