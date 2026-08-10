"""趋势面板指标（纯计算，无 I/O，可单独单元测试）。

节点详情「趋势面板」把 EMA 与 RSI 两个指标合成一个 -100 ~ +100 的趋势得分：
- EMA 定方向：当前价相对 EMA 的偏离，偏离越大方向越明确（默认权重 70%）
- RSI 定动能：高于偏多起始线为正、低于偏空起始线为负，中性区不表态（默认权重 30%）

两个指标各自先算出 -100 ~ +100 的原始分，再乘权重相加，因此「最高贡献」始终等于
权重 × 100（默认即 ±70 与 ±30），权重改了范围自动跟着改。

得分只用于展示，不参与下单、过滤与任何分发决策。参数权威存库
（system_setting.trend_config，全后台共享、不分节点/币种），结构与校验都收在
本模块，路由层只做编排。
"""
from __future__ import annotations

import math

# 可选 K 线周期 -> 每根秒数。与节点侧 mt5_client.TIMEFRAMES 的键保持一致，
# 节点解析不了的周期会返回空 K 线，因此入口就把范围限死。
TIMEFRAME_SECONDS: dict[str, int] = {
    "M1": 60, "M5": 300, "M15": 900, "M30": 1800,
    "H1": 3600, "H4": 14400, "D1": 86400, "W1": 604800, "MN": 2592000,
}

TREND_BULLISH = "bullish"
TREND_BEARISH = "bearish"
TREND_NEUTRAL = "neutral"
TREND_UNKNOWN = "unknown"  # K 线不足，无法判定（不等于中性）

RSI_OVERBOUGHT = "overbought"
RSI_OVERSOLD = "oversold"

# K 线根数上下限：下限保证 EMA/RSI 有收敛空间，上限护住单次回传体积与终端查询开销
MIN_VIEW_BARS = 30
MAX_VIEW_BARS = 500
MAX_BARS = 1000
# 计算所需根数向上取整到该倍数，让微调周期时仍能落回同一个行情缓存键
BARS_QUANTUM = 50
# 高周期历史往往不全，MT5 为补齐会同步拉服务器数据并卡住 Python GIL；
# 心跳发不出 → WS ping 超时 → 节点被判离线。按周期收紧探针根数上限。
TIMEFRAME_BAR_CAPS: dict[str, int] = {
    "M1": 1000, "M5": 1000, "M15": 1000, "M30": 1000,
    "H1": 800, "H4": 500, "D1": 400, "W1": 150, "MN": 80,
}

DEFAULTS: dict[str, float | int | str] = {
    "timeframe": "M15",
    "ema_period": 50,
    "rsi_period": 14,
    "ema_weight": 0.7,
    "rsi_weight": 0.3,
    # 价格偏离 EMA 达到该百分比即视为方向满分（不同品种波动差异大，故开放配置）
    "ema_full_scale_pct": 0.2,
    "rsi_bull": 60.0,
    "rsi_bear": 40.0,
    "rsi_overbought": 70.0,
    "rsi_oversold": 30.0,
    "bullish": 20.0,
    "bearish": -20.0,
    "bars": 120,
}

CONFIG_KEYS = tuple(DEFAULTS.keys())


def default_config() -> dict:
    return dict(DEFAULTS)


def _as_float(value: object, fallback: float) -> float:
    try:
        num = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return float(fallback)
    return float(fallback) if math.isnan(num) or math.isinf(num) else num


