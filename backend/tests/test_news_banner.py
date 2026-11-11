"""顶部滚动提示条（财经日历黑名单横幅）的用例。

覆盖：
- `banner_state`：今天有黑名单时段 → 显示；关掉总开关 → 不显示；
- 「关闭后当天不再显示，次日重新提示」的日期语义；
- 「关闭提示条」不清掉设置（尤其不能动 `manual_paused`）；
- 接口层：`POST /api/me/news/banner/dismiss` 与 `GET /api/me/news` 的 banner 字段；
- 越权防护：只能关自己的提示条。
"""
import pathlib

import fakeredis
import pytest
from fastapi.testclient import TestClient

from app import news_guard
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


def _seed(events: list[dict], settings: dict) -> None:
    """往测试库写事件与用户设置（同步 TestClient → 用 portal 跑协程）。"""
    from anyio.from_thread import start_blocking_portal

    async def _go():
        from app.db import SessionLocal
        from app.orm import EconomicEvent, UserNewsSettings

        async with SessionLocal() as s:
            for ev in events:
                s.add(EconomicEvent(**ev))
            if settings:
                s.add(UserNewsSettings(**settings))
            await s.commit()

    with start_blocking_portal() as portal:
        portal.call(_go)


def _banner_state(user_id: int, symbol: str = "", now: float | None = None):
    """跑一次 banner_state（同步包装）。"""
    from anyio.from_thread import start_blocking_portal

    async def _go():
        return await news_guard.banner_state(user_id, symbol, now=now)

    with start_blocking_portal() as portal:
        return portal.call(_go)


def _today_event(hour_utc: int = 10) -> dict:
    """落在**北京今天**内的一条高影响 USD 事件。

    注意：判定口径是「北京自然日」，所以事件时刻要按 UTC+8 的今天来算，
    不能简单用 UTC 的今天 —— 否则 UTC 晚上建的用例会被算到「明天」。
    """
    import time
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    # 北京今天的 10:00 对应的 UTC 时刻（北京时间 = UTC+8）
    bj_now = now.astimezone(timezone(timedelta(hours=8)))
    target_bj = bj_now.replace(hour=hour_utc, minute=0, second=0, microsecond=0)
    target = target_bj.astimezone(timezone.utc)
    return {
        "event_id": f"ev{int(target.timestamp())}",
        "event_time": int(target.timestamp()),
        "title": "CPI y/y",
        "country": "USD",
        "impact": "high",
        "forecast": None,
        "previous": None,
        "source": "test",
    }


def _settings(user_id: int, **over) -> dict:
    base = {
        "user_id": user_id,
        "enabled": True,
        "before_min": 30,
        "after_min": 30,
        "min_impact": "high",
        "currencies_json": ["USD"],
        "symbols_json": [],
        "manual_paused": False,
    }
    base.update(over)
    return base


# =====================================================================
# beijing_today：日期口径
# =====================================================================
def test_beijing_today_uses_utc8_not_utc():
    """「当天」的判定必须按北京时间，不能按服务器 UTC。

    取一个 UTC 已经是 8 号、但北京时间已经是 9 号的时刻：
    UTC 2026-11-08T17:00Z → 北京 2026-11-09 01:00。
    """
    ts = 1794162000.0  # 2026-11-08T17:00:00Z
    assert news_guard.beijing_today(ts) == "2026-11-09"


def test_beijing_day_bounds_covers_whole_day():
    start, end = news_guard.beijing_day_bounds(1794162000.0)
    assert end - start == 23 * 3600 + 59 * 60 + 59


# =====================================================================
# banner_state：显示条件
# =====================================================================
def test_banner_shows_when_today_has_blackout_window(client):
    """今天有高影响事件落进窗口 → 提示条该显示。"""
    ev = _today_event()
    _seed([ev], _settings(990001))
    state = _banner_state(990001)
    assert state["show"] is True
    assert state["dismissed"] is False
    assert state["events"], "应有今天命中黑名单的事件"
    assert "CPI" in state["reason"]


def test_banner_hidden_when_disabled(client):
    """关掉黑名单总开关 → 不显示提示条。"""
    ev = _today_event()
    _seed([ev], _settings(990002, enabled=False))
    state = _banner_state(990002)
    assert state["show"] is False
    assert state["events"] == []


def test_banner_hidden_when_low_impact_only(client):
    """中低影响事件不触发提示条（默认只拦高影响）。"""
    ev = _today_event()
    ev["impact"] = "low"
    ev["event_id"] = "ev_low"
    _seed([ev], _settings(990003))
    state = _banner_state(990003)
    assert state["show"] is False


def test_banner_hidden_when_currency_not_watched(client):
    """不关注的事件币种不触发提示条。"""
    ev = _today_event()
    ev["country"] = "JPY"
    ev["event_id"] = "ev_jpy"
    _seed([ev], _settings(990004, currencies_json=["USD"]))
    state = _banner_state(990004)
    assert state["show"] is False


