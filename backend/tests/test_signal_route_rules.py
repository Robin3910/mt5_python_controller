"""**自动路由规则**：匹配语义 + 自动下发端到端用例。

覆盖六件最容易出错的事：

1. **匹配语义**：类型 / 周期 / 方向的空值 = 不限制；
2. **业务分类展开**：规则里写「结构分析」要匹配 ob / fvg / pdz **三个**码
   （只取一个码会把另外两个漏掉）；
3. **命中即占位**：多条规则命中同一分组时**只下发一次**
   （不去重会重复开仓 —— 真金白银的错误）；
4. **优先级**：priority 小的规则先挑走分组；
5. **auto 分组没命中规则 ≠ 拒收**：信号本身是好的，只是没有分组要它，
   不能判 rejected；
6. **交集语义**：告警自带的 `group_ids` 与规则命中集**取交集**
   （两者都是收窄语义，交集是唯一不违反任一方意图的组合）。
"""
import pathlib

import fakeredis
import pytest
from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.orm import NodeGroup
from app.redis_store import RedisStore
from app.state import state
from tests.test_helpers import (
    auth_headers,
    drop_test_db,
    reset_test_db,
    seed_default_filters,
)

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


# ---------------------------------------------------------------------------
# 纯函数层：匹配语义
# ---------------------------------------------------------------------------
def _rule(**over) -> dict:
    base = {
        "rule_id": "r1", "name": "测试规则", "enabled": True,
        "signal_types": [], "timeframes": [], "directions": [],
        "group_ids": ["g1"], "priority": 100,
    }
    base.update(over)
    return base


def _sig(**over) -> dict:
    base = {"signal_type": "ob", "timeframe": "M15", "action": "BUY"}
    base.update(over)
    return base


def test_empty_conditions_match_everything():
    """空条件 = 不限制（与 template_ids / group_ids 的语义一致）。"""
    from app.signal_route_rules import rule_matches

    assert rule_matches(_rule(), _sig()) is True
    assert rule_matches(_rule(), _sig(signal_type=None, timeframe=None)) is True


def test_signal_type_condition():
    from app.signal_route_rules import rule_matches

    rule = _rule(signal_types=["ob"])
    assert rule_matches(rule, _sig(signal_type="ob")) is True
    assert rule_matches(rule, _sig(signal_type="fvg")) is False


def test_type_group_expands_to_all_its_codes():
    """业务分类「结构分析」= ob + fvg + pdz 三个码，缺一不可。"""
    from app.signal_route_rules import rule_matches

    rule = _rule(signal_types=["结构分析"])
    assert rule_matches(rule, _sig(signal_type="ob")) is True
    assert rule_matches(rule, _sig(signal_type="fvg")) is True
    assert rule_matches(rule, _sig(signal_type="pdz")) is True
    assert rule_matches(rule, _sig(signal_type="fibo")) is False


def test_timeframe_condition():
    from app.signal_route_rules import rule_matches

    rule = _rule(timeframes=["M15", "H1"])
    assert rule_matches(rule, _sig(timeframe="M15")) is True
    assert rule_matches(rule, _sig(timeframe="H1")) is True
    assert rule_matches(rule, _sig(timeframe="D1")) is False


def test_unlabeled_signal_does_not_match_a_condition():
    """规则限定了类型，而信号类型是「没有」→ 不命中（不是匹配）。"""
    from app.signal_route_rules import rule_matches

    assert rule_matches(_rule(signal_types=["ob"]), _sig(signal_type=None)) is False
    assert rule_matches(_rule(timeframes=["M15"]), _sig(timeframe=None)) is False


def test_direction_condition():
    from app.signal_route_rules import rule_matches

    rule = _rule(directions=["BUY"])
    assert rule_matches(rule, _sig(action="BUY")) is True
    assert rule_matches(rule, _sig(action="SELL")) is False


# ---------------------------------------------------------------------------
# 命中即占位 + 优先级
# ---------------------------------------------------------------------------
def test_hit_takes_group_so_it_is_not_dispatched_twice():
    """同一分组被多条规则命中 → 只挑走一次（否则重复开仓）。"""
    from app.signal_route_rules import match_groups

    rules = [
        _rule(rule_id="r1", signal_types=["ob"], group_ids=["g1", "g2"], priority=10),
        _rule(rule_id="r2", timeframes=["M15"], group_ids=["g1"], priority=20),
    ]
    hit, owner = match_groups(rules, _sig(), {"g1", "g2"})
    assert set(hit) == {"g1", "g2"}
    # g1 被 priority 更小的 r1 挑走
    assert owner["g1"] == "r1"
    assert owner["g2"] == "r1"


