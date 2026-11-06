"""节点鉴权首包上报的券商服务器名：写库 + 缓存刷新。

服务器名原来只写 Redis 缓存，节点离线或缓存被清之后就没了 ——
面板卡片上的「服务器」位会变空。落库后它一直可查。
"""
import pathlib

import fakeredis
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.orm import Node
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


@pytest.fixture
def store():
    return RedisStore(fakeredis.FakeAsyncRedis(decode_responses=True))


@pytest.fixture(autouse=True)
async def _init():
    await init_db()


def _enrolled(client, headers, *, login=8801, server="ICMarkets-Live"):
    response = client.post(f"{PREFIX}/enrollments", headers=headers,
                           json={"mt5_login": login, "mt5_server": server})
    assert response.status_code == 201, response.text
    return response.json()


async def test_report_mt5_server_persists_to_db(client, store):
    """上报的服务器名要落库，不只是躺在 Redis 缓存里。"""
    from app import node_service

    admin = auth_headers(client)
    node = _enrolled(client, admin)

    await node_service.report_mt5_server(store, node["node_id"], "ICMarketsSC-Demo")

    async with SessionLocal() as s:
        row = await s.get(Node, node["node_id"])
        assert row is not None
        assert row.mt5_server == "ICMarketsSC-Demo"


async def test_report_mt5_server_ignores_unchanged_value(client, store):
    """值没变就不落库（节点每次重连都会带这个字段）。"""
    from app import node_service

    admin = auth_headers(client)
    node = _enrolled(client, admin, server="ICMarketsSC-Demo")

    result = await node_service.report_mt5_server(store, node["node_id"], "ICMarketsSC-Demo")

    assert result is None


async def test_report_mt5_server_ignores_empty_value(client, store):
    """空值不写（老版本节点不带这个字段）。"""
    from app import node_service

    admin = auth_headers(client)
    node = _enrolled(client, admin, server="ICMarkets-Live")

    assert await node_service.report_mt5_server(store, node["node_id"], "") is None
    assert await node_service.report_mt5_server(store, node["node_id"], "   ") is None

    async with SessionLocal() as s:
        row = await s.get(Node, node["node_id"])
        assert row.mt5_server == "ICMarkets-Live"


def test_stats_falls_back_to_persisted_server(client):
    """Redis 里没有服务器名时，统计接口要用库里的历史值兜底。"""
    admin = auth_headers(client)
    node = _enrolled(client, admin, server="ICMarkets-Live")

    items = client.get(f"{PREFIX}/stats", headers=admin).json()["items"]

    assert items[0]["mt5_server"] == "ICMarkets-Live"
