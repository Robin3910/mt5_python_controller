"""跨客户端构建、面板安装与会话边界的安全回归，不启动真实进程或访问网络。"""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from urllib.error import URLError

import pytest

from auth_service import DashboardAuthError
from client_binding import binding_supported, require_binding
import client_deploy as deploy
from client_deploy import (
    apply_package, backup_dir_for, extract_package, replace_exe_file,
    restore_backup, source_bundle_files,
)
import dashboard_session as sessions
from env_policy import protected_changes, redact_env
from models import InstanceConfig
import process_manager as processes
from process_manager import ManagedProcess, ProcessManager


def _capable_client(root: Path, version: str, binary: bytes) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    exe = root / "node_client.exe"
    exe.write_bytes(binary)
    (root / "version.txt").write_text(version + "\n", encoding="utf-8")
    (root / "client_capabilities.json").write_text(json.dumps({
        "account_binding": True,
        "executable_sha256": hashlib.sha256(binary).hexdigest(),
    }), encoding="utf-8")
    return exe


def test_client_build_manifest_round_trip_to_dashboard_install(tmp_path):
    source = Path(__file__).resolve().parent.parent / "node_client" / "build_package.py"
    spec = importlib.util.spec_from_file_location("node_build_protocol", source)
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    exe = _capable_client(tmp_path / "build", "2.0.0", b"MZ new client")
    archive = builder.build_package(exe.parent, tmp_path / "archives")
    unpacked = tmp_path / "unpacked"
    assert extract_package(archive, unpacked)[0]
    target = _capable_client(tmp_path / "install", "1.0.0", b"MZ old capable client")
    identity = target.parent / ".env"
    identity.write_text("NODE_TOKEN=per-node-example\n", encoding="utf-8")

    assert apply_package(unpacked, target)[0]
    assert binding_supported(target)
    assert identity.read_text(encoding="utf-8") == "NODE_TOKEN=per-node-example\n"
    assert restore_backup(target, "1.0.0")[0]
    assert binding_supported(target)
    assert target.read_bytes() == b"MZ old capable client"
    assert (backup_dir_for(target, "2.0.0") / "client_capabilities.json").is_file()


def test_direct_exe_replacement_copies_manifest_and_preserves_identity(tmp_path):
    source = _capable_client(tmp_path / "source", "2.0.0", b"MZ source")
    target = _capable_client(tmp_path / "target", "1.0.0", b"MZ target")
    (target.parent / ".env").write_text("NODE_TOKEN=keep-example\n", encoding="utf-8")
    assert {p.name for p in source_bundle_files(source)} == {
        "node_client.exe", "version.txt", "client_capabilities.json",
    }
    assert replace_exe_file(source_exe=source, target_exe=target)[0]
    assert binding_supported(target)
    assert (target.parent / ".env").read_text(encoding="utf-8") == "NODE_TOKEN=keep-example\n"


def _installed_contents(directory):
    directory = Path(directory)
    return {path.relative_to(directory).as_posix(): path.read_bytes()
            for path in directory.rglob("*")
            if path.is_file() and path.relative_to(directory).parts[0] != ".backups"}


