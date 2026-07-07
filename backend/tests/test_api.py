"""End-to-end API smoke test: login -> create node -> WS auth -> webhook -> trade_result.

Uses fakeredis (patched into RedisStore.from_url) and a SQLite test DB so it runs
without external services.
"""
import asyncio
import pathlib
import fakeredis
import pytest
from fastapi.testclient import TestClient

from app.redis_store import RedisStore

_TEST_DB = pathlib.Path(__file__).resolve().parent / "_test_api.db"


@pytest.fixture
def client(monkeypatch):
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


from tests.test_helpers import seed_default_filters


def _auth(client) -> dict:
    r = client.post("/api/login", json={"username": "admin", "password": "admin123"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _node_token(client, headers) -> str:
    """获取全局节点接入令牌（启动期已自动生成）。"""
    r = client.get("/api/config/node-token", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["token"]


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_login_rejects_bad_credentials(client):
    r = client.post("/api/login", json={"username": "admin", "password": "wrong"})
    assert r.status_code == 401


def test_change_password(client):
    h = _auth(client)
    r = client.post(
        "/api/change-password",
        json={"current_password": "admin123", "new_password": "newpass99"},
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True

    # old password rejected
    r = client.post("/api/login", json={"username": "admin", "password": "admin123"})
    assert r.status_code == 401

    # new password works
    r = client.post("/api/login", json={"username": "admin", "password": "newpass99"})
    assert r.status_code == 200, r.text


def test_change_password_rejects_wrong_current(client):
    h = _auth(client)
    r = client.post(
        "/api/change-password",
        json={"current_password": "wrong", "new_password": "newpass99"},
        headers=h,
    )
    assert r.status_code == 401


def test_change_password_requires_auth(client):
    r = client.post(
        "/api/change-password",
        json={"current_password": "admin123", "new_password": "newpass99"},
    )
    assert r.status_code == 401


def test_nodes_require_auth(client):
    assert client.get("/api/nodes").status_code == 401


def test_full_flow(client):
    h = _auth(client)
    token = _node_token(client, h)

    # 管理员预先创建节点（也可省略；node_client 首次登录会自动注册）
    r = client.post(
        "/api/nodes",
        json={
            "name": "node-1",
            "mt5_login": 5001,
            "filters": {
                "EURUSD": {
                    "follow_sync": True,
                    "follow_poll": True,
                    "lot_mode": "signal",
                    "poll_order": 0,
                }
            },
        },
        headers=h,
    )
    assert r.status_code == 201, r.text
    node_id = r.json()["node_id"]
    assert "token" not in r.json(), "POST /api/nodes 不应再返回每节点 token"

    with client.websocket_connect("/ws/node") as ws:
        ws.send_json({"type": "auth", "data": {"token": token, "mt5_login": 5001}})
        ack = ws.receive_json()
        assert ack["type"] == "auth_ok"
        assert ack["data"]["node_id"] == node_id

        ws.send_json(
            {
                "type": "account",
                "data": {
                    "account": {"login": 5001, "balance": 1000, "equity": 1000},
                    "positions": [],
                    "prices": {"EURUSD": 1.0742},
                },
            }
        )

        seed_default_filters(client)

        # webhook signal -> sync dispatch -> node receives an open command
        r = client.post("/webhook", json={"action": "buy", "symbol": "EURUSD", "volume": 0.1})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "accepted"
        assert body["mode"] == "sync"

        cmd = ws.receive_json()
        assert cmd["cmd"] == "open"
        assert cmd["symbol"] == "EURUSD"
        assert cmd["action"] == "BUY"
        assert cmd["volume"] == 0.1

        # node acks the fill
        ws.send_json(
            {
                "type": "trade_result",
                "data": {"signal_id": cmd["signal_id"], "symbol": "EURUSD", "success": True, "order": 99},
            }
        )

    # node listing reflects the registered node
    r = client.get("/api/nodes", headers=h)
    assert r.status_code == 200
    assert any(n["node_id"] == node_id for n in r.json())


def test_duplicate_node_login_rejected(client):
    h = _auth(client)
    token = _node_token(client, h)
    r = client.post(
        "/api/nodes",
        json={"name": "dup", "mt5_login": 6001},
        headers=h,
    )
    assert r.status_code == 201, r.text

    with client.websocket_connect("/ws/node") as ws1:
        ws1.send_json({"type": "auth", "data": {"token": token, "mt5_login": 6001}})
        assert ws1.receive_json()["type"] == "auth_ok"

        # 同一 MT5 登录号的第二个连接应被拒绝，并带上原因
        with client.websocket_connect("/ws/node") as ws2:
            ws2.send_json({"type": "auth", "data": {"token": token, "mt5_login": 6001}})
            msg = ws2.receive_json()
            assert msg["type"] == "auth_fail"
            assert msg["data"]["reason"] == "already_online"

        # 首个连接仍然在线，可继续正常上报（探活 ping 可能先于 pong 到达，需消费掉）
        ws1.send_json({"type": "heartbeat", "data": {}})
        types = set()
        for _ in range(3):
            types.add(ws1.receive_json()["type"])
            if "pong" in types:
                break
        assert "pong" in types


def test_invalid_global_token_rejected(client):
    """非法全局令牌：直接拒绝，不暴露节点是否存在。"""
    with client.websocket_connect("/ws/node") as ws:
        ws.send_json({"type": "auth", "data": {"token": "wrong-token", "mt5_login": 7001}})
        msg = ws.receive_json()
        assert msg["type"] == "auth_fail"
        assert msg["data"]["reason"] == "invalid_token"


def test_missing_mt5_login_rejected(client):
    h = _auth(client)
    token = _node_token(client, h)
    with client.websocket_connect("/ws/node") as ws:
        ws.send_json({"type": "auth", "data": {"token": token}})  # 缺 mt5_login
        msg = ws.receive_json()
        assert msg["type"] == "auth_fail"
        assert msg["data"]["reason"] == "missing_mt5_login"


from app.node_service import default_node_filters_from_global


def test_default_node_filters_from_global():
    cfg = default_node_filters_from_global(
        {
            "EURUSD": {"enabled": True},
            "xauusd.m": {"enabled": True},
            "bad": "not-a-dict",
        }
    )
    assert set(cfg.keys()) == {"EURUSD", "XAUUSD.M"}
    assert cfg["EURUSD"] == {
        "follow_sync": True,
        "follow_poll": True,
        "lot_mode": "fixed",
        "lot": 0.01,
        "poll_order": 0,
    }
    assert default_node_filters_from_global({}) == {}


def test_auto_register_on_first_login(client):
    """node_client 用未注册的 mt5_login 登录时，后端按默认配置自动入库。"""
    h = seed_default_filters(client)
    token = _node_token(client, h)

    # 库中不存在该 mt5_login
    nodes_before = client.get("/api/nodes", headers=h).json()
    assert not any(n["mt5_login"] == 8001 for n in nodes_before)

    with client.websocket_connect("/ws/node") as ws:
        ws.send_json({"type": "auth", "data": {"token": token, "mt5_login": 8001}})
        ack = ws.receive_json()
        assert ack["type"] == "auth_ok"
        new_node_id = ack["data"]["node_id"]

    # 自动注册的节点应使用默认配置，并按中控台已有品种生成按币种配置
    r = client.get("/api/nodes", headers=h)
    created = next(n for n in r.json() if n["mt5_login"] == 8001)
    assert created["node_id"] == new_node_id
    assert created["name"] == "node-8001"
    assert created["enabled"] is True
    filters = created.get("filters") or {}
    assert set(filters.keys()) == {"EURUSD", "XAUUSD", "GBPUSD"}
    for sym in filters.values():
        assert sym["follow_sync"] is True
        assert sym["follow_poll"] is True
        assert sym["lot_mode"] == "fixed"
        assert sym["lot"] == 0.01
        assert sym["poll_order"] == 0


def test_create_node_duplicate_mt5_login_returns_409(client):
    h = _auth(client)
    r1 = client.post(
        "/api/nodes",
        json={"name": "first", "mt5_login": 9001},
        headers=h,
    )
    assert r1.status_code == 201

    r2 = client.post(
        "/api/nodes",
        json={"name": "dup", "mt5_login": 9001},
        headers=h,
    )
    assert r2.status_code == 409
    assert "9001" in r2.json()["detail"]


def test_list_nodes_search_by_name_and_mt5_login(client):
    h = _auth(client)
    client.post("/api/nodes", json={"name": "Alpha VPS", "mt5_login": 60108484}, headers=h)
    client.post("/api/nodes", json={"name": "Beta Server", "mt5_login": 70001234}, headers=h)

    by_name = client.get("/api/nodes", params={"q": "alpha"}, headers=h)
    assert by_name.status_code == 200
    assert len(by_name.json()) == 1
    assert by_name.json()[0]["name"] == "Alpha VPS"

    by_login = client.get("/api/nodes", params={"q": "1234"}, headers=h)
    assert by_login.status_code == 200
    assert len(by_login.json()) == 1
    assert by_login.json()[0]["mt5_login"] == 70001234

    empty = client.get("/api/nodes", params={"q": "missing-node"}, headers=h)
    assert empty.status_code == 200
    assert empty.json() == []


def test_node_token_rotate(client):
    """重置令牌后，旧令牌应失效，新令牌可用。"""
    h = _auth(client)
    old_token = _node_token(client, h)

    r = client.post("/api/config/node-token/rotate", headers=h)
    assert r.status_code == 200, r.text
    new_token = r.json()["token"]
    assert new_token != old_token

    # 旧令牌已失效
    with client.websocket_connect("/ws/node") as ws:
        ws.send_json({"type": "auth", "data": {"token": old_token, "mt5_login": 10001}})
        assert ws.receive_json()["data"]["reason"] == "invalid_token"

    # 新令牌可正常接入并触发自动注册
    with client.websocket_connect("/ws/node") as ws:
        ws.send_json({"type": "auth", "data": {"token": new_token, "mt5_login": 10001}})
        assert ws.receive_json()["type"] == "auth_ok"


def test_webhook_duplicate_suppressed(client):
    seed_default_filters(client)
    _auth(client)
    p = {"action": "sell", "symbol": "XAUUSD", "volume": 0.1}
    r1 = client.post("/webhook", json=p)
    assert r1.json()["status"] == "accepted"
    r2 = client.post("/webhook", json=p)
    assert r2.json()["status"] == "duplicate"


def test_webhook_rejects_unparseable(client):
    r = client.post("/webhook", json={"foo": "bar"})
    assert r.status_code == 400


def test_list_signal_events(client):
    h = seed_default_filters(client)
    payload = {"action": "buy", "symbol": "EURUSD", "volume": 0.1, "sl": 1.05}
    wh = client.post("/webhook", json=payload)
    assert wh.status_code == 200, wh.text

    r = client.get("/api/events/signals", headers=h, params={"page": 1, "page_size": 10})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] >= 1
    latest = body["items"][0]
    assert latest["symbol"] == "EURUSD"
    assert latest["action"] == "BUY"
    assert latest["parsed_ok"] is True
    assert '"action"' in (latest.get("raw_payload") or "")
    assert "buy" in (latest.get("raw_payload") or "").lower()


def test_2fa_login_flow(client):
    h = _auth(client)

    setup = client.post("/api/2fa/setup", headers=h)
    assert setup.status_code == 200, setup.text
    secret = setup.json()["secret"]

    import pyotp

    code = pyotp.TOTP(secret).now()
    confirm = client.post("/api/2fa/confirm", json={"totp_code": code}, headers=h)
    assert confirm.status_code == 200, confirm.text

    r1 = client.post("/api/login", json={"username": "admin", "password": "admin123"})
    assert r1.status_code == 200, r1.text
    body = r1.json()
    assert body["requires_2fa"] is True
    login_token = body["login_token"]

    code2 = pyotp.TOTP(secret).now()
    r2 = client.post("/api/login/2fa", json={"login_token": login_token, "totp_code": code2})
    assert r2.status_code == 200, r2.text
    assert r2.json()["token"]

    disable = client.post(
        "/api/2fa/disable",
        json={"password": "admin123", "totp_code": pyotp.TOTP(secret).now()},
        headers=h,
    )
    assert disable.status_code == 200, disable.text