def _as_int(value: object, fallback: int) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return int(fallback)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def normalize_config(raw: dict | None) -> dict:
    """把任意入参整理成稳定且自洽的配置；未知字段丢弃，非法值回落默认。

    夹取之外还做三处关系修正，避免存进库的配置本身就是矛盾的：
    权重之和归一化到 1、RSI 偏多线必须高于偏空线、超买超卖不得反压在偏多偏空线内侧。
    """
    src = raw if isinstance(raw, dict) else {}

    timeframe = str(src.get("timeframe") or DEFAULTS["timeframe"]).strip().upper()
    if timeframe not in TIMEFRAME_SECONDS:
        timeframe = str(DEFAULTS["timeframe"])

    ema_period = int(_clamp(_as_int(src.get("ema_period"), int(DEFAULTS["ema_period"])), 2, 400))
    rsi_period = int(_clamp(_as_int(src.get("rsi_period"), int(DEFAULTS["rsi_period"])), 2, 100))

    ema_weight = _clamp(_as_float(src.get("ema_weight"), float(DEFAULTS["ema_weight"])), 0.0, 1.0)
    rsi_weight = _clamp(_as_float(src.get("rsi_weight"), float(DEFAULTS["rsi_weight"])), 0.0, 1.0)
    total = ema_weight + rsi_weight
    if total <= 0:
        ema_weight, rsi_weight = float(DEFAULTS["ema_weight"]), float(DEFAULTS["rsi_weight"])
    else:
        ema_weight, rsi_weight = ema_weight / total, rsi_weight / total

    full_scale = _clamp(
        _as_float(src.get("ema_full_scale_pct"), float(DEFAULTS["ema_full_scale_pct"])),
        0.01, 10.0,
    )

    rsi_bull = _clamp(_as_float(src.get("rsi_bull"), float(DEFAULTS["rsi_bull"])), 50.0, 99.0)
    rsi_bear = _clamp(_as_float(src.get("rsi_bear"), float(DEFAULTS["rsi_bear"])), 1.0, 50.0)
    if rsi_bear >= rsi_bull:
        rsi_bear = min(rsi_bear, rsi_bull - 1.0)
    rsi_overbought = _clamp(
        _as_float(src.get("rsi_overbought"), float(DEFAULTS["rsi_overbought"])),
        rsi_bull, 100.0,
    )
    rsi_oversold = _clamp(
        _as_float(src.get("rsi_oversold"), float(DEFAULTS["rsi_oversold"])),
        0.0, rsi_bear,
    )

    bullish = _clamp(_as_float(src.get("bullish"), float(DEFAULTS["bullish"])), 0.0, 99.0)
    bearish = _clamp(_as_float(src.get("bearish"), float(DEFAULTS["bearish"])), -99.0, 0.0)
    bars = int(_clamp(_as_int(src.get("bars"), int(DEFAULTS["bars"])), MIN_VIEW_BARS, MAX_VIEW_BARS))

    return {
        "timeframe": timeframe,
        "ema_period": ema_period,
        "rsi_period": rsi_period,
        "ema_weight": round(ema_weight, 4),
        "rsi_weight": round(rsi_weight, 4),
        "ema_full_scale_pct": round(full_scale, 4),
        "rsi_bull": round(rsi_bull, 2),
        "rsi_bear": round(rsi_bear, 2),
        "rsi_overbought": round(rsi_overbought, 2),
        "rsi_oversold": round(rsi_oversold, 2),
        "bullish": round(bullish, 2),
        "bearish": round(bearish, 2),
        "bars": bars,
    }


def bars_needed(cfg: dict) -> int:
    """算这份配置要向节点取多少根已收盘 K 线。

    EMA 与 RSI 都是递归指标，种子值的影响需要若干个周期才衰减到可忽略，因此取远
    多于周期本身的根数；面板要显示的根数也要一并覆盖。高周期另受 TIMEFRAME_BAR_CAPS
    约束，避免向终端索取几十年月线/周线把节点卡死。
    """
    ema_period = int(cfg.get("ema_period") or DEFAULTS["ema_period"])
    rsi_period = int(cfg.get("rsi_period") or DEFAULTS["rsi_period"])
    view = int(cfg.get("bars") or DEFAULTS["bars"])
    timeframe = str(cfg.get("timeframe") or DEFAULTS["timeframe"]).upper()
    need = max(ema_period * 4, (rsi_period + 1) * 5, view)
    need = math.ceil(need / BARS_QUANTUM) * BARS_QUANTUM
    tf_cap = TIMEFRAME_BAR_CAPS.get(timeframe, MAX_BARS)
    return int(min(need, tf_cap, MAX_BARS))


