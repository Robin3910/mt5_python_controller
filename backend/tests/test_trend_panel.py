"""趋势面板：指标纯函数、节点行情探针与 /api/nodes/{id}/trend 接口。"""
import pathlib

import fakeredis
import pytest
from fastapi.testclient import TestClient

from app import market_probe, trend_indicators
from app.connections import manager
from app.redis_store import RedisStore
from tests.test_helpers import drop_test_db, reset_test_db

_TEST_DB = pathlib.Path(__file__).resolve().parent / "_test_api.db"


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


def _bars(closes: list[float], *, span: float = 0.5) -> list[dict]:
    """按收盘价序列造 K 线（高低价对称包住收盘价）。"""
    return [
        {"time": 1_700_000_000 + i * 900, "open": c, "high": c + span, "low": c - span, "close": c}
        for i, c in enumerate(closes)
    ]


def _rising(count: int = 120, start: float = 100.0, step: float = 1.0) -> list[float]:
    return [start + i * step for i in range(count)]


def _stub_node(monkeypatch, *, bars=None, quote=None, error=None, online=True) -> list[dict]:
    """用回包替代真实节点：send_to_node 一被调用就以节点身份 resolve。

    返回收集到的下发命令列表，便于断言探针参数。
    """
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
            payload["bars"] = bars or []
            payload["quote"] = quote or {}
        market_probe.resolve(payload["req_id"], payload)
        return True

    monkeypatch.setattr(manager, "send_to_node", fake_send)
    return sent


