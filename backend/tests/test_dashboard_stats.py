"""控制端面板汇总统计（今日信号 / 平均延迟 / 服务器名）回归。

覆盖三块：
1) `dashboard_stats`：分日口径（UTC+8 自然日）、两条链路（normal + strategy）相加、
   延迟只统计两端齐全的行（不把「在途」当成 0 耗时）；
2) `/api/node-dashboard/stats`：只返回**调用方可见**的节点，字段齐全；
3) 权限隔离：普通用户看不到别人名下的节点统计。

延迟口径（改过一轮，容易再错，这里钉死）：
- normal 链路的往返时延 = `sent_at` → `finished_at`（节点回报结果）；
- strategy 链路的往返时延 = `sent_at` → `opened_at`（节点首次回报开仓），
  **不是** `finished_at` —— 那是策略持仓收口，算出来是持仓时长不是时延；
- 两条链路的样本按数量加权合并，不对两个均值取算术平均；
- 「离线 / 超时」这类没走完往返的伪失败不计入。
"""
import pathlib
from datetime import datetime, timedelta

import fakeredis
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.db import SessionLocal, init_db
from app.orm import GroupTaskDispatch, SignalDispatch
from app.redis_store import RedisStore

from tests.test_helpers import auth_headers, drop_test_db, reset_test_db

_TEST_DB = pathlib.Path(__file__).resolve().parent / "_test_api.db"
PREFIX = "/api/node-dashboard"


@pytest.fixture
def client(monkeypatch):
    reset_test_db(_TEST_DB)
    monkeypatch.setattr(RedisStore, "from_url", classmethod(
        lambda cls, url=None: RedisStore(fakeredis.FakeAsyncRedis(decode_responses=True))))
    from app.main import app
    with TestClient(app) as c:
        yield c
    drop_test_db(_TEST_DB)


@pytest.fixture(autouse=True)
async def _clean_dispatch_tables():
    """清空分发明细表 —— 上一条用例留下的行会污染下一条的计数。"""
    await init_db()
    async with SessionLocal() as s:
        await s.execute(delete(SignalDispatch))
        await s.execute(delete(GroupTaskDispatch))
        await s.commit()


async def _seed_dispatch(node_id: str, *, sent_at: datetime, latency_ms: float | None,
                          status: str = "done", error: str | None = None):
    """写一条 normal 链路的分发明细（信号链路）。"""
    async with SessionLocal() as s:
        s.add(SignalDispatch(
            signal_id="sig_stats", node_id=node_id,
            sent_at=sent_at,
            finished_at=(sent_at + timedelta(milliseconds=latency_ms)) if latency_ms is not None else None,
            status=status, error=error,
        ))
        await s.commit()


async def _seed_dispatch_second_precision(node_id: str, *, sent_at: datetime, latency_ms: int):
    """写一条**只有整秒精度**的分发明细（模拟精度迁移之前的历史行）。

    MySQL 的 DATETIME(0) 会把微秒整秒截断，这里显式把微秒清零来复现。
    """
    base = sent_at.replace(microsecond=0)
    async with SessionLocal() as s:
        s.add(SignalDispatch(
            signal_id="sig_stats", node_id=node_id,
            sent_at=base,
            finished_at=base + timedelta(milliseconds=latency_ms),
            status="done",
        ))
        await s.commit()


async def _seed_group_task(node_id: str, *, sent_at: datetime,
                           opened_ms: float | None = None, hold_ms: float | None = None):
    """写一条 strategy 链路的分发明细（策略链路）。

    opened_ms：节点首次回报开仓的时刻相对发包的偏移 —— 这才是往返时延；
    hold_ms：策略持仓到收口的总时长 —— 用来验证它**不**参与时延计算。
    """
    async with SessionLocal() as s:
        s.add(GroupTaskDispatch(
            task_id=1, signal_id="sig_stats", group_id="grp_1", node_id=node_id,
            sent_at=sent_at,
            opened_at=(sent_at + timedelta(milliseconds=opened_ms)) if opened_ms is not None else None,
            finished_at=(sent_at + timedelta(milliseconds=hold_ms)) if hold_ms is not None else None,
        ))
        await s.commit()