@pytest.mark.parametrize("failure_number", [2, 3, 5])
def test_package_copy_failure_restores_entire_changed_tree(tmp_path, monkeypatch, failure_number):
    target = _capable_client(tmp_path / "install", "1.0.0", b"MZ old bound client")
    (target.parent / "sub").mkdir()
    (target.parent / "sub" / "lib.dll").write_bytes(b"old installed library")
    (target.parent / ".env").write_text("NODE_TOKEN=synthetic-protected-identity\n", encoding="utf-8")
    before = _installed_contents(target.parent)
    source = _capable_client(tmp_path / "source", "2.0.0", b"MZ new bound client")
    (source.parent / "a-new.dll").write_bytes(b"new file absent from old installation")
    (source.parent / "sub").mkdir()
    (source.parent / "sub" / "lib.dll").write_bytes(b"new library")
    (source.parent / ".env").write_text("NODE_TOKEN=synthetic-package-identity-must-not-copy\n", encoding="utf-8")
    original = deploy._copy_with_retry
    counter = 0
    def fail_during_copy(src, dst):
        nonlocal counter
        if src.is_relative_to(source.parent):
            counter += 1
            if counter == failure_number:
                dst.write_bytes(b"partial failed write")
                raise OSError("synthetic-package-copy-failure")
        original(src, dst)
    monkeypatch.setattr(deploy, "_copy_with_retry", fail_during_copy)
    ok, message = apply_package(source.parent, target)
    assert not ok and "已恢复" in message
    assert _installed_contents(target.parent) == before
    assert binding_supported(target)
    assert not (target.parent / "a-new.dll").exists()
    assert (backup_dir_for(target, "1.0.0") / "node_client.exe").read_bytes() == b"MZ old bound client"


@pytest.mark.parametrize("failure_number", [2, 3, 4])
def test_direct_replacement_failure_restores_exe_sidecars_and_existing_backup(tmp_path, monkeypatch, failure_number):
    target = _capable_client(tmp_path / "install", "1.0.0", b"MZ old bound client")
    target.with_suffix(".exe.bak").write_bytes(b"older existing backup")
    (target.parent / ".env").write_text("NODE_TOKEN=synthetic-protected-identity\n", encoding="utf-8")
    before = _installed_contents(target.parent)
    source = _capable_client(tmp_path / "source", "2.0.0", b"MZ new bound client")
    original = deploy._copy_with_retry
    counter = 0
    def fail_during_copy(src, dst):
        nonlocal counter
        if dst.parent == target.parent and not src.parent.name.startswith("node_client_restore_"):
            counter += 1
            if counter == failure_number:
                dst.write_bytes(b"partial failed write")
                raise OSError("synthetic-exe-copy-failure")
        original(src, dst)
    monkeypatch.setattr(deploy, "_copy_with_retry", fail_during_copy)
    ok, message = replace_exe_file(source_exe=source, target_exe=target)
    assert not ok and "已恢复" in message
    assert _installed_contents(target.parent) == before
    assert binding_supported(target)


def test_direct_replacement_failure_removes_new_backup_file(tmp_path, monkeypatch):
    target = _capable_client(tmp_path / "install", "1.0.0", b"MZ old bound client")
    source = _capable_client(tmp_path / "source", "2.0.0", b"MZ new bound client")
    original = deploy._copy_with_retry
    def fail_exe(src, dst):
        if src == source and dst == target:
            dst.write_bytes(b"partial failed write")
            raise OSError("synthetic-exe-copy-failure")
        original(src, dst)
    monkeypatch.setattr(deploy, "_copy_with_retry", fail_exe)
    assert not replace_exe_file(source_exe=source, target_exe=target)[0]
    assert not target.with_suffix(".exe.bak").exists()
    assert binding_supported(target)


def test_recovery_failure_keeps_original_snapshot_for_manual_repair(tmp_path, monkeypatch):
    target = _capable_client(tmp_path / "install", "1.0.0", b"MZ old bound client")
    (target.parent / ".env").write_text("NODE_TOKEN=synthetic-protected-identity\n", encoding="utf-8")
    source = _capable_client(tmp_path / "source", "2.0.0", b"MZ new bound client")
    snapshot_root = tmp_path / "temporary"
    snapshot_root.mkdir()
    monkeypatch.setattr(deploy.tempfile, "gettempdir", lambda: str(snapshot_root))
    original = deploy._copy_with_retry
    def fail_and_refuse_recovery(src, dst):
        if src == source and dst == target:
            dst.write_bytes(b"partial failed write")
            raise OSError("synthetic-copy-failure")
        if src.parent.name.startswith("node_client_restore_") and dst == target:
            raise OSError("synthetic-recovery-file-lock")
        original(src, dst)
    monkeypatch.setattr(deploy, "_copy_with_retry", fail_and_refuse_recovery)
    ok, message = replace_exe_file(source_exe=source, target_exe=target)
    assert not ok and "恢复失败" in message
    retained = list(snapshot_root.glob("node_client_restore_*"))
    assert len(retained) == 1 and str(retained[0]) in message
    mapping = json.loads((retained[0] / "recovery.json").read_text(encoding="utf-8"))
    assert Path(mapping[str(target)]).read_bytes() == b"MZ old bound client"
    assert not any(Path(path).name == ".env" for path in mapping)
    assert (target.parent / ".env").read_text(encoding="utf-8") == "NODE_TOKEN=synthetic-protected-identity\n"