def _mk_node(client, headers, mt5_login: int) -> str:
    r = client.post("/api/nodes", json={"name": f"trend-{mt5_login}", "mt5_login": mt5_login},
                    headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["node_id"]


# --------------------------- 配置规范化 ---------------------------
def test_normalize_defaults():
    cfg = trend_indicators.normalize_config(None)
    assert cfg["timeframe"] == "M15"
    assert cfg["ema_period"] == 50
    assert cfg["rsi_period"] == 14
    assert cfg["ema_weight"] == 0.7
    assert cfg["rsi_weight"] == 0.3
    assert cfg["rsi_overbought"] == 70.0
    assert cfg["rsi_oversold"] == 30.0
    assert cfg["bullish"] == 20.0
    assert cfg["bearish"] == -20.0


def test_normalize_clamps_out_of_range():
    cfg = trend_indicators.normalize_config({
        "timeframe": "m30",
        "ema_period": 9999,
        "rsi_period": 0,
        "bars": 5,
        "ema_full_scale_pct": 0,
    })
    assert cfg["timeframe"] == "M30"
    assert cfg["ema_period"] == 400
    assert cfg["rsi_period"] == 2
    assert cfg["bars"] == trend_indicators.MIN_VIEW_BARS
    assert cfg["ema_full_scale_pct"] == 0.01


def test_normalize_rejects_unknown_timeframe():
    assert trend_indicators.normalize_config({"timeframe": "M7"})["timeframe"] == "M15"


def test_normalize_scales_weights_to_one():
    cfg = trend_indicators.normalize_config({"ema_weight": 0.8, "rsi_weight": 0.8})
    assert cfg["ema_weight"] == 0.5
    assert cfg["rsi_weight"] == 0.5
    assert cfg["ema_weight"] + cfg["rsi_weight"] == 1.0


def test_normalize_falls_back_when_weights_are_zero():
    cfg = trend_indicators.normalize_config({"ema_weight": 0, "rsi_weight": 0})
    assert cfg["ema_weight"] == 0.7
    assert cfg["rsi_weight"] == 0.3


def test_normalize_fixes_rsi_line_relations():
    """偏空线不得压到偏多线之上，超买超卖也不得反落在偏多偏空线内侧。"""
    cfg = trend_indicators.normalize_config({
        "rsi_bull": 55, "rsi_bear": 50, "rsi_overbought": 50, "rsi_oversold": 90,
    })
    assert cfg["rsi_bear"] < cfg["rsi_bull"]
    assert cfg["rsi_overbought"] >= cfg["rsi_bull"]
    assert cfg["rsi_oversold"] <= cfg["rsi_bear"]


def test_bars_needed_quantized_and_capped():
    need = trend_indicators.bars_needed(trend_indicators.normalize_config(None))
    assert need % trend_indicators.BARS_QUANTUM == 0
    assert need >= 200  # EMA(50) 至少要 4 倍周期才收敛
    big = trend_indicators.bars_needed(trend_indicators.normalize_config({"ema_period": 400}))
    assert big == trend_indicators.MAX_BARS


def test_bars_needed_caps_high_timeframes():
    """月线/周线不能按分钟线那套去要几百根，否则终端补历史会卡死节点。"""
    mn = trend_indicators.bars_needed(trend_indicators.normalize_config({"timeframe": "MN"}))
    assert mn == trend_indicators.TIMEFRAME_BAR_CAPS["MN"]
    assert mn >= int(trend_indicators.DEFAULTS["ema_period"])  # 仍够算出默认 EMA
    w1 = trend_indicators.bars_needed(trend_indicators.normalize_config({"timeframe": "W1"}))
    assert w1 == trend_indicators.TIMEFRAME_BAR_CAPS["W1"]
    # 低周期不受该上限影响（仍走量子化后的常规值）
    m15 = trend_indicators.bars_needed(trend_indicators.normalize_config({"timeframe": "M15"}))
    assert m15 >= 200


# ------------------------------ EMA ------------------------------
def test_ema_series_matches_manual_recursion():
    series = trend_indicators.ema_series([1.0, 2.0, 3.0, 4.0], 3)
    assert series[0] is None and series[1] is None
    assert series[2] == pytest.approx(2.0)  # 种子 = 前 3 根均值
    assert series[3] == pytest.approx(3.0)  # 4*0.5 + 2*0.5


def test_ema_series_none_when_bars_insufficient():
    assert trend_indicators.ema_series([1.0, 2.0], 5) == [None, None]


def test_ema_score_sign_follows_price_position():
    assert trend_indicators.ema_raw_score(100.5, 100.0, 1.0) == pytest.approx(50.0)
    assert trend_indicators.ema_raw_score(99.5, 100.0, 1.0) == pytest.approx(-50.0)
    assert trend_indicators.ema_raw_score(100.0, 100.0, 1.0) == pytest.approx(0.0)


def test_ema_score_caps_at_full_scale():
    assert trend_indicators.ema_raw_score(150.0, 100.0, 0.2) == pytest.approx(100.0)
    assert trend_indicators.ema_raw_score(50.0, 100.0, 0.2) == pytest.approx(-100.0)


# ------------------------------ RSI ------------------------------
def test_rsi_all_gains_is_100():
    series = trend_indicators.rsi_series([float(i) for i in range(1, 20)], 14)
    assert series[-1] == pytest.approx(100.0)


def test_rsi_all_losses_is_zero():
    series = trend_indicators.rsi_series([float(20 - i) for i in range(19)], 14)
    assert series[-1] == pytest.approx(0.0)


def test_rsi_flat_market_is_fifty():
    """横盘（无涨也无跌）多空力量相等，必须报 50 而不是满分。"""
    series = trend_indicators.rsi_series([100.0] * 20, 14)
    assert series[-1] == pytest.approx(50.0)


def test_rsi_ratio_two_to_one():
    # period=2：涨 2 跌 1 -> RS=2 -> RSI=66.67
    series = trend_indicators.rsi_series([10.0, 12.0, 11.0], 2)
    assert series[2] == pytest.approx(100.0 - 100.0 / 3.0, abs=1e-6)


def test_rsi_series_none_when_bars_insufficient():
    assert trend_indicators.rsi_series([1.0, 2.0], 14) == [None, None]


def test_rsi_score_zero_inside_neutral_zone():
    for value in (40.0, 45.0, 50.0, 55.0, 60.0):
        assert trend_indicators.rsi_raw_score(value, 60.0, 40.0) == pytest.approx(0.0)


def test_rsi_score_scales_outside_neutral_zone():
    assert trend_indicators.rsi_raw_score(80.0, 60.0, 40.0) == pytest.approx(50.0)
    assert trend_indicators.rsi_raw_score(100.0, 60.0, 40.0) == pytest.approx(100.0)
    assert trend_indicators.rsi_raw_score(20.0, 60.0, 40.0) == pytest.approx(-50.0)
    assert trend_indicators.rsi_raw_score(0.0, 60.0, 40.0) == pytest.approx(-100.0)


def test_rsi_state_zones():
    cfg = trend_indicators.normalize_config(None)
    assert trend_indicators.rsi_state(75.0, cfg) == trend_indicators.RSI_OVERBOUGHT
    assert trend_indicators.rsi_state(25.0, cfg) == trend_indicators.RSI_OVERSOLD
    assert trend_indicators.rsi_state(65.0, cfg) == trend_indicators.TREND_BULLISH
    assert trend_indicators.rsi_state(35.0, cfg) == trend_indicators.TREND_BEARISH
    assert trend_indicators.rsi_state(50.0, cfg) == trend_indicators.TREND_NEUTRAL


# --------------------------- 综合得分 ---------------------------
def test_classify_uses_thresholds_exclusively():
    cfg = trend_indicators.normalize_config(None)
    assert trend_indicators.classify(20.0, cfg) == trend_indicators.TREND_NEUTRAL
    assert trend_indicators.classify(20.1, cfg) == trend_indicators.TREND_BULLISH
    assert trend_indicators.classify(-20.0, cfg) == trend_indicators.TREND_NEUTRAL
    assert trend_indicators.classify(-20.1, cfg) == trend_indicators.TREND_BEARISH


def test_evaluate_weighted_scores_sum_to_total():
    cfg = trend_indicators.normalize_config(None)
    res = trend_indicators.evaluate(_bars(_rising()), 220.0, cfg)
    assert res["ready"] is True
    assert res["ema"]["max_score"] == 70.0
    assert res["rsi"]["max_score"] == 30.0
    assert res["score"] == pytest.approx(res["ema"]["score"] + res["rsi"]["score"])
    # 单边上涨且远离 EMA：两项都打满 -> 极端偏多
    assert res["score"] == pytest.approx(100.0)
    assert res["trend"] == trend_indicators.TREND_BULLISH
    assert res["ema"]["position"] == "above"


def test_evaluate_bearish_on_falling_market():
    cfg = trend_indicators.normalize_config(None)
    closes = [220.0 - i for i in range(120)]
    res = trend_indicators.evaluate(_bars(closes), 99.0, cfg)
    assert res["trend"] == trend_indicators.TREND_BEARISH
    assert res["score"] == pytest.approx(-100.0)
    assert res["ema"]["position"] == "below"


def test_evaluate_flat_market_is_neutral():
    cfg = trend_indicators.normalize_config(None)
    res = trend_indicators.evaluate(_bars([100.0] * 120), 100.0, cfg)
    assert res["ready"] is True
    assert res["rsi"]["value"] == pytest.approx(50.0)
    assert res["score"] == pytest.approx(0.0)
    assert res["trend"] == trend_indicators.TREND_NEUTRAL


def test_evaluate_reports_unknown_when_bars_insufficient():
    """K 线不够时必须区分「算不出」与「中性」。"""
    cfg = trend_indicators.normalize_config(None)
    res = trend_indicators.evaluate(_bars(_rising(10)), 110.0, cfg)
    assert res["ready"] is False
    assert res["trend"] == trend_indicators.TREND_UNKNOWN
    assert res["ema"]["value"] is None
    assert res["score"] == 0.0


def test_evaluate_falls_back_to_last_close_without_quote():
    cfg = trend_indicators.normalize_config(None)
    res = trend_indicators.evaluate(_bars(_rising()), 0.0, cfg)
    assert res["price_source"] == "close"
    assert res["price"] == pytest.approx(219.0)


def test_evaluate_trims_series_to_view_window():
    cfg = trend_indicators.normalize_config({"bars": 30})
    res = trend_indicators.evaluate(_bars(_rising(200)), 300.0, cfg)
    assert len(res["bars"]) == 30
    assert len(res["ema_series"]) == 30
    assert len(res["rsi_series"]) == 30
    assert res["bar_count"] == 200  # 计算仍用全量


# --------------------------- 行情探针 ---------------------------
def test_quote_price_prefers_mid():
    assert market_probe.quote_price({"mid": 10.0, "bid": 9.0}) == 10.0
    assert market_probe.quote_price({"bid": 9.0, "ask": 11.0}) == 9.0
    assert market_probe.quote_price({}) == 0.0
    assert market_probe.quote_price(None) == 0.0


# ------------------------------ 接口 ------------------------------
def test_trend_requires_auth(client):
    assert client.get("/api/nodes/x/trend", params={"symbol": "XAUUSD"}).status_code == 401


def test_trend_unknown_node(client):
    h = _auth(client)
    r = client.get("/api/nodes/nope/trend", params={"symbol": "XAUUSD"}, headers=h)
    assert r.status_code == 404


def test_trend_rejects_invalid_symbol(client):
    h = _auth(client)
    node_id = _mk_node(client, h, 88301)
    r = client.get(f"/api/nodes/{node_id}/trend", params={"symbol": "xau usd!"}, headers=h)
    assert r.status_code == 400


def test_trend_offline_node_returns_conflict(client, monkeypatch):
    h = _auth(client)
    node_id = _mk_node(client, h, 88302)
    _stub_node(monkeypatch, online=False)
    r = client.get(f"/api/nodes/{node_id}/trend", params={"symbol": "XAUUSD"}, headers=h)
    assert r.status_code == 409
    assert "离线" in r.json()["detail"]


def test_trend_surfaces_node_error(client, monkeypatch):
    h = _auth(client)
    node_id = _mk_node(client, h, 88303)
    _stub_node(monkeypatch, error="XAUUSD M15 无可用 K 线（品种不存在）")
    r = client.get(f"/api/nodes/{node_id}/trend", params={"symbol": "XAUUSD"}, headers=h)
    assert r.status_code == 409
    assert "无可用 K 线" in r.json()["detail"]


def test_trend_returns_score_and_series(client, monkeypatch):
    h = _auth(client)
    node_id = _mk_node(client, h, 88304)
    sent = _stub_node(
        monkeypatch, bars=_bars(_rising()), quote={"bid": 219.8, "ask": 220.2, "mid": 220.0},
    )
    r = client.get(f"/api/nodes/{node_id}/trend", params={"symbol": "xauusd"}, headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["symbol"] == "XAUUSD"
    assert body["ready"] is True
    assert body["trend"] == "bullish"
    assert body["score"] == pytest.approx(100.0)
    assert body["price"] == pytest.approx(220.0)
    assert body["price_source"] == "quote"
    assert body["ema"]["period"] == 50
    assert body["rsi"]["value"] == pytest.approx(100.0)
    assert len(body["bars"]) == body["config"]["bars"]
    assert len(body["ema_series"]) == len(body["bars"])
    assert sent[0]["cmd"] == "market_probe"
    assert sent[0]["timeframe"] == "M15"
    assert sent[0]["count"] == trend_indicators.bars_needed(body["config"])


def test_trend_query_overrides_saved_config(client, monkeypatch):
    h = _auth(client)
    node_id = _mk_node(client, h, 88305)
    sent = _stub_node(monkeypatch, bars=_bars(_rising(200)), quote={"mid": 300.0})
    r = client.get(
        f"/api/nodes/{node_id}/trend",
        params={"symbol": "XAUUSD", "timeframe": "H1", "ema_period": 20, "bars": 40},
        headers=h,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["config"]["timeframe"] == "H1"
    assert body["config"]["ema_period"] == 20
    assert body["config"]["bars"] == 40
    assert len(body["bars"]) == 40
    assert sent[0]["timeframe"] == "H1"


def test_trend_reuses_market_data_within_window(client, monkeypatch):
    """面板会持续轮询，同一取数窗口内不该反复惊动节点终端。"""
    h = _auth(client)
    node_id = _mk_node(client, h, 88306)
    sent = _stub_node(monkeypatch, bars=_bars(_rising()), quote={"mid": 220.0})
    params = {"symbol": "XAUUSD"}
    first = client.get(f"/api/nodes/{node_id}/trend", params=params, headers=h)
    second = client.get(f"/api/nodes/{node_id}/trend", params=params, headers=h)
    assert first.json()["cached"] is False
    assert second.json()["cached"] is True
    assert len(sent) == 1


def test_trend_config_change_does_not_refetch(client, monkeypatch):
    """只调权重阈值时 K 线取数不变，应直接命中缓存并给出新得分。"""
    h = _auth(client)
    node_id = _mk_node(client, h, 88307)
    # 缓步上行：偏离 EMA 的幅度落在满分线可调区间内，否则得分早已打满、改参数看不出差别
    closes = _rising(120, 100.0, 0.002)
    sent = _stub_node(monkeypatch, bars=_bars(closes, span=0.01), quote={"mid": closes[-1]})
    base = client.get(f"/api/nodes/{node_id}/trend", params={"symbol": "XAUUSD"}, headers=h)
    tuned = client.get(
        f"/api/nodes/{node_id}/trend",
        params={"symbol": "XAUUSD", "ema_full_scale_pct": 0.4},
        headers=h,
    )
    assert len(sent) == 1
    assert tuned.json()["cached"] is True
    # 满分线放宽一倍：同一偏离换算出的 EMA 得分应随之减半
    assert 0 < tuned.json()["ema"]["score"] < base.json()["ema"]["score"]
    assert tuned.json()["score"] < base.json()["score"]


# --------------------------- 参数持久化 ---------------------------
def test_patch_node_trend_persists(client):
    h = _auth(client)
    node_id = _mk_node(client, h, 88308)
    r = client.patch(
        f"/api/nodes/{node_id}",
        json={"trend": {"timeframe": "H1", "ema_period": 30, "ema_weight": 0.6,
                        "rsi_weight": 0.4, "bullish": 25, "bearish": -25}},
        headers=h,
    )
    assert r.status_code == 200, r.text
    trend = r.json()["trend"]
    assert trend["timeframe"] == "H1"
    assert trend["ema_period"] == 30
    assert trend["ema_weight"] == 0.6
    assert trend["rsi_weight"] == 0.4
    assert trend["bullish"] == 25.0

    again = client.get(f"/api/nodes/{node_id}", headers=h)
    assert again.json()["trend"]["ema_period"] == 30


def test_patch_node_trend_clamps_instead_of_failing(client):
    h = _auth(client)
    node_id = _mk_node(client, h, 88309)
    r = client.patch(
        f"/api/nodes/{node_id}", json={"trend": {"ema_period": 9999, "bars": 1}}, headers=h,
    )
    assert r.status_code == 200, r.text
    assert r.json()["trend"]["ema_period"] == 400
    assert r.json()["trend"]["bars"] == trend_indicators.MIN_VIEW_BARS


def test_new_node_carries_default_trend_config(client):
    h = _auth(client)
    node_id = _mk_node(client, h, 88310)
    r = client.get(f"/api/nodes/{node_id}", headers=h)
    assert r.json()["trend"] == trend_indicators.normalize_config(None)


def test_saved_trend_config_applies_without_query(client, monkeypatch):
    h = _auth(client)
    node_id = _mk_node(client, h, 88311)
    client.patch(f"/api/nodes/{node_id}", json={"trend": {"timeframe": "H4", "bars": 50}},
                 headers=h)
    sent = _stub_node(monkeypatch, bars=_bars(_rising()), quote={"mid": 220.0})
    r = client.get(f"/api/nodes/{node_id}/trend", params={"symbol": "XAUUSD"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["config"]["timeframe"] == "H4"
    assert sent[0]["timeframe"] == "H4"
