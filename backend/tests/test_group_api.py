"""分组管理 API 与 strategy 信号端到端用例（fakeredis + SQLite，无需外部服务）。

覆盖三块：
1) /api/groups 的鉴权与 CRUD（含名称唯一、分发模式校验、成员校验、级联清理）；
2) /webhook 的 model 字段路由（缺省/normal 走原有按币种链路，strategy 走分组链路）；
3) strategy 信号端到端：分组下发 -> 节点收到带任务号魔术号的命令 -> 回报 ->
   /api/groups/{id}/signals 能看到主任务与各节点处理过程。
"""
import pathlib
import time

import fakeredis
import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.redis_store import RedisStore

_TEST_DB = pathlib.Path(__file__).resolve().parent / "_test_api.db"
@pytest.fixture
def client(monkeypatch):
    """每个用例一个全新 app + 全新 fakeredis（与 test_api.py 保持同一套引导方式）。"""
    def fake_from_url(cls, url=None):
        return RedisStore(fakeredis.FakeAsyncRedis(decode_responses=True))

    reset_test_db(_TEST_DB)
    monkeypatch.setattr(RedisStore, "from_url", classmethod(fake_from_url))
    from app.main import app

    with TestClient(app) as c:
        yield c
    # 连接池由 app 的 lifespan 在自己的事件循环里释放，这里只清文件
    drop_test_db(_TEST_DB)


