"""信号绩效统计口径（`signal_perf`）的用例。

这个模块是「绩效面板」的唯一口径来源，所以这里逐条钉死**口径**本身：

1. **只统计 tpl_1**（AI 智能加仓）—— 其它模版的仓没有「首仓 + 逐档加仓」结构，
   混进来算出来的胜率没有可比性；
2. **信号级胜率**：该信号名下**所有持仓净盈亏合计 > 0** 算赢；
3. **持仓级胜率**：单个 `(node_id, position_id)` 的净盈亏 > 0 算赢；
   —— **`position_id` 必须带 `node_id`**：MT5 的持仓号是账户内编号，
   两个不同节点（不同账户）完全可能出现相同的 `position_id`；
4. **进行中不算输**：未收口的信号单列「进行中」，不进分母；
5. **点数 / 指定价加仓的入场级周期恒为 NULL**（不依赖周期）。

**不做档位级胜率**（本轮拍板）：档位盈亏只在「入场明细」里逐行展示。
"""
import asyncio
import pathlib

import pytest

from app.db import SessionLocal, engine, init_db
from app.orm import Deal, GroupSignalTask, SignalEntryLeg, SignalHistory

# conftest 把 MYSQL_DSN 指向 _test_api.db —— 必须用同一个文件，
# 否则引擎连的是别的库，表根本没建。
_TEST_DB = pathlib.Path(__file__).resolve().parent / "_test_api.db"


def _run(coro):
    """跑一个协程，并在收尾时 dispose 引擎。

    引擎是模块级的，连接池里的连接绑定在创建它的事件循环上。测试里每次
    `asyncio.run` 都是新循环 —— 不 dispose 的话，第二次会拿到上一个循环的
    连接，报「attached to a different loop」。
    """

    async def _wrapper():
        try:
            return await coro
        finally:
            await engine.dispose()

    return asyncio.run(_wrapper())


@pytest.fixture(autouse=True)
def _db():
    """每个用例一个干净的库（建表 + 清空）。"""
    from tests.test_helpers import drop_test_db, reset_test_db

    reset_test_db(_TEST_DB)

    async def _init():
        try:
            await init_db()
        finally:
            await engine.dispose()

    asyncio.run(_init())
    yield
    drop_test_db(_TEST_DB)


# ---------------------------------------------------------------------------
# 造数
# ---------------------------------------------------------------------------
async def _seed_signal(signal_id: str, *, timeframe=None, signal_type=None,
                       status: str = "done", source: str = "tradingview") -> None:
    """一条 signal_history（周期 / 类型是纯统计维度，缺失就留 NULL）。"""
    async with SessionLocal() as s:
        s.add(SignalHistory(
            signal_id=signal_id,
            source_ip="127.0.0.1",
            raw_payload="{}",
            action="BUY",
            symbol="XAUUSD",
            volume=0.1,
            parsed_ok=True,
            status=status,
            source=source,
            model="strategy",
            timeframe=timeframe,
            signal_type=signal_type,
        ))
        await s.commit()


async def _seed_task(signal_id: str, task_id: int, *, status: str = "done",
                     template_id: str = "tpl_1") -> None:
    """一条 group_signal_task（strategy_snapshot 里带模版 ID）。"""
    async with SessionLocal() as s:
        s.add(GroupSignalTask(
            task_id=task_id,
            signal_id=signal_id,
            group_id=f"grp_{task_id}",
            group_name=f"组{task_id}",
            action="BUY",
            symbol="XAUUSD",
            volume=0.1,
            status=status,
            strategy_id=f"sty_{task_id}",
            strategy_snapshot_json={"template_id": template_id},
            trigger_source="tv_webhook",
            route_mode="direct",
        ))
        await s.commit()


async def _seed_deal(task_id: int, *, node_id: str, position_id: int, profit: float,
                     deal_id: int, entry_type: str = "OUT", volume: float = 0.1,
                     order_id: int | None = None) -> None:
    """一条成交（net_profit = profit + swap + commission）。"""
    async with SessionLocal() as s:
        s.add(Deal(
            node_id=node_id,
            deal_id=deal_id,
            order_id=order_id,
            position_id=position_id,
            symbol="XAUUSD",
            direction="BUY",
            entry_type=entry_type,
            reason=0,
            volume=volume,
            price=2400.0,
            profit=profit,
            swap=0.0,
            commission=0.0,
            net_profit=profit,
            magic=1000 + task_id,
            deal_time=1_700_000_000,
            source="strategy",
            task_id=task_id,
            group_id=f"grp_{task_id}",
        ))
        await s.commit()