def closes_of(bars: list[dict]) -> list[float]:
    """取收盘价序列；非法值按 0 处理，由调用方的长度校验兜住。"""
    return [_as_float((b or {}).get("close"), 0.0) for b in bars or []]


def ema_series(closes: list[float], period: int) -> list[float | None]:
    """EMA 序列，与入参等长；不足一个周期的位置为 None。

    种子取前 period 根的简单平均，之后按 2/(period+1) 的平滑系数递推。
    """
    n = len(closes)
    out: list[float | None] = [None] * n
    if period < 1 or n < period:
        return out
    k = 2.0 / (period + 1)
    prev = sum(closes[:period]) / period
    out[period - 1] = prev
    for i in range(period, n):
        prev = closes[i] * k + prev * (1.0 - k)
        out[i] = prev
    return out


def _rsi_from_avg(avg_gain: float, avg_loss: float) -> float:
    """由平均涨跌幅换算 RSI，并处理两个退化情形。

    完全没有跌幅时 RS 无定义：有涨幅按满分 100，涨跌都为 0（横盘）按 50 —— 此时
    多空力量确实相等，报 100 会把「没有行情」误读成「极强多头」。
    """
    if avg_loss <= 0:
        return 100.0 if avg_gain > 0 else 50.0
    if avg_gain <= 0:
        return 0.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def rsi_series(closes: list[float], period: int) -> list[float | None]:
    """RSI 序列（Wilder 平滑），与入参等长；不足 period+1 根的位置为 None。"""
    n = len(closes)
    out: list[float | None] = [None] * n
    if period < 1 or n < period + 1:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        delta = closes[i] - closes[i - 1]
        gains += max(delta, 0.0)
        losses += max(-delta, 0.0)
    avg_gain, avg_loss = gains / period, losses / period
    out[period] = _rsi_from_avg(avg_gain, avg_loss)
    for i in range(period + 1, n):
        delta = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(delta, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-delta, 0.0)) / period
        out[i] = _rsi_from_avg(avg_gain, avg_loss)
    return out


def ema_raw_score(price: float, ema_value: float, full_scale_pct: float) -> float:
    """EMA 原始分 -100 ~ +100：价格在 EMA 上方为正，偏离达到满分线即触顶。"""
    if price <= 0 or ema_value <= 0 or full_scale_pct <= 0:
        return 0.0
    deviation_pct = (price - ema_value) / ema_value * 100.0
    return _clamp(deviation_pct / full_scale_pct, -1.0, 1.0) * 100.0


def rsi_raw_score(rsi_value: float, bull: float, bear: float) -> float:
    """RSI 原始分 -100 ~ +100：偏多线以上为正、偏空线以下为负，两线之间为 0。"""
    if rsi_value >= bull:
        span = 100.0 - bull
        return 100.0 if span <= 0 else _clamp((rsi_value - bull) / span, 0.0, 1.0) * 100.0
    if rsi_value <= bear:
        return -100.0 if bear <= 0 else -_clamp((bear - rsi_value) / bear, 0.0, 1.0) * 100.0
    return 0.0


def rsi_state(rsi_value: float, cfg: dict) -> str:
    """RSI 状态文案用的分区：超买 / 超卖 / 偏多 / 偏空 / 中性。"""
    if rsi_value >= float(cfg["rsi_overbought"]):
        return RSI_OVERBOUGHT
    if rsi_value <= float(cfg["rsi_oversold"]):
        return RSI_OVERSOLD
    if rsi_value >= float(cfg["rsi_bull"]):
        return TREND_BULLISH
    if rsi_value <= float(cfg["rsi_bear"]):
        return TREND_BEARISH
    return TREND_NEUTRAL