from tests.test_helpers import (
    auth_headers,
    drop_test_db,
    reset_test_db,
    seed_default_filters,
)
def _node_token(client, headers) -> str:
    r = client.get("/api/config/node-token", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _mk_node(client, headers, mt5_login: int, name: str | None = None) -> str:
    r = client.post(
        "/api/nodes", json={"name": name or f"node-{mt5_login}", "mt5_login": mt5_login},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    return r.json()["node_id"]


def _wait_task_status(client, headers, group_id: str, expected: str, tries: int = 100) -> dict:
    """节点回报是异步落库，轮询等待主任务状态收口后再断言。"""
    page = {}
    for _ in range(tries):
        page = client.get(f"/api/groups/{group_id}/signals", headers=headers).json()
        if page["items"] and page["items"][0]["status"] == expected:
            return page
        time.sleep(0.02)
    return page


def _wait_dispatch_event(client, headers, group_id: str, dispatch_id: int,
                         event_type: str, tries: int = 100) -> dict | None:
    """节点上报是异步落库，轮询等待目标事件出现后再断言。"""
    url = f"/api/groups/{group_id}/dispatches/{dispatch_id}/events"
    for _ in range(tries):
        for event in client.get(url, headers=headers).json():
            if event["event_type"] == event_type:
                return event
        time.sleep(0.02)
    return None


def _mk_group(client, headers, *, symbol="XAUUSD", **body) -> dict:
    """建分组；默认自动绑定一条同品种策略（strategy 信号只进已绑策略的分组）。

    显式传 strategy_id=None 表示不绑定。
    """
    payload = {"name": "组一", "enabled": True, "dispatch_mode": "sync", "node_ids": []}
    payload.update(body)
    if "strategy_id" not in payload:
        sty = _mk_strategy(
            client, headers, name=f"策略-{payload['name']}", symbol=symbol,
        )
        payload["strategy_id"] = sty["strategy_id"]
    r = client.post("/api/groups", json=payload, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


# =====================================================================
# 1. 鉴权
# =====================================================================
def test_group_endpoints_require_auth(client):
    assert client.get("/api/groups").status_code == 401
    assert client.post("/api/groups", json={"name": "x"}).status_code == 401
    assert client.patch("/api/groups/grp_x", json={"name": "y"}).status_code == 401
    assert client.delete("/api/groups/grp_x").status_code == 401
    assert client.get("/api/groups/grp_x/signals").status_code == 401
    assert client.get("/api/groups/grp_x/dispatches/1/events").status_code == 401


# =====================================================================
# 2. 新建分组
# =====================================================================
def test_create_group_response_shape(client):
    h = auth_headers(client)
    node_id = _mk_node(client, h, 5101, "节点A")

    g = _mk_group(client, h, name="黄金策略组", dispatch_mode="poll",
                  remark="测试组", node_ids=[node_id], strategy_id=None)

    assert g["group_id"].startswith("grp_")
    assert g["name"] == "黄金策略组"
    assert g["enabled"] is True
    assert g["dispatch_mode"] == "poll"
    assert g["remark"] == "测试组"
    assert g["node_count"] == 1
    assert g["online_node_count"] == 0     # 节点尚未建立 WS 连接
    assert g["signal_count"] == 0
    assert g["nodes"][0]["node_id"] == node_id
    assert g["nodes"][0]["name"] == "节点A"
    assert g["nodes"][0]["mt5_login"] == 5101
    assert g["nodes"][0]["status"] == "offline"
    assert g["nodes"][0]["sort_order"] == 0
    assert g.get("strategy_id") in (None, "")
    assert g.get("strategy_name") in (None, "")


def _mk_strategy(client, headers, name: str = "测试策略", symbol: str = "XAUUSD") -> dict:
    from app.strategy_templates import TEMPLATE_1_ID
    r = client.post(
        "/api/strategies",
        json={"template_id": TEMPLATE_1_ID, "name": name, "symbol": symbol},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_create_group_with_strategy_binding(client):
    h = auth_headers(client)
    sty = _mk_strategy(client, h, name="黄金逆势")
    g = _mk_group(client, h, name="绑定策略组", strategy_id=sty["strategy_id"])
    assert g["strategy_id"] == sty["strategy_id"]
    assert g["strategy_name"] == "黄金逆势"


def test_create_group_unknown_strategy_returns_400(client):
    h = auth_headers(client)
    r = client.post(
        "/api/groups",
        json={"name": "坏策略组", "strategy_id": "sty_not_exist"},
        headers=h,
    )
    assert r.status_code == 400
    assert "策略不存在" in r.json()["detail"]


def test_strategy_one_to_one_binding(client):
    """同一策略不可绑定到两个分组。"""
    h = auth_headers(client)
    sty = _mk_strategy(client, h, name="独占策略")
    _mk_group(client, h, name="组A", strategy_id=sty["strategy_id"])
    r = client.post(
        "/api/groups",
        json={"name": "组B", "strategy_id": sty["strategy_id"]},
        headers=h,
    )
    assert r.status_code == 400
    assert "一对一" in r.json()["detail"]


def test_update_group_strategy_bind_and_unbind(client):
    h = auth_headers(client)
    sty = _mk_strategy(client, h, name="可换绑策略")
    g = _mk_group(client, h, name="换绑组")
    gid = g["group_id"]

    bound = client.patch(
        f"/api/groups/{gid}", json={"strategy_id": sty["strategy_id"]}, headers=h,
    )
    assert bound.status_code == 200, bound.text
    assert bound.json()["strategy_id"] == sty["strategy_id"]
    assert bound.json()["strategy_name"] == "可换绑策略"

    # 局部更新其它字段时，不应清掉策略绑定
    keep = client.patch(f"/api/groups/{gid}", json={"remark": "仍绑定"}, headers=h)
    assert keep.status_code == 200
    assert keep.json()["strategy_id"] == sty["strategy_id"]
    assert keep.json()["remark"] == "仍绑定"

    unbound = client.patch(f"/api/groups/{gid}", json={"strategy_id": None}, headers=h)
    assert unbound.status_code == 200, unbound.text
    assert unbound.json()["strategy_id"] in (None, "")


def test_delete_strategy_clears_group_binding(client):
    h = auth_headers(client)
    sty = _mk_strategy(client, h, name="将被删除")
    g = _mk_group(client, h, name="跟随解绑组", strategy_id=sty["strategy_id"])
    assert g["strategy_id"] == sty["strategy_id"]

    deleted = client.delete(f"/api/strategies/{sty['strategy_id']}", headers=h)
    assert deleted.status_code == 200, deleted.text

    refreshed = client.get(f"/api/groups/{g['group_id']}", headers=h)
    assert refreshed.status_code == 200
    assert refreshed.json()["strategy_id"] in (None, "")
def test_create_group_defaults_to_sync(client):
    h = auth_headers(client)
    g = _mk_group(client, h, name="默认模式组")
    assert g["dispatch_mode"] == "sync"
    assert g["node_count"] == 0


def test_create_group_duplicate_name_returns_409(client):
    h = auth_headers(client)
    _mk_group(client, h, name="重名组")
    r = client.post("/api/groups", json={"name": "重名组"}, headers=h)
    assert r.status_code == 409
    assert "已存在" in r.json()["detail"]


def test_create_group_invalid_dispatch_mode_returns_400(client):
    h = auth_headers(client)
    r = client.post(
        "/api/groups", json={"name": "非法模式组", "dispatch_mode": "random"}, headers=h,
    )
    assert r.status_code == 400
    assert "分发模式非法" in r.json()["detail"]


def test_create_group_unknown_node_returns_400(client):
    h = auth_headers(client)
    r = client.post(
        "/api/groups", json={"name": "野节点组", "node_ids": ["nd_missing"]}, headers=h,
    )
    assert r.status_code == 400
    assert "节点不存在" in r.json()["detail"]


def test_create_group_dedups_node_ids_and_keeps_order(client):
    h = auth_headers(client)
    a = _mk_node(client, h, 5111)
    b = _mk_node(client, h, 5112)

    g = _mk_group(client, h, name="去重组", node_ids=[b, a, b])
    assert [n["node_id"] for n in g["nodes"]] == [b, a]
    assert [n["sort_order"] for n in g["nodes"]] == [0, 1]


# =====================================================================
# 3. 查询与搜索
# =====================================================================
def test_list_and_search_groups(client):
    h = auth_headers(client)
    _mk_group(client, h, name="Alpha 策略")
    _mk_group(client, h, name="Beta 策略")

    all_groups = client.get("/api/groups", headers=h).json()
    assert [g["name"] for g in all_groups] == ["Alpha 策略", "Beta 策略"]

    hit = client.get("/api/groups", params={"q": "alpha"}, headers=h).json()
    assert [g["name"] for g in hit] == ["Alpha 策略"]

    assert client.get("/api/groups", params={"q": "无此组"}, headers=h).json() == []

def test_get_group_404(client):
    h = auth_headers(client)
    assert client.get("/api/groups/grp_missing", headers=h).status_code == 404
    assert client.get("/api/groups/grp_missing/signals", headers=h).status_code == 404


def test_group_signals_empty_page_shape(client):
    h = auth_headers(client)
    g = _mk_group(client, h, name="空信号组")
    r = client.get(f"/api/groups/{g['group_id']}/signals", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"items": [], "total": 0, "page": 1, "page_size": 20}


# =====================================================================
# 4. 编辑 / 启停 / 删除
# =====================================================================
def test_update_group_fields(client):
    h = auth_headers(client)
    a = _mk_node(client, h, 5121)
    b = _mk_node(client, h, 5122)
    g = _mk_group(client, h, name="待改组", node_ids=[a])

    r = client.patch(
        f"/api/groups/{g['group_id']}",
        json={"name": "改后组", "enabled": False, "dispatch_mode": "poll",
              "remark": "改过了", "node_ids": [b, a]},
        headers=h,
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["name"] == "改后组"
    assert out["enabled"] is False
    assert out["dispatch_mode"] == "poll"
    assert out["remark"] == "改过了"
    assert [n["node_id"] for n in out["nodes"]] == [b, a]
def test_toggle_group_enabled(client):
    h = auth_headers(client)
    g = _mk_group(client, h, name="启停组")
    gid = g["group_id"]

    off = client.patch(f"/api/groups/{gid}", json={"enabled": False}, headers=h).json()
    assert off["enabled"] is False
    on = client.patch(f"/api/groups/{gid}", json={"enabled": True}, headers=h).json()
    assert on["enabled"] is True

def test_update_group_partial_keeps_other_fields(client):
    h = auth_headers(client)
    a = _mk_node(client, h, 5131)
    g = _mk_group(client, h, name="局部改组", dispatch_mode="poll",
                  remark="原备注", node_ids=[a])

    out = client.patch(
        f"/api/groups/{g['group_id']}", json={"name": "局部改组2"}, headers=h,
    ).json()
    assert out["dispatch_mode"] == "poll"
    assert out["remark"] == "原备注"
    assert [n["node_id"] for n in out["nodes"]] == [a]


def test_update_group_duplicate_name_returns_409(client):
    h = auth_headers(client)
    _mk_group(client, h, name="已存在组")
    g2 = _mk_group(client, h, name="另一个组")
    r = client.patch(
        f"/api/groups/{g2['group_id']}", json={"name": "已存在组"}, headers=h,
    )
    assert r.status_code == 409


def test_update_group_same_name_allowed(client):
    """改其它字段时带上自身原名，不应被唯一校验拦住。"""
    h = auth_headers(client)
    g = _mk_group(client, h, name="同名放行组")
    r = client.patch(
        f"/api/groups/{g['group_id']}",
        json={"name": "同名放行组", "dispatch_mode": "poll"}, headers=h,
    )
    assert r.status_code == 200, r.text
    assert r.json()["dispatch_mode"] == "poll"


def test_update_missing_group_returns_404(client):
    h = auth_headers(client)
    r = client.patch("/api/groups/grp_missing", json={"name": "x"}, headers=h)
    assert r.status_code == 404


def test_delete_group(client):
    h = auth_headers(client)
    gid = _mk_group(client, h, name="待删组")["group_id"]

    r = client.delete(f"/api/groups/{gid}", headers=h)
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "deleted", "group_id": gid}
    assert client.get(f"/api/groups/{gid}", headers=h).status_code == 404
    assert client.delete(f"/api/groups/{gid}", headers=h).status_code == 404


def test_deleting_node_removes_it_from_groups(client):
    """节点被删除后应自动从分组成员中摘除，避免留下悬空成员。"""
    h = auth_headers(client)
    a = _mk_node(client, h, 5141)
    b = _mk_node(client, h, 5142)
    gid = _mk_group(client, h, name="级联组", node_ids=[a, b])["group_id"]

    assert client.delete(f"/api/nodes/{a}", headers=h).status_code == 200

    out = client.get(f"/api/groups/{gid}", headers=h).json()
    assert [n["node_id"] for n in out["nodes"]] == [b]
    assert out["node_count"] == 1


def test_group_audit_logged(client):
    h = auth_headers(client)
    gid = _mk_group(client, h, name="审计组")["group_id"]
    client.patch(f"/api/groups/{gid}", json={"enabled": False}, headers=h)
    client.delete(f"/api/groups/{gid}", headers=h)

    logs = client.get("/api/audits", params={"page_size": 50}, headers=h).json()["items"]
    actions = [x["action"] for x in logs if x["target"] == gid]
    assert {"create_group", "update_group", "delete_group"} <= set(actions)


# =====================================================================
# 5. Webhook model 字段路由
# =====================================================================
def test_missing_model_uses_normal_chain(client):
    """未带 model：保持项目原有默认行为（按币种分发）。"""
    seed_default_filters(client)
    r = client.post("/webhook", json={"action": "buy", "symbol": "EURUSD", "volume": 0.1})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["model"] == "normal"
    assert body["mode"] == "sync"      # 走 Dispatcher，而非分组
    assert "groups" not in body


def test_explicit_normal_model_uses_normal_chain(client):
    seed_default_filters(client)
    r = client.post(
        "/webhook",
        json={"action": "buy", "symbol": "EURUSD", "volume": 0.1, "model": "normal"},
    )
    body = r.json()
    assert body["model"] == "normal"
    assert body["mode"] == "sync"


def test_model_is_case_insensitive(client):
    seed_default_filters(client)
    r = client.post(
        "/webhook",
        json={"action": "buy", "symbol": "EURUSD", "volume": 0.1, "model": "NORMAL"},
    )
    assert r.json()["model"] == "normal"


def test_invalid_model_returns_400(client):
    seed_default_filters(client)
    r = client.post(
        "/webhook",
        json={"action": "buy", "symbol": "EURUSD", "volume": 0.1, "model": "group"},
    )
    assert r.status_code == 400
    assert "invalid model" in r.json()["detail"]


def test_strategy_without_group_is_rejected(client):
    """strategy 信号但没有任何启用分组 -> 拒收，且不落到 normal 链路。"""
    seed_default_filters(client)
    r = client.post(
        "/webhook",
        json={"action": "buy", "symbol": "EURUSD", "volume": 0.1, "model": "strategy"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "rejected"
    assert body["model"] == "strategy"
    assert body["mode"] == "rejected"
    assert body["groups"] == 0
    assert "分组" in body["reason"]


def test_strategy_ignores_symbol_config(client):
    """规则隔离：品种完全没在中控台配置，strategy 信号仍进入分组链路。"""
    h = auth_headers(client)
    _mk_group(client, h, name="隔离验证组", symbol="USDJPY")
    r = client.post(
        "/webhook",
        json={"action": "buy", "symbol": "USDJPY", "volume": 0.1, "model": "strategy"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "accepted"
    assert body["mode"] == "group"      # 未被“品种未配置”拒收
    assert body["groups"] == 1
    assert body["targets"] == 0            # 组内无在线节点
    assert body["tasks"][0]["status"] == "skipped"


def test_normal_and_strategy_not_deduped_against_each_other(client):
    """去重指纹带 model：同一笔行情的两种模型信号互不误判为重复。"""
    h = auth_headers(client)
    seed_default_filters(client)
    _mk_group(client, h, name="去重验证组", symbol="EURUSD")
    payload = {"action": "buy", "symbol": "EURUSD", "volume": 0.1}

    first = client.post("/webhook", json=payload).json()
    assert first["status"] == "accepted"
    dup = client.post("/webhook", json=payload).json()
    assert dup["status"] == "duplicate"

    other = client.post("/webhook", json={**payload, "model": "strategy"}).json()
    assert other["status"] == "accepted"
    assert other["mode"] == "group"


def test_manual_signal_supports_strategy_model(client):
    """中控台手动触发也能选择 strategy 模型，走同一套分组链路。"""
    h = auth_headers(client)
    _mk_group(client, h, name="手动触发组")
    r = client.post(
        "/api/console/manual-signal",
        json={"symbol": "XAUUSD", "action": "BUY", "volume": 0.1, "model": "strategy"},
        headers=h,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["model"] == "strategy"
    assert body["mode"] == "group"


def test_manual_signal_passes_sl_tp_and_comment(client):
    """手动触发的止损 / 止盈 / 备注按 Webhook 同名字段透传到分组主任务。"""
    h = auth_headers(client)
    gid = _mk_group(client, h, name="参数透传组")["group_id"]
    r = client.post(
        "/api/console/manual-signal",
        json={
            "symbol": "XAUUSD", "action": "SELL", "volume": 0.2, "model": "strategy",
            "stop_loss": 2600.5, "take_profit": 2500.5, "comment": "手动开仓",
        },
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "accepted"

    task = client.get(f"/api/groups/{gid}/signals", headers=h).json()["items"][0]
    assert (task["action"], task["volume"]) == ("SELL", 0.2)
    assert (task["sl"], task["tp"]) == (2600.5, 2500.5)
    assert task["comment"] == "手动开仓"


def test_manual_open_requires_volume(client):
    """开仓必须显式给手数，不静默回落到默认手数。"""
    h = auth_headers(client)
    _mk_group(client, h, name="手数校验组")
    r = client.post(
        "/api/console/manual-signal",
        json={"symbol": "XAUUSD", "action": "BUY", "model": "strategy"},
        headers=h,
    )
    assert r.status_code == 400
    assert "手数" in r.json()["detail"]


def test_manual_signal_rejects_unknown_action(client):
    h = auth_headers(client)
    r = client.post(
        "/api/console/manual-signal",
        json={"symbol": "XAUUSD", "action": "HOLD", "volume": 0.1},
        headers=h,
    )
    assert r.status_code == 400
    assert "动作非法" in r.json()["detail"]


def test_manual_close_requires_strategy_model(client):
    """CLOSE 只对 strategy 开放：normal 的 CLOSE 是全局按币种平仓，不从信号入口提供。"""
    h = auth_headers(client)
    seed_default_filters(client)
    r = client.post(
        "/api/console/manual-signal",
        json={"symbol": "XAUUSD", "action": "CLOSE"},
        headers=h,
    )
    assert r.status_code == 400
    assert "CLOSE" in r.json()["detail"]


def test_manual_close_goes_through_group_close(client):
    """strategy 的 CLOSE 走分组终止链路；组内无进行中任务时不新建主任务。"""
    h = auth_headers(client)
    gid = _mk_group(client, h, name="手动终止组")["group_id"]
    r = client.post(
        "/api/console/manual-signal",
        json={"symbol": "XAUUSD", "action": "CLOSE", "model": "strategy"},
        headers=h,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["model"] == "strategy"
    assert body["mode"] == "group_close"
    assert body["tasks"][0]["status"] == "skipped"
    assert client.get(f"/api/groups/{gid}/signals", headers=h).json()["total"] == 0


# =====================================================================
# 6. strategy 信号端到端
# =====================================================================
def test_strategy_end_to_end_sync(client):
    """分组 sync：两个在线节点各收到自己魔术号的开仓命令，回报后子任务收口。"""
    h = auth_headers(client)
    token = _node_token(client, h)
    n1 = _mk_node(client, h, 5201, "节点甲")
    n2 = _mk_node(client, h, 5202, "节点乙")
    gid = _mk_group(client, h, name="端到端组", node_ids=[n1, n2])["group_id"]

    with client.websocket_connect("/ws/node") as ws1, \
            client.websocket_connect("/ws/node") as ws2:
        ws1.send_json({"type": "auth", "data": {"token": token, "mt5_login": 5201}})
        assert ws1.receive_json()["type"] == "auth_ok"
        ws2.send_json({"type": "auth", "data": {"token": token, "mt5_login": 5202}})
        assert ws2.receive_json()["type"] == "auth_ok"

        # 分组内节点已在线：列表中的在线数应为 2
        listed = client.get(f"/api/groups/{gid}", headers=h).json()
        assert listed["online_node_count"] == 2
        r = client.post(
            "/webhook",
            json={"action": "buy", "symbol": "XAUUSD", "volume": 0.2, "model": "strategy"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "accepted"
        assert body["mode"] == "group"
        assert body["targets"] == 2
        task = body["tasks"][0]
        assert task["group_id"] == gid
        assert task["dispatch_mode"] == "sync"
        assert "magic" not in task  # 魔术号在各节点子任务上

        cmd1 = ws1.receive_json()
        cmd2 = ws2.receive_json()
        for cmd in (cmd1, cmd2):
            assert cmd["cmd"] == "strategy_start"
            assert cmd["entry"]["action"] == "BUY"
            assert cmd["entry"]["symbol"] == "XAUUSD"
            assert cmd["entry"]["volume"] == 0.2
            assert cmd["model"] == "strategy"
            assert cmd["group_id"] == gid
            assert cmd["task_id"] == task["task_id"]
            # MT5 下单用的魔术号即该节点子任务号换算值
            assert cmd["magic"] == Config.NODE_TASK_MAGIC_BASE + cmd["dispatch_id"]
            # 策略规则随命令下发，节点据此常驻加仓
            assert cmd["strategy"]["symbol"] == "XAUUSD"
        # 两个节点各持一个魔术号，互不共用
        assert cmd1["magic"] != cmd2["magic"]

        ws1.send_json({"type": "trade_result", "data": {
            "signal_id": cmd1["signal_id"], "magic": cmd1["magic"],
            "symbol": "XAUUSD", "success": True, "order": 7001, "price": 2400.5,
        }})
        ws2.send_json({"type": "trade_result", "data": {
            "signal_id": cmd2["signal_id"], "magic": cmd2["magic"],
            "symbol": "XAUUSD", "success": False, "error": "no money",
        }})
        # n1 首单成交进入 opened，主任务处于 running（策略仍在跑）
        _wait_task_status(client, h, gid, "running")

        # n1 上报持仓已全平 -> 一成一败，主任务收口为 partial
        ws1.send_json({"type": "strategy_finished", "data": {
            "task_id": task["task_id"], "magic": cmd1["magic"],
            "status": "done", "reason": "positions_cleared",
            "total_orders": 2, "total_volume": 0.42, "realized_profit": 8.0,
        }})
        page = _wait_task_status(client, h, gid, "partial")

    assert page["total"] == 1
    item = page["items"][0]
    assert item["task_id"] == task["task_id"]
    assert item["signal_id"] == body["signal_id"]
    assert item["group_name"] == "端到端组"
    assert item["symbol"] == "XAUUSD"
    assert item["action"] == "BUY"
    assert item["volume"] == 0.2
    assert item["dispatch_mode"] == "sync"
    assert item["node_count"] == 2
    assert set(item["node_ids"]) == {n1, n2}
    assert item["payload"]["cmd"] == "strategy_start"
    assert item["status"] == "partial"      # 一成一败
    assert item["total_orders"] == 2
    assert item["realized_profit"] == 8.0

    by_node = {d["node_id"]: d for d in item["dispatches"]}
    assert by_node[n1]["status"] == "done"
    assert by_node[n1]["node_name"] == "节点甲"
    assert by_node[n1]["order"] == 7001
    assert by_node[n1]["price"] == 2400.5
    assert by_node[n1]["finish_reason"] == "positions_cleared"
    assert by_node[n1]["magic"] == cmd1["magic"]
    assert by_node[n1]["symbol"] == "XAUUSD"
    assert by_node[n2]["status"] == "failed"
    assert by_node[n2]["error"] == "no money"
    assert by_node[n2]["magic"] == cmd2["magic"]

    # 分组列表的信号数随之增长
    assert client.get(f"/api/groups/{gid}", headers=h).json()["signal_count"] == 1


def test_dispatch_events_related_orders(client):
    """点击节点子任务可查关联订单：首单回报补事件 + strategy_progress 加仓事件 + 收口。"""
    h = auth_headers(client)
    token = _node_token(client, h)
    n1 = _mk_node(client, h, 5251, "订单节点")
    gid = _mk_group(client, h, name="关联订单组", node_ids=[n1])["group_id"]

    with client.websocket_connect("/ws/node") as ws1:
        ws1.send_json({"type": "auth", "data": {"token": token, "mt5_login": 5251}})
        assert ws1.receive_json()["type"] == "auth_ok"

        client.post("/webhook", json={
            "action": "buy", "symbol": "XAUUSD", "volume": 0.1, "model": "strategy",
        })
        start = ws1.receive_json()
        assert start["cmd"] == "strategy_start"
        did = start["dispatch_id"]

        ws1.send_json({"type": "trade_result", "data": {
            "signal_id": start["signal_id"], "magic": start["magic"],
            "symbol": "XAUUSD", "success": True, "order": 88001, "price": 2401.2,
        }})
        _wait_task_status(client, h, gid, "running")

        # trade_result 未写事件表时，接口仍应补出首单关联订单
        ev1 = client.get(f"/api/groups/{gid}/dispatches/{did}/events", headers=h)
        assert ev1.status_code == 200, ev1.text
        items = ev1.json()
        assert any(e["event_type"] == "open" and e["order_ticket"] == 88001 for e in items)

        ws1.send_json({"type": "strategy_progress", "data": {
            "task_id": start["task_id"], "dispatch_id": did, "magic": start["magic"],
            "event": "add_counter", "symbol": "XAUUSD", "action": "BUY",
            "phase": "running", "position_count": 2, "add_count": 1,
            "total_orders": 2, "total_volume": 0.2, "profit": 1.5,
            "last_order": {"ticket": 88002, "price": 2398.0, "volume": 0.1},
        }})
        time.sleep(0.05)

        ws1.send_json({"type": "strategy_finished", "data": {
            "task_id": start["task_id"], "magic": start["magic"],
            "status": "done", "reason": "positions_cleared",
            "total_orders": 2, "total_volume": 0.2, "realized_profit": 3.0,
        }})
        _wait_task_status(client, h, gid, "done")

    ev2 = client.get(f"/api/groups/{gid}/dispatches/{did}/events", headers=h)
    assert ev2.status_code == 200, ev2.text
    types = [e["event_type"] for e in ev2.json()]
    assert "open" in types
    assert "add_counter" in types
    assert "close_all" in types
    # 最新在前：收口 → 加仓 → 开仓
    assert types.index("close_all") < types.index("add_counter") < types.index("open")
    add = next(e for e in ev2.json() if e["event_type"] == "add_counter")
    assert add["order_ticket"] == 88002
    assert add["price"] == 2398.0

    assert client.get(f"/api/groups/{gid}/dispatches/999999/events", headers=h).status_code == 404
    assert client.get("/api/groups/grp_missing/dispatches/1/events", headers=h).status_code == 404


def test_dispatch_event_keeps_open_reason_and_calc_detail(client):
    """节点上报的开单原因与计算依据要原样落库并回读，用于还原这一单为什么下。"""
    h = auth_headers(client)
    token = _node_token(client, h)
    n1 = _mk_node(client, h, 5261, "原因节点")
    gid = _mk_group(client, h, name="开单原因组", node_ids=[n1])["group_id"]
    reason = (
        "逆势加仓 · 规则#0：BUY 基准价 2400 → 现价 2399，逆向偏离 100 点 ≥ 阈值 100 点；"
        "手数 = 首单 0.1 × 倍数 1.1 + 追加 0 = 0.11 手"
    )
    detail = {
        "kind": "add", "rule_type": 1, "rule_type_label": "逆势加仓", "rule_index": 0,
        "level_index": None, "batch": False, "direction": "BUY",
        "base_price": 2400.0, "price": 2399.0, "point": 0.01,
        "deviation": 100.0, "threshold": 100.0,
        "base_volume": 0.1, "lot_times": 1.1, "extra_lot": 0.0, "volume": 0.11,
        "volume_formula": "0.1 × 1.1 + 0 = 0.11",
        "position_count": 1, "add_count": 0, "next_position_no": 2,
        "limit_kind": "max_allow_num", "limit_value": 3,
    }

    with client.websocket_connect("/ws/node") as ws1:
        ws1.send_json({"type": "auth", "data": {"token": token, "mt5_login": 5261}})
        assert ws1.receive_json()["type"] == "auth_ok"

        client.post("/webhook", json={
            "action": "buy", "symbol": "XAUUSD", "volume": 0.1, "model": "strategy",
        })
        start = ws1.receive_json()
        did = start["dispatch_id"]

        ws1.send_json({"type": "strategy_progress", "data": {
            "task_id": start["task_id"], "dispatch_id": did, "magic": start["magic"],
            "event": "add_counter", "symbol": "XAUUSD", "action": "BUY",
            "phase": "running", "position_count": 2, "add_count": 1,
            "total_orders": 2, "total_volume": 0.21,
            "last_order": {"ticket": 88010, "price": 2399.0, "volume": 0.11},
            "message": reason, "detail": detail,
        }})
        add = _wait_dispatch_event(client, h, gid, did, "add_counter")

    assert add is not None
    assert add["message"] == reason
    assert add["detail"] == detail


def test_dispatch_event_truncates_overlong_reason(client):
    """说明列是 VARCHAR(255)，超长文本必须截断而不是整条上报失败。"""
    h = auth_headers(client)
    token = _node_token(client, h)
    n1 = _mk_node(client, h, 5262, "超长说明节点")
    gid = _mk_group(client, h, name="超长说明组", node_ids=[n1])["group_id"]

    with client.websocket_connect("/ws/node") as ws1:
        ws1.send_json({"type": "auth", "data": {"token": token, "mt5_login": 5262}})
        assert ws1.receive_json()["type"] == "auth_ok"

        client.post("/webhook", json={
            "action": "buy", "symbol": "XAUUSD", "volume": 0.1, "model": "strategy",
        })
        start = ws1.receive_json()
        did = start["dispatch_id"]

        ws1.send_json({"type": "strategy_progress", "data": {
            "task_id": start["task_id"], "dispatch_id": did, "magic": start["magic"],
            "event": "add_trend", "symbol": "XAUUSD", "action": "BUY", "phase": "running",
            "message": "顺" * 400,
        }})
        add = _wait_dispatch_event(client, h, gid, did, "add_trend")

    assert add is not None
    assert len(add["message"]) == 255
    assert add["detail"] is None


def test_strategy_end_to_end_poll_only_one_node(client):
    """分组 poll：一条信号只交给组内一个节点，第二条交给下一个节点。"""
    h = auth_headers(client)
    token = _node_token(client, h)
    n1 = _mk_node(client, h, 5211, "轮询甲")
    n2 = _mk_node(client, h, 5212, "轮询乙")
    gid = _mk_group(
        client, h, name="轮询端到端组", dispatch_mode="poll", node_ids=[n1, n2],
    )["group_id"]

    with client.websocket_connect("/ws/node") as ws1, \
            client.websocket_connect("/ws/node") as ws2:
        ws1.send_json({"type": "auth", "data": {"token": token, "mt5_login": 5211}})
        assert ws1.receive_json()["type"] == "auth_ok"
        ws2.send_json({"type": "auth", "data": {"token": token, "mt5_login": 5212}})
        assert ws2.receive_json()["type"] == "auth_ok"

        first = client.post("/webhook", json={
            "action": "buy", "symbol": "XAUUSD", "volume": 0.1, "model": "strategy",
        }).json()
        assert first["targets"] == 1
        cmd1 = ws1.receive_json()          # 队首节点领取
        assert cmd1["cmd"] == "strategy_start"

        # 节点互斥：领取者收口后其占位才放开（这里轮转到另一个节点，本不受阻）
        ws1.send_json({"type": "strategy_finished", "data": {
            "task_id": cmd1["task_id"], "magic": cmd1["magic"], "status": "done",
        }})
        _wait_task_status(client, h, gid, "done")

        second = client.post("/webhook", json={
            "action": "buy", "symbol": "XAUUSD", "volume": 0.3, "model": "strategy",
        }).json()
        assert second["targets"] == 1
        cmd2 = ws2.receive_json()          # 轮转到下一个节点
        assert cmd2["entry"]["volume"] == 0.3
        assert cmd2["task_id"] != cmd1["task_id"]

    page = client.get(f"/api/groups/{gid}/signals", headers=h).json()
    assert page["total"] == 2
    for item in page["items"]:
        assert item["dispatch_mode"] == "poll"
        assert item["node_count"] == 1     # 每条信号只下发一个节点
        assert len(item["dispatches"]) == 1


def test_node_busy_then_released_end_to_end(client):
    """组内节点互斥全链路：运行中的节点跳过本分组新信号，收口后重新可用。"""
    h = auth_headers(client)
    token = _node_token(client, h)
    n1 = _mk_node(client, h, 5241, "互斥甲")
    gid = _mk_group(client, h, name="互斥端到端组", node_ids=[n1])["group_id"]

    with client.websocket_connect("/ws/node") as ws1:
        ws1.send_json({"type": "auth", "data": {"token": token, "mt5_login": 5241}})
        assert ws1.receive_json()["type"] == "auth_ok"

        client.post("/webhook", json={
            "action": "buy", "symbol": "XAUUSD", "volume": 0.1, "model": "strategy",
        })
        start = ws1.receive_json()
        ws1.send_json({"type": "trade_result", "data": {
            "signal_id": start["signal_id"], "magic": start["magic"],
            "symbol": "XAUUSD", "success": True, "order": 1,
        }})
        _wait_task_status(client, h, gid, "running")

        # 该节点在本分组内已被占：第二条信号进得来但无节点可发
        # （手数换一个值，避开 5 秒内的重复信号去重）
        busy = client.post("/webhook", json={
            "action": "buy", "symbol": "XAUUSD", "volume": 0.2, "model": "strategy",
        }).json()
        assert busy["targets"] == 0
        assert busy["tasks"][0]["status"] == "skipped"
        assert "均有进行中的任务" in busy["tasks"][0]["reason"]

        # 收口后占位释放，同节点同品种重新接单
        ws1.send_json({"type": "strategy_finished", "data": {
            "task_id": start["task_id"], "magic": start["magic"], "status": "done",
        }})
        _wait_task_status(client, h, gid, "done")

        again = client.post("/webhook", json={
            "action": "buy", "symbol": "XAUUSD", "volume": 0.3, "model": "strategy",
        }).json()
        assert again["targets"] == 1
        resumed = ws1.receive_json()
        assert resumed["cmd"] == "strategy_start"
        assert resumed["magic"] != start["magic"]   # 新子任务 -> 新魔术号

        # 收尾：把最后这条也收口，避免退出时还有落库在途影响后续用例
        ws1.send_json({"type": "strategy_finished", "data": {
            "task_id": resumed["task_id"], "magic": resumed["magic"], "status": "done",
        }})
        _wait_task_status(client, h, gid, "done")


def test_strategy_close_terminates_running_task(client):
    """CLOSE 是终止指令：绕过互斥，让每个在跑节点平掉自己魔术号的持仓。"""
    h = auth_headers(client)
    token = _node_token(client, h)
    n1 = _mk_node(client, h, 5221)
    n2 = _mk_node(client, h, 5222)
    gid = _mk_group(
        client, h, name="平仓端到端组", dispatch_mode="sync", node_ids=[n1, n2],
    )["group_id"]

    with client.websocket_connect("/ws/node") as ws1, \
            client.websocket_connect("/ws/node") as ws2:
        ws1.send_json({"type": "auth", "data": {"token": token, "mt5_login": 5221}})
        assert ws1.receive_json()["type"] == "auth_ok"
        ws2.send_json({"type": "auth", "data": {"token": token, "mt5_login": 5222}})
        assert ws2.receive_json()["type"] == "auth_ok"

        client.post("/webhook", json={
            "action": "buy", "symbol": "XAUUSD", "volume": 0.1, "model": "strategy",
        })
        start1 = ws1.receive_json()
        start2 = ws2.receive_json()
        assert start1["cmd"] == "strategy_start"
        for ws, cmd in ((ws1, start1), (ws2, start2)):
            ws.send_json({"type": "trade_result", "data": {
                "signal_id": cmd["signal_id"], "magic": cmd["magic"],
                "symbol": "XAUUSD", "success": True, "order": 1,
            }})
        _wait_task_status(client, h, gid, "running")

        r = client.post("/webhook", json={
            "action": "close", "symbol": "XAUUSD", "model": "strategy",
        }).json()
        assert r["mode"] == "group_close"
        assert r["targets"] == 2            # 在跑的节点全部收到终止指令

        for ws, start in ((ws1, start1), (ws2, start2)):
            cmd = ws.receive_json()
            assert cmd["cmd"] == "strategy_stop"
            # 各节点收到的是自己那个魔术号，不是共用的
            assert cmd["magic"] == start["magic"]
            assert cmd["dispatch_id"] == start["dispatch_id"]
            assert cmd["group_id"] == gid
            assert cmd["model"] == "strategy"

    page = client.get(f"/api/groups/{gid}/signals", headers=h).json()
    assert page["total"] == 1               # CLOSE 不新建主任务
    assert page["items"][0]["action"] == "BUY"


def test_manual_close_dispatch_stops_one_node(client):
    """分组信号页手动平仓：只向指定子任务下发 strategy_stop。"""
    h = auth_headers(client)
    token = _node_token(client, h)
    n1 = _mk_node(client, h, 5241)
    n2 = _mk_node(client, h, 5242)
    gid = _mk_group(
        client, h, name="手动单节点平仓组", dispatch_mode="sync", node_ids=[n1, n2],
    )["group_id"]

    with client.websocket_connect("/ws/node") as ws1, \
            client.websocket_connect("/ws/node") as ws2:
        ws1.send_json({"type": "auth", "data": {"token": token, "mt5_login": 5241}})
        assert ws1.receive_json()["type"] == "auth_ok"
        ws2.send_json({"type": "auth", "data": {"token": token, "mt5_login": 5242}})
        assert ws2.receive_json()["type"] == "auth_ok"

        client.post("/webhook", json={
            "action": "buy", "symbol": "XAUUSD", "volume": 0.1, "model": "strategy",
        })
        start1 = ws1.receive_json()
        start2 = ws2.receive_json()
        for ws, cmd in ((ws1, start1), (ws2, start2)):
            ws.send_json({"type": "trade_result", "data": {
                "signal_id": cmd["signal_id"], "magic": cmd["magic"],
                "symbol": "XAUUSD", "success": True, "order": 1,
            }})
        _wait_task_status(client, h, gid, "running")

        page = client.get(f"/api/groups/{gid}/signals", headers=h).json()
        d1 = next(x for x in page["items"][0]["dispatches"] if x["node_id"] == n1)
        d2 = next(x for x in page["items"][0]["dispatches"] if x["node_id"] == n2)

        r = client.post(f"/api/groups/{gid}/dispatches/{d1['id']}/close", headers=h)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "closing"
        assert body["dispatch_id"] == d1["id"]
        assert body["node_id"] == n1

        stop = ws1.receive_json()
        assert stop["cmd"] == "strategy_stop"
        assert stop["dispatch_id"] == d1["id"]
        assert stop["magic"] == start1["magic"]
        assert stop["reason"] == "manual_close"

        page2 = client.get(f"/api/groups/{gid}/signals", headers=h).json()
        by_id = {x["id"]: x for x in page2["items"][0]["dispatches"]}
        assert by_id[d1["id"]]["status"] == "closing"
        assert by_id[d2["id"]]["status"] in ("opened", "running")
        assert by_id[d2["id"]]["status"] != "closing"

        # 收口两端，避免污染后续用例
        for ws, start, did in (
            (ws1, start1, d1["id"]),
            (ws2, start2, d2["id"]),
        ):
            ws.send_json({"type": "strategy_finished", "data": {
                "task_id": start["task_id"], "dispatch_id": did,
                "magic": start["magic"], "status": "done",
            }})
        _wait_task_status(client, h, gid, "done")

        again = client.post(f"/api/groups/{gid}/dispatches/{d1['id']}/close", headers=h)
        assert again.status_code == 400
        assert "已结束" in again.json()["detail"]


def test_disabled_group_receives_nothing(client):
    h = auth_headers(client)
    token = _node_token(client, h)
    n1 = _mk_node(client, h, 5231)
    n2 = _mk_node(client, h, 5232)
    on_gid = _mk_group(client, h, name="在用组", node_ids=[n1])["group_id"]
    off_gid = _mk_group(
        client, h, name="停用组", enabled=False, node_ids=[n2],
    )["group_id"]
    with client.websocket_connect("/ws/node") as ws1, \
            client.websocket_connect("/ws/node") as ws2:
        ws1.send_json({"type": "auth", "data": {"token": token, "mt5_login": 5231}})
        assert ws1.receive_json()["type"] == "auth_ok"
        ws2.send_json({"type": "auth", "data": {"token": token, "mt5_login": 5232}})
        assert ws2.receive_json()["type"] == "auth_ok"

        r = client.post("/webhook", json={
            "action": "buy", "symbol": "XAUUSD", "volume": 0.1, "model": "strategy",
        }).json()
        assert r["groups"] == 1
        assert r["targets"] == 1
        assert ws1.receive_json()["cmd"] == "strategy_start"

    assert client.get(f"/api/groups/{on_gid}/signals", headers=h).json()["total"] == 1
    assert client.get(f"/api/groups/{off_gid}/signals", headers=h).json()["total"] == 0


def test_strategy_skips_disabled_node_in_group(client):
    """成员节点被禁用后不再参与分组下发（有效节点 = 已启用 + 在线）。"""
    h = auth_headers(client)
    token = _node_token(client, h)
    n1 = _mk_node(client, h, 5241)
    n2 = _mk_node(client, h, 5242)
    gid = _mk_group(client, h, name="禁用成员组", node_ids=[n1, n2])["group_id"]

    with client.websocket_connect("/ws/node") as ws1, \
            client.websocket_connect("/ws/node") as ws2:
        ws1.send_json({"type": "auth", "data": {"token": token, "mt5_login": 5241}})
        assert ws1.receive_json()["type"] == "auth_ok"
        ws2.send_json({"type": "auth", "data": {"token": token, "mt5_login": 5242}})
        assert ws2.receive_json()["type"] == "auth_ok"

        # 连接保持在线，但把 n2 禁用：在线不等于有效
        assert client.patch(
            f"/api/nodes/{n2}", json={"enabled": False}, headers=h,
        ).status_code == 200
        assert client.get(f"/api/groups/{gid}", headers=h).json()["online_node_count"] == 1

        r = client.post("/webhook", json={
            "action": "buy", "symbol": "XAUUSD", "volume": 0.1, "model": "strategy",
        }).json()
        assert r["targets"] == 1
        assert ws1.receive_json()["cmd"] == "strategy_start"

    page = client.get(f"/api/groups/{gid}/signals", headers=h).json()
    assert page["items"][0]["node_ids"] == [n1]


def test_strategy_signal_appears_in_events_with_model(client):
    """信号事件流应带上处理模型，便于在事件页区分两条链路。"""
    h = auth_headers(client)
    _mk_group(client, h, name="事件流组")
    client.post("/webhook", json={
        "action": "buy", "symbol": "XAUUSD", "volume": 0.1, "model": "strategy",
    })

    items = client.get(
        "/api/events/signals", params={"page_size": 20}, headers=h,
    ).json()["items"]
    strategy = [x for x in items if x["model"] == "strategy"]
    assert len(strategy) == 1
    assert strategy[0]["symbol"] == "XAUUSD"
    assert strategy[0]["dispatch_mode"] == "group"


def test_purge_trade_logs_clears_tables_keeps_config(client):
    """清空交易记录：五张运行表清空，分组/策略配置与审计保留。"""
    h = auth_headers(client)
    token = _node_token(client, h)
    n1 = _mk_node(client, h, 5261, "清空节点")
    g = _mk_group(client, h, name="清空交易记录组", node_ids=[n1])
    gid = g["group_id"]
    strategy_id = g["strategy_id"]

    with client.websocket_connect("/ws/node") as ws1:
        ws1.send_json({"type": "auth", "data": {"token": token, "mt5_login": 5261}})
        assert ws1.receive_json()["type"] == "auth_ok"
        client.post("/webhook", json={
            "action": "buy", "symbol": "XAUUSD", "volume": 0.1, "model": "strategy",
        })
        start = ws1.receive_json()
        ws1.send_json({"type": "trade_result", "data": {
            "signal_id": start["signal_id"], "magic": start["magic"],
            "symbol": "XAUUSD", "success": True, "order": 99001, "price": 2400.0,
        }})
        _wait_task_status(client, h, gid, "running")
        ws1.send_json({"type": "strategy_finished", "data": {
            "task_id": start["task_id"], "magic": start["magic"], "status": "done",
        }})
        _wait_task_status(client, h, gid, "done")

    assert client.get(f"/api/groups/{gid}/signals", headers=h).json()["total"] >= 1
    assert client.get("/api/events/signals", headers=h).json()["total"] >= 1

    assert client.post("/api/console/purge-trade-logs", json={}).status_code == 401
    bad = client.post(
        "/api/console/purge-trade-logs", json={"confirm": "wrong"}, headers=h,
    )
    assert bad.status_code == 400

    ok = client.post(
        "/api/console/purge-trade-logs",
        json={"confirm": "清空交易记录"},
        headers=h,
    )
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["total_deleted"] >= 1
    assert body["deleted"]["signal_history"] >= 1
    assert body["deleted"]["group_signal_task"] >= 1

    assert client.get(f"/api/groups/{gid}/signals", headers=h).json()["total"] == 0
    assert client.get("/api/events/signals", headers=h).json()["total"] == 0
    # 配置保留
    assert client.get(f"/api/groups/{gid}", headers=h).status_code == 200
    assert client.get(f"/api/strategies/{strategy_id}", headers=h).status_code == 200
    assert client.get(f"/api/nodes/{n1}", headers=h).status_code == 200
    # 操作本身写入审计
    audits = client.get("/api/audits", params={"page_size": 50}, headers=h).json()["items"]
    assert any(a["action"] == "purge_trade_logs" for a in audits)
