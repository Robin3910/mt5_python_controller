"""出入金 → 止损自动重算（只认资金操作；出金保留旧 SL）。

覆盖：
- 纯函数层：资金操作成交的识别与净金额
- 入金：余额涨了且确认是出入金 → 止损按新余额重算
- 出金：余额跌了 → 止损**保留旧值不动**
- 平仓结算：余额涨了但不是资金操作 → 止损不动
- 2b 衔接：入金导致的新止损价越价时，保留旧值而不是强平
- 手动分散仓在场时（只同步止损那条线），入金同样触发重算
"""
import asyncio

import market_hub as mh
from mock_mt5 import MockMT5Client

import signal_pl as sp
from strategy_runner import StrategyRunner

MAGIC = 900000001


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
        self.unsubscribed = True
        sub.close()

    async def bar_metric(self, symbol: str, timeframe: str, metric: str) -> float:
        return 0.0

    async def bar_range_pair(
        self, symbol: str, timeframe: str, *, refresh: float = 5.0,
    ) -> tuple[float, float]:
        return (0.0, 0.0)


def _rule(**over) -> dict:
    rule = {
        "type": 1, "status": 1, "action": "all",
        "point": 100.0, "lot_times": 1.0, "extra_lot": 0.0,
        "max_allow_num": 3, "batch_enabled": False, "batch_levels": [],
        "float_pl_ratio": {
            "enabled": True, "ratio": -20.0, "monitor_mode": "loop", "max_times": 1,
        },
        "lot_pl_tiers": {
            "enabled": False, "batch_count": 2, "close_action": "all",
            "tiers": [
                {"min_lot": 0.1, "pl_amount": 50.0},
                {"min_lot": 0.5, "pl_amount": 100.0},
            ],
        },
    }
    rule.update(over)
    return rule


def _runner(sent: list, *, mt5=None, strategy=None, runtime=None) -> tuple[StrategyRunner, FakeHub]:
    hub = FakeHub()

    async def send(payload: dict) -> None:
        sent.append(payload)

    async def _exec(fn, *args):
        return fn(*args)

    runner = StrategyRunner(
        task_id=1, magic=MAGIC, group_id="g1", signal_id="s1",
        entry={"action": "BUY", "symbol": "XAUUSD", "volume": 0.1},
        strategy=strategy or {"rules": [_rule()]},
        mt5=mt5 or MockMT5Client(),
        hub=hub, exec_fn=_exec, send_fn=send, report_interval=1.0,
        runtime=runtime,
    )
    return runner, hub


async def _settle(times: int = 6) -> None:
    for _ in range(times):
        await asyncio.sleep(0)


def _tick(hub: FakeHub, positions, price: float) -> None:
    hub.sub.offer(mh.MarketEvent(
        kind=mh.TICK, symbol="XAUUSD", magic=MAGIC,
        positions=tuple(positions), price=price, point=0.01,
        bid=price, ask=price,
    ))


def _held(mt5) -> list[dict]:
    return [p for p in mt5._positions if int(p.get("magic") or 0) == MAGIC]


async def _open_and_sync(runner, hub, mt5) -> float:
    """跑到首单开出来、止损写好；返回开仓价。"""
    runner.start()
    await _settle()
    held = _held(mt5)
    open_px = held[0]["price_open"]
    _tick(hub, held, open_px)
    await _settle()
    return open_px


def _advance_probe(runner) -> None:
    """把探测节流时间往前拨，让下一次探测真的执行。"""
    runner._balance_probe_at = 0.0


