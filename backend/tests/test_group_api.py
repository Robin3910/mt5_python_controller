"""分组管理 API 与 strategy 信号端到端用例（fakeredis + SQLite，无需外部服务）。

覆盖三块：
1) /api/groups 的鉴权与 CRUD（含名称唯一、分发模式校验、成员校验、级联清理）；
2) /webhook 的 model 字段路由（缺省/normal 走原有按币种链路，strategy 走分组链路）；
3) strategy 信号端到端：分组下发 -> 节点收到带任务号魔术号的命令 -> 回报 ->
   /api/groups/{id}/signals 能看到主任务与各节点处理过程。
"""
import asyncio
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

    monkeypatch.setattr(RedisStore, "from_url", classmethod(fake_from_url))
    from app.main import app

    with TestClient(app) as c:
        yield c

    asyncio.run(__import__("app.db", fromlist=["engine"]).engine.dispose())
    try:
        _TEST_DB.unlink()
    except (FileNotFoundError, PermissionError):
        pass


from tests.test_helpers import auth_headers, seed_default_filters
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


def _mk_group(client, headers, **body) -> dict:
    payload = {"name": "组一", "enabled": True, "dispatch_mode": "sync", "node_ids": []}
    payload.update(body)
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


# =====================================================================
# 2. 新建分组
# =====================================================================
def test_create_group_response_shape(client):
    h = auth_headers(client)
    node_id = _mk_node(client, h, 5101, "节点A")

    g = _mk_group(client, h, name="黄金策略组", dispatch_mode="poll",
                  remark="测试组", node_ids=[node_id])

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
    _mk_group(client, h, name="隔离验证组")
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
    _mk_group(client, h, name="去重验证组")
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


# =====================================================================
# 6. strategy 信号端到端
# =====================================================================
def test_strategy_end_to_end_sync(client):
    """分组 sync：两个在线节点都收到带任务号魔术号的开仓命令，回报后明细收口。"""
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
        assert task["magic"] == Config.GROUP_TASK_MAGIC_BASE + task["task_id"]

        cmd1 = ws1.receive_json()
        cmd2 = ws2.receive_json()
        for cmd in (cmd1, cmd2):
            assert cmd["cmd"] == "open"
            assert cmd["action"] == "BUY"
            assert cmd["symbol"] == "XAUUSD"
            assert cmd["volume"] == 0.2
            assert cmd["model"] == "strategy"
            assert cmd["group_id"] == gid
            assert cmd["task_id"] == task["task_id"]
            # MT5 下单用的魔术号即主任务号换算值
            assert cmd["magic"] == task["magic"]

        ws1.send_json({"type": "trade_result", "data": {
            "signal_id": cmd1["signal_id"], "magic": cmd1["magic"],
            "symbol": "XAUUSD", "success": True, "order": 7001, "price": 2400.5,
        }})
        ws2.send_json({"type": "trade_result", "data": {
            "signal_id": cmd2["signal_id"], "magic": cmd2["magic"],
            "symbol": "XAUUSD", "success": False, "error": "no money",
        }})
        # 一成一败 -> 主任务收口为 partial（连接保持打开，等回报处理完）
        page = _wait_task_status(client, h, gid, "partial")

    assert page["total"] == 1
    item = page["items"][0]
    assert item["task_id"] == task["task_id"]
    assert item["magic"] == task["magic"]
    assert item["signal_id"] == body["signal_id"]
    assert item["group_name"] == "端到端组"
    assert item["symbol"] == "XAUUSD"
    assert item["action"] == "BUY"
    assert item["volume"] == 0.2
    assert item["dispatch_mode"] == "sync"
    assert item["node_count"] == 2
    assert set(item["node_ids"]) == {n1, n2}
    assert item["payload"]["cmd"] == "open"
    assert item["status"] == "partial"      # 一成一败

    by_node = {d["node_id"]: d for d in item["dispatches"]}
    assert by_node[n1]["status"] == "done"
    assert by_node[n1]["node_name"] == "节点甲"
    assert by_node[n1]["order"] == 7001
    assert by_node[n1]["price"] == 2400.5
    assert by_node[n1]["magic"] == task["magic"]
    assert by_node[n2]["status"] == "failed"
    assert by_node[n2]["error"] == "no money"

    # 分组列表的信号数随之增长
    assert client.get(f"/api/groups/{gid}", headers=h).json()["signal_count"] == 1


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
        assert cmd1["cmd"] == "open"

        second = client.post("/webhook", json={
            "action": "buy", "symbol": "XAUUSD", "volume": 0.3, "model": "strategy",
        }).json()
        assert second["targets"] == 1
        cmd2 = ws2.receive_json()          # 轮转到下一个节点
        assert cmd2["volume"] == 0.3
        assert cmd2["task_id"] != cmd1["task_id"]

    page = client.get(f"/api/groups/{gid}/signals", headers=h).json()
    assert page["total"] == 2
    for item in page["items"]:
        assert item["dispatch_mode"] == "poll"
        assert item["node_count"] == 1     # 每条信号只下发一个节点
        assert len(item["dispatches"]) == 1


def test_strategy_close_broadcasts_in_group(client):
    h = auth_headers(client)
    token = _node_token(client, h)
    n1 = _mk_node(client, h, 5221)
    n2 = _mk_node(client, h, 5222)
    gid = _mk_group(
        client, h, name="平仓端到端组", dispatch_mode="poll", node_ids=[n1, n2],
    )["group_id"]

    with client.websocket_connect("/ws/node") as ws1, \
            client.websocket_connect("/ws/node") as ws2:
        ws1.send_json({"type": "auth", "data": {"token": token, "mt5_login": 5221}})
        assert ws1.receive_json()["type"] == "auth_ok"
        ws2.send_json({"type": "auth", "data": {"token": token, "mt5_login": 5222}})
        assert ws2.receive_json()["type"] == "auth_ok"

        r = client.post("/webhook", json={
            "action": "close", "symbol": "XAUUSD", "model": "strategy",
        }).json()
        assert r["mode"] == "group"
        assert r["targets"] == 2            # CLOSE 不受 poll 限制，组内全员平仓

        for ws in (ws1, ws2):
            cmd = ws.receive_json()
            assert cmd["cmd"] == "close"
            assert cmd["close_target"] == "symbol"
            assert cmd["close_symbol"] == "XAUUSD"
            assert cmd["group_id"] == gid
            assert cmd["model"] == "strategy"

    page = client.get(f"/api/groups/{gid}/signals", headers=h).json()
    assert page["items"][0]["action"] == "CLOSE"
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
        assert ws1.receive_json()["cmd"] == "open"

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
        assert ws1.receive_json()["cmd"] == "open"

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