@pytest.mark.parametrize("method", ["package", "exe"])
def test_legacy_replacement_cannot_inherit_supported_manifest(tmp_path, method):
    target = _capable_client(tmp_path / "target", "2.0.0", b"MZ new")
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    source = legacy / "node_client.exe"
    source.write_bytes(b"MZ old without account binding")
    if method == "package":
        assert apply_package(legacy, target)[0]
    else:
        assert replace_exe_file(source_exe=source, target_exe=target)[0]
    assert not (target.parent / "client_capabilities.json").exists()
    with pytest.raises(RuntimeError, match="不支持安全账户绑定"):
        require_binding(target)


def test_manifest_hash_and_fixed_development_entry_are_required(tmp_path):
    executable = _capable_client(tmp_path / "client", "1.0.0", b"MZ verified")
    executable.write_bytes(b"MZ substituted executable")
    assert not binding_supported(executable)
    arbitrary = tmp_path / "node_client.py"
    arbitrary.write_text("ACCOUNT_BINDING_SUPPORTED = True\n", encoding="utf-8")
    assert not binding_supported(arbitrary)
    trusted = Path(__file__).resolve().parent.parent / "node_client" / "node_client.py"
    assert binding_supported(trusted)


@pytest.mark.parametrize("assignment", ["node_token", "export NODE_TOKEN", "export node_token", "'NODE_TOKEN'"])
def test_raw_env_protection_matches_case_insensitive_dotenv_keys(assignment):
    before = f"{assignment}=old-example-secret\nWATCH_SYMBOLS=XAUUSD\n"
    after = f"{assignment}=new-example-secret\nWATCH_SYMBOLS=XAUUSD\n"
    assert "old-example-secret" not in redact_env(before)
    assert "NODE_TOKEN" in protected_changes(before, after)


class _API:
    base = "https://example.test"

    def __init__(self):
        self.nodes_value = [
            {"node_id": "nd_own", "mt5_login": 12345, "owner_user_id": 7,
             "approval_status": "approved", "enabled": True, "credential_generation": 1},
            {"node_id": "nd_other", "mt5_login": 54321, "owner_user_id": 8,
             "approval_status": "approved", "enabled": True, "credential_generation": 1},
        ]
        self.check_error = None
        self.authorize = MagicMock(return_value={"operation_id": 1})
        self.result = MagicMock(return_value={"ok": True})
        self.daemon_event = MagicMock(return_value={"ok": True})

    def me(self, _token):
        return {"user_id": 7, "username": "example-user", "is_admin": False, "menus": ["nodes"]}

    def nodes(self, _token):
        return self.nodes_value

    def check_grant(self, _token):
        if self.check_error:
            raise self.check_error
        return {"ok": True, "node_id": "nd_own", "mt5_login": 12345, "generation": 1}


@pytest.fixture
def active_session(monkeypatch):
    monkeypatch.setattr(sessions, "load_audit_queue", lambda: [])
    monkeypatch.setattr(sessions, "save_audit_queue", lambda _records: None)
    events = []
    monkeypatch.setattr(sessions, "load_daemon_events", lambda: list(events))
    monkeypatch.setattr(sessions, "save_daemon_events", lambda records: events.__setitem__(slice(None), records))
    session = sessions.DashboardSession()
    api = _API()
    session.establish(api, "example-access-jwt")
    return session, api


