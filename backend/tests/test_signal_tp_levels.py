"""**马丁止盈档位**（`signal_tp_levels`）的用例。

档位 = 最终止盈那一刻的持仓笔数（含首仓）。

这个模块最关键的三条口径（都是拍板定的）：
1. **按节点分开统计** —— 同一信号在两个节点上档位不同 → 各算各的；
2. **止损不进止盈分布** —— 止损收口没有止盈事件，不产生档位；
3. **持仓中不产生档位** —— `tp_level = None`，不进分母。

覆盖：
    10|1. TP 时刻取最早的 `reason=5` 出场成交；
2. 在场判定（首 IN ≤ TP ≤ 末 OUT；同秒算在场）；
3. 止损 / 持仓中 / 非 tpl_1 的排除；
4. 按节点分开统计；
5. 分布聚合（分母 = 已止盈单元数）。
"""
import asyncio
import pathlib

import pytest

from app.db import SessionLocal, engine, init_db
from app.orm import Deal, GroupSignalTask, GroupTaskDispatch, SignalEntryLeg, SignalHistory

_TEST_DB = pathlib.Path(__file__).resolve().parent / "_test_api.db"


def _run(coro):
    async def _wrapper():
        try:
            return await coro
        finally:
            await engine.dispose()

    return asyncio.run(_wrapper())


@pytest.fixture(autouse=True)
def _db():
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
async def _seed_signal(signal_id: str, *, timeframe="M15", signal_type="ob") -> None:
    async with SessionLocal() as s:
        s.add(SignalHistory(
            signal_id=signal_id,
            source_ip="127.0.0.1",
            raw_payload="{}",
            action="BUY",
            symbol="XAUUSD",
            volume=0.1,
            parsed_ok=True,
            status="done",
            source="tradingview",
            model="strategy",
            timeframe=timeframe,
            signal_type=signal_type,
        ))
        await s.commit()


async def _seed_task(signal_id: str, task_id: int, *,
                     template_id: str = "tpl_1") -> None:
    async with SessionLocal() as s:
        s.add(GroupSignalTask(
            task_id=task_id,
            signal_id=signal_id,
            group_id=f"grp_{task_id}",
            group_name=f"组{task_id}",
            action="BUY",
            symbol="XAUUSD",
            volume=0.1,
            status="done",
            strategy_id=f"sty_{task_id}",
            strategy_snapshot_json={"template_id": template_id},
            trigger_source="tv_webhook",
            route_mode="direct",
        ))
        await s.commit()


async def _seed_dispatch(task_id: int, *, node_id: str, magic: int,
                         signal_id: str = "") -> None:
    async with SessionLocal() as s:
        s.add(GroupTaskDispatch(
            task_id=task_id,
            signal_id=signal_id,
            group_id=f"grp_{task_id}",
            node_id=node_id,
            symbol="XAUUSD",
            action="BUY",
            magic=magic,
            status="done",
        ))
        await s.commit()


async def _seed_deal(task_id: int, *, magic: int, node_id: str, position_id: int,
                     deal_id: int, entry_type: str, reason: int,
                     deal_time: int, order_id=None) -> None:
    async with SessionLocal() as s:
        s.add(Deal(
            node_id=node_id,
            deal_id=deal_id,
            order_id=order_id,
            position_id=position_id,
            symbol="XAUUSD",
            direction="BUY",
            entry_type=entry_type,
            reason=reason,
            volume=0.1,
            price=2400.0,
            profit=0.0,
            swap=0.0,
            commission=0.0,
            net_profit=0.0,
            magic=magic,
            deal_time=deal_time,
            source="strategy",
            task_id=task_id,
            group_id=f"grp_{task_id}",
        ))
        await s.commit()