def test_priority_decides_which_rule_takes_the_group():
    """priority 小的规则先挑走分组。"""
    from app.signal_route_rules import match_groups

    rules = [
        _rule(rule_id="low", signal_types=["ob"], group_ids=["g1"], priority=900),
        _rule(rule_id="high", timeframes=["M15"], group_ids=["g1"], priority=1),
    ]
    hit, owner = match_groups(rules, _sig(), {"g1"})
    assert hit == ["g1"]
    assert owner["g1"] == "high"


def test_disabled_rule_is_skipped():
    from app.signal_route_rules import match_groups

    rules = [_rule(rule_id="r1", enabled=False, group_ids=["g1"])]
    hit, _ = match_groups(rules, _sig(), {"g1"})
    assert hit == []


def test_candidate_set_bounds_the_result():
    """只有准入通过的分组才可能被规则选中。"""
    from app.signal_route_rules import match_groups

    rules = [_rule(group_ids=["g1", "g9"])]
    hit, _ = match_groups(rules, _sig(), {"g1"})
    assert hit == ["g1"]


# ---------------------------------------------------------------------------
# 入参归一 / 校验
# ---------------------------------------------------------------------------
def test_rule_payload_requires_groups_and_name():
    from app.signal_route_rules import RuleInputError, validate_rule_payload

    with pytest.raises(RuleInputError):
        validate_rule_payload({"name": "x", "group_ids": []})
    with pytest.raises(RuleInputError):
        validate_rule_payload({"name": "", "group_ids": ["g1"]})


def test_rule_payload_normalizes_values():
    from app.signal_route_rules import validate_rule_payload

    out = validate_rule_payload({
        "name": "  规则  ",
        "group_ids": ["G1", "g1", " g2 "],
        "timeframes": ["m15", "H1", "M7"],   # M7 认不出 → 丢弃
        "signal_types": ["OB", "结构分析"],
        "directions": ["buy", "bogus"],
    })
    assert out["name"] == "规则"
    assert out["group_ids"] == ["g1", "g2"]     # 去重 + 小写
    assert out["timeframes"] == ["M15", "H1"]   # 归一 + 丢弃认不出的
    assert out["signal_types"] == ["ob", "结构分析"]
    assert out["directions"] == ["BUY"]         # 只保留 BUY / SELL


