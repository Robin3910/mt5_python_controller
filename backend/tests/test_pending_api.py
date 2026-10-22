"""远程「删挂单」编排：停策略 → 撤单 → 复扫，及其失败分支。

打的是 `pending_api` 的路由函数本体（直接调用，不走 HTTP），
重点验证编排顺序与 status 判定，不重复测 node_rpc 的配对（见 test_node_rpc.py）。
"""
import fakeredis
import pytest

from app import node_rpc, pending_api
from app.connections import manager
from app.models import CancelPendingRequest
from app.redis_store import RedisStore
from app.state import state


class _Req:
    """最小 Request 替身：client_ip 只用到 headers 与 client.host。"""
    headers: dict = {}

    class client:  # noqa: N801
        host = "127.0.0.1"


class _Principal:
    username = "tester"


@pytest.fixture(autouse=True)
def _clean():
    node_rpc.reset_waiters()
    yield
    node_rpc.reset_waiters()


@pytest.fixture
async def store(monkeypatch):
    redis = RedisStore(fakeredis.FakeAsyncRedis(decode_responses=True))
    state.store = redis

    async def no_ownership_check(_store, _p, _node_id):
        return None

    monkeypatch.setattr(pending_api, "owned_node_or_404", no_ownership_check)
    yield redis
    state.store = None


@pytest.fixture
def dispatcher():
    class _Dispatcher:
        def __init__(self):
            self.calls = []

        async def stop_all_on_node(self, node_id, *, signal_id, reason):
            self.calls.append({"node_id": node_id, "signal_id": signal_id, "reason": reason})
            return {"stopped": 1, "forced": 0, "total": 1}

    return _Dispatcher()


async def _call(store, dispatcher, *, node_id="nd_a", scope="all", online=True):
    """直接调路由函数：先按需把节点塞进在线表。"""
    if online:
        manager.nodes.setdefault(node_id, object())
    try:
        return await pending_api.cancel_pending_on_node_route(
            node_id,
            CancelPendingRequest(scope=scope),
            _Req(),
            store=store,
            group_dispatcher=dispatcher,
            p=_Principal(),
        )
    finally:
        if online:
            manager.nodes.pop(node_id, None)


def _patch_cancel(monkeypatch, replies):
    """按调用次序回放节点回包。"""
    sent = []

    async def fake_request(node_id, cmd, **kwargs):
        sent.append({"node_id": node_id, "cmd": cmd, **kwargs})
        idx = len(sent) - 1
        return replies[idx] if idx < len(replies) else {
            "success": True, "cancelled": 0, "failed": [], "results": [], "remaining": 0,
        }

    monkeypatch.setattr(pending_api.node_rpc, "request", fake_request)
    return sent


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
async def test_stops_strategies_before_cancelling(store, dispatcher, monkeypatch):
    """核心不变量：先停策略再撤单。顺序颠倒策略会把挂单补回来。"""
    order = []

    async def fake_request(_node_id, _cmd, **_k):
        order.append("cancel")
        return {"success": True, "cancelled": 2, "failed": [], "results": [], "remaining": 0}

    async def fake_stop(node_id, *, signal_id, reason):
        order.append("stop")
        return {"stopped": 1, "forced": 0, "total": 1}

    monkeypatch.setattr(pending_api.node_rpc, "request", fake_request)
    monkeypatch.setattr(dispatcher, "stop_all_on_node", fake_stop)
    monkeypatch.setattr(pending_api, "_wait_subtasks_settled", _settled_now)

    res = await _call(store, dispatcher)

    assert order == ["stop", "cancel"]
    assert res["status"] == "ok"
    assert res["cancelled"] == 2
    assert res["remaining"] == 0
    assert res["rounds"] == 1
    # 停止原因必须与平仓区分开，否则前端会显示成「正常完成」
    assert res["strategies"] == {"stopped": 1, "forced": 0, "total": 1}


async def _settled_now(*_a, **_k):
    return True