async def _seed_leg(task_id: int, signal_id: str, *, seq: int, reason: str,
                    timeframe: str | None, volume: float = 0.1,
                    price: float = 2400.0, order_ticket: int | None = None) -> None:
    """一条入场明细（signal_entry_leg）。"""
    async with SessionLocal() as s:
        s.add(SignalEntryLeg(
            signal_id=signal_id,
            task_id=task_id,
            node_id="node_a",
            symbol="XAUUSD",
            action="BUY",
            seq=seq,
            reason=reason,
            timeframe=timeframe,
            volume=volume,
            price=price,
            order_ticket=order_ticket,
        ))
        await s.commit()


async def _perf():
    from app import signal_perf, signal_stats

    return await signal_perf.performance(signal_stats.SignalFilters(window="all"))


async def _scenario(*seeds):
    """按顺序造数，然后返回绩效结果。"""
    for seed in seeds:
        await seed()
    return await _perf()


# ---------------------------------------------------------------------------
# 1. 只统计 tpl_1
# ---------------------------------------------------------------------------
def test_non_tpl_1_signals_are_excluded_entirely():
    """非 tpl_1 的信号**整个跳过**，不能计入「零成交」（那会把它们算成亏损）。"""
    async def _main():
        return await _scenario(
            lambda: _seed_signal("sig_a", timeframe="M15", signal_type="ob"),
            lambda: _seed_task("sig_a", 1, template_id="tpl_2"),  # 趋势策略
        )

    res = _run(_main())
    totals = res["totals"]
    assert totals["signals_total"] == 0
    assert totals["signals_no_trade"] == 0
    assert res["by_timeframe"] == []
    assert res["by_signal_type"] == []


def test_tpl_1_signals_are_counted():
    async def _main():
        return await _scenario(
            lambda: _seed_signal("sig_a", timeframe="M15", signal_type="ob"),
            lambda: _seed_task("sig_a", 1),
            lambda: _seed_deal(1, node_id="node_a", position_id=100, profit=25.0, deal_id=1),
        )

    res = _run(_main())
    assert res["totals"]["signals_total"] == 1
    assert res["totals"]["signals_closed"] == 1
    assert res["totals"]["signals_win"] == 1
    assert res["totals"]["signal_win_rate"] == 100.0


# ---------------------------------------------------------------------------
# 2. 信号级胜率
# ---------------------------------------------------------------------------
def test_signal_level_win_is_sum_of_all_positions():
    """信号级：**全部持仓净盈亏合计 > 0** 才算赢。

    一赢一亏但合计为正 → 整条信号算赢（信号级看的是这条信号的总账）。
    """
    async def _main():
        return await _scenario(
            lambda: _seed_signal("sig_a"),
            lambda: _seed_task("sig_a", 1),
            lambda: _seed_deal(1, node_id="node_a", position_id=100, profit=30.0, deal_id=1),
            lambda: _seed_deal(1, node_id="node_a", position_id=101, profit=-5.0, deal_id=2),
        )

    res = _run(_main())
    totals = res["totals"]
    assert totals["signals_win"] == 1, "合计 +25 > 0 应算赢"
    assert totals["signal_profit"] == 25.0
    # 持仓级则是两笔分别判：1 赢 1 输
    assert totals["positions_closed"] == 2
    assert totals["positions_win"] == 1
    assert totals["position_win_rate"] == 50.0


def test_signal_level_loss_when_sum_is_negative():
    """合计为负 → 信号级算输。"""
    async def _main():
        return await _scenario(
            lambda: _seed_signal("sig_a"),
            lambda: _seed_task("sig_a", 1),
            lambda: _seed_deal(1, node_id="node_a", position_id=100, profit=10.0, deal_id=1),
            lambda: _seed_deal(1, node_id="node_a", position_id=101, profit=-40.0, deal_id=2),
        )

    res = _run(_main())
    assert res["totals"]["signals_win"] == 0
    assert res["totals"]["signal_profit"] == -30.0
    assert res["totals"]["signal_win_rate"] == 0.0


# ---------------------------------------------------------------------------
# 3. 持仓级：position_id 必须带 node_id
# ---------------------------------------------------------------------------
def test_same_position_id_on_different_nodes_are_separate_positions():
    """**不同节点的相同 position_id 必须算成两笔持仓**。

    MT5 的持仓号是账户内编号，两个节点（两个 MT5 账户）完全可能出现相同的
    position_id。只按 position_id 分组会把两个账户的仓合并成一笔 —— 胜率直接算错。
    """
    async def _main():
        return await _scenario(
            lambda: _seed_signal("sig_a"),
            lambda: _seed_task("sig_a", 1),
            # 节点 A：持仓 100 亏 50；节点 B：持仓 100 赚 80
            lambda: _seed_deal(1, node_id="node_a", position_id=100, profit=-50.0, deal_id=1),
            lambda: _seed_deal(1, node_id="node_b", position_id=100, profit=80.0, deal_id=2),
        )

    res = _run(_main())
    totals = res["totals"]
    assert totals["positions_closed"] == 2, "必须是两笔独立持仓"
    assert totals["positions_win"] == 1
    assert totals["position_win_rate"] == 50.0
    assert totals["signal_profit"] == 30.0


