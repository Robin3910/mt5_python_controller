"""节点上报成交 -> 服务端入库 -> 概览统计 的端到端链路测试。

走真实路径：节点用 WebSocket 上报 `deals_report` -> ws_gateway 落库 ->
概览接口读出统计结果。验证三件事：
1. 上报即入库，且概览能立刻读到（不用等任何批处理）；
2. 重复上报同一批不会让统计翻倍（幂等）；
3. 手工单（terminal）也会被计入 —— 概览的盈亏是**账户真实盈亏**，
   不能只统计策略任务的成交。
"""
import json
import time

import fakeredis
import pytest
from fastapi.testclient import TestClient

from app.redis_store import RedisStore

from tests.test_helpers import auth_headers, drop_test_db, reset_test_db

_TEST_DB = __import__("pathlib").Path(__file__).resolve().parent / "_test_api.db"


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


def mk_node(client, headers, mt5_login: int) -> str:
    r = client.post(
        "/api/nodes", json={"name": f"n{mt5_login}", "mt5_login": mt5_login}, headers=headers,
    )
    assert r.status_code == 201, r.text
    return r.json()["node_id"]


def report_deals(client, node_id: str, token: str, mt5_login: int, deals: list[dict]) -> None:
    """以节点身份通过 WebSocket 上报一批成交。"""
    with client.websocket_connect("/ws/node") as ws:
        ws.send_json({"type": "auth", "data": {"token": token, "mt5_login": mt5_login}})
        assert ws.receive_json()["type"] == "auth_ok"
        ws.send_json({
            "type": "deals_report",
            "data": {
                "deals": deals,
                "account": {"balance": 10000.0, "equity": 10000.0, "server_time_offset": 0},
            },
        })
        # 服务端按序处理消息：等一个 pong 就说明 deals_report 已处理完
        ws.send_json({"type": "heartbeat", "data": {}})
        for _ in range(3):
            if ws.receive_json()["type"] == "pong":
                break


def mk_deal(deal_id: int, *, position_id: int, profit: float, entry_type: str = "OUT") -> dict:
    return {
        "deal_id": deal_id,
        "order_id": deal_id,
        "position_id": position_id,
        "symbol": "XAUUSD",
        "direction": "SELL" if entry_type == "OUT" else "BUY",
        "entry_type": entry_type,
        "reason": 0,
        "volume": 0.1,
        "price": 2400.0,
        "profit": profit,
        "swap": 0.0,
        "commission": 0.0,
        "magic": 0,
        "comment": "",
        "time": int(time.time()),
    }


def test_reported_deals_show_up_in_overview(client):
    """上报的成交立刻能在概览里读到统计。"""
    headers = auth_headers(client)
    login = 60109901
    node_id = mk_node(client, headers, login)

    token = client.get("/api/config/node-token", headers=headers).json()["token"]
    report_deals(client, node_id, token, login, [
        mk_deal(1, position_id=100, profit=100.0),
        mk_deal(2, position_id=200, profit=-40.0),
    ])

    r = client.get(f"/api/deals/nodes/{node_id}/performance", headers=headers)
    assert r.status_code == 200, r.text
    summary = r.json()["summary"]
    assert summary["data_available"] is True
    assert summary["total_trades"] == 2
    assert summary["win_trades"] == 1
    assert summary["lose_trades"] == 1
    assert summary["win_rate"] == pytest.approx(50.0)
    assert summary["total_profit"] == pytest.approx(60.0)


def test_duplicate_report_does_not_double_count(client):
    """同一批重复上报：统计不翻倍（服务端按成交号幂等）。"""
    headers = auth_headers(client)
    login = 60109902
    node_id = mk_node(client, headers, login)

    token = client.get("/api/config/node-token", headers=headers).json()["token"]
    deals = [mk_deal(1, position_id=100, profit=100.0)]
    report_deals(client, node_id, token, login, deals)
    report_deals(client, node_id, token, login, deals)

    r = client.get(f"/api/deals/nodes/{node_id}/performance", headers=headers)
    summary = r.json()["summary"]
    assert summary["total_profit"] == pytest.approx(100.0)
    assert summary["total_trades"] == 1


def test_manual_terminal_deal_counted(client):
    """终端手工单也计入概览 —— 概览是账户真实盈亏，不是「只看策略」。"""
    headers = auth_headers(client)
    login = 60109903
    node_id = mk_node(client, headers, login)

    token = client.get("/api/config/node-token", headers=headers).json()["token"]
    report_deals(client, node_id, token, login, [
        mk_deal(1, position_id=100, profit=25.0),
        mk_deal(2, position_id=101, profit=-5.0),
    ])

    # 明细接口能看到来源是 terminal（对不上任何信号/策略关联）
    r = client.get(f"/api/deals/nodes/{node_id}/deals", headers=headers)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) == 2
    assert {i["source"] for i in items} == {"terminal"}
    assert {i["source_label"] for i in items} == {"终端"}
    assert r.json()["summary"]["total_profit"] == pytest.approx(20.0)