async def _seed_leg(task_id: int, *, signal_id: str, node_id: str, magic: int,
                    seq: int, rule_type, order_ticket: int) -> None:
    async with SessionLocal() as s:
        s.add(SignalEntryLeg(
            signal_id=signal_id,
            task_id=task_id,
            node_id=node_id,
            magic=magic,
            symbol="XAUUSD",
            action="BUY",
            seq=seq,
            reason="first" if seq == 0 else "atr",
            rule_type=rule_type,
            level_index=None if seq == 0 else seq,
            order_ticket=order_ticket,
        ))
        await s.commit()


async def _seed_martingale(
    signal_id: str, task_id: int, *, magic: int, node_id: str,
    levels: int, tp_time: int = 1_700_000_100,
) -> None:
    """造一条「加了 N 仓后止盈」的信号。

    `levels` = 止盈那一刻的持仓笔数（含首仓）。
    """
    await _seed_signal(signal_id)
    await _seed_task(signal_id, task_id)
    await _seed_dispatch(task_id, node_id=node_id, magic=magic, signal_id=signal_id)
    base = 1_700_000_000
    for i in range(levels):
        pid = 1000 + task_id * 100 + i
        await _seed_deal(
            task_id, magic=magic, node_id=node_id, position_id=pid,
            deal_id=pid * 10 + 1, entry_type="IN", reason=0,
            deal_time=base + i * 10, order_id=pid * 10 + 1,
        )
        await _seed_leg(
            task_id, signal_id=signal_id, node_id=node_id, magic=magic,
            seq=i, rule_type=None if i == 0 else 1, order_ticket=pid * 10 + 1,
        )
    # 全部止盈出场
    for i in range(levels):
        pid = 1000 + task_id * 100 + i
        await _seed_deal(
            task_id, magic=magic, node_id=node_id, position_id=pid,
            deal_id=pid * 10 + 2, entry_type="OUT", reason=5,
            deal_time=tp_time, order_id=None,
        )


# ---------------------------------------------------------------------------
# 1. 纯函数
# ---------------------------------------------------------------------------
def test_position_present_boundaries():
    """在场判定：首 IN ≤ TP ≤ 末 OUT；同秒算在场。"""
    from app.signal_tp_levels import position_present

    assert position_present(100, 200, 150) is True
    assert position_present(100, 200, 200) is True, "同一秒平掉的算在场"
    assert position_present(100, 200, 201) is False
    assert position_present(100, None, 150) is True, "还没平过 → 在场"
    assert position_present(None, 200, 150) is False, "还没开 → 不在场"
    assert position_present(201, None, 150) is False


def test_tp_state_of():
    from app.signal_tp_levels import tp_state_of

    assert tp_state_of(100, None, True) == "tp"
    assert tp_state_of(None, 100, True) == "sl"
    assert tp_state_of(None, None, True) == "running"
    assert tp_state_of(None, None, False) == "none"


def test_clamp_level():
    from app.signal_tp_levels import clamp_level

    assert clamp_level(1) == 1
    assert clamp_level(5) == 5
    assert clamp_level(9) == 5, "超出上限归到 5"
    assert clamp_level(0) is None
    assert clamp_level(None) is None


# ---------------------------------------------------------------------------
# 2. TP 时刻与档位
# ---------------------------------------------------------------------------
def test_tp_level_counts_positions_at_tp_moment():
    """档位 = 最终止盈那一刻的持仓笔数。"""
    async def _main():
        await _seed_martingale("sig_a", 1, magic=5001, node_id="node_a", levels=3)
        from app import signal_tp_levels
        return await signal_tp_levels.tp_units([1])

    units = _run(_main())
    assert len(units) == 1
    u = units[0]
    assert u["tp_level"] == 3
    assert u["tp_state"] == "tp"
    assert u["node_id"] == "node_a"
    assert u["signal_id"] == "sig_a"


def test_single_position_is_level_one():
    """只有首仓就止盈 → 1 档。"""
    async def _main():
        await _seed_martingale("sig_a", 1, magic=5001, node_id="node_a", levels=1)
        from app import signal_tp_levels
        return await signal_tp_levels.tp_units([1])

    units = _run(_main())
    assert units[0]["tp_level"] == 1


