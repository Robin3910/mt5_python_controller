"""策略模版与策略管理 API 单元测试。"""
import pathlib

import fakeredis
import pytest
from fastapi.testclient import TestClient

from app.redis_store import RedisStore
from app.strategy_templates import (
    RULE_TYPE_GRID,
    RULE_TYPE_RISK_SIZED,
    TEMPLATE_1_ID,
    TEMPLATE_1_NAME,
    TEMPLATE_2_ID,
    TEMPLATE_2_NAME,
    TEMPLATE_3_ID,
    TEMPLATE_3_NAME,
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
    # 连接池由 app 的 lifespan 在自己的事件循环里释放，这里只清文件
    drop_test_db(_TEST_DB)


from tests.test_helpers import auth_headers, drop_test_db, reset_test_db


def test_strategy_endpoints_require_auth(client):
    assert client.get("/api/strategies").status_code == 401
    assert client.get("/api/strategies/templates").status_code == 401
    assert client.post("/api/strategies", json={
        "template_id": TEMPLATE_1_ID, "name": "x", "symbol": "XAUUSD",
    }).status_code == 401


def test_list_templates_contains_template_1(client):
    h = auth_headers(client)
    r = client.get("/api/strategies/templates", headers=h)
    assert r.status_code == 200, r.text
    items = r.json()
    assert len(items) >= 1
    tpl = next(t for t in items if t["template_id"] == TEMPLATE_1_ID)
    assert tpl["name"] == TEMPLATE_1_NAME
    assert len(tpl["rules"]) == 2
    types = {rule["type"] for rule in tpl["rules"]}
    assert types == {1, 2}  # 逆势 + 顺势
    by_type = {rule["type"]: rule for rule in tpl["rules"]}
    assert by_type[1]["status"] == 1
    assert by_type[1]["action"] == "all"
    assert by_type[1]["lot_times"] == 1.1
    assert by_type[1]["extra_lot"] == 0
    assert by_type[1]["max_allow_num"] == 3
    assert by_type[1]["batch_enabled"] is True
    assert by_type[1]["batch_action"] == "all"
    assert by_type[1]["batch_count"] == 3
    assert by_type[1]["total_lot_limit"] == 10
    assert len(by_type[1]["batch_levels"]) == 3
    assert by_type[2]["status"] == 1
    assert by_type[2]["action"] == "all"
    assert by_type[2]["lot_times"] == 0.8
    assert by_type[2]["extra_lot"] == 0
    assert by_type[2]["max_allow_num"] == 10
    assert by_type[2]["batch_enabled"] is False
    assert by_type[2]["batch_action"] == "all"


def test_create_strategy_from_template(client):
    h = auth_headers(client)
    r = client.post(
        "/api/strategies",
        json={"template_id": TEMPLATE_1_ID, "name": "黄金策略A", "symbol": "xauusd"},
        headers=h,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["strategy_id"].startswith("sty_")
    assert body["name"] == "黄金策略A"
    assert body["symbol"] == "XAUUSD"  # 自动大写
    assert body["template_id"] == TEMPLATE_1_ID
    assert body["template_name"] == TEMPLATE_1_NAME
    assert body["enabled"] is True
    assert len(body["rules"]) == 2
    assert {rule["type"] for rule in body["rules"]} == {1, 2}


def test_create_strategy_with_custom_rules(client):
    """选模版后可覆盖默认规则参数，落库以自定义值为准。"""
    h = auth_headers(client)
    r = client.post(
        "/api/strategies",
        json={
            "template_id": TEMPLATE_1_ID,
            "name": "自定义参数策略",
            "symbol": "XAUUSD",
            "rules": [
                {
                    "type": 1,
                    "status": 1,
                    "action": "buy",
                    "point": 200,
                    "lot_times": 1.5,
                    "extra_lot": 0.02,
                    "max_allow_num": 3,
                },
                {
                    "type": 2,
                    "status": 0,
                    "action": "sell",
                    "point": 50,
                    "lot_times": 2,
                    "extra_lot": 0,
                    "max_allow_num": 1,
                },
            ],
        },
        headers=h,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    by_type = {rule["type"]: rule for rule in body["rules"]}
    assert by_type[1]["action"] == "buy"
    assert by_type[1]["point"] == 200
    assert by_type[1]["lot_times"] == 1.5
    assert by_type[1]["extra_lot"] == 0.02
    assert by_type[1]["max_allow_num"] == 3
    assert by_type[2]["status"] == 0
    assert by_type[2]["action"] == "sell"


def _batch_rule(level: dict) -> dict:
    """单档分批的逆势规则，用于验证档位字段的规范化。"""
    return {
        "type": 1, "status": 1, "action": "all",
        "point": 100, "lot_times": 1.1, "extra_lot": 0, "max_allow_num": 3,
        "batch_enabled": True, "batch_action": "all",
        "batch_count": 1, "total_lot_limit": 10,
        "batch_levels": [{"pos_from": 2, "pos_to": 10, "lot_times": 1.2, "extra_lot": 0, **level}],
    }


def _created_level(client, headers, name: str, level: dict) -> dict:
    r = client.post(
        "/api/strategies",
        json={
            "template_id": TEMPLATE_1_ID, "name": name, "symbol": "XAUUSD",
            "rules": [_batch_rule(level)],
        },
        headers=headers,
    )
    assert r.status_code == 201, r.text
    rule = next(x for x in r.json()["rules"] if x["type"] == 1)
    return rule["batch_levels"][0]


def test_batch_level_keeps_each_calc_type(client):
    """四种加仓间距计算方式都要能原样存下来。"""
    h = auth_headers(client)

    price = _created_level(client, h, "指定价策略", {"calc_type": "price", "price": 2399.5})
    assert (price["calc_type"], price["price"]) == ("price", 2399.5)

    atr = _created_level(client, h, "ATR策略", {"calc_type": "atr", "timeframe": "H1"})
    assert (atr["calc_type"], atr["timeframe"]) == ("atr", "H1")

    rng = _created_level(client, h, "波幅策略", {"calc_type": "range", "timeframe": "M15"})
    assert (rng["calc_type"], rng["timeframe"]) == ("range", "M15")

    point = _created_level(client, h, "点数策略", {"calc_type": "point", "point": 250})
    assert (point["calc_type"], point["point"]) == ("point", 250)


def test_batch_level_normalizes_illegal_calc_and_timeframe(client):
    """枚举类字段没法靠类型校验拦住，非法取值回落到默认，不把坏配置下发给节点。"""
    h = auth_headers(client)
    level = _created_level(
        client, h, "非法取值策略", {"calc_type": "unknown", "timeframe": "M7"},
    )
    assert level["calc_type"] == "point"
    assert level["timeframe"] == "M5"


def test_batch_level_rejects_negative_price(client):
    h = auth_headers(client)
    r = client.post(
        "/api/strategies",
        json={
            "template_id": TEMPLATE_1_ID, "name": "负价策略", "symbol": "XAUUSD",
            "rules": [_batch_rule({"calc_type": "price", "price": -1})],
        },
        headers=h,
    )
    assert r.status_code == 422


def test_batch_level_defaults_when_fields_absent(client):
    """老配置没有新字段时要能补上默认值，不影响既有策略。"""
    h = auth_headers(client)
    level = _created_level(client, h, "老配置策略", {"point": 150})
    assert level["calc_type"] == "point"
    assert level["price"] == 0
    assert level["timeframe"] == "M5"


# ---------------------------------------------------------------------------
# 策略模版2：以损定量趋势单
# ---------------------------------------------------------------------------

def _risk_sized_rule(**over) -> dict:
    rule = {
        "type": RULE_TYPE_RISK_SIZED,
        "status": 1,
        "action": "all",
        "risk_amount": 500,
        "rr_ratio": 3,
        "base_ratio": 40,
        "add_batches": 3,
        "max_total_lot": 2,
        "breakeven_enabled": True,
        "breakeven_times": 1.5,
        "breakeven_mode": "loop",
    }
    rule.update(over)
    return rule


def _created_risk_rule(client, headers, name: str, **over) -> dict:
    r = client.post(
        "/api/strategies",
        json={
            "template_id": TEMPLATE_2_ID, "name": name, "symbol": "BTCUSD",
            "rules": [_risk_sized_rule(**over)],
        },
        headers=headers,
    )
    assert r.status_code == 201, r.text
    rules = r.json()["rules"]
    assert len(rules) == 1
    return rules[0]


def test_list_templates_contains_template_2(client):
    h = auth_headers(client)
    items = client.get("/api/strategies/templates", headers=h).json()
    tpl = next(t for t in items if t["template_id"] == TEMPLATE_2_ID)
    assert tpl["name"] == TEMPLATE_2_NAME
    assert "止损价" in tpl["description"]
    assert len(tpl["rules"]) == 1
    rule = tpl["rules"][0]
    assert rule["type"] == RULE_TYPE_RISK_SIZED
    assert rule["status"] == 1
    assert rule["action"] == "all"
    assert rule["risk_amount"] == 100
    assert rule["rr_ratio"] == 2.5
    assert rule["base_ratio"] == 30
    assert rule["add_batches"] == 10
    assert rule["max_total_lot"] == 0
    assert rule["breakeven_enabled"] is True
    assert rule["breakeven_times"] == 2
    assert rule["breakeven_mode"] == "once"
    assert "entry_direction" not in rule
    assert "batch_gap_points" not in rule


def test_create_strategy_from_template_2(client):
    h = auth_headers(client)
    r = client.post(
        "/api/strategies",
        json={"template_id": TEMPLATE_2_ID, "name": "BTC以损定量", "symbol": "btcusd"},
        headers=h,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["template_id"] == TEMPLATE_2_ID
    assert body["template_name"] == TEMPLATE_2_NAME
    assert body["symbol"] == "BTCUSD"
    assert [rule["type"] for rule in body["rules"]] == [RULE_TYPE_RISK_SIZED]


def test_risk_sized_rule_keeps_custom_values(client):
    rule = _created_risk_rule(client, auth_headers(client), "自定义以损定量")
    assert rule["risk_amount"] == 500
    assert rule["rr_ratio"] == 3
    assert rule["base_ratio"] == 40
    assert rule["add_batches"] == 3
    assert rule["max_total_lot"] == 2
    assert rule["breakeven_enabled"] is True
    assert rule["breakeven_times"] == 1.5
    assert rule["breakeven_mode"] == "loop"


def test_risk_sized_full_base_when_not_distributing(client):
    """无分散仓时底仓必须是全仓，否则剩下的仓位永远开不进来。"""
    rule = _created_risk_rule(
        client, auth_headers(client), "无分散仓以损定量", add_batches=0, base_ratio=30,
    )
    assert rule["add_batches"] == 0
    assert rule["base_ratio"] == 100


def test_risk_sized_strips_legacy_entry_fields(client):
    """旧的补仓方向 / 间距字段不再落库。"""
    rule = _created_risk_rule(
        client, auth_headers(client), "剥离旧字段",
        entry_direction="sideways", batch_gap_points=999,
    )
    assert "entry_direction" not in rule
    assert "batch_gap_points" not in rule


def test_risk_sized_normalizes_illegal_breakeven_mode(client):
    rule = _created_risk_rule(
        client, auth_headers(client), "非法保本模式", breakeven_mode="always",
    )
    assert rule["breakeven_mode"] == "once"


def test_risk_sized_rejects_out_of_range_values(client):
    h = auth_headers(client)
    for over in ({"risk_amount": -1}, {"base_ratio": 101}, {"rr_ratio": -0.5},
                 {"add_batches": -1}, {"breakeven_times": -1}, {"add_batches": 51}):
        r = client.post(
            "/api/strategies",
            json={
                "template_id": TEMPLATE_2_ID, "name": f"越界{over}", "symbol": "BTCUSD",
                "rules": [_risk_sized_rule(**over)],
            },
            headers=h,
        )
        assert r.status_code == 422, (over, r.text)


def test_risk_sized_rule_survives_update(client):
    h = auth_headers(client)
    created = client.post(
        "/api/strategies",
        json={"template_id": TEMPLATE_2_ID, "name": "待改以损定量", "symbol": "BTCUSD"},
        headers=h,
    ).json()
    r = client.patch(
        f"/api/strategies/{created['strategy_id']}",
        json={"rules": [_risk_sized_rule(risk_amount=800, add_batches=1)]},
        headers=h,
    )
    assert r.status_code == 200, r.text
    rule = r.json()["rules"][0]
    assert rule["type"] == RULE_TYPE_RISK_SIZED
    assert rule["risk_amount"] == 800
    assert rule["add_batches"] == 1


def test_unknown_rule_type_still_falls_back_to_counter(client):
    """未知 type 一律按逆势加仓处理，保持历史行为。"""
    h = auth_headers(client)
    r = client.post(
        "/api/strategies",
        json={
            "template_id": TEMPLATE_1_ID, "name": "未知类型策略", "symbol": "XAUUSD",
            "rules": [{"type": 99, "status": 1, "action": "all"}],
        },
        headers=h,
    )
    assert r.status_code == 201, r.text
    rule = r.json()["rules"][0]
    assert rule["type"] == 1
    assert rule["point"] == 100     # 逆势默认值


# ---------------------------------------------------------------------------
# 规则规范化（纯函数，无需 API）
#
# API 响应走 Pydantic 模型，两种 type 的字段都会带默认值输出；真正要保证的是落库
# 只写该 type 自己的字段，所以这一段直接测归一化函数。
# ---------------------------------------------------------------------------

def test_normalize_keeps_only_risk_sized_fields():
    from app import strategy_templates as tpl

    out = tpl.normalize_rule({
        **_risk_sized_rule(),
        # 混入加仓类字段：不属于本 type，不应落库
        "point": 200, "lot_times": 9, "extra_lot": 1, "max_allow_num": 7,
        "batch_enabled": True, "batch_count": 5, "batch_levels": [{"pos_from": 2}],
        "entry_direction": "pullback", "batch_gap_points": 100,
    })
    assert set(out) == {
        "type", "status", "action", "risk_amount", "rr_ratio", "base_ratio",
        "add_batches", "max_total_lot", "breakeven_enabled", "breakeven_times",
        "breakeven_mode",
    }


def test_normalize_keeps_only_add_on_fields():
    from app import strategy_templates as tpl

    out = tpl.normalize_rule({
        "type": 1, "status": 1, "action": "all",
        # 混入以损定量字段：不属于本 type，不应落库
        "risk_amount": 500, "rr_ratio": 3, "breakeven_enabled": True,
    })
    assert "risk_amount" not in out
    assert "breakeven_enabled" not in out
    assert out["point"] == 100


def test_normalize_risk_sized_clamps_ratios():
    from app import strategy_templates as tpl

    out = tpl.normalize_rule(_risk_sized_rule(base_ratio=0))
    assert out["base_ratio"] == 30      # 0 无法开出底仓，回落到默认


def test_rules_to_rule_set_ignores_risk_sized():
    """模版1 的还原函数遇到模版2 规则应直接跳过，不能崩。"""
    from app import strategy_templates as tpl

    rule_set = tpl.rules_to_rule_set([_risk_sized_rule()])
    assert rule_set.counter.point == 100
    assert rule_set.trend.point == 100


def test_pick_risk_sized_rule():
    from app import strategy_templates as tpl

    assert tpl.pick_risk_sized_rule([_risk_sized_rule()]) is not None
    assert tpl.pick_risk_sized_rule([_risk_sized_rule(status=0)]) is None
    assert tpl.pick_risk_sized_rule([{"type": 1, "status": 1}]) is None
    assert tpl.pick_risk_sized_rule(None) is None


# ---------------------------------------------------------------------------
# 开仓准入
# ---------------------------------------------------------------------------

def test_entry_reject_requires_stop_loss_for_risk_sized():
    from app import group_rules

    strategy = {"rules": [_risk_sized_rule()]}
    assert group_rules.entry_reject_reason(strategy, 62246.36) is None
    assert "止损价" in (group_rules.entry_reject_reason(strategy, None) or "")
    assert "止损价" in (group_rules.entry_reject_reason(strategy, 0) or "")


def test_entry_reject_ignores_add_on_strategies():
    """模版1 首单手数来自信号，没有止损也能开，准入不应拦它。"""
    from app import group_rules

    strategy = {"rules": [{"type": 1, "status": 1, "action": "all"}]}
    assert group_rules.entry_reject_reason(strategy, None) is None


def test_create_strategy_duplicate_name_409(client):
    h = auth_headers(client)
    payload = {"template_id": TEMPLATE_1_ID, "name": "重名策略", "symbol": "EURUSD"}
    assert client.post("/api/strategies", json=payload, headers=h).status_code == 201
    r = client.post("/api/strategies", json=payload, headers=h)
    assert r.status_code == 409


def test_create_strategy_unknown_template_400(client):
    h = auth_headers(client)
    r = client.post(
        "/api/strategies",
        json={"template_id": "tpl_missing", "name": "坏模版", "symbol": "XAUUSD"},
        headers=h,
    )
    assert r.status_code == 400
    assert "模版不存在" in r.json()["detail"]


def test_list_search_toggle_delete_strategy(client):
    h = auth_headers(client)
    created = client.post(
        "/api/strategies",
        json={"template_id": TEMPLATE_1_ID, "name": "可搜策略", "symbol": "GBPUSD"},
        headers=h,
    ).json()
    sid = created["strategy_id"]

    listed = client.get("/api/strategies", headers=h).json()
    assert any(s["strategy_id"] == sid for s in listed)

    hit = client.get("/api/strategies", params={"q": "gbpusd"}, headers=h).json()
    assert [s["strategy_id"] for s in hit] == [sid]

    off = client.patch(f"/api/strategies/{sid}", json={"enabled": False}, headers=h)
    assert off.status_code == 200
    assert off.json()["enabled"] is False

    assert client.delete(f"/api/strategies/{sid}", headers=h).status_code == 200
    assert client.get(f"/api/strategies/{sid}", headers=h).status_code == 404


# ---------------------------------------------------------------------------
# 模版3：网格交易
# ---------------------------------------------------------------------------

def _grid_rule(**over) -> dict:
    rule = {
        "type": RULE_TYPE_GRID, "status": 1, "action": "all",
        "price_lower": 100.0, "price_upper": 110.0,
        "grid_count": 10, "grid_mode": "arithmetic",
        "grid_side": "long", "lot_per_grid": 0.01,
        "total_lot_limit": 0.0, "trigger_price": 0.0,
        "stop_lower": 0.0, "stop_upper": 0.0,
        "close_on_stop": True, "prefill_enabled": True,
        "trailing_up": False, "trailing_max": 0,
    }
    rule.update(over)
    return rule


def test_list_templates_contains_template_3(client):
    h = auth_headers(client)
    r = client.get("/api/strategies/templates", headers=h)
    assert r.status_code == 200, r.text
    items = r.json()
    tpl = next(t for t in items if t["template_id"] == TEMPLATE_3_ID)
    assert tpl["name"] == TEMPLATE_3_NAME
    assert len(tpl["rules"]) == 1
    assert tpl["rules"][0]["type"] == RULE_TYPE_GRID
    assert tpl["rules"][0]["grid_mode"] == "arithmetic"
    assert tpl["rules"][0]["grid_side"] == "long"
    assert tpl["rules"][0]["prefill_enabled"] is True
    assert tpl["rules"][0]["trailing_up"] is False


def test_create_strategy_from_template_3(client):
    h = auth_headers(client)
    r = client.post(
        "/api/strategies",
        json={
            "template_id": TEMPLATE_3_ID,
            "name": "金网格",
            "symbol": "XAUUSD",
            "rules": [_grid_rule(price_lower=2300, price_upper=2400, grid_count=20)],
        },
        headers=h,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["template_id"] == TEMPLATE_3_ID
    assert body["rules"][0]["type"] == RULE_TYPE_GRID
    assert body["rules"][0]["price_lower"] == 2300
    assert body["rules"][0]["grid_count"] == 20
    assert body["rules"][0]["grid_side"] == "long"
    assert body["rules"][0]["lot_per_grid"] == 0.01


def test_normalize_grid_clamps_and_swaps_range():
    from app import strategy_templates as tpl

    out = tpl.normalize_rule(_grid_rule(
        price_lower=110, price_upper=100, grid_count=1, stop_lower=105, stop_upper=105,
    ))
    assert out["price_lower"] == 100
    assert out["price_upper"] == 110
    assert out["grid_count"] == 2          # 下限夹到 2
    assert out["stop_lower"] == 0.0        # 不低于下限则清零
    assert out["stop_upper"] == 0.0        # 不高于上限则清零
    # 规范化只保留网格字段，不加仓 / 以损定量字段
    assert "point" not in out
    assert "risk_amount" not in out


def test_normalize_grid_follow_falls_back_to_long():
    """历史 follow 已废弃，规范化回落到只做多。"""
    from app import strategy_templates as tpl

    out = tpl.normalize_rule(_grid_rule(grid_side="follow"))
    assert out["grid_side"] == "long"


def test_normalize_grid_short_keeps_directional_stops():
    """空头网格：上沿价为止损、下沿价为止盈；几何合法值应保留。"""
    from app import strategy_templates as tpl

    out = tpl.normalize_rule(_grid_rule(
        grid_side="short",
        price_lower=4152, price_upper=4167,
        stop_lower=4140,   # 空头止盈
        stop_upper=4170,   # 空头止损
    ))
    assert out["grid_side"] == "short"
    assert out["stop_lower"] == 4140
    assert out["stop_upper"] == 4170

    # 几何非法仍清零（与方向无关）
    bad = tpl.normalize_rule(_grid_rule(
        grid_side="short",
        price_lower=4152, price_upper=4167,
        stop_lower=4155,   # 未低于下限
        stop_upper=4160,   # 未高于上限
    ))
    assert bad["stop_lower"] == 0.0
    assert bad["stop_upper"] == 0.0


def test_normalize_grid_trailing():
    from app import strategy_templates as tpl

    out = tpl.normalize_rule(_grid_rule(trailing_up=1, trailing_max=-5))
    assert out["trailing_up"] is True
    assert out["trailing_max"] == 0        # 负数夹回不限

    out = tpl.normalize_rule(_grid_rule(trailing_max=10**9))
    assert out["trailing_max"] == tpl.GRID_TRAILING_MAX

    # 历史规则没有这两个字段时回落到默认值（关闭）
    legacy = _grid_rule()
    legacy.pop("trailing_up")
    legacy.pop("trailing_max")
    out = tpl.normalize_rule(legacy)
    assert out["trailing_up"] is False
    assert out["trailing_max"] == 0


def test_create_strategy_keeps_grid_trailing(client):
    h = auth_headers(client)
    r = client.post(
        "/api/strategies",
        json={
            "template_id": TEMPLATE_3_ID,
            "name": "追踪网格",
            "symbol": "XAUUSD",
            "rules": [_grid_rule(trailing_up=True, trailing_max=3)],
        },
        headers=h,
    )
    assert r.status_code == 201, r.text
    rule = r.json()["rules"][0]
    assert rule["trailing_up"] is True
    assert rule["trailing_max"] == 3


def test_pick_grid_rule():
    from app import strategy_templates as tpl

    assert tpl.pick_grid_rule([_grid_rule()]) is not None
    assert tpl.pick_grid_rule([_grid_rule(status=0)]) is None
    assert tpl.pick_grid_rule([{"type": 1, "status": 1}]) is None


def test_entry_reject_grid_side_and_lot():
    from app import group_rules

    strategy = {"rules": [_grid_rule(grid_side="long")]}
    assert group_rules.entry_reject_reason(strategy, None, signal_action="BUY") is None
    assert "只做多" in (
        group_rules.entry_reject_reason(strategy, None, signal_action="SELL") or ""
    )

    strategy = {"rules": [_grid_rule(grid_side="short")]}
    assert "只做空" in (
        group_rules.entry_reject_reason(strategy, None, signal_action="BUY") or ""
    )

    strategy = {"rules": [_grid_rule(lot_per_grid=0)]}
    assert "每格手数" in (
        group_rules.entry_reject_reason(strategy, None, signal_action="BUY") or ""
    )

    strategy = {"rules": [_grid_rule(price_lower=0, price_upper=0)]}
    assert "价格区间" in (
        group_rules.entry_reject_reason(strategy, None, signal_action="BUY") or ""
    )

    strategy = {"rules": [_grid_rule(lot_per_grid=0.02, total_lot_limit=0.01)]}
    assert "总手数上限" in (
        group_rules.entry_reject_reason(strategy, None, signal_action="BUY") or ""
    )

    strategy = {"rules": [_grid_rule(status=0)], "template_id": "tpl_3"}
    assert "启用中的规则" in (
        group_rules.entry_reject_reason(strategy, None, signal_action="BUY") or ""
    ) or "网格规则" in (
        group_rules.entry_reject_reason(strategy, None, signal_action="BUY") or ""
    )


def test_validate_rules_for_template_rejects_mismatch_and_empty():
    from app import strategy_templates as tpl

    assert tpl.validate_rules_for_template(TEMPLATE_3_ID, [_grid_rule()]) is None
    assert "不允许规则类型" in (
        tpl.validate_rules_for_template(TEMPLATE_3_ID, [{"type": 1, "status": 1}]) or ""
    )
    assert "至少启用" in (
        tpl.validate_rules_for_template(TEMPLATE_3_ID, [_grid_rule(status=0)]) or ""
    )
    assert "价格区间" in (
        tpl.validate_rules_for_template(
            TEMPLATE_3_ID, [_grid_rule(price_lower=0, price_upper=0)],
        ) or ""
    )


def test_create_grid_strategy_rejects_disabled_rule(client):
    h = auth_headers(client)
    r = client.post(
        "/api/strategies",
        json={
            "template_id": TEMPLATE_3_ID,
            "name": "全关网格",
            "symbol": "XAUUSD",
            "rules": [_grid_rule(status=0)],
        },
        headers=h,
    )
    assert r.status_code == 400, r.text
    assert "启用" in r.json()["detail"]


def test_create_grid_strategy_rejects_wrong_type(client):
    h = auth_headers(client)
    r = client.post(
        "/api/strategies",
        json={
            "template_id": TEMPLATE_3_ID,
            "name": "错配模版",
            "symbol": "XAUUSD",
            "rules": [{"type": 1, "status": 1, "action": "all", "point": 100}],
        },
        headers=h,
    )
    assert r.status_code == 400, r.text
    assert "不允许规则类型" in r.json()["detail"]
