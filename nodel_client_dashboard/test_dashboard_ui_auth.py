"""真实 Tk 登录与实例归属回归；全部数据与进程均隔离，不连接真实后台。"""
from __future__ import annotations

import threading
import tkinter as tk
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import app_ui
import dashboard_auth_ui
import login_view
import store
from auth_service import DashboardAuthError
from models import InstanceConfig
from process_manager import ManagedProcess, ProcessManager


class TestAPI:
    __test__ = False
    base = "https://dashboard.example.test"

    def __init__(self, _base=None):
        self.second_factor = False
        self.denied = False
        self.login_error = False
        self.principal = {"user_id": 7, "username": "tester", "is_admin": False, "menus": ["nodes"]}
        self.items = []
        self.login = MagicMock(side_effect=self._login)
        self.login_2fa = MagicMock(return_value={"token": "synthetic-access-token"})
        self.authorize = MagicMock(return_value={"operation_id": 123})
        self.result = MagicMock(return_value={"ok": True})
        self.replay_daemon_event = MagicMock(return_value={"ok": True})

    def _login(self, _username, _password):
        if self.login_error:
            raise DashboardAuthError("用户名或密码错误", status=401)
        if self.second_factor:
            return {"requires_2fa": True, "login_token": "synthetic-second-factor-token"}
        return {"token": "synthetic-access-token"}

    def me(self, _token):
        return {**self.principal, "menus": [] if self.denied else ["nodes"]}

    def nodes(self, _token):
        return self.items


