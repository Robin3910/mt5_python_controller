"""加仓收口闸门：手动分散仓是本任务的最后一次加仓。

产品语义：某条规则的手动分散仓一挂出单，该规则的批次就铺完了，不能再开新的
分批加仓（逆势 / 顺势都不再）。收口按规则独立——逆势的手动仓只锁逆势，顺势照常。
"""
import asyncio

import manual_scatter as ms
import market_hub as mh
from mock_mt5 import MockMT5Client
from strategy_rules import PositionCtx, evaluate, plan_counter_limit
from strategy_runner import StrategyRunner

MAGIC = 900000001
RULE_TYPE_COUNTER = 1
RULE_TYPE_TREND = 2


# ----------------------------------------------------------------------
# 纯函数：收口判定
# ----------------------------------------------------------------------
def test_add_closed_rule_types_filters_valid_types():
    assert ms.add_closed_rule_types({1, 2}) == {1, 2}
    assert ms.add_closed_rule_types([]) == set()
    assert ms.add_closed_rule_types({0, 3, 99}) == set()
    # 非法的条目不能炸
    assert ms.add_closed_rule_types(["x", None]) == set()
    assert ms.add_closed_rule_types([1, "2"]) == {1, 2}


def test_is_add_closed_matches_by_rule():
    assert ms.is_add_closed({1}, RULE_TYPE_COUNTER) is True
    assert ms.is_add_closed({1}, RULE_TYPE_TREND) is False
    assert ms.is_add_closed(set(), RULE_TYPE_COUNTER) is False


# ----------------------------------------------------------------------
# 判定层：收口规则不再产生加仓决策
# ----------------------------------------------------------------------
def _ctx(closed: set | None = None, **over) -> PositionCtx:
    ctx = PositionCtx(
        direction="BUY",
        position_count=3,
        base_volume=0.1,
        base_price=4400.0,
        price=4410.0,
        point=0.01,
        add_count=2,
        add_counts={1: 2, 2: 0},
    )
    if closed:
        ctx.closed_rules = set(closed)
    for k, v in over.items():
        setattr(ctx, k, v)
    return ctx


def _rule(rule_type: int, **over) -> dict:
    rule = {
        "type": rule_type, "status": 1, "action": "all",
        "point": 10.0, "lot_times": 1.0, "extra_lot": 0.0,
        "max_allow_num": 5, "batch_enabled": False, "batch_levels": [],
    }
    rule.update(over)
    return rule


def test_evaluate_skips_closed_trend_rule():
    """顺势规则收口后，市价判定不再给出加仓决策。"""
    rules = [_rule(RULE_TYPE_TREND)]
    open_ctx = _ctx()
    closed_ctx = _ctx(closed={RULE_TYPE_TREND})
    assert evaluate(rules, open_ctx, rule_types=(RULE_TYPE_TREND,)) is not None
    assert evaluate(rules, closed_ctx, rule_types=(RULE_TYPE_TREND,)) is None


def test_evaluate_closed_counter_does_not_block_trend():
    """逆势收口不影响顺势：跨规则隔离。"""
    rules = [_rule(RULE_TYPE_TREND)]
    ctx = _ctx(closed={RULE_TYPE_COUNTER})
    assert evaluate(rules, ctx, rule_types=(RULE_TYPE_TREND,)) is not None


def test_plan_counter_limit_returns_none_when_counter_closed():
    """逆势规则收口后，不再计划下一档限价。"""
    rules = [_rule(RULE_TYPE_COUNTER)]
    assert plan_counter_limit(rules, _ctx()) is not None
    assert plan_counter_limit(rules, _ctx(closed={RULE_TYPE_COUNTER})) is None


def test_plan_counter_limit_still_plans_when_only_trend_closed():
    rules = [_rule(RULE_TYPE_COUNTER)]
    assert plan_counter_limit(rules, _ctx(closed={RULE_TYPE_TREND})) is not None


# ----------------------------------------------------------------------
# 执行器层
# ----------------------------------------------------------------------
class FakeHub:
    def __init__(self) -> None:
        self.sub: mh.Subscription | None = None

    def subscribe(self, *, symbol, magic, direction, hold_when_empty=False,
                  track_orders=False) -> mh.Subscription:
        self.sub = mh.Subscription(
            symbol=symbol, magic=magic, direction=direction,
            hold_when_empty=bool(hold_when_empty),
            track_orders=bool(track_orders),
        )
        return self.sub

    def unsubscribe(self, sub) -> None:
        sub.close()

    async def bar_metric(self, symbol: str, timeframe: str, metric: str) -> float:
        return 0.0


