"""策略模版与策略管理 API 单元测试。"""
import asyncio
import pathlib

import fakeredis
import pytest
from fastapi.testclient import TestClient

from app.redis_store import RedisStore
from app.strategy_templates import TEMPLATE_1_ID, TEMPLATE_1_NAME

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


from tests.test_helpers import auth_headers


def test_strategy_endpoints_require_auth(client):
    assert client.get("/api/strategies").status_code == 401
    assert client.get("/api/strategies/templates").status_code == 401
    assert client.post("/api/strategies", json={
        "template_id": TEMPLATE_1_ID, "name": "x", "symbol": "XAUUSD",
    }).status_code == 401


def test_list_templates_contains_template_1(client):
    h = auth_headers(client)
    r = client.get("/api/strategies/templates", headers=h)
    assert r.status_code == 200, r.text
    items = r.json()
    assert len(items) >= 1
    tpl = next(t for t in items if t["template_id"] == TEMPLATE_1_ID)
    assert tpl["name"] == TEMPLATE_1_NAME
    assert len(tpl["rules"]) == 2
    types = {rule["type"] for rule in tpl["rules"]}
    assert types == {1, 2}  # 逆势 + 顺势
    by_type = {rule["type"]: rule for rule in tpl["rules"]}
    assert by_type[1]["status"] == 1
    assert by_type[1]["action"] == "all"
    assert by_type[1]["lot_times"] == 1.1
    assert by_type[1]["extra_lot"] == 0
    assert by_type[1]["max_allow_num"] == 3
    assert by_type[1]["batch_enabled"] is True
    assert by_type[1]["batch_action"] == "all"
    assert by_type[1]["batch_count"] == 3
    assert by_type[1]["total_lot_limit"] == 10
    assert len(by_type[1]["batch_levels"]) == 3
    assert by_type[2]["status"] == 1
    assert by_type[2]["action"] == "all"
    assert by_type[2]["lot_times"] == 0.8
    assert by_type[2]["extra_lot"] == 0
    assert by_type[2]["max_allow_num"] == 10
    assert by_type[2]["batch_enabled"] is False
    assert by_type[2]["batch_action"] == "all"


def test_create_strategy_from_template(client):
    h = auth_headers(client)
    r = client.post(
        "/api/strategies",
        json={"template_id": TEMPLATE_1_ID, "name": "黄金策略A", "symbol": "xauusd"},
        headers=h,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["strategy_id"].startswith("sty_")
    assert body["name"] == "黄金策略A"
    assert body["symbol"] == "XAUUSD"  # 自动大写
    assert body["template_id"] == TEMPLATE_1_ID
    assert body["template_name"] == TEMPLATE_1_NAME
    assert body["enabled"] is True
    assert len(body["rules"]) == 2
    assert {rule["type"] for rule in body["rules"]} == {1, 2}


def test_create_strategy_with_custom_rules(client):
    """选模版后可覆盖默认规则参数，落库以自定义值为准。"""
    h = auth_headers(client)
    r = client.post(
        "/api/strategies",
        json={
            "template_id": TEMPLATE_1_ID,
            "name": "自定义参数策略",
            "symbol": "XAUUSD",
            "rules": [
                {
                    "type": 1,
                    "status": 1,
                    "action": "buy",
                    "point": 200,
                    "lot_times": 1.5,
                    "extra_lot": 0.02,
                    "max_allow_num": 3,
                },
                {
                    "type": 2,
                    "status": 0,
                    "action": "sell",
                    "point": 50,
                    "lot_times": 2,
                    "extra_lot": 0,
                    "max_allow_num": 1,
                },
            ],
        },
        headers=h,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    by_type = {rule["type"]: rule for rule in body["rules"]}
    assert by_type[1]["action"] == "buy"
    assert by_type[1]["point"] == 200
    assert by_type[1]["lot_times"] == 1.5
    assert by_type[1]["extra_lot"] == 0.02
    assert by_type[1]["max_allow_num"] == 3
    assert by_type[2]["status"] == 0
    assert by_type[2]["action"] == "sell"


def test_create_strategy_duplicate_name_409(client):
    h = auth_headers(client)
    payload = {"template_id": TEMPLATE_1_ID, "name": "重名策略", "symbol": "EURUSD"}
    assert client.post("/api/strategies", json=payload, headers=h).status_code == 201
    r = client.post("/api/strategies", json=payload, headers=h)
    assert r.status_code == 409


def test_create_strategy_unknown_template_400(client):
    h = auth_headers(client)
    r = client.post(
        "/api/strategies",
        json={"template_id": "tpl_missing", "name": "坏模版", "symbol": "XAUUSD"},
        headers=h,
    )
    assert r.status_code == 400
    assert "模版不存在" in r.json()["detail"]


def test_list_search_toggle_delete_strategy(client):
    h = auth_headers(client)
    created = client.post(
        "/api/strategies",
        json={"template_id": TEMPLATE_1_ID, "name": "可搜策略", "symbol": "GBPUSD"},
        headers=h,
    ).json()
    sid = created["strategy_id"]

    listed = client.get("/api/strategies", headers=h).json()
    assert any(s["strategy_id"] == sid for s in listed)

    hit = client.get("/api/strategies", params={"q": "gbpusd"}, headers=h).json()
    assert [s["strategy_id"] for s in hit] == [sid]

    off = client.patch(f"/api/strategies/{sid}", json={"enabled": False}, headers=h)
    assert off.status_code == 200
    assert off.json()["enabled"] is False

    assert client.delete(f"/api/strategies/{sid}", headers=h).status_code == 200
    assert client.get(f"/api/strategies/{sid}", headers=h).status_code == 404
