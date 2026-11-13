"""信号中心的**路由中枢**与**双模式分发**端到端用例。

覆盖四件最容易出错的事：

1. **direct / center 双模式**：分组切到 `center` 后，TV 告警**不再**自动开仓，
   信号改为进入信号中心待路由队列（`route_state='pending'`）；
2. **中心下发**：管理员在信号中心选中信号 + 分组后，信号才会真正下发；
3. **重复路由冲突**：同一信号 + 同一分组已有未收口任务 → 409；
4. **幂等指纹带结构类型**：同参数但不同 `signal_type` 的信号**不能**互相吞掉
   （否则同根 K 上的第二个结构点连信号中心都进不去，`signal_type` 胜率会失真）。
"""
import contextlib
import pathlib

import fakeredis
import pytest
from fastapi.testclient import TestClient

from app.redis_store import RedisStore
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


@contextlib.contextmanager
def _live_group(client, headers, *, name: str, mt5_login: int = 6101, **body):
    """建一个**带在线节点**的分组。

    分组里没有在线节点时，主任务会立刻收口成 `skipped`（没有可下发的对象），
    那样既测不到「任务进行中」，也测不到重复路由的 409。���以这里起一个
    真实的节点 WebSocket，让任务真正跑起来。
    """
    token = client.get("/api/config/node-token", headers=headers).json()["token"]
    node = client.post(
        "/api/nodes", json={"name": f"节点-{mt5_login}", "mt5_login": mt5_login},
        headers=headers,
    )
    assert node.status_code == 201, node.text
    node_id = node.json()["node_id"]

    group = _mk_group(client, headers, name=name, node_ids=[node_id], **body)
    with client.websocket_connect("/ws/node") as ws:
        ws.send_json({"type": "auth", "data": {"token": token, "mt5_login": mt5_login}})
        assert ws.receive_json()["type"] == "auth_ok"
        yield group, ws


def _tv_signal(symbol: str = "XAUUSD", **extra) -> dict:
    """一条 TV 结构点告警（model=strategy 走分组链路）。"""
    payload = {
        "action": "buy", "symbol": symbol, "volume": 0.1,
        "model": "strategy", "template_ids": ["tpl_1"],
        "tf": "M15", "signal_type": "ob",
    }
    payload.update(extra)
    return payload


# ---------------------------------------------------------------------------
# 1. 分组信号来源模式：direct（默认）
# ---------------------------------------------------------------------------
def test_group_signal_source_defaults_to_direct(client):
    """存量分组零行为变化：默认 direct。"""
    h = auth_headers(client)
    g = _mk_group(client, h, name="默认直连组")
    assert g["signal_source"] == "direct"
    assert client.get(f"/api/groups/{g['group_id']}", headers=h).json()["signal_source"] == "direct"


