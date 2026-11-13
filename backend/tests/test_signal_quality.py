"""信号质量分（`signal_quality`）的用例。

质量分 = 历史同类信号（类型×周期）的**信号级胜率**，小样本做贝叶斯收缩。

这个模块最关键的不是公式本身，而是**它与绩效面板的口径必须完全一致** ——
质量分和绩效面板是同一个数字的两种呈现，两者对不上，管理员就不信了。
所以本文件专门有一条用例钉死这件事。

覆盖：
1. 收缩公式的数值（样本足时≈原始胜率；样本少时向全局回归）；
2. 回退链（类型×周期 → 类型 → 全局 → 无先验）；
3. 样本量分档；
4. `n=0` 兜底返回 None（不是 0）；
5. **与 `signal_perf` 口径一致性**（同一批数据两边数字必须相等）。
"""
import asyncio
import pathlib

import pytest

from app.db import SessionLocal, engine, init_db
from app.orm import Deal, GroupSignalTask, SignalHistory

# conftest 把 MYSQL_DSN 指向 _test_api.db —— 必须用同一个文件，
# 否则引擎连的是别的库，表根本没建。
_TEST_DB = pathlib.Path(__file__).resolve().parent / "_test_api.db"


def _run(coro):
    """跑一个协程，并在收尾时 dispose 引擎。"""
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


@pytest.fixture(autouse=True)
def _clear_quality_cache():
    """质量分先验有 TTL 缓存 —— 每个用例都要从干净的缓存开始。"""
    from app import signal_quality

    signal_quality.clear_cache()
    yield
    signal_quality.clear_cache()


# ---------------------------------------------------------------------------
# 造数（与 test_signal_perf.py 同一套）
# ---------------------------------------------------------------------------
async def _seed_signal(signal_id: str, *, timeframe=None, signal_type=None,
                       status: str = "done") -> None:
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
            source="tradingview",
            model="strategy",
            timeframe=timeframe,
            signal_type=signal_type,
        ))
        await s.commit()


async def _seed_task(signal_id: str, task_id: int, *, status: str = "done",
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
            status=status,
            strategy_id=f"sty_{task_id}",
            strategy_snapshot_json={"template_id": template_id},
            trigger_source="tv_webhook",
            route_mode="direct",
        ))
        await s.commit()


