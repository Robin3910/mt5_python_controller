"""节点卡片重命名：**只改本机显示名**，不回传后端。

需求：面板里节点卡片的名称要能编辑，但只是本机的事 —— 管理后台
「节点管理」里的节点名称保持不变。

实现要点：
  - 独立的 `display_name` 字段，只写本机 instances.json；
  - `cfg.name`（后端节点名）不动 —— 改它会被后续的 `apply_node()` 覆盖回去；
  - 清空 `display_name` 就恢复成后端的名字。

跑法：
    python -m pytest test_local_rename.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import store  # noqa: E402
from dashboard_bridge import DashboardBridge  # noqa: E402
from models import InstanceConfig  # noqa: E402
from viewmodels import build_list_item  # noqa: E402


def _bridge(tmp_path):
    """造一个带假实例的桥接层（不连后端）。"""
    from e2e_support import FakeApi, FakeSession

    bridge = DashboardBridge()
    session = FakeSession()
    session.api = FakeApi()
    bridge.session = session
    bridge.manager.session = session

    cfg = InstanceConfig.create(
        name="后端节点名", exe_path=r"C:\Mt5\prod-01\node_client.exe",
        cwd=r"C:\Mt5\prod-01",
    )
    cfg.node_id = "nd_9f21"
    cfg.mt5_login = 50231488
    bridge.manager.add(cfg)
    bridge._inventory[cfg.id] = cfg
    # 真实流程里由 `_load_permitted_instances()` 置位；没有它 `_persist()` 会跳过落盘。
    bridge._inventory_loaded = True
    return bridge, cfg


# ─────────────────────────────────────────────── 模型

def test_display_name_defaults_to_empty_and_label_falls_back_to_name():
    """没改名时 `label` 就是后端节点名。"""
    cfg = InstanceConfig.create(name="后端节点名", exe_path=r"C:\Mt5\a\node_client.exe")
    assert cfg.display_name == ""
    assert cfg.label == "后端节点名"


def test_display_name_roundtrips_through_dict():
    """改名要能存进 instances.json 再读回来。"""
    cfg = InstanceConfig.create(name="后端节点名", exe_path=r"C:\Mt5\a\node_client.exe")
    cfg.display_name = "我的小号"
    again = InstanceConfig.from_dict(cfg.to_dict())
    assert again.display_name == "我的小号", again.display_name
    assert again.label == "我的小号", again.label


# ─────────────────────────────────────────────── 桥接层

def test_rename_updates_local_display_name_only(tmp_path):
    """重命名只改本机显示名，`cfg.name`（后端节点名）保持不变。"""
    original_dir = store.data_dir
    store.data_dir = lambda: tmp_path
    bridge, cfg = _bridge(tmp_path)
    try:
        result = bridge.rename_instance(cfg.id, "  我的小号  ")
        assert result["ok"] is True
        assert result["name"] == "我的小号", result
        assert cfg.display_name == "我的小号", cfg.display_name
        assert cfg.name == "后端节点名", "后端节点名不该被本机改名影响"

        # 落盘的是本机清单，重开面板还在
        saved = json.loads((tmp_path / "instances.json").read_text(encoding="utf-8"))
        entry = saved["instances"][0]
        assert entry["display_name"] == "我的小号", entry
        assert entry["name"] == "后端节点名", entry
    finally:
        store.data_dir = original_dir
        bridge.stop()


def test_rename_with_empty_name_restores_backend_name(tmp_path):
    """清空名字 = 恢复成后端节点名。"""
    original_dir = store.data_dir
    store.data_dir = lambda: tmp_path
    bridge, cfg = _bridge(tmp_path)
    try:
        bridge.rename_instance(cfg.id, "我的小号")
        assert cfg.label == "我的小号"

        result = bridge.rename_instance(cfg.id, "   ")
        assert result["name"] == "后端节点名", result
        assert cfg.display_name == "", cfg.display_name
        assert cfg.label == "后端节点名", cfg.label
    finally:
        store.data_dir = original_dir
        bridge.stop()


def test_rename_rejects_too_long_name(tmp_path):
    """超长名字直接拒掉，不改任何东西。"""
    original_dir = store.data_dir
    store.data_dir = lambda: tmp_path
    bridge, cfg = _bridge(tmp_path)
    try:
        with pytest.raises(Exception) as exc:
            bridge.rename_instance(cfg.id, "x" * 65)
        assert "64" in str(exc.value), exc.value
        assert cfg.display_name == "", cfg.display_name
    finally:
        store.data_dir = original_dir
        bridge.stop()


def test_rename_unknown_instance_is_404(tmp_path):
    """实例不存在时返回 404。"""
    original_dir = store.data_dir
    store.data_dir = lambda: tmp_path
    bridge, _cfg = _bridge(tmp_path)
    try:
        with pytest.raises(Exception) as exc:
            bridge.rename_instance("no-such-id", "名字")
        assert getattr(exc.value, "status", 0) == 404, exc.value
    finally:
        store.data_dir = original_dir
        bridge.stop()


def test_rename_never_touches_the_backend(tmp_path):
    """改名是**纯本机**动作：不向后端发任何请求。

    `session.perform()` 会向后端申请审计票据 —— 那等于把「本机显示名」
    记到服务器上，而且断网时连改名都做不了。所以这里刻意绕开它。
    """
    original_dir = store.data_dir
    store.data_dir = lambda: tmp_path
    bridge, cfg = _bridge(tmp_path)

    calls: list[tuple] = []

    def _authorize(token, payload):
        calls.append(("authorize", payload))
        raise AssertionError("改名不该向后端申请审计票据")

    def _result(token, operation_id, payload):
        calls.append(("result", payload))

    bridge.session.api.authorize = _authorize  # type: ignore[attr-defined]
    bridge.session.api.result = _result  # type: ignore[attr-defined]
    try:
        result = bridge.rename_instance(cfg.id, "我的小号")
        assert result["ok"] is True, result
        assert result["name"] == "我的小号", result
        assert cfg.display_name == "我的小号", cfg.display_name
        assert calls == [], f"改名不该有网络请求，实际 {calls}"
    finally:
        store.data_dir = original_dir
        bridge.stop()


# ─────────────────────────────────────────────── 界面

def test_card_title_prefers_local_display_name(tmp_path):
    """卡片标题用本机自定义名。"""
    cfg = InstanceConfig.create(name="后端节点名", exe_path=r"C:\Mt5\a\node_client.exe")
    cfg.display_name = "我的小号"

    class _MP:
        pass

    mp = _MP()
    mp.cfg = cfg
    mp.runtime = type("_R", (), {
        "busy": "", "process_alive": False, "health": "dead", "version": "",
        "node_id": "nd_9f21", "mt5_login": 50231488, "ws_state": None,
        "uptime_s": None, "algo_trading_allowed": None, "runners": [],
        "last_error": "", "pid": None,
    })()

    vm = build_list_item(mp, None, None)
    assert vm.title == "我的小号", vm.title


# ─────────────────────────────────────────────── HTTP 接口

def test_rename_endpoint_over_http(tmp_path):
    """走真实的 HTTP 接口改名：接口能用，标题同步更新。"""
    import urllib.request

    from e2e_support import FakeApi, FakeSession

    original_dir = store.data_dir
    store.data_dir = lambda: tmp_path

    bridge = DashboardBridge()
    session = FakeSession()
    session.api = FakeApi()
    bridge.session = session
    bridge.manager.session = session

    cfg = InstanceConfig.create(
        name="后端节点名", exe_path=r"C:\Mt5\prod-01\node_client.exe",
        cwd=r"C:\Mt5\prod-01",
    )
    cfg.node_id = "nd_9f21"
    bridge.manager.add(cfg)
    bridge._inventory[cfg.id] = cfg
    bridge._inventory_loaded = True
    bridge.start()
    try:
        url = f"http://127.0.0.1:{bridge.port}/api/instance/rename?token={bridge.token}"
        data = json.dumps({"id": cfg.id, "name": "我的小号"}).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        assert payload["ok"] is True, payload
        assert payload["name"] == "我的小号", payload
        assert cfg.display_name == "我的小号", cfg.display_name

        # 快照里的卡片标题也跟着变
        snapshot = bridge.snapshot()
        assert snapshot is not None
        titles = [n["title"] for n in snapshot["nodes"]]
        assert "我的小号" in titles, titles
    finally:
        bridge.stop()
        store.data_dir = original_dir