def test_group_signal_source_can_be_switched_to_center(client):
    """分组可以切到 center：只接受信号中心下发。"""
    h = auth_headers(client)
    g = _mk_group(client, h, name="可切中心组")
    gid = g["group_id"]

    r = client.patch(f"/api/groups/{gid}", json={"signal_source": "center"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["signal_source"] == "center"

    r = client.patch(f"/api/groups/{gid}", json={"signal_source": "bogus"}, headers=h)
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# 2. direct 模式下 center 分组不自动开仓
# ---------------------------------------------------------------------------
def test_center_group_does_not_auto_dispatch_and_signal_stays_pending(client):
    """center 分组不消费 TV 告警 → 信号入库为 pending，等管理员路由。"""
    h = auth_headers(client)
    seed_default_filters(client)
    with _live_group(client, h, name="中心待路由组") as (g, _ws):
        gid = g["group_id"]
        client.patch(f"/api/groups/{gid}", json={"signal_source": "center"}, headers=h)

        r = client.post("/webhook", json=_tv_signal())
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "pending"
        assert body["mode"] == "pending"
        assert body["route_state"] == "pending"
        assert body["signal_id"]

        # 该信号确实进了信号中心
        stream = client.get("/api/signal-center/stream", headers=h).json()
        item = next(x for x in stream["items"] if x["signal_id"] == body["signal_id"])
        assert item["route_state"] == "pending"
        assert item["timeframe"] == "M15"
        assert item["signal_type"] == "ob"


def test_direct_group_still_auto_dispatches_alongside_center_group(client):
    """直连分组照旧自动下发；center 分组的那条信号仍留在待路由队列。"""
    h = auth_headers(client)
    seed_default_filters(client)
    with _live_group(client, h, name="照旧直连组", mt5_login=6201) as (direct, _ws1):
        # 第二个分组复用同一个策略会违反「一对一」绑定，这里另起一条策略
        sty = _mk_strategy(client, h, name="中心组策略")
        center = _mk_group(
            client, h, name="同品种中心组", strategy_id=sty["strategy_id"],
        )
        client.patch(
            f"/api/groups/{center['group_id']}", json={"signal_source": "center"}, headers=h,
        )

        r = client.post("/webhook", json=_tv_signal())
        assert r.status_code == 200, r.text
        body = r.json()
        # 有直连分组消费 → 不进 pending 分支
        assert body["status"] == "accepted"
        assert body["mode"] == "group"
        # 但 route_state 仍是 pending（center 分组还在等着）
        assert body["route_state"] == "pending"
        assert body["pending_groups"] == [center["group_id"]]
        assert direct["group_id"] != center["group_id"]


# ---------------------------------------------------------------------------
# 3. 中心下发（管理员路由）
# ---------------------------------------------------------------------------
def test_route_pending_signal_to_center_group(client):
    """管理员在信号中心把待路由信号下发给 center 分组。"""
    h = auth_headers(client)
    seed_default_filters(client)
    with _live_group(client, h, name="待下发中心组") as (g, _ws):
        gid = g["group_id"]
        client.patch(f"/api/groups/{gid}", json={"signal_source": "center"}, headers=h)

        pending = client.post("/webhook", json=_tv_signal()).json()
        assert pending["status"] == "pending"

        r = client.post(
            f"/api/signal-center/signals/{pending['signal_id']}/route",
            json={"group_ids": [gid]}, headers=h,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["signal_id"] == pending["signal_id"]
        assert body["route_state"] == "routed"
        assert body["groups"] == 1

        # 路由后信号状态变成 routed
        stream = client.get("/api/signal-center/stream", headers=h).json()
        item = next(x for x in stream["items"] if x["signal_id"] == pending["signal_id"])
        assert item["route_state"] == "routed"


def test_route_allows_overriding_volume_sl_tp(client):
    """路由可以覆盖手数 / 止损 / 止盈。"""
    h = auth_headers(client)
    seed_default_filters(client)
    with _live_group(client, h, name="覆盖参数组") as (g, _ws):
        gid = g["group_id"]
        client.patch(f"/api/groups/{gid}", json={"signal_source": "center"}, headers=h)

        pending = client.post("/webhook", json=_tv_signal()).json()
        r = client.post(
            f"/api/signal-center/signals/{pending['signal_id']}/route",
            json={"group_ids": [gid], "volume": 0.5, "sl": 2400.0, "tp": 2600.0},
            headers=h,
        )
        assert r.status_code == 200, r.text
        # 任务确实按覆盖后的参数建了
        tasks = client.get(f"/api/groups/{gid}/signals", headers=h).json()
        assert tasks["items"], "路由后应产生主任务"
        task = tasks["items"][0]
        assert task["volume"] == 0.5
        assert task["sl"] == 2400.0
        assert task["tp"] == 2600.0


def test_route_duplicate_active_task_returns_409(client):
    """同一信号重复路由到同一分组 → 409（已有未收口任务）。"""
    h = auth_headers(client)
    seed_default_filters(client)
    with _live_group(client, h, name="重复路由组") as (g, _ws):
        gid = g["group_id"]
        client.patch(f"/api/groups/{gid}", json={"signal_source": "center"}, headers=h)

        pending = client.post("/webhook", json=_tv_signal()).json()
        first = client.post(
            f"/api/signal-center/signals/{pending['signal_id']}/route",
            json={"group_ids": [gid]}, headers=h,
        )
        assert first.status_code == 200, first.text

        second = client.post(
            f"/api/signal-center/signals/{pending['signal_id']}/route",
            json={"group_ids": [gid]}, headers=h,
        )
        assert second.status_code == 409, second.text
        detail = second.json()["detail"]
        assert "进行中" in detail["message"]
        assert detail["task_ids"]


def test_route_unknown_signal_returns_400(client):
    h = auth_headers(client)
    r = client.post(
        "/api/signal-center/signals/sig_nope/route",
        json={"group_ids": ["grp_x"]}, headers=h,
    )
    assert r.status_code == 400
    assert "不存在" in r.json()["detail"]


def test_route_without_group_ids_returns_400(client):
    h = auth_headers(client)
    seed_default_filters(client)
    pending = client.post("/webhook", json=_tv_signal()).json()
    r = client.post(
        f"/api/signal-center/signals/{pending['signal_id']}/route",
        json={"group_ids": []}, headers=h,
    )
    assert r.status_code == 400


def test_drop_pending_signal_marks_dropped(client):
    """丢弃待路由信号：只改路由状态，不动已建任务。"""
    h = auth_headers(client)
    seed_default_filters(client)
    with _live_group(client, h, name="待丢弃组") as (g, _ws):
        gid = g["group_id"]
        client.patch(f"/api/groups/{gid}", json={"signal_source": "center"}, headers=h)

        pending = client.post("/webhook", json=_tv_signal()).json()
        r = client.post(
            f"/api/signal-center/signals/{pending['signal_id']}/drop",
            json={"note": "结构不成立"}, headers=h,
        )
        assert r.status_code == 200, r.text
        assert r.json()["route_state"] == "dropped"

        stream = client.get("/api/signal-center/stream", headers=h).json()
        item = next(x for x in stream["items"] if x["signal_id"] == pending["signal_id"])
        assert item["route_state"] == "dropped"


def test_batch_route_reports_per_signal_results(client):
    """批量路由：逐条独立处理，一条失败不影响其余。"""
    h = auth_headers(client)
    seed_default_filters(client)
    with _live_group(client, h, name="批量路由组") as (g, _ws):
        gid = g["group_id"]
        client.patch(f"/api/groups/{gid}", json={"signal_source": "center"}, headers=h)

        p1 = client.post("/webhook", json=_tv_signal(signal_type="ob")).json()
        p2 = client.post("/webhook", json=_tv_signal(signal_type="fvg")).json()

        r = client.post(
            "/api/signal-center/signals/batch-route",
            json={"signal_ids": [p1["signal_id"], p2["signal_id"], "sig_nope"],
                  "group_ids": [gid]},
            headers=h,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["summary"]["ok"] == 2
        assert body["summary"]["failed"] == 1
        assert any(x["signal_id"] == "sig_nope" for x in body["failed"])


# ---------------------------------------------------------------------------
# 4. 幂等指纹必须区分结构类型
# ---------------------------------------------------------------------------
def test_dedup_distinguishes_signal_type(client):
    """**同参数但不同 signal_type 的信号不能互相吞掉**。

    Pine 侧 sl/tp 是按 ATR 算的，同一根 K 上「OB 回踩」与「FVG 出现」
    可能算出完全相同的 sl/tp —— 不带结构类型区分，第二条会被静默吞掉，
    那个 signal_type 的胜率就永远统计不到（统计失真）。
    """
    h = auth_headers(client)
    seed_default_filters(client)
    with _live_group(client, h, name="去重类型组") as (_g, _ws):
        base = {
            "action": "buy", "symbol": "XAUUSD", "volume": 0.1,
            "model": "strategy", "template_ids": ["tpl_1"],
            "tf": "M15", "sl": 2400.0, "tp": 2500.0,
        }
        ob = client.post("/webhook", json={**base, "signal_type": "ob"}).json()
        assert ob["status"] == "accepted"

        fvg = client.post("/webhook", json={**base, "signal_type": "fvg"}).json()
        assert fvg["status"] == "accepted", "不同结构类型不应被判为重复"
        assert fvg["signal_id"] != ob["signal_id"]


def test_dedup_distinguishes_timeframe(client):
    """同结构类型但不同周期 → 也不该互相吞掉。"""
    h = auth_headers(client)
    seed_default_filters(client)
    with _live_group(client, h, name="去重周期组", mt5_login=6202) as (_g, _ws):
        base = {
            "action": "buy", "symbol": "XAUUSD", "volume": 0.1,
            "model": "strategy", "template_ids": ["tpl_1"],
            "signal_type": "ob", "sl": 2400.0, "tp": 2500.0,
        }
        m15 = client.post("/webhook", json={**base, "tf": "M15"}).json()
        assert m15["status"] == "accepted"

        h1 = client.post("/webhook", json={**base, "tf": "H1"}).json()
        assert h1["status"] == "accepted", "不同周期不应被判为重复"


def test_dedup_still_suppresses_identical_structured_signal(client):
    """同类型同周期同参数连发 → **仍然**被幂等吞掉（本就该是重复）。"""
    h = auth_headers(client)
    seed_default_filters(client)
    with _live_group(client, h, name="去重同参组", mt5_login=6203) as (_g, _ws):
        payload = {
            "action": "buy", "symbol": "XAUUSD", "volume": 0.1,
            "model": "strategy", "template_ids": ["tpl_1"],
            "tf": "M15", "signal_type": "ob", "sl": 2400.0, "tp": 2500.0,
        }
        first = client.post("/webhook", json=payload).json()
        assert first["status"] == "accepted"
        dup = client.post("/webhook", json=payload).json()
        assert dup["status"] == "duplicate"


def test_dedup_of_legacy_signal_without_type_is_unchanged(client):
    """**不带 tf / signal_type 的老告警行为逐字节不变**。

    存量 TV 告警（纯 action/symbol/volume）去重语义必须与改造前一致 ——
    指纹里这两个字段为空串，等价于没加。
    """
    h = auth_headers(client)
    seed_default_filters(client)
    with _live_group(client, h, name="去重老告警组", mt5_login=6204) as (_g, _ws):
        payload = {"action": "buy", "symbol": "XAUUSD", "volume": 0.1}
        first = client.post("/webhook", json=payload).json()
        assert first["status"] == "accepted"
        dup = client.post("/webhook", json=payload).json()
        assert dup["status"] == "duplicate"

        # 带类型的老告警不会与不带类型的老告警互相吞掉（新增维度生效）
        typed = client.post("/webhook", json={**payload, "signal_type": "ob"}).json()
        assert typed["status"] == "accepted"


# ---------------------------------------------------------------------------
# 5. 统计维度的落库
# ---------------------------------------------------------------------------
def test_signal_timeframe_and_type_are_persisted(client):
    """周期与信号类型落库到 signal_history（纯统计维度，缺失就是 NULL）。"""
    h = auth_headers(client)
    seed_default_filters(client)
    with _live_group(client, h, name="落库校验组", mt5_login=6205) as (_g, _ws):
        r = client.post("/webhook", json=_tv_signal(tf="60", signal_type="fvg"))
        assert r.status_code == 200, r.text

        stream = client.get("/api/signal-center/stream", headers=h).json()
        item = stream["items"][0]
        assert item["timeframe"] == "H1", "60 应归一成 H1"
        assert item["signal_type"] == "fvg"


def test_signal_without_tf_or_type_is_accepted_and_null(client):
    """**缺周期 / 缺类型照样受理**（绝不 400），落库为 NULL。"""
    h = auth_headers(client)
    seed_default_filters(client)
    with _live_group(client, h, name="无元信息组", mt5_login=6206) as (_g, _ws):
        r = client.post("/webhook", json={
            "action": "buy", "symbol": "XAUUSD", "volume": 0.1,
            "model": "strategy", "template_ids": ["tpl_1"],
        })
        assert r.status_code == 200, r.text
        assert r.json()["status"] in ("accepted", "pending")

        stream = client.get("/api/signal-center/stream", headers=h).json()
        item = stream["items"][0]
        assert item["timeframe"] is None
        assert item["signal_type"] is None


def test_invalid_tf_and_type_do_not_reject_signal(client):
    """非法周期 / 未知类型：存归一后的值或原值，**绝不拒收**。"""
    h = auth_headers(client)
    seed_default_filters(client)
    with _live_group(client, h, name="非法元信息组", mt5_login=6207) as (_g, _ws):
        r = client.post("/webhook", json=_tv_signal(tf="M7", signal_type="MyNewType"))
        assert r.status_code == 200, r.text

        stream = client.get("/api/signal-center/stream", headers=h).json()
        item = stream["items"][0]
        assert item["timeframe"] is None, "认不出的周期归 NULL"
        assert item["signal_type"] == "mynewtype", "未知类型保留原值（小写）"