def _enrolled(client, headers, *, login=8801, server="ICMarkets-Live"):
    response = client.post(f"{PREFIX}/enrollments", headers=headers,
                           json={"mt5_login": login, "mt5_server": server})
    assert response.status_code == 201, response.text
    return response.json()


async def test_today_signals_count_both_links():
    """今日信号 = normal 链路 + strategy 链路，同一节点两条都算。"""
    from app import dashboard_stats

    now = datetime.now()
    await _seed_dispatch("nd_a", sent_at=now, latency_ms=100)
    await _seed_dispatch("nd_a", sent_at=now, latency_ms=300)
    await _seed_group_task("nd_a", sent_at=now, opened_ms=200)

    result = await dashboard_stats.node_daily_stats(["nd_a"])

    assert result["nd_a"]["today_signals"] == 3
    # 三个样本（100 / 300 / 200）的加权平均 = 200
    assert result["nd_a"]["avg_latency_ms"] == pytest.approx(200.0)


async def test_latency_is_sample_weighted_across_links():
    """两条链路的样本按数量加权合并，不对两个均值取算术平均。

    normal 1 个样本 1000ms，strategy 3 个样本 100ms：
    加权平均 = (1000 + 3*100) / 4 = 325ms；
    若按「两个均值的算术平均」会算成 (1000 + 100) / 2 = 550ms —— 差很多。
    """
    from app import dashboard_stats

    now = datetime.now()
    await _seed_dispatch("nd_a", sent_at=now, latency_ms=1000)
    for _ in range(3):
        await _seed_group_task("nd_a", sent_at=now, opened_ms=100)

    result = await dashboard_stats.node_daily_stats(["nd_a"])

    assert result["nd_a"]["avg_latency_ms"] == pytest.approx(325.0)


async def test_strategy_latency_ignores_holding_time():
    """策略链路的时延取「首次回报开仓」，不是「持仓到收口」。

    发包后 50ms 节点回报开仓，2000ms 后策略平仓收口：
    时延应该是 50ms，而不是 2000ms。
    """
    from app import dashboard_stats

    now = datetime.now()
    await _seed_group_task("nd_a", sent_at=now, opened_ms=50, hold_ms=2000)

    result = await dashboard_stats.node_daily_stats(["nd_a"])

    assert result["nd_a"]["avg_latency_ms"] == pytest.approx(50.0)


async def test_strategy_row_without_open_is_not_a_latency_sample():
    """策略子任务没开成仓（没有 opened_at）就没有往返样本，不计入平均。"""
    from app import dashboard_stats

    now = datetime.now()
    await _seed_group_task("nd_a", sent_at=now, opened_ms=None, hold_ms=2000)
    await _seed_dispatch("nd_a", sent_at=now, latency_ms=400)

    result = await dashboard_stats.node_daily_stats(["nd_a"])

    assert result["nd_a"]["today_signals"] == 2
    assert result["nd_a"]["avg_latency_ms"] == pytest.approx(400.0)


async def test_pseudo_failures_excluded_from_latency():
    """离线 / 超时这类没走完往返的伪失败不计入时延平均。

    离线的耗时接近 0ms，计进去会把均值拉到接近 0，看着像「零延迟」。
    """
    from app import dashboard_stats

    now = datetime.now()
    await _seed_dispatch("nd_a", sent_at=now, latency_ms=600)
    await _seed_dispatch("nd_a", sent_at=now, latency_ms=1, status="failed", error="offline")
    await _seed_dispatch("nd_a", sent_at=now, latency_ms=15000, status="failed", error="timeout")

    result = await dashboard_stats.node_daily_stats(["nd_a"])

    assert result["nd_a"]["avg_latency_ms"] == pytest.approx(600.0)


async def test_node_reported_failure_still_counts():
    """节点**回报了**失败（比如券商拒单）仍是一次真实往返，计入平均。"""
    from app import dashboard_stats

    now = datetime.now()
    await _seed_dispatch("nd_a", sent_at=now, latency_ms=600)
    await _seed_dispatch("nd_a", sent_at=now, latency_ms=200,
                         status="failed", error="invalid volume")

    result = await dashboard_stats.node_daily_stats(["nd_a"])

    assert result["nd_a"]["avg_latency_ms"] == pytest.approx(400.0)


