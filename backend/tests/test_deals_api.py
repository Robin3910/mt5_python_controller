"""成交明细与绩效统计测试（fakeredis + SQLite，无需外部服务）。

覆盖三块：
1) `deal_store`：幂等写入（重复上报不翻倍）、来源回填（magic/order_id/comment）；
2) `deal_stats`：**胜率按持仓级聚合**（不是按成交笔数）、净盈亏含手续费与库存费、
   期初权益取窗口内最早一笔、分日归集与服务器时区偏移；
3) `deals_api`：鉴权、归属校验（404）、响应结构。
"""
import time

import fakeredis
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.db import SessionLocal, init_db
from app.orm import Deal, SignalDispatch
from app.redis_store import RedisStore

from tests.test_helpers import (
    auth_headers,
    drop_test_db,
    reset_test_db,
)

_TEST_DB = __import__("pathlib").Path(__file__).resolve().parent / "_test_api.db"


@pytest.fixture
def client(monkeypatch):
    """全新 app + 全新 fakeredis（与 test_group_api.py 同一套引导方式）。"""
    def fake_from_url(cls, url=None):
        return RedisStore(fakeredis.FakeAsyncRedis(decode_responses=True))

    reset_test_db(_TEST_DB)
    monkeypatch.setattr(RedisStore, "from_url", classmethod(fake_from_url))
    from app.main import app

    with TestClient(app) as c:
        yield c
    drop_test_db(_TEST_DB)


@pytest.fixture
async def store():
    await init_db()
    async with SessionLocal() as s:
        for table in (Deal, SignalDispatch):
            await s.execute(delete(table))
        await s.commit()


def mk_deal(
    deal_id: int,
    *,
    node_id: str = "nd_a",
    entry_type: str = "OUT",
    position_id: int = 100,
    profit: float = 0.0,
    swap: float = 0.0,
    commission: float = 0.0,
    deal_time: int | None = None,
    magic: int | None = None,
    order_id: int | None = None,
    comment: str | None = None,
    equity: float = 0.0,
    server_time_offset: int = 0,
) -> dict:
    """构造一笔成交上报（节点侧的原始字段形态）。"""
    return {
        "deal_id": deal_id,
        "order_id": order_id,
        "position_id": position_id,
        "symbol": "XAUUSD",
        "direction": "SELL" if entry_type == "OUT" else "BUY",
        "entry_type": entry_type,
        "reason": 0,
        "volume": 0.1,
        "price": 2400.0,
        "profit": profit,
        "swap": swap,
        "commission": commission,
        "magic": magic,
        "comment": comment,
        "time": deal_time if deal_time is not None else int(time.time()),
        "equity": equity,
        "server_time_offset": server_time_offset,
    }


