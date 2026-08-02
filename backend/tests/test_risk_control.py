"""账户级风控：配置规范化 / 校验 / 节点 PATCH 持久化。"""
import pathlib

import fakeredis
import pytest
from fastapi.testclient import TestClient

from app.redis_store import RedisStore
from app import risk_control
from tests.test_helpers import drop_test_db, reset_test_db

_TEST_DB = pathlib.Path(__file__).resolve().parent / "_test_api.db"


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


def _auth(client) -> dict:
    r = client.post("/api/login", json={"username": "admin", "password": "admin123"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_normalize_defaults():
    cfg = risk_control.normalize_risk(None)
    assert cfg["float_pl_ratio"]["enabled"] is False
    assert cfg["equity_min"]["amount"] == 1000.0
    assert cfg["symbol_pl_orders"]["items"] == []
    assert cfg["symbol_pl_protect"]["items"] == []
    assert cfg["lot_pl_tiers"]["batch_count"] == 2
    assert len(cfg["lot_pl_tiers"]["tiers"]) == 2


def test_normalize_list_and_tiers():
    cfg = risk_control.normalize_risk({
        "symbol_pl_orders": {
            "items": [{
                "enabled": True,
                "symbol": "xauusd",
                "pl_amount": 80,
                "order_op": "gte",
                "order_count": 2,
                "close_action": "hedge",
                "monitor_mode": "times",
                "max_times": 3,
                "remaining_times": 0,
            }]
        },
        "lot_pl_tiers": {
            "enabled": True,
            "batch_count": 3,
            "close_action": "buy",
            "tiers": [{"min_lot": 0.2, "pl_amount": 30}],
        },
    })
    item = cfg["symbol_pl_orders"]["items"][0]
    assert item["symbol"] == "XAUUSD"
    assert item["remaining_times"] == 3
    assert item["close_action"] == "hedge"
    assert cfg["lot_pl_tiers"]["batch_count"] == 3
    assert len(cfg["lot_pl_tiers"]["tiers"]) == 3
    assert cfg["lot_pl_tiers"]["tiers"][0]["min_lot"] == 0.2


def test_validate_protect_signs():
    cfg = risk_control.normalize_risk({
        "symbol_pl_protect": {
            "items": [{
                "enabled": True,
                "symbol": "XAUUSD",
                "trigger_amount": -50,
                "narrow_amount": -100,
            }]
        }
    })
    assert "亏损保护" in (risk_control.validate_risk(cfg) or "")


def test_merge_runtime_state_keeps_admin_config():
    """节点回报的是旧快照：只取开关与剩余次数，其余以库为准。"""
    current = risk_control.normalize_risk({
        "equity_min": {
            "enabled": True, "amount": 2000,
            "monitor_mode": "times", "max_times": 3, "remaining_times": 3,
        },
        "symbol_pl_orders": {
            "items": [{
                "id": "a1", "enabled": True, "symbol": "EURUSD",
                "pl_amount": 300, "order_op": "any", "order_count": 0,
                "close_action": "all", "monitor_mode": "times",
                "max_times": 2, "remaining_times": 2,
            }]
        },
    })
    # 节点手里还是改动前的旧值，并且刚消耗掉一次
    reported = risk_control.normalize_risk({
        "equity_min": {
            "enabled": True, "amount": 800,
            "monitor_mode": "times", "max_times": 3, "remaining_times": 2,
        },
        "symbol_pl_orders": {
            "items": [{
                "id": "a1", "enabled": False, "symbol": "XAUUSD",
                "pl_amount": 100, "order_op": "gte", "order_count": 5,
                "close_action": "buy", "monitor_mode": "times",
                "max_times": 2, "remaining_times": 0,
            }]
        },
    })

    merged = risk_control.merge_runtime_state(current, reported)

    # 金额 / 品种 / 动作等配置项保持管理员刚保存的值
    assert merged["equity_min"]["amount"] == 2000
    assert merged["symbol_pl_orders"]["items"][0]["symbol"] == "EURUSD"
    assert merged["symbol_pl_orders"]["items"][0]["close_action"] == "all"
    # 运行态跟随节点回报
    assert merged["equity_min"]["remaining_times"] == 2
    assert merged["symbol_pl_orders"]["items"][0]["enabled"] is False
    assert merged["symbol_pl_orders"]["items"][0]["remaining_times"] == 0


def test_merge_runtime_state_ignores_unknown_items():
    """回报里没有的条目（管理员刚新增）保持库里的状态。"""
    current = risk_control.normalize_risk({
        "symbol_pl_protect": {
            "items": [{
                "id": "new", "enabled": True, "symbol": "XAUUSD",
                "trigger_amount": -100, "narrow_amount": -40,
                "monitor_mode": "times", "max_times": 2, "remaining_times": 2,
            }]
        },
    })
    merged = risk_control.merge_runtime_state(current, {"symbol_pl_protect": {"items": []}})
    assert merged["symbol_pl_protect"]["items"][0]["enabled"] is True
    assert merged["symbol_pl_protect"]["items"][0]["remaining_times"] == 2


def test_validate_rejects_zero_ratio():
    cfg = risk_control.normalize_risk({"float_pl_ratio": {"ratio": 0}})
    assert risk_control.validate_risk(cfg) == "账户盈亏比比例不能为 0"


def test_patch_node_risk_new_rules(client):
    h = _auth(client)
    r = client.post("/api/nodes", json={"name": "risk-n3", "mt5_login": 88011}, headers=h)
    assert r.status_code == 201, r.text
    node_id = r.json()["node_id"]

    payload = {
        "risk": {
            "float_pl_ratio": {"enabled": False, "ratio": -20},
            "equity_min": {"enabled": False, "amount": 1000},
            "symbol_pl_orders": {
                "items": [{
                    "enabled": True,
                    "symbol": "XAUUSD",
                    "pl_amount": 100,
                    "order_op": "any",
                    "order_count": 0,
                    "close_action": "all",
                    "monitor_mode": "loop",
                    "max_times": 1,
                }]
            },
            "symbol_pl_protect": {
                "items": [{
                    "enabled": True,
                    "symbol": "XAUUSD",
                    "trigger_amount": -100,
                    "narrow_amount": -40,
                    "monitor_mode": "times",
                    "max_times": 2,
                }]
            },
            "lot_pl_tiers": {
                "enabled": True,
                "batch_count": 2,
                "close_action": "all",
                "tiers": [
                    {"min_lot": 0.1, "pl_amount": 50},
                    {"min_lot": 1, "pl_amount": 200},
                ],
            },
        }
    }
    r = client.patch(f"/api/nodes/{node_id}", json=payload, headers=h)
    assert r.status_code == 200, r.text
    risk = r.json()["risk"]
    assert len(risk["symbol_pl_orders"]["items"]) == 1
    assert risk["symbol_pl_protect"]["items"][0]["remaining_times"] == 2
    assert risk["lot_pl_tiers"]["tiers"][1]["min_lot"] == 1
