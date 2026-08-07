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

    def subscribe(self, *, symbol, magic, direction, hold_when_empty=False) -> mh.Subscription:
        self.sub = mh.Subscription(
            symbol=symbol, magic=magic, direction=direction,
            hold_when_empty=bool(hold_when_empty),
        )
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
    # 无出场成交历史时仍退回默认码
    assert finished[0]["reason"] == "positions_cleared"
    assert runner.done


async def test_gone_event_reports_stop_loss_reason_from_deals():
    """被动全平：从成交历史识别止损打掉，不再只报 positions_cleared。"""
    sent: list = []
    mt5 = MockMT5Client()
    runner, hub = _runner(sent, mt5=mt5)
    runner.start()
    await _settle()

    # 终端止损打掉：持仓清空并留下 DEAL_REASON_SL 出场记录
    mt5.clear_by_magic_with_reason(MAGIC, reason=4)
    hub.sub.offer(mh.MarketEvent(kind=mh.GONE, symbol="XAUUSD", magic=MAGIC))
    await _settle()

    finished = _of_type(sent, "strategy_finished")
    assert finished and finished[0]["status"] == "done"
    assert finished[0]["reason"].startswith("止损打掉")
    assert finished[0]["detail"]["kind"] == "close_reason"
    assert finished[0]["detail"]["sl"] >= 1
    assert runner.done


async def test_gone_keeps_explicit_stop_reason():
    """主动 stop 已写过原因时，不被成交历史覆盖。"""
    sent: list = []
    mt5 = MockMT5Client()
    runner, hub = _runner(sent, mt5=mt5)
    runner.start()
    await _settle()

    runner.request_stop("账户风控·净值下限：净值 900 < 1000；动作 全部平仓")
    mt5.clear_by_magic_with_reason(MAGIC, reason=4)
    hub.sub.offer(mh.MarketEvent(kind=mh.GONE, symbol="XAUUSD", magic=MAGIC))
    await _settle(10)

    finished = _of_type(sent, "strategy_finished")
    assert finished
    assert "账户风控" in finished[0]["reason"]
    assert "止损打掉" not in finished[0]["reason"]
    runner.cancel()
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
    mt5.positions_by_magic(MAGIC)[0]  # ensure readable
    # 给持仓打上浮盈，收口时应作为已实现盈亏上报（不能再写死 0）
    for p in mt5._positions:
        if int(p.get("magic") or 0) == MAGIC:
            p["profit"] = 12.5

    runner.request_stop("close_signal")
    await _settle()

    assert mt5.positions_by_magic(MAGIC) == []
    finished = _of_type(sent, "strategy_finished")
    assert finished and finished[0]["status"] == "done"
    assert finished[0]["reason"] == "close_signal"
    assert finished[0]["realized_profit"] == 12.5
    assert runner.done


async def test_stop_request_forwards_account_risk_detail():
    """账户风控停策略时，结束原因与触发参数要一并上报，供后台展示。"""
    sent: list = []
    mt5 = MockMT5Client()
    runner, _hub = _runner(sent, mt5=mt5)
    runner.start()
    await _settle()

    detail = {
        "kind": "account_risk",
        "rule": "lot_pl_tiers",
        "rule_label": "手数分档盈亏",
        "min_lot": 0.5,
        "pl_amount": 20,
        "current_lot": 0.6,
        "current_pl": 26.4,
    }
    reason = "账户风控·手数分档盈亏：分档#2 总手数 0.6>=0.5 且盈亏 26.40 达 20；动作 全部平仓"
    runner.request_stop(reason, detail=detail)
    await _settle()

    finished = _of_type(sent, "strategy_finished")
    assert finished and finished[0]["reason"] == reason
    assert finished[0]["detail"] == detail


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


# ---------------------------------------------------------------------------
# 以损定量趋势单（策略模版2）
#
# mock 的黄金规格：point=tick_size=0.01、tick_value=1.0，即一手一美元价差值 100。
# 测试统一把中间价钉在 2400、止损放 2397；mock 的点差是 0.12，于是 bid=2399.94、
# ask=2400.06，多单按触发侧 bid 算止损距离 2.94（一手亏 294）-> 总手数 1.02。
# mock 的市价单固定按中间价 2400 成交，底仓成交后阶梯会重锚到 2400（距离 2.88）。
# ---------------------------------------------------------------------------

ENTRY_PRICE = 2400.0
STOP_LOSS = 2397.0
TOTAL_LOT = 1.02
# 重锚到成交价 2400 后：止损距 2.88，满档 = 2400 + 2.88 × 2.5，两单阶梯等分
LADDER = [2403.6, 2407.2]


def risk_rule(**over) -> dict:
    rule = {
        "type": 3, "status": 1, "action": "all",
        "risk_amount": 300.0, "rr_ratio": 2.5, "base_ratio": 30.0,
        "add_batches": 2, "max_total_lot": 0.0,
        "breakeven_enabled": False, "breakeven_times": 1.0, "breakeven_mode": "once",
    }
    rule.update(over)
    return rule


