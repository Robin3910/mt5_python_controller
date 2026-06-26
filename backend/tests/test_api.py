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


def _auth(client) -> dict:
    r = client.post("/api/login", json={"username": "admin", "password": "admin123"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


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

    # create a node -> get one-time token
    r = client.post(
        "/api/nodes",
        json={"name": "node-1", "lot_mode": "signal", "mt5_login": 5001},
        headers=h,
    )
    assert r.status_code == 201, r.text
    node_id = r.json()["node_id"]
    token = r.json()["token"]

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
    r = client.post(
        "/api/nodes",
        json={"name": "dup", "lot_mode": "global", "mt5_login": 6001},
        headers=h,
    )
    assert r.status_code == 201, r.text
    token = r.json()["token"]

    with client.websocket_connect("/ws/node") as ws1:
        ws1.send_json({"type": "auth", "data": {"token": token, "mt5_login": 6001}})
        assert ws1.receive_json()["type"] == "auth_ok"

        # 同一令牌的第二个连接应被拒绝，并带上原因
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


def test_mt5_login_mismatch_rejected(client):
    h = _auth(client)
    r = client.post(
        "/api/nodes",
        json={"name": "bind", "lot_mode": "global", "mt5_login": 7001},
        headers=h,
    )
    assert r.status_code == 201, r.text
    token = r.json()["token"]

    with client.websocket_connect("/ws/node") as ws:
        ws.send_json({"type": "auth", "data": {"token": token, "mt5_login": 9999}})
        msg = ws.receive_json()
        assert msg["type"] == "auth_fail"
        assert msg["data"]["reason"] == "mt5_login_mismatch"


def test_webhook_duplicate_suppressed(client):
    _auth(client)
    p = {"action": "sell", "symbol": "XAUUSD", "volume": 0.1}
    r1 = client.post("/webhook", json=p)
    assert r1.json()["status"] == "accepted"
    r2 = client.post("/webhook", json=p)
    assert r2.json()["status"] == "duplicate"


def test_webhook_rejects_unparseable(client):
    r = client.post("/webhook", json={"foo": "bar"})
    assert r.status_code == 400


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