# ----------------------------------------------------------------------
# 纯函数层
# ----------------------------------------------------------------------
def test_is_funding_deal_only_accepts_funding_types():
    """只有出入金等资金操作算；普通交易成交不算。"""
    assert sp.is_funding_deal({"deal_type": 2}) is True   # DEAL_TYPE_BALANCE
    assert sp.is_funding_deal({"deal_type": 3}) is True   # CREDIT
    assert sp.is_funding_deal({"deal_type": 4}) is True   # CHARGE
    assert sp.is_funding_deal({"deal_type": 6}) is True   # BONUS
    # 交易成交：BUY / SELL
    assert sp.is_funding_deal({"deal_type": 0}) is False
    assert sp.is_funding_deal({"deal_type": 1}) is False
    # 券商侧按日扣的佣金：不是客户主动出入金
    assert sp.is_funding_deal({"deal_type": 8}) is False
    # 没有 deal_type（老数据）不能当成资金操作
    assert sp.is_funding_deal({"direction": "BUY"}) is False
    assert sp.is_funding_deal(None) is False


def test_funding_balance_delta_ignores_trade_closes():
    """平仓结算不算进资金操作净金额。"""
    deals = [
        {"deal_type": 2, "profit": 500.0},   # 出入金
        {"deal_type": 1, "profit": 120.0},   # 平仓成交（已实现盈亏）
        {"deal_type": 2, "profit": -100.0},  # 出金
    ]
    assert sp.funding_balance_delta(deals) == 400.0
    assert len(sp.filter_funding_deals(deals)) == 2


def test_funding_balance_delta_empty_is_zero():
    assert sp.funding_balance_delta([]) == 0.0
    assert sp.funding_balance_delta(None) == 0.0


# ----------------------------------------------------------------------
# 入金 → 止损重算
# ----------------------------------------------------------------------
async def test_deposit_recalculates_stop_loss():
    """客户手动加保证金：止损按新余额重算（放宽）。"""
    sent: list = []
    mt5 = MockMT5Client()
    mt5.balance = 10000.0
    runner, hub = _runner(sent, mt5=mt5)

    open_px = await _open_and_sync(runner, hub, mt5)
    assert mt5.positions_by_magic(MAGIC)[0]["sl"] == open_px - 200, "初始止损按 10000 算"

    # 客户入金 10000 → 余额 20000 → 亏损金额 4000 → 距离翻倍
    mt5.deposit(10000.0)
    _advance_probe(runner)
    _tick(hub, _held(mt5), open_px)
    await _settle()

    pos = mt5.positions_by_magic(MAGIC)[0]
    assert pos["sl"] == open_px - 400, "入金后止损必须按新余额重算"
    runner.cancel()


async def test_deposit_only_triggers_once():
    """同一笔出入金不能反复触发重算（否则止损会被反复推）。"""
    sent: list = []
    mt5 = MockMT5Client()
    mt5.balance = 10000.0
    runner, hub = _runner(sent, mt5=mt5)

    open_px = await _open_and_sync(runner, hub, mt5)
    mt5.deposit(10000.0)
    _advance_probe(runner)
    _tick(hub, _held(mt5), open_px)
    await _settle()
    assert mt5.positions_by_magic(MAGIC)[0]["sl"] == open_px - 400

    # 再来几拍：不应再改（余额没变，出入金也只有那一笔）
    for _ in range(3):
        _advance_probe(runner)
        _tick(hub, _held(mt5), open_px)
        await _settle()
    assert mt5.positions_by_magic(MAGIC)[0]["sl"] == open_px - 400, "同一笔入金只算一次"
    runner.cancel()


async def test_deposit_with_balance_unchanged_does_not_move_stop():
    """余额没变（出入金金额为 0 或与余额不符）时止损不该动。"""
    sent: list = []
    mt5 = MockMT5Client()
    mt5.balance = 10000.0
    runner, hub = _runner(sent, mt5=mt5)

    open_px = await _open_and_sync(runner, hub, mt5)
    before = mt5.positions_by_magic(MAGIC)[0]["sl"]

    # 0 元的出入金：不是入账
    mt5.deposit(0.0)
    _advance_probe(runner)
    _tick(hub, _held(mt5), open_px)
    await _settle()

    assert mt5.positions_by_magic(MAGIC)[0]["sl"] == before
    runner.cancel()


