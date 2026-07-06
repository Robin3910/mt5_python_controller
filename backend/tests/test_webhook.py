"""`/webhook` HTTP 层全场景用例（鉴权 / IP 白名单 / 去重 / 入参形态 / 响应）。

与 test_parser.py（纯解析层）配套：本文件锁定 WEBHOOK.md 描述的端到端行为，包括
鉴权与白名单顺序、token 的 4 种传入方式、请求体三种形态、去重、各类 400/401/403。

使用 fakeredis + SQLite，无需任何外部服务。settings 为进程内单例，测试通过
monkeypatch 直接改其属性来开关鉴权/白名单（webhook 处理器在请求期读取，故生效）。
"""
import asyncio
import pathlib

import fakeredis
import pytest
from fastapi.testclient import TestClient

from app.redis_store import RedisStore
from app.settings import settings

_TEST_DB = pathlib.Path(__file__).resolve().parent / "_test_api.db"

TOKEN = "s3cr3t-token"


@pytest.fixture
def client(monkeypatch):
    """每个用例一个全新 app + 全新 fakeredis（去重状态天然隔离）。"""
    def fake_from_url(cls, url=None):
        return RedisStore(fakeredis.FakeAsyncRedis(decode_responses=True))

    monkeypatch.setattr(RedisStore, "from_url", classmethod(fake_from_url))
    from app.main import app

    with TestClient(app) as c:
        yield c

    asyncio.run(__import__("app.db", fromlist=["engine"]).engine.dispose())
    try:
        _TEST_DB.unlink()
    except (FileNotFoundError, PermissionError):
        pass


def _enable_auth(monkeypatch, token=TOKEN):
    monkeypatch.setattr(settings, "enable_auth", True)
    monkeypatch.setattr(settings, "auth_token", token)


def _enable_ip_whitelist(monkeypatch, ips):
    monkeypatch.setattr(settings, "enable_ip_whitelist", True)
    monkeypatch.setattr(settings, "whitelisted_ips", ips)


from tests.test_helpers import seed_default_filters


# =====================================================================
# 1. 入参形态（格式一 JSON / 格式二 文本 / 格式三 内嵌文本）
# =====================================================================
def test_rejects_unconfigured_symbol(client):
    r = client.post("/webhook", json={"action": "buy", "symbol": "EURUSD", "volume": 0.1})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "rejected"
    assert body["signal_id"].startswith("sig_")
    assert body["action"] == "BUY"
    assert body["symbol"] == "EURUSD"
    assert body["volume"] == 0.1
    assert body["mode"] == "rejected"
    assert "未配置" in (body.get("reason") or "")
    assert body["targets"] == 0


def test_accepted_json_response_shape(client):
    seed_default_filters(client)
    r = client.post("/webhook", json={"action": "buy", "symbol": "EURUSD", "volume": 0.1})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "accepted"
    assert body["signal_id"].startswith("sig_")
    assert body["action"] == "BUY"
    assert body["symbol"] == "EURUSD"
    assert body["volume"] == 0.1
    assert body["mode"] == "sync"
    assert body["targets"] == 0


def test_accepted_plain_text_body(client):
    seed_default_filters(client)
    r = client.post("/webhook", content="close XAUUSD")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "accepted"
    assert body["action"] == "CLOSE"
    assert body["symbol"] == "XAUUSD"
    assert body["mode"] == "close"     # CLOSE 走平仓广播


