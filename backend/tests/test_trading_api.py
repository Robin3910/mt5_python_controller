"""交易页一键触发 API 单测：主节点校验、限价必填、网格方向、自动建组、复用。

参考 test_api.py 的 fixture/client 模式：fakeredis + SQLite 测试库 + admin 登录 helper。
process_signal 内部会读 signal_concurrent / Redis 状态 / 节点 WS 等，链路复杂，
这里只验证前置校验和「找 / 建策略+分组」逻辑，故 mock 掉 trading_api.process_signal。
主节点在线校验同样旁路：测试环境下默认没有节点在线。
"""
import pathlib

import fakeredis
import pytest
from fastapi.testclient import TestClient

from app.connections import manager
from app.redis_store import RedisStore

# 必须与 conftest.py 里设置的 MYSQL_DSN 指向同一文件，否则 env 启动时
# 创建的 engine 会指向旧路径，新 fixture 的 reset_test_db 清的是另一个文件。
_TEST_DB = pathlib.Path(__file__).resolve().parent / "_test_api.db"


@pytest.fixture
def client(monkeypatch):
    def fake_from_url(cls, url=None):
        return RedisStore(fakeredis.FakeAsyncRedis(decode_responses=True))

    from tests.test_helpers import drop_test_db, reset_test_db

    reset_test_db(_TEST_DB)
    monkeypatch.setattr(RedisStore, "from_url", classmethod(fake_from_url))
    from app.main import app

    with TestClient(app) as c:
        yield c
    drop_test_db(_TEST_DB)


def _admin_token(client: TestClient) -> str:
    """admin 登录并取 token（直接持有，避免每个请求再解 header）。"""
    r = client.post("/api/login", json={"username": "admin", "password": "admin123"})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _admin_headers(client: TestClient) -> dict:
    """从 test_helpers 复用 admin 登录 helper，便于和其它测试保持一致。"""
    from tests.test_helpers import auth_headers
    return auth_headers(client)


def _make_primary_node(client: TestClient, *, name: str, mt5_login: int) -> str:
    """通过公开 API 建一个主节点，返回 node_id。"""
    h = _admin_headers(client)
    r = client.post("/api/nodes", json={"name": name, "mt5_login": mt5_login}, headers=h)
    assert r.status_code == 201, r.text
    nid = r.json()["node_id"]
    set_p = client.post(f"/api/nodes/{nid}/primary", json={"is_primary": True}, headers=h)
    assert set_p.status_code == 200, set_p.text
    return nid


def _post_quick_trigger(
    client: TestClient, monkeypatch, admin_token: str, body: dict,
    *,
    patch_grid_defaults: bool = False,
):
    """旁路 manager.is_node_online 与 trading_api.process_signal 后发起请求。

    patch_grid_defaults=True 时，还会替换 trading_api._build_default_rules 里的
    网格默认参数（默认 price_lower/price_upper 都为 0，会触发创建校验失败）。
    """
    from app import trading_api
    from app.strategy_templates import (
        TEMPLATE_3_ID,
        default_grid_rule,
    )

    headers = {"Authorization": f"Bearer {admin_token}"}
    with monkeypatch.context() as mp:
        mp.setattr(manager, "is_node_online", lambda _node_id: True)

        async def _fake_process_signal(*_a, **_kw):
            return {
                "status": "accepted",
                "signal_id": "test_sig",
                "groups": 1,
                "targets": 1,
            }

        mp.setattr(trading_api, "process_signal", _fake_process_signal)
        if patch_grid_defaults:
            _original_build = trading_api._build_default_rules

            def _build(strategy_type: str):
                if strategy_type == "grid":
                    rule = default_grid_rule().to_dict()
                    # 默认 0/0 会被创建校验拒绝；给个合法区间
                    rule["price_lower"] = 1900.0
                    rule["price_upper"] = 2000.0
                    return TEMPLATE_3_ID, [rule]
                return _original_build(strategy_type)

            mp.setattr(trading_api, "_build_default_rules", _build)
        return client.post("/api/trading/quick-trigger", json=body, headers=headers)


# ---------------------------------------------------------------------------
# 测试用例
# ---------------------------------------------------------------------------