@pytest.fixture
def desktop(monkeypatch, tmp_path):
    (tmp_path / ".env").write_text(f"APP_URL='{TestAPI.base}'\n", encoding="utf-8")
    monkeypatch.setattr(store, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(app_ui.DashboardApp, "_setup_tray", lambda _self: None)
    monkeypatch.setattr(app_ui.DashboardApp, "_check_remote_version", lambda _self: None)
    monkeypatch.setattr(ManagedProcess, "recover", MagicMock())
    try:
        app = app_ui.DashboardApp()
    except tk.TclError as exc:
        pytest.skip(f"当前环境无法创建 Tk 桌面：{exc}")
    app.withdraw()
    original_destroy = app.destroy
    def destroy_without_timers():
        # 多个真实 Tk 测试共享线程；销毁前清除该解释器的 after，避免残留噪音。
        for timer in app.tk.splitlist(app.tk.call("after", "info")):
            app.after_cancel(timer)
        original_destroy()
    app.destroy = destroy_without_timers
    callback_errors = []
    app.report_callback_exception = lambda *error: callback_errors.append(error)
    api = TestAPI()
    monkeypatch.setattr(login_view, "DashboardAPI", lambda _base: api)
    app.session.grant_for_recovery = MagicMock()
    yield app, api, tmp_path, callback_errors
    if not app._quitting:
        app._quit_app()
    assert not callback_errors, [str(error[1]) for error in callback_errors]


def pump(app, predicate=lambda: False, *, timeout_ms=1200):
    """运行真实 Tk 主循环，让工作线程可以安全提交 after 回调。"""
    def poll():
        if predicate():
            app.quit()
        else:
            tk.Misc.after(app, 10, poll)
    tk.Misc.after(app, 10, poll)
    timer = tk.Misc.after(app, timeout_ms, app.quit)
    app.mainloop()
    try:
        app.after_cancel(timer)
    except tk.TclError:
        pass


def submit_login(app):
    view = app._login_view
    view.username.set("tester")
    view.password.set("synthetic-password")
    view._login()
    return view


def test_unlogged_window_does_not_load_or_adopt_instances(desktop, monkeypatch):
    app, _api, _tmp_path, _errors = desktop
    load = MagicMock()
    monkeypatch.setattr(ProcessManager, "load", load)
    assert not app.session.active
    assert not app.manager._items
    assert app._login_view is not None
    load.assert_not_called()
    ManagedProcess.recover.assert_not_called()


def test_login_layout_and_restore_do_not_require_main_widgets(desktop):
    app, _api, _tmp_path, _errors = desktop
    app.deiconify()
    app.update_idletasks()
    assert app._login_view.winfo_width() > 600
    assert app._login_view.winfo_height() > 400
    app._repair_ui_after_restore()
    assert app._login_view is not None


def test_close_login_does_not_delete_existing_local_inventory(desktop):
    app, _api, tmp_path, _errors = desktop
    cfg = InstanceConfig.create(name="必须保留", exe_path=str(tmp_path / "node_client.exe"))
    store.save_instances([cfg])
    app._quit_app()
    assert [item.id for item in store.load_instances()] == [cfg.id]


def test_login_failure_stays_on_login_page_without_persisting_secrets(desktop):
    app, api, tmp_path, _errors = desktop
    api.login_error = True
    view = submit_login(app)
    pump(app, lambda: not view._busy)
    assert not app.session.active
    assert app._login_view is view
    assert "错误" in view.status.cget("text")
    assert not app.manager._items
    assert not (tmp_path / "panel_config.json").exists()
    assert view.password.get() == ""


def test_two_factor_login_and_logout_do_not_persist_credentials(desktop):
    app, api, tmp_path, _errors = desktop
    api.second_factor = True
    view = submit_login(app)
    pump(app, lambda: bool(view._pending_token) and not view._busy)
    assert not app.session.active
    assert not app.manager._items
    view.code.set("123456")
    view._login()
    pump(app, lambda: app.session.active and app._login_view is None)
    assert app.session.active
    api.login_2fa.assert_called_once_with("synthetic-second-factor-token", "123456")
    app._logout()
    pump(app, lambda: app._login_view is not None)
    assert not app.session.active and app.session.token == ""
    saved = (tmp_path / "panel_config.json").read_text(encoding="utf-8")
    assert "tester" in saved
    for secret in ("synthetic-password", "synthetic-access-token", "synthetic-second-factor-token", "123456"):
        assert secret not in saved


def test_login_without_node_menu_cannot_initialize_management(desktop):
    app, api, _tmp_path, _errors = desktop
    api.denied = True
    view = submit_login(app)
    pump(app, lambda: not view._busy)
    assert not app.session.active
    assert not app.manager._items
    assert app._login_view is view
    assert "权限" in view.status.cget("text")


def test_destroyed_login_response_cannot_restore_session(desktop):
    app, api, _tmp_path, _errors = desktop
    entered, release = threading.Event(), threading.Event()
    def blocked(_username, _password):
        entered.set()
        assert release.wait(2)
        return {"token": "synthetic-stale-access-token"}
    api.login.side_effect = blocked
    old = submit_login(app)
    assert entered.wait(1)
    app.session.lock("测试：登录请求已作废", clear_token=True)
    app._show_login("请重新登录")
    release.set()
    pump(app, timeout_ms=350)
    assert app._login_view is not old
    assert not app.session.active and not app.session.token
    assert not app.manager._items


def test_owner_filter_actual_legacy_account_matching_and_full_inventory_save(desktop, monkeypatch):
    app, api, tmp_path, _errors = desktop
    mine = InstanceConfig.create(name="旧运行实例", exe_path=str(tmp_path / "mine.exe"), status_port=19001)
    other = InstanceConfig.create(name="他人实例", exe_path=str(tmp_path / "other.exe"))
    other.node_id, other.mt5_login = "nd_other", 222
    store.save_instances([mine, other])
    api.items = [{"node_id": "nd_mine", "mt5_login": 111, "owner_user_id": 7,
                  "enabled": False, "approval_status": "approved", "legacy_allowed": True}]
    monkeypatch.setattr(dashboard_auth_ui, "_http_json", lambda *_args, **_kwargs: (200, {"mt5_login": 111}))
    app.session.establish(api, "synthetic-access-token")
    app._authenticated()
    assert [cfg.id for cfg in app.manager.configs()] == [mine.id]
    assert app.manager.get(other.id) is None
    assert app.manager.get(mine.id).cfg.node_id == "nd_mine"
    app.manager.get(mine.id).cfg.name = "本人修改"
    app._persist()
    saved = {cfg.id: cfg for cfg in store.load_instances()}
    assert set(saved) == {mine.id, other.id}
    assert saved[mine.id].name == "本人修改"
    assert saved[other.id].name == "他人实例"


def test_tray_exit_stops_workers_and_preserves_node_process(desktop):
    app, _api, tmp_path, _errors = desktop
    cfg = InstanceConfig.create(exe_path=str(tmp_path / "node_client.exe"))
    mp = ManagedProcess(cfg)
    mp.runtime.process_alive = True
    mp.shutdown_workers = MagicMock()
    mp.stop = MagicMock()
    app.manager._items[cfg.id] = mp
    app._quit_app()
    mp.shutdown_workers.assert_called_once()
    mp.stop.assert_not_called()
    assert mp.runtime.process_alive


def test_saved_backend_cannot_be_silently_rebound_by_matching_node_id(desktop):
    app, api, tmp_path, _errors = desktop
    cfg = InstanceConfig.create(exe_path=str(tmp_path / "node_client.exe"))
    cfg.node_id, cfg.mt5_login = "nd_mine", 111
    cfg.backend_base = "https://another-backend.example.test"
    store.save_instances([cfg])
    api.items = [{"node_id": "nd_mine", "mt5_login": 111, "owner_user_id": 7,
                  "enabled": False, "approval_status": "approved"}]
    app.session.establish(api, "synthetic-access-token")
    app._authenticated()
    assert app.manager.configs() == []
    app._persist()
    assert store.load_instances()[0].backend_base == cfg.backend_base


def test_online_review_does_not_automatically_revive_revoked_guard(desktop, monkeypatch):
    app, api, tmp_path, _errors = desktop
    cfg = InstanceConfig.create(exe_path=str(tmp_path / "node_client.exe"), daemon=True)
    cfg.node_id, cfg.mt5_login = "nd_mine", 111
    store.save_instances([cfg])
    api.items = [{"node_id": "nd_mine", "mt5_login": 111, "owner_user_id": 7,
                  "enabled": False, "approval_status": "approved"}]
    app.session.establish(api, "synthetic-access-token")
    app._authenticated()
    mp = app.manager.get(cfg.id)
    mp.daemon_grant = SimpleNamespace(allowed=False)
    app.session.grant_for_recovery.reset_mock()
    api.items[0]["enabled"] = True
    monkeypatch.setattr(app.session, "sync_credential", lambda _cfg: True)
    app._apply_review(app.session.generation)
    app.session.grant_for_recovery.assert_not_called()
    assert not mp.daemon_grant.allowed


def test_connection_dialog_never_discovers_or_displays_legacy_global_token(desktop, monkeypatch):
    import version_service
    from connection_dialog import ConnectionConfigDialog
    app, api, _tmp_path, _errors = desktop
    store.save_panel_config({"backend_base": api.base, "node_token": "synthetic-legacy-global-secret"})
    monkeypatch.setattr(version_service, "read_node_env", MagicMock(side_effect=AssertionError("不得读节点令牌")))
    app.session.establish(api, "synthetic-access-token")
    app._authenticated()
    dialog = ConnectionConfigDialog(app, app.manager)
    assert dialog.base_var.get() == api.base
    assert dialog.base_entry.cget("state") == "disabled"
    assert not hasattr(dialog, "token_var")
    version_service.read_node_env.assert_not_called()
    dialog.destroy()


def test_login_without_app_url_stays_on_page(desktop):
    app, api, tmp_path, _errors = desktop
    (tmp_path / ".env").unlink()
    view = app._login_view
    view.username.set("tester")
    view.password.set("synthetic-password")
    view._login()
    assert not app.session.active
    assert app._login_view is view
    assert "APP_URL" in view.status.cget("text")
    assert view._base_label.cget("text") == "未配置"
    api.login.assert_not_called()


def test_connection_dialog_shows_env_address_without_editing(desktop):
    from connection_dialog import ConnectionConfigDialog
    app, api, _tmp_path, _errors = desktop
    api.principal["is_admin"] = True
    saved = {"backend_base": api.base, "node_token": "synthetic-legacy-global-secret", "last_username": "tester"}
    store.save_panel_config(saved)
    app.session.establish(api, "synthetic-access-token")
    app._authenticated()
    dialog = ConnectionConfigDialog(app, app.manager)
    assert dialog.base_var.get() == api.base
    assert dialog.base_entry.cget("state") == "disabled"
    assert not hasattr(dialog, "_save")
    dialog.base_var.set("https://new-backend.example.test/")
    assert store.load_panel_config() == saved
    dialog.destroy()


def test_desktop_enrollment_approval_start_and_logout_flow(desktop):
    app, api, tmp_path, _errors = desktop
    exe = tmp_path / "client" / "node_client.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"MZ synthetic bound client")
    (exe.parent / "client_capabilities.json").write_text(json.dumps({
        "account_binding": True, "executable_sha256": hashlib.sha256(exe.read_bytes()).hexdigest(),
    }), encoding="utf-8")
    node = {"node_id": "nd_enrolled", "mt5_login": 111, "name": "新申请",
            "owner_user_id": None, "requested_by_user_id": 7, "enabled": False,
            "approval_status": "pending", "credential_generation": 1,
            "has_credential": False, "legacy_allowed": False}
    api.items = [node]
    api.enroll = MagicMock(return_value=node)
    api.credential = MagicMock(return_value={
        "node_id": "nd_enrolled", "mt5_login": 111, "generation": 1,
        "token": "ndv1.nd_enrolled.synthetic-node-secret", "valid": True,
    })
    api.daemon_grant = MagicMock(return_value={
        "node_id": "nd_enrolled", "mt5_login": 111, "generation": 1,
        "grant_token": "synthetic-memory-only-grant", "grant_operation_id": 125, "actor_user_id": 7,
    })
    view = submit_login(app)
    pump(app, lambda: app.session.active and app._login_view is None)
    assert view.password.get() == ""
    cfg = InstanceConfig.create(name="新申请", exe_path=str(exe), cwd=str(exe.parent))
    app._enroll_instance(cfg, 111)
    mp = app.manager.add(cfg)
    mp.start = MagicMock(side_effect=lambda: setattr(mp.runtime, "process_alive", True))
    app._persist()
    with pytest.raises(DashboardAuthError, match="开通"):
        app.manager.start(cfg.id)
    mp.start.assert_not_called()
    node.update(owner_user_id=7, enabled=True, approval_status="approved")
    app.session.review()
    app._apply_review(app.session.generation)
    assert api.credential.call_args.args[1] == "issue"
    assert "NODE_TOKEN=ndv1.nd_enrolled.synthetic-node-secret" in (exe.parent / ".env").read_text(encoding="utf-8")
    app.manager.start(cfg.id)
    mp.start.assert_called_once()
    grant = mp.daemon_grant
    app._logout()
    pump(app, lambda: app._login_view is not None)
    assert not app.session.token and not app.session.active
    assert mp.runtime.process_alive and mp.daemon_grant is grant
    persisted = (tmp_path / "instances.json").read_text(encoding="utf-8")
    assert "nd_enrolled" in persisted
    assert "synthetic-memory-only-grant" not in persisted
    assert "synthetic-access-token" not in persisted