# ----------------------------------------------------------------------
# 出金 → 保留旧 SL
# ----------------------------------------------------------------------
async def test_withdrawal_keeps_old_stop_loss():
    """客户出金：止损保留旧值不动。"""
    sent: list = []
    mt5 = MockMT5Client()
    mt5.balance = 10000.0
    runner, hub = _runner(sent, mt5=mt5)

    open_px = await _open_and_sync(runner, hub, mt5)
    before = mt5.positions_by_magic(MAGIC)[0]["sl"]
    assert before == open_px - 200

    mt5.withdraw(5000.0)
    _advance_probe(runner)
    _tick(hub, _held(mt5), open_px)
    await _settle()

    pos = mt5.positions_by_magic(MAGIC)[0]
    assert pos["sl"] == before, "出金不该动止损"
    assert len(mt5.positions_by_magic(MAGIC)) == 1, "出金不该平仓"
    runner.cancel()


async def test_withdrawal_then_deposit_recalculates_on_deposit():
    """出金后入金：只在入金那一侧重算。"""
    sent: list = []
    mt5 = MockMT5Client()
    mt5.balance = 10000.0
    runner, hub = _runner(sent, mt5=mt5)

    open_px = await _open_and_sync(runner, hub, mt5)
    before = mt5.positions_by_magic(MAGIC)[0]["sl"]

    mt5.withdraw(5000.0)
    _advance_probe(runner)
    _tick(hub, _held(mt5), open_px)
    await _settle()
    assert mt5.positions_by_magic(MAGIC)[0]["sl"] == before

    # 再入金 5000 → 余额回到 10000 → 止损应回到初始值
    mt5.deposit(5000.0)
    _advance_probe(runner)
    _tick(hub, _held(mt5), open_px)
    await _settle()
    assert mt5.positions_by_magic(MAGIC)[0]["sl"] == open_px - 200
    runner.cancel()


# ----------------------------------------------------------------------
# 入金只影响止损那一侧
# ----------------------------------------------------------------------
async def test_deposit_does_not_touch_take_profit():
    """入金只重算止损：止盈由分档金额决定，与余额无关，不能跟着动。"""
    sent: list = []
    mt5 = MockMT5Client()
    mt5.balance = 10000.0
    rule = _rule(lot_pl_tiers={
        "enabled": True, "batch_count": 2, "close_action": "all",
        "tiers": [
            {"min_lot": 0.1, "pl_amount": 100.0},
            {"min_lot": 0.5, "pl_amount": 200.0},
        ],
    })
    runner, hub = _runner(sent, mt5=mt5, strategy={"rules": [rule]})

    open_px = await _open_and_sync(runner, hub, mt5)
    pos = mt5.positions_by_magic(MAGIC)[0]
    tp_before = pos["tp"]
    # D = 100 / ((1/0.01) * 0.1) = 10
    assert tp_before == open_px + 10

    mt5.deposit(10000.0)
    _advance_probe(runner)
    _tick(hub, _held(mt5), open_px)
    await _settle()

    pos = mt5.positions_by_magic(MAGIC)[0]
    assert pos["tp"] == tp_before, "止盈不该被入金影响"
    assert pos["sl"] == open_px - 400, "止损按新余额重算"
    runner.cancel()


# ----------------------------------------------------------------------
# 平仓结算不算资金操作
# ----------------------------------------------------------------------
async def test_trade_close_settlement_does_not_move_stop():
    """平仓结算带来的余额变化不是本金变化：止损不动。

    余额会因已实现盈亏上升，但没有任何资金操作成交 —— 止损不该被推走。
    """
    sent: list = []
    mt5 = MockMT5Client()
    mt5.balance = 10000.0
    runner, hub = _runner(sent, mt5=mt5)

    open_px = await _open_and_sync(runner, hub, mt5)
    before = mt5.positions_by_magic(MAGIC)[0]["sl"]

    # 平掉一笔并让余额上升（模拟已实现盈亏结算）
    mt5.balance += 500.0
    _advance_probe(runner)
    _tick(hub, _held(mt5), open_px)
    await _settle()

    assert mt5.positions_by_magic(MAGIC)[0]["sl"] == before, "平仓结算不该动止损"
    runner.cancel()


