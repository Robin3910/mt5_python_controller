"""网格试算：纯函数、行情探针带回的合约规格与 /api/nodes/{id}/grid_sizing 接口。"""
import pathlib

import fakeredis
import pytest
from fastapi.testclient import TestClient

from app import grid_sizing, market_probe
from app.connections import manager
from app.redis_store import RedisStore
from tests.test_helpers import drop_test_db, reset_test_db

_TEST_DB = pathlib.Path(__file__).resolve().parent / "_test_api.db"

# 试算基准场景：区间 2000~2100、止损 1990、ATR=10 → 10 格；
# tick_value/tick_size = 100，即 1 个价格单位 = 每手 100 USD
_SPEC = {
    "symbol": "XAUUSD",
    "point": 0.01,
    "digits": 2,
    "tick_size": 0.01,
    "tick_value": 1.0,
    "volume_min": 0.01,
    "volume_step": 0.01,
    "volume_max": 100.0,
}
# 满仓（10 格，成本 2000..2090）到 1990 的距离合计 550 → 每手亏损 55000
_FULL_LOSS_PER_LOT = 55000.0


@pytest.fixture
def client(monkeypatch):
    def fake_from_url(cls, url=None):
        return RedisStore(fakeredis.FakeAsyncRedis(decode_responses=True))

    reset_test_db(_TEST_DB)
    market_probe.reset()  # 行情缓存是进程级的，用例之间必须互不串味
    monkeypatch.setattr(RedisStore, "from_url", classmethod(fake_from_url))
    from app.main import app

    with TestClient(app) as c:
        yield c
    market_probe.reset()
    drop_test_db(_TEST_DB)