def _risk_runner(sent: list, *, mt5=None, stop_loss=STOP_LOSS,
                 **rule_over) -> tuple[StrategyRunner, FakeHub, MockMT5Client]:
    client = mt5 or MockMT5Client()
    client.prices_map["XAUUSD"] = ENTRY_PRICE
    hub = FakeHub()

    async def send(payload: dict) -> None:
        sent.append(payload)

    async def _exec(fn, *args):
        return fn(*args)

    runner = StrategyRunner(
        task_id=1, magic=MAGIC, group_id="g1", signal_id="s1",
        # volume 故意给一个不该被采用的值：模版2 的手数只由风险金额决定
        entry={"action": "BUY", "symbol": "XAUUSD", "volume": 0.1,
               "stop_loss": stop_loss},
        strategy={"name": "BTC以损定量", "template_id": "tpl_2",
                  "rules": [risk_rule(**rule_over)]},
        mt5=client, hub=hub, exec_fn=_exec, send_fn=send, report_interval=1.0,
    )
    return runner, hub, client


def _tick(hub: FakeHub, positions, price: float) -> None:
    hub.sub.offer(mh.MarketEvent(
        kind=mh.TICK, symbol="XAUUSD", magic=MAGIC,
        positions=tuple(positions), price=price, point=0.01,
    ))


async def test_risk_sized_base_order_ignores_signal_volume():
    """底仓手数来自「风险金额 ÷ 每手止损亏损」，与信号 volume 无关。"""
    sent: list = []
    runner, hub, mt5 = _risk_runner(sent)
    runner.start()
    await _settle()

    held = mt5.positions_by_magic(MAGIC)
    # 总手数 1.02，底仓 30%；开仓时底仓 + 2 分散仓一次打齐
    assert len(held) == 3
    assert held[0]["volume"] == 0.3
    assert held[0]["volume"] != 0.1
    assert runner.risk_sized is True
    runner.cancel()


async def test_risk_sized_base_has_no_tp_distribute_climbs_the_ladder():
    """底仓 TP=0；分散仓共用信号止损，按等分阶梯逐档挂止盈。"""
    sent: list = []
    runner, hub, mt5 = _risk_runner(sent)
    runner.start()
    await _settle()

    held = mt5.positions_by_magic(MAGIC)
    assert held[0]["sl"] == STOP_LOSS
    assert not held[0].get("tp")
    assert [p["sl"] for p in held[1:]] == [STOP_LOSS, STOP_LOSS]
    assert [p["tp"] for p in held[1:]] == LADDER
    runner.cancel()


async def test_risk_sized_distribute_lots_are_equal():
    """分散仓严格等手数，余量不并到末笔。"""
    sent: list = []
    runner, hub, mt5 = _risk_runner(sent, add_batches=4)
    runner.start()
    await _settle()

    volumes = [p["volume"] for p in mt5.positions_by_magic(MAGIC)[1:]]
    assert len(volumes) == 4
    assert len(set(volumes)) == 1
    runner.cancel()


async def test_risk_sized_open_reports_lot_formula():
    sent: list = []
    runner, hub, mt5 = _risk_runner(sent)
    runner.start()
    await _settle()

    opened = _progress(sent, "open")[0]
    assert "以损定量" in opened["message"]
    assert "底仓 30%" in opened["message"]
    detail = opened["detail"]
    assert detail["kind"] == "open"
    assert detail["total_lot"] == TOTAL_LOT
    assert detail["risk_amount"] == 300.0
    assert detail["risk_used"] <= 300.0
    assert detail["stop_loss"] == STOP_LOSS
    assert detail["tp_step"] == 3.6
    assert detail["order_count"] == 3
    assert len(detail["batches"]) == 3      # 底仓 + 2 分散
    assert runner.add_count == 2
    runner.cancel()


async def test_risk_sized_without_stop_loss_fails_fast():
    """算不出手数就不该下单，直接收口让服务端看到失败原因。"""
    sent: list = []
    runner, hub, mt5 = _risk_runner(sent, stop_loss=0.0)
    runner.start()
    await _settle()

    assert mt5.positions_by_magic(MAGIC) == []
    assert not _of_type(sent, "trade_result")
    errors = _progress(sent, "error")
    assert errors and "止损价" in errors[0]["message"]
    finished = _of_type(sent, "strategy_finished")
    assert finished and finished[0]["status"] == "failed"
    assert runner.done


async def test_risk_sized_stop_on_wrong_side_fails_fast():
    sent: list = []
    runner, hub, mt5 = _risk_runner(sent, stop_loss=2403.0)
    runner.start()
    await _settle()

    errors = _progress(sent, "error")
    assert errors and "不利方向" in errors[0]["message"]
    assert runner.done


