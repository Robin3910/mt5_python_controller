"""版本选择对话框：从一份候选清单里挑一个目标版本。

两处都用它：「更新到指定版本」选后端已上传的版本，「回滚到本机备份」选本机
`.backups/` 里的版本。两者原先都用 `CTkInputDialog` 让人手输版本号 —— 版本号形如
`1.1.0-20260813090507`，手抄极易错一位，输错要等到下载 404 或「版本不可用」才发现；
`CTkInputDialog` 也不受 `dialog_window` 管，位置由库决定，不居中于父窗口。

候选清单由 `loader` 在后台线程提供，因此远端拉取与本机枚举共用同一套 UI；详情区
按字段存在性渲染，缺 size / sha256 的本机备份也能正常显示。

窗口按父窗口居中，遮挡与显示一律走 `dialog_window`（勿改回 withdraw，原因见该模块）。
"""
from __future__ import annotations

import threading
import tkinter as tk
from collections.abc import Callable, Sequence

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

_EMPTY = "（无可用版本）"

# loader 返回 (候选项, 当前发布版本号)；候选项至少要有 version 字段
Loader = Callable[[], tuple[list[dict], str]]


class VersionPickerDialog(ctk.CTkToplevel):
    """选一个版本；确认后 `selected` 为版本号，取消则为空串。"""

    _W = 600
    _H = 400

    def __init__(
        self,
        master,
        *,
        title: str,
        hint: str,
        loader: Loader,
        empty_hint: str,
        loading_hint: str = "",
    ) -> None:
        super().__init__(master)
        self.title(title)
        self.configure(fg_color=BG)
        self.transient(master)
        self.resizable(False, False)
        hide_while_building(self)

        self._loader = loader
        self._hint = hint
        self._empty_hint = empty_hint
        self._loading_hint = loading_hint
        self._items: list[dict] = []
        self._busy = False

        self.selected = ""
        self.selected_sha256 = ""

        self.version_var = tk.StringVar(value=_EMPTY)

        self._build()
        self.bind("<Escape>", lambda _e: self.destroy())
        self.after(20, self._show_centered)
        self.after(80, self._load)

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
            card, text=self._hint, text_color=TEXT_MUTED, font=font(12),
            wraplength=self._W - 70, justify="left",
        ).grid(row=1, column=0, padx=16, pady=(0, 10), sticky="w")

        picker = ctk.CTkFrame(card, fg_color="transparent")
        picker.grid(row=2, column=0, padx=12, pady=4, sticky="ew")
        self.version_menu = ctk.CTkOptionMenu(
            picker, variable=self.version_var, values=[_EMPTY],
            command=lambda _v: self._render_detail(),
            fg_color=BG_ELEVATED, button_color=GLASS_BORDER,
            button_hover_color=ACCENT, text_color=TEXT, width=280,
        )
        self.version_menu.pack(side="left", padx=4)
        self.reload_btn = ghost_btn(picker, "刷新列表", self._load, width=100)
        self.reload_btn.pack(side="left", padx=4)

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
            bottom, text=self._loading_hint, text_color=TEXT_DIM, font=font(12), anchor="w"
        )
        self.status.pack(side="left", padx=12, fill="x", expand=True)

    def _show_centered(self) -> None:
        show_centered(self, self._W, self._H)

    # ---------------------------------------------------------------- 数据

    def _load(self) -> None:
        if self._busy:
            return
        self._busy = True
        if self._loading_hint:
            self.status.configure(text=self._loading_hint, text_color=TEXT_MUTED)

        def worker() -> None:
            try:
                items, current = self._loader()
            except vs.VersionServiceError as e:
                # 必须先取出消息：except 块结束时 e 会被解绑，延迟执行的 lambda 里读不到它
                msg = str(e)
                self.after(0, lambda: self._fail(msg))
                return
            self.after(0, lambda: self._fill(items, current))

        threading.Thread(target=worker, name="version-picker", daemon=True).start()

    def _reset_empty(self) -> None:
        self._items = []
        self.version_menu.configure(values=[_EMPTY])
        self.version_var.set(_EMPTY)
        self.ok_btn.configure(state="disabled")
        self.detail.configure(text="")
        self.notes.configure(text="")

    def _fail(self, message: str) -> None:
        self._busy = False
        self._reset_empty()
        self.status.configure(text=message, text_color=DANGER)

    def _fill(self, items: list[dict], current: str) -> None:
        self._busy = False
        self._items = items
        names = [str(i.get("version") or "") for i in items if i.get("version")]
        if not names:
            self._reset_empty()
            self.status.configure(text=self._empty_hint, text_color=WARNING)
            return
        self.version_menu.configure(values=names)
        # 默认选当前发布版本；没有就用最新的一个
        self.version_var.set(current if current in names else names[0])
        self.ok_btn.configure(state="normal")
        self.status.configure(text=f"共 {len(names)} 个可选版本", text_color=ACCENT)
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

        parts: list[str] = []
        size = int(item.get("size") or 0)
        if size:
            parts.append(f"{size / 1024 / 1024:.1f} MB")
        if item.get("local"):
            parts.append("本机备份")
        if item.get("is_current"):
            parts.append("当前发布版本")
        sha = str(item.get("sha256") or "")
        if sha:
            parts.append(f"sha256 {sha[:12]}…")
        elif not item.get("local"):
            # 本机备份是从磁盘复制回去的，本来就没有校验和，不必提示
            parts.append("无校验和")
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


def _ask(parent, **kwargs) -> tuple[str, str]:
    """模态弹出选择窗口并等它关闭，返回 (版本号, sha256)；取消时版本号为空。

    子窗口的 `grab_set` 会顶掉父窗口的，关掉它之后必须把输入权交回父窗口，
    否则父对话框会失去模态、主窗口变成可点。
    """
    dlg = VersionPickerDialog(parent, **kwargs)
    parent.wait_window(dlg)
    try:
        parent.grab_set()
    except tk.TclError:
        pass
    return dlg.selected, dlg.selected_sha256


def ask_backend_version(parent, target: vs.BackendTarget) -> tuple[str, str]:
    """从后端已上传的版本清单里选一个，返回 (版本号, sha256)。"""
    return _ask(
        parent,
        title="更新到指定版本",
        hint="列出后端已上传的全部版本（新版在前），不限于当前发布版本；"
             "可用于灰度或回退到某个具体版本。",
        loader=lambda: vs.fetch_available_versions(target),
        empty_hint="后端还没有任何客户端安装包，请先在管理后台「客户端版本」上传",
        loading_hint="正在获取版本清单…",
    )


def ask_local_backup(parent, versions: Sequence[str]) -> str:
    """从本机备份版本里选一个，返回版本号；取消时为空串。"""
    picked, _ = _ask(
        parent,
        title="回滚到本机备份",
        hint="列出所选实例都存在的本机备份版本（新版在前）。回滚从 .backups 就地恢复、"
             "不依赖网络，且回滚前会先把当前版本另存一份。",
        loader=lambda: ([{"version": v, "local": True} for v in versions], ""),
        empty_hint="所选实例没有共同的本机备份版本",
    )
    return picked
