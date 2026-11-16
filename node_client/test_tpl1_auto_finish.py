"""模版1「AI智能加仓」（tpl_1）端到端：没持仓就结束任务并清理挂单。

这里走的是**生产链路**：真实 MarketHub 采样 + 真实 StrategyRunner 订阅，
不手工投事件。前面单元测试用 FakeHub 手工投事件验证判定逻辑，
这里验证的是整条链路接起来之后行为确实成立：

- 正常运行中持仓被平掉 → 任务收口，盘上残留挂单被撤掉；
- 重连恢复时持仓早已被平掉 → 任务收口，残留挂单被撤掉；
- 其它模版（网格、以损定量）不受这条规则影响。
"""
import asyncio

import market_hub as mh
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


def grid_rule(**over) -> dict:
    rule = {
        "type": 4, "status": 1, "action": "all",
        "price_lower": 2300.0, "price_upper": 2360.0,
        "grid_count": 10, "grid_mode": "arithmetic",
        "grid_side": "long", "lot_per_grid": 0.01,
        "total_lot_limit": 0.0, "trigger_price": 0.0,
        "stop_lower": 0.0, "stop_upper": 0.0,
        "close_on_stop": True, "prefill_enabled": True,
    }
    rule.update(over)
    return rule


def risk_rule(**over) -> dict:
    rule = {
        "type": 3, "status": 1, "action": "all",
        "risk_amount": 300.0, "rr_ratio": 2.5, "base_ratio": 30.0,
        "add_batches": 2, "max_total_lot": 0.0,
        "breakeven_enabled": False, "breakeven_times": 1.0, "breakeven_mode": "once",
    }
    rule.update(over)
    return rule


async def _exec(fn, *args):
    return fn(*args)


async def _wait_until(predicate, timeout: float = 5.0, step: float = 0.02) -> bool:
    """轮询等待条件成立；超时返回 False。"""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(step)
    return predicate()


def _finished(sent: list) -> list[dict]:
    return [m["data"] for m in sent if m["type"] == "strategy_finished"]


def _build(strategy: dict, mt5: MockMT5Client, sent: list, entry: dict | None = None):
    hub = mh.MarketHub(mt5, _exec, interval=0.02, idle_interval=0.2, empty_confirm=2)

    async def send(payload: dict) -> None:
        sent.append(payload)

    runner = StrategyRunner(
        task_id=1, magic=MAGIC, group_id="g1", signal_id="s1",
        entry=entry or {"action": "BUY", "symbol": "XAUUSD", "volume": 0.1},
        strategy=strategy,
        mt5=mt5, hub=hub, exec_fn=_exec, send_fn=send, report_interval=1.0,
    )
    return runner, hub


async def test_tpl1_finishes_and_cleans_orders_when_position_closed():
    """持仓被止损打掉 → 任务收口，逆势限价挂单被撤掉。"""
    sent: list = []
    mt5 = MockMT5Client()
    runner, hub = _build(
        {"template_id": "tpl_1", "rules": [counter_rule()]}, mt5, sent,
    )
    hub.start()
    runner.start()
    try:
        # 等首单成交并挂出逆势限价
        assert await _wait_until(
            lambda: len(mt5.pending_orders_by_magic(MAGIC)) == 1
        ), "逆势限价没有挂出来"

        # 终端止损打掉持仓
        mt5.clear_by_magic_with_reason(MAGIC, reason=4)

        assert await _wait_until(lambda: bool(_finished(sent))), "任务没有收口"
        finished = _finished(sent)
        assert finished[0]["status"] == "done"
        # 残留挂单必须撤干净：留在盘上就是没人管的孤儿单
        assert mt5.pending_orders_by_magic(MAGIC) == []
    finally:
        await hub.close()


async def test_tpl1_resume_without_position_finishes_and_cleans_orders():
    """重连恢复时持仓早已被平掉 → 任务收口，残留挂单被撤掉。

    这是本规则最主要的价值：hub 只有在见过持仓之后才会派发 GONE，
    持仓在节点离线期间被平掉的话它永远观察不到，任务会一直挂着，
    挂单也会一直留在盘上变成孤儿单。
    """
    sent: list = []
    mt5 = MockMT5Client()
    # 节点离线期间挂出的逆势限价，重连后还在盘上
    mt5.place_pending_order(
        "XAUUSD", "BUY", 0.1, 100.0, comment="R1D100", magic=MAGIC,
    )
    assert len(mt5.pending_orders_by_magic(MAGIC)) == 1

    runner, hub = _build(
        {"template_id": "tpl_1", "rules": [counter_rule()]}, mt5, sent,
    )
    hub.start()
    runner.start(resume=True)
    try:
        assert await _wait_until(lambda: bool(_finished(sent))), "任务没有收口"
        assert _finished(sent)[0]["status"] == "done"
        assert mt5.pending_orders_by_magic(MAGIC) == []
    finally:
        await hub.close()


async def test_tpl1_keeps_running_while_position_exists():
    """有持仓时绝不能收口：这条规则只认「持仓归零」。"""
    sent: list = []
    mt5 = MockMT5Client()
    runner, hub = _build(
        {"template_id": "tpl_1", "rules": [counter_rule()]}, mt5, sent,
    )
    hub.start()
    runner.start()
    try:
        assert await _wait_until(
            lambda: len(mt5.positions_by_magic(MAGIC)) == 1
        ), "首单没有成交"
        # 多跑几轮采样，确认任务还活着
        await asyncio.sleep(0.5)
        assert not _finished(sent)
        assert not runner.done
    finally:
        runner.cancel()
        await hub.close()


async def test_tpl2_limit_entry_waits_for_fill():
    """以损定量（tpl_2）的限价开仓：成交前本来就没持仓，不能收口。"""
    sent: list = []
    mt5 = MockMT5Client()
    mt5.prices_map["XAUUSD"] = 2330.0
    runner, hub = _build(
        {
            "template_id": "tpl_2",
            "rules": [risk_rule(entry_mode="limit")],
        },
        mt5, sent,
        entry={
            "action": "BUY", "symbol": "XAUUSD", "volume": 0.1,
            "entry_price": 2330.0, "stop_loss": 2320.0,
        },
    )
    hub.start()
    runner.start()
    try:
        await asyncio.sleep(0.6)
        # 挂单在途、还没成交：不能收口
        assert not _finished(sent)
        assert not runner.done
    finally:
        runner.cancel()
        await hub.close()


async def test_grid_tpl3_holds_when_empty():
    """网格（tpl_3）空仓是正常运行态，不能被模版1 的规则带走。"""
    sent: list = []
    mt5 = MockMT5Client()
    mt5.prices_map["XAUUSD"] = 2330.0
    runner, hub = _build(
        {"template_id": "tpl_3", "rules": [grid_rule()]}, mt5, sent,
    )
    hub.start()
    runner.start()
    try:
        await asyncio.sleep(0.6)
        assert not _finished(sent)
        assert not runner.done
    finally:
        runner.cancel()
        await hub.close()