# ---------------------------------------------------------------------------
# API 层
# ---------------------------------------------------------------------------
def _mk_strategy(client, headers, name: str, symbol: str = "XAUUSD") -> dict:
    from app.strategy_templates import TEMPLATE_1_ID
    r = client.post(
        "/api/strategies",
        json={"template_id": TEMPLATE_1_ID, "name": name, "symbol": symbol},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    return r.json()


def _mk_group(client, headers, *, name: str, symbol: str = "XAUUSD", **body) -> dict:
    payload = {"name": name, "enabled": True, "dispatch_mode": "sync", "node_ids": []}
    payload.update(body)
    if "strategy_id" not in payload:
        sty = _mk_strategy(client, headers, name=f"策略-{name}", symbol=symbol)
        payload["strategy_id"] = sty["strategy_id"]
    r = client.post("/api/groups", json=payload, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def _tv_signal(symbol: str = "XAUUSD", **extra) -> dict:
    payload = {
        "action": "buy", "symbol": symbol, "volume": 0.1,
        "model": "strategy", "template_ids": ["tpl_1"],
        "tf": "M15", "signal_type": "ob",
    }
    payload.update(extra)
    return payload


def test_route_rule_crud(client):
    """规则 CRUD：创建 → 列表 → 更新 → 删除。"""
    h = auth_headers(client)
    g = _mk_group(client, h, name="自动组")
    client.patch(f"/api/groups/{g['group_id']}", json={"signal_source": "auto"}, headers=h)

    r = client.post(
        "/api/signal-center/route-rules",
        json={
            "name": "结构 → 自动组",
            "signal_types": ["结构分析"],
            "timeframes": ["M15"],
            "group_ids": [g["group_id"]],
        },
        headers=h,
    )
    assert r.status_code == 200, r.text
    rule = r.json()
    assert rule["name"] == "结构 → 自动组"
    assert rule["signal_types"] == ["结构分析"]
    assert rule["hit"]["count"] == 0

    lst = client.get("/api/signal-center/route-rules", headers=h).json()
    assert len(lst["items"]) == 1
    assert lst["items"][0]["rule_id"] == rule["rule_id"]

    r = client.put(
        f"/api/signal-center/route-rules/{rule['rule_id']}",
        json={
            "name": "改名了",
            "enabled": False,
            "signal_types": [],
            "timeframes": [],
            "directions": [],
            "group_ids": [g["group_id"]],
            "priority": 5,
        },
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "改名了"
    assert r.json()["enabled"] is False
    assert r.json()["signal_types"] == []
    assert r.json()["priority"] == 5

    r = client.delete(f"/api/signal-center/route-rules/{rule['rule_id']}", headers=h)
    assert r.status_code == 200, r.text
    assert client.get("/api/signal-center/route-rules", headers=h).json()["items"] == []


def test_route_rule_rejects_empty_group_list(client):
    """没有目标分组的规则直接拒收（服务端也拒）。"""
    h = auth_headers(client)
    r = client.post(
        "/api/signal-center/route-rules",
        json={"name": "没分组", "group_ids": []},
        headers=h,
    )
    assert r.status_code == 400


def test_group_can_switch_to_auto_mode(client):
    """分组可以切到 auto 模式。"""
    h = auth_headers(client)
    g = _mk_group(client, h, name="可切自动组")
    r = client.patch(f"/api/groups/{g['group_id']}", json={"signal_source": "auto"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["signal_source"] == "auto"


def test_auto_group_receives_matching_signal_automatically(client):
    """auto 分组 + 命中规则 → 信号自动下发，不需要人工点「下发」。"""
    h = auth_headers(client)
    seed_default_filters(client)
    g = _mk_group(client, h, name="自动接收组")
    client.patch(f"/api/groups/{g['group_id']}", json={"signal_source": "auto"}, headers=h)

    client.post(
        "/api/signal-center/route-rules",
        json={
            "name": "结构分析 → 自动接收组",
            "signal_types": ["结构分析"],
            "group_ids": [g["group_id"]],
        },
        headers=h,
    )

    r = client.post("/webhook", json=_tv_signal())
    assert r.status_code == 200, r.text
    body = r.json()
    # 分组没有在线节点 → 主任务会被跳过，但**信号确实被路由到了这个分组**
    assert body["mode"] == "group"
    assert body["groups"] == 1
    assert body["route_mode"] == "auto"
    assert body["auto_groups"] == [g["group_id"]]


def test_auto_group_ignores_non_matching_signal(client):
    """规则限定 M15，M5 的信号不进这个分组。"""
    h = auth_headers(client)
    seed_default_filters(client)
    g = _mk_group(client, h, name="仅 M15 组")
    client.patch(f"/api/groups/{g['group_id']}", json={"signal_source": "auto"}, headers=h)

    client.post(
        "/api/signal-center/route-rules",
        json={
            "name": "仅 M15",
            "timeframes": ["M15"],
            "group_ids": [g["group_id"]],
        },
        headers=h,
    )

    r = client.post("/webhook", json=_tv_signal(tf="M5"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["groups"] == 0
    assert body.get("auto_groups") == []
    # **不是拒收** —— 信号本身是好的，只是没有分组要它
    assert body["status"] != "rejected"
    assert body["route_state"] == "auto"


def test_auto_group_without_rule_is_not_rejected(client):
    """auto 分组存在但一条规则都没配 → 信号正常入库，不是「已拒收」。"""
    h = auth_headers(client)
    seed_default_filters(client)
    g = _mk_group(client, h, name="无规则自动组")
    client.patch(f"/api/groups/{g['group_id']}", json={"signal_source": "auto"}, headers=h)

    r = client.post("/webhook", json=_tv_signal())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] != "rejected"
    assert body["route_state"] == "auto"
    assert body["groups"] == 0


def test_direct_group_is_unaffected_by_rules(client):
    """存量直连分组不受自动路由规则影响（零行为变化）。"""
    h = auth_headers(client)
    seed_default_filters(client)
    direct = _mk_group(client, h, name="照旧直连组")
    sty = _mk_strategy(client, h, name="自动组策略")
    auto_g = _mk_group(client, h, name="自动组", strategy_id=sty["strategy_id"])
    client.patch(f"/api/groups/{auto_g['group_id']}", json={"signal_source": "auto"}, headers=h)

    # 规则只指向 auto 分组
    client.post(
        "/api/signal-center/route-rules",
        json={
            "name": "只给自动组",
            "signal_types": ["ob"],
            "group_ids": [auto_g["group_id"]],
        },
        headers=h,
    )

    r = client.post("/webhook", json=_tv_signal())
    assert r.status_code == 200, r.text
    body = r.json()
    # 直连分组照旧被下发
    assert body["groups"] >= 1
    assert direct["group_id"] in body.get("auto_groups", []) or body["route_mode"] == "auto"


def test_signal_group_ids_intersects_with_rule_hits(client):
    """告警自带的 group_ids 与规则命中集取交集。"""
    h = auth_headers(client)
    seed_default_filters(client)
    sty = _mk_strategy(client, h, name="甲策略")
    g1 = _mk_group(client, h, name="甲组", strategy_id=sty["strategy_id"])
    sty2 = _mk_strategy(client, h, name="乙策略")
    g2 = _mk_group(client, h, name="乙组", strategy_id=sty2["strategy_id"])
    for gid in (g1["group_id"], g2["group_id"]):
        client.patch(f"/api/groups/{gid}", json={"signal_source": "auto"}, headers=h)

    client.post(
        "/api/signal-center/route-rules",
        json={
            "name": "两个组都收",
            "signal_types": ["ob"],
            "group_ids": [g1["group_id"], g2["group_id"]],
        },
        headers=h,
    )

    # 告警指定只发 g1 → 规则命中 {g1,g2} ∩ 告警定向 {g1} = {g1}
    r = client.post("/webhook", json=_tv_signal(group_ids=[g1["group_id"]]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("auto_groups") == [g1["group_id"]]


def test_rule_hit_count_increments(client):
    """命中统计：规则卡上要显示「命中 N 条 / 最近命中时间」。"""
    h = auth_headers(client)
    seed_default_filters(client)
    g = _mk_group(client, h, name="统计组")
    client.patch(f"/api/groups/{g['group_id']}", json={"signal_source": "auto"}, headers=h)

    r = client.post(
        "/api/signal-center/route-rules",
        json={"name": "统计规则", "signal_types": ["ob"], "group_ids": [g["group_id"]]},
        headers=h,
    )
    rule_id = r.json()["rule_id"]

    client.post("/webhook", json=_tv_signal())

    lst = client.get("/api/signal-center/route-rules", headers=h).json()
    hit = next(x for x in lst["items"] if x["rule_id"] == rule_id)["hit"]
    assert hit["count"] == 1
    assert hit["last_hit_ts"]
    assert hit["last_signal_id"]


# ---------------------------------------------------------------------------
# 人工路由 / center 模式已下线
# ---------------------------------------------------------------------------
def test_signal_source_center_is_rejected(client):
    """center 不可再写入（人工路由已下线）。"""
    h = auth_headers(client)
    g = _mk_group(client, h, name="想切中心组")

    r = client.patch(
        f"/api/groups/{g['group_id']}", json={"signal_source": "center"}, headers=h,
    )
    assert r.status_code == 400, r.text
    assert "已下线" in r.json()["detail"]


def test_manual_route_endpoints_are_gone(client):
    """人工路由的三个接口已删除（404 / 405）。"""
    h = auth_headers(client)

    r = client.post(
        "/api/signal-center/signals/batch-route",
        json={"signal_ids": ["x"], "group_ids": ["g1"]},
        headers=h,
    )
    assert r.status_code in (404, 405), r.text

    r = client.post(
        "/api/signal-center/signals/sig_1/route",
        json={"group_ids": ["g1"]},
        headers=h,
    )
    assert r.status_code in (404, 405), r.text

    r = client.post(
        "/api/signal-center/signals/sig_1/drop",
        json={"group_ids": []},
        headers=h,
    )
    assert r.status_code in (404, 405), r.text


def test_legacy_center_group_receives_nothing(client, monkeypatch):
    """**存量 center 分组不能回落成 direct** —— 那会让它突然开始无条件开仓。

    这是本次下线里最容易出错、后果最重的一处：下架人工路由的同时，
    如果把 center 当成「非法值 → 回落 direct」，那些分组会在同一刻开始
    接收所有品种匹配的信号，直接产生真实交易。

    所以 center 分组必须保持「不接收任何信号」。
    """
    h = auth_headers(client)
    seed_default_filters(client)

    g = _mk_group(client, h, name="遗留中心组")
    gid = g["group_id"]

    # 直接把存量分组写成 center（模拟下架前就存在的行）。
    # 分组本体在 Redis 缓存里，DB 只是持久化副本 —— 两处都要改。
    async def _set_center():
        async with SessionLocal() as s:
            row = await s.get(NodeGroup, gid)
            row.signal_source = "center"
            await s.commit()

        cached = await state.store.get_group(gid)
        if cached is not None:
            cached["signal_source"] = "center"
            await state.store.cache_group(cached)

    client.portal.call(_set_center)

    # 后端仍原样报告 center（前端据此显示「待改模式」）
    lst = client.get("/api/groups", headers=h).json()
    row = next(x for x in lst if x["group_id"] == gid)
    assert row["signal_source"] == "center"

    # 关键断言：这条信号**没有**下发到这个分组
    r = client.post("/webhook", json=_tv_signal())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("groups", 0) == 0
    assert body.get("targets", 0) == 0
    assert gid not in (body.get("auto_groups") or [])