async def _seed_deal(task_id: int, *, node_id: str, position_id: int, profit: float,
                     deal_id: int, entry_type: str = "OUT") -> None:
    async with SessionLocal() as s:
        s.add(Deal(
            node_id=node_id,
            deal_id=deal_id,
            order_id=None,
            position_id=position_id,
            symbol="XAUUSD",
            direction="BUY",
            entry_type=entry_type,
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


async def _seed_win(signal_id: str, task_id: int, *, timeframe=None,
                     signal_type=None, profit: float = 20.0) -> None:
    """一条「赢」的信号（信号级：合计 > 0）。"""
    await _seed_signal(signal_id, timeframe=timeframe, signal_type=signal_type)
    await _seed_task(signal_id, task_id)
    await _seed_deal(task_id, node_id="node_a", position_id=100 + task_id,
                     profit=profit, deal_id=task_id * 10 + 1)


async def _seed_loss(signal_id: str, task_id: int, *, timeframe=None,
                     signal_type=None, profit: float = -20.0) -> None:
    """一条「输」的信号。"""
    await _seed_signal(signal_id, timeframe=timeframe, signal_type=signal_type)
    await _seed_task(signal_id, task_id)
    await _seed_deal(task_id, node_id="node_a", position_id=100 + task_id,
                     profit=profit, deal_id=task_id * 10 + 1)


async def _prior():
    from app import signal_quality

    return await signal_quality.get_prior("all")


# ---------------------------------------------------------------------------
# 1. 收缩公式（纯函数）
# ---------------------------------------------------------------------------
def test_shrink_with_large_sample_equals_raw_rate():
    """样本足够时（n >> k）收缩后≈原始胜率。"""
    from app import signal_quality

    # 87 笔赢 51 → 58.62%；k=10、g=55% 时收缩到 58.25%
    assert signal_quality.shrink(51, 87, 55.0) == pytest.approx(58.25, abs=0.05)


def test_shrink_pulls_small_sample_toward_global():
    """小样本向全局回归：3 笔全赢不会变成 100 分。"""
    from app import signal_quality

    # (3×100 + 10×55) / (3+10) = 850/13 = 65.38
    score = signal_quality.shrink(3, 3, 55.0)
    assert score == pytest.approx(65.38, abs=0.05)
    assert score < 100.0
    assert score > 55.0, "3 笔全赢仍应高于全局先验"


def test_shrink_pulls_all_losses_toward_global():
    """小样本全亏也不会掉到 0 —— 向全局回归。"""
    from app import signal_quality

    # (0 + 10×55) / 13 = 42.31
    score = signal_quality.shrink(0, 3, 55.0)
    assert score == pytest.approx(42.31, abs=0.05)
    assert score > 0.0
    assert score < 55.0, "3 笔全亏仍应低于全局先验"


def test_shrink_with_zero_samples_returns_global():
    """n=0 → 直接等于全局先验。"""
    from app import signal_quality

    assert signal_quality.shrink(0, 0, 55.0) == 55.0


def test_shrink_clamps_to_valid_range():
    from app import signal_quality

    assert 0.0 <= signal_quality.shrink(0, 1, 0.0) <= 100.0
    assert 0.0 <= signal_quality.shrink(1, 1, 100.0) <= 100.0


def test_shrink_handles_garbage_input():
    """脏输入不抛异常。"""
    from app import signal_quality

    assert signal_quality.shrink(-1, -1, 55.0) == 55.0
    assert signal_quality.shrink("x", "y", 55.0) == 0.0


# ---------------------------------------------------------------------------
# 2. 样本量分档
# ---------------------------------------------------------------------------
def test_sample_level_thresholds():
    from app import signal_quality

    assert signal_quality.sample_level(0) == "none"
    assert signal_quality.sample_level(3) == "insufficient"
    assert signal_quality.sample_level(5) == "thin"
    assert signal_quality.sample_level(19) == "thin"
    assert signal_quality.sample_level(20) == "sufficient"
    assert signal_quality.sample_level(500) == "sufficient"


# ---------------------------------------------------------------------------
# 3. 回退链
# ---------------------------------------------------------------------------
def test_no_prior_returns_none():
    """没有任何历史 → 返回 None（**不是 0 分**）。"""
    async def _main():
        return await _prior()

    prior = _run(_main())
    from app import signal_quality

    assert signal_quality.score_for("ob", "M15", prior) is None


def test_fallback_to_global_when_bucket_too_small():
    """桶样本不足 → 回退到全局。"""
    async def _main():
        await _seed_win("sig_a", 1, timeframe="M15", signal_type="ob")
        await _seed_win("sig_b", 2, timeframe="M15", signal_type="ob")
        await _seed_win("sig_c", 3, timeframe="M15", signal_type="ob")
        return await _prior()

    prior = _run(_main())
    from app import signal_quality

    q = signal_quality.score_for("ob", "M15", prior)
    assert q is not None
    assert q["level"] == "global", "3 笔 < MIN_SAMPLES(5) → 应回退全局"
    assert q["basis"] == "__global__"
    assert q["basis_label"] == "全部信号"


def test_fallback_to_type_bucket_when_type_tf_too_small():
    """类型×周期样本不足、但同类型跨周期够 → 回退到「仅类型」桶。"""
    async def _main():
        # ob·M15 只有 2 笔（不够）
        await _seed_win("sig_a", 1, timeframe="M15", signal_type="ob")
        await _seed_win("sig_b", 2, timeframe="M15", signal_type="ob")
        # ob·H1 有 8 笔（够）→ 类型桶 ob 的样本量 = 10
        for i in range(8):
            await _seed_win(f"sig_h{i}", 100 + i, timeframe="H1", signal_type="ob")
        return await _prior()

    prior = _run(_main())
    from app import signal_quality

    q = signal_quality.score_for("ob", "M15", prior)
    assert q is not None
    assert q["level"] == "type", "主桶 2 笔不足 → 应回退到「仅类型」桶"
    assert q["n"] == 10, "类型桶样本 = 同类型跨周期合计"


def test_main_bucket_used_when_enough_samples():
    """主桶样本足够 → 直接命中，不再回退。"""
    async def _main():
        for i in range(8):
            await _seed_win(f"sig_{i}", 100 + i, timeframe="M15", signal_type="ob")
        return await _prior()

    prior = _run(_main())
    from app import signal_quality

    q = signal_quality.score_for("ob", "M15", prior)
    assert q is not None
    assert q["level"] == "type_tf"
    assert q["n"] == 8
    assert q["basis"] == "ob|M15"
    assert q["basis_label"] == "订单块 OB · 15 分钟"


def test_unlabeled_type_and_timeframe_uses_none_bucket():
    """未标注类型 / 周期 → 落到 `__none__` 桶；样本够则命中主桶。"""
    async def _main():
        for i in range(6):
            await _seed_win(f"sig_{i}", 100 + i, timeframe=None, signal_type=None)
        return await _prior()

    prior = _run(_main())
    from app import signal_quality

    q = signal_quality.score_for(None, None, prior)
    assert q is not None
    assert q["level"] == "type_tf", "未标注类型也归 `__none__` 桶，样本够则命中主桶"
    assert q["n"] == 6
    assert q["basis"] == "__none__|__none__"


def test_missing_timeframe_falls_back_to_type_bucket():
    """信号没标周期，但历史上同类型有样本 → 回退到「仅类型」桶。

    这正是回退链存在的意义：TV 告警偶尔漏标周期，不能因此就查不到历史。
    """
    async def _main():
        # 历史：ob·M15 有 6 笔
        for i in range(6):
            await _seed_win(f"sig_{i}", 120 + i, timeframe="M15", signal_type="ob")
        return await _prior()

    prior = _run(_main())
    from app import signal_quality

    q = signal_quality.score_for("ob", None, prior)
    assert q is not None
    assert q["level"] == "type", "周期缺失 → 回退到「仅类型」桶"
    assert q["n"] == 6


# ---------------------------------------------------------------------------
# 4. 与绩效面板口径一致（本文件最重要的一条）
# ---------------------------------------------------------------------------
def test_quality_matches_signal_perf_win_rate():
    """**质量分与绩效面板必须逐位相等**（同一批数据、同一个口径）。

    质量分是「把绩效前置成可排序的参考」，不是另起一套算法。
    一旦两边对不上，管理员就再也不信这个数字了。
    """
    async def _main():
        # 10 条 ob·M15：6 赢 4 输 → 60%
        for i in range(6):
            await _seed_win(f"sig_w{i}", 200 + i, timeframe="M15", signal_type="ob")
        for i in range(4):
            await _seed_loss(f"sig_l{i}", 300 + i, timeframe="M15", signal_type="ob")
        return await _prior()

    prior = _run(_main())

    async def _perf():
        from app import signal_perf, signal_stats

        return await signal_perf.performance(
            signal_stats.SignalFilters(window="all")
        )

    perf = _run(_perf())

    # 绩效面板：按信号类型的 ob 桶
    ob_bucket = next(
        b for b in perf["by_signal_type"] if b["key"] == "ob"
    )
    assert ob_bucket["signal_win_rate"] == 60.0
    assert ob_bucket["signals_closed"] == 10
    assert ob_bucket["signals_win"] == 6

    # 质量分：同一个桶的胜率必须逐位相等
    from app import signal_quality

    q = signal_quality.score_for("ob", "M15", prior)
    assert q is not None
    assert q["n"] == 10
    assert q["wins"] == 6
    assert q["win_rate"] == ob_bucket["signal_win_rate"], "两边胜率必须相等"

    # 先验表里的那一行也必须一致
    row = next(b for b in prior["buckets"] if b["key"] == "ob|M15")
    assert row["win_rate"] == ob_bucket["signal_win_rate"]
    assert row["n"] == ob_bucket["signals_closed"] - ob_bucket["signals_no_trade"]
    assert row["wins"] == ob_bucket["signals_win"]


def test_global_prior_matches_perf_totals():
    """全局先验 = 绩效面板的 totals。"""
    async def _main():
        for i in range(7):
            await _seed_win(f"sig_w{i}", 400 + i, timeframe="M15", signal_type="ob")
        for i in range(3):
            await _seed_loss(f"sig_l{i}", 500 + i, timeframe="H1", signal_type="fvg")
        return await _prior()

    prior = _run(_main())

    async def _perf():
        from app import signal_perf, signal_stats

        return await signal_perf.performance(
            signal_stats.SignalFilters(window="all")
        )

    perf = _run(_perf())
    totals = perf["totals"]
    assert prior["global"]["n"] == totals["signals_closed"] - totals["signals_no_trade"]
    assert prior["global"]["wins"] == totals["signals_win"]
    assert prior["global"]["win_rate"] == totals["signal_win_rate"]


def test_non_tpl_1_excluded_from_prior():
    """非 tpl_1 的信号不进先验（与绩效面板同口径）。"""
    async def _main():
        for i in range(6):
            await _seed_win(f"sig_w{i}", 600 + i, timeframe="M15", signal_type="ob")
        # 非 tpl_1
        await _seed_signal("sig_x", timeframe="M15", signal_type="ob")
        await _seed_task("sig_x", 900, template_id="tpl_2")
        await _seed_deal(900, node_id="node_a", position_id=999, profit=100.0, deal_id=9999)
        return await _prior()

    prior = _run(_main())
    assert prior["global"]["n"] == 6, "tpl_2 的信号不进先验"


# ---------------------------------------------------------------------------
# 5. 先验表结构
# ---------------------------------------------------------------------------
def test_prior_buckets_have_expected_shape():
    async def _main():
        for i in range(6):
            await _seed_win(f"sig_{i}", 700 + i, timeframe="M15", signal_type="ob")
        return await _prior()

    prior = _run(_main())
    assert prior["window"] == "all"
    assert len(prior["buckets"]) == 1
    row = prior["buckets"][0]
    assert row["key"] == "ob|M15"
    assert row["signal_type"] == "ob"
    assert row["timeframe"] == "M15"
    assert row["label"] == "订单块 OB · 15 分钟"
    assert row["n"] == 6
    assert row["wins"] == 6
    assert row["sample_level"] == "thin"
    assert prior["updated_at"] > 0


def test_prior_empty_database_returns_zeroed_shape():
    """空库也要返回完整零值结构（前端直接读 fields，不能拿到 undefined）。"""
    prior = _run(_prior())
    assert prior["buckets"] == []
    assert prior["global"]["n"] == 0
    assert prior["global"]["win_rate"] == 0.0


def test_running_signals_not_in_prior_denominator():
    """进行中的信号不进先验分母（与绩效面板一致）。"""
    async def _main():
        for i in range(6):
            await _seed_win(f"sig_{i}", 800 + i, timeframe="M15", signal_type="ob")
        # 进行中
        await _seed_signal("sig_run", timeframe="M15", signal_type="ob")
        await _seed_task("sig_run", 850, status="running")
        await _seed_deal(850, node_id="node_a", position_id=850, profit=5.0, deal_id=8888)
        return await _prior()

    prior = _run(_main())
    row = next(b for b in prior["buckets"] if b["key"] == "ob|M15")
    assert row["n"] == 6, "进行中不进分母"
    assert prior["global"]["n"] == 6


def test_types_in_group_expands_all_members():
    """业务分类 → 它包含的**全部**类型码。

    「结构分析」下面有 ob / fvg / pdz 三个码 —— 按分类筛选时
    必须全部展开，只取一个码会把另外两个漏掉。
    """
    from app import signal_meta

    assert set(signal_meta.types_in_group("结构分析")) == {"ob", "fvg", "pdz"}
    assert signal_meta.types_in_group("突破信号") == ["bos"]
    assert signal_meta.types_in_group("不存在的分类") == []
    assert signal_meta.types_in_group(None) == []


def test_signal_type_group_labels():
    """信号类型 → 业务分类；未登记的码归「其它」但**不丢原码**。"""
    from app import signal_meta

    assert signal_meta.signal_type_group("ob") == "结构分析"
    assert signal_meta.signal_type_group("FVG") == "结构分析"
    assert signal_meta.signal_type_group("bos") == "突破信号"
    assert signal_meta.signal_type_group("unknown_code") == "其它"
    assert signal_meta.normalize_signal_type("unknown_code") == "unknown_code"


def test_signal_name_and_action_label():
    """可读信号名与方向标签的拼装。"""
    from app import signal_meta

    assert signal_meta.action_label("BUY") == "多"
    assert signal_meta.action_label("SELL") == "空"
    assert "看涨" in signal_meta.signal_name("BUY", "ob")
    assert "订单块" in signal_meta.signal_name("BUY", "ob")
    assert "看跌" in signal_meta.signal_name("SELL", "fvg")
    assert signal_meta.signal_name(None, None) == "未标注信号"


# ---------------------------------------------------------------------------
# 6. 可靠性等级（绝对阈值）
# ---------------------------------------------------------------------------
def test_level_thresholds_are_absolute():
    """等级用**绝对阈值**：≥60 高 / 50-60 中 / <50 低。

    刻意不用「相对全局先验」—— 相对阈值会随全局先验漂移，
    同一个 58 分这周算「高」下周算「中」，管理员对不上号。
    """
    from app import signal_quality

    assert signal_quality.level_for(60.0) == "high"
    assert signal_quality.level_for(75.5) == "high"
    assert signal_quality.level_for(100.0) == "high"
    assert signal_quality.level_for(50.0) == "mid"
    assert signal_quality.level_for(59.99) == "mid"
    assert signal_quality.level_for(49.99) == "low"
    assert signal_quality.level_for(0.0) == "low"


def test_level_none_distinguished_from_zero():
    """`None`（暂无历史）与「分数是 0」必须区分开。"""
    from app import signal_quality

    assert signal_quality.level_for(None) == "none"
    assert signal_quality.level_for(0.0) == "low"


def test_level_handles_garbage_input():
    """脏输入不抛异常。"""
    from app import signal_quality

    assert signal_quality.level_for("x") == "none"
    assert signal_quality.level_for(float("nan")) in ("high", "mid", "low", "none")


def test_level_advice_and_labels():
    """等级 → 决策指导文案 / 中文标签。"""
    from app import signal_quality

    assert signal_quality.advice_for("high") == "关注"
    assert signal_quality.advice_for("mid") == "观察"
    assert signal_quality.advice_for("low") == "暂不参与"
    assert signal_quality.advice_for("none") == "暂无历史"
    assert signal_quality.level_label("high") == "高"
    assert signal_quality.level_label("mid") == "中"
    assert signal_quality.level_label("low") == "低"


def test_score_levels_bundle():
    """一次拿到分数 + 等级 + 文案。"""
    from app import signal_quality

    out = signal_quality.score_levels(72.3)
    assert out["level"] == "high"
    assert out["level_label"] == "高"
    assert out["advice"] == "关注"

    out = signal_quality.score_levels(None)
    assert out["level"] == "none"
    assert out["advice"] == "暂无历史"