def _managed(session, root: Path, node_id="nd_own", login=12345):
    exe = _capable_client(root, "1.0.0", b"MZ capable")
    cfg = InstanceConfig.create(exe_path=str(exe), cwd=str(root), status_port=18765)
    cfg.node_id, cfg.mt5_login = node_id, login
    cfg.enabled, cfg.approval_status = True, "approved"
    mp = ManagedProcess(cfg)
    mp.dashboard_managed = True
    manager = ProcessManager(session=session)
    manager._items[cfg.id] = mp
    return manager, mp


@pytest.mark.parametrize("action", ["start", "stop", "remove", "set_daemon"])
def test_direct_manager_calls_cannot_bypass_locked_session(active_session, tmp_path, action):
    session, api = active_session
    manager, mp = _managed(session, tmp_path)
    invoked = MagicMock()
    mp.start = mp.stop = mp.set_daemon = invoked
    session.lock("已退出登录")
    args = [mp.cfg.id, True] if action == "set_daemon" else [mp.cfg.id]
    with pytest.raises(DashboardAuthError):
        getattr(manager, action)(*args)
    invoked.assert_not_called()
    api.authorize.assert_not_called()


def test_foreign_node_is_hidden_and_direct_stop_is_denied(active_session, tmp_path):
    session, _api = active_session
    manager, mp = _managed(session, tmp_path, "nd_other", 54321)
    stopped = MagicMock()
    mp.stop = stopped
    with pytest.raises(DashboardAuthError, match="无权"):
        manager.stop(mp.cfg.id)
    stopped.assert_not_called()
    manager.set_visible(set())
    assert manager.configs() == []
    assert manager.get(mp.cfg.id) is None
    manager.stop(mp.cfg.id)
    stopped.assert_not_called()


@pytest.mark.parametrize("locked", [False, True])
def test_direct_manager_load_cannot_recover_unpermitted_instances(active_session, tmp_path, monkeypatch, locked):
    session, api = active_session
    cfg = InstanceConfig.create(exe_path=str(tmp_path / "node_client.exe"), cwd=str(tmp_path))
    cfg.node_id, cfg.mt5_login = "nd_other", 54321
    cfg.approval_status, cfg.enabled = "pending", False
    if locked:
        cfg.node_id, cfg.mt5_login = "nd_own", 12345
        session.lock("已退出登录", clear_token=True)
    recovered = MagicMock()
    monkeypatch.setattr(ManagedProcess, "recover", recovered)
    manager = ProcessManager(session=session)
    with pytest.raises(DashboardAuthError):
        manager.load([cfg])
    assert manager._items == {}
    recovered.assert_not_called()


def test_original_applicant_cannot_control_an_approved_transferred_node(active_session, tmp_path):
    session, api = active_session
    api.nodes_value[1]["requested_by_user_id"] = 7
    manager, mp = _managed(session, tmp_path, "nd_other", 54321)
    stopped = MagicMock()
    mp.stop = stopped
    with pytest.raises(DashboardAuthError, match="无权"):
        manager.stop(mp.cfg.id)
    stopped.assert_not_called()


@pytest.mark.parametrize("status_node_id,visible", [("nd_unknown", False), (None, True)])
def test_legacy_account_match_only_falls_back_when_status_node_id_is_missing(active_session, tmp_path, monkeypatch, status_node_id, visible):
    import dashboard_auth_ui

    session, api = active_session
    api.nodes_value[0]["enabled"] = False
    cfg = InstanceConfig.create(exe_path=str(tmp_path / "node_client.exe"), cwd=str(tmp_path), status_port=19001)
    manager = ProcessManager(session=session)
    def load_without_workers(configs):
        for item in configs:
            manager._items[item.id] = ManagedProcess(item)
    monkeypatch.setattr(manager, "load", load_without_workers)
    owner = SimpleNamespace(session=session, manager=manager, _inventory={}, _persist=MagicMock())
    monkeypatch.setattr(dashboard_auth_ui, "load_instances", lambda: [cfg])
    monkeypatch.setattr(dashboard_auth_ui, "_http_json", lambda *_args, **_kwargs: (
        200, {"node_id": status_node_id, "mt5_login": 12345, "process": "alive"},
    ))
    dashboard_auth_ui.DashboardAuthUI._load_permitted_instances(owner)
    assert bool(manager.configs()) is visible
    assert cfg.node_id == ("nd_own" if visible else None)