def test_partial_close_of_one_position_counts_as_one_position():
    """一笔仓分两次出场 → 算**一笔**持仓（不是两笔）。"""
    async def _main():
        return await _scenario(
            lambda: _seed_signal("sig_a"),
            lambda: _seed_task("sig_a", 1),
            lambda: _seed_deal(1, node_id="node_a", position_id=100, profit=20.0, deal_id=1),
            lambda: _seed_deal(1, node_id="node_a", position_id=100, profit=15.0, deal_id=2),
        )

    res = _run(_main())
    assert res["totals"]["positions_closed"] == 1
    assert res["totals"]["positions_win"] == 1
    assert res["totals"]["position_profit"] == 35.0


def test_entry_deals_are_not_counted_as_out_profit():
    """开仓成交（entry_type=IN）不计入盈亏 —— 只有出场成交算数。"""
    async def _main():
        return await _scenario(
            lambda: _seed_signal("sig_a"),
            lambda: _seed_task("sig_a", 1),
            lambda: _seed_deal(1, node_id="node_a", position_id=100, profit=0.0, deal_id=1,
                               entry_type="IN", volume=0.1),
            lambda: _seed_deal(1, node_id="node_a", position_id=100, profit=12.0, deal_id=2,
                               entry_type="OUT", volume=0.1),
        )

    res = _run(_main())
    assert res["totals"]["positions_closed"] == 1
    assert res["totals"]["position_profit"] == 12.0
    # 手数按全部成交统计（开仓 + 出场）
    assert res["totals"]["volume"] == 0.2


# ---------------------------------------------------------------------------
# 4. 进行中不算输
# ---------------------------------------------------------------------------
def test_running_signals_are_excluded_from_denominator():
    """**进行中的信号不算输**，单列「进行中」，不进胜率分母。

    把进行中的当输会把胜率算得极低 —— 那不是策略的问题，是口径的问题。
    """
    async def _main():
        return await _scenario(
            lambda: _seed_signal("sig_running", status="running"),
            lambda: _seed_task("sig_running", 1, status="running"),
            lambda: _seed_deal(1, node_id="node_a", position_id=100, profit=-100.0, deal_id=1),
        )

    res = _run(_main())
    totals = res["totals"]
    assert totals["signals_total"] == 1
    assert totals["signals_running"] == 1
    assert totals["signals_closed"] == 0
    assert totals["signals_win"] == 0
    assert totals["signal_win_rate"] == 0.0
    # 浮盈单列展示
    assert totals["running_profit"] == -100.0
    # 不进持仓级分母
    assert totals["positions_closed"] == 0
    assert totals["running_position_profit"] == -100.0


def test_closed_signal_with_no_trade_is_excluded_from_denominator():
    """收口但零成交：没开仓就没有胜负，不进分母（单列 signals_no_trade）。"""
    async def _main():
        return await _scenario(
            lambda: _seed_signal("sig_a"),
            lambda: _seed_task("sig_a", 1),
        )

    res = _run(_main())
    totals = res["totals"]
    assert totals["signals_closed"] == 1
    assert totals["signals_no_trade"] == 1
    assert totals["signal_win_rate"] == 0.0, "分母为 0 时胜率是 0 而不是 NaN"


# ---------------------------------------------------------------------------
# 5. 分桶：周期 / 信号类型
# ---------------------------------------------------------------------------
def test_buckets_by_timeframe_and_signal_type():
    """周期与信号类型各自成桶；未标注的进 __none__ 桶。"""
    async def _main():
        return await _scenario(
            lambda: _seed_signal("sig_a", timeframe="M15", signal_type="ob"),
            lambda: _seed_task("sig_a", 1),
            lambda: _seed_deal(1, node_id="node_a", position_id=100, profit=10.0, deal_id=1),

            lambda: _seed_signal("sig_b", timeframe="H1", signal_type="fvg"),
            lambda: _seed_task("sig_b", 2),
            lambda: _seed_deal(2, node_id="node_a", position_id=200, profit=-10.0, deal_id=2),

            lambda: _seed_signal("sig_c"),
            lambda: _seed_task("sig_c", 3),
            lambda: _seed_deal(3, node_id="node_a", position_id=300, profit=5.0, deal_id=3),
        )

    res = _run(_main())
    by_tf = {b["key"]: b for b in res["by_timeframe"]}
    assert set(by_tf) == {"M15", "H1", "__none__"}
    assert by_tf["M15"]["signals_total"] == 1
    assert by_tf["M15"]["signal_win_rate"] == 100.0
    assert by_tf["H1"]["signal_win_rate"] == 0.0
    assert by_tf["__none__"]["label"] == "未标注"

    by_st = {b["key"]: b for b in res["by_signal_type"]}
    assert set(by_st) == {"ob", "fvg", "__none__"}
    assert by_st["ob"]["signals_total"] == 1


