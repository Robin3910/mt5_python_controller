"""**AI 信号管理中心**（`signal_ai_center`）的用例。

这个模块的价值全在**口径一致性**上，所以用例盯三件事：
1. 指标卡口径（今日按 UTC+8、覆盖周期不写分母、平均质量只算有先验的）；
2. 等级分布与逐条信号判定的等级**必须一致**（同一个 `level_for`）；
3. 业务分类聚合（结构分析 = ob + fvg + pdz）与分类过滤必须对得上。
"""
import asyncio
import pathlib

import pytest

from app.db import SessionLocal, engine, init_db
from app.orm import Deal, GroupSignalTask, SignalHistory

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


@pytest.fixture(autouse=True)
def _clear_quality_cache():
    from app import signal_quality

    signal_quality.clear_cache()
    yield
    signal_quality.clear_cache()


# ---------------------------------------------------------------------------
# 造数
# ---------------------------------------------------------------------------
async def _seed_signal(signal_id: str, *, timeframe="M15", signal_type="ob",
                       action="BUY", symbol="XAUUSD") -> None:
    async with SessionLocal() as s:
        s.add(SignalHistory(
            signal_id=signal_id,
            source_ip="127.0.0.1",
            raw_payload="{}",
            action=action,
            symbol=symbol,
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


async def _seed_deal(task_id: int, *, node_id: str, position_id: int, profit: float,
                     deal_id: int) -> None:
    async with SessionLocal() as s:
        s.add(Deal(
            node_id=node_id,
            deal_id=deal_id,
            order_id=None,
            position_id=position_id,
            symbol="XAUUSD",
            direction="BUY",
            entry_type="OUT",
            reason=0,
            volume=0.1,
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


async def _seed_win(signal_id: str, task_id: int, *, timeframe="M15",
                     signal_type="ob", profit: float = 20.0) -> None:
    await _seed_signal(signal_id, timeframe=timeframe, signal_type=signal_type)
    await _seed_task(signal_id, task_id)
    await _seed_deal(task_id, node_id="node_a", position_id=100 + task_id,
                     profit=profit, deal_id=task_id * 10 + 1)


# ---------------------------------------------------------------------------
# 1. 空库也要有完整零值结构
# ---------------------------------------------------------------------------
def test_overview_empty_database_returns_zeroed_shape():
    """空库时前端直接读字段，不能拿到 undefined。"""
    async def _main():
        from app import signal_ai_center, signal_stats

        return await signal_ai_center.overview(signal_stats.SignalFilters(window="all"))

    ov = _run(_main())
    assert ov["kpi"]["today_signals"] == 0
    assert ov["kpi"]["timeframes_covered"] == 0
    assert ov["kpi"]["high_reliability"] == 0
    assert ov["kpi"]["avg_quality"] == 0.0
    assert len(ov["reliability"]) == 3
    assert len(ov["score_buckets"]) == 4
    assert ov["by_type"] == []
    assert ov["by_timeframe"] == []
    assert ov["top_signals"] == []
    assert ov["scope"], "必须带口径说明"


def test_signals_page_empty_database():
    async def _main():
        from app import signal_ai_center, signal_stats

        return await signal_ai_center.signals_page(
            signal_stats.SignalFilters(window="all"), page=1, page_size=10,
        )

    page = _run(_main())
    assert page["total"] == 0
    assert page["page"] == 1
    assert page["pages"] == 1
    assert page["items"] == []


# ---------------------------------------------------------------------------
# 2. 指标卡口径
# ---------------------------------------------------------------------------
def test_kpi_counts_signals_and_timeframes():
    """覆盖周期 = 窗口内出现过的**不同周期数**（不写分母）。"""
    async def _main():
        await _seed_signal("s1", timeframe="M15")
        await _seed_signal("s2", timeframe="M15")
        await _seed_signal("s3", timeframe="H1")
        from app import signal_ai_center, signal_stats

        return await signal_ai_center.overview(signal_stats.SignalFilters(window="all"))

    ov = _run(_main())
    assert ov["kpi"]["signals_total"] == 3
    assert ov["kpi"]["timeframes_covered"] == 2, "M15 + H1 = 2 个周期"


def test_avg_quality_only_counts_scored_signals():
    """平均质量只算**有可用先验**的信号（没先验的不参与，也不拉低均值）。

    注意：回退链里「全局兜底」也会给出分数 ——
    只要全局先验有样本，任何信号都查得到分（这是 `signal_quality` 的既定行为）。
    真正「无分数」的是**全局先验也没有样本**的情况（空库）。
    """
    async def _main():
        # 6 条 ob·M15 全赢 → 先验有样本
        for i in range(6):
            await _seed_win(f"w{i}", 200 + i, timeframe="M15", signal_type="ob")
        # 1 条 fvg·H1：没有同类历史 → 回退到全局桶，仍有分数
        await _seed_signal("x1", timeframe="H1", signal_type="fvg")
        from app import signal_ai_center, signal_stats

        return await signal_ai_center.overview(signal_stats.SignalFilters(window="all"))

    ov = _run(_main())
    kpi = ov["kpi"]
    assert kpi["signals_total"] == 7
    assert kpi["signals_scored"] == 7, "全局兜底也给分数"
    assert kpi["avg_quality"] > 0


def test_avg_quality_is_zero_when_no_prior_at_all():
    """**全局先验都没有样本**时，信号一律无分数 → 均值为 0，不拉低别处的均值。"""
    async def _main():
        # 只有信号、没有 tpl_1 的任务 → 先验为空
        await _seed_signal("x1", timeframe="H1", signal_type="fvg")
        from app import signal_ai_center, signal_stats

        return await signal_ai_center.overview(signal_stats.SignalFilters(window="all"))

    ov = _run(_main())
    kpi = ov["kpi"]
    assert kpi["signals_total"] == 1
    assert kpi["signals_scored"] == 0, "没有可用先验 → 不计入均值"
    assert kpi["avg_quality"] == 0.0


def test_today_signals_counts_utc8_day():
    """今日触发信号按 **UTC+8 自然日**。"""
    async def _main():
        from app import signal_ai_center

        return await signal_ai_center._today_signal_count()

    # 空库 → 0
    assert _run(_main()) == 0


# ---------------------------------------------------------------------------
# 3. 等级分布与逐条判定必须一致
# ---------------------------------------------------------------------------
def test_reliability_distribution_matches_signal_level():
    """等级分布的条数 = 逐条信号判为该等级的条数。"""
    async def _main():
        for i in range(6):
            await _seed_win(f"w{i}", 300 + i, timeframe="M15", signal_type="ob")
        from app import signal_ai_center, signal_stats

        return await signal_ai_center.overview(signal_stats.SignalFilters(window="all"))

    ov = _run(_main())
    counts = {r["level"]: r["count"] for r in ov["reliability"]}
    assert sum(counts.values()) == ov["kpi"]["signals_total"]
    assert counts["high"] == ov["kpi"]["high_reliability"], "高可靠信号数必须等于等级分布里的「高」"


def test_high_reliability_matches_absolute_threshold():
    """高可靠 = 质量分 ≥ 60（绝对阈值）。"""
    async def _main():
        # 6 赢 → 先验分约 58.2（收缩后），落在「中」区间
        for i in range(6):
            await _seed_win(f"w{i}", 400 + i, timeframe="M15", signal_type="ob")
        from app import signal_ai_center, signal_stats

        return await signal_ai_center.overview(signal_stats.SignalFilters(window="all"))

    ov = _run(_main())
    from app import signal_quality

    for row in ov["top_signals"]:
        assert row["level"] == signal_quality.level_for(row["score"])
        assert row["advice"] == signal_quality.advice_for(row["level"])


# ---------------------------------------------------------------------------
# 4. 业务分类聚合与过滤
# ---------------------------------------------------------------------------
def test_type_aggregation_groups_structural_codes_together():
    """结构分析 = ob + fvg + pdz 合**一张卡**（原型是按分类展示的）。"""
    async def _main():
        await _seed_signal("s1", timeframe="M15", signal_type="ob")
        await _seed_signal("s2", timeframe="M15", signal_type="fvg")
        await _seed_signal("s3", timeframe="M15", signal_type="pdz")
        await _seed_signal("s4", timeframe="H1", signal_type="bos")
        from app import signal_ai_center, signal_stats

        return await signal_ai_center.overview(signal_stats.SignalFilters(window="all"))

    ov = _run(_main())
    by_label = {r["label"]: r for r in ov["by_type"]}
    assert "结构分析" in by_label
    assert by_label["结构分析"]["count"] == 3
    assert set(by_label["结构分析"]["types"]) == {"ob", "fvg", "pdz"}
    assert by_label["突破信号"]["count"] == 1


def test_timeframe_cards_use_readable_labels():
    """周期卡要用**人读标签**（4 小时），不能退化成裸代码 H4。"""
    async def _main():
        await _seed_signal("s1", timeframe="H4", signal_type="ob")
        await _seed_signal("s2", timeframe="M15", signal_type="ob")
        from app import signal_ai_center, signal_stats

        return await signal_ai_center.overview(signal_stats.SignalFilters(window="all"))

    ov = _run(_main())
    by_key = {r["key"]: r for r in ov["by_timeframe"]}
    assert by_key["H4"]["label"] == "4 小时"
    assert by_key["M15"]["label"] == "15 分钟"


def test_type_group_filter_returns_all_member_codes():
    """按分类筛明细时，分类下的**全部**类型码都要命中。"""
    async def _main():
        await _seed_signal("s1", timeframe="M15", signal_type="ob")
        await _seed_signal("s2", timeframe="M15", signal_type="fvg")
        await _seed_signal("s3", timeframe="M15", signal_type="pdz")
        await _seed_signal("s4", timeframe="H1", signal_type="bos")
        from app import signal_ai_center, signal_stats

        return await signal_ai_center.signals_page(
            signal_stats.SignalFilters(window="all"), type_group="结构分析",
        )

    page = _run(_main())
    assert page["total"] == 3, "结构分析的三个码都要命中"
    types = {r["signal_type"] for r in page["items"]}
    assert types == {"ob", "fvg", "pdz"}


def test_type_group_filter_unknown_group_matches_nothing():
    """未知分类 → 命中 0 条（不是「不过滤」）。"""
    async def _main():
        await _seed_signal("s1", timeframe="M15", signal_type="ob")
        from app import signal_ai_center, signal_stats

        return await signal_ai_center.signals_page(
            signal_stats.SignalFilters(window="all"), type_group="不存在的分类",
        )

    page = _run(_main())
    assert page["total"] == 0


# ---------------------------------------------------------------------------
# 5. 明细分页
# ---------------------------------------------------------------------------
def test_signals_page_pagination_slices_correctly():
    """页码分页：第 2 页拿到的必须是第 11~20 条（不是第 1~10 条）。"""
    async def _main():
        for i in range(25):
            await _seed_signal(f"s{i:02d}", timeframe="M15", signal_type="ob")
        from app import signal_ai_center, signal_stats

        f = signal_stats.SignalFilters(window="all")
        p1 = await signal_ai_center.signals_page(f, page=1, page_size=10)
        p2 = await signal_ai_center.signals_page(f, page=2, page_size=10)
        p3 = await signal_ai_center.signals_page(f, page=3, page_size=10)
        return p1, p2, p3

    p1, p2, p3 = _run(_main())
    assert p1["total"] == 25
    assert p1["pages"] == 3
    assert len(p1["items"]) == 10
    assert len(p2["items"]) == 10
    assert len(p3["items"]) == 5

    ids1 = {r["signal_id"] for r in p1["items"]}
    ids2 = {r["signal_id"] for r in p2["items"]}
    ids3 = {r["signal_id"] for r in p3["items"]}
    assert not (ids1 & ids2), "第 1 页与第 2 页不能有重复"
    assert not (ids2 & ids3)
    assert len(ids1 | ids2 | ids3) == 25


def test_signals_page_page_beyond_range_clamps():
    """页码越界 → 夹到最后一页，不报错也不返回空。"""
    async def _main():
        for i in range(3):
            await _seed_signal(f"s{i}", timeframe="M15", signal_type="ob")
        from app import signal_ai_center, signal_stats

        return await signal_ai_center.signals_page(
            signal_stats.SignalFilters(window="all"), page=99, page_size=10,
        )

    page = _run(_main())
    assert page["page"] == 1
    assert len(page["items"]) == 3