def test_first_credential_issue_does_not_confuse_unissued_generation_with_loss(active_session, tmp_path):
    session, api = active_session
    _manager, mp = _managed(session, tmp_path)
    api.base = "https://example.test"
    api.nodes_value[0]["has_credential"] = False
    api.credential = MagicMock(return_value={
        "token": "ndv1.nd_own.example-secret", "generation": 2, "node_id": "nd_own", "mt5_login": 12345,
    })
    api.daemon_grant = MagicMock(return_value={
        "grant_token": "example-daemon-proof", "node_id": "nd_own", "mt5_login": 12345, "generation": 2,
    })
    session.prepare_start(mp)
    api.credential.assert_called_once_with("example-access-jwt", "issue", {"node_id": "nd_own"})
    env = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "NODE_TOKEN=ndv1.nd_own.example-secret" in env
    assert "DASHBOARD_EXPECTED_MT5_LOGIN=12345" in env


@pytest.mark.parametrize("action", ["replace", "update", "rollback"])
def test_manager_rejects_downgrade_without_capability_before_stopping(active_session, tmp_path, action):
    session, api = active_session
    manager, mp = _managed(session, tmp_path / "installed")
    mp.runtime.process_alive = True
    stopped = MagicMock()
    mp.stop = stopped
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    old_exe = legacy / "node_client.exe"
    old_exe.write_bytes(b"MZ unbound legacy")
    if action == "rollback":
        old = backup_dir_for(mp.cfg.exe_path, "0.9.0")
        old.mkdir(parents=True)
        (old / "node_client.exe").write_bytes(b"MZ unbound legacy")
        invoke = lambda: manager.batch_rollback("0.9.0")
    elif action == "replace":
        invoke = lambda: manager.batch_replace(old_exe)
    else:
        invoke = lambda: manager.batch_update(legacy)
    with pytest.raises(RuntimeError, match="不支持安全账户绑定"):
        invoke()
    stopped.assert_not_called()
    api.authorize.assert_not_called()


@pytest.mark.parametrize("action", ["replace", "update"])
def test_batch_file_failure_restores_first_instance_and_skips_next(active_session, tmp_path, monkeypatch, action):
    session, api = active_session
    manager, first = _managed(session, tmp_path / "first")
    _other_manager, second = _managed(session, tmp_path / "second")
    manager._items[second.cfg.id] = second
    source = _capable_client(tmp_path / "source", "2.0.0", b"MZ updated client")
    before_first, before_second = _installed_contents(first.cfg.cwd), _installed_contents(second.cfg.cwd)
    monkeypatch.setattr(first, "assert_identity", lambda: None)
    monkeypatch.setattr(first, "_status_reachable", lambda **_kwargs: False)
    monkeypatch.setattr(second, "assert_identity", lambda: None)
    monkeypatch.setattr(second, "_status_reachable", lambda **_kwargs: False)
    original = deploy._copy_with_retry
    def fail_first_exe(src, dst):
        if src == source and dst == Path(first.cfg.exe_path):
            dst.write_bytes(b"partial failed write")
            raise OSError("synthetic-first-instance-copy-failure")
        original(src, dst)
    monkeypatch.setattr(deploy, "_copy_with_retry", fail_first_exe)
    results = manager.batch_replace(source) if action == "replace" else manager.batch_update(source.parent)
    assert len(results) == 1 and not results[0].ok
    assert _installed_contents(Path(first.cfg.cwd)) == before_first
    assert _installed_contents(Path(second.cfg.cwd)) == before_second
    assert api.authorize.call_count == 1
    assert api.result.call_args.args[2]["result"] == "fail"


