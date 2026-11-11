"""财经日历抓取：ForexFactory 公开 JSON → `economic_event` 表。

参考实现：Robin3910/xauusd_ai_trader_v2 的 `economic_calendar.py`。
借鉴它「事件 → 黑名单时间窗」的建模与「拉取失败回落缓存」的容错口径；
但按本项目的实际情况做了四处修正（详见 docs/superpowers/specs/
2026-11-11-news-blackout-design.md §2.2）：

1. **只拉 `ff_calendar_thisweek.json`**：实测 `ff_calendar_nextweek.json` 返回 404，
   该数据源只有「本周」一个可用地址，所以前瞻窗口按 7 天算，不做 14 天的假承诺。
2. **时间存 UTC epoch 整数**：参考实现转成北京时间裸字符串，本项目一律存 epoch。
3. **服务端拉一次全站共享**：节点 / 客户端不各自去拉外网。
4. **impact 取值是 High/Medium/Low/Holiday**：归一化成小写后落库，
   `Holiday` 显式保留（永远不拦，但要在表里有记录以免重复解析）。

抓取失败时**保留上一批数据**（不清表），并把 `stale` 标记透出给前端 ——
数据源短暂不可用就把全站交易停了，比不停更糟。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
from sqlalchemy import select

from .db import SessionLocal
from .orm import EconomicEvent

logger = logging.getLogger(__name__)

# ForexFactory 的公开 JSON（无需 API key）
FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

# 抓取周期：30 分钟一次。日历本身变动很慢（偶尔新增/修正事件），
# 半小时足够；太频繁会给对方站点添麻烦，也会让「数据更新时间」失去意义。
FETCH_INTERVAL_SEC = 30 * 60
HTTP_TIMEOUT_SEC = 20.0

# 前瞻窗口：数据源只给「本周」，所以按 7 天算。多留 1 天余量吸收跨周事件。
LOOKAHEAD_DAYS = 7

# —— 影响等级归一化 ——
IMPACT_HIGH = "high"
IMPACT_MEDIUM = "medium"
IMPACT_LOW = "low"
IMPACT_HOLIDAY = "holiday"

IMPACT_ORDER: dict[str, int] = {
    IMPACT_HIGH: 3,
    IMPACT_MEDIUM: 2,
    IMPACT_LOW: 1,
}
# holiday 不在序表里 —— 它永远不参与拦截（没有「比 holiday 更高」的等级）
IMPACT_RANK: dict[str, int] = {**IMPACT_ORDER, IMPACT_HOLIDAY: 0}

# 归一化映射：数据源给的是首字母大写（High / Medium / Low / Holiday）
_IMPACT_ALIASES: dict[str, str] = {
    "high": IMPACT_HIGH,
    "medium": IMPACT_MEDIUM,
    "low": IMPACT_LOW,
    "holiday": IMPACT_HOLIDAY,
}

# 美东时区：数据源的 date 字段带偏移（如 -04:00 / -05:00），随夏令时变化；
# 裸字符串（无偏移）按美东处理。用 ZoneInfo 而不是固定 -05:00 —— 夏令时期间
# 美东是 -04:00，固定偏移会差 1 小时。
try:
    from zoneinfo import ZoneInfo

    TZ_EASTERN = ZoneInfo("America/New_York")
except Exception:  # noqa: BLE001
    # 极端环境缺 tzdata 时退回固定 -05:00（最多 1 小时偏差，不致命）
    TZ_EASTERN = timezone(timedelta(hours=-5))  # type: ignore[assignment]

SOURCE = "forexfactory"


def normalize_impact(raw: object) -> Optional[str]:
    """把数据源的 impact 归一化成小写枚举；无法识别返回 None（该条丢弃）。"""
    text = str(raw or "").strip().lower()
    return _IMPACT_ALIASES.get(text)


def impact_rank(impact: object) -> int:
    """影响等级序号，用于「≥ 阈值」比较；未知等级按 0（holiday 同级，不拦）。"""
    return IMPACT_RANK.get(str(impact or "").strip().lower(), 0)


def impact_at_least(impact: object, min_impact: object) -> bool:
    """该事件的影响等级是否达到（或超过）阈值。

    holiday 的序号是 0，所以任何阈值下它都 **不满足** `>=`（阈值最小是 low=1），
    自然被排除 —— 不需要单独写一条 holiday 分支。
    """
    threshold = impact_rank(min_impact)
    if threshold <= 0:
        return False
    return impact_rank(impact) >= threshold


def parse_event_time(raw: object) -> Optional[int]:
    """解析数据源的 date 字段为 UTC epoch 秒；解析不了返回 None。

    数据源格式：`2026-11-12T08:30:00-04:00`（带美东偏移）。
    带偏移的用 `fromisoformat` 解析（偏移会随夏令时自动变，不用自己算）；
    **不带偏移的裸串按美东处理**（美东夏令时是 -04:00，用固定的 -05:00
    会有最多 1 小时的偏差 —— 偏差落在 ±30 分钟窗口的边缘时不致命，
    但仍按美东解析以保证口径正确）。

    `Z` 后缀先换成 `+00:00`（Python 3.10 前的 `fromisoformat` 不认 Z）。
    """
    text = str(raw or "").strip()
    if not text:
        return None
    cleaned = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        parsed = None
        for fmt in (
            "%Y-%m-%dT%H:%M:%S.%f%z",
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S%z",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%dT%H:%M",
        ):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ_EASTERN)
    return int(parsed.timestamp())


def make_event_id(title: str, event_time: int, country: str) -> str:
    """事件主键：标题 + 时刻 + 国家 的 sha1 截断。

    同一事件被反复抓取时算出同一个 id → 覆盖写 → 天然幂等。
    """
    raw = f"{title}|{event_time}|{country}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:40]


def parse_events(payload: Any, *, now: Optional[float] = None) -> list[dict]:
    """把数据源返回的 list[dict] 解析成待落库的事件列表。

    过滤规则：
      - impact 无法识别 → 丢弃（数据源加了新取值时不要瞎猜）；
      - 事件时间早于「现在 - 1 小时」→ 丢弃（已经过去很久的没意义；
        留 1 小时余量是因为事件可能延迟，且用户可能在窗口内回看）；
      - 事件时间晚于「现在 + LOOKAHEAD_DAYS」→ 丢弃。
    """
    if not isinstance(payload, list):
        return []
    now_ts = float(now if now is not None else time.time())
    cutoff_lo = now_ts - 3600
    cutoff_hi = now_ts + LOOKAHEAD_DAYS * 86400

    out: list[dict] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        country = str(item.get("country") or "").strip().upper()[:8]
        if not country:
            continue
        impact = normalize_impact(item.get("impact"))
        if impact is None:
            continue
        event_time = parse_event_time(item.get("date"))
        if event_time is None:
            continue
        if event_time < cutoff_lo or event_time > cutoff_hi:
            continue
        forecast = str(item.get("forecast") or "").strip()[:32] or None
        previous = str(item.get("previous") or "").strip()[:32] or None
        out.append({
            "event_id": make_event_id(title, event_time, country),
            "event_time": event_time,
            "title": title[:160],
            "country": country,
            "impact": impact,
            "forecast": forecast,
            "previous": previous,
            "source": SOURCE,
        })
    out.sort(key=lambda e: e["event_time"])
    return out


async def fetch_events(*, timeout: float = HTTP_TIMEOUT_SEC) -> list[dict]:
    """拉取并解析数据源；网络失败返回空列表（调用方保留旧数据）。"""
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(FF_URL, headers={"User-Agent": "mt5-hub/1.0"})
            resp.raise_for_status()
            payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("economic calendar fetch failed: %s", exc)
        return []
    events = parse_events(payload)
    logger.info("economic calendar fetched %d event(s)", len(events))
    return events


async def save_events(events: list[dict]) -> int:
    """把事件写进库（覆盖写，幂等）；返回实际写入条数。

    只覆盖**本次抓到的**事件，不清表：清表会让「拉取失败 → 保留旧数据」
    的容错失效（清表与保留是两件事，不能混在一次写里做）。
    """
    if not events:
        return 0
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with SessionLocal() as session:
        for ev in events:
            row = await session.get(EconomicEvent, ev["event_id"])
            if row is None:
                session.add(EconomicEvent(**ev, fetched_at=now))
                continue
            row.event_time = ev["event_time"]
            row.title = ev["title"]
            row.country = ev["country"]
            row.impact = ev["impact"]
            row.forecast = ev["forecast"]
            row.previous = ev["previous"]
            row.source = ev["source"]
            row.fetched_at = now
        await session.commit()
    return len(events)


async def refresh_events() -> dict:
    """跑一轮抓取 + 落库，返回本轮的状态摘要。

    返回 `{"ok": bool, "count": int, "fetched_at": float, "stale": bool}`。
    抓取失败时 `ok=False` 且**不清库** —— 上一批数据继续生效。
    """
    events = await fetch_events()
    if not events:
        return {
            "ok": False,
            "count": 0,
            "fetched_at": 0.0,
            "stale": True,
        }
    await save_events(events)
    return {
        "ok": True,
        "count": len(events),
        "fetched_at": time.time(),
        "stale": False,
    }


async def list_events(
    start_ts: int, end_ts: int, *, impacts: Optional[list[str]] = None,
) -> list[dict]:
    """取时间窗内的事件（按时间升序）。impacts 为空表示不限等级。"""
    async with SessionLocal() as session:
        stmt = select(EconomicEvent).where(
            EconomicEvent.event_time >= int(start_ts),
            EconomicEvent.event_time <= int(end_ts),
        )
        if impacts:
            stmt = stmt.where(EconomicEvent.impact.in_(list(impacts)))
        rows = (await session.execute(stmt.order_by(EconomicEvent.event_time))).scalars().all()
        return [
            {
                "event_id": r.event_id,
                "event_time": r.event_time,
                "title": r.title,
                "country": r.country,
                "impact": r.impact,
                "forecast": r.forecast,
                "previous": r.previous,
                "source": r.source,
                "fetched_at": r.fetched_at.timestamp() if r.fetched_at else 0.0,
            }
            for r in rows
        ]


async def latest_fetched_at() -> float:
    """最近一次成功抓到的事件的时间戳；没有任何数据时返回 0。

    用它判断「数据是否陈旧」：比「最近一次抓取动作的时间」更准 ——
    抓取失败时动作时间会更新但数据没更新，用事件时间才能看出数据真的旧了。
    """
    async with SessionLocal() as session:
        stmt = (
            select(EconomicEvent.fetched_at)
            .where(EconomicEvent.fetched_at.is_not(None))
            .order_by(EconomicEvent.fetched_at.desc())
            .limit(1)
        )
        row = (await session.execute(stmt)).scalar_one_or_none()
    return row.timestamp() if row else 0.0


class EconomicCalendarWorker:
    """财经日历定时抓取 worker。

    与 `LeaderStatsAggregator` / `PollWorker` 同构：在 lifespan 里 start/stop，
    随应用启停。任何一轮失败都不该让 worker 退出（循环里 catch 掉）。
    """

    def __init__(self, interval: int = FETCH_INTERVAL_SEC) -> None:
        self._interval = interval
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self.last_ok: bool = False
        self.last_fetched_at: float = 0.0
        self.last_error: str = ""

    def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self.run(), name="economic-calendar")
        logger.info("economic calendar worker started (interval=%ss)", self._interval)

    async def stop(self) -> None:
        self._stop.set()
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                result = await refresh_events()
                self.last_ok = bool(result.get("ok"))
                if self.last_ok:
                    self.last_fetched_at = float(result.get("fetched_at") or time.time())
                    self.last_error = ""
                else:
                    self.last_error = "抓取失败，保留上次数据"
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.last_ok = False
                self.last_error = str(e)
                logger.warning("economic calendar refresh failed: %s", e)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                continue

    async def refresh_now(self) -> dict:
        """手动触发一轮（供「刷新」按钮 / 排障接口调用）。"""
        result = await refresh_events()
        self.last_ok = bool(result.get("ok"))
        if self.last_ok:
            self.last_fetched_at = float(result.get("fetched_at") or time.time())
            self.last_error = ""
        else:
            self.last_error = "抓取失败，保留上次数据"
        return result