async def test_risk_sized_opens_all_distribute_immediately():
    """分散仓不再等回撤/突破，开仓时与底仓一并市价打出。"""
    sent: list = []
    runner, hub, mt5 = _risk_runner(sent)
    runner.start()
    await _settle()

    held = mt5.positions_by_magic(MAGIC)
    assert len(held) == 3
    assert round(sum(p["volume"] for p in held), 2) == TOTAL_LOT
    assert runner.add_count == 2
    distribute = _progress(sent, "add_trend")
    assert len(distribute) == 2
    assert all("分散仓" in e["message"] for e in distribute)
    runner.cancel()


async def test_risk_sized_distribute_shares_the_same_stop():
    """分散仓沿用同一止损，总风险因此保持不变。"""
    sent: list = []
    runner, hub, mt5 = _risk_runner(sent)
    runner.start()
    await _settle()

    for pos, tp in zip(mt5.positions_by_magic(MAGIC)[1:], LADDER):
        assert pos["sl"] == STOP_LOSS
        assert pos["tp"] == tp
        assert pos["comment"].startswith("R3B")
    runner.cancel()


async def test_risk_sized_no_distribute_puts_everything_in_base():
    sent: list = []
    runner, hub, mt5 = _risk_runner(sent, add_batches=0)
    runner.start()
    await _settle()

    held = mt5.positions_by_magic(MAGIC)
    assert len(held) == 1
    assert held[0]["volume"] == TOTAL_LOT
    assert not held[0].get("tp")
    runner.cancel()


async def test_risk_sized_breakeven_moves_stop_to_average_price():
    sent: list = []
    runner, hub, mt5 = _risk_runner(sent, breakeven_enabled=True, breakeven_times=1.0)
    runner.start()
    await _settle()

    held = mt5.positions_by_magic(MAGIC)
    _tick(hub, held, 2403.0)      # 浮盈 300 点 = 止损距离 × 1
    await _settle()

    moved = _progress(sent, "breakeven")
    assert moved and "保本触发" in moved[0]["message"]
    assert mt5.positions_by_magic(MAGIC)[0]["sl"] == ENTRY_PRICE
    # 保本改单不能顺手抹掉已挂的止盈（分散仓）
    assert mt5.positions_by_magic(MAGIC)[1]["tp"] == LADDER[0]
    runner.cancel()


async def test_risk_sized_breakeven_only_triggers_once():
    sent: list = []
    runner, hub, mt5 = _risk_runner(
        sent, breakeven_enabled=True, breakeven_times=1.0, breakeven_mode="once",
    )
    runner.start()
    await _settle()

    for _ in range(3):
        _tick(hub, mt5.positions_by_magic(MAGIC), 2404.0)
        await _settle()

    assert len(_progress(sent, "breakeven")) == 1
    runner.cancel()


async def test_risk_sized_breakeven_once_does_not_rearm_after_sl_reset():
    """按次：即便止损被拉回原位，也不再二次保本。"""
    sent: list = []
    runner, hub, mt5 = _risk_runner(
        sent, breakeven_enabled=True, breakeven_times=1.0, breakeven_mode="once",
    )
    runner.start()
    await _settle()

    held = mt5.positions_by_magic(MAGIC)
    _tick(hub, held, 2403.0)
    await _settle()
    assert len(_progress(sent, "breakeven")) == 1

    for pos in mt5.positions_by_magic(MAGIC):
        mt5.modify_position_sl(int(pos["ticket"]), STOP_LOSS)
    _tick(hub, mt5.positions_by_magic(MAGIC), 2403.0)
    await _settle()

    assert len(_progress(sent, "breakeven")) == 1
    runner.cancel()


async def test_risk_sized_breakeven_loop_can_rearm_after_sl_reset():
    """循环：止损被拉回后，浮盈再次达标会再移一次。"""
    sent: list = []
    runner, hub, mt5 = _risk_runner(
        sent, breakeven_enabled=True, breakeven_times=1.0, breakeven_mode="loop",
    )
    runner.start()
    await _settle()

    _tick(hub, mt5.positions_by_magic(MAGIC), 2403.0)
    await _settle()
    assert len(_progress(sent, "breakeven")) == 1

    for pos in mt5.positions_by_magic(MAGIC):
        mt5.modify_position_sl(int(pos["ticket"]), STOP_LOSS)
    _tick(hub, mt5.positions_by_magic(MAGIC), 2403.0)
    await _settle()

    assert len(_progress(sent, "breakeven")) == 2
    assert mt5.positions_by_magic(MAGIC)[0]["sl"] == ENTRY_PRICE
    runner.cancel()


async def test_risk_sized_breakeven_waits_for_threshold():
    sent: list = []
    runner, hub, mt5 = _risk_runner(sent, breakeven_enabled=True, breakeven_times=1.0)
    runner.start()
    await _settle()

    _tick(hub, mt5.positions_by_magic(MAGIC), 2402.0)   # 浮盈 200 点，未达 300
    await _settle()

    assert not _progress(sent, "breakeven")
    assert mt5.positions_by_magic(MAGIC)[0]["sl"] == STOP_LOSS
    runner.cancel()