def _runner(sent: list, *, mt5=None, strategy=None, runtime=None):
    hub = FakeHub()

    async def send(payload: dict) -> None:
        sent.append(payload)

    async def _exec(fn, *args):
        return fn(*args)

    runner = StrategyRunner(
        task_id=1, magic=MAGIC, group_id="g1", signal_id="s1",
        entry={"action": "BUY", "symbol": "XAUUSD", "volume": 0.1},
        strategy=strategy or {"template_id": "tpl_1", "rules": [_rule(RULE_TYPE_TREND)]},
        mt5=mt5 or MockMT5Client(),
        hub=hub, exec_fn=_exec, send_fn=send, report_interval=1.0,
        runtime=runtime,
    )
    return runner, hub


def test_runner_add_closed_helpers():
    runner, _hub = _runner([])
    assert runner._add_closed(RULE_TYPE_TREND) is False
    assert runner._open_rule_types() == (RULE_TYPE_COUNTER, RULE_TYPE_TREND)

    runner._manual_fired.add(RULE_TYPE_TREND)
    assert runner._add_closed(RULE_TYPE_TREND) is True
    assert runner._add_closed(RULE_TYPE_COUNTER) is False
    # 顺没收口 → 顺势不再可加仓，逆势照常
    assert runner._open_rule_types() == (RULE_TYPE_COUNTER,)


def test_runner_manual_fired_restores_closed_state_on_resume():
    """重连后从 runtime 恢复收口状态：不会因为断线重新加仓。"""
    runner, _hub = _runner([], runtime={"manual_fired": [2]})
    assert runner._add_closed(RULE_TYPE_TREND) is True
    assert runner._add_closed(RULE_TYPE_COUNTER) is False


def test_runner_seed_from_positions_marks_closed_by_comment():
    """重连后从持仓备注（M1/M2）识别手动仓，恢复收口状态。"""
    runner, _hub = _runner([])
    runner._seed_from_positions([
        {"ticket": 1, "comment": "R2D100", "volume": 0.1, "price_open": 4400.0, "time": 1.0},
        {"ticket": 2, "comment": "M2", "volume": 0.1, "price_open": 4400.0, "time": 2.0},
    ])
    assert runner._add_closed(RULE_TYPE_TREND) is True
    assert runner._add_closed(RULE_TYPE_COUNTER) is False


async def test_counter_limit_cancelled_when_counter_closed():
    """逆势收口后，在途的逆势限价单被撤掉，不再挂下一档。"""
    sent: list = []
    mt5 = MockMT5Client()
    mt5.prices_map["XAUUSD"] = 4400.0
    runner, hub = _runner(
        sent, mt5=mt5,
        strategy={"template_id": "tpl_1", "rules": [_rule(RULE_TYPE_COUNTER)]},
    )
    runner._manual_fired.add(RULE_TYPE_COUNTER)
    runner.start()
    await asyncio.sleep(0)

    # 造一张在途的逆势限价单
    res = mt5.place_pending_order(
        "XAUUSD", "BUY", 0.1, 4300.0, None, None, "R1D100", MAGIC, "limit",
    )
    assert res.get("success") is True
    ticket = res.get("order")
    event = mh.MarketEvent(
        kind=mh.TICK, symbol="XAUUSD", magic=MAGIC,
        positions=(), price=4400.0, point=0.01, bid=4399.9, ask=4400.0,
    )
    await runner._sync_counter_limit(event, [])
    assert ticket not in [int(o["ticket"]) for o in mt5.pending_orders()], \
        "收口后逆势限价单应被撤掉"


async def test_counter_limit_kept_when_only_trend_closed():
    """只有顺势收口时，逆势限价单保留。"""
    sent: list = []
    mt5 = MockMT5Client()
    mt5.prices_map["XAUUSD"] = 4400.0
    runner, hub = _runner(
        sent, mt5=mt5,
        strategy={"template_id": "tpl_1", "rules": [_rule(RULE_TYPE_COUNTER)]},
    )
    runner._manual_fired.add(RULE_TYPE_TREND)
    runner.start()
    await asyncio.sleep(0)

    res = mt5.place_pending_order(
        "XAUUSD", "BUY", 0.1, 4300.0, None, None, "R1D100", MAGIC, "limit",
    )
    assert res.get("success") is True
    ticket = res.get("order")
    # 造一笔在场持仓：逆势限价要有锚点才会被保留
    mt5.place_market_order("XAUUSD", "BUY", 0.1, None, None, "R2D100", MAGIC)
    positions = mt5.positions_by_magic(MAGIC)
    event = mh.MarketEvent(
        kind=mh.TICK, symbol="XAUUSD", magic=MAGIC,
        positions=tuple(positions), price=4400.0, point=0.01,
        bid=4399.9, ask=4400.0,
    )
    await runner._sync_counter_limit(event, positions)
    assert ticket in [int(o["ticket"]) for o in mt5.pending_orders()]
