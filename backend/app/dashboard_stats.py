"""控制端面板的汇总统计：今日信号数与平均时延。

数据源（两条分发链路都要算）：
  - normal 链路：`signal_dispatch`（一条信号 × 一个节点一行）
  - strategy 链路：`group_task_dispatch`（一个主任务 × 一个节点一行）

口径与前端展示直接对应：
  - **今日信号**：今天（UTC+8 自然日）内**下发到节点**的信号条数。
    用 `sent_at`（服务端发包时刻）而不是 `received_at`/`dispatched_at`：
    前者跨链路一致，且是唯一「服务端自己的时钟」，不受节点本地时钟影响。
  - **平均延迟**：**命令往返时延** —— 服务端发包 → 服务端收到节点回报的耗时（毫秒）。
    两端都在服务端，是跨节点唯一可比的时间轴
    （节点上报的 `recv_at`/`order_at`/`filled_at` 用的是节点本地时钟，不可跨机相减，
    见 `latency_marks.py` 文件头说明）。

    两条链路的「收到回报」取的是**不同的列**，因为语义不同：
      - normal 链路：`finished_at`。节点执行完 open 命令回报结果时收口。
      - strategy 链路：`opened_at`。节点首次回报「已开仓」时落库。
        这里**不能用** `finished_at` —— 那是策略持仓全部平掉、子任务收口的时刻，
        算出来是「持仓时长」（几十分钟到几天），不是「时延」。
        拿持仓当时延展示给运维看，数字会大到没有意义（实测出现过 3978000ms）。

    精度：埋点列在 MySQL 上是 DATETIME(3)（毫秒），SQLite 天然微秒。
    整秒精度会把「发包与回报同秒」记成 0ms，跨机往返时延根本看不出差别。
    精度迁移之前写的历史行只有整秒信息，误差可达 ±1 秒，**不计入**平均
    （见 `_has_sub_second`）。

为什么按「节点」维度出：控制端面板是**本机**运维视角，统计的是**本机这批节点**
今天跑了多少信号、平均往返多快，而不是全局。调用方按可见节点传 node_ids。
"""
from __future__ import annotations

import logging
import time
from datetime import datetime

from sqlalchemy import func, or_, select

from .db import SessionLocal
from .orm import GroupTaskDispatch, SignalDispatch

logger = logging.getLogger(__name__)

# 分日用的固定偏移（秒）：UTC+8，与 `deal_stats.py` 保持同一口径
_DAY_OFFSET = 8 * 3600
_DAY_SECONDS = 86400

# 伪失败：命令**根本没到节点**（连接断开）或**超时未回**。
# 这两种不是「命令往返」，计入会把均值带偏（离线≈0ms、超时≈超时上限），
# 所以正常链路的时延样本要把它们剔掉。
# 节点**回报了失败**（比如券商拒单）不算在内 —— 那是一次真实的往返。
_NOT_ROUND_TRIP_ERRORS = ("offline", "timeout")