async def test_risk_sized_breakeven_disabled_never_moves_stop():
    sent: list = []
    runner, hub, mt5 = _risk_runner(sent, breakeven_enabled=False)
    runner.start()
    await _settle()

    _tick(hub, mt5.positions_by_magic(MAGIC), 2450.0)
    await _settle()

    assert not _progress(sent, "breakeven")
    assert mt5.positions_by_magic(MAGIC)[0]["sl"] == STOP_LOSS
    runner.cancel()


async def test_risk_sized_resume_fills_remaining_distribute():
    """恢复后用持仓还原计划，立刻补开尚未打出的分散仓。"""
    sent: list = []
    mt5 = MockMT5Client()
    mt5.prices_map["XAUUSD"] = ENTRY_PRICE
    mt5.place_market_order("XAUUSD", "BUY", 0.29, STOP_LOSS, None, "S1", MAGIC)

    runner, hub, mt5 = _risk_runner(sent, mt5=mt5)
    runner.start(resume=True)
    await _settle()
    assert not _of_type(sent, "trade_result")

    _tick(hub, mt5.positions_by_magic(MAGIC), ENTRY_PRICE)
    await _settle()

    assert len(mt5.positions_by_magic(MAGIC)) == 3
    assert runner.add_count == 2
    runner.cancel()


async def test_risk_sized_resume_does_not_reopen_tpd_distribute():
    """中间档止盈离场后重连：按 R3B 备注认已开完，不得再开同档分散仓。"""
    sent: list = []
    runner, hub, mt5 = _risk_runner(sent)
    runner.start()
    await _settle()
    before = mt5.positions_by_magic(MAGIC)
    assert len(before) == 3
    runner.cancel()
    await _settle()

    # 平掉第一档分散仓（模拟阶梯 TP），只剩底仓 + R3B2
    first_dist = next(p for p in before if p.get("comment") == "R3B1")
    mt5.close_ticket(first_dist["ticket"])
    remaining = mt5.positions_by_magic(MAGIC)
    assert [p.get("comment") for p in remaining] == ["S1", "R3B2"]

    sent2: list = []
    resumed, hub2, _ = _risk_runner(sent2, mt5=mt5)
    resumed.start(resume=True)
    await _settle()
    _tick(hub2, remaining, ENTRY_PRICE)
    await _settle(10)

    after = mt5.positions_by_magic(MAGIC)
    assert [p.get("comment") for p in after] == ["S1", "R3B2"]
    assert sum(1 for p in after if p.get("comment") == "R3B2") == 1
    assert resumed.total_orders >= 3
    resumed.cancel()


async def test_risk_sized_stop_aborts_distribute_burst():
    """分散仓 burst 期间收到 stop：停手不再开后续档，再平掉已开仓。"""
    sent: list = []

    class StopAfterBaseMT5(MockMT5Client):
        def __init__(self) -> None:
            super().__init__()
            self.opens = 0
            self.runner: StrategyRunner | None = None

        def place_market_order(self, symbol, action, volume, sl=None, tp=None,
                               comment="", magic=None, max_retry=3) -> dict:
            res = super().place_market_order(
                symbol, action, volume, sl, tp, comment, magic, max_retry,
            )
            self.opens += 1
            # 底仓成交后立刻请求终止，后续分散仓应被跳过
            if self.opens == 1 and self.runner is not None:
                self.runner.request_stop("close_signal")
            return res

    mt5 = StopAfterBaseMT5()
    runner, hub, mt5 = _risk_runner(sent, mt5=mt5, add_batches=4)
    mt5.runner = runner
    runner.start()
    await _settle(20)

    # 不应把 1+4 全开完；stop 后应收口并清空该魔术号持仓
    assert mt5.opens == 1
    assert mt5.positions_by_magic(MAGIC) == []
    finished = _of_type(sent, "strategy_finished")
    assert finished and finished[0]["status"] == "done"
    assert finished[0]["reason"] == "close_signal"


async def test_risk_sized_resume_degrades_when_stop_already_moved():
    """止损已被保本挪走时无法还原计划，降级为只监控，不再补开也不再动止损。"""
    sent: list = []
    mt5 = MockMT5Client()
    mt5.prices_map["XAUUSD"] = ENTRY_PRICE
    # 止损已挪到开仓价（保本后的状态）
    mt5.place_market_order("XAUUSD", "BUY", 0.29, ENTRY_PRICE, None, "S1", MAGIC)

    runner, hub, mt5 = _risk_runner(sent, mt5=mt5, breakeven_enabled=True)
    runner.start(resume=True)
    await _settle()

    _tick(hub, mt5.positions_by_magic(MAGIC), 2399.5)
    await _settle()

    assert len(mt5.positions_by_magic(MAGIC)) == 1
    assert not _progress(sent, "breakeven")
    assert not runner.done          # 仍在监控，等持仓归零才收口
    runner.cancel()


