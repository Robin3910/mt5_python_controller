"""中控台「AI 开仓准入」（tpl_1）端到端用例 —— 走真实 HTTP `/webhook`。

这一组刻意**不 mock 分发链路**：从 `/webhook` 进，到 `signal_history` 出，
验证 L1 闸门在真实链路上的行为（拒收编码、入库状态、响应体、任务记录）。

覆盖：
- 方向不符 / 价格超区间 / 缺价 → 被拦下（拒收 + 无主任务）；
- 方向与价格都符合 → 正常下发；
- 未启用准入 → 存量行为零变化；
- auto 路由同样被拦，但规则命中统计照常；
- `tpl_2` 分组不受影响。
"""
import pathlib

import fakeredis
import pytest
from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.orm import GroupSignalTask, SignalHistory
from app.redis_store import RedisStore
from tests.test_helpers import auth_headers, drop_test_db, reset_test_db

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


def _mk_strategy(client, headers, name: str, symbol: str = "XAUUSD",
                 template_id: str = "tpl_1") -> dict:
    r = client.post(
        "/api/strategies",
        json={"template_id": template_id, "name": name, "symbol": symbol},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    return r.json()


def _mk_group(client, headers, *, name: str, symbol: str = "XAUUSD",
              template_id: str = "tpl_1", **body) -> dict:
    payload = {"name": name, "enabled": True, "dispatch_mode": "sync", "node_ids": []}
    payload.update(body)
    if "strategy_id" not in payload:
        sty = _mk_strategy(client, headers, name=f"策略-{name}", symbol=symbol,
                           template_id=template_id)
        payload["strategy_id"] = sty["strategy_id"]
    r = client.post("/api/groups", json=payload, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def _set_filters(client, headers, *, ai_entry=None) -> None:
    """写中控台 filters；ai_entry 为 None 时不写该子块（存量形态）。"""
    payload = {
        "XAUUSD": {
            "enabled": True,
            "allow_buy": True,
            "allow_sell": True,
            "dispatch_mode": "sync",
            "position_scope": "symbol",
            "default_action": "pass",
            "lot_enabled": False,
            "lot": 0.01,
            "intervals": [],
        }
    }
    if ai_entry is not None:
        payload["XAUUSD"]["ai_entry"] = ai_entry
    r = client.put("/api/config/filters", json=payload, headers=headers)
    assert r.status_code == 200, r.text


def _ai_entry(**over) -> dict:
    base = {
        "enabled": True,
        "allow_buy": True,
        "allow_sell": False,
        "default_action": "block",
        "intervals": [{"low": 4000, "high": 4200, "allow": ["BUY"]}],
    }
    base.update(over)
    return base


def _tv_signal(symbol: str = "XAUUSD", **extra) -> dict:
    payload = {
        "action": "buy", "symbol": symbol, "volume": 0.1,
        "model": "strategy", "template_ids": ["tpl_1"],
        "tf": "M15", "signal_type": "ob",
    }
    payload.update(extra)
    return payload


def _fetch_signal(signal_id: str):
    """同步读回 signal_history 行（TestClient 是同步的）。"""
    import asyncio

    async def _read():
        from sqlalchemy import select
        async with SessionLocal() as s:
            return (
                await s.execute(
                    select(SignalHistory).where(SignalHistory.signal_id == signal_id)
                )
            ).scalars().first()

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_read())
    finally:
        loop.close()


def _count_tasks(signal_id: str) -> int:
    import asyncio

    async def _count():
        from sqlalchemy import func, select
        async with SessionLocal() as s:
            return (
                await s.execute(
                    select(func.count())
                    .select_from(GroupSignalTask)
                    .where(GroupSignalTask.signal_id == signal_id)
                )
            ).scalar_one()

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_count())
    finally:
        loop.close()


# =====================================================================
# 1. 方向不符 -> 丢弃
# =====================================================================
def test_sell_signal_is_dropped(client):
    h = auth_headers(client)
    _set_filters(client, h, ai_entry=_ai_entry())
    _mk_group(client, h, name="AI组")

    r = client.post("/webhook", json=_tv_signal(action="sell"))
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["status"] == "rejected"
    assert body["targets"] == 0
    assert body["groups"] == 0
    assert "AI 开仓准入" in body["reason"]
    assert body["filtered_groups"][0]["code"] == "ai_entry_admission"

    # 被拦分组不产生主任务
    assert _count_tasks(body["signal_id"]) == 0
    # 信号入库并带拒收编码
    row = _fetch_signal(body["signal_id"])
    assert row is not None
    assert row.reject_code == "ai_entry_admission"
    assert row.status == "rejected"


