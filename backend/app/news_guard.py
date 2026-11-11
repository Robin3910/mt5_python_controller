"""财经日历黑名单判定：**此刻是否禁止新开仓**。

核心入口：`block_open_reason(user_id, symbol)`。

三条硬约束（贯穿全模块，改动时务必守住）：

1. **只拦新开仓**。平仓、改单、持仓管理一律放行 —— 数据公布期间把裸仓
   丢在那里不管，比错过几单危险得多。已在跑的任务也不终止。
2. **fail-open**。判定自身出错（DB 读失败、代码异常）一律**放行**并记日志：
   判定层故障不能静默变成「永不开单」。
3. **手动急停优先**。`manual_paused` 命中时直接拦，不看新闻 —— 急停必须
   绝对可靠，不受数据源、参数配置的任何影响。

用法（两处分发引擎接入，见 dispatcher.py / group_dispatcher.py）：

```python
reason = await news_guard.block_open_reason(user_id=..., symbol=...)
if reason:
    await self._skip(..., reason, signal_codes.SKIP_NEWS_BLACKOUT)
    return {"status": "skipped", "reason": reason}
```
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select

from . import economic_calendar
from .db import SessionLocal
from .orm import UserNewsSettings

logger = logging.getLogger(__name__)

# —— 默认值（用户没有设置行时用）——
# 存量用户零配置即生效：开黑名单 / 前后 30 分钟 / 只拦高影响 / 只关注 USD
DEFAULT_ENABLED = True
DEFAULT_BEFORE_MIN = 30
DEFAULT_AFTER_MIN = 30
DEFAULT_MIN_IMPACT = economic_calendar.IMPACT_HIGH
DEFAULT_CURRENCIES: tuple[str, ...] = ("USD",)

# 窗口分钟数上下限（防止有人配成 0 天或 30 天）
MIN_WINDOW_MIN = 0
MAX_WINDOW_MIN = 120

# 全局性事件的 country 取值：对所有币种都生效
GLOBAL_COUNTRY = "ALL"

# 前后端共用的展示口径：北京时间
TZ_BEIJING = "UTC+8"


def beijing_today(now: Optional[float] = None) -> str:
    """服务器当前时刻对应的**北京日期**（YYYY-MM-DD）。

    提示条的「当天关闭」以北京时间为准 —— 前端展示的日历时刻也是 UTC+8，
    判定口径必须一致，否则会出现「凌晨 1 点关掉、早上 8 点又冒出来」
    （服务端按 UTC 算还是同一天，前端按北京时间算又是新的一天）。
    """
    ts = float(now if now is not None else time.time())
    return (
        datetime.fromtimestamp(ts, tz=timezone.utc)
        .astimezone(timezone(timedelta(hours=8)))
        .strftime("%Y-%m-%d")
    )


@dataclass(frozen=True)
class NewsSettings:
    """一个用户的新闻黑名单设置（规范化后的只读视图）。"""

    enabled: bool = DEFAULT_ENABLED
    before_min: int = DEFAULT_BEFORE_MIN
    after_min: int = DEFAULT_AFTER_MIN
    min_impact: str = DEFAULT_MIN_IMPACT
    currencies: tuple[str, ...] = DEFAULT_CURRENCIES
    symbols: tuple[str, ...] = ()
    manual_paused: bool = False
    manual_paused_at: Optional[float] = None
    manual_pause_note: Optional[str] = None
    # 顶部滚动提示条被关闭的当天（北京时间 YYYY-MM-DD）；None = 当天未关闭
    banner_dismissed_date: Optional[str] = None
    exists: bool = False

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "before_min": self.before_min,
            "after_min": self.after_min,
            "min_impact": self.min_impact,
            "currencies": list(self.currencies),
            "symbols": list(self.symbols),
            "manual_paused": self.manual_paused,
            "manual_paused_at": self.manual_paused_at,
            "manual_pause_note": self.manual_pause_note,
            "banner_dismissed_date": self.banner_dismissed_date,
        }


def _clamp_window(value: object, fallback: int) -> int:
    """窗口分钟数夹到 [0, 120]；解析不了用默认值。"""
    try:
        minutes = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    return max(MIN_WINDOW_MIN, min(MAX_WINDOW_MIN, minutes))


# 关注币种里允许出现的代码：日历里出现过的货币 + 常见外汇货币。
# 白名单而不是「任意 3 个字母」—— 否则用户手滑打进去的 "BAD" 也会被当成币种，
# 存进库里成了一个永远匹配不上的幽灵币种。
KNOWN_CURRENCIES: frozenset[str] = frozenset({
    "USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD",
    "CNY", "CNH", "SGD", "HKD", "KRW", "INR", "MXN", "ZAR",
    "TRY", "SEK", "NOK", "DKK", "PLN", "RUB", "BRL",
})


def _norm_currencies(raw: object) -> tuple[str, ...]:
    """币种集合规范化：大写、去空白、去重、保序；空则用默认（USD）。

    不在 `KNOWN_CURRENCIES` 白名单里的代码一律丢弃 —— 见该常量处的说明。
    """
    if isinstance(raw, str):
        items = [part for part in raw.split(",")]
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        items = []
    out: list[str] = []
    for item in items:
        code = str(item or "").strip().upper()
        if code not in KNOWN_CURRENCIES:
            continue
        if code not in out:
            out.append(code)
    return tuple(out) if out else DEFAULT_CURRENCIES


def _norm_symbols(raw: object) -> tuple[str, ...]:
    """品种集合规范化：大写、去空白、去重、保序（可为空 = 按币种推导）。"""
    if not isinstance(raw, (list, tuple)):
        return ()
    out: list[str] = []
    for item in raw:
        sym = str(item or "").strip().upper()
        if sym and sym not in out:
            out.append(sym)
    return tuple(out)


def normalize_settings(raw: dict | None) -> NewsSettings:
    """把任意入参整理成稳定的 NewsSettings。"""
    src = raw if isinstance(raw, dict) else {}
    min_impact = economic_calendar.normalize_impact(src.get("min_impact")) or DEFAULT_MIN_IMPACT
    if min_impact == economic_calendar.IMPACT_HOLIDAY:
        # holiday 不是有效的「最低等级」—— 总得拦点什么
        min_impact = DEFAULT_MIN_IMPACT
    paused_at = src.get("manual_paused_at")
    return NewsSettings(
        enabled=bool(src.get("enabled", DEFAULT_ENABLED)),
        before_min=_clamp_window(src.get("before_min"), DEFAULT_BEFORE_MIN),
        after_min=_clamp_window(src.get("after_min"), DEFAULT_AFTER_MIN),
        min_impact=min_impact,
        currencies=_norm_currencies(src.get("currencies")),
        symbols=_norm_symbols(src.get("symbols")),
        manual_paused=bool(src.get("manual_paused", False)),
        manual_paused_at=float(paused_at) if paused_at else None,
        manual_pause_note=src.get("manual_pause_note") or None,
        banner_dismissed_date=src.get("banner_dismissed_date") or None,
        exists=bool(src.get("exists", False)),
    )


async def get_settings(user_id: int) -> NewsSettings:
    """读某个用户的设置；没有行就返回默认值（存量用户零配置即生效）。"""
    if not user_id or int(user_id) <= 0:
        return NewsSettings()
    try:
        async with SessionLocal() as session:
            row = await session.get(UserNewsSettings, int(user_id))
            if row is None:
                return NewsSettings()
            raw = {
                "enabled": bool(row.enabled),
                "before_min": row.before_min,
                "after_min": row.after_min,
                "min_impact": row.min_impact,
                "currencies": row.currencies_json,
                "symbols": row.symbols_json,
                "manual_paused": bool(row.manual_paused),
                "manual_paused_at": row.manual_paused_at.timestamp() if row.manual_paused_at else None,
                "manual_pause_note": row.manual_pause_note,
                "banner_dismissed_date": row.banner_dismissed_date,
                "exists": True,
            }
            return normalize_settings(raw)
    except Exception as exc:  # noqa: BLE001
        # 读失败 → 用默认值并放行（fail-open）
        logger.warning("news settings read failed for user %s: %s", user_id, exc)
        return NewsSettings()


async def save_settings(user_id: int, payload: dict) -> NewsSettings:
    """保存设置（upsert）。返回规范化后的设置。

    `banner_dismissed_date` **不由本函数维护** —— 它只跟「提示条被关掉」这件事
    有关，由 `dismiss_banner` 单独写。设置面板存别的字段时不能把它清掉，
    否则用户关掉提示条、随手改个币种，提示条又冒出来。
    """
    from datetime import datetime as _dt

    normalized = normalize_settings(payload)
    async with SessionLocal() as session:
        row = await session.get(UserNewsSettings, int(user_id))
        if row is None:
            row = UserNewsSettings(user_id=int(user_id))
            session.add(row)
        row.enabled = normalized.enabled
        row.before_min = normalized.before_min
        row.after_min = normalized.after_min
        row.min_impact = normalized.min_impact
        row.currencies_json = list(normalized.currencies)
        row.symbols_json = list(normalized.symbols)
        row.manual_paused = normalized.manual_paused
        row.manual_paused_at = (
            _dt.fromtimestamp(normalized.manual_paused_at) if normalized.manual_paused_at else None
        )
        row.manual_pause_note = normalized.manual_pause_note
        await session.commit()
    return normalized


async def dismiss_banner(user_id: int, day: Optional[str] = None) -> str:
    """记录「顶部提示条已被关闭」，返回记录下来的日期。

    `day` 形如 `2026-11-11`（北京时间）。传 None 时用服务器当前的北京时间。

    记**日期**而不是布尔值：需求是「关闭后当天不再显示」，次日仍要重新提示。
    """
    from datetime import datetime as _dt

    target = (day or beijing_today()).strip()
    async with SessionLocal() as session:
        row = await session.get(UserNewsSettings, int(user_id))
        if row is None:
            row = UserNewsSettings(user_id=int(user_id))
            session.add(row)
        row.banner_dismissed_date = target
        await session.commit()
    return target


async def banner_dismissed_today(user_id: int, day: Optional[str] = None) -> bool:
    """今天是否已经关闭过提示条。"""
    settings = await get_settings(user_id)
    target = (day or beijing_today()).strip()
    return bool(settings.banner_dismissed_date == target)


# ---------------------------------------------------------------------------
# 币种推导
# ---------------------------------------------------------------------------
def symbol_currencies(symbol: object, currencies: tuple[str, ...]) -> set[str]:
    """这个品种受哪些「已关注币种」影响。

    规则：**品种名里出现的任一关注币种码，即认为该品种受其影响**。

    - `XAUUSD` 含 `USD` → 命中（黄金归美元）；默认关注集合只有 USD 时成立。
    - `EURUSD` 含 `EUR` 也含 `USD` —— 两个都在关注集合里就都命中，
      这正是想要的（欧元事件同样会影响 EURUSD）。

    比「取品种后 3 位」正确：`EURUSD` 后 3 位是 `USD`（错，应是 EUR）。
    """
    text = "".join(ch for ch in str(symbol or "").upper() if ch.isalpha())
    if not text:
        return set()
    return {code for code in currencies if code in text}


def event_matches_currency(event_country: str, currencies: tuple[str, ...]) -> bool:
    """事件币种是否与关注币种匹配。

    `country == "All"`（全球性事件）对所有币种都生效 —— 数据源里有这类事件，
    必须单独处理，否则全球性数据公布时不会触发黑名单。
    """
    country = str(event_country or "").strip().upper()
    if not country:
        return False
    if country == GLOBAL_COUNTRY:
        return True
    return country in currencies


def event_matches_symbol(event_country: str, symbol: str, settings: NewsSettings) -> bool:
    """该事件是否影响这个品种。

    品种级覆盖（`symbols_json`）优先：显式列出的品种直接按其币种判定；
    没列出的按「品种名含关注币种」推导。
    """
    if settings.symbols:
        sym = str(symbol or "").strip().upper()
        if sym and sym in settings.symbols:
            return event_matches_currency(event_country, settings.currencies)
        # 品种级覆盖里没这个品种 → 不受影响（用户显式限定了范围）
        return False
    return event_matches_currency(event_country, settings.currencies)


# ---------------------------------------------------------------------------
# 核心判定
# ---------------------------------------------------------------------------
def format_event_reason(event: dict, before_min: int, after_min: int) -> str:
    """黑名单拦截的人读原因。"""
    title = str(event.get("title") or "经济数据").strip()
    country = str(event.get("country") or "").strip()
    impact = str(event.get("impact") or "").strip()
    when = datetime.fromtimestamp(
        int(event.get("event_time") or 0), tz=timezone.utc,
    ).astimezone(timezone(timedelta(hours=8)))
    stamp = when.strftime("%Y-%m-%d %H:%M")
    span = (
        f"±{before_min} 分钟"
        if before_min == after_min
        else f"前 {before_min} / 后 {after_min} 分钟"
    )
    tail = f" ({country}, {impact})" if country else ""
    return f"{title}{tail} @ {stamp} 公布时段黑名单（{span}）"


async def find_blocking_event(
    settings: NewsSettings, symbol: str, now: Optional[float] = None,
) -> Optional[dict]:
    """找出现在落在黑名单窗口内、且影响该品种的事件；没有返回 None。

    窗口查询范围：`now ± (before_min + after_min)` 之内的事件，
    再逐条判断「事件时刻 ± 窗口」是否覆盖 now。
    """
    if not settings.enabled:
        return None
    now_ts = float(now if now is not None else time.time())
    before = settings.before_min * 60
    after = settings.after_min * 60
    if before <= 0 and after <= 0:
        # 两个窗口都是 0 → 只拦「恰好是事件那一刻」的时刻，
        # 按秒比较几乎不可能命中，直接跳过（避免无谓的 DB 查询）。
        return None

    impacts = [
        level for level, rank in economic_calendar.IMPACT_ORDER.items()
        if rank >= economic_calendar.impact_rank(settings.min_impact)
    ]
    if not impacts:
        return None

    try:
        events = await economic_calendar.list_events(
            int(now_ts - before), int(now_ts + after), impacts=impacts,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("news event query failed: %s", exc)
        return None

    for ev in events:
        event_time = int(ev.get("event_time") or 0)
        if not (event_time - before <= now_ts <= event_time + after):
            continue
        if not event_matches_symbol(str(ev.get("country") or ""), symbol, settings):
            continue
        return ev
    return None


async def block_open_reason(
    user_id: Optional[int], symbol: str, *, now: Optional[float] = None,
) -> Optional[str]:
    """此刻是否禁止该用户在这个品种上**新开仓**；禁止时返回原因，放行返回 None。

    判定顺序（短路）：
      1. 手动急停 → 拦（优先级最高，不受其它任何条件影响）；
      2. 用户关了黑名单总开关 → 放行；
      3. 命中事件黑名单窗口 → 拦。

    **异常一律放行**（fail-open）：判定层故障不该变成「永不开单」。
    """
    try:
        settings = await get_settings(user_id or 0)
        if settings.manual_paused:
            note = f"（{settings.manual_pause_note}）" if settings.manual_pause_note else ""
            return f"已手动停止做单{note}，本次新开仓已拦截"
        if not settings.enabled:
            return None
        event = await find_blocking_event(settings, symbol, now=now)
        if event is None:
            return None
        return format_event_reason(event, settings.before_min, settings.after_min)
    except Exception as exc:  # noqa: BLE001
        logger.warning("news guard check failed (allow open): %s", exc)
        return None


async def current_status(
    user_id: Optional[int], symbol: str = "", *, now: Optional[float] = None,
) -> dict:
    """当前黑名单状态（前端状态条 + 倒计时用）。

    返回 `{blocked, reason, code, until, remaining_sec, server_time}`。
    """
    now_ts = float(now if now is not None else time.time())
    settings = await get_settings(user_id or 0)
    blocked = False
    reason: Optional[str] = None
    until: Optional[int] = None

    if settings.manual_paused:
        blocked = True
        reason = (
            f"已手动停止做单（{settings.manual_pause_note}）"
            if settings.manual_pause_note else "已手动停止做单"
        )
    elif settings.enabled:
        event = await find_blocking_event(settings, symbol, now=now_ts)
        if event is not None:
            blocked = True
            reason = format_event_reason(event, settings.before_min, settings.after_min)
            until = int(event["event_time"]) + settings.after_min * 60

    return {
        "blocked": blocked,
        "reason": reason,
        "code": "news_blackout" if blocked else None,
        "until": until,
        "remaining_sec": max(0, until - int(now_ts)) if until else 0,
        "server_time": now_ts,
        "settings": settings.to_dict(),
    }


# ---------------------------------------------------------------------------
# 顶部滚动提示条
# ---------------------------------------------------------------------------
async def find_today_blackout_events(
    settings: NewsSettings, symbol: str = "", *, now: Optional[float] = None,
) -> list[dict]:
    """今天（北京时间）内落在黑名单窗口里的事件，按时间排序。

    与 `find_blocking_event` 的区别：那个问「**此刻**是否在黑名单里」，
    这个问「**今天**有没有黑名单时段」—— 用于顶部提示条的预排期。

    「今天」按北京时间的自然日算（00:00–23:59），窗口按事件时刻 ± before/after
    落在这一天内就算。跨天事件（如 23:50 的数据 + 后 30 分钟）只要窗口
    与今天有交集就应提示。
    """
    if not settings.enabled:
        return []
    now_ts = float(now if now is not None else time.time())
    before = settings.before_min * 60
    after = settings.after_min * 60
    if before <= 0 and after <= 0:
        return []

    impacts = [
        level for level, rank in economic_calendar.IMPACT_ORDER.items()
        if rank >= economic_calendar.impact_rank(settings.min_impact)
    ]
    if not impacts:
        return []

    day_start, day_end = beijing_day_bounds(now_ts)
    try:
        # 查询范围要覆盖「事件在昨天、窗口延伸到今天」的情况，
        # 所以往前多取 before、往后多取 after。
        events = await economic_calendar.list_events(
            int(day_start - before), int(day_end + after), impacts=impacts,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("news banner event query failed: %s", exc)
        return []

    out: list[dict] = []
    for ev in events:
        event_time = int(ev.get("event_time") or 0)
        win_start = event_time - before
        win_end = event_time + after
        # 窗口与「今天」有交集即算（只看窗口起点 ≤ 今天末尾 且 窗口终点 ≥ 今天起点）
        if win_start > day_end or win_end < day_start:
            continue
        country = str(ev.get("country") or "")
        if symbol:
            # 传了品种 → 按品种级判定（品种名含关注币种 / 品种级覆盖）
            if not event_matches_symbol(country, symbol, settings):
                continue
        else:
            # 没传品种 → 仍要按**币种**过滤：用户只关注 USD 时，
            # JPY 的数据不该触发提示条。
            # （不能因为「没有品种信息」就跳过币种判定 —— 那等于提示条
            #   会对所有币种的数据都弹，等于没有黑名单。）
            if not event_matches_currency(country, settings.currencies):
                continue
        out.append(ev)
    out.sort(key=lambda e: int(e.get("event_time") or 0))
    return out


def beijing_day_bounds(ts: float) -> tuple[int, int]:
    """某个时刻所在北京自然日的 [00:00, 23:59:59] epoch 秒区间。"""
    local = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(
        timezone(timedelta(hours=8))
    )
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start.replace(hour=23, minute=59, second=59)
    return int(start.timestamp()), int(end.timestamp())


async def banner_state(
    user_id: Optional[int], symbol: str = "", *, now: Optional[float] = None,
) -> dict:
    """顶部滚动提示条的状态（前端顶栏组件据此决定显示与否）。

    返回：
      - `show`：是否该显示提示条
      - `dismissed`：今天是否已被用户关闭
      - `events`：今天落进黑名单的事件列表
      - `reason`：一句话说明（用于提示条文案）

    显示条件（三者同时满足）：
      1. 黑名单总开关是开的；
      2. 今天有事件落在黑名单窗口内；
      3. 用户今天还没关闭过提示条。
    """
    now_ts = float(now if now is not None else time.time())
    today = beijing_today(now_ts)
    settings = await get_settings(user_id or 0)

    dismissed = settings.banner_dismissed_date == today
    events: list[dict] = []
    if settings.enabled and not dismissed:
        events = await find_today_blackout_events(settings, symbol, now=now_ts)

    reason = ""
    if settings.manual_paused:
        reason = "已手动停止做单"
    elif events:
        first = events[0]
        stamp = datetime.fromtimestamp(
            int(first.get("event_time") or 0), tz=timezone.utc,
        ).astimezone(timezone(timedelta(hours=8)))
        reason = (
            f"{first.get('title')} 等 {len(events)} 项高影响数据"
            f"（{stamp.strftime('%m-%d %H:%M')} 北京时间）公布期间已停止新开仓"
        )

    return {
        "show": bool(events) and not dismissed,
        "dismissed": dismissed,
        "today": today,
        "reason": reason,
        "events": [
            {
                "event_id": ev.get("event_id"),
                "event_time": ev.get("event_time"),
                "title": ev.get("title"),
                "country": ev.get("country"),
                "impact": ev.get("impact"),
            }
            for ev in events
        ],
        "server_time": now_ts,
    }
