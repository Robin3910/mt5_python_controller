"""信号的**质量分**：历史同类信号的成功率（小样本做收缩）。

一句话定义：

> 质量分 = 历史同类信号（相同「信号类型 + 周期」）的**信号级胜率**（0~100）

设计文档：`docs/superpowers/specs/2026-11-13-signal-quality-design.md`

## 为什么是这个定义

质量分是「把已经算出来的历史绩效，**前置**成一条可排序的参考」。
既然 `signal_perf` 已经把信号级胜率算对了，质量分就**不该另起一套算法** ——
同一个口径、同一个数字，只是换个位置呈现（列表里的一列 vs 面板里的一张表）。

**因此本模块不自己算胜率，而是复用 `signal_perf._aggregate`** ——
那是本系统里信号级胜率的**唯一实现**。两边各写一套聚合，迟早会因某天改了一处而
对不上；一旦对不上，管理员就再也不信这个数字了。

## 三条铁律

1. **不落库**：`signal_history` 不新增任何列。先验是**活的**（只在信号收口时才变），
   一期不做「当时是几分」的复盘。
2. **不参与交易决策**：质量分**只展示**，不拦截、不丢弃、不阻止路由。
   低分桶一旦被系统性过滤 → 样本不再更新 → 先验越来越陈旧 → 越筛越极端。
3. **先验不受筛选条影响**：固定按「近 N 天全量」计算，与 `SignalFilters` 无关。
   先验是**背景知识**，不是「当前筛选结果的统计」——
   否则管理员切一下筛选条，质量分就变了，同一条信号在不同 tab 显示不同分数。

## 口径（与 `signal_perf` 完全一致）

| 项 | 口径 |
|---|---|
| 统计范围 | 只算 **AI 智能加仓（tpl_1）** 的任务 |
| 判定 | 该信号**全部持仓**净盈亏合计 `> 0` → 赢（**信号级**） |
| 分母 | 已收口**且有成交**的信号数（`signals_closed - signals_no_trade`） |
| 进行中 | 不进分母（单列「进行中」） |

## 小样本：贝叶斯收缩

```
先验分 = (赢数 + k × 全局胜率) / (样本数 + k)
```

`k` = 10（先验强度 ≈ 相当于额外观察 10 个样本）。
样本多时退化成原始胜率，样本少时自动向全局回归 ——
3 笔 100% 胜率不会变成「100 分」把管理员骗了。

## 两个正交维度（别混）

- `level`：**回退到了哪一档**（`type_tf` / `type` / `global` / `none`）
- `sample_level`：**样本够不够**（`sufficient` / `thin` / `insufficient` / `none`）

一个桶可以是「主桶命中（`level=type_tf`）但样本偏少（`n=6` → `sample_level=thin`）」。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

from . import signal_meta, signal_perf

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 参数（一期默认值）
# ---------------------------------------------------------------------------

# 收缩强度：先验相当于额外观察多少个样本。越大越保守（越向全局均值回归）
SHRINKAGE_K = 10.0

# 某个桶至少要有多少样本才「单独成桶」；不足则回退到上一级
MIN_SAMPLES = 5

# 样本量分档阈值：>= 充分 / >= 偏少 / < 不足
SAMPLE_SUFFICIENT = 20

# 先验默认统计窗口（与信号中心筛选条无关，见模块 docstring 铁律 3）
DEFAULT_PRIOR_WINDOW = "30d"

# 先验缓存 TTL（秒）。先验只在信号收口时才变，60 秒足够新鲜。
CACHE_TTL = 60.0

# ---------------------------------------------------------------------------
# 回退层级
# ---------------------------------------------------------------------------

LEVEL_TYPE_TF = "type_tf"      # 命中「类型 × 周期」主桶
LEVEL_TYPE = "type"            # 回退到「仅类型」桶
LEVEL_GLOBAL = "global"        # 回退到全局
LEVEL_NONE = "none"            # 完全没有可用先验

SAMPLE_SUFFICIENT_LEVEL = "sufficient"
SAMPLE_THIN_LEVEL = "thin"
SAMPLE_INSUFFICIENT_LEVEL = "insufficient"
SAMPLE_NONE_LEVEL = "none"

# ---------------------------------------------------------------------------
# 可靠性等级（**绝对阈值**，与打分逻辑完全独立）
# ---------------------------------------------------------------------------
# 等级是对**已有质量分**的一层分级，**不参与打分** ——
# 打分口径只有上面那一个（历史同类信号的信号级胜率 + 贝叶斯收缩），
# 等级只是把分数翻译成「高 / 中 / 低」三档给人看。
#
# 阈值用**绝对值**而不是相对全局先验：
# 相对阈值会随全局先验漂移（同一个 58 分上周算「高」这周算「中」），
# 管理员对不上号；绝对阈值稳定、可解释、可复述。
LEVEL_HIGH = "high"
LEVEL_MID = "mid"
LEVEL_LOW = "low"
LEVEL_NONE = "none"

#: ≥ 该分数 → 高
LEVEL_HIGH_MIN = 60.0
#: ≥ 该分数 → 中（低于它 → 低）
LEVEL_MID_MIN = 50.0

#: 等级 → 决策指导文案
LEVEL_ADVICE: dict[str, str] = {
    LEVEL_HIGH: "关注",
    LEVEL_MID: "观察",
    LEVEL_LOW: "暂不参与",
    LEVEL_NONE: "暂无历史",
}

#: 等级 → 中文标签
LEVEL_LABELS: dict[str, str] = {
    LEVEL_HIGH: "高",
    LEVEL_MID: "中",
    LEVEL_LOW: "低",
    LEVEL_NONE: "暂无",
}

# 缓存：window -> (过期时刻, 先验)
_cache: dict[str, tuple[float, dict]] = {}


# ---------------------------------------------------------------------------
# 纯函数（便于单测）
# ---------------------------------------------------------------------------
def shrink(
    wins: int, n: int, global_rate: float, k: float = SHRINKAGE_K,
) -> float:
    """贝叶斯收缩后的胜率（0~100）。

    ```
    结果 = (赢数 × 100 + k × 全局胜率) / (样本数 + k)
    ```

    等价于 `(wins/n + k×g) / (n+k)`，只是把百分比换算放在分子上 ——
    这样 `global_rate` 直接传**百分数**（0~100），不用先除以 100。

    - `n <= 0` → 返回 `global_rate`（没有样本就等于全局先验）；
    - `k <= 0` → 退化为原始胜率（不收缩）。

    样本足够时（`n >> k`）结果≈原始胜率；样本很少时自动向 `global_rate` 回归。
    """
    try:
        n = int(n)
        wins = int(wins)
        g = float(global_rate)
        kk = float(k)
    except (TypeError, ValueError):
        return 0.0
    g = max(0.0, min(100.0, g))
    if n <= 0:
        return round(g, 2)
    if kk <= 0:
        rate = (wins / n * 100.0) if n else 0.0
        return round(max(0.0, min(100.0, rate)), 2)
    rate = (wins * 100.0 + kk * g) / (n + kk)
    return round(max(0.0, min(100.0, rate)), 2)


def sample_level(n: int) -> str:
    """样本量分档。"""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return SAMPLE_NONE_LEVEL
    if n <= 0:
        return SAMPLE_NONE_LEVEL
    if n >= SAMPLE_SUFFICIENT:
        return SAMPLE_SUFFICIENT_LEVEL
    if n >= MIN_SAMPLES:
        return SAMPLE_THIN_LEVEL
    return SAMPLE_INSUFFICIENT_LEVEL


def _bucket_key(signal_type: object, timeframe: object) -> tuple[str, str]:
    """(主键, 标签) —— 全部走 `signal_meta` 归一。

    主键 = `类型|周期`（周期缺失时用 `__none__`，与 `signal_perf` 的分桶一致）。
    """
    st = signal_meta.normalize_signal_type(signal_type) or "__none__"
    tf = signal_meta.normalize_timeframe(timeframe) or "__none__"
    st_label = signal_meta.signal_type_label(signal_type)
    tf_label = signal_meta.timeframe_label(timeframe)
    if tf == "__none__":
        label = st_label
    else:
        label = f"{st_label} · {tf_label}"
    return f"{st}|{tf}", label


# ---------------------------------------------------------------------------
# 先验计算
# ---------------------------------------------------------------------------
async def compute_prior(window: str = DEFAULT_PRIOR_WINDOW) -> dict:
    """算出全局先验 + 各桶先验。

    **不受信号中心的筛选条影响** —— 固定按 `window` 拉全量信号
    （见模块 docstring 铁律 3）。

    返回：
    {
      "window": "30d",
      "global": {"n": 412, "wins": 227, "win_rate": 55.1, "score": 55.1,
                 "avg_profit": 8.2},
      "buckets": [{"key": "ob|M15", "signal_type": "ob", "timeframe": "M15",
                   "label": "OB · M15", "n": 87, "wins": 51,
                   "win_rate": 58.6, "score": 58.2, "avg_profit": 12.4}, ...],
      "updated_at": 1731...
    }
    """
    from . import signal_stats as _stats

    f = _stats.SignalFilters(window=window)
    signals = await _stats._fetch_signals(f)
    result: dict[str, Any] = {
        "window": window,
        "global": {
            "n": 0, "wins": 0, "win_rate": 0.0, "score": 0.0, "avg_profit": 0.0,
        },
        "buckets": [],
        "updated_at": int(time.time()),
    }
    if not signals:
        return result

    tasks_by_signal = await signal_perf._signal_task_map(
        [s.signal_id for s in signals]
    )
    all_task_ids = [t["task_id"] for ts in tasks_by_signal.values() for t in ts]
    profit_by_task = await signal_perf._magic_profit_map(all_task_ids)

    def _key(sig: Any):
        return _bucket_key(sig.signal_type, sig.timeframe)

    # —— 全局桶 ——
    global_buckets = signal_perf._aggregate(
        signals, tasks_by_signal, profit_by_task, None,
    )
    g = global_buckets.get("__total__")
    global_n = int(g.signals_closed - g.signals_no_trade) if g else 0
    global_wins = int(g.signals_win) if g else 0
    global_rate = (global_wins / global_n * 100.0) if global_n > 0 else 0.0
    global_avg = (g.signal_profit / global_n) if (g and global_n > 0) else 0.0

    # —— 类型×周期桶 ——
    buckets = signal_perf._aggregate(
        signals, tasks_by_signal, profit_by_task, _key,
    )

    out: list[dict] = []
    for b in buckets.values():
        n = int(b.signals_closed - b.signals_no_trade)
        wins = int(b.signals_win)
        avg = (b.signal_profit / n) if n > 0 else 0.0
        # 从主键反解回原始值（`__none__` = 未标注）
        raw_st, raw_tf = b.key.split("|", 1) if "|" in b.key else (b.key, None)
        out.append({
            "key": b.key,
            "signal_type": raw_st if raw_st != "__none__" else None,
            "timeframe": raw_tf if raw_tf != "__none__" else None,
            "label": b.label,
            "n": n,
            "wins": wins,
            "win_rate": round(b.signal_win_rate, 2),
            "score": shrink(wins, n, global_rate),
            "avg_profit": round(avg, 2),
            "sample_level": sample_level(n),
        })

    out.sort(key=lambda x: (-x["n"], x["key"]))
    result["global"] = {
        "n": global_n,
        "wins": global_wins,
        "win_rate": round(global_rate, 2),
        "score": round(global_rate, 2),
        "avg_profit": round(global_avg, 2),
        "sample_level": sample_level(global_n),
    }
    result["buckets"] = out
    return result


def _index_prior(prior: dict) -> dict:
    """把先验整理成查表结构：{主键: bucket} + {类型键: 聚合桶}。

    **类型桶是「同类型跨周期」的聚合**，不是「样本最多的那个周期」：
    一个类型在 M15 有 2 笔、H1 有 8 笔，类型桶就该是 10 笔 ——
    取单个最大桶会漏掉其它周期的样本，回退链第二档就名存实亡了。
    """
    by_tf: dict[str, dict] = {}
    type_acc: dict[str, dict] = {}
    for b in prior.get("buckets") or []:
        key = str(b.get("key") or "")
        if not key:
            continue
        by_tf[key] = b
        st = str(b.get("signal_type") or "__none__")
        acc = type_acc.setdefault(st, {
            "key": st, "signal_type": st if st != "__none__" else None,
            "timeframe": None, "label": "", "n": 0, "wins": 0,
            "win_rate": 0.0, "score": 0.0, "avg_profit": 0.0,
            "sample_level": "none",
        })
        acc["n"] += int(b.get("n") or 0)
        acc["wins"] += int(b.get("wins") or 0)
        acc["avg_profit"] = (
            (float(b.get("avg_profit") or 0.0) * int(b.get("n") or 0)) / acc["n"]
            if acc["n"] else 0.0
        )
    # 类型桶的标签是「类型名」（不含周期 —— 它是跨周期聚合的）
    for st, acc in type_acc.items():
        acc["label"] = signal_meta.signal_type_label(
            None if st == "__none__" else st
        )
    # 聚合桶的胜率 = 赢数 / 样本数（不能平均各周期的胜率 —— 那是加权错误）
    for st, acc in type_acc.items():
        n = acc["n"]
        acc["win_rate"] = round(acc["wins"] / n * 100.0, 2) if n else 0.0
        acc["sample_level"] = sample_level(n)
    return {"by_tf": by_tf, "by_type": type_acc}


# ---------------------------------------------------------------------------
# 打分
# ---------------------------------------------------------------------------
def score_for(
    signal_type: object,
    timeframe: object,
    prior: dict,
    *,
    min_samples: int = MIN_SAMPLES,
    k: float = SHRINKAGE_K,
) -> Optional[dict]:
    """给一条信号打分。**纯函数**（先验由调用方算好传进来）。

    回退链：
      ① `(type, tf)`  n ≥ min_samples  → 用这一档
      ② `type`       n ≥ min_samples  → 用这一档
      ③ 全局          n ≥ 1            → 用这一档
      ④ 都没有                          → 返回 None（前端显示「暂无历史」）

    返回 `None` 表示**没有可用先验** —— 与「分数是 0」是两件事，必须区分。
    """
    g = prior.get("global") or {}
    global_rate = float(g.get("win_rate") or 0.0)
    global_n = int(g.get("n") or 0)

    st = signal_meta.normalize_signal_type(signal_type)
    tf = signal_meta.normalize_timeframe(timeframe)
    st_key = st or "__none__"
    tf_key = tf or "__none__"

    idx = _index_prior(prior)
    by_tf = idx["by_tf"]
    by_type = idx["by_type"]

    # —— 回退链 ——
    main = by_tf.get(f"{st_key}|{tf_key}")
    if main is not None and int(main.get("n") or 0) >= min_samples:
        return _build(main, LEVEL_TYPE_TF, global_rate, global_n, k)

    type_bucket = by_type.get(st_key)
    if type_bucket is not None and int(type_bucket.get("n") or 0) >= min_samples:
        return _build(type_bucket, LEVEL_TYPE, global_rate, global_n, k)

    if global_n >= 1:
        return _build(g, LEVEL_GLOBAL, global_rate, global_n, k)

    return None


def _build(
    bucket: dict, level: str, global_rate: float, global_n: int, k: float,
) -> dict:
    n = int(bucket.get("n") or 0)
    wins = int(bucket.get("wins") or 0)
    win_rate = float(bucket.get("win_rate") or 0.0)
    return {
        "score": shrink(wins, n, global_rate, k),
        "win_rate": round(win_rate, 2),
        "n": n,
        "wins": wins,
        "avg_profit": round(float(bucket.get("avg_profit") or 0.0), 2),
        "basis": str(bucket.get("key") or "") if level != LEVEL_GLOBAL else "__global__",
        "basis_label": (
            str(bucket.get("label") or "") if level != LEVEL_GLOBAL else "全部信号"
        ),
        "level": level,
        "sample_level": sample_level(n),
    }


# ---------------------------------------------------------------------------
# 带缓存的入口
# ---------------------------------------------------------------------------
async def get_prior(window: str = DEFAULT_PRIOR_WINDOW) -> dict:
    """取先验（带 TTL 缓存）。"""
    key = str(window or DEFAULT_PRIOR_WINDOW)
    now = time.time()
    hit = _cache.get(key)
    if hit is not None and hit[0] > now:
        return hit[1]
    try:
        prior = await compute_prior(key)
    except Exception as e:  # noqa: BLE001
        logger.warning("signal_quality.compute_prior failed: %s", e)
        prior = {
            "window": key,
            "global": {"n": 0, "wins": 0, "win_rate": 0.0, "score": 0.0,
                       "avg_profit": 0.0, "sample_level": SAMPLE_NONE_LEVEL},
            "buckets": [],
            "updated_at": int(now),
        }
    _cache[key] = (now + CACHE_TTL, prior)
    return prior


async def quality_map(
    signals: list[Any], *, window: str = DEFAULT_PRIOR_WINDOW,
) -> dict[tuple[str, str], Optional[dict]]:
    """批量给一批信号打分 → `{(类型, 周期): quality}`。

    **同一组 (类型, 周期) 只算一次** —— 信号流一页 20 条可能有 15 条同类型，
    没必要重复查先验。
    """
    prior = await get_prior(window)
    out: dict[tuple[str, str], Optional[dict]] = {}
    for sig in signals:
        st = signal_meta.normalize_signal_type(getattr(sig, "signal_type", None))
        tf = signal_meta.normalize_timeframe(getattr(sig, "timeframe", None))
        key = (st or "", tf or "")
        if key in out:
            continue
        out[key] = score_for(
            getattr(sig, "signal_type", None),
            getattr(sig, "timeframe", None),
            prior,
        )
    return out


def clear_cache() -> None:
    """清空先验缓存（测试用 / 手动强制刷新用）。"""
    _cache.clear()


# ---------------------------------------------------------------------------
# 可靠性等级（绝对阈值）
# ---------------------------------------------------------------------------
def level_for(score: Optional[float]) -> str:
    """把质量分翻译成可靠性等级。

    **绝对阈值**：
    - `score >= 60` → 高（`high`）
    - `50 <= score < 60` → 中（`mid`）
    - `score < 50` → 低（`low`）
    - `score is None` → 无（`none`，即「暂无历史」）

    等级**不参与打分**，只是展示层的一层翻译；
    没有可用先验（`None`）与「分数是 0」必须区分开 ——
    0 分是「历史同类信号全亏」，`none` 是「压根没有同类历史」。
    """
    if score is None:
        return LEVEL_NONE
    try:
        v = float(score)
    except (TypeError, ValueError):
        return LEVEL_NONE
    if v >= LEVEL_HIGH_MIN:
        return LEVEL_HIGH
    if v >= LEVEL_MID_MIN:
        return LEVEL_MID
    return LEVEL_LOW


def advice_for(level: str) -> str:
    """等级 → 决策指导文案。"""
    return LEVEL_ADVICE.get(str(level or ""), LEVEL_ADVICE[LEVEL_NONE])


def level_label(level: str) -> str:
    """等级 → 中文标签。"""
    return LEVEL_LABELS.get(str(level or ""), LEVEL_LABELS[LEVEL_NONE])


def score_levels(score: Optional[float]) -> dict:
    """一次拿到分数 + 等级 + 文案（前端展示直接用）。"""
    lv = level_for(score)
    return {
        "level": lv,
        "level_label": level_label(lv),
        "advice": advice_for(lv),
    }