async def test_risk_sized_distribute_failure_reports_error_and_keeps_running():
    class BaseOnlyMT5(MockMT5Client):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def place_market_order(self, *a, **kw):
            self.calls += 1
            if self.calls == 1:
                return super().place_market_order(*a, **kw)
            return {"success": False, "error": "requote"}

    sent: list = []
    runner, hub, mt5 = _risk_runner(sent, mt5=BaseOnlyMT5())
    runner.start()
    await _settle()

    errors = _progress(sent, "error")
    assert errors and "分散仓失败" in errors[0]["message"]
    assert runner.add_count == 0
    assert len(mt5.positions_by_magic(MAGIC)) == 1
    assert not runner.done
    runner.cancel()


async def test_risk_sized_never_evaluates_add_on_rules():
    """模版2 走独立执行路径，不该触碰加仓判定，也不该读 K 线。"""
    sent: list = []
    runner, hub, mt5 = _risk_runner(sent)
    runner.start()
    await _settle()

    _tick(hub, mt5.positions_by_magic(MAGIC), 2399.5)
    await _settle()

    assert hub.metric_calls == []
    assert not _progress(sent, "add_counter")
    runner.cancel()


async def test_add_on_strategy_is_not_treated_as_risk_sized():
    """模版1 的执行路径不受新分支影响。"""
    sent: list = []
    runner, _ = _runner(sent)
    assert runner.risk_sized is False


# ---------------------------------------------------------------------------
# 模版3：网格交易
# ---------------------------------------------------------------------------

GRID_LOWER = 2300.0
GRID_UPPER = 2400.0
GRID_PRICE = 2350.0


def grid_rule(**over) -> dict:
    rule = {
        "type": 4, "status": 1, "action": "all",
        "price_lower": GRID_LOWER, "price_upper": GRID_UPPER,
        "grid_count": 10, "grid_mode": "arithmetic",
        "grid_side": "long", "lot_per_grid": 0.01,
        "total_lot_limit": 0.0, "trigger_price": 0.0,
        "stop_lower": 0.0, "stop_upper": 0.0,
        "close_on_stop": True, "prefill_enabled": True,
    }
    rule.update(over)
    return rule


def _grid_runner(sent: list, *, mt5=None, **rule_over):
    client = mt5 or MockMT5Client()
    client.prices_map["XAUUSD"] = GRID_PRICE
    hub = FakeHub()

    async def send(payload: dict) -> None:
        sent.append(payload)

    async def _exec(fn, *args):
        return fn(*args)

    runner = StrategyRunner(
        task_id=1, magic=MAGIC, group_id="g1", signal_id="s1",
        entry={"action": "BUY", "symbol": "XAUUSD", "volume": 0.1},
        strategy={"name": "金网格", "template_id": "tpl_3",
                  "rules": [grid_rule(**rule_over)]},
        mt5=client, hub=hub, exec_fn=_exec, send_fn=send, report_interval=1.0,
    )
    return runner, hub, client


async def test_grid_mode_and_hold_when_empty_subscribe():
    sent: list = []
    runner, hub, _ = _grid_runner(sent)
    assert runner.is_grid is True
    assert runner.risk_sized is False
    runner.start()
    await _settle()
    assert hub.sub is not None
    assert hub.sub.hold_when_empty is True
    runner.cancel()


async def test_grid_prefill_opens_levels_above_price():
    """初始建仓：现价上方（买线已跌破）的格位先买入。"""
    sent: list = []
    runner, hub, mt5 = _grid_runner(sent)
    runner.start()
    await _settle()

    opened = _progress(sent, "open")
    assert opened
    assert opened[0]["detail"]["kind"] == "grid_plan"
    # 2350 在 2300~2400、10 格时，卖出线仍高于 2350 的格（5~9）应被预填
    assert opened[0]["detail"]["prefill_levels"] == [5, 6, 7, 8, 9]
    assert runner.total_orders > 0
    assert len(mt5.positions_by_magic(MAGIC)) == runner.total_orders
    # 预填的每一格退出线都在现价上方，不会开仓即锁定亏损
    plan = runner._grid_plan
    for level in plan.holdings:
        assert plan.levels[level + 1] > GRID_PRICE
    assert not runner.done
    runner.cancel()


async def test_grid_empty_positions_do_not_finish():
    """空仓不收口：投递空持仓事件后任务继续存活。"""
    sent: list = []
    runner, hub, mt5 = _grid_runner(sent, prefill_enabled=False)
    runner.start()
    await _settle()
    assert runner.total_orders == 0

    _tick(hub, [], GRID_PRICE)
    await _settle()
    assert not _of_type(sent, "strategy_finished")
    assert not runner.done
    runner.cancel()


