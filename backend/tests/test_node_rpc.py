"""节点 RPC 公共层：req_id 配对 / 离线 / 超时 / 回包异常。"""
import asyncio

import pytest

from app import node_rpc
from app.connections import manager


@pytest.fixture(autouse=True)
def _clean():
    node_rpc.reset_waiters()
    yield
    node_rpc.reset_waiters()


async def test_request_resolves_on_reply(monkeypatch):
    sent = []

    async def fake_send(node_id, cmd):
        sent.append((node_id, cmd))
        # 回包走真实链路：node_rpc.resolve
        node_rpc.resolve(cmd["req_id"], {"success": True, "cancelled": 3, "remaining": 0})
        return True

    monkeypatch.setattr(manager, "send_to_node", fake_send)

    payload = await node_rpc.request("nd_a", {"cmd": "cancel_pending_all", "scope": "all"})
    assert payload["success"] is True
    assert payload["cancelled"] == 3
    assert len(sent) == 1
    assert sent[0][0] == "nd_a"
    assert sent[0][1]["cmd"] == "cancel_pending_all"
    assert sent[0][1]["req_id"]            # req_id 由本模块注入
    assert node_rpc.pending_count() == 0   # future 已回收


async def test_request_returns_error_when_node_offline(monkeypatch):
    async def fake_send(_node_id, _cmd):
        return False

    monkeypatch.setattr(manager, "send_to_node", fake_send)

    payload = await node_rpc.request(
        "nd_off", {"cmd": "cancel_pending_all"}, offline_error="节点离线无法撤单",
    )
    assert payload["success"] is False
    assert payload["error"] == "节点离线无法撤单"
    assert node_rpc.pending_count() == 0


async def test_request_times_out_without_reply(monkeypatch):
    async def fake_send(_node_id, _cmd):
        return True      # 发得出去，但节点永远不回包（老版本客户端不认识该命令）

    monkeypatch.setattr(manager, "send_to_node", fake_send)

    payload = await node_rpc.request(
        "nd_a", {"cmd": "cancel_pending_all"}, timeout=0.05,
        timeout_error="节点未响应",
    )
    assert payload["success"] is False
    assert payload["error"] == "节点未响应"
    assert node_rpc.pending_count() == 0   # 超时后 future 不泄漏


async def test_resolve_ignores_unknown_req_id():
    """回包先到 / req_id 对不上：不得抛异常。"""
    node_rpc.resolve("nobody-waiting", {"success": True})
    assert node_rpc.pending_count() == 0


async def test_request_recovers_after_timeout_then_late_reply(monkeypatch):
    """超时后再来的回包不得唤醒已超时的等待项（也不会炸）。"""
    holder = {}

    async def fake_send(_node_id, cmd):
        holder["req_id"] = cmd["req_id"]
        return True

    monkeypatch.setattr(manager, "send_to_node", fake_send)

    first = await node_rpc.request(
        "nd_a", {"cmd": "cancel_pending_all"}, timeout=0.05,
    )
    assert first["success"] is False

    node_rpc.resolve(holder["req_id"], {"success": True})   # 迟到的回包
    assert node_rpc.pending_count() == 0


async def test_two_requests_pair_independently(monkeypatch):
    """并发两条命令按 req_id 各自唤醒，互不串台。"""
    order: list[str] = []

    async def fake_send(node_id, cmd):
        order.append(cmd["req_id"])
        if len(order) == 1:   # 第一条先回，携带自己的标识
            node_rpc.resolve(cmd["req_id"], {"success": True, "who": 1})
        return True

    monkeypatch.setattr(manager, "send_to_node", fake_send)

    t1 = asyncio.create_task(node_rpc.request("nd_a", {"cmd": "a"}, timeout=1))
    t2 = asyncio.create_task(node_rpc.request("nd_a", {"cmd": "b"}, timeout=1))
    # 第二条此刻还没发出去（上面只在第一次 send 时回包），手动补回包
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    for req in order[1:]:
        node_rpc.resolve(req, {"success": True, "who": 2})

    r1, r2 = await asyncio.gather(t1, t2)
    assert r1["who"] == 1
    assert r2["who"] == 2
    assert node_rpc.pending_count() == 0