async def test_sends_cancel_all_command_with_scope(store, dispatcher, monkeypatch):
    sent = _patch_cancel(monkeypatch, [
        {"success": True, "cancelled": 1, "failed": [], "results": [], "remaining": 0},
    ])
    monkeypatch.setattr(pending_api, "_wait_subtasks_settled", _settled_now)

    res = await _call(store, dispatcher, scope="manual")

    assert sent[0]["cmd"]["cmd"] == "cancel_pending_all"
    assert sent[0]["cmd"]["scope"] == "manual"
    assert res["scope"] == "manual"


async def test_lock_released_after_success(store, dispatcher, monkeypatch):
    """互斥锁必须释放，否则节点再也点不了第二次。"""
    _patch_cancel(monkeypatch, [
        {"success": True, "cancelled": 0, "failed": [], "results": [], "remaining": 0},
    ])
    monkeypatch.setattr(pending_api, "_wait_subtasks_settled", _settled_now)

    await _call(store, dispatcher)
    assert await store.acquire_cancel_pending_lock("nd_a", ttl_s=5) is True


async def test_lock_released_after_error(store, dispatcher, monkeypatch):
    """异常路径也要释放锁（finally）。"""
    _patch_cancel(monkeypatch, [
        {"success": False, "cancelled": 0, "error": "节点未在超时时间内确认"},
    ])
    monkeypatch.setattr(pending_api, "_wait_subtasks_settled", _settled_now)

    res = await _call(store, dispatcher)
    assert res["status"] == "error"
    assert await store.acquire_cancel_pending_lock("nd_a", ttl_s=5) is True


async def test_concurrent_call_rejected(store, dispatcher, monkeypatch):
    """第二个并发请求被 409 挡下（多标签页/多用户）。"""
    from fastapi import HTTPException

    _patch_cancel(monkeypatch, [
        {"success": True, "cancelled": 0, "failed": [], "results": [], "remaining": 0},
    ])
    monkeypatch.setattr(pending_api, "_wait_subtasks_settled", _settled_now)
    assert await store.acquire_cancel_pending_lock("nd_a", ttl_s=30) is True

    with pytest.raises(HTTPException) as ei:
        await _call(store, dispatcher)
    assert ei.value.status_code == 409


# ---------------------------------------------------------------------------
# 复扫与失败分支
# ---------------------------------------------------------------------------
async def test_rescans_once_and_stops(store, dispatcher, monkeypatch):
    """撤完还有残余：最多复扫一轮，共 2 次，不再无限重试。"""
    sent = _patch_cancel(monkeypatch, [
        {"success": True, "cancelled": 2, "failed": [], "results": [],
         "remaining": 1},
        {"success": True, "cancelled": 1, "failed": [], "results": [],
         "remaining": 0},
    ])
    monkeypatch.setattr(pending_api, "_wait_subtasks_settled", _settled_now)
    monkeypatch.setattr(pending_api, "RESCAN_WAIT_SEC", 0)

    res = await _call(store, dispatcher)
    assert len(sent) == 2
    assert res["rounds"] == 2
    assert res["cancelled"] == 3
    assert res["remaining"] == 0
    assert res["status"] == "ok"


async def test_status_regenerated_when_orders_come_back(store, dispatcher, monkeypatch):
    """撤了又长回来（多半是限价监听）：报 regenerated 并停止重试。"""
    _patch_cancel(monkeypatch, [
        {"success": True, "cancelled": 2, "failed": [], "results": [], "remaining": 2},
        {"success": True, "cancelled": 2, "failed": [], "results": [], "remaining": 2},
    ])
    monkeypatch.setattr(pending_api, "_wait_subtasks_settled", _settled_now)
    monkeypatch.setattr(pending_api, "RESCAN_WAIT_SEC", 0)

    res = await _call(store, dispatcher)
    assert res["status"] == "regenerated"
    assert res["regenerated"] == 2
    assert res["rounds"] == 2          # 到上限就停，不无限重试