async def test_grid_buy_on_down_cross_and_sell_on_up_cross():
    sent: list = []
    runner, hub, mt5 = _grid_runner(
        sent, prefill_enabled=False, grid_count=4,
        price_lower=100.0, price_upper=110.0,
    )
    mt5.prices_map["XAUUSD"] = 106.0
    runner.start()
    await _settle()

    # 106 → 104：下跌穿越 105 → 买入格位 2（levels: 100,102.5,105,107.5,110）
    _tick(hub, mt5.positions_by_magic(MAGIC), 104.0)
    await _settle()
    buys = _progress(sent, "grid_add")
    assert buys
    assert runner.total_orders >= 1
    held = mt5.positions_by_magic(MAGIC)
    assert held

    # 104 → 108：上涨穿越 107.5 → 卖出格位 2
    _tick(hub, held, 108.0)
    await _settle()
    closes = _progress(sent, "close_partial")
    assert closes
    runner.cancel()


async def test_grid_stop_lower_terminates_and_closes():
    sent: list = []
    runner, hub, mt5 = _grid_runner(
        sent, prefill_enabled=True, stop_lower=2290.0, close_on_stop=True,
    )
    runner.start()
    await _settle()
    assert runner.total_orders > 0

    _tick(hub, mt5.positions_by_magic(MAGIC), 2285.0)
    await _settle(10)

    finished = _of_type(sent, "strategy_finished")
    assert finished
    assert finished[0]["status"] == "done"
    assert mt5.positions_by_magic(MAGIC) == []


async def test_grid_breakout_without_trailing_keeps_range():
    """没开追踪时突破上限只是无格可卖，网格区间保持不变。"""
    sent: list = []
    runner, hub, mt5 = _grid_runner(
        sent, prefill_enabled=False, grid_count=4,
        price_lower=100.0, price_upper=110.0,
    )
    mt5.prices_map["XAUUSD"] = 106.0
    runner.start()
    await _settle()

    _tick(hub, mt5.positions_by_magic(MAGIC), 111.0)
    await _settle()

    assert not _progress(sent, "grid_shift")
    assert runner._grid_plan.levels[-1] == 110.0
    runner.cancel()


async def test_grid_trailing_up_shifts_range_on_breakout():
    """开了向上追踪：突破上限不停机，网格整体上移一格继续跑。"""
    sent: list = []
    runner, hub, mt5 = _grid_runner(
        sent, prefill_enabled=False, grid_count=4,
        price_lower=100.0, price_upper=110.0, trailing_up=True,
        stop_lower=95.0,
    )
    mt5.prices_map["XAUUSD"] = 106.0
    runner.start()
    await _settle()

    _tick(hub, mt5.positions_by_magic(MAGIC), 111.0)
    await _settle()

    shifts = _progress(sent, "grid_shift")
    assert shifts
    detail = shifts[0]["detail"]
    assert detail["kind"] == "grid_shift"
    assert detail["steps"] == 1
    assert detail["price_lower"] == 102.5
    assert detail["price_upper"] == 112.5
    assert detail["stop_lower"] == 97.5      # 止损同步上移
    assert not runner.done
    runner.cancel()


async def test_grid_gap_up_sells_before_trailing_shift():
    """突破外沿的那一根 tick 同时穿过卖出线：先兑现穿越，再平移。"""
    sent: list = []
    runner, hub, mt5 = _grid_runner(
        sent, prefill_enabled=True, grid_count=4,
        price_lower=100.0, price_upper=110.0, trailing_up=True,
    )
    mt5.prices_map["XAUUSD"] = 106.0
    runner.start()
    await _settle()
    # 预填卖出线仍高于 106 的格位：2（卖 107.5）、3（卖 110）
    assert set(runner._grid_plan.holdings) == {2, 3}

    _tick(hub, mt5.positions_by_magic(MAGIC), 111.0)
    await _settle(10)

    # 两格都在平移前按穿越卖掉了，没有被平移丢进 dropped
    assert len(_progress(sent, "close_partial")) == 2
    assert mt5.positions_by_magic(MAGIC) == []
    shifts = _progress(sent, "grid_shift")
    assert shifts and shifts[0]["detail"]["dropped_levels"] == []
    assert runner._grid_plan.levels[-1] == 112.5
    assert runner._grid_plan.holdings == {}
    runner.cancel()


async def test_grid_retries_failed_sell_on_next_event():
    """卖出失败的格位不会再被同一条线触发，必须逐轮重试兑现。"""
    sent: list = []
    runner, hub, mt5 = _grid_runner(
        sent, prefill_enabled=True, grid_count=4,
        price_lower=100.0, price_upper=110.0,
    )
    mt5.prices_map["XAUUSD"] = 106.0

    real_close = mt5.close_ticket
    calls = {"n": 0}

    def flaky_close(ticket):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"success": False, "error": "requote"}
        return real_close(ticket)

    mt5.close_ticket = flaky_close

    runner.start()
    await _settle()
    assert set(runner._grid_plan.holdings) == {2, 3}

    # 一次涨到 111：格 2 卖失败进重试队列，格 3 正常兑现
    _tick(hub, mt5.positions_by_magic(MAGIC), 111.0)
    await _settle(10)
    assert runner._grid_pending_sells == {2}
    assert len(mt5.positions_by_magic(MAGIC)) == 1

    # 价格没有再穿越 107.5，靠重试队列把这一格平掉
    _tick(hub, mt5.positions_by_magic(MAGIC), 111.5)
    await _settle(10)
    assert runner._grid_pending_sells == set()
    assert mt5.positions_by_magic(MAGIC) == []
    runner.cancel()