# =====================================================================
# 2. 价格超区间 -> 丢弃
# =====================================================================
def test_buy_signal_outside_interval_is_dropped(client):
    h = auth_headers(client)
    _set_filters(client, h, ai_entry=_ai_entry())
    _mk_group(client, h, name="AI组")

    # 信号自带价 4300 -> 在 4000~4200 之外 -> 拦截
    r = client.post("/webhook", json=_tv_signal(price=4300))
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["status"] == "rejected"
    assert body["targets"] == 0
    assert "不在任何配置区间内" in body["reason"]
    assert _count_tasks(body["signal_id"]) == 0


def test_buy_signal_missing_price_is_dropped(client):
    """缺价直接拦截。"""
    h = auth_headers(client)
    _set_filters(client, h, ai_entry=_ai_entry())
    _mk_group(client, h, name="AI组")

    # 信号不带价、节点也没有账户快照 -> 取不到判定价
    r = client.post("/webhook", json=_tv_signal())
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["status"] == "rejected"
    assert body["targets"] == 0
    assert "无可用价格" in body["reason"]
    assert _count_tasks(body["signal_id"]) == 0


# =====================================================================
# 3. 方向与价格都符合 -> 正常下发
# =====================================================================
def test_buy_signal_inside_interval_is_dispatched(client):
    h = auth_headers(client)
    _set_filters(client, h, ai_entry=_ai_entry())
    _mk_group(client, h, name="AI组")

    r = client.post("/webhook", json=_tv_signal(price=4100))
    assert r.status_code == 200, r.text
    body = r.json()

    # 分组没有在线节点 -> 主任务会被跳过，但信号确实路由到了该分组
    assert body["status"] != "rejected"
    assert body["groups"] == 1
    assert body["mode"] == "group"


# =====================================================================
# 4. 未启用准入 -> 存量行为零变化
# =====================================================================
def test_disabled_ai_entry_keeps_legacy_behavior(client):
    h = auth_headers(client)
    _set_filters(client, h)  # 不写 ai_entry
    _mk_group(client, h, name="AI组")

    r = client.post("/webhook", json=_tv_signal(action="sell"))
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["status"] != "rejected"
    assert body["groups"] == 1


# =====================================================================
# 5. auto 路由同样被拦，规则命中统计照常
# =====================================================================
def test_auto_route_is_blocked_but_rule_hit_recorded(client):
    h = auth_headers(client)
    _set_filters(client, h, ai_entry=_ai_entry())
    g = _mk_group(client, h, name="自动组")
    client.patch(f"/api/groups/{g['group_id']}", json={"signal_source": "auto"}, headers=h)

    rule = client.post(
        "/api/signal-center/route-rules",
        json={
            "name": "结构分析 → 自动组",
            "signal_types": ["结构分析"],
            "group_ids": [g["group_id"]],
        },
        headers=h,
    )
    assert rule.status_code == 200, rule.text
    rule_id = rule.json()["rule_id"]

    r = client.post("/webhook", json=_tv_signal(action="sell"))
    assert r.status_code == 200, r.text
    body = r.json()

    # 规则命中了，但被 AI 准入拦下 -> 不下发
    assert body["auto_groups"] == [g["group_id"]]
    assert body["targets"] == 0
    assert body["status"] == "rejected"
    assert _count_tasks(body["signal_id"]) == 0

    # 规则命中统计**照常记录**（顺序：规则匹配 -> 准入 -> 下发）
    lst = client.get("/api/signal-center/route-rules", headers=h).json()
    hit = next(x for x in lst["items"] if x["rule_id"] == rule_id)["hit"]
    assert hit["count"] == 1


# =====================================================================
# 6. tpl_2 分组不受影响
# =====================================================================
def test_tpl2_group_is_not_affected(client):
    h = auth_headers(client)
    _set_filters(client, h, ai_entry=_ai_entry())
    _mk_group(client, h, name="带单组", template_id="tpl_2")

    # tpl_2 走「以损定量趋势单」，需要止损价
    r = client.post(
        "/webhook",
        json=_tv_signal(action="sell", template_ids=["tpl_2"], sl=4000, price=4100),
    )
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["status"] != "rejected"
    assert body["groups"] == 1
    assert "AI 开仓准入" not in (body.get("reason") or "")
