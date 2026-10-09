"""登录、归属匹配、锁定、专属令牌操作；主布局保持原有职责。"""
from __future__ import annotations

import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, simpledialog

from auth_service import DashboardAuthError
from dashboard_session import DashboardSession
from login_view import LoginView
from process_manager import _http_json
from store import load_instances, save_instances
from session_ui import requires_session


class DashboardAuthUI:
    def _auth_init(self):
        self.session = DashboardSession(on_lock=self._session_locked)
        self.dashboard = self
        self._inventory = {}
        self._inventory_loaded = False
        self._main_widgets = []
        self._login_view = None
        self._review_running = False

    def _session_locked(self, reason):
        try:
            self.after(0, lambda: self._show_login(reason))
        except (tk.TclError, RuntimeError):
            pass

    def _show_login(self, reason=""):
        if self._quitting:
            return
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=0)
        self.manager.set_visible(set())
        self._selected_id = None
        for widget in list(self.winfo_children()):
            if widget is not self._login_view:
                widget.destroy()
        self._main_widgets = []
        self._list_rows = {}
        self._chip_widgets = {}
        self._list_order = ()
        self._detail_snap = self._log_snap = None
        if self._login_view is not None:
            self._login_view.destroy()
        self._login_view = LoginView(self, self._authenticated, reason)
        if self.session.token:
            generation = self.session.generation
            self.after(30000, lambda: self._recover_connection(generation))

    def _recover_connection(self, generation):
        if generation != self.session.generation or not self.session.token or self.session.active or self._quitting:
            return
        def work():
            try:
                self.session.review(recover=True)
                self.after(0, self._authenticated)
            except DashboardAuthError:
                pass
        threading.Thread(target=work, name="dashboard-session-recover", daemon=True).start()

    def _authenticated(self):
        if not self.session.active:
            return
        if self._login_view is not None:
            self._login_view.destroy()
            self._login_view = None
        try:
            self._load_permitted_instances()
        except (DashboardAuthError, OSError, ValueError):
            self.session.lock("实例身份校验失败，请重新登录后检查绑定")
            return
        self._build_layout()
        self.grid_rowconfigure(0, weight=0)
        self._main_widgets = list(self.winfo_children())
        self._bind_list()
        self._bind_detail(refresh_log=True)
        generation = self.session.generation
        self.after(1000, self._tick_ui)
        if self._tray is None:
            self.after(200, self._setup_tray)
        self.after(30000, lambda: self._review_session(generation))
        self.after(3000, self._check_remote_version)

    def _load_permitted_instances(self):
        stored = load_instances()
        for cfg in stored:
            self._inventory.setdefault(cfg.id, cfg)
        self._inventory_loaded = True
        candidates = list(self._inventory.values())
        allowed = []
        for cfg in candidates:
            mp = self.manager._items.get(cfg.id)
            if mp is not None:
                cfg = mp.cfg
            if cfg.backend_base and cfg.backend_base.rstrip("/") != self.session.api.base:
                if self.session.is_admin:
                    cfg.approval_status = "unbound"
                    allowed.append(cfg)
                continue
            actual = None
            if cfg.status_port > 0:
                try:
                    _, actual = _http_json("GET", f"http://127.0.0.1:{cfg.status_port}/status", timeout=0.3)
                except Exception:
                    pass
            if actual:
                node_id = str(actual.get("node_id") or "")
                login = int(actual.get("mt5_login") or 0)
                node = self.session.nodes.get(node_id)
                if not node and not node_id and login:
                    matches = [n for n in self.session.nodes.values() if int(n.get("mt5_login") or 0) == login]
                    node = matches[0] if len(matches) == 1 else None
                if node and login == int(node.get("mt5_login") or 0):
                    self.session.apply_node(cfg, node)
                else:
                    if not self.session.is_admin:
                        # 无权或身份矛盾的清单保留原始绑定，普通账号不修改隐藏记录。
                        continue
                    cfg.node_id = None
                    cfg.approval_status = "unbound"
            elif cfg.node_id:
                node = self.session.nodes.get(str(cfg.node_id))
                if node:
                    self.session.apply_node(cfg, node)
            if self.session.owns(cfg):
                allowed.append(cfg)
        self.manager.set_visible({c.id for c in allowed})
        self.manager.load(allowed)
        for cfg in allowed:
            self._inventory[cfg.id] = cfg
            mp = self.manager._items[cfg.id]
            if cfg.approval_status == "approved" and cfg.enabled:
                if not self.session.sync_credential(cfg):
                    mp.runtime.last_error = "本机专属令牌缺失或失效，请确认后重置专属令牌"
                    if mp.daemon_grant is not None:
                        mp.daemon_grant.allowed = False
        self._persist()

    def _review_session(self, generation):
        if self._quitting or generation != self.session.generation or not self.session.active:
            return
        if self._review_running:
            self.after(30000, lambda: self._review_session(generation))
            return
        self._review_running = True
        def work():
            try:
                self.session.review()
                self.after(0, lambda: self._apply_review(generation))
            except DashboardAuthError:
                pass
            finally:
                self._review_running = False
                try:
                    self.after(30000, lambda: self._review_session(generation))
                except (tk.TclError, RuntimeError):
                    pass
        threading.Thread(target=work, name="dashboard-session-check", daemon=True).start()

    def _apply_review(self, generation):
        if generation != self.session.generation or not self.session.active:
            return
        visible = set()
        for iid, mp in self.manager._items.items():
            if mp.cfg.backend_base and mp.cfg.backend_base.rstrip("/") != self.session.api.base:
                continue
            node = self.session.nodes.get(str(mp.cfg.node_id or ""))
            if node:
                self.session.apply_node(mp.cfg, node)
            if self.session.owns(mp.cfg):
                visible.add(iid)
                if node and mp.cfg.approval_status == "approved" and mp.cfg.enabled:
                    if not self.session.sync_credential(mp.cfg):
                        mp.runtime.last_error = "本机专属令牌缺失或失效，请确认后重置专属令牌"
                        if mp.daemon_grant is not None:
                            mp.daemon_grant.allowed = False
            if mp.daemon_grant is not None and (not node or not node.get("enabled") or node.get("approval_status") not in (None, "approved")):
                mp.daemon_grant.allowed = False
        self.manager.set_visible(visible)
        if self._selected_id not in visible:
            self._selected_id = None
        self._safe_refresh(refresh_log=True)
        self._persist()

    def _persist(self):
        if not self._inventory_loaded:
            return
        for iid, mp in self.manager._items.items():
            self._inventory[iid] = mp.cfg
        save_instances(list(self._inventory.values()))

    def _logout(self):
        try:
            self.session.perform("logout", None, lambda: None)
        finally:
            self._persist()
            self.session.lock("已退出登录，节点与既有守护继续运行", clear_token=True)

    @requires_session
    def _bind_instance(self):
        self.session.check()
        mp = self._selected()
        if mp is None:
            return
        if not self.session.is_admin:
            raise DashboardAuthError("旧实例手工绑定仅管理员可操作", status=403)
        choices = "\n".join(f"{n['node_id']} · {n.get('mt5_login')} · {n.get('name')}" for n in self.session.nodes.values())
        node_id = simpledialog.askstring("管理员绑定实例", "请输入后台节点 ID：\n" + choices, parent=self)
        if not node_id:
            return
        node = self.session.nodes.get(node_id.strip())
        if not node:
            messagebox.showerror("绑定失败", "节点 ID 不存在", parent=self)
            return
        if mp.runtime.process_alive and int(mp.runtime.mt5_login or 0) != int(node.get("mt5_login") or 0):
            messagebox.showerror("绑定失败", "运行中的实际 MT5 账号与目标节点不符", parent=self)
            return
        def save():
            if mp.daemon_grant is not None:
                mp.daemon_grant.allowed = False
            self.session.apply_node(mp.cfg, node)
            self._persist()
        self.session.perform("bind_instance", mp.cfg, save, params={"node_id": node_id.strip(), "mt5_login": node.get("mt5_login")})
        self._safe_refresh()

    @requires_session
    def _rotate_credential(self):
        mp = self._selected()
        if mp is None:
            return
        self.session.check()
        if not messagebox.askyesno("确认重置专属令牌", "旧令牌将立即失效；请确认本机令牌确实丢失或需要更换。是否继续？", parent=self):
            return
        generation = self.session.generation
        def work():
            import env_file as ef
            self.session.check(generation)
            issued = self.session.credential(mp.cfg, "rotate")
            self.session.check(generation)
            text, _ = ef.read_env(mp.cfg.cwd)
            self.session.perform("edit_env", mp.cfg, lambda: ef.write_env(mp.cfg.cwd, ef.update_env_text(text, {"NODE_TOKEN": str(issued["token"]), "MANAGER_WS_URL": ef.ws_url_from_backend_base(self.session.api.base), "DASHBOARD_EXPECTED_MT5_LOGIN": str(issued["mt5_login"])})), generation=generation)
            mp.cfg.credential_generation = int(issued["generation"])
            node = self.session.nodes.get(str(mp.cfg.node_id))
            if node is not None:
                node["credential_generation"] = mp.cfg.credential_generation
                node["has_credential"] = True
                node["legacy_allowed"] = False
            self.session.grant_for_recovery(mp)
            self.after(0, self._persist)
        self._run_async("重置专属令牌", work)

    def validate_instance_location(self, cfg, exe_path: str, cwd: str):
        exe, directory = Path(exe_path).resolve(), Path(cwd).resolve()
        for other in self._inventory.values():
            if other.id != cfg.id and (Path(other.exe_path).resolve() == exe or Path(other.cwd).resolve() == directory):
                raise DashboardAuthError("该目录已登记为其他实例，不能重复使用", status=403)

    def _enroll_instance(self, cfg, login: int, server: str = ""):
        node = self.session.enrolled(login, cfg.name, server)
        self.session.apply_node(cfg, node)
        return cfg