async def test_grid_trailing_retries_failed_dropped_close():
    """卖不掉的格位被平移挤出网格后不能脱管，后续事件要继续重试兑现。"""
    sent: list = []
    runner, hub, mt5 = _grid_runner(
        sent, prefill_enabled=True, grid_count=4,
        price_lower=100.0, price_upper=110.0, trailing_up=True,
    )
    mt5.prices_map["XAUUSD"] = 106.0

    real_close = mt5.close_ticket
    calls = {"n": 0}

    def flaky_close(ticket):
        calls["n"] += 1
        if calls["n"] <= 3:
            return {"success": False, "error": "requote"}
        return real_close(ticket)

    mt5.close_ticket = flaky_close

    runner.start()
    await _settle()
    assert set(runner._grid_plan.holdings) == {2, 3}

    # 直接跳到 118：两格卖出都失败，随后被平移（4 格）挤出网格
    _tick(hub, mt5.positions_by_magic(MAGIC), 118.0)
    await _settle(10)
    assert runner._grid_plan.shift_count == 4
    assert runner._grid_pending_sells == set()   # 已挤出网格，改由 orphans 负责
    assert len(runner._grid_orphans) == 1

    # 下一轮事件重试成功
    _tick(hub, mt5.positions_by_magic(MAGIC), 118.5)
    await _settle(10)
    assert runner._grid_orphans == set()
    assert mt5.positions_by_magic(MAGIC) == []
    runner.cancel()


async def test_grid_disabled_rule_fails_closed():
    """tpl_3 的网格规则被关掉时直接失败，不能回退成用信号手数开普通市价单。"""
    sent: list = []
    runner, _hub, mt5 = _grid_runner(sent, status=0)
    assert runner.is_grid is False
    runner.start()
    await _settle()

    assert mt5.positions_by_magic(MAGIC) == []
    assert not _of_type(sent, "trade_result")
    finished = _of_type(sent, "strategy_finished")
    assert finished and finished[0]["status"] == "failed"
    assert "strategy_rule_missing" in finished[0]["reason"]


async def test_grid_rejects_netting_account():
    """净持仓账户会把同品种仓位合并，逐格 ticket 的模型不成立，必须拒绝启动。"""
    sent: list = []
    client = MockMT5Client()
    client.margin_mode = "netting"
    runner, _hub, mt5 = _grid_runner(sent, mt5=client)
    runner.start()
    await _settle()

    assert mt5.positions_by_magic(MAGIC) == []
    finished = _of_type(sent, "strategy_finished")
    assert finished and finished[0]["status"] == "failed"
    assert "hedging" in finished[0]["reason"]


async def test_grid_close_on_stop_false_detaches_with_positions():
    """不清仓的配置只停止交易；持仓还在就不能报终态，否则占位被放掉没人管。"""
    sent: list = []
    runner, hub, mt5 = _grid_runner(
        sent, prefill_enabled=True, stop_lower=2290.0, close_on_stop=False,
    )
    runner.start()
    await _settle()
    held = mt5.positions_by_magic(MAGIC)
    assert held

    _tick(hub, held, 2285.0)
    await _settle(10)

    finished = _of_type(sent, "strategy_finished")
    assert finished and finished[0]["status"] == "detached"
    assert finished[0]["residual_positions"] == len(held)
    # 持仓原样保留，网格也不再交易
    assert len(mt5.positions_by_magic(MAGIC)) == len(held)
    assert not runner.done

    # 持仓被外部平光后才真正收口
    mt5.close_by_magic(MAGIC)
    _tick(hub, [], 2280.0)
    await _settle(10)
    assert [f for f in _of_type(sent, "strategy_finished") if f["status"] == "done"]


async def test_stop_with_residual_positions_reports_non_terminal():
    """平不干净时上报 stop_failed 并继续重试，不能当成已收口。"""
    sent: list = []
    runner, hub, mt5 = _grid_runner(sent, prefill_enabled=True)
    runner.start()
    await _settle()
    assert mt5.positions_by_magic(MAGIC)

    real_close_by_magic = mt5.close_by_magic
    mt5.close_by_magic = lambda magic: {"success": False, "error": "market closed"}
    runner.request_stop("stop_command")
    await _settle(10)

    finished = _of_type(sent, "strategy_finished")
    assert finished and finished[0]["status"] == "stop_failed"
    assert finished[0]["residual_positions"] > 0
    assert not runner.done

    # 券商恢复后下一轮重试就能真正收口
    mt5.close_by_magic = real_close_by_magic
    hub.sub.wake()
    await _settle(10)
    assert [f for f in _of_type(sent, "strategy_finished") if f["status"] == "done"]
    assert mt5.positions_by_magic(MAGIC) == []


