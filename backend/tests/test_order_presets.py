"""开仓参数预设（Order Preset）的接口与单元用例。

覆盖：
- CRUD：GET / PUT / DELETE，upsert 语义，字段级「传什么改什么」；
- 归属隔离：A 看不到 B 的预设；body 里的 user_id 不生效；
- 通配模板：`*` 通用模板的解析优先级（精确 > 通配）；
- 三种开仓方式互不污染；
- 老用户种子回填：从已有策略实例推导出预设；
- 归一化：非法值回落默认、base_ratio 夹取。
"""
import pathlib

import fakeredis
import pytest
from fastapi.testclient import TestClient

from app import order_presets
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


def _get(client, h) -> list[dict]:
    r = client.get("/api/me/order-presets", headers=h)
    assert r.status_code == 200, r.text
    return r.json()["presets"]


def _put(client, h, **payload) -> dict:
    r = client.put("/api/me/order-presets", json=payload, headers=h)
    assert r.status_code == 200, r.text
    return r.json()["preset"]


# ---------------------------------------------------------------------------
# 基础 CRUD
# ---------------------------------------------------------------------------
def test_preset_crud(client):
    h = auth_headers(client)
    assert _get(client, h) == []

    saved = _put(
        client, h, symbol="XAUUSD", strategy_type="trend_market",
        risk_amount=200, rr_ratio=3.0, base_ratio=40, breakeven_times=1.5,
    )
    assert saved["risk_amount"] == 200
    assert saved["rr_ratio"] == 3.0
    assert saved["base_ratio"] == 40
    assert saved["breakeven_times"] == 1.5
    assert saved["symbol"] == "XAUUSD"

    rows = _get(client, h)
    assert len(rows) == 1
    assert rows[0]["risk_amount"] == 200

    # upsert：再存一次不新增行
    _put(client, h, symbol="XAUUSD", strategy_type="trend_market", risk_amount=250)
    rows = _get(client, h)
    assert len(rows) == 1
    assert rows[0]["risk_amount"] == 250
    # 字段级「传什么改什么」：没传的字段保持原值
    assert rows[0]["rr_ratio"] == 3.0

    r = client.delete("/api/me/order-presets/XAUUSD/trend_market", headers=h)
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert _get(client, h) == []


def test_preset_requires_login(client):
    r = client.get("/api/me/order-presets")
    assert r.status_code == 401


def test_preset_strategy_types_are_isolated(client):
    h = auth_headers(client)
    _put(client, h, symbol="XAUUSD", strategy_type="trend_market", risk_amount=100)
    _put(client, h, symbol="XAUUSD", strategy_type="trend_limit", risk_amount=200)
    _put(client, h, symbol="XAUUSD", strategy_type="grid", max_allow_loss=300)

    rows = _get(client, h)
    assert len(rows) == 3
    by_type = {r["strategy_type"]: r for r in rows}
    assert by_type["trend_market"]["risk_amount"] == 100
    assert by_type["trend_limit"]["risk_amount"] == 200
    assert by_type["grid"]["max_allow_loss"] == 300
    # 三份互不污染：市价那份的 rr 没被限价那份带走
    assert by_type["trend_market"]["rr_ratio"] == order_presets.DEFAULT_RR_RATIO


def test_preset_user_isolation(client):
    admin = auth_headers(client)
    _mk_user(client, admin, "alice")
    alice = _login(client, "alice")
    bob = _login(client, "bob") if False else None

    _put(client, alice, symbol="XAUUSD", strategy_type="trend_market", risk_amount=111)
    assert len(_get(client, alice)) == 1
    # admin 看不到 alice 的预设
    assert _get(client, admin) == []


def test_preset_ignores_body_user_id(client):
    """body 里带的 user_id 不生效 —— 归属写死为当前登录人。"""
    admin = auth_headers(client)
    _mk_user(client, admin, "alice")
    alice = _login(client, "alice")
    other = _mk_user(client, admin, "bob")

    _put(
        client, alice, symbol="XAUUSD", strategy_type="trend_market",
        risk_amount=100, user_id=other["id"],
    )
    rows = _get(client, alice)
    assert len(rows) == 1
    assert _get(client, _login(client, "bob")) == []


# ---------------------------------------------------------------------------
# 通配模板
# ---------------------------------------------------------------------------
def test_wildcard_preset(client):
    h = auth_headers(client)
    _put(client, h, symbol="*", strategy_type="trend_market", risk_amount=88)

    rows = _get(client, h)
    assert len(rows) == 1
    assert rows[0]["symbol"] == "*"


