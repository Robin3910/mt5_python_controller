"""本机 local_status HTTP 服务测试。"""
from __future__ import annotations

import asyncio
import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from local_status import LocalStatusServer


def _http_json(method: str, url: str, timeout: float = 2.0) -> tuple[int, dict]:
    req = Request(url, method=method)
    try:
        with urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        body = e.read().decode("utf-8")
        return e.code, json.loads(body) if body else {}


@pytest.mark.asyncio
async def test_port_zero_does_not_listen():
    stopped = False

    def on_stop():
        nonlocal stopped
        stopped = True

    srv = LocalStatusServer(
        host="127.0.0.1",
        port=0,
        status_provider=lambda: {"ok": True, "ws_state": "starting", "uptime_s": 1},
        on_stop=on_stop,
    )
    assert not srv.enabled
    await srv.start()
    assert srv._server is None
    await srv.close()
    assert not stopped


@pytest.mark.asyncio
async def test_health_status_stop():
    state = {"ws_state": "authenticated", "uptime_s": 3.5, "node_id": 7, "runners": []}
    stopped = asyncio.Event()

    def provider():
        return {
            "ok": True,
            "process": "alive",
            "ws_state": state["ws_state"],
            "ws_connected": True,
            "uptime_s": state["uptime_s"],
            "node_id": state["node_id"],
            "mt5_login": 90000001,
            "runners": state["runners"],
        }

    def on_stop():
        stopped.set()

    srv = LocalStatusServer(
        host="127.0.0.1",
        port=0,  # bind ephemeral via start_server — need real port
        status_provider=provider,
        on_stop=on_stop,
    )
    # Use an ephemeral free port
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv.port = port

    await srv.start()
    try:
        await asyncio.sleep(0.05)
        base = f"http://127.0.0.1:{port}"

        def get_health():
            return _http_json("GET", f"{base}/health")

        code, health = await asyncio.to_thread(get_health)
        assert code == 200
        assert health["ok"] is True
        assert health["process"] == "alive"
        assert health["ws_state"] == "authenticated"

        code, status = await asyncio.to_thread(_http_json, "GET", f"{base}/status")
        assert code == 200
        assert status["node_id"] == 7
        assert status["mt5_login"] == 90000001
        assert status["runners"] == []

        code, stop_body = await asyncio.to_thread(_http_json, "POST", f"{base}/stop")
        assert code == 200
        assert stop_body["ok"] is True
        assert stopped.is_set()

        code, _ = await asyncio.to_thread(_http_json, "GET", f"{base}/nope")
        assert code == 404
    finally:
        await srv.close()


@pytest.mark.asyncio
async def test_status_snapshot_includes_runners_shape():
    """NodeClient.status_snapshot 形状由集成侧保证；此处校验 provider 透传。"""
    runners = [{
        "task_id": 1, "magic": 9, "symbol": "XAUUSD", "mode": "grid",
        "stopping": False, "finished": False, "done": False,
    }]

    srv = LocalStatusServer(
        host="127.0.0.1",
        port=1,
        status_provider=lambda: {
            "ok": True, "process": "alive", "ws_state": "authenticated",
            "uptime_s": 1, "node_id": 1, "mt5_login": 1, "runners": runners,
        },
        on_stop=lambda: None,
    )
    body = srv._health_body()
    assert body["ws_state"] == "authenticated"
    full = srv._status_provider()
    assert full["runners"][0]["task_id"] == 1