def test_reused_status_port_cannot_stop_another_account(active_session, tmp_path, monkeypatch):
    session, _api = active_session
    manager, mp = _managed(session, tmp_path)
    calls = []
    def response(method, _url, **_kwargs):
        calls.append(method)
        return 200, {"process": "alive", "pid": 43210, "node_id": "nd_other", "mt5_login": 54321}
    monkeypatch.setattr(processes, "_http_json", response)
    killed = MagicMock()
    monkeypatch.setattr(mp, "_force_kill_pid", killed)
    with pytest.raises(RuntimeError, match="身份"):
        manager.stop(mp.cfg.id)
    assert calls == ["GET"]
    killed.assert_not_called()


def test_adopted_process_cannot_stop_without_current_status_identity(active_session, tmp_path, monkeypatch):
    session, _api = active_session
    manager, mp = _managed(session, tmp_path)
    mp.runtime.pid = 43210
    mp.runtime.process_alive = True
    calls = []

    def response(method, _url, **_kwargs):
        calls.append(method)
        if method == "GET":
            raise URLError("status timeout")
        return 200, {"ok": True}

    monkeypatch.setattr(processes, "_http_json", response)
    killed = MagicMock()
    monkeypatch.setattr(mp, "_force_kill_pid", killed)
    with pytest.raises(RuntimeError, match="身份|确认|状态"):
        manager.stop(mp.cfg.id)
    assert "POST" not in calls
    killed.assert_not_called()


def test_stop_never_force_kills_stale_reused_pid(active_session, tmp_path, monkeypatch):
    session, _api = active_session
    manager, mp = _managed(session, tmp_path)
    mp.runtime.pid = 11111
    mp.runtime.process_alive = True
    monkeypatch.setattr(processes, "_http_json", lambda *_args, **_kwargs: (
        200, {"process": "alive", "pid": 22222, "node_id": "nd_own", "mt5_login": 12345},
    ))
    clock_values = iter([0, 4])
    monkeypatch.setattr(processes.time, "time", lambda: next(clock_values, 4))
    killed = MagicMock()
    monkeypatch.setattr(mp, "_force_kill_pid", killed)
    manager.stop(mp.cfg.id)
    assert all(call.args[0] != 11111 for call in killed.call_args_list)


def test_remove_identity_failure_preserves_registered_instance(active_session, tmp_path, monkeypatch):
    session, _api = active_session
    manager, mp = _managed(session, tmp_path)
    monkeypatch.setattr(processes, "_http_json", lambda *_args, **_kwargs: (
        200, {"process": "alive", "pid": 54321, "node_id": "nd_other", "mt5_login": 54321},
    ))
    with pytest.raises(RuntimeError, match="身份"):
        manager.remove(mp.cfg.id)
    assert manager.get(mp.cfg.id) is mp


def test_swap_checks_original_session_before_copy_after_stop(active_session, tmp_path, monkeypatch):
    session, _api = active_session
    manager, mp = _managed(session, tmp_path)
    mp.runtime.process_alive = True
    session._local.generation = session.generation
    monkeypatch.setattr(processes.time, "sleep", lambda _delay: None)
    monkeypatch.setattr(mp, "assert_identity", lambda: None)
    monkeypatch.setattr(mp, "stop", lambda **_kwargs: session.lock("操作中会话已变化"))
    apply = MagicMock(return_value=(True, "ok"))
    try:
        with pytest.raises(DashboardAuthError, match="会话"):
            manager._swap(mp, apply, restart_if_was_running=False, ok_label="已更新")
    finally:
        session._local.generation = None
    apply.assert_not_called()