def test_quick_trigger_rejects_non_primary(client):
    """非主节点（不存在的 node_id）应被拒绝 → 400 + '主节点不存在或未启用'。"""
    admin_token = _admin_token(client)
    # 不需要 mock is_node_online：前置 store.get_node 校验就先失败
    r = client.post(
        "/api/trading/quick-trigger",
        json={
            "node_id": "does_not_exist",
            "strategy_type": "trend_market",
            "symbol": "XAUUSD",
            "volume": 0.01,
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 400, r.text
    assert "主节点不存在" in r.json()["detail"]


def test_quick_trigger_requires_entry_price_for_limit(client, monkeypatch):
    """trend_limit 模式缺入场价 → 400 + '限价开仓必须填写入场价'。"""
    admin_token = _admin_token(client)
    _make_primary_node(client, name="limit-node", mt5_login=70222)

    # 拿到真实 node_id（POST /api/nodes 内部按 (序号-mt5_login) 生成名称）
    h = _admin_headers(client)
    listing = client.get("/api/nodes", headers=h).json()
    primary = next(n for n in listing if n["is_primary"] and n["mt5_login"] == 70222)

    body = {
        "node_id": primary["node_id"],
        "strategy_type": "trend_limit",
        "symbol": "XAUUSD",
        "volume": 0.01,
        "sl": 1900.0,
        # 注意：故意不传 entry_price
    }

    r = _post_quick_trigger(client, monkeypatch, admin_token, body)
    assert r.status_code == 400, r.text
    assert "入场价" in r.json()["detail"]


def test_quick_trigger_grid_creates_strategy_and_group(client, monkeypatch):
    """grid + direction=all：自动创建分组+策略，返回 group_id/strategy_id。

    分组/策略名按 (strategy_type-username-mt5_login-symbol) 拼接：grid-admin-70333-XAUUSD。
    """
    admin_token = _admin_token(client)
    _make_primary_node(client, name="grid-node", mt5_login=70333)

    h = _admin_headers(client)
    listing = client.get("/api/nodes", headers=h).json()
    primary = next(n for n in listing if n["is_primary"] and n["mt5_login"] == 70333)

    body = {
        "node_id": primary["node_id"],
        "strategy_type": "grid",
        "symbol": "XAUUSD",
        "volume": 0.01,
        "direction": "all",
    }

    r = _post_quick_trigger(client, monkeypatch, admin_token, body, patch_grid_defaults=True)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["group_id"], "应返回 group_id"
    assert j["strategy_id"], "应返回 strategy_id"
    assert j["result"]["status"] == "accepted", "process_signal 被 mock 返回 accepted"

    # 校验：分组和策略真的落到了 Redis，名称含 grid-admin-70333-XAUUSD
    groups = client.get("/api/groups", headers=h).json()
    strats = client.get("/api/strategies", headers=h).json()
    matched_group = next(
        (g for g in groups if g["group_id"] == j["group_id"]), None,
    )
    assert matched_group is not None, f"分组未落库: {j['group_id']}"
    assert matched_group["name"] == "grid-admin-70333-XAUUSD", matched_group["name"]
    matched_strat = next(
        (s for s in strats if s["strategy_id"] == j["strategy_id"]), None,
    )
    assert matched_strat is not None, f"策略未落库: {j['strategy_id']}"
    assert matched_strat["name"] == "grid-admin-70333-XAUUSD", matched_strat["name"]


def test_quick_trigger_reuses_existing(client, monkeypatch):
    """同 (strategy_type, username, mt5_login, symbol) 第二次触发应复用已有 group/strategy。"""
    admin_token = _admin_token(client)
    _make_primary_node(client, name="reuse-node", mt5_login=70444)

    h = _admin_headers(client)
    listing = client.get("/api/nodes", headers=h).json()
    primary = next(n for n in listing if n["is_primary"] and n["mt5_login"] == 70444)

    body = {
        "node_id": primary["node_id"],
        "strategy_type": "grid",
        "symbol": "XAUUSD",
        "volume": 0.01,
        "direction": "all",
    }

    a = _post_quick_trigger(client, monkeypatch, admin_token, body, patch_grid_defaults=True).json()
    b = _post_quick_trigger(client, monkeypatch, admin_token, body, patch_grid_defaults=True).json()

    assert a["group_id"] == b["group_id"], "第二次触发应复用同一分组"
    assert a["strategy_id"] == b["strategy_id"], "第二次触发应复用同一策略"

    # 库里也应只有一个分组、一个策略被创建
    groups = client.get("/api/groups", headers=h).json()
    strats = client.get("/api/strategies", headers=h).json()
    matched_groups = [g for g in groups if g["name"].startswith("grid-admin-70444-")]
    matched_strats = [s for s in strats if s["name"].startswith("grid-admin-70444-")]
    assert len(matched_groups) == 1, f"应只有 1 个 grid 分组，实际 {len(matched_groups)}"
    assert len(matched_strats) == 1, f"应只有 1 个 grid 策略，实际 {len(matched_strats)}"


def test_quick_trigger_different_symbol_creates_separate_strategy_and_group(
    client, monkeypatch,
):
    """同一主节点、不同品种触发，应各自分到独立的 group/strategy，不复用也不覆盖。"""
    admin_token = _admin_token(client)
    _make_primary_node(client, name="multi-sym-node", mt5_login=70555)

    h = _admin_headers(client)
    listing = client.get("/api/nodes", headers=h).json()
    primary = next(n for n in listing if n["is_primary"] and n["mt5_login"] == 70555)

    base = {
        "node_id": primary["node_id"],
        "strategy_type": "grid",
        "volume": 0.01,
        "direction": "all",
    }
    a_body = {**base, "symbol": "XAUUSD"}
    b_body = {**base, "symbol": "EURUSD"}

    a = _post_quick_trigger(
        client, monkeypatch, admin_token, a_body, patch_grid_defaults=True,
    ).json()
    b = _post_quick_trigger(
        client, monkeypatch, admin_token, b_body, patch_grid_defaults=True,
    ).json()

    assert a["group_id"] != b["group_id"], "不同 symbol 应分到不同 group"
    assert a["strategy_id"] != b["strategy_id"], "不同 symbol 应分到不同 strategy"

    # 库内应有两条独立记录，名称分别带 -XAUUSD / -EURUSD
    groups = client.get("/api/groups", headers=h).json()
    strats = client.get("/api/strategies", headers=h).json()
    sym_groups = sorted(
        g["name"] for g in groups
        if g["name"].startswith("grid-admin-70555-")
    )
    sym_strats = sorted(
        s["name"] for s in strats
        if s["name"].startswith("grid-admin-70555-")
    )
    assert sym_groups == [
        "grid-admin-70555-EURUSD", "grid-admin-70555-XAUUSD",
    ], sym_groups
    assert sym_strats == [
        "grid-admin-70555-EURUSD", "grid-admin-70555-XAUUSD",
    ], sym_strats

    # 各自 strategy.symbol 也要与命名一致
    a_strat = next(s for s in strats if s["strategy_id"] == a["strategy_id"])
    b_strat = next(s for s in strats if s["strategy_id"] == b["strategy_id"])
    assert a_strat["symbol"] == "XAUUSD", a_strat
    assert b_strat["symbol"] == "EURUSD", b_strat