async def test_second_precision_history_excluded_from_latency():
    """精度迁移之前的历史行（只有整秒信息）不计入平均延迟。

    那些行的时延误差可达 ±1 秒 —— 混进均值会把 100ms 的真实往返算成 1000ms。
    指标宁可显示「没有样本」（None），也不能给一个错的数字。
    """
    from app import dashboard_stats

    now = datetime.now()
    await _seed_dispatch_second_precision("nd_a", sent_at=now, latency_ms=1000)
    await _seed_dispatch("nd_a", sent_at=now, latency_ms=200)

    result = await dashboard_stats.node_daily_stats(["nd_a"])

    # 两条都算今日信号，但只有毫秒精度的那条进时延平均
    assert result["nd_a"]["today_signals"] == 2
    assert result["nd_a"]["avg_latency_ms"] == pytest.approx(200.0)


async def test_all_second_precision_gives_no_latency():
    """只有精度不足的历史样本时，时延为 None（不是 0）。"""
    from app import dashboard_stats

    now = datetime.now()
    await _seed_dispatch_second_precision("nd_a", sent_at=now, latency_ms=1000)

    result = await dashboard_stats.node_daily_stats(["nd_a"])

    assert result["nd_a"]["avg_latency_ms"] is None


async def test_yesterday_signals_excluded():
    """昨天（UTC+8）的信号不算今日信号。"""
    from app import dashboard_stats

    old = datetime.now() - timedelta(days=1)
    await _seed_dispatch("nd_a", sent_at=old, latency_ms=100)

    result = await dashboard_stats.node_daily_stats(["nd_a"])

    assert result["nd_a"]["today_signals"] == 0
    assert result["nd_a"]["avg_latency_ms"] is None


async def test_latency_ignores_unfinished_rows():
    """缺 finished_at 的在途行不进平均延迟（否则会被当成 0 耗时拉低均值）。"""
    from app import dashboard_stats

    now = datetime.now()
    await _seed_dispatch("nd_a", sent_at=now, latency_ms=500)
    await _seed_dispatch("nd_a", sent_at=now, latency_ms=None)

    result = await dashboard_stats.node_daily_stats(["nd_a"])

    assert result["nd_a"]["today_signals"] == 2
    assert result["nd_a"]["avg_latency_ms"] == pytest.approx(500.0)


async def test_stats_empty_for_unknown_nodes():
    """查不到数据的节点也会有一项，但计数为 0、延迟为 None（不是 0 延迟）。"""
    from app import dashboard_stats

    assert await dashboard_stats.node_daily_stats([]) == {}
    result = await dashboard_stats.node_daily_stats(["nope"])
    assert result["nope"] == {"today_signals": 0, "avg_latency_ms": None}


def test_stats_endpoint_returns_visible_nodes(client):
    """接口返回可见节点的服务器名与统计字段。"""
    admin = auth_headers(client)
    node = _enrolled(client, admin)

    response = client.get(f"{PREFIX}/stats", headers=admin)

    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len(items) == 1
    item = items[0]
    assert item["node_id"] == node["node_id"]
    assert item["mt5_server"] == "ICMarkets-Live"
    assert item["mt5_login"] == 8801
    assert item["today_signals"] == 0
    assert item["avg_latency_ms"] is None


def test_stats_endpoint_scopes_to_own_nodes(client):
    """普通用户只拿到自己名下节点的统计。"""
    admin = auth_headers(client)
    alice = client.post("/api/users", headers=admin,
                        json={"username": "alice", "password": "pass1234"}).json()
    alice_headers = {"Authorization": "Bearer " + client.post(
        "/api/login", json={"username": "alice", "password": "pass1234"}).json()["token"]}

    alice_node = _enrolled(client, alice_headers, login=8801)
    admin_node = _enrolled(client, admin, login=8802, server="Other-Live")
    client.put(f"/api/users/{alice['id']}/nodes", headers=admin,
               json={"node_ids": [alice_node["node_id"]]})

    items = client.get(f"{PREFIX}/stats", headers=alice_headers).json()["items"]

    assert [i["node_id"] for i in items] == [alice_node["node_id"]]
    assert admin_node["node_id"] not in {i["node_id"] for i in items}


def test_stats_endpoint_requires_auth(client):
    """未鉴权访问统计接口必须被拒。"""
    assert client.get(f"{PREFIX}/stats").status_code in (401, 403)