# ---------------------------------------------------------------------------
# 6. 入场明细：点数 / 指定价的周期恒为 NULL
# ---------------------------------------------------------------------------
def test_entry_legs_breakdown_by_reason():
    """入场原因分桶 + 入场明细逐行展示（**不做档位级胜率**）。"""
    async def _main():
        return await _scenario(
            lambda: _seed_signal("sig_a", timeframe="M15", signal_type="ob"),
            lambda: _seed_task("sig_a", 1),
            lambda: _seed_deal(1, node_id="node_a", position_id=100, profit=10.0, deal_id=1,
                               order_id=9001),
            lambda: _seed_leg(1, "sig_a", seq=0, reason="first", timeframe="M15",
                              order_ticket=9001),
            lambda: _seed_leg(1, "sig_a", seq=1, reason="point", timeframe=None),
            lambda: _seed_leg(1, "sig_a", seq=2, reason="atr", timeframe="H1"),
        )

    res = _run(_main())
    by_reason = {b["key"]: b for b in res["by_entry_reason"]}
    assert set(by_reason) == {"first", "point", "atr"}
    assert by_reason["first"]["orders"] == 1

    legs = {leg["seq"]: leg for leg in res["legs"]}
    assert legs[0]["timeframe"] == "M15"
    assert legs[0]["reason_label"] == "首仓"
    assert legs[1]["reason_label"] == "点数加仓"
    assert legs[1]["timeframe"] is None, "**点数加仓没有周期**"
    assert legs[2]["timeframe"] == "H1"


def test_point_and_price_legs_have_null_timeframe():
    """点数 / 指定价加仓的入场级周期**恒为 NULL**。

    硬凑一个周期会把「点数间距」混进「ATR 间距」的分桶，统计出来的数字没有意义。
    """
    async def _main():
        return await _scenario(
            lambda: _seed_signal("sig_a"),
            lambda: _seed_task("sig_a", 1),
            lambda: _seed_deal(1, node_id="node_a", position_id=100, profit=10.0, deal_id=1),
            lambda: _seed_leg(1, "sig_a", seq=0, reason="first", timeframe="M15"),
            lambda: _seed_leg(1, "sig_a", seq=1, reason="point", timeframe=None),
            lambda: _seed_leg(1, "sig_a", seq=2, reason="price", timeframe=None),
        )

    res = _run(_main())
    legs = {leg["seq"]: leg for leg in res["legs"]}
    assert legs[1]["timeframe"] is None
    assert legs[2]["timeframe"] is None
    # 未标注的周期标签是「未标注」，不是空串
    assert legs[1]["timeframe_label"] == "未标注"


def test_leg_realized_profit_uses_position_net_profit():
    """入场明细的盈亏按该入场对应持仓的净盈亏归集（带 node_id 反查）。"""
    async def _main():
        return await _scenario(
            lambda: _seed_signal("sig_a"),
            lambda: _seed_task("sig_a", 1),
            lambda: _seed_deal(1, node_id="node_a", position_id=100, profit=18.0, deal_id=1,
                               order_id=7001),
            lambda: _seed_leg(1, "sig_a", seq=0, reason="first", timeframe="M15",
                              order_ticket=7001),
        )

    res = _run(_main())
    leg = res["legs"][0]
    assert leg["closed"] is True
    assert leg["realized_profit"] == 18.0


# ---------------------------------------------------------------------------
# 7. 空数据
# ---------------------------------------------------------------------------
def test_empty_database_returns_zeroed_shape():
    """没有任何数据时也要返回完整的结构（前端要直接渲染）。"""
    res = _run(_perf())
    assert res["totals"]["signals_total"] == 0
    assert res["totals"]["signal_win_rate"] == 0.0
    assert res["totals"]["position_win_rate"] == 0.0
    assert res["by_timeframe"] == []
    assert res["by_signal_type"] == []
    assert res["by_entry_reason"] == []
    assert res["legs"] == []