def _auth(client) -> dict:
    r = client.post("/api/login", json={"username": "admin", "password": "admin123"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _mk_node(client, headers, mt5_login: int) -> str:
    r = client.post("/api/nodes", json={"name": f"grid-{mt5_login}", "mt5_login": mt5_login},
                    headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["node_id"]


def _flat_bars(count: int = 15, *, close: float = 2050.0, span: float = 5.0) -> list[dict]:
    """每根高低差 2×span 且无跳空的 K 线，ATR 稳定等于 2×span。"""
    return [
        {"time": 1_700_000_000 + i * 3600, "open": close,
         "high": close + span, "low": close - span, "close": close}
        for i in range(count)
    ]


def _stub_node(monkeypatch, *, bars=None, spec=_SPEC, quote=None, error=None,
               online=True) -> list[dict]:
    """用回包替代真实节点；返回下发命令列表，便于断言探针参数。"""
    sent: list[dict] = []

    async def fake_send(node_id: str, message: dict) -> bool:
        sent.append(message)
        if not online:
            return False
        if message.get("cmd") != "market_probe":
            return True
        payload = {
            "req_id": message["req_id"],
            "symbol": message["symbol"],
            "timeframe": message["timeframe"],
        }
        if error:
            payload["error"] = error
        else:
            payload["bars"] = bars if bars is not None else _flat_bars()
            payload["quote"] = quote or {}
            if spec is not None:
                payload["spec"] = spec
        market_probe.resolve(payload["req_id"], payload)
        return True

    monkeypatch.setattr(manager, "send_to_node", fake_send)
    return sent


def _params(**over) -> dict:
    params = {
        "price_lower": 2000.0,
        "price_upper": 2100.0,
        "grid_mode": "arithmetic",
        "grid_side": "long",
        "stop_lower": 1990.0,
        "stop_upper": 0.0,
        "atr_mult": 1.0,
        "spacing": 0.0,
        "max_loss": 550.0,
        "prefill_enabled": False,
        "close_on_stop": True,
        "trailing_up": False,
        "total_lot_limit": 0.0,
    }
    params.update(over)
    return params


def _evaluate(**over) -> dict:
    price = over.pop("price", 0.0)
    bars = over.pop("bars", None)
    spec = over.pop("spec", _SPEC)
    return grid_sizing.evaluate(
        bars if bars is not None else _flat_bars(), spec, _params(**over), price=price,
    )


def _codes(result: dict) -> set[str]:
    return {w["code"] for w in result["warnings"]}


# --------------------------- ATR（与节点侧同口径）---------------------------
def test_bars_needed_covers_prev_close():
    """ATR 的真实波幅要用前一根收盘价，所以比统计根数多取一根。"""
    assert grid_sizing.bars_needed() == 15


def test_true_range_uses_high_low_without_gap():
    assert grid_sizing.true_range({"high": 11, "low": 9, "close": 10}, 10) == 2


def test_true_range_covers_gap_against_prev_close():
    assert grid_sizing.true_range({"high": 15, "low": 14, "close": 14.5}, 9) == 6


def test_average_true_range_skips_first_bar():
    assert grid_sizing.average_true_range(_flat_bars()) == pytest.approx(10.0)
    assert grid_sizing.average_true_range([]) == 0.0
    assert grid_sizing.average_true_range(_flat_bars(1)) == 0.0


# --------------------------- 格距与网格线 ---------------------------
def test_grid_count_for_spacing_rounds_and_clamps():
    assert grid_sizing.grid_count_for_spacing(2000, 2100, 10) == 10
    assert grid_sizing.grid_count_for_spacing(2000, 2100, 30) == 3      # 3.33 → 3
    assert grid_sizing.grid_count_for_spacing(2000, 2100, 60) == 2      # 夹到下限
    assert grid_sizing.grid_count_for_spacing(2000, 2100, 0.01) == 200  # 夹到上限
    assert grid_sizing.grid_count_for_spacing(2000, 2100, 0) == 0


def test_build_levels_matches_node_side():
    levels = grid_sizing.build_levels(2000, 2100, 10, "arithmetic", digits=2)
    assert levels[0] == 2000.0 and levels[-1] == 2100.0
    assert len(levels) == 11
    assert levels[5] == 2050.0

    geo = grid_sizing.build_levels(100, 200, 2, "geometric", digits=4)
    assert geo[0] == 100.0 and geo[-1] == 200.0
    assert geo[1] == pytest.approx(141.4214, abs=1e-4)


# --------------------------- 成本与手数 ---------------------------
def test_entry_costs_use_grid_lines_without_prefill():
    levels = grid_sizing.build_levels(2000, 2100, 10, "arithmetic", digits=2)
    costs = grid_sizing.entry_costs(levels, is_long=True, prefill_enabled=False, price=2055)
    assert costs == [2000, 2010, 2020, 2030, 2040, 2050, 2060, 2070, 2080, 2090]


def test_entry_costs_use_market_price_for_prefilled_levels():
    """预填按市价成交：卖出线仍高于现价的格，成本是现价而不是格线价。"""
    levels = grid_sizing.build_levels(2000, 2100, 10, "arithmetic", digits=2)
    costs = grid_sizing.entry_costs(levels, is_long=True, prefill_enabled=True, price=2055)
    assert costs[:5] == [2000, 2010, 2020, 2030, 2040]
    assert costs[5:] == [2055] * 5

    # 空头对称：平仓线仍低于现价的格按现价开空，其余在自己的上沿线开
    short = grid_sizing.entry_costs(levels, is_long=False, prefill_enabled=True, price=2055)
    assert short[:6] == [2055] * 6
    assert short[6:] == [2070, 2080, 2090, 2100]


def test_loss_per_lot_ignores_levels_already_in_profit():
    spec = grid_sizing.SymbolSpec.from_probe(_SPEC)
    levels = grid_sizing.build_levels(2000, 2100, 10, "arithmetic", digits=2)
    costs = grid_sizing.entry_costs(levels, is_long=True, prefill_enabled=False, price=0)
    assert grid_sizing.loss_per_lot(costs, 1990, is_long=True, spec=spec) == pytest.approx(
        _FULL_LOSS_PER_LOT,
    )
    # 止损在所有成本之上时，多头各格都是盈利侧，不应折出负的风险
    assert grid_sizing.loss_per_lot(costs, 1990, is_long=False, spec=spec) == 0.0


def test_suggest_lot_floors_to_volume_step():
    spec = grid_sizing.SymbolSpec.from_probe(_SPEC)
    assert grid_sizing.suggest_lot(550, _FULL_LOSS_PER_LOT, spec) == 0.01
    assert grid_sizing.suggest_lot(5500, _FULL_LOSS_PER_LOT, spec) == 0.1
    # 1090 只够 0.019 手，向下取整到 0.01：宁可小于预算，不可超出
    assert grid_sizing.suggest_lot(1090, _FULL_LOSS_PER_LOT, spec) == 0.01
    assert grid_sizing.suggest_lot(0, _FULL_LOSS_PER_LOT, spec) == 0.0


# --------------------------- 完整试算 ---------------------------
def test_evaluate_sizes_grid_and_lot():
    out = _evaluate()
    assert out["ready"] is True
    assert out["atr"] == pytest.approx(10.0)
    assert out["spacing"] == pytest.approx(10.0)
    assert out["spacing_source"] == "atr"
    assert out["grid_count"] == 10
    assert out["lot_per_grid"] == 0.01
    assert out["worst_lot"] == pytest.approx(0.1)
    assert out["worst_loss"] == pytest.approx(550.0)
    assert len(out["levels"]) == 11


def test_evaluate_honours_manual_spacing():
    out = _evaluate(spacing=25.0, max_loss=10_000.0)
    assert out["spacing_source"] == "manual"
    assert out["spacing"] == pytest.approx(25.0)
    assert out["grid_count"] == 4


def test_evaluate_atr_mult_scales_spacing():
    out = _evaluate(atr_mult=2.0, max_loss=10_000.0)
    assert out["spacing"] == pytest.approx(20.0)
    assert out["grid_count"] == 5


def test_evaluate_rejects_invalid_range():
    out = _evaluate(price_upper=1900.0)
    assert out["ready"] is False
    assert "range_invalid" in _codes(out)


def test_evaluate_needs_bars_for_atr():
    out = _evaluate(bars=[])
    assert out["ready"] is False
    assert "atr_unavailable" in _codes(out)


def test_evaluate_without_spec_gives_grid_count_only():
    """老节点或读不到规格时只降级掉手数，格数照给。"""
    out = _evaluate(spec=None)
    assert out["ready"] is True
    assert out["grid_count"] == 10
    assert out["lot_per_grid"] == 0.0
    assert "spec_unavailable" in _codes(out)


def test_evaluate_without_stop_refuses_to_size():
    out = _evaluate(stop_lower=0.0)
    assert out["ready"] is True
    assert out["lot_per_grid"] == 0.0
    assert "stop_missing" in _codes(out)


def test_evaluate_short_side_uses_upper_stop():
    out = _evaluate(grid_side="short", stop_lower=0.0, stop_upper=2110.0, max_loss=10_000.0)
    assert out["lot_per_grid"] > 0
    assert "stop_missing" not in _codes(out)

    # 空头拿下沿价当止损是无效的：那是止盈侧
    out = _evaluate(grid_side="short", stop_lower=1990.0, stop_upper=0.0)
    assert out["lot_per_grid"] == 0.0
    assert "stop_missing" in _codes(out)


def test_evaluate_flags_lot_below_min():
    out = _evaluate(max_loss=100.0)
    assert out["lot_per_grid"] == 0.0
    assert "lot_below_min" in _codes(out)


def test_evaluate_flags_stop_inside_range():
    """止损填进区间内会被规范化清零，助手必须先喊住。"""
    out = _evaluate(stop_lower=2050.0)
    assert "stop_inside_range" in _codes(out)


def test_evaluate_flags_clamped_grid_count():
    out = _evaluate(spacing=0.05, max_loss=10_000.0)
    assert out["grid_count"] == 200
    assert "grid_count_clamped" in _codes(out)


def test_evaluate_flags_spacing_wider_than_range():
    out = _evaluate(spacing=200.0)
    assert out["ready"] is False
    assert "spacing_too_wide" in _codes(out)


def test_evaluate_flags_total_lot_limit_conflicts():
    low = _evaluate(max_loss=5500.0, total_lot_limit=0.05)
    assert low["lot_per_grid"] == pytest.approx(0.1)
    assert "total_lot_limit_low" in _codes(low)

    caps = _evaluate(max_loss=5500.0, total_lot_limit=0.5)
    assert "total_lot_limit_caps" in _codes(caps)


def test_evaluate_flags_risk_semantics():
    out = _evaluate(close_on_stop=False, trailing_up=True)
    codes = _codes(out)
    assert "close_on_stop_off" in codes
    assert "trailing_up_on" in codes
    assert "soft_stop" in codes


def test_evaluate_geometric_notes_uneven_spacing():
    out = _evaluate(grid_mode="geometric", max_loss=10_000.0)
    assert out["ready"] is True
    assert "geometric_spacing" in _codes(out)


def test_evaluate_prefill_uses_market_cost():
    """预填时最坏亏损按现价成本算，与不预填不是同一个数。"""
    plain = _evaluate(max_loss=5500.0)
    prefilled = _evaluate(max_loss=5500.0, prefill_enabled=True, price=2055.0)
    assert prefilled["worst_loss"] != plain["worst_loss"]
    assert "prefill_market_cost" in _codes(prefilled)


# --------------------------- 探针透传合约规格 ---------------------------
def test_probe_passes_spec_through(client, monkeypatch):
    h = _auth(client)
    node_id = _mk_node(client, h, 88401)
    _stub_node(monkeypatch)
    r = client.get(f"/api/nodes/{node_id}/grid_sizing",
                   params={"symbol": "XAUUSD", **_params()}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["lot_per_grid"] == 0.01


def test_probe_spec_defaults_to_empty(client, monkeypatch):
    """节点没带 spec 时不能报错——趋势风控走的是同一条探针链路。"""
    h = _auth(client)
    node_id = _mk_node(client, h, 88402)
    _stub_node(monkeypatch, spec=None)
    r = client.get(f"/api/nodes/{node_id}/grid_sizing",
                   params={"symbol": "XAUUSD", **_params()}, headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["grid_count"] == 10
    assert body["lot_per_grid"] == 0.0
    assert "spec_unavailable" in {w["code"] for w in body["warnings"]}


# ------------------------------ 接口 ------------------------------
def test_grid_sizing_requires_auth(client):
    r = client.get("/api/nodes/x/grid_sizing", params={"symbol": "XAUUSD"})
    assert r.status_code == 401


def test_grid_sizing_unknown_node(client):
    h = _auth(client)
    r = client.get("/api/nodes/nope/grid_sizing", params={"symbol": "XAUUSD"}, headers=h)
    assert r.status_code == 404


def test_grid_sizing_rejects_invalid_symbol(client):
    h = _auth(client)
    node_id = _mk_node(client, h, 88403)
    r = client.get(f"/api/nodes/{node_id}/grid_sizing",
                   params={"symbol": "xau usd!"}, headers=h)
    assert r.status_code == 400


def test_grid_sizing_offline_node_returns_conflict(client, monkeypatch):
    h = _auth(client)
    node_id = _mk_node(client, h, 88404)
    _stub_node(monkeypatch, online=False)
    r = client.get(f"/api/nodes/{node_id}/grid_sizing",
                   params={"symbol": "XAUUSD", **_params()}, headers=h)
    assert r.status_code == 409
    assert "离线" in r.json()["detail"]


def test_grid_sizing_asks_node_for_atr_bars(client, monkeypatch):
    h = _auth(client)
    node_id = _mk_node(client, h, 88405)
    sent = _stub_node(monkeypatch)
    r = client.get(
        f"/api/nodes/{node_id}/grid_sizing",
        params={"symbol": "xauusd", "timeframe": "H4", **_params()},
        headers=h,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["symbol"] == "XAUUSD"
    assert body["timeframe"] == "H4"
    assert sent[0]["cmd"] == "market_probe"
    assert sent[0]["timeframe"] == "H4"
    assert sent[0]["count"] == grid_sizing.bars_needed()


def test_grid_sizing_falls_back_to_default_timeframe(client, monkeypatch):
    h = _auth(client)
    node_id = _mk_node(client, h, 88406)
    sent = _stub_node(monkeypatch)
    r = client.get(
        f"/api/nodes/{node_id}/grid_sizing",
        params={"symbol": "XAUUSD", "timeframe": "X9", **_params()},
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert r.json()["timeframe"] == grid_sizing.DEFAULT_TIMEFRAME
    assert sent[0]["timeframe"] == grid_sizing.DEFAULT_TIMEFRAME
