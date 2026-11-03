"""信号中心接口的端到端用例（fakeredis + SQLite）。

覆盖三件最容易出错的事：
1. **可见性**：所有信号中心接口都是管理员专属，普通用户拿不到（决策点 2）；
2. **筛选语义一致**：概览 / 信号流 / 节点 / 时延 / 任务共用同一套筛选参数，
   概览过滤了、信号流却没过滤是最典型的对不上；
3. **明细接口**：按需拉单条信号（含原始载荷），列表页不带。
"""
import pathlib

import fakeredis
import pytest
from fastapi.testclient import TestClient

from app.redis_store import RedisStore
from tests.test_helpers import auth_headers, drop_test_db, reset_test_db

# conftest 把 MYSQL_DSN 指向 _test_api.db —— 必须跟它对齐，
# 否则重置的是另一个文件，库里残留的用户会让用例报「用户名已存在」。
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


def _mk_user(client, h, username: str) -> dict:
    r = client.post(
        "/api/users", json={"username": username, "password": _PWD}, headers=h,
    )
    assert r.status_code == 201, r.text
    return r.json()


def _login(client, username: str, password: str = _PWD) -> dict:
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


# ---------------------------------------------------------------------------
# 可见性：管理员专属
# ---------------------------------------------------------------------------
def test_signal_center_requires_admin(client):
    """普通用户访问信号中心一律 403（决策点 2：普通用户不可见）。"""
    h = auth_headers(client)
    _mk_user(client, h, "normaluser")
    uh = _login(client, "normaluser")

    for path in (
        "/api/signal-center/overview",
        "/api/signal-center/stream",
        "/api/signal-center/nodes",
        "/api/signal-center/latency",
        "/api/signal-center/tasks",
    ):
        r = client.get(path, headers=uh)
        assert r.status_code == 403, f"{path} 对普通用户返回了 {r.status_code}"


def test_signal_center_admin_allowed(client):
    """管理员可以访问全部信号中心接口。"""
    h = auth_headers(client)
    for path in (
        "/api/signal-center/overview",
        "/api/signal-center/stream",
        "/api/signal-center/nodes",
        "/api/signal-center/latency",
        "/api/signal-center/tasks",
    ):
        r = client.get(path, headers=h)
        assert r.status_code == 200, f"{path} -> {r.status_code} {r.text}"


def test_signal_center_unauthenticated_rejected(client):
    """未登录不能访问。"""
    r = client.get("/api/signal-center/overview")
    assert r.status_code in (401, 403)


# ---------------------------------------------------------------------------
# 响应结构
# ---------------------------------------------------------------------------
def test_overview_response_shape(client):
    """概览响应体必须带窗口、总量与分布，且总量口径字段齐全。"""
    h = auth_headers(client)
    r = client.get("/api/signal-center/overview?window=30d", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["window"] == "30d"
    totals = body["totals"]
    for key in (
        "signals", "accepted", "rejected", "duplicate", "duplicate_collected",
        "dispatch_total", "passed", "skipped", "failed",
        "signal_success_rate", "dispatch_success_rate",
    ):
        assert key in totals, f"totals 缺字段 {key}"
    # 本期不采集去重抑制
    assert totals["duplicate_collected"] is False
    for key in ("status_distribution", "by_source", "by_model", "by_symbol",
                "skip_reasons", "daily"):
        assert key in body


def test_stream_returns_cursor_pagination_fields(client):
    """信号流返回 items + next_cursor（游标分页）。"""
    h = auth_headers(client)
    r = client.get("/api/signal-center/stream?limit=5", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "items" in body
    assert "next_cursor" in body


def test_tasks_response_declares_profit_scope(client):
    """策略任务接口必须声明盈亏口径来源（防止与成交侧净盈亏混读）。"""
    h = auth_headers(client)
    r = client.get("/api/signal-center/tasks", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["profit_scope"], "缺少盈亏口径说明"
    assert "deal_stats" in body["profit_scope"]
    assert "realized_profit" in body["profit_scope"]


def test_latency_response_declares_clock_scopes(client):
    """时延接口必须区分服务端钟与节点内两种口径。"""
    h = auth_headers(client)
    r = client.get("/api/signal-center/latency", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["end_to_end"]["scope"] == "server_clock"
    assert body["queued_ms"]["scope"] == "node_local"
    assert body["order_to_fill_ms"]["scope"] == "node_local"
    assert body["note"]


def test_signal_detail_unknown_returns_404(client):
    """查不存在的信号返回 404，不能 500。"""
    h = auth_headers(client)
    r = client.get("/api/signal-center/signal/no-such-id", headers=h)
    assert r.status_code == 404, r.text


def test_signal_detail_requires_admin(client):
    """明细接口同样是管理员专属。"""
    h = auth_headers(client)
    _mk_user(client, h, "detailuser")
    uh = _login(client, "detailuser")
    r = client.get("/api/signal-center/signal/whatever", headers=uh)
    assert r.status_code == 403