def test_tp_moment_is_the_earliest_take_profit_deal():
    """TP 时刻取最早的止盈成交；更晚的止盈成交不改变档位。"""
    async def _main():
        await _seed_martingale("sig_a", 1, magic=5001, node_id="node_a", levels=3)
        # 再补一笔更晚的止盈成交（另一个持仓）
        await _seed_deal(
            1, magic=5001, node_id="node_a", position_id=1999,
            deal_id=19990, entry_type="OUT", reason=5,
            deal_time=1_700_000_500,
        )
        from app import signal_tp_levels
        return await signal_tp_levels.tp_units([1])

    units = _run(_main())
    assert units[0]["tp_at"] == 1_700_000_100
    assert units[0]["tp_level"] == 3


def test_stop_loss_produces_no_level():
    """止损收口 → 不产生档位（不进止盈分布）。"""
    async def _main():
        await _seed_signal("sig_sl")
        await _seed_task("sig_sl", 2)
        await _seed_dispatch(2, node_id="node_a", magic=5002, signal_id="sig_sl")
        await _seed_deal(
            2, magic=5002, node_id="node_a", position_id=2001,
            deal_id=20010, entry_type="IN", reason=0,
            deal_time=1_700_000_000, order_id=20010,
        )
        await _seed_deal(
            2, magic=5002, node_id="node_a", position_id=2001,
            deal_id=20011, entry_type="OUT", reason=4,
            deal_time=1_700_000_100,
        )
        from app import signal_tp_levels
        return await signal_tp_levels.tp_units([2])

    units = _run(_main())
    assert units[0]["tp_level"] is None
    assert units[0]["tp_state"] == "sl"


def test_running_position_has_no_level():
    """仍在持仓（无止盈成交）→ 档位为 None，状态 running。"""
    async def _main():
        await _seed_signal("sig_run")
        await _seed_task("sig_run", 3)
        await _seed_dispatch(3, node_id="node_a", magic=5003, signal_id="sig_run")
        await _seed_deal(
            3, magic=5003, node_id="node_a", position_id=3001,
            deal_id=30010, entry_type="IN", reason=0,
            deal_time=1_700_000_000, order_id=30010,
        )
        from app import signal_tp_levels
        return await signal_tp_levels.tp_units([3])

    units = _run(_main())
    assert units[0]["tp_level"] is None
    assert units[0]["tp_state"] == "running"


def test_partial_close_at_tp_moment_still_counts():
    """部分平仓：TP 时刻仍在场的才算数。"""
    async def _main():
        await _seed_signal("sig_p")
        await _seed_task("sig_p", 4)
        await _seed_dispatch(4, node_id="node_a", magic=5004, signal_id="sig_p")
        # 两笔仓：pid 4001 先平掉了，pid 4002 在 TP 时刻仍在场
        await _seed_deal(
            4, magic=5004, node_id="node_a", position_id=4001,
            deal_id=40010, entry_type="IN", reason=0,
            deal_time=1_700_000_000, order_id=40010,
        )
        await _seed_deal(
            4, magic=5004, node_id="node_a", position_id=4002,
            deal_id=40020, entry_type="IN", reason=0,
            deal_time=1_700_000_010, order_id=40020,
        )
        await _seed_deal(
            4, magic=5004, node_id="node_a", position_id=4001,
            deal_id=40011, entry_type="OUT", reason=0,
            deal_time=1_700_000_050,
        )
        await _seed_deal(
            4, magic=5004, node_id="node_a", position_id=4002,
            deal_id=40021, entry_type="OUT", reason=5,
            deal_time=1_700_000_100,
        )
        from app import signal_tp_levels
        return await signal_tp_levels.tp_units([4])

    units = _run(_main())
    assert units[0]["tp_level"] == 1, "已平掉的那笔不计入档位"