def test_old_worker_callback_uses_worker_generation_after_new_login(active_session, monkeypatch):
    from app_ui import DashboardApp

    session, api = active_session
    app = object.__new__(DashboardApp)
    app.session, app._quitting = session, False
    scheduled = []
    monkeypatch.setattr("app_ui.ctk.CTk.after", lambda _self, _ms, fn, *_args: scheduled.append(fn) or "timer")
    old_generation = session.generation
    session.lock("退出登录", clear_token=True)
    session.establish(api, "example-new-access-jwt")
    show_old_user_result = MagicMock()
    session._local.generation = old_generation
    try:
        app.after(0, show_old_user_result)
    finally:
        session._local.generation = None
    scheduled[0]()
    show_old_user_result.assert_not_called()


def test_legacy_running_status_without_node_id_can_match_its_mt5_account(active_session, tmp_path, monkeypatch):
    session, _api = active_session
    _manager, mp = _managed(session, tmp_path)
    monkeypatch.setattr(processes, "_http_json", lambda *_args, **_kwargs: (
        200, {"process": "alive", "pid": 43210, "node_id": None, "mt5_login": 12345},
    ))
    monkeypatch.setattr(mp, "_ensure_workers", lambda: None)
    spawn = MagicMock()
    monkeypatch.setattr(mp, "_spawn", spawn)
    mp.start()
    spawn.assert_not_called()
    assert mp.runtime.process_alive and mp.runtime.pid == 43210


def test_existing_daemon_grant_survives_logout_and_network_loss_but_revokes_online(active_session):
    session, api = active_session
    grant = sessions.DaemonGrant(api, {
        "grant_token": "example-daemon-proof", "node_id": "nd_own", "mt5_login": 12345, "generation": 1,
    })
    session.lock("已退出登录")
    assert grant.check()
    api.check_error = DashboardAuthError("offline")
    restarted = MagicMock()
    assert grant.restart(restarted)
    restarted.assert_called_once()
    api.check_error = DashboardAuthError("节点已禁用或转移", status=403)
    assert not grant.restart(restarted)
    assert restarted.call_count == 1
    api.check_error = None
    assert not grant.check(), "已撤销的守护不得因恢复启用而自行复活"


def test_online_revocation_withdraws_guard_without_stopping_live_process(active_session, tmp_path, monkeypatch):
    session, api = active_session
    _manager, mp = _managed(session, tmp_path)
    mp.cfg.daemon = True
    mp._want_running = True
    mp.daemon_grant = sessions.DaemonGrant(api, {
        "grant_token": "example-daemon-proof", "node_id": "nd_own", "mt5_login": 12345, "generation": 1,
    })
    api.check_error = DashboardAuthError("撤回守护授权", status=403)
    monkeypatch.setattr(processes, "_http_json", lambda *_args, **_kwargs: (
        200, {"process": "alive", "pid": 43210, "node_id": "nd_own", "mt5_login": 12345},
    ))
    stop = MagicMock()
    mp.stop = stop
    session.lock("已退出登录", clear_token=True)
    mp._poll_once()
    assert not mp.cfg.daemon
    assert mp._want_running and mp.runtime.process_alive
    stop.assert_not_called()


def test_managed_spawn_failure_is_reported_as_failed_daemon_event(active_session, tmp_path, monkeypatch):
    _session, api = active_session
    _manager, mp = _managed(_session, tmp_path)
    monkeypatch.setattr(mp, "assert_identity", lambda: None)
    monkeypatch.setattr(processes, "DailyLogWriter", MagicMock())
    monkeypatch.setattr(processes.subprocess, "Popen", MagicMock(side_effect=OSError("synthetic-process-start-failure")))
    grant = sessions.DaemonGrant(api, {
        "grant_token": "example-daemon-proof", "node_id": "nd_own", "mt5_login": 12345,
        "generation": 1, "grant_operation_id": 101, "actor_user_id": 7,
    })
    with pytest.raises(RuntimeError, match="未启动"):
        grant.restart(mp._spawn)
    assert not mp.runtime.process_alive and mp._proc is None
    assert [call.args[1]["phase"] for call in api.daemon_event.call_args_list] == ["intent", "result"]
    assert api.daemon_event.call_args.args[1]["result"] == "fail"
