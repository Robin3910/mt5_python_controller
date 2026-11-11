"""财经日历黑名单（新闻黑名单 + 停止做单开关）的单元与端到端用例。

覆盖：
- 解析层：impact 归一化、带偏移 / 不带偏移的时间解析、holiday 处理；
- 判定层：窗口边界、影响等级阈值、币种推导、手动急停优先级、关开关即放行；
- 接口层：GET / PUT 的读写与越权防护、事件列表、时间戳口径。
"""
import pathlib

import fakeredis
import pytest
from fastapi.testclient import TestClient

from app import economic_calendar, news_guard
from app.redis_store import RedisStore
from tests.test_helpers import auth_headers, drop_test_db, reset_test_db

_TEST_DB = pathlib.Path(__file__).resolve().parent / "_test_api.db"
_PWD = "pass1234"


@pytest.fixture
def client(monkeypatch):
    def fake_from_url(cls, url=None):
        return RedisStore(fakeredis.FakeAsyncRedis(decode_responses=True))

    reset_test_db(_TEST_DB)
    monkeypatch.setattr(RedisStore, "from_url", classmethod(fake_from_url))
    from app.main import app

    with TestClient(app) as c:
        yield c
    drop_test_db(_TEST_DB)


def _login(client, username: str, password: str = _PWD) -> dict:
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _mk_user(client, h, username: str) -> dict:
    r = client.post("/api/users", json={"username": username, "password": _PWD}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()


def _iso_with_offset(epoch: int, hours: int = -4) -> str:
    """epoch -> 带时区偏移的 ISO 串（模拟数据源的 date 字段格式）。"""
    from datetime import datetime, timedelta, timezone

    tz = timezone(timedelta(hours=hours))
    return datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone(tz).isoformat()


def _future_epoch_utc(hour: int, minute: int, days_ahead: int = 2) -> int:
    """未来某天的 UTC 时:分 对应的 epoch（用于接口层的展示口径测试）。"""
    import time
    from datetime import datetime, timedelta, timezone

    base = datetime.fromtimestamp(time.time(), tz=timezone.utc) + timedelta(days=days_ahead)
    midnight = base.replace(hour=0, minute=0, second=0, microsecond=0)
    return int((midnight + timedelta(hours=hour, minutes=minute)).timestamp())


def _seed_events_and_settings(events: list[dict], settings: dict) -> None:
    """往测试库写事件与用户设置。

    本文件的 `client` fixture 是**同步** TestClient，主线程此刻没有 event loop；
    自己 `asyncio.get_event_loop()` 在全量跑时会被前面的用例关掉而炸掉。
    所以借 anyio 的 blocking portal 把协程丢进去跑（同一线程、同一 loop）——
    与 test_follow_api.py / test_ai_follow_api.py 的既有做法一致。
    """
    from anyio.from_thread import start_blocking_portal

    async def _go():
        from app.db import SessionLocal
        from app.orm import EconomicEvent, UserNewsSettings

        async with SessionLocal() as s:
            for ev in events:
                s.add(EconomicEvent(**ev))
            s.add(UserNewsSettings(**settings))
            await s.commit()

    with start_blocking_portal() as portal:
        portal.call(_go)


def _seed_event(event_id: str, event_time: int, title: str, country: str, impact: str) -> None:
    """往测试库写一条事件（复用上面的 portal 包装）。"""
    _seed_events_and_settings(
        events=[{
            "event_id": event_id, "event_time": event_time, "title": title,
            "country": country, "impact": impact, "forecast": None,
            "previous": None, "source": "test",
        }],
        settings={
            "user_id": 990000, "enabled": True, "before_min": 30, "after_min": 30,
            "min_impact": "high", "currencies_json": ["USD"], "symbols_json": [],
            "manual_paused": False,
        },
    )


def _block_reason(user_id: int, symbol: str, now: float):
    """跑一次判定（同步包装）。"""
    from anyio.from_thread import start_blocking_portal

    async def _go():
        return await news_guard.block_open_reason(user_id, symbol, now=now)

    with start_blocking_portal() as portal:
        return portal.call(_go)


# =====================================================================
# 解析层
# =====================================================================
def test_normalize_impact():
    assert economic_calendar.normalize_impact("High") == "high"
    assert economic_calendar.normalize_impact("medium") == "medium"
    assert economic_calendar.normalize_impact("Low") == "low"
    assert economic_calendar.normalize_impact("Holiday") == "holiday"
    # 未知取值必须返回 None（丢弃该条），不能瞎猜
    assert economic_calendar.normalize_impact("Bogus") is None
    assert economic_calendar.normalize_impact("") is None


def test_impact_at_least_threshold():
    # 默认只拦高影响
    assert economic_calendar.impact_at_least("high", "high") is True
    assert economic_calendar.impact_at_least("medium", "high") is False
    assert economic_calendar.impact_at_least("low", "high") is False
    # 阈值放到 medium 时，中影响纳入
    assert economic_calendar.impact_at_least("medium", "medium") is True
    # holiday 永远不满足任何阈值
    assert economic_calendar.impact_at_least("holiday", "low") is False


def test_parse_event_time_with_offset():
    """带偏移的时间串按偏移换算成 epoch（不能当裸本地时间）。"""
    a = economic_calendar.parse_event_time("2026-11-12T08:30:00-04:00")
    b = economic_calendar.parse_event_time("2026-11-12T08:30:00-05:00")
    assert a is not None and b is not None
    # 同一钟面、不同偏移，相差 1 小时
    assert abs(a - b) == 3600


def test_parse_event_time_invalid():
    assert economic_calendar.parse_event_time("") is None
    assert economic_calendar.parse_event_time("not-a-date") is None


def test_parse_events_filters_and_normalizes():
    # 事件时间取「明天」，落在 7 天前瞻窗口内（窗口外的事件会被丢弃）
    now = 1_800_000_000.0
    tomorrow = _iso_with_offset(int(now) + 86400)
    payload = [
        {"title": "CPI y/y", "country": "USD", "date": tomorrow,
         "impact": "High", "forecast": "0.8%", "previous": "0.9%"},
        {"title": "Holiday", "country": "USD", "date": tomorrow,
         "impact": "Holiday", "forecast": "", "previous": ""},
        {"title": "Bogus", "country": "USD", "date": tomorrow,
         "impact": "Weird", "forecast": "", "previous": ""},
        {"title": "   ", "country": "USD", "date": tomorrow,
         "impact": "High", "forecast": "", "previous": ""},
    ]
    events = economic_calendar.parse_events(payload, now=now)
    titles = {e["title"] for e in events}
    assert "CPI y/y" in titles
    # 未知 impact 丢弃；空标题丢弃；holiday 保留在表里（只是不拦）
    assert "Bogus" not in titles
    assert "   " not in titles
    holiday = next(e for e in events if e["title"] == "Holiday")
    assert holiday["impact"] == "holiday"
    # 币种归一化为大写
    assert next(e for e in events if e["title"] == "CPI y/y")["country"] == "USD"


def test_parse_events_drops_out_of_window_events():
    """太早（已过去很久）或太晚（超出前瞻）的事件都要丢弃。"""
    now = 1_800_000_000.0
    past = _iso_with_offset(int(now) - 5 * 86400)
    far = _iso_with_offset(int(now) + 30 * 86400)
    events = economic_calendar.parse_events([
        {"title": "Old", "country": "USD", "date": past, "impact": "High"},
        {"title": "Far", "country": "USD", "date": far, "impact": "High"},
    ], now=now)
    assert events == []


def test_event_id_is_stable():
    """同一事件反复抓取算出同一个 id（落库幂等的前提）。"""
    a = economic_calendar.make_event_id("CPI y/y", 1_700_000_000, "USD")
    b = economic_calendar.make_event_id("CPI y/y", 1_700_000_000, "USD")
    c = economic_calendar.make_event_id("CPI y/y", 1_700_000_001, "USD")
    assert a == b
    assert a != c


# =====================================================================
# 判定层：币种推导
# =====================================================================
def test_symbol_currencies():
    assert news_guard.symbol_currencies("XAUUSD", ("USD",)) == {"USD"}
    assert news_guard.symbol_currencies("EURUSD", ("EUR",)) == {"EUR"}
    assert news_guard.symbol_currencies("EURUSD", ("EUR", "USD")) == {"EUR", "USD"}
    # 不相关的币种不命中
    assert news_guard.symbol_currencies("XAUUSD", ("EUR",)) == set()
    assert news_guard.symbol_currencies("", ("USD",)) == set()


def test_event_matches_currency_global_event():
    """country == All 的全球性事件对所有币种都生效。"""
    assert news_guard.event_matches_currency("All", ("USD",)) is True
    assert news_guard.event_matches_currency("EUR", ("USD",)) is False
    assert news_guard.event_matches_currency("USD", ("USD",)) is True


def test_normalize_settings_clamps_and_defaults():
    s = news_guard.normalize_settings(None)
    assert s.enabled is True
    assert s.before_min == 30 and s.after_min == 30
    assert s.min_impact == "high"
    assert s.currencies == ("USD",)
    assert s.manual_paused is False

    # 窗口夹到 [0, 120]
    s2 = news_guard.normalize_settings({"before_min": 9999, "after_min": -5})
    assert s2.before_min == 120 and s2.after_min == 0

    # 币种归一化：去重、大写、保序；白名单外的代码丢弃；空则回落默认
    s3 = news_guard.normalize_settings({"currencies": ["usd", "eur", "usd", "bad"]})
    assert s3.currencies == ("USD", "EUR")
    s4 = news_guard.normalize_settings({"currencies": []})
    assert s4.currencies == ("USD",)

    # holiday 不是有效的「最低等级」，要回落成 high
    s5 = news_guard.normalize_settings({"min_impact": "holiday"})
    assert s5.min_impact == "high"


# =====================================================================
# 判定层：拦截行为（直接跑判定，绕开分发引擎）
# =====================================================================
def test_block_open_reason_blocks_inside_window(client):
    """事件前后 30 分钟内必须拦截。"""
    now = 1_800_000_000
    _seed_events_and_settings(
        events=[{
            "event_id": "evt-1", "event_time": now, "title": "CPI y/y",
            "country": "USD", "impact": "high", "forecast": None,
            "previous": None, "source": "test",
        }],
        settings={
            "user_id": 900001, "enabled": True, "before_min": 30, "after_min": 30,
            "min_impact": "high", "currencies_json": ["USD"], "symbols_json": [],
            "manual_paused": False,
        },
    )

    # 事件前 29 分钟：拦
    reason = _block_reason(900001, "XAUUSD", now - 29 * 60)
    assert reason is not None
    assert "CPI y/y" in reason

    # 事件后 29 分钟：拦
    assert _block_reason(900001, "XAUUSD", now + 29 * 60) is not None

    # 窗口之外（提前 31 分钟）：放行
    assert _block_reason(900001, "XAUUSD", now - 31 * 60) is None


def test_block_open_reason_window_boundary_is_inclusive(client):
    """窗口端点含在内（正负 30 分钟整点仍应拦截）。"""
    now = 1_800_000_000
    _seed_events_and_settings(
        events=[{
            "event_id": "evt-b", "event_time": now, "title": "FOMC",
            "country": "USD", "impact": "high", "forecast": None,
            "previous": None, "source": "test",
        }],
        settings={
            "user_id": 900002, "enabled": True, "before_min": 30, "after_min": 30,
            "min_impact": "high", "currencies_json": ["USD"], "symbols_json": [],
            "manual_paused": False,
        },
    )

    assert _block_reason(900002, "XAUUSD", now - 30 * 60) is not None
    assert _block_reason(900002, "XAUUSD", now + 30 * 60) is not None
    assert _block_reason(900002, "XAUUSD", now - 30 * 60 - 1) is None
    assert _block_reason(900002, "XAUUSD", now + 30 * 60 + 1) is None


def test_medium_impact_not_blocked_by_default(client):
    """默认只拦高影响：中影响事件不拦。"""
    now = 1_800_000_000
    _seed_events_and_settings(
        events=[{
            "event_id": "evt-m", "event_time": now, "title": "PMI",
            "country": "USD", "impact": "medium", "forecast": None,
            "previous": None, "source": "test",
        }],
        settings={
            "user_id": 900003, "enabled": True, "before_min": 30, "after_min": 30,
            "min_impact": "high", "currencies_json": ["USD"], "symbols_json": [],
            "manual_paused": False,
        },
    )

    assert _block_reason(900003, "XAUUSD", now) is None


def test_currency_mismatch_not_blocked(client):
    """事件币种不在关注范围 -> 不拦。"""
    now = 1_800_000_000
    _seed_events_and_settings(
        events=[{
            "event_id": "evt-e", "event_time": now, "title": "ECB Rate",
            "country": "EUR", "impact": "high", "forecast": None,
            "previous": None, "source": "test",
        }],
        settings={
            "user_id": 900004, "enabled": True, "before_min": 30, "after_min": 30,
            "min_impact": "high", "currencies_json": ["USD"], "symbols_json": [],
            "manual_paused": False,
        },
    )

    assert _block_reason(900004, "XAUUSD", now) is None


def test_manual_pause_blocks_even_without_events(client):
    """手动急停：不依赖任何事件，独立生效。"""
    _seed_events_and_settings(
        events=[],
        settings={
            "user_id": 900005, "enabled": True, "before_min": 30, "after_min": 30,
            "min_impact": "high", "currencies_json": ["USD"], "symbols_json": [],
            "manual_paused": True,
        },
    )

    reason = _block_reason(900005, "XAUUSD", 1_800_000_000)
    assert reason is not None
    assert "手动停止做单" in reason


def test_disabled_settings_allow_everything(client):
    """关掉黑名单总开关 -> 一律放行。"""
    now = 1_800_000_000
    _seed_events_and_settings(
        events=[{
            "event_id": "evt-d", "event_time": now, "title": "CPI y/y",
            "country": "USD", "impact": "high", "forecast": None,
            "previous": None, "source": "test",
        }],
        settings={
            "user_id": 900006, "enabled": False, "before_min": 30, "after_min": 30,
            "min_impact": "high", "currencies_json": ["USD"], "symbols_json": [],
            "manual_paused": False,
        },
    )

    assert _block_reason(900006, "XAUUSD", now) is None


def test_default_settings_apply_without_row(client):
    """没有设置行 = 用默认值（存量用户零配置即生效）。"""
    from anyio.from_thread import start_blocking_portal

    async def _go():
        return await news_guard.get_settings(987654)

    with start_blocking_portal() as portal:
        settings = portal.call(_go)
    assert settings.enabled is True
    assert settings.before_min == 30 and settings.after_min == 30
    assert settings.min_impact == "high"
    assert settings.currencies == ("USD",)


def test_global_event_blocks_all_currencies(client):
    """country == All 的全球性事件对所有关注币种生效。"""
    now = 1_800_000_000
    _seed_events_and_settings(
        events=[{
            "event_id": "evt-g", "event_time": now, "title": "Global Event",
            "country": "All", "impact": "high", "forecast": None,
            "previous": None, "source": "test",
        }],
        settings={
            "user_id": 900007, "enabled": True, "before_min": 30, "after_min": 30,
            "min_impact": "high", "currencies_json": ["USD"], "symbols_json": [],
            "manual_paused": False,
        },
    )

    assert _block_reason(900007, "XAUUSD", now) is not None


# =====================================================================
# 接口层
# =====================================================================
def test_get_news_returns_defaults(client):
    h = auth_headers(client)
    r = client.get("/api/me/news", headers=h)
    assert r.status_code == 200, r.text
    data = r.json()
    # 没配置过任何设置 -> 返回默认值（开 / 30-30 / high / USD）
    assert data["settings"]["enabled"] is True
    assert data["settings"]["before_min"] == 30
    assert data["settings"]["after_min"] == 30
    assert data["settings"]["min_impact"] == "high"
    assert data["settings"]["currencies"] == ["USD"]
    assert data["settings"]["manual_paused"] is False
    assert data["status"]["blocked"] is False
    assert "events" in data
    assert "calendar" in data


def test_put_news_settings_persists(client):
    h = auth_headers(client)
    r = client.put(
        "/api/me/news/settings",
        json={"enabled": True, "before_min": 15, "after_min": 45, "currencies": ["USD", "EUR"]},
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert r.json()["settings"]["before_min"] == 15
    assert r.json()["settings"]["after_min"] == 45
    assert r.json()["settings"]["currencies"] == ["USD", "EUR"]

    # 重新拉取 -> 值确实落库了
    r2 = client.get("/api/me/news", headers=h)
    assert r2.json()["settings"]["before_min"] == 15
    assert r2.json()["settings"]["currencies"] == ["USD", "EUR"]


def test_manual_pause_toggle(client):
    h = auth_headers(client)
    r = client.put("/api/me/news/settings", json={"manual_paused": True}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["settings"]["manual_paused"] is True

    r2 = client.get("/api/me/news", headers=h)
    assert r2.json()["status"]["blocked"] is True
    assert "手动停止做单" in r2.json()["status"]["reason"]

    client.put("/api/me/news/settings", json={"manual_paused": False}, headers=h)
    r3 = client.get("/api/me/news", headers=h)
    assert r3.json()["status"]["blocked"] is False


def test_settings_are_per_user(client):
    """A 改自己的设置不能影响 B。"""
    admin_h = auth_headers(client)
    _mk_user(client, admin_h, "newsuser_a")
    _mk_user(client, admin_h, "newsuser_b")

    ha = _login(client, "newsuser_a")
    hb = _login(client, "newsuser_b")

    client.put("/api/me/news/settings", json={"before_min": 5}, headers=ha)
    ra = client.get("/api/me/news", headers=ha).json()
    rb = client.get("/api/me/news", headers=hb).json()
    assert ra["settings"]["before_min"] == 5
    assert rb["settings"]["before_min"] == 30


def test_news_requires_login(client):
    r = client.get("/api/me/news")
    assert r.status_code == 401


def test_admin_events_endpoint_requires_admin(client):
    """普通用户不能看管理员接口。"""
    admin_h = auth_headers(client)
    _mk_user(client, admin_h, "newsuser_c")
    hc = _login(client, "newsuser_c")
    r = client.get("/api/admin/news/events", headers=hc)
    assert r.status_code == 403

    r2 = client.get("/api/admin/news/events", headers=admin_h)
    assert r2.status_code == 200, r2.text
    assert "events" in r2.json()


def test_event_list_uses_beijing_time(client):
    """事件列表展示的是北京时间串。"""
    # UTC 12:30 -> 北京时间 20:30
    _seed_event("evt-tz", _future_epoch_utc(12, 30), "CPI y/y", "USD", "high")

    h = auth_headers(client)
    data = client.get("/api/me/news", headers=h).json()
    match = [e for e in data["events"] if e["event_id"] == "evt-tz"]
    assert match, "事件应出现在列表里"
    assert match[0]["event_time_text"].endswith("20:30")


def test_event_in_blackout_flag(client):
    """事件应带上「是否落入我的黑名单」标记。"""
    _seed_event("evt-flag", _future_epoch_utc(12, 30), "CPI y/y", "USD", "high")

    h = auth_headers(client)
    data = client.get("/api/me/news", headers=h).json()
    match = [e for e in data["events"] if e["event_id"] == "evt-flag"]
    assert match
    assert match[0]["in_blackout"] is True


def test_event_outside_currency_marked_not_in_blackout(client):
    """币种不匹配的事件要标记为「不在我的黑名单」并说明原因。"""
    _seed_event("evt-eur", _future_epoch_utc(12, 30), "ECB Rate", "EUR", "high")

    h = auth_headers(client)
    data = client.get("/api/me/news", headers=h).json()
    match = [e for e in data["events"] if e["event_id"] == "evt-eur"]
    assert match
    assert match[0]["in_blackout"] is False
    assert match[0]["block_reason"]