def today_start_ts() -> int:
    """今天（UTC+8 自然日）00:00 的 Unix 秒。"""
    now = int(time.time())
    return ((now + _DAY_OFFSET) // _DAY_SECONDS) * _DAY_SECONDS - _DAY_OFFSET


def _today_start_dt() -> datetime:
    return datetime.fromtimestamp(today_start_ts())


def _has_sub_second(ts: datetime) -> bool:
    """这一列是否带毫秒信息。

    精度迁移（MySQL DATETIME → DATETIME(3)）之前写的行是整秒截断的，微秒位恒为 0；
    迁移之后写的行带毫秒，微秒位非 0（恰好落在整秒边界上的概率约千分之一）。

    用它把「精度不足的历史样本」剔出去 —— 那些样本的时延误差可达 ±1 秒，
    混进均值里会让「平均延迟」完全失真（实测会把 100ms 的真实往返算成 1000ms）。

    副作用是好的：万一某个环境漏跑了迁移，所有样本都会被判为精度不足，
    指标显示 `--`（没有可信样本），而不是给出一个错的数字。
    """
    return ts.microsecond != 0


async def node_daily_stats(node_ids: list[str]) -> dict[str, dict]:
    """每个节点的今日信号数与平均延迟（毫秒）。

    返回 `{node_id: {"today_signals": int, "avg_latency_ms": float | None}}`。
    **请求的每个节点都有一项**：没有数据的节点是 `today_signals=0`、
    `avg_latency_ms=None`（调用方不用再补缺省值，也不会把「没数据」当成 0 延迟）。
    """
    ids = [str(n) for n in (node_ids or []) if n]
    if not ids:
        return {}

    start = _today_start_dt()
    out: dict[str, dict] = {}

    # —— 今日信号数（两条链路各自统计后相加）——
    normal_counts = await _count_since(SignalDispatch, SignalDispatch.node_id,
                                       SignalDispatch.sent_at, ids, start)
    strategy_counts = await _count_since(GroupTaskDispatch, GroupTaskDispatch.node_id,
                                          GroupTaskDispatch.sent_at, ids, start)
    for nid in ids:
        out[nid] = {
            "today_signals": int(normal_counts.get(nid, 0)) + int(strategy_counts.get(nid, 0)),
            "avg_latency_ms": None,
        }

    # —— 平均延迟：命令往返时延（服务端时间轴两端）——
    # 两条链路的样本合并后再求均值：先各自求出「总和 + 样本数」，
    # 再按样本数加权合并。不能直接对两个均值取算术平均 ——
    # 那样只有 1 个样本的链路会和 100 个样本的链路等权，均值会被带偏。
    normal_lat = await _avg_latency(SignalDispatch, SignalDispatch.node_id,
                                     SignalDispatch.sent_at, SignalDispatch.finished_at,
                                     ids, start,
                                     exclude_pseudo_failures=True)
    strategy_lat = await _avg_latency(GroupTaskDispatch, GroupTaskDispatch.node_id,
                                       GroupTaskDispatch.sent_at, GroupTaskDispatch.opened_at,
                                       ids, start)
    for nid in ids:
        total = 0.0
        count = 0
        for bucket in (normal_lat.get(nid), strategy_lat.get(nid)):
            if bucket:
                total += bucket[0]   # 该链路的耗时总和
                count += bucket[1]   # 该链路的样本数
        if count:
            out[nid]["avg_latency_ms"] = total / count
    return out


async def _count_since(model, node_col, ts_col, node_ids: list[str], start: datetime) -> dict[str, int]:
    """按节点统计 `ts_col >= start` 的行数。"""
    try:
        async with SessionLocal() as s:
            rows = (
                await s.execute(
                    select(node_col, func.count())
                    .where(node_col.in_(node_ids), ts_col.is_not(None), ts_col >= start)
                    .group_by(node_col)
                )
            ).all()
            return {str(nid): int(cnt) for nid, cnt in rows}
    except Exception as e:  # noqa: BLE001
        logger.warning("node_daily_stats count failed: %s", e)
        return {}


async def _avg_latency(model, node_col, sent_col, finished_col,
                       node_ids: list[str], start: datetime,
                       exclude_pseudo_failures: bool = False) -> dict[str, tuple[float, int]]:
    """按节点统计 `sent_at → 回报时刻` 的往返耗时（毫秒）。

    返回 `{node_id: (耗时总和, 样本数)}` —— 保留样本数是为了让调用方按样本数加权
    合并两条链路，而不是对两个均值取算术平均。

    只统计两端都齐全的行：缺 `sent_at`（没发出去）或缺回报时刻（还没回报）
    的行不能算平均，否则会把「在途」当成 0 耗时，把均值压低。

    `exclude_pseudo_failures=True` 时额外剔除「离线 / 超时」这类没走完往返的伪失败。
    策略链路不需要这个开关：它的回报时刻 `opened_at` 只有节点真的开仓了才会有。
    """
    try:
        async with SessionLocal() as s:
            conditions = [
                node_col.in_(node_ids),
                sent_col.is_not(None),
                finished_col.is_not(None),
                sent_col >= start,
            ]
            if exclude_pseudo_failures:
                # status != 'failed' 才保留；status='failed' 时再看 error：
                # error 为空（节点回报的失败）保留，为 offline/timeout 则剔除。
                conditions.append(
                    or_(
                        model.status != "failed",
                        func.coalesce(model.error, "") .notin_(_NOT_ROUND_TRIP_ERRORS),
                    )
                )
            rows = (
                await s.execute(
                    select(node_col, sent_col, finished_col)
                    .where(*conditions)
                )
            ).all()
    except Exception as e:  # noqa: BLE001
        logger.warning("node_daily_stats latency failed: %s", e)
        return {}

    buckets: dict[str, list[float]] = {}
    for nid, sent_at, finished_at in rows:
        if sent_at is None or finished_at is None:
            continue
        if not _has_sub_second(sent_at) or not _has_sub_second(finished_at):
            # 精度不足的历史样本：误差可达 ±1 秒，混进均值会让指标失真
            continue
        ms = (finished_at - sent_at).total_seconds() * 1000.0
        if ms < 0:
            # 时钟回拨等异常值直接丢弃，不让均值被污染
            continue
        buckets.setdefault(str(nid), []).append(ms)
    return {
        nid: (sum(vals), len(vals))
        for nid, vals in buckets.items()
        if vals
    }