def test_resolve_preset_prefers_exact_over_wildcard(client):
    h = auth_headers(client)
    _put(client, h, symbol="*", strategy_type="trend_market", risk_amount=88)
    _put(client, h, symbol="XAUUSD", strategy_type="trend_market", risk_amount=222)

    async def _resolve():
        return await order_presets.resolve_preset(
            _user_id(h), "XAUUSD", "trend_market",
        )

    from anyio.from_thread import start_blocking_portal

    with start_blocking_portal() as portal:
        got = portal.call(_resolve)
    assert got is not None
    assert got.risk_amount == 222


def test_resolve_preset_falls_back_to_wildcard(client):
    h = auth_headers(client)
    _put(client, h, symbol="*", strategy_type="trend_market", risk_amount=88)

    async def _resolve():
        return await order_presets.resolve_preset(
            _user_id(h), "EURUSD", "trend_market",
        )

    from anyio.from_thread import start_blocking_portal

    with start_blocking_portal() as portal:
        got = portal.call(_resolve)
    assert got is not None
    assert got.risk_amount == 88
    assert got.symbol == "*"


def _user_id(headers: dict) -> int:
    """从 JWT 里取 user_id（测试里直接解 payload，够用）。"""
    import base64
    import json

    token = headers["Authorization"].split(" ", 1)[1]
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return int(json.loads(base64.urlsafe_b64decode(payload))["uid"])


# ---------------------------------------------------------------------------
# 归一化
# ---------------------------------------------------------------------------
def test_preset_normalizes_values(client):
    h = auth_headers(client)
    saved = _put(
        client, h, symbol="  xauusd ", strategy_type="TREND_MARKET",
        risk_amount=-5, rr_ratio=0, base_ratio=0, grid_mode="weird",
    )
    assert saved["symbol"] == "XAUUSD"
    assert saved["strategy_type"] == "trend_market"
    # 负数回落默认值（risk_amount 传 -5 → 保持默认 100）
    assert saved["risk_amount"] == order_presets.DEFAULT_RISK_AMOUNT
    # rr_ratio 允许 0（= 不设止盈），与后端规则口径一致
    assert saved["rr_ratio"] == 0
    # base_ratio 下限是 1（0 会让底仓无手数）
    assert saved["base_ratio"] == order_presets.BASE_RATIO_MIN
    # 非法网格模式回落等差
    assert saved["grid_mode"] == order_presets.DEFAULT_GRID_MODE


def test_preset_rejects_bad_body(client):
    h = auth_headers(client)
    r = client.put("/api/me/order-presets", json=["not", "a", "dict"], headers=h)
    assert r.status_code in (400, 422)


# ---------------------------------------------------------------------------
# 老用户种子回填
# ---------------------------------------------------------------------------
def test_seed_preset_from_existing_strategy(client):
    """老用户已有策略实例（分组名符合约定）→ 读取预设时自动回填。"""
    h = auth_headers(client)
    _mk_user(client, h, "carol")
    carol = _login(client, "carol")

    # 造一个策略实例 + 分组：策略是模版2（以损定量），分组名按约定
    r = client.post(
        "/api/strategies",
        json={
            "template_id": "tpl_2",
            "name": "carol-trend",
            "symbol": "XAUUSD",
            "rules": [{
                "type": 3, "status": 1, "action": "all",
                "risk_amount": 300, "rr_ratio": 4.0, "base_ratio": 50,
                "breakeven_enabled": True, "breakeven_times": 1.5,
                "entry_mode": "market",
            }],
        },
        headers=carol,
    )
    assert r.status_code == 201, r.text
    strategy_id = r.json()["strategy_id"]

    r = client.post(
        "/api/groups",
        json={
            "name": "trend_market-carol-12345-XAUUSD",
            "strategy_id": strategy_id,
        },
        headers=carol,
    )
    assert r.status_code == 201, r.text

    rows = _get(client, carol)
    seeded = [x for x in rows if x["symbol"] == "XAUUSD" and x["strategy_type"] == "trend_market"]
    assert len(seeded) == 1
    assert seeded[0]["risk_amount"] == 300
    assert seeded[0]["rr_ratio"] == 4.0
    assert seeded[0]["base_ratio"] == 50
    assert seeded[0]["breakeven_times"] == 1.5