async def test_runner_crash_with_open_positions_reports_faulted():
    """崩溃时持仓还在：报非终态 faulted，别让服务端把占位放掉。"""
    sent: list = []
    runner, hub, mt5 = _grid_runner(
        sent, prefill_enabled=False, grid_count=4,
        price_lower=100.0, price_upper=110.0,
    )
    mt5.prices_map["XAUUSD"] = 106.0
    runner.start()
    await _settle()

    _tick(hub, [], 104.0)          # 下跌穿越 105 → 买入一格
    await _settle()
    held = mt5.positions_by_magic(MAGIC)
    assert len(held) == 1

    def boom(*_args, **_kwargs):
        raise RuntimeError("terminal gone")

    mt5.place_market_order = boom
    _tick(hub, held, 101.0)        # 再穿越 102.5 → 买入时崩溃
    await _settle(10)

    finished = _of_type(sent, "strategy_finished")
    assert finished and finished[0]["status"] == "faulted"
    assert finished[0]["residual_positions"] == 1
    assert len(mt5.positions_by_magic(MAGIC)) == 1


async def test_grid_resume_prefers_runtime_levels_over_price_guess():
    """有 runtime 时按落库网格线还原，不按现价重新猜平移量。"""
    sent: list = []
    client = MockMT5Client()
    # 绝对格位 4（相对 3 + shift 1）：区间已上移到 [102.5, 112.5]
    client.place_market_order("XAUUSD", "BUY", 0.01, None, None, "G4L4", MAGIC)
    runtime = {
        "triggered": True,
        "shift_count": 1,
        "levels": [102.5, 105.0, 107.5, 110.0, 112.5],
        "stop_lower": 97.5,
        "stop_upper": 0.0,
        "side": "BUY",
        "lot_per_grid": 0.01,
    }
    runner, hub, mt5 = _grid_runner(
        sent, mt5=client, prefill_enabled=False, grid_count=4,
        price_lower=100.0, price_upper=110.0, trailing_up=True, stop_lower=95.0,
    )
    runner._resume_runtime = runtime
    # 现价仍在原区间内：若走猜测路径不会平移；有 runtime 必须还原到 shift=1
    mt5.prices_map["XAUUSD"] = 106.0

    runner.start(resume=True)
    await _settle()
    _tick(hub, mt5.positions_by_magic(MAGIC), 106.0)
    await _settle(10)

    assert runner._grid_plan is not None
    assert runner._grid_plan.shift_count == 1
    assert runner._grid_plan.levels[0] == 102.5
    assert runner._grid_plan.levels[-1] == 112.5
    assert 3 in runner._grid_plan.holdings  # 绝对 4 → 相对 3
    runtime = runner._grid_runtime_payload()
    assert runtime is not None
    assert runtime["shift_count"] == 1
    assert runtime["levels"][0] == 102.5
    runner.cancel()


async def test_grid_resume_terminates_when_price_already_past_stop():
    """恢复时现价已跌破止损：第一轮事件就要收口，不能等下一次穿越。"""
    sent: list = []
    client = MockMT5Client()
    client.place_market_order("XAUUSD", "BUY", 0.01, None, None, "G4L5", MAGIC)
    runner, hub, mt5 = _grid_runner(sent, mt5=client, stop_lower=2290.0)
    mt5.prices_map["XAUUSD"] = 2285.0

    runner.start(resume=True)
    await _settle()
    _tick(hub, mt5.positions_by_magic(MAGIC), 2285.0)
    await _settle(10)

    finished = _of_type(sent, "strategy_finished")
    assert finished and finished[0]["status"] == "done"
    assert "止损" in finished[0]["reason"]
    assert mt5.positions_by_magic(MAGIC) == []


async def test_grid_trailing_comment_carries_absolute_level():
    """平移后新买入的格位备注记绝对格位号，重连才对得上原来那一格。"""
    sent: list = []
    runner, hub, mt5 = _grid_runner(
        sent, prefill_enabled=False, grid_count=4,
        price_lower=100.0, price_upper=110.0, trailing_up=True,
    )
    mt5.prices_map["XAUUSD"] = 106.0
    runner.start()
    await _settle()

    _tick(hub, [], 111.0)
    await _settle()
    assert runner._grid_plan.shift_count == 1

    # 新区间 [102.5, 112.5]，跌破 110 → 买入相对格位 3，绝对格位 4
    _tick(hub, [], 109.0)
    await _settle()
    held = mt5.positions_by_magic(MAGIC)
    assert held
    assert held[-1]["comment"] == "G4L4"
    runner.cancel()