def test_accepted_json_text_field(client):
    seed_default_filters(client)
    r = client.post("/webhook", json={"text": "buy EURUSD"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "accepted"
    assert body["action"] == "BUY" and body["symbol"] == "EURUSD"


def test_close_action_reports_mode_close(client):
    seed_default_filters(client)
    r = client.post("/webhook", json={"action": "close", "symbol": "EURUSD"})
    assert r.status_code == 200, r.text
    assert r.json()["mode"] == "close"


# =====================================================================
# 2. 无法解析 / 不支持的形态 -> 400 cannot parse signal
# =====================================================================
@pytest.mark.parametrize(
    "kwargs",
    [
        {"json": {"foo": "bar"}},
        {"json": {"action": "buy"}},          # 缺 symbol
        {"json": {"symbol": "EURUSD"}},       # 缺 action
        {"json": {"signal": "buy EURUSD 0.1"}},  # signal 只取动作，无品种字段
        {"content": ""},                        # 空体 -> {}
        {"content": "[1,2]"},                  # JSON 数组
        {"content": "123"},                    # JSON 数字
        {"content": "true"},                   # JSON 布尔
        {"content": "null"},                   # JSON null
        {"content": "hello world"},            # 文本无动作关键字
    ],
)
def test_unparseable_returns_400(client, kwargs):
    r = client.post("/webhook", **kwargs)
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == "cannot parse signal"


# =====================================================================
# 3. 去重（DEDUP_WINDOW 内相同指纹）
# =====================================================================
def test_duplicate_suppressed_within_window(client):
    seed_default_filters(client)
    payload = {"action": "sell", "symbol": "XAUUSD", "volume": 0.1}
    r1 = client.post("/webhook", json=payload)
    assert r1.json()["status"] == "accepted"

    r2 = client.post("/webhook", json=payload)
    body = r2.json()
    assert r2.status_code == 200
    assert body["status"] == "duplicate"
    assert body["action"] == "SELL" and body["symbol"] == "XAUUSD"
    assert "signal_id" not in body   # 去重响应不含 signal_id


def test_distinct_volume_not_deduped(client):
    seed_default_filters(client)
    a = client.post("/webhook", json={"action": "buy", "symbol": "EURUSD", "volume": 0.1})
    b = client.post("/webhook", json={"action": "buy", "symbol": "EURUSD", "volume": 0.2})
    assert a.json()["status"] == "accepted"
    assert b.json()["status"] == "accepted"   # 指纹含 volume，不同则不去重


# =====================================================================
# 4. Token 鉴权（ENABLE_AUTH）
# =====================================================================
def test_auth_disabled_accepts_without_token(client):
    """默认 ENABLE_AUTH=false：不带 token 也放行。"""
    r = client.post("/webhook", json={"action": "buy", "symbol": "EURUSD"})
    assert r.status_code == 200, r.text


def test_auth_required_missing_token_401(client, monkeypatch):
    _enable_auth(monkeypatch)
    r = client.post("/webhook", json={"action": "buy", "symbol": "EURUSD"})
    assert r.status_code == 401
    assert r.json()["detail"] == "invalid token"


def test_auth_wrong_token_401(client, monkeypatch):
    _enable_auth(monkeypatch)
    r = client.post(
        "/webhook",
        json={"action": "buy", "symbol": "EURUSD"},
        headers={"X-Auth-Token": "nope"},
    )
    assert r.status_code == 401


@pytest.mark.parametrize("channel", ["header", "query", "json_token", "json_auth_token", "bearer"])
def test_auth_valid_token_all_channels(client, monkeypatch, channel):
    """token 的 4 类传入方式都应被接受（header / query / JSON 字段 / Bearer）。"""
    seed_default_filters(client)
    _enable_auth(monkeypatch)
    url = "/webhook"
    headers = {}
    body = {"action": "buy", "symbol": "EURUSD"}

    if channel == "header":
        headers["X-Auth-Token"] = TOKEN
    elif channel == "query":
        url = f"/webhook?token={TOKEN}"
    elif channel == "json_token":
        body["token"] = TOKEN
    elif channel == "json_auth_token":
        body["auth_token"] = TOKEN
    elif channel == "bearer":
        headers["Authorization"] = f"Bearer {TOKEN}"

    r = client.post(url, json=body, headers=headers)
    assert r.status_code == 200, (channel, r.text)
    assert r.json()["status"] == "accepted"


def test_auth_header_takes_precedence_over_body(client, monkeypatch):
    """优先级 header > query > json > bearer：header 正确即放行，即便 body 里 token 是错的。"""
    _enable_auth(monkeypatch)
    r = client.post(
        "/webhook",
        json={"action": "buy", "symbol": "EURUSD", "token": "wrong"},
        headers={"X-Auth-Token": TOKEN},
    )
    assert r.status_code == 200, r.text


def test_auth_text_body_with_header_token(client, monkeypatch):
    """纯文本体无法携带 JSON token，但可用 header 传 token。"""
    seed_default_filters(client)
    _enable_auth(monkeypatch)
    r = client.post("/webhook", content="buy EURUSD", headers={"X-Auth-Token": TOKEN})
    assert r.status_code == 200, r.text
    assert r.json()["action"] == "BUY"


def test_auth_checked_before_parsing(client, monkeypatch):
    """鉴权在解析之前：token 错误时即便请求体不可解析也返回 401（而非 400）。"""
    _enable_auth(monkeypatch)
    r = client.post("/webhook", json={"foo": "bar"}, headers={"X-Auth-Token": "wrong"})
    assert r.status_code == 401


def test_auth_pass_then_parse_fail_400(client, monkeypatch):
    """token 正确但请求体不可解析：通过鉴权后在解析阶段返回 400。"""
    _enable_auth(monkeypatch)
    r = client.post("/webhook", json={"foo": "bar"}, headers={"X-Auth-Token": TOKEN})
    assert r.status_code == 400
    assert r.json()["detail"] == "cannot parse signal"


# =====================================================================
# 5. IP 白名单（ENABLE_IP_WHITELIST）
# =====================================================================
def test_ip_whitelist_disabled_allows_any(client):
    """默认关闭：任意来源放行。"""
    r = client.post("/webhook", json={"action": "buy", "symbol": "EURUSD"})
    assert r.status_code == 200


def test_ip_whitelist_denies_unlisted_source(client, monkeypatch):
    _enable_ip_whitelist(monkeypatch, "10.0.0.1")   # 不含 TestClient 来源
    r = client.post("/webhook", json={"action": "buy", "symbol": "EURUSD"})
    assert r.status_code == 403
    assert r.json()["detail"] == "ip not allowed"


def test_ip_whitelist_allows_via_x_forwarded_for(client, monkeypatch):
    _enable_ip_whitelist(monkeypatch, "9.9.9.9")
    r = client.post(
        "/webhook",
        json={"action": "buy", "symbol": "EURUSD"},
        headers={"X-Forwarded-For": "9.9.9.9, 1.1.1.1"},
    )
    assert r.status_code == 200, r.text


def test_ip_whitelist_uses_first_xff_hop(client, monkeypatch):
    """X-Forwarded-For 取第一跳：白名单含第一跳放行、只含第二跳则拒绝。"""
    _enable_ip_whitelist(monkeypatch, "9.9.9.9")
    ok = client.post(
        "/webhook",
        json={"action": "buy", "symbol": "EURUSD"},
        headers={"X-Forwarded-For": "9.9.9.9, 8.8.8.8"},
    )
    assert ok.status_code == 200, ok.text

    monkeypatch.setattr(settings, "whitelisted_ips", "8.8.8.8")   # 只允许第二跳
    deny = client.post(
        "/webhook",
        json={"action": "buy", "symbol": "EURUSD"},
        headers={"X-Forwarded-For": "9.9.9.9, 8.8.8.8"},
    )
    assert deny.status_code == 403


def test_ip_whitelist_checked_before_auth(client, monkeypatch):
    """白名单在鉴权之前：IP 不通过时，即便 token 正确也返回 403。"""
    _enable_ip_whitelist(monkeypatch, "10.0.0.1")
    _enable_auth(monkeypatch)
    r = client.post(
        "/webhook",
        json={"action": "buy", "symbol": "EURUSD"},
        headers={"X-Auth-Token": TOKEN},
    )
    assert r.status_code == 403