# =====================================================================
# 关闭语义：当天不显示，次日重新提示
# =====================================================================
def test_dismiss_marks_only_today(client):
    """关闭后当天不再显示；次日（换一个日期判定）重新显示。"""
    ev = _today_event()
    _seed([ev], _settings(990005))

    from anyio.from_thread import start_blocking_portal

    async def _dismiss(day):
        return await news_guard.dismiss_banner(990005, day)

    with start_blocking_portal() as portal:
        today = news_guard.beijing_today()
        saved = portal.call(_dismiss, today)
    assert saved == today

    # 当天：不再显示
    state = _banner_state(990005)
    assert state["show"] is False
    assert state["dismissed"] is True

    # 次日：重新提示（后端按日期比对，不是布尔开关）
    tomorrow = _banner_state(990005)
    assert tomorrow["dismissed"] is True, "今天已关闭"
    # 换一个「昨天」的关闭日期 → 视为未关闭
    from anyio.from_thread import start_blocking_portal as _p

    async def _dismiss2():
        return await news_guard.dismiss_banner(990005, "2000-01-01")

    with _p() as portal:
        portal.call(_dismiss2)
    state = _banner_state(990005)
    assert state["show"] is True
    assert state["dismissed"] is False


def test_dismiss_does_not_touch_other_settings(client):
    """关提示条只关提示条：不能顺手把「停止做单」开关或窗口参数改了。"""
    ev = _today_event()
    _seed([ev], _settings(990006, before_min=45, after_min=15, manual_paused=True))

    from anyio.from_thread import start_blocking_portal

    async def _dismiss():
        return await news_guard.dismiss_banner(990006)

    with start_blocking_portal() as portal:
        portal.call(_dismiss)

    async def _read():
        return await news_guard.get_settings(990006)

    with start_blocking_portal() as portal:
        s = portal.call(_read)
    assert s.manual_paused is True, "停止做单开关不能被提示条关闭影响"
    assert s.before_min == 45
    assert s.after_min == 15
    assert s.enabled is True


def test_save_settings_preserves_dismiss_marker(client):
    """改设置不能把「今天已关闭」清掉 —— 否则用户关掉提示条、又改个币种，
    提示条就又冒出来了。"""
    ev = _today_event()
    _seed([ev], _settings(990007))

    from anyio.from_thread import start_blocking_portal

    async def _go():
        await news_guard.dismiss_banner(990007)
        await news_guard.save_settings(990007, {"currencies": ["USD", "EUR"]})
        return await news_guard.get_settings(990007)

    with start_blocking_portal() as portal:
        s = portal.call(_go)
    assert s.banner_dismissed_date == news_guard.beijing_today()
    assert s.currencies == ("USD", "EUR")


# =====================================================================
# 接口层
# =====================================================================
def test_api_overview_includes_banner(client):
    """GET /api/me/news 要带上 banner 字段。"""
    h = auth_headers(client)
    user = _mk_user(client, h, "banner_user1")
    h1 = _login(client, "banner_user1")
    ev = _today_event()
    _seed([ev], _settings(user["id"]))

    r = client.get("/api/me/news", headers=h1)
    assert r.status_code == 200, r.text
    data = r.json()
    assert "banner" in data
    assert data["banner"]["show"] is True
    assert data["banner"]["events"]


def test_api_dismiss_banner_endpoint(client):
    """POST /api/me/news/banner/dismiss 记下「当天已关闭」，之后不再显示。"""
    h = auth_headers(client)
    user = _mk_user(client, h, "banner_user2")
    h2 = _login(client, "banner_user2")
    ev = _today_event()
    _seed([ev], _settings(user["id"]))

    r = client.post("/api/me/news/banner/dismiss", json={}, headers=h2)
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert r.json()["day"] == news_guard.beijing_today()

    # 再拉一次 → 不再显示
    data = client.get("/api/me/news", headers=h2).json()
    assert data["banner"]["show"] is False
    assert data["banner"]["dismissed"] is True


def test_api_dismiss_banner_rejects_bad_date(client):
    """日期格式不对要报错，不能把脏数据写进库里。"""
    h = auth_headers(client)
    _mk_user(client, h, "banner_user3")
    h3 = _login(client, "banner_user3")
    r = client.post("/api/me/news/banner/dismiss", json={"day": "not-a-date"}, headers=h3)
    assert r.status_code == 400


def test_api_dismiss_requires_login(client):
    """未登录不能关闭提示条。"""
    r = client.post("/api/me/news/banner/dismiss", json={})
    assert r.status_code in (401, 403)


def test_api_dismiss_only_affects_self(client):
    """A 关闭提示条不影响 B。"""
    h = auth_headers(client)
    user_a = _mk_user(client, h, "banner_user4")
    user_b = _mk_user(client, h, "banner_user5")
    h4 = _login(client, "banner_user4")
    h5 = _login(client, "banner_user5")
    ev = _today_event()
    _seed([ev], _settings(user_a["id"]))

    assert client.post("/api/me/news/banner/dismiss", json={}, headers=h4).status_code == 200

    # A 已关闭
    data4 = client.get("/api/me/news", headers=h4).json()
    assert data4["banner"]["dismissed"] is True

    # B 没关过 → 仍应显示（接口取的是**当前登录用户**的 user_id）
    data5 = client.get("/api/me/news", headers=h5).json()
    assert data5["banner"]["dismissed"] is False
    assert data5["banner"]["show"] is True