def classify(score: float, cfg: dict) -> str:
    """综合得分 -> 趋势结论。"""
    if score > float(cfg["bullish"]):
        return TREND_BULLISH
    if score < float(cfg["bearish"]):
        return TREND_BEARISH
    return TREND_NEUTRAL


def evaluate(bars: list[dict], price: float, cfg: dict) -> dict:
    """算出趋势面板需要的全部数值。

    price 传当前报价（取不到时传 0，函数会回落到最后一根收盘价并在 price_source
    里标明）。K 线不足以算出指标时返回 ready=False、trend=unknown —— 把「算不出」
    和「中性」分开，面板才不会把缺数据显示成有结论。
    """
    cfg = normalize_config(cfg)
    rows = [b for b in (bars or []) if isinstance(b, dict)]
    closes = closes_of(rows)
    ema_line = ema_series(closes, int(cfg["ema_period"]))
    rsi_line = rsi_series(closes, int(cfg["rsi_period"]))
    ema_value = ema_line[-1] if ema_line else None
    rsi_value = rsi_line[-1] if rsi_line else None

    last_close = closes[-1] if closes else 0.0
    price_source = "quote" if price > 0 else "close"
    current = price if price > 0 else last_close

    view = int(cfg["bars"])
    result = {
        "config": cfg,
        "ready": ema_value is not None and rsi_value is not None and current > 0,
        "price": round(current, 5) if current else 0.0,
        "price_source": price_source,
        "last_close": round(last_close, 5) if last_close else 0.0,
        "bar_time": _as_float(rows[-1].get("time"), 0.0) if rows else 0.0,
        "bar_count": len(rows),
        # 只回传面板要画的尾部区间，前面的根数只为让递归指标收敛
        "bars": rows[-view:],
        "ema_series": [None if v is None else round(v, 5) for v in ema_line[-view:]],
        "rsi_series": [None if v is None else round(v, 2) for v in rsi_line[-view:]],
    }

    if not result["ready"]:
        result.update({
            "score": 0.0,
            "trend": TREND_UNKNOWN,
            "ema": {"period": int(cfg["ema_period"]), "value": None, "raw": 0.0,
                    "score": 0.0, "deviation_pct": 0.0, "position": "unknown"},
            "rsi": {"period": int(cfg["rsi_period"]), "value": None, "raw": 0.0,
                    "score": 0.0, "state": TREND_UNKNOWN},
        })
        return result

    ema_val = float(ema_value)  # type: ignore[arg-type]
    rsi_val = float(rsi_value)  # type: ignore[arg-type]
    deviation_pct = (current - ema_val) / ema_val * 100.0 if ema_val > 0 else 0.0
    ema_raw = ema_raw_score(current, ema_val, float(cfg["ema_full_scale_pct"]))
    rsi_raw = rsi_raw_score(rsi_val, float(cfg["rsi_bull"]), float(cfg["rsi_bear"]))
    ema_score = ema_raw * float(cfg["ema_weight"])
    rsi_score = rsi_raw * float(cfg["rsi_weight"])
    score = ema_score + rsi_score

    result.update({
        "score": round(score, 2),
        "trend": classify(score, cfg),
        "ema": {
            "period": int(cfg["ema_period"]),
            "value": round(ema_val, 5),
            "raw": round(ema_raw, 2),
            "score": round(ema_score, 2),
            "max_score": round(float(cfg["ema_weight"]) * 100.0, 2),
            "deviation_pct": round(deviation_pct, 4),
            "position": "above" if current > ema_val else ("below" if current < ema_val else "equal"),
        },
        "rsi": {
            "period": int(cfg["rsi_period"]),
            "value": round(rsi_val, 2),
            "raw": round(rsi_raw, 2),
            "score": round(rsi_score, 2),
            "max_score": round(float(cfg["rsi_weight"]) * 100.0, 2),
            "state": rsi_state(rsi_val, cfg),
        },
    })
    return result
