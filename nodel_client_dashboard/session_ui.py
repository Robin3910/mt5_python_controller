"""Tk 会话边界：旧会话的对话框与延迟回调不可执行。"""
from __future__ import annotations

import functools
import tkinter as tk
from tkinter import messagebox

import customtkinter as ctk

from auth_service import DashboardAuthError


def secured(action: str, selector=None):
    def decorate(fn):
        @functools.wraps(fn)
        def invoke(self, *args, **kwargs):
            try:
                generation = getattr(self, "_session_generation", self.session.generation)
                self.session.check(generation)
                cfg = selector(self, *args, **kwargs) if selector else None
                return self.session.perform(action, cfg, lambda: fn(self, *args, **kwargs), generation=generation)
            except DashboardAuthError as exc:
                try:
                    messagebox.showerror("操作未执行", str(exc), parent=self)
                except tk.TclError:
                    pass
                return None
        return invoke
    return decorate


def requires_session(fn):
    @functools.wraps(fn)
    def invoke(self, *args, **kwargs):
        previous_generation = getattr(self.session._local, "generation", None)
        try:
            generation = getattr(self, "_session_generation", self.session.generation)
            self.session.check(generation)
            self.session._local.generation = generation
            return fn(self, *args, **kwargs)
        except DashboardAuthError as exc:
            try:
                messagebox.showerror("操作未执行", str(exc), parent=self)
            except tk.TclError:
                pass
            return None
        finally:
            self.session._local.generation = previous_generation
    return invoke


class SessionDialog(ctk.CTkToplevel):
    def __init__(self, master, *args, **kwargs):
        self.session = master.session
        self.session.check()
        self._session_generation = self.session.generation
        self.dashboard = getattr(master, "dashboard", master)
        super().__init__(master, *args, **kwargs)

    def after(self, ms, func=None, *args):
        if func is None:
            return super().after(ms)
        generation = self._session_generation
        def current():
            if generation == self.session.generation and self.session.active:
                return func(*args)
        try:
            return super().after(ms, current)
        except (tk.TclError, RuntimeError):
            return None

    def session_check(self):
        self.session.check(self._session_generation)

    def version_target(self):
        self.session_check()
        from version_service import BackendTarget
        return BackendTarget(self.session.api.base, self.session.token, "用户登录", "Authorization", self.session)
