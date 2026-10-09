"""后台账户与 2FA 登录页；不载入节点列表，也不接管节点。"""
from __future__ import annotations

import threading
import tkinter as tk

import customtkinter as ctk

from auth_service import DashboardAPI, DashboardAuthError
from store import load_app_url, load_panel_config, save_panel_config
from theme import ACCENT, BG, BG_ELEVATED, DANGER, TEXT, TEXT_MUTED, font, glass_card, primary_btn


class LoginView(ctk.CTkFrame):
    def __init__(self, master, on_authenticated, reason: str = ""):
        super().__init__(master, fg_color=BG)
        self._on_authenticated = on_authenticated
        self._busy = False
        self._pending_token = ""
        self._attempt = 0
        config = load_panel_config()
        self.username = tk.StringVar(value=str(config.get("last_username") or ""))
        self.password = tk.StringVar()
        self.code = tk.StringVar()
        self.grid(row=0, column=0, rowspan=2, sticky="nsew")
        card = glass_card(self)
        card.place(relx=0.5, rely=0.5, anchor="center")
        ctk.CTkLabel(card, text="节点控制台登录", text_color=ACCENT, font=font(24, "bold")).pack(padx=42, pady=(30, 8))
        ctk.CTkLabel(card, text="使用后台账户，按本人节点权限进行管理", text_color=TEXT_MUTED).pack(pady=(0, 18))
        ctk.CTkLabel(card, text="后端地址", text_color=TEXT_MUTED, anchor="w").pack(fill="x", padx=34)
        self._base_label = ctk.CTkLabel(card, text=self._backend_text(), text_color=TEXT, anchor="w", wraplength=355, justify="left")
        self._base_label.pack(fill="x", padx=34, pady=(4, 12))
        for label, variable, secret in [("用户名", self.username, False), ("密码", self.password, True)]:
            ctk.CTkLabel(card, text=label, text_color=TEXT_MUTED, anchor="w").pack(fill="x", padx=34)
            entry = ctk.CTkEntry(card, textvariable=variable, show="*" if secret else "", width=360, height=36, fg_color=BG_ELEVATED, text_color=TEXT)
            entry.pack(padx=34, pady=(4, 12))
            entry.bind("<Return>", lambda _event: self._login())
        self._code_label = ctk.CTkLabel(card, text="2FA 验证码", text_color=TEXT_MUTED, anchor="w")
        self._code_entry = ctk.CTkEntry(card, textvariable=self.code, width=360, height=36, fg_color=BG_ELEVATED, text_color=TEXT)
        self._code_entry.bind("<Return>", lambda _event: self._login())
        missing = not load_app_url()
        hint = "未配置后端地址：请在程序目录的 .env 中设置 APP_URL" if missing else "请输入用户名和密码"
        self.status = ctk.CTkLabel(card, text=reason or hint, text_color=DANGER if missing and not reason else TEXT_MUTED, wraplength=355)
        self.status.pack(padx=34, pady=10)
        self.button = primary_btn(card, "登录", self._login, width=360)
        self.button.pack(padx=34, pady=(4, 30))

    def _backend_text(self) -> str:
        return load_app_url() or "未配置"

    def _login(self):
        if self._busy:
            return
        configured = load_app_url()
        self._base_label.configure(text=configured or "未配置", text_color=TEXT if configured else DANGER)
        if not configured:
            self.status.configure(text="未配置后端地址：请在程序目录的 .env 中设置 APP_URL", text_color=DANGER)
            return
        try:
            api = DashboardAPI(configured)
        except DashboardAuthError as exc:
            self.status.configure(text=str(exc), text_color=DANGER)
            return
        username, password, code = self.username.get().strip(), self.password.get(), self.code.get().strip()
        pending = self._pending_token
        generation = self.master.session.generation
        self.password.set("")
        self.code.set("")
        self._busy = True
        self._attempt += 1
        attempt = self._attempt
        self.button.configure(state="disabled")
        self.status.configure(text="正在验证…", text_color=TEXT_MUTED)
        def work():
            try:
                result = api.login_2fa(pending, code) if pending else api.login(username, password)
                if result.get("requires_2fa"):
                    def second_step():
                        self._pending_token = str(result["login_token"])
                        self._code_label.pack(fill="x", padx=34, before=self.status)
                        self._code_entry.pack(padx=34, pady=(4, 12), before=self.status)
                        self._code_entry.focus_set()
                        self.status.configure(text="请输入 2FA 验证码后再次点击登录", text_color=ACCENT)
                    schedule(second_step)
                else:
                    token = str(result["token"])
                    principal = api.me(token)
                    nodes = api.nodes(token)
                    def done():
                        try:
                            self.master.session.accept_login(api, token, principal, nodes, expected_generation=generation)
                        except DashboardAuthError as exc:
                            self.status.configure(text=str(exc), text_color=DANGER)
                            self._busy = False
                            self.button.configure(state="normal")
                            return
                        self._pending_token = ""
                        save_panel_config({"backend_base": api.base, "last_username": username})
                        self._on_authenticated()
                    schedule(done)
            except (DashboardAuthError, KeyError, ValueError) as exc:
                error = str(exc) if isinstance(exc, DashboardAuthError) else "登录响应无效"
                def failed():
                    self._pending_token = ""
                    self._code_label.pack_forget()
                    self._code_entry.pack_forget()
                    self.status.configure(text=error, text_color=DANGER)
                schedule(failed)
            finally:
                def idle():
                    self._busy = False
                    self.button.configure(state="normal")
                schedule(idle)
        def schedule(callback):
            def current():
                if not self.winfo_exists() or self._attempt != attempt:
                    return
                # 登录是一个新交互；Tk 模态重入期间不能继承旧动作的线程局部世代。
                previous = getattr(self.master.session._local, "generation", None)
                self.master.session._local.generation = None
                try:
                    callback()
                finally:
                    self.master.session._local.generation = previous
            try:
                self.after(0, current)
            except (tk.TclError, RuntimeError):
                pass
        threading.Thread(target=work, name="dashboard-login", daemon=True).start()

    def destroy(self):
        self._attempt += 1
        self._pending_token = ""
        self.password.set("")
        self.code.set("")
        super().destroy()