async def test_status_partial_when_some_fail(store, dispatcher, monkeypatch):
    _patch_cancel(monkeypatch, [
        {"success": False, "cancelled": 1,
         "failed": [{"ticket": 55, "symbol": "XAUUSD", "error": "invalid price"}],
         "results": [], "remaining": 1},
        {"success": False, "cancelled": 0,
         "failed": [{"ticket": 55, "symbol": "XAUUSD", "error": "invalid price"}],
         "results": [], "remaining": 1},
    ])
    monkeypatch.setattr(pending_api, "_wait_subtasks_settled", _settled_now)
    monkeypatch.setattr(pending_api, "RESCAN_WAIT_SEC", 0)

    res = await _call(store, dispatcher)
    assert res["status"] == "partial"
    assert res["failed"]                      # 失败明细不吞
    assert res["failed"][0]["ticket"] == 55


async def test_timeout_does_not_resend(store, dispatcher, monkeypatch):
    """老版本节点不回包：超时即返回错误，不重发制造噪音。"""
    sent = _patch_cancel(monkeypatch, [
        {"success": False, "cancelled": 0, "error": "节点未在超时时间内确认"},
    ])
    monkeypatch.setattr(pending_api, "_wait_subtasks_settled", _settled_now)

    res = await _call(store, dispatcher)
    assert len(sent) == 1
    assert res["status"] == "error"
    assert "超时" in res["error"]


async def test_busy_flag_set_and_cleared(store, dispatcher, monkeypatch):
    """人工撤单窗口必须让限价监听避让，且事后清干净。"""
    seen: list[bool] = []

    async def fake_request(_node_id, _cmd, **_k):
        seen.append(await store.is_cancel_pending_busy("nd_a"))
        return {"success": True, "cancelled": 0, "failed": [], "results": [], "remaining": 0}

    monkeypatch.setattr(pending_api.node_rpc, "request", fake_request)
    monkeypatch.setattr(pending_api, "_wait_subtasks_settled", _settled_now)

    await _call(store, dispatcher)
    assert seen == [True]                            # 撤单期间窗口是打开的
    assert await store.is_cancel_pending_busy("nd_a") is False   # 事后已关


async def test_marks_cancelled_tickets_for_limit_watch(store, dispatcher, monkeypatch):
    """撤掉的 ticket 要记账：限价监听的撤单失败据此不误报。"""
    _patch_cancel(monkeypatch, [
        {"success": True, "cancelled": 1,
         "failed": [],
         "results": [{"success": True, "ticket": 7788}],
         "remaining": 0},
    ])
    monkeypatch.setattr(pending_api, "_wait_subtasks_settled", _settled_now)

    await _call(store, dispatcher)
    assert await store.is_manual_cancelled_ticket("nd_a", 7788) is True
    assert await store.is_manual_cancelled_ticket("nd_a", 9999) is False


async def test_rejects_offline_node(store, dispatcher):
    """离线节点直接 409，不进入编排。"""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        await _call(store, dispatcher, online=False)
    assert ei.value.status_code == 409
    assert dispatcher.calls == []      # 没停策略、没撤单


async def test_unknown_scope_falls_back_to_all(store, dispatcher, monkeypatch):
    """脏数据防护：未知 scope 收敛成 all（撤光），不裸传给节点。"""
    sent = _patch_cancel(monkeypatch, [
        {"success": True, "cancelled": 0, "failed": [], "results": [], "remaining": 0},
    ])
    monkeypatch.setattr(pending_api, "_wait_subtasks_settled", _settled_now)

    manager.nodes.setdefault("nd_a", object())
    try:
        res = await pending_api.cancel_pending_on_node_route(
            "nd_a",
            CancelPendingRequest(scope="../../etc/passwd"),   # 非法值
            _Req(),
            store=store,
            group_dispatcher=dispatcher,
            p=_Principal(),
        )
    finally:
        manager.nodes.pop("nd_a", None)

    assert res["scope"] == "all"
    assert sent[0]["cmd"]["scope"] == "all"