# ----------------------------------------------------------------------
# 入金后新止损价的位置关系
# ----------------------------------------------------------------------
async def test_deposit_widens_stop_away_from_market():
    """入金让止损离现价更远：多单止损下移、空单止损上移。

    这一点很要紧：止损是「离现价越远越安全」的方向，所以入金后重算出的
    新止损**不可能**比旧止损更靠近现价 —— 也就是说入金不会触发 2b 的
    「新值越价」分支，也不会把仓位推进更危险的位置。
    """
    sent: list = []
    mt5 = MockMT5Client()
    mt5.balance = 10000.0
    runner, hub = _runner(sent, mt5=mt5)

    open_px = await _open_and_sync(runner, hub, mt5)
    old_sl = mt5.positions_by_magic(MAGIC)[0]["sl"]
    assert old_sl == open_px - 200

    mt5.deposit(10000.0)
    _advance_probe(runner)
    _tick(hub, _held(mt5), open_px)
    await _settle()

    new_sl = mt5.positions_by_magic(MAGIC)[0]["sl"]
    assert new_sl == open_px - 400, "余额翻倍 → 止损距离翻倍"
    assert new_sl < old_sl, "多单止损应下移，离现价更远"
    assert new_sl < open_px, "止损仍在现价下方（多单的正确一侧）"
    assert len(mt5.positions_by_magic(MAGIC)) == 1, "不该被平掉"
    runner.cancel()


async def test_deposit_after_price_dip_does_not_close_position():
    """价格已跌破旧止损、此时客户入金：新止损更宽，写新值而不是平仓。

    入金是客户的明确意图 —— 他加了钱，就是想让这批仓位多扛一会儿。旧止损
    在这个时点已经被现价越过，但那是**旧余额**下的止损；按新余额重算出的
    止损还没被越过，所以该写新值，不该把仓位平掉。
    """
    sent: list = []
    mt5 = MockMT5Client()
    mt5.balance = 10000.0
    runner, hub = _runner(sent, mt5=mt5)

    open_px = await _open_and_sync(runner, hub, mt5)
    old_sl = mt5.positions_by_magic(MAGIC)[0]["sl"]
    assert old_sl == open_px - 200

    # 价格跌到旧止损（-200）之下，但新止损（-400）还没到
    mt5.deposit(10000.0)
    _advance_probe(runner)
    _tick(hub, _held(mt5), open_px - 250)
    await _settle()

    pos = mt5.positions_by_magic(MAGIC)[0]
    assert pos["sl"] == open_px - 400, "应写入按新余额算出的止损"
    assert len(mt5.positions_by_magic(MAGIC)) == 1, "不该被市价平掉"
    runner.cancel()


# ----------------------------------------------------------------------
# 手动分散仓在场（只同步止损那条线）
# ----------------------------------------------------------------------
async def test_deposit_recalculates_in_manual_authority_branch():
    """手动仓在场（止盈听手动仓权威）时，入金同样要重算止损。"""
    sent: list = []
    mt5 = MockMT5Client()
    mt5.balance = 10000.0
    runner, hub = _runner(sent, mt5=mt5)

    open_px = await _open_and_sync(runner, hub, mt5)
    assert mt5.positions_by_magic(MAGIC)[0]["sl"] == open_px - 200

    # 手动仓成交 → 止盈权威在场 → 走只同步止损的分支
    runner._manual_tp_authority = {1: open_px + 50}
    mt5.deposit(10000.0)
    _advance_probe(runner)
    _tick(hub, _held(mt5), open_px)
    await _settle()

    pos = mt5.positions_by_magic(MAGIC)[0]
    assert pos["sl"] == open_px - 400, "手动分支下入金也要重算止损"
    runner.cancel()


# ----------------------------------------------------------------------
# 首次探测不追溯
# ----------------------------------------------------------------------
async def test_first_probe_does_not_replay_history_deposit():
    """节点上线前就存在的出入金不追溯：首次探测只登记游标。"""
    sent: list = []
    mt5 = MockMT5Client()
    mt5.balance = 10000.0
    runner, hub = _runner(sent, mt5=mt5)

    # 上线前的一次入金
    mt5.deposit(10000.0)
    mt5.balance = 20000.0

    open_px = await _open_and_sync(runner, hub, mt5)
    before = mt5.positions_by_magic(MAGIC)[0]["sl"]

    _advance_probe(runner)
    _tick(hub, _held(mt5), open_px)
    await _settle()

    # 首次探测只登记游标，不触发重算
    assert mt5.positions_by_magic(MAGIC)[0]["sl"] == before
    runner.cancel()


# ----------------------------------------------------------------------
# 关闭盈亏比时不影响余额探测
# ----------------------------------------------------------------------
async def test_disabled_ratio_ignores_deposit():
    """没启用盈亏比就没有止损可重算，入金不产生任何改单。"""
    sent: list = []
    mt5 = MockMT5Client()
    mt5.balance = 10000.0
    rule = _rule(float_pl_ratio={
        "enabled": False, "ratio": -20.0, "monitor_mode": "loop", "max_times": 1,
    })
    runner, hub = _runner(sent, mt5=mt5, strategy={"rules": [rule]})

    runner.start()
    await _settle()
    held = _held(mt5)
    open_px = held[0]["price_open"]
    _tick(hub, held, open_px)
    await _settle()
    before = mt5.positions_by_magic(MAGIC)[0]["sl"]

    mt5.deposit(10000.0)
    _advance_probe(runner)
    _tick(hub, _held(mt5), open_px)
    await _settle()

    assert mt5.positions_by_magic(MAGIC)[0]["sl"] == before
    runner.cancel()


# ----------------------------------------------------------------------
# runtime 往返：止损基准余额要能跨重连保留
# ----------------------------------------------------------------------
async def test_sl_balance_survives_resume():
    """止损基准余额随 runtime 落库并在重连后还原。

    没有它，节点重启后就不知道「当前止损是按多少余额算的」，客户再入金时
    无法判断该不该重算。
    """
    sent: list = []
    mt5 = MockMT5Client()
    mt5.balance = 10000.0
    runner, hub = _runner(sent, mt5=mt5)

    open_px = await _open_and_sync(runner, hub, mt5)
    runner._lot_sltp_sl_balance = 10000.0
    payload = runner._manual_runtime_payload()
    assert payload is not None
    assert payload["sl_balance"] == 10000.0
    runner.cancel()

    # 重连：从 runtime 还原
    runner2, _hub2 = _runner(sent, mt5=mt5, runtime=payload)
    assert runner2._lot_sltp_sl_balance == 10000.0
    assert open_px > 0


async def test_sl_balance_absent_in_runtime_is_none():
    """老版本落库的 runtime 没有这个字段：按「不知道基准」处理，不报错。"""
    sent: list = []
    runner, _hub = _runner(sent, runtime={"manual_fired": []})
    assert runner._lot_sltp_sl_balance is None


# ----------------------------------------------------------------------
# 旧版客户端没有资金操作查询能力
# ----------------------------------------------------------------------
async def test_client_without_balance_operations_is_tolerated():
    """旧版 MT5 客户端没有 balance_operations_since：不标脏，也不报错。"""
    sent: list = []

    class OldClient(MockMT5Client):
        """旧版客户端：没有查资金操作成交的能力。"""

        balance_operations_since = None

    mt5 = OldClient()
    mt5.balance = 10000.0
    runner, hub = _runner(sent, mt5=mt5)

    open_px = await _open_and_sync(runner, hub, mt5)
    before = mt5.positions_by_magic(MAGIC)[0]["sl"]

    mt5.deposit(10000.0)
    _advance_probe(runner)
    _tick(hub, _held(mt5), open_px)
    await _settle()

    assert mt5.positions_by_magic(MAGIC)[0]["sl"] == before
    assert runner._balance_dirty is False
    runner.cancel()
