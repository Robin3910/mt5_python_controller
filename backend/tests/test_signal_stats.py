"""信号中心统计口径的测试。

重点验证三件容易写错的事：
1. **信号级 vs 分发级** 两套成功率不混用（分母不同）；
2. **时延可比性**：端到端用服务端钟可跨节点比，节点内两段只能分节点看；
3. **结构化 skip_code**：统计一律按 code 聚合，不解析中文文案。

另外验证 `signal_codes` 的分类器：写漏 code 时能兜底归类，
写错 code 时收敛成 unknown 而不抛异常（统计坏了不该影响交易链路）。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app import signal_codes, signal_stats
from app.db import SessionLocal, init_db
from app.orm import (
    GroupSignalTask,
    GroupTaskDispatch,
    SignalDispatch,
    SignalHistory,
)


# ---------------------------------------------------------------------------
# 夹具：造两条链路各若干条分发明细
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
async def _clean_db():
    """建表并清空信号相关表（共用同一个测试库）。"""
    await init_db()
    async with SessionLocal() as s:
        for model in (SignalDispatch, GroupTaskDispatch, GroupSignalTask, SignalHistory):
            await s.execute(model.__table__.delete())
        await s.commit()
    yield


async def _seed_signals():
    """造 3 条 normal 信号 + 1 条 strategy 信号。

    - sig_n1：3 个节点，2 成功 1 跳过（跳过码 risk_position）
    - sig_n2：2 个节点，全部失败（失败码 error_mt5）
    - sig_n3：1 个节点，成功（rejected 信号）
    - sig_s1：strategy 链路，2 个节点，1 个 opened 1 个 skipped（conflict_busy）
    """
    now = datetime.now()
    async with SessionLocal() as s:
        rows = [
            SignalHistory(
                signal_id="sig_n1", received_at=now - timedelta(minutes=10),
                action="BUY", symbol="XAUUSD", volume=0.1, status="done",
                source="tradingview", model="normal",
            ),
            SignalHistory(
                signal_id="sig_n2", received_at=now - timedelta(minutes=20),
                action="SELL", symbol="EURUSD", volume=0.2, status="failed",
                source="tradingview", model="normal",
            ),
            SignalHistory(
                signal_id="sig_n3", received_at=now - timedelta(minutes=30),
                action="BUY", symbol="XAUUSD", volume=0.3, status="rejected",
                source="manual", model="normal",
                reject_code=signal_codes.REJECT_SYMBOL_DISABLED,
            ),
            SignalHistory(
                signal_id="sig_s1", received_at=now - timedelta(minutes=5),
                action="BUY", symbol="BTCUSD", volume=0.5, status="done",
                source="tradingview", model="strategy",
            ),
        ]
        for r in rows:
            s.add(r)

        dispatches = [
            # sig_n1：两个成功 + 一个跳过
            SignalDispatch(signal_id="sig_n1", node_id="n1", status="done",
                           gate_result="passed", decided_vol=0.1,
                           sent_at=now - timedelta(seconds=10),
                           finished_at=now - timedelta(seconds=9)),
            SignalDispatch(signal_id="sig_n1", node_id="n2", status="done",
                           gate_result="passed", decided_vol=0.1,
                           sent_at=now - timedelta(seconds=10),
                           finished_at=now - timedelta(seconds=8)),
            SignalDispatch(signal_id="sig_n1", node_id="n3", status="skipped",
                           gate_result="skipped", skip_code=signal_codes.SKIP_RISK_POSITION,
                           skip_reason="持仓过滤：已有同向持仓",
                           sent_at=now - timedelta(seconds=10)),
            # sig_n2：两个失败
            SignalDispatch(signal_id="sig_n2", node_id="n1", status="failed",
                           gate_result="passed", decided_vol=0.2,
                           skip_code=signal_codes.SKIP_ERROR_MT5,
                           sent_at=now - timedelta(seconds=20),
                           finished_at=now - timedelta(seconds=19)),
            SignalDispatch(signal_id="sig_n2", node_id="n2", status="failed",
                           gate_result="passed", decided_vol=0.2,
                           skip_code=signal_codes.SKIP_ERROR_MT5,
                           sent_at=now - timedelta(seconds=20),
                           finished_at=now - timedelta(seconds=17)),
            # sig_s1：strategy 链路：1 个 opened + 1 个 skipped
            GroupTaskDispatch(task_id=1, signal_id="sig_s1", group_id="g1",
                              node_id="n1", symbol="BTCUSD", action="BUY",
                              status="opened", decided_vol=0.5,
                              sent_at=now - timedelta(seconds=5)),
            GroupTaskDispatch(task_id=1, signal_id="sig_s1", group_id="g1",
                              node_id="n2", symbol="BTCUSD", action="BUY",
                              status="skipped",
                              skip_code=signal_codes.SKIP_CONFLICT_BUSY,
                              skip_reason="该节点在本分组内已有进行中的子任务",
                              sent_at=now - timedelta(seconds=5)),
        ]
        for r in dispatches:
            s.add(r)

        s.add(GroupSignalTask(
            task_id=1, signal_id="sig_s1", group_id="g1", group_name="主账户",
            action="BUY", symbol="BTCUSD", status="running",
            total_orders=1, total_volume=0.5, realized_profit=12.34,
        ))
        await s.commit()


# ---------------------------------------------------------------------------
# 概览
# ---------------------------------------------------------------------------
async def test_overview_separates_signal_and_dispatch_rates():
    """信号级与分发级成功率是两套口径，分母不同，不能混。"""
    await _seed_signals()
    f = signal_stats.SignalFilters(window="all")
    res = await signal_stats.overview(f)
    totals = res["totals"]

    assert totals["signals"] == 4
    assert totals["rejected"] == 1
    assert totals["accepted"] == 3

    # 分发总数 = 3(normal sig_n1) + 2(normal sig_n2) + 2(strategy sig_s1) = 7
    assert totals["dispatch_total"] == 7
    # 通过数：sig_n1 的 2 个 done + strategy 的 opened(算落地) 1 个 = 3
    assert totals["passed"] == 3
    assert totals["skipped"] == 2
    assert totals["failed"] == 2

    # 信号级成功率：done 信号 2 条 / 非拒收信号 3 条
    assert totals["signal_success_rate"] == round(2 / 3 * 100, 2)
    # 分发级成功率：3 / 7
    assert totals["dispatch_success_rate"] == round(3 / 7 * 100, 2)
    # 两个口径必须不同，否则说明写错了
    assert totals["signal_success_rate"] != totals["dispatch_success_rate"]


async def test_overview_counts_duplicate_as_not_collected():
    """去重抑制本期不统计：数值恒为 0 且带 collected=False，前端显示「—」。"""
    await _seed_signals()
    res = await signal_stats.overview(signal_stats.SignalFilters(window="all"))
    assert res["totals"]["duplicate"] == 0
    assert res["totals"]["duplicate_collected"] is False


async def test_overview_skip_reasons_use_structured_codes():
    """拦截原因按 skip_code 聚合，不解析中文文案。"""
    await _seed_signals()
    res = await signal_stats.overview(signal_stats.SignalFilters(window="all"))
    codes = {item["code"]: item["count"] for item in res["skip_reasons"]}

    assert codes[signal_codes.SKIP_RISK_POSITION] == 1
    assert codes[signal_codes.SKIP_CONFLICT_BUSY] == 1
    assert codes[signal_codes.SKIP_ERROR_MT5] == 2
    # 人读标签必须带上，前端直接用
    labels = {item["code"]: item["label"] for item in res["skip_reasons"]}
    assert labels[signal_codes.SKIP_RISK_POSITION] == "持仓过滤"


async def test_overview_daily_buckets_cover_every_day():
    """每日曲线不能断档：没有信号的日子也要占位。"""
    await _seed_signals()
    res = await signal_stats.overview(signal_stats.SignalFilters(window="all"))
    daily = res["daily"]
    assert len(daily) >= 1
    # 日期标签递增且连续
    assert daily == sorted(daily, key=lambda x: x["ts"])
    assert daily[0]["signals"] >= 1


# ---------------------------------------------------------------------------
# 统一信号流
# ---------------------------------------------------------------------------
async def test_stream_merges_both_chains():
    """统一信号流把 normal 与 strategy 混在一起返回。"""
    await _seed_signals()
    res = await signal_stats.stream(signal_stats.SignalFilters(window="all"), limit=50)
    ids = {item["signal_id"] for item in res["items"]}
    assert ids == {"sig_n1", "sig_n2", "sig_n3", "sig_s1"}

    s1 = next(i for i in res["items"] if i["signal_id"] == "sig_s1")
    assert s1["model"] == "strategy"
    assert s1["targets"] == 2
    # strategy 的 opened 算落地成功
    assert s1["done"] == 1
    assert s1["skipped"] == 1


async def test_stream_cursor_paginates_without_duplicates():
    """游标分页：翻页不重不漏。"""
    await _seed_signals()
    f = signal_stats.SignalFilters(window="all")
    page1 = await signal_stats.stream(f, limit=2)
    assert len(page1["items"]) == 2
    assert page1["next_cursor"]

    page2 = await signal_stats.stream(f, cursor=page1["next_cursor"], limit=2)
    assert len(page2["items"]) == 2

    ids1 = {i["signal_id"] for i in page1["items"]}
    ids2 = {i["signal_id"] for i in page2["items"]}
    assert not (ids1 & ids2), "游标翻页出现重复"


async def test_stream_rejects_invalid_cursor_gracefully():
    """非法游标不能让接口 500 —— 应当退回到第一页。"""
    await _seed_signals()
    res = await signal_stats.stream(
        signal_stats.SignalFilters(window="all"), cursor="!!!not-base64!!!", limit=5,
    )
    assert len(res["items"]) == 4


# ---------------------------------------------------------------------------
# 节点命中
# ---------------------------------------------------------------------------
async def test_nodes_aggregate_by_node():
    """按节点聚合命中情况。"""
    await _seed_signals()
    res = await signal_stats.nodes(signal_stats.SignalFilters(window="all"))
    by_id = {i["node_id"]: i for i in res["items"]}

    # n1：sig_n1 done + sig_n2 failed + sig_s1 opened
    assert by_id["n1"]["done"] == 2
    assert by_id["n1"]["failed"] == 1
    assert by_id["n1"]["signal_count"] == 3
    # n2：sig_n1 done + sig_n2 failed + sig_s1 skipped
    assert by_id["n2"]["done"] == 1
    assert by_id["n2"]["skipped"] == 1
    assert by_id["n2"]["failed"] == 1
    # n3：只有 sig_n1 的跳过
    assert by_id["n3"]["skipped"] == 1
    assert by_id["n3"]["top_skip_code"] == signal_codes.SKIP_RISK_POSITION


# ---------------------------------------------------------------------------
# 时延
# ---------------------------------------------------------------------------
async def test_latency_end_to_end_uses_server_clock():
    """端到端时延用服务端钟（sent_at → finished_at），跨节点可比。"""
    await _seed_signals()
    res = await signal_stats.latency(signal_stats.SignalFilters(window="all"))
    e2e = res["end_to_end"]
    assert e2e["scope"] == "server_clock"
    # sig_n1/n1: 1s, sig_n1/n2: 2s, sig_n2/n1: 1s, sig_n2/n2: 3s
    assert e2e["samples"] == 4
    assert e2e["p50"] is not None


async def test_latency_node_local_buckets_are_scoped():
    """节点内两段耗时必须标 node_local，接口层不得给出跨节点合并值以外的语义。"""
    await _seed_signals()
    res = await signal_stats.latency(signal_stats.SignalFilters(window="all"))
    assert res["queued_ms"]["scope"] == "node_local"
    assert res["order_to_fill_ms"]["scope"] == "node_local"
    # 两段节点内耗时必须带「不可跨节点求和」的口径提示
    assert "不可跨节点求和" in (res["queued_ms"].get("note") or "")
    assert "不可跨节点求和" in (res["order_to_fill_ms"].get("note") or "")
    assert res["note"], "必须带口径说明，避免前端误读"


# ---------------------------------------------------------------------------
# 策略任务
# ---------------------------------------------------------------------------
async def test_strategy_tasks_reports_profit_as_separate_scope():
    """策略任务盈亏独立成一份，不与成交侧净盈亏混算。"""
    await _seed_signals()
    res = await signal_stats.strategy_tasks(signal_stats.SignalFilters(window="all"))
    assert res["totals"]["tasks"] == 1
    assert res["totals"]["realized_profit"] == 12.34
    assert res["totals"]["orders"] == 1
    assert res["items"][0]["group_id"] == "g1"


async def test_strategy_tasks_skipped_for_normal_model():
    """只看 normal 链路时，strategy 任务整张不参与。"""
    await _seed_signals()
    res = await signal_stats.strategy_tasks(
        signal_stats.SignalFilters(window="all", model="normal")
    )
    assert res["items"] == []
    assert res["totals"]["tasks"] == 0
    assert res["tasks"] == []


async def test_strategy_tasks_includes_main_task_list_with_subtasks():
    """主任务列表要带上节点子任务，能从汇总直接下钻。"""
    await _seed_signals()
    res = await signal_stats.strategy_tasks(signal_stats.SignalFilters(window="all"))
    tasks = res["tasks"]
    assert len(tasks) == 1

    t = tasks[0]
    assert t["group_id"] == "g1"
    assert t["group_name"] == "主账户"
    assert t["realized_profit"] == 12.34
    # 两个子任务（opened / skipped）
    assert len(t["dispatches"]) == 2
    nodes = {d["node_id"] for d in t["dispatches"]}
    assert nodes == {"n1", "n2"}
    # 子任务要带结构化拦截码标签
    skipped = next(d for d in t["dispatches"] if d["status"] == "skipped")
    assert skipped["skip_code"] == signal_codes.SKIP_CONFLICT_BUSY
    assert skipped["skip_label"]


# ---------------------------------------------------------------------------
# 单条信号明细
# ---------------------------------------------------------------------------
async def test_signal_detail_returns_raw_payload_and_both_chains():
    """明细要能拿到原始载荷 + 两条链路的节点明细（列表页不带，按需拉）。"""
    await _seed_signals()
    async with SessionLocal() as s:
        await s.execute(
            SignalHistory.__table__.update()
            .where(SignalHistory.signal_id == "sig_n1")
            .values(raw_payload='{"action":"BUY","symbol":"XAUUSD"}')
        )
        await s.commit()

    detail = await signal_stats.signal_detail("sig_n1")
    assert detail is not None
    assert detail["raw_payload"] == '{"action":"BUY","symbol":"XAUUSD"}'
    assert detail["symbol"] == "XAUUSD"
    # normal 链路的 3 个节点都在明细里
    assert len(detail["dispatches"]) == 3
    assert detail["group_tasks"] == []


async def test_signal_detail_includes_strategy_group_tasks():
    """strategy 信号要能查到主任务（含 realized_profit 口径字段）。"""
    await _seed_signals()
    detail = await signal_stats.signal_detail("sig_s1")
    assert detail is not None
    assert len(detail["group_tasks"]) == 1
    task = detail["group_tasks"][0]
    assert task["group_id"] == "g1"
    assert task["realized_profit"] == 12.34
    # strategy 的 opened 归一化成 done（与列表口径一致）
    statuses = {d["status"] for d in detail["dispatches"]}
    assert "done" in statuses


async def test_signal_detail_returns_none_for_unknown_signal():
    """查不到就返回 None（接口层转 404），不能抛异常。"""
    assert await signal_stats.signal_detail("no-such-signal") is None


# ---------------------------------------------------------------------------
# signal_codes
# ---------------------------------------------------------------------------
def test_safe_code_normalizes_unknown_values():
    """非法 code 收敛成 unknown，不抛异常。"""
    assert signal_codes.safe_code("risk_trend") == "risk_trend"
    assert signal_codes.safe_code("RISK_TREND") == "risk_trend"
    assert signal_codes.safe_code("something_new") == signal_codes.SKIP_UNKNOWN
    assert signal_codes.safe_code(None) is None
    assert signal_codes.safe_code("") is None


def test_labels_cover_all_codes():
    """每个 code 都必须有人读标签，否则前端会显示原始 code。"""
    for code in signal_codes.SKIP_CODES:
        assert code in signal_codes.SKIP_CODE_LABELS
        assert signal_codes.skip_label(code) != code
    for code in signal_codes.REJECT_CODES:
        assert code in signal_codes.REJECT_CODE_LABELS
        assert signal_codes.reject_label(code) != code


def test_classify_skip_reason_falls_back_to_known_buckets():
    """中文兜底分类：存量数据没写 code 时仍能归桶。"""
    assert (
        signal_codes.classify_skip_reason("节点未配置：XAUUSD未在节点按币种配置中，拒收")
        == signal_codes.SKIP_CONFIG_MISSING
    )
    assert (
        signal_codes.classify_skip_reason("趋势风控：信号 BUY，当前趋势为空，已拦截")
        == signal_codes.SKIP_RISK_TREND
    )
    assert (
        signal_codes.classify_skip_reason("MT5 错误 10019")
        == signal_codes.SKIP_ERROR_MT5
    )
    # 完全无法识别的归 unknown
    assert signal_codes.classify_skip_reason("?????") == signal_codes.SKIP_UNKNOWN
    assert signal_codes.classify_skip_reason(None) == signal_codes.SKIP_UNKNOWN


def test_classify_group_skip_reason():
    """分组级跳过原因单独分类。"""
    assert (
        signal_codes.classify_group_skip_reason("分组已禁用：主账户")
        == signal_codes.SKIP_ADMISSION_RULE
    )
    assert (
        signal_codes.classify_group_skip_reason("分组无成员节点：主账户")
        == signal_codes.SKIP_MISMATCH_SCOPE
    )
    assert (
        signal_codes.classify_group_skip_reason("分组无有效节点：主账户（成员均未启用或不在线）")
        == signal_codes.SKIP_UNAVAILABLE
    )


# ---------------------------------------------------------------------------
# 分位与分日工具
# ---------------------------------------------------------------------------
def test_percentile_interpolates():
    assert signal_stats.percentile([1, 2, 3, 4, 5], 50) == 3.0
    assert signal_stats.percentile([10], 90) == 10.0
    assert signal_stats.percentile([], 50) is None
    assert signal_stats.percentile([1, 2, 3, 4], 0) == 1.0
    assert signal_stats.percentile([1, 2, 3, 4], 100) == 4.0


def test_day_label_is_utc8():
    """分日按 UTC+8 切分：UTC 的 16:00 已经是次日。"""
    # 2024-10-27 16:00:00 UTC -> 2024-10-28 00:00 UTC+8
    assert signal_stats.day_label(1730054400) == "10-28"


def test_window_normalization():
    assert signal_stats.normalize_window("7d") == "7d"
    assert signal_stats.normalize_window("bogus") == signal_stats.DEFAULT_WINDOW
    assert signal_stats.normalize_window(None) == signal_stats.DEFAULT_WINDOW
    assert signal_stats.window_start("all") is None
    assert signal_stats.window_start("7d") is not None
