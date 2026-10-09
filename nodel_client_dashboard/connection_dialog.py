"""当前登录后端连接；地址来自程序目录 .env，不在界面修改。"""
from __future__ import annotations

import threading
import tkinter as tk
from tkinter import messagebox

import customtkinter as ctk

from auth_service import DashboardAuthError
from dialog_window import hide_while_building, show_centered
from process_manager import ProcessManager
from session_ui import SessionDialog, requires_session
from theme import ACCENT, BG, BG_ELEVATED, DANGER, GLASS_BORDER, TEXT, TEXT_MUTED, font, ghost_btn, glass_card


class ConnectionConfigDialog(SessionDialog):
    """查看当前登录后端。地址由程序目录 .env 的 APP_URL 决定。"""

    _W = 620
    _H = 320

    def __init__(self, master, manager: ProcessManager, on_saved=None) -> None:
        super().__init__(master)
        self.title("后端连接")
        self.configure(fg_color=BG)
        self.transient(master)
        self.resizable(False, False)
        hide_while_building(self)
        self.manager = manager
        self._on_saved = on_saved
        self._testing = False
        self.base_var = tk.StringVar(value=self.session.api.base)
        self._build()
        self.bind("<Escape>", lambda _event: self.destroy())
        self.after(20, lambda: show_centered(self, self._W, self._H))

    def _build(self) -> None:
        card = glass_card(self)
        card.pack(fill="both", expand=True, padx=16, pady=16)
        ctk.CTkLabel(card, text="后端连接", text_color=ACCENT, font=font(20, "bold")).pack(anchor="w", padx=20, pady=(20, 8))
        ctk.CTkLabel(
            card, text="版本查询与下载使用当前登录会话。后端地址读取程序目录 .env 的 APP_URL。",
            text_color=TEXT_MUTED, wraplength=540, justify="left",
        ).pack(anchor="w", padx=20, pady=(0, 14))
        ctk.CTkLabel(card, text="API 地址", text_color=TEXT_MUTED).pack(anchor="w", padx=20)
        self.base_entry = ctk.CTkEntry(
            card, textvariable=self.base_var, fg_color=BG_ELEVATED, border_color=GLASS_BORDER,
            text_color=TEXT, height=36, state="disabled",
        )
        self.base_entry.pack(fill="x", padx=20, pady=(6, 12))
        ctk.CTkLabel(
            card, text=f"当前用户：{self.session.principal.get('username', '')}", text_color=TEXT_MUTED,
        ).pack(anchor="w", padx=20)
        self.status = ctk.CTkLabel(card, text="", text_color=TEXT_MUTED, wraplength=535)
        self.status.pack(fill="x", padx=20, pady=12)
        buttons = ctk.CTkFrame(card, fg_color="transparent")
        buttons.pack(pady=(4, 18))
        ghost_btn(buttons, "验证当前连接", self._test, width=130).pack(side="left", padx=6)
        ghost_btn(buttons, "关闭", self.destroy, width=80).pack(side="left", padx=6)

    @requires_session
    def _test(self) -> None:
        if self._testing:
            return
        generation = self._session_generation
        self._testing = True
        self.status.configure(text="正在验证当前登录连接…", text_color=TEXT_MUTED)

        def worker():
            try:
                self.session._local.generation = generation
                self.session.perform("version_check", None, self.session.review, generation=generation)
                self.after(0, lambda: self.status.configure(text="当前连接与会话验证成功", text_color=ACCENT))
            except DashboardAuthError as exc:
                error = str(exc)
                self.after(0, lambda: self.status.configure(text=error, text_color=DANGER))
            except Exception:
                self.after(0, lambda: messagebox.showerror("验证连接", "连接验证失败，请重新登录", parent=self))
            finally:
                self.session._local.generation = None
                self.after(0, lambda: setattr(self, "_testing", False))

        threading.Thread(target=worker, name="dashboard-connection-check", daemon=True).start()