# ---------------------------------------------------------------------------
# 3. 按节点分开统计
# ---------------------------------------------------------------------------
def test_same_signal_on_two_nodes_counted_separately():
    """同一信号在两个节点上档位不同 → **各算各的**，不合并。"""
    async def _main():
        await _seed_signal("sig_multi")
        await _seed_task("sig_multi", 5)
        await _seed_dispatch(5, node_id="node_a", magic=5005, signal_id="sig_multi")
        await _seed_dispatch(5, node_id="node_b", magic=5006, signal_id="sig_multi")
        # node_a：3 仓止盈
        for i in range(3):
            pid = 5000 + i
            await _seed_deal(
                5, magic=5005, node_id="node_a", position_id=pid,
                deal_id=pid * 10 + 1, entry_type="IN", reason=0,
                deal_time=1_700_000_000 + i * 10, order_id=pid * 10 + 1,
            )
            await _seed_deal(
                5, magic=5005, node_id="node_a", position_id=pid,
                deal_id=pid * 10 + 2, entry_type="OUT", reason=5,
                deal_time=1_700_000_100,
            )
        # node_b：1 仓止盈
        await _seed_deal(
            5, magic=5006, node_id="node_b", position_id=6001,
            deal_id=60010, entry_type="IN", reason=0,
            deal_time=1_700_000_000, order_id=60010,
        )
        await _seed_deal(
            5, magic=5006, node_id="node_b", position_id=6001,
            deal_id=60011, entry_type="OUT", reason=5,
            deal_time=1_700_000_100,
        )
        from app import signal_tp_levels
        return await signal_tp_levels.tp_units([5])

    units = _run(_main())
    assert len(units) == 2
    by_node = {u["node_id"]: u for u in units}
    assert by_node["node_a"]["tp_level"] == 3
    assert by_node["node_b"]["tp_level"] == 1

    # 分布：两个节点各自一份，不合并
    from app.signal_tp_levels import build_distribution

    dist = build_distribution(units, node_names={"node_a": "账户A", "node_b": "账户B"})
    assert dist["closed_tp_units"] == 2
    assert len(dist["by_node"]) == 2
    a = next(n for n in dist["by_node"] if n["node_id"] == "node_a")
    b = next(n for n in dist["by_node"] if n["node_id"] == "node_b")
    assert a["node_name"] == "账户A"
    assert a["units"] == 1
    assert a["buckets"][2]["count"] == 1
    assert b["buckets"][0]["count"] == 1


# ---------------------------------------------------------------------------
# 4. 口径范围
# ---------------------------------------------------------------------------
def test_non_tpl_1_excluded():
    """非 AI 智能加仓（tpl_1）的任务不统计。"""
    async def _main():
        await _seed_signal("sig_x")
        await _seed_task("sig_x", 6, template_id="tpl_2")
        from app import signal_tp_levels
        return await signal_tp_levels.units_for_signals(["sig_x"])

    units = _run(_main())
    assert units == []


def test_distribution_denominator_only_counts_take_profits():
    """分布分母 = 已止盈的单元数；止损 / 持仓中不计入。"""
    from app.signal_tp_levels import build_distribution

    units = [
        {"node_id": "n1", "tp_level": 1, "tp_state": "tp"},
        {"node_id": "n1", "tp_level": 3, "tp_state": "tp"},
        {"node_id": "n2", "tp_level": None, "tp_state": "sl"},
        {"node_id": "n2", "tp_level": None, "tp_state": "running"},
    ]
    dist = build_distribution(units)
    assert dist["closed_tp_units"] == 2
    assert dist["states"]["tp"] == 2
    assert dist["states"]["sl"] == 1
    assert dist["states"]["running"] == 1
    assert dist["overall"]["buckets"][0]["count"] == 1
    assert dist["overall"]["buckets"][2]["count"] == 1
    assert dist["overall"]["buckets"][0]["pct"] == 50.0


def test_distribution_empty_is_zeroed():
    """空数据也要返回完整零值结构。"""
    from app.signal_tp_levels import build_distribution

    dist = build_distribution([])
    assert dist["closed_tp_units"] == 0
    assert dist["by_node"] == []
    assert len(dist["overall"]["buckets"]) == 5
    assert all(b["count"] == 0 for b in dist["overall"]["buckets"])