def mk_node(client, headers, mt5_login: int, name: str | None = None) -> str:
    r = client.post(
        "/api/nodes", json={"name": name or f"node-{mt5_login}", "mt5_login": mt5_login},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    return r.json()["node_id"]


# =====================================================================
# 1. deal_store：幂等写入与来源回填
# =====================================================================
async def test_save_deals_is_idempotent(store):
    """同一批成交重复上报只入库一次（否则统计会因重传翻倍）。"""
    from app import deal_store

    rows = [mk_deal(1, profit=10.0), mk_deal(2, profit=-5.0)]

    first = await deal_store.save_deals("nd_a", rows)
    second = await deal_store.save_deals("nd_a", rows)
    third = await deal_store.save_deals("nd_a", rows)

    assert first == 2
    assert second == 0
    assert third == 0
    async with SessionLocal() as s:
        assert (await s.execute(__import__("sqlalchemy").select(Deal))).scalars().all().__len__() == 2


async def test_save_deals_drops_missing_deal_id(store):
    """缺 deal_id 的成交无法幂等去重，必须丢弃而不是反复入库。"""
    from app import deal_store

    inserted = await deal_store.save_deals("nd_a", [{"deal_id": 0, "profit": 1.0}, {"profit": 2.0}])

    assert inserted == 0


async def test_save_deals_keeps_net_profit_with_fees(store):
    """net_profit = profit + swap + commission（统计口径统一用这个）。"""
    from app import deal_store

    await deal_store.save_deals("nd_a", [mk_deal(1, profit=100.0, swap=-2.0, commission=-1.0)])

    async with SessionLocal() as s:
        row = (await s.execute(__import__("sqlalchemy").select(Deal))).scalars().first()
    assert row.net_profit == pytest.approx(97.0)
    assert row.profit == pytest.approx(100.0)


async def test_backfill_source_by_magic(store):
    """magic 能对上子任务 -> 来源标记为 strategy，并带出 task_id/group_id。"""
    from app import deal_store
    from app.orm import GroupTaskDispatch

    async with SessionLocal() as s:
        s.add(
            GroupTaskDispatch(
                id=1, task_id=55, signal_id="sig_1", group_id="grp_1",
                node_id="nd_a", magic=987654,
            )
        )
        await s.commit()

    await deal_store.save_deals("nd_a", [mk_deal(1, magic=987654)])
    changed = await deal_store.backfill_sources("nd_a", [1])

    assert changed == 1
    async with SessionLocal() as s:
        row = (await s.execute(__import__("sqlalchemy").select(Deal))).scalars().first()
    assert row.source == "strategy"
    assert row.task_id == 55
    assert row.group_id == "grp_1"


async def test_backfill_source_terminal_when_no_match(store):
    """对不上任何关联的成交保留 terminal（终端手工单是合法结果，不是错误）。"""
    from app import deal_store

    await deal_store.save_deals("nd_a", [mk_deal(1, magic=111, order_id=222)])
    await deal_store.backfill_sources("nd_a", [1])

    async with SessionLocal() as s:
        row = (await s.execute(__import__("sqlalchemy").select(Deal))).scalars().first()
    assert row.source == "terminal"


# =====================================================================
# 2. deal_stats：统计口径
# =====================================================================
async def test_win_rate_is_position_level_not_deal_count(store):
    """一笔持仓分两次出场只算一笔 —— 胜率按 position_id 聚合。

    这笔持仓分批止盈：第一次 +50，第二次 +30。
    按成交笔数算是 2 笔 2 胜（胜率 100%）；
    按持仓级算是 1 笔 1 胜（胜率 100%），总交易笔数应为 1 而不是 2。
    """
    from app import deal_stats

    await deal_store_save(store, [
        mk_deal(1, position_id=100, profit=50.0),
        mk_deal(2, position_id=100, profit=30.0),
    ])

    summary = await deal_stats.node_summary("nd_a")

    assert summary["total_trades"] == 1
    assert summary["win_trades"] == 1
    assert summary["total_profit"] == pytest.approx(80.0)
    assert summary["win_rate"] == pytest.approx(100.0)


async def test_win_rate_counts_losing_positions(store):
    """一盈一亏两笔持仓：胜率 50%。"""
    from app import deal_stats

    await deal_store_save(store, [
        mk_deal(1, position_id=100, profit=100.0),
        mk_deal(2, position_id=200, profit=-40.0),
    ])

    summary = await deal_stats.node_summary("nd_a")

    assert summary["total_trades"] == 2
    assert summary["win_trades"] == 1
    assert summary["lose_trades"] == 1
    assert summary["win_rate"] == pytest.approx(50.0)
    assert summary["total_profit"] == pytest.approx(60.0)


async def test_open_deals_not_counted_as_profit(store):
    """开仓成交（IN）不计入已实现盈亏与胜率分母。"""
    from app import deal_stats

    await deal_store_save(store, [
        mk_deal(1, entry_type="IN", position_id=100, profit=0.0),
        mk_deal(2, entry_type="OUT", position_id=100, profit=25.0),
    ])

    summary = await deal_stats.node_summary("nd_a")

    assert summary["total_profit"] == pytest.approx(25.0)
    assert summary["total_trades"] == 1


async def test_profit_rate_uses_earliest_equity(store):
    """收益率的分母是窗口内**最早**一笔的 equity（不是 max、不是期末）。"""
    from app import deal_stats

    now = int(time.time())
    await deal_store_save(store, [
        mk_deal(1, position_id=100, profit=0.0, equity=1000.0, deal_time=now - 300),
        mk_deal(2, position_id=200, profit=100.0, equity=5000.0, deal_time=now - 200),
        mk_deal(3, position_id=300, profit=0.0, equity=9999.0, deal_time=now - 100),
    ])

    summary = await deal_stats.node_summary("nd_a")

    assert summary["base_equity"] == pytest.approx(1000.0)
    assert summary["profit_rate"] == pytest.approx(10.0)
    assert summary["profit_rate_valid"] is True


async def test_summary_without_data_marks_unavailable(store):
    """没有任何成交：data_available=False（区分「没数据」与「有数据但全亏」）。"""
    from app import deal_stats

    summary = await deal_stats.node_summary("nd_none")

    assert summary["data_available"] is False
    assert summary["total_trades"] == 0
    assert summary["total_profit"] == 0.0


async def test_daily_net_profit_buckets_by_day(store):
    """按日归集：两笔不同天的成交分到两根柱子，柱子数 = days。"""
    from app import deal_stats

    now = int(time.time())
    await deal_store_save(store, [
        mk_deal(1, position_id=100, profit=100.0, deal_time=now - 3600),
        mk_deal(2, position_id=200, profit=-30.0, deal_time=now - 86400 * 3),
    ])

    daily = await deal_stats.daily_net_profit(["nd_a"], days=7)

    assert len(daily) == 7
    assert sum(d["profit"] for d in daily) == pytest.approx(70.0)
    # 没有成交的日子也要占位（X 轴连续）
    assert all(d["deals"] == 0 for d in daily[:-4])


async def test_daily_net_profit_respects_server_offset(store):
    """服务器钟面偏移要被减掉，否则跨零点的成交会归到错误的一天。

    构造 GMT+3 经纪商：真实时间「前天 22:00 (UTC+8)」，
    服务器钟面却是「昨天 01:00 (UTC+8)」。
    不减掉偏移的话，这笔会被归到昨天 —— 差了整整一天。
    """
    from app import deal_stats

    _day_offset = 8 * 3600
    _day_seconds = 86400
    now = int(time.time())
    today_start = ((now + _day_offset) // _day_seconds) * _day_seconds - _day_offset
    real_utc = today_start - 2 * _day_seconds + 22 * 3600   # 前天 22:00 (UTC+8)
    server_clock = real_utc + 3 * 3600                       # 服务器钟面快 3 小时

    await deal_store_save(store, [
        mk_deal(
            1, position_id=100, profit=77.0,
            deal_time=server_clock, server_time_offset=3 * 3600,
        )
    ])

    daily = await deal_stats.daily_net_profit(["nd_a"], days=7)

    # 换算回真实 UTC 后应落在「前天」那根柱子上（index 4），而不是昨天（index 5）
    hit = [i for i, d in enumerate(daily) if d["profit"] != 0.0]
    assert hit == [4], f"应归到前天（index 4），实际 {hit}"
    assert daily[4]["profit"] == pytest.approx(77.0)
    assert daily[5]["profit"] == 0.0


async def test_daily_net_profit_empty_returns_empty_list(store):
    """没有节点 id 时返回空列表，不抛错。"""
    from app import deal_stats

    assert await deal_stats.daily_net_profit([], days=7) == []


async def test_summary_window_filter_respects_server_offset(store):
    """统计窗口的时间过滤要按**真实 UTC** 比较。

    `deal_time` 存的是服务器钟面（快 offset 秒），而 `from_ts` / `to_ts`
    来自 `time.time()`（真实 UTC）。直接拿 `deal_time` 比会整体错位 offset 秒。

    真正的坑在**上界**：一笔 5 分钟前成交的单，服务器钟面是 now+3h，
    拿它跟真实 `now` 比会得到「这笔成交发生在未来」-> 被 `to_ts` 切出窗口，
    概览的净盈亏因此比 MT5 少一块。
    """
    from app import deal_stats

    now = int(time.time())
    offset = 3 * 3600
    real_utc = now - 300
    server_clock = real_utc + offset

    await deal_store_save(store, [
        mk_deal(
            1, position_id=100, profit=50.0,
            deal_time=server_clock, server_time_offset=offset,
        )
    ])

    # 窗口 = 最近 1 小时（真实 UTC 域），含上界
    summary = await deal_stats.node_summary("nd_a", now - 3600, now)
    assert summary["total_profit"] == pytest.approx(50.0), "最近的成交被上界切掉了"
    assert summary["total_trades"] == 1


async def test_summary_window_filter_excludes_older_than_window(store):
    """窗口之外的成交仍然要被排除（偏移修正不能把过滤放松成全量）。"""
    from app import deal_stats

    now = int(time.time())
    offset = 3 * 3600
    real_utc = now - 86400 * 2
    server_clock = real_utc + offset

    await deal_store_save(store, [
        mk_deal(
            1, position_id=100, profit=50.0,
            deal_time=server_clock, server_time_offset=offset,
        )
    ])

    # 窗口 = 最近 1 小时：两天前的成交不在窗口内
    summary = await deal_stats.node_summary("nd_a", now - 3600, now)
    assert summary["total_profit"] == pytest.approx(0.0)
    assert summary["data_available"] is False


async def test_list_deals_window_filter_respects_server_offset(store):
    """成交明细分页的时间过滤同样按真实 UTC 比较。"""
    from app import deal_store

    now = int(time.time())
    offset = 3 * 3600
    server_clock = now - 300 + offset

    await deal_store_save(store, [
        mk_deal(
            1, position_id=100, profit=50.0,
            deal_time=server_clock, server_time_offset=offset,
        )
    ])

    page = await deal_store.list_deals(
        node_id="nd_a", from_ts=now - 3600, to_ts=now,
    )
    assert page["total"] == 1, "最近的成交被时间过滤切掉了"
    assert page["items"][0]["deal_id"] == 1


async def deal_store_save(store, rows):
    """便捷封装：写入成交（fixture 名保留 store 参数仅为统一签名）。"""
    from app import deal_store

    return await deal_store.save_deals("nd_a", rows)


# =====================================================================
# 3. deals_api：鉴权与响应结构
# =====================================================================
def test_performance_api_requires_auth(client):
    """未登录访问应被拒绝。"""
    r = client.get("/api/deals/nodes/nd_a/performance")
    assert r.status_code in (401, 403), r.text


def test_performance_api_returns_shape(client):
    """接口返回结构完整：summary + daily。"""
    headers = auth_headers(client)
    node_id = mk_node(client, headers, 60108501, "盈亏节点")

    r = client.get(f"/api/deals/nodes/{node_id}/performance?window=30d&days=7", headers=headers)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["node_id"] == node_id
    assert data["window"] == "30d"
    assert data["summary"]["data_available"] is False
    assert len(data["daily"]) == 7
    assert set(data["daily"][0]) == {"date", "ts", "profit", "deals"}


def test_performance_api_invalid_window_falls_back(client):
    """非法窗口按默认档处理，避免「传错参数 = 全量查询」的意外。"""
    headers = auth_headers(client)
    node_id = mk_node(client, headers, 60108502, "盈亏节点2")

    r = client.get(f"/api/deals/nodes/{node_id}/performance?window=xxx", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["window"] == "30d"


def test_performance_api_unknown_node_404(client):
    """不存在的节点返回 404。"""
    headers = auth_headers(client)
    r = client.get("/api/deals/nodes/nd_missing/performance", headers=headers)
    assert r.status_code == 404, r.text


def test_deals_api_unknown_node_404(client):
    """成交明细接口同样做归属校验。"""
    headers = auth_headers(client)
    r = client.get("/api/deals/nodes/nd_missing/deals", headers=headers)
    assert r.status_code == 404, r.text
