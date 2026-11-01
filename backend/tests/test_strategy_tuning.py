"""策略运行期调参：config_version、上限下限校验、runtime_hints。"""
import pathlib

import fakeredis
import pytest
from fastapi.testclient import TestClient

from app.redis_store import RedisStore
from app.strategy_templates import TEMPLATE_1_ID

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


from tests.test_helpers import auth_headers  # noqa: E402


def _rule(rule_type: int = 1, **over) -> dict:
    rule = {
        "type": rule_type, "status": 1, "action": "all",
        "point": 100, "lot_times": 1.0, "extra_lot": 0.0,
        "max_allow_num": 5, "batch_enabled": False, "batch_count": 0,
        "total_lot_limit": 5,
    }
    rule.update(over)
    return rule


# ----------------------------------------------------------------------
# config_version
# ----------------------------------------------------------------------
def test_config_version_increments_on_save(client):
    h = auth_headers(client)
    created = client.post(
        "/api/strategies",
        json={
            "template_id": TEMPLATE_1_ID, "name": "版本号策略", "symbol": "XAUUSD",
            "rules": [_rule()],
        },
        headers=h,
    ).json()
    assert created["config_version"] == 1

    r = client.patch(
        f"/api/strategies/{created['strategy_id']}",
        json={"rules": [_rule(total_lot_limit=6)]},
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert r.json()["config_version"] == 2

    r = client.patch(
        f"/api/strategies/{created['strategy_id']}",
        json={"name": "改个名"},
        headers=h,
    )
    assert r.status_code == 200
    assert r.json()["config_version"] == 3


def test_snapshot_carries_config_version(client):
    """快照里带 config_version，节点侧用它做版本门。"""
    from app import group_rules

    snap = group_rules.strategy_rules_snapshot({
        "strategy_id": "s1", "name": "n", "symbol": "XAUUSD",
        "template_id": TEMPLATE_1_ID, "config_version": 7,
        "rules": [_rule()],
    })
    assert snap["config_version"] == 7


def test_snapshot_config_version_defaults_to_zero():
    from app import group_rules

    snap = group_rules.strategy_rules_snapshot({
        "strategy_id": "s1", "name": "n", "symbol": "XAUUSD",
        "template_id": TEMPLATE_1_ID, "rules": [_rule()],
    })
    assert snap["config_version"] == 0


# ----------------------------------------------------------------------
# runtime_hints
# ----------------------------------------------------------------------
def test_get_strategy_returns_runtime_hints(client):
    h = auth_headers(client)
    created = client.post(
        "/api/strategies",
        json={
            "template_id": TEMPLATE_1_ID, "name": "提示策略", "symbol": "XAUUSD",
            "rules": [_rule()],
        },
        headers=h,
    ).json()
    r = client.get(f"/api/strategies/{created['strategy_id']}", headers=h)
    assert r.status_code == 200, r.text
    hints = r.json()["runtime_hints"]
    assert hints is not None
    assert hints["running_task_count"] == 0
    assert hints["max_added_by_rule"] == {}
    assert hints["manual_fired"] == []


# ----------------------------------------------------------------------
# 上限下限校验（后端兜底）
# ----------------------------------------------------------------------
def test_limit_below_added_is_rejected(client, monkeypatch):
    """已开 3 笔时把上限改成 2 → 400 拒绝。"""
    h = auth_headers(client)
    created = client.post(
        "/api/strategies",
        json={
            "template_id": TEMPLATE_1_ID, "name": "缩容策略", "symbol": "XAUUSD",
            "rules": [_rule(total_lot_limit=5)],
        },
        headers=h,
    ).json()

    from app import group_persist

    async def fake_hints(strategy_id: str) -> dict:
        return {
            "running_task_count": 1,
            "max_added_by_rule": {"1": 3},
            "manual_fired": [],
            "nodes_offline": 0,
        }

    monkeypatch.setattr(group_persist, "strategy_runtime_hints", fake_hints)

    r = client.patch(
        f"/api/strategies/{created['strategy_id']}",
        json={"rules": [_rule(total_lot_limit=2)]},
        headers=h,
    )
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert detail["error"] == "limit_below_added"
    assert detail["blocks"][0]["rule_type"] == 1
    assert detail["blocks"][0]["added"] == 3
    assert detail["blocks"][0]["limit"] == 2
    assert "低于已开笔数" in detail["message"]


def test_limit_equal_to_added_is_allowed(client, monkeypatch):
    """缩容到恰好等于已开笔数 → 允许。"""
    h = auth_headers(client)
    created = client.post(
        "/api/strategies",
        json={
            "template_id": TEMPLATE_1_ID, "name": "刚好缩容", "symbol": "XAUUSD",
            "rules": [_rule(total_lot_limit=5)],
        },
        headers=h,
    ).json()

    from app import group_persist

    async def fake_hints(strategy_id: str) -> dict:
        return {
            "running_task_count": 1,
            "max_added_by_rule": {"1": 3},
            "manual_fired": [],
            "nodes_offline": 0,
        }

    monkeypatch.setattr(group_persist, "strategy_runtime_hints", fake_hints)

    r = client.patch(
        f"/api/strategies/{created['strategy_id']}",
        json={"rules": [_rule(total_lot_limit=3)]},
        headers=h,
    )
    assert r.status_code == 200, r.text


def test_limit_expansion_is_allowed(client, monkeypatch):
    h = auth_headers(client)
    created = client.post(
        "/api/strategies",
        json={
            "template_id": TEMPLATE_1_ID, "name": "扩容", "symbol": "XAUUSD",
            "rules": [_rule(total_lot_limit=3)],
        },
        headers=h,
    ).json()

    from app import group_persist

    async def fake_hints(strategy_id: str) -> dict:
        return {
            "running_task_count": 1,
            "max_added_by_rule": {"1": 3},
            "manual_fired": [],
            "nodes_offline": 0,
        }

    monkeypatch.setattr(group_persist, "strategy_runtime_hints", fake_hints)

    r = client.patch(
        f"/api/strategies/{created['strategy_id']}",
        json={"rules": [_rule(total_lot_limit=10)]},
        headers=h,
    )
    assert r.status_code == 200, r.text


def test_limit_check_is_per_rule(client, monkeypatch):
    """按规则独立校验：逆势超限被拒，顺势可以改。"""
    h = auth_headers(client)
    created = client.post(
        "/api/strategies",
        json={
            "template_id": TEMPLATE_1_ID, "name": "分规则", "symbol": "XAUUSD",
            "rules": [_rule(1, total_lot_limit=5), _rule(2, total_lot_limit=5)],
        },
        headers=h,
    ).json()

    from app import group_persist

    async def fake_hints(strategy_id: str) -> dict:
        return {
            "running_task_count": 1,
            "max_added_by_rule": {"1": 3, "2": 0},
            "manual_fired": [],
            "nodes_offline": 0,
        }

    monkeypatch.setattr(group_persist, "strategy_runtime_hints", fake_hints)

    # 逆势改成 2（低于已开 3）→ 拒
    r = client.patch(
        f"/api/strategies/{created['strategy_id']}",
        json={"rules": [_rule(1, total_lot_limit=2), _rule(2, total_lot_limit=5)]},
        headers=h,
    )
    assert r.status_code == 400
    assert r.json()["detail"]["blocks"][0]["rule_type"] == 1

    # 只改顺势 → 放行
    r = client.patch(
        f"/api/strategies/{created['strategy_id']}",
        json={"rules": [_rule(1, total_lot_limit=5), _rule(2, total_lot_limit=1)]},
        headers=h,
    )
    assert r.status_code == 200, r.text


def test_limit_zero_is_unlimited_not_rejected(client, monkeypatch):
    """0 = 不限制，不参与下限比较。"""
    h = auth_headers(client)
    created = client.post(
        "/api/strategies",
        json={
            "template_id": TEMPLATE_1_ID, "name": "不限", "symbol": "XAUUSD",
            "rules": [_rule(total_lot_limit=5)],
        },
        headers=h,
    ).json()

    from app import group_persist

    async def fake_hints(strategy_id: str) -> dict:
        return {
            "running_task_count": 1,
            "max_added_by_rule": {"1": 3},
            "manual_fired": [],
            "nodes_offline": 0,
        }

    monkeypatch.setattr(group_persist, "strategy_runtime_hints", fake_hints)

    r = client.patch(
        f"/api/strategies/{created['strategy_id']}",
        json={"rules": [_rule(total_lot_limit=0)]},
        headers=h,
    )
    assert r.status_code == 200, r.text


def test_no_running_task_means_no_limit_check(client):
    """没有在跑任务时，上限随便改。"""
    h = auth_headers(client)
    created = client.post(
        "/api/strategies",
        json={
            "template_id": TEMPLATE_1_ID, "name": "空闲", "symbol": "XAUUSD",
            "rules": [_rule(total_lot_limit=5)],
        },
        headers=h,
    ).json()
    r = client.patch(
        f"/api/strategies/{created['strategy_id']}",
        json={"rules": [_rule(total_lot_limit=1)]},
        headers=h,
    )
    assert r.status_code == 200, r.text
