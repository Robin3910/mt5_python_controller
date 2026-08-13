"""版本选择对话框：从后端拉可下载版本清单，挑一个目标版本。

原先「更新到指定版本」用 `CTkInputDialog` 让人手输版本号，而版本号形如
`1.1.0-20260813090507`，手抄极易错一位，且输错要等到下载返回 404 才发现。改成
下拉选择后还有个实质收益：清单里带着每个版本的 sha256，选出来的版本因此可以做
完整性校验 —— 手输那条路径拿不到校验和，只能跳过。

窗口按父窗口居中显示，遮挡与显示一律走 `dialog_window`（勿改回 withdraw，
原因见 `dialog_window` 模块注释）。
"""
from __future__ import annotations

import threading
import tkinter as tk

import customtkinter as ctk

import version_service as vs
from dialog_window import hide_while_building, show_centered
from theme import (
    ACCENT,
    BG,
    BG_ELEVATED,
    DANGER,
    GLASS_BORDER,
    TEXT,
    TEXT_DIM,
    TEXT_MUTED,
    WARNING,
    font,
    ghost_btn,
    glass_card,
    mono,
    primary_btn,
)

_LOADING = "（正在获取…）"
_EMPTY = "（无可用版本）"


class VersionPickerDialog(ctk.CTkToplevel):
    """选一个后端已上传的版本；确认后 `selected` 为版本号，取消则为空串。"""

    _W = 600
    _H = 400

    def __init__(self, master, target: vs.BackendTarget, *, title: str = "更新到指定版本") -> None:
        super().__init__(master)
        self.title(title)
        self.configure(fg_color=BG)
        self.transient(master)
        self.resizable(False, False)
        hide_while_building(self)

        self._target = target
        self._items: list[dict] = []
        self._current = ""
        self._busy = False

        self.selected = ""
        self.selected_sha256 = ""

        self.version_var = tk.StringVar(value=_LOADING)

        self._build()
        self.bind("<Escape>", lambda _e: self.destroy())
        self.after(20, self._show_centered)
        self.after(120, self._load)

    # ---------------------------------------------------------------- 布局

    def _build(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        card = glass_card(self)
        card.grid(row=0, column=0, sticky="nsew", padx=16, pady=(16, 8))
        card.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            card, text="选择目标版本", text_color=ACCENT, font=font(11, "bold")
        ).grid(row=0, column=0, padx=16, pady=(14, 2), sticky="w")
        ctk.CTkLabel(
            card,
            text="列出后端已上传的全部版本（新版在前），不限于当前发布版本；可用于灰度或回退到某个具体版本。",
            text_color=TEXT_MUTED, font=font(12), wraplength=self._W - 70, justify="left",
        ).grid(row=1, column=0, padx=16, pady=(0, 10), sticky="w")

        picker = ctk.CTkFrame(card, fg_color="transparent")
        picker.grid(row=2, column=0, padx=12, pady=4, sticky="ew")
        self.version_menu = ctk.CTkOptionMenu(
            picker, variable=self.version_var, values=[_LOADING],
            command=lambda _v: self._render_detail(),
            fg_color=BG_ELEVATED, button_color=GLASS_BORDER,
            button_hover_color=ACCENT, text_color=TEXT, width=280,
        )
        self.version_menu.pack(side="left", padx=4)
        ghost_btn(picker, "刷新列表", self._load, width=100).pack(side="left", padx=4)

        self.detail = ctk.CTkLabel(
            card, text="", text_color=TEXT_DIM, font=mono(12), anchor="w",
            wraplength=self._W - 70, justify="left",
        )
        self.detail.grid(row=3, column=0, padx=16, pady=(10, 4), sticky="ew")

        self.notes = ctk.CTkLabel(
            card, text="", text_color=TEXT_MUTED, font=font(12), anchor="w",
            wraplength=self._W - 70, justify="left",
        )
        self.notes.grid(row=4, column=0, padx=16, pady=(0, 14), sticky="ew")

        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 16))
        self.ok_btn = primary_btn(bottom, "确定", self._confirm, width=100)
        self.ok_btn.pack(side="left", padx=4)
        self.ok_btn.configure(state="disabled")
        ghost_btn(bottom, "取消", self.destroy, width=90).pack(side="left", padx=4)
        self.status = ctk.CTkLabel(
            bottom, text="正在获取版本清单…", text_color=TEXT_DIM, font=font(12), anchor="w"
        )
        self.status.pack(side="left", padx=12, fill="x", expand=True)

    def _show_centered(self) -> None:
        show_centered(self, self._W, self._H)

    # ---------------------------------------------------------------- 数据

    def _load(self) -> None:
        if self._busy:
            return
        if not self._target.ready:
            self.status.configure(text="尚未配置后端地址或节点令牌", text_color=DANGER)
            return
        self._busy = True
        self.status.configure(text="正在获取版本清单…", text_color=TEXT_MUTED)

        def worker() -> None:
            try:
                items, current = vs.fetch_available_versions(self._target)
            except vs.VersionServiceError as e:
                # 必须先取出消息：except 块结束时 e 会被解绑，延迟执行的 lambda 里读不到它
                msg = str(e)
                self.after(0, lambda: self._fail(msg))
                return
            self.after(0, lambda: self._fill(items, current))

        threading.Thread(target=worker, name="version-picker", daemon=True).start()

    def _fail(self, message: str) -> None:
        self._busy = False
        self._items = []
        self.version_menu.configure(values=[_EMPTY])
        self.version_var.set(_EMPTY)
        self.ok_btn.configure(state="disabled")
        self.status.configure(text=message, text_color=DANGER)

    def _fill(self, items: list[dict], current: str) -> None:
        self._busy = False
        self._items = items
        self._current = current
        names = [str(i.get("version") or "") for i in items if i.get("version")]
        if not names:
            self.version_menu.configure(values=[_EMPTY])
            self.version_var.set(_EMPTY)
            self.ok_btn.configure(state="disabled")
            self.status.configure(
                text="后端还没有任何客户端安装包，请先在管理后台「客户端版本」上传",
                text_color=WARNING,
            )
            return
        self.version_menu.configure(values=names)
        # 默认选当前发布版本；没发布过就用最新的一个
        self.version_var.set(current if current in names else names[0])
        self.ok_btn.configure(state="normal")
        self.status.configure(text=f"共 {len(names)} 个可用版本", text_color=ACCENT)
        self._render_detail()

    def _selected_item(self) -> dict | None:
        want = self.version_var.get().strip()
        for item in self._items:
            if str(item.get("version") or "") == want:
                return item
        return None

    def _render_detail(self) -> None:
        item = self._selected_item()
        if item is None:
            self.detail.configure(text="")
            self.notes.configure(text="")
            return
        size_mb = int(item.get("size") or 0) / 1024 / 1024
        parts = [f"{size_mb:.1f} MB"]
        if item.get("is_current"):
            parts.append("当前发布版本")
        sha = str(item.get("sha256") or "")
        parts.append(f"sha256 {sha[:12]}…" if sha else "无校验和")
        self.detail.configure(text="　·　".join(parts))
        notes = str(item.get("notes") or "")
        self.notes.configure(text=f"更新说明：{notes}" if notes else "")

    def _confirm(self) -> None:
        item = self._selected_item()
        if item is None:
            return
        self.selected = str(item.get("version") or "")
        self.selected_sha256 = str(item.get("sha256") or "")
        self.destroy()
