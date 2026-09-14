"""CustomTkinter 主界面：深色金融科技风 + 玻璃拟态卡片。"""
from __future__ import annotations

import os
import subprocess
import threading
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

import version_service as vs
from client_deploy import count_pending_upgrades, read_version_near
from models import InstanceConfig, default_label_from_path, shorten_path
from process_manager import ProcessManager
from store import load_instances, load_panel_config, save_instances
from tray_icon import TrayController, apply_window_icon, tray_available
from version import get_version
from theme import (
    ACCENT,
    ACCENT_DIM,
    ACCENT_SOFT,
    BG,
    BG_ELEVATED,
    CYAN,
    DANGER,
    DANGER_DIM,
    DEAD,
    GLASS_BORDER,
    GLASS_BORDER_ACTIVE,
    GLASS_SOFT,
    TEXT,
    TEXT_DIM,
    TEXT_MUTED,
    WARNING,
    apply_theme,
    danger_btn,
    font,
    ghost_btn,
    glass_card,
    health_color,
    mono,
    primary_btn,
    soft_card,
    status_chip,
)
from viewmodels import (
    DetailVM,
    ListItemVM,
    build_detail,
    build_list,
    list_order,
)


# 顶栏「版本更新」按钮：无更新时的常态文案与宽度
_VERSION_BTN_IDLE = "版本更新"
_VERSION_BTN_W = 100
_VERSION_BTN_W_ALERT = 190

# 启动约 3 秒后查一次后端发布版本，之后每 1 分钟复检
_VERSION_CHECK_INTERVAL_MS = 60 * 1000
_VERSION_CHECK_FIRST_MS = 3000


@dataclass
class _ListRow:
    card: ctk.CTkFrame
    title: ctk.CTkLabel
    dot: ctk.CTkLabel
    path: ctk.CTkLabel
    status: ctk.CTkLabel
    snapshot: ListItemVM | None = None



class EditInstanceDialog(ctk.CTkToplevel):
    """编辑实例标签 / 客户端路径 / 工作目录。"""

    _W = 620
    _H = 420

    def __init__(self, master, cfg: InstanceConfig) -> None:
        super().__init__(master)
        self.title("编辑实例")
        self.resizable(False, False)
        self.configure(fg_color=BG)
        apply_window_icon(self)
        self.transient(master)
        self.result: InstanceConfig | None = None
        self._cfg = cfg
        # 先隐藏，布局完成后再居中显示，避免闪在左上角
        self.withdraw()

        card = glass_card(self)
        card.pack(fill="both", expand=True, padx=16, pady=16)
        card.grid_columnconfigure(1, weight=1)
        card.grid_rowconfigure(6, weight=1)

        ctk.CTkLabel(
            card, text="安全编辑", text_color=ACCENT, font=font(11, "bold")
        ).grid(row=0, column=0, columnspan=3, padx=16, pady=(16, 2), sticky="w")
        ctk.CTkLabel(
            card, text="编辑节点实例", text_color=TEXT, font=font(18, "bold")
        ).grid(row=1, column=0, columnspan=3, padx=16, pady=(0, 12), sticky="w")

        ctk.CTkLabel(card, text="客户端标签", text_color=TEXT_MUTED, font=font(12)).grid(
            row=2, column=0, padx=16, pady=8, sticky="w"
        )
        self.name_var = tk.StringVar(value=cfg.name)
        ctk.CTkEntry(
            card, textvariable=self.name_var, fg_color=BG_ELEVATED,
            border_color=GLASS_BORDER, text_color=TEXT, height=36, corner_radius=10,
        ).grid(row=2, column=1, columnspan=2, padx=16, pady=8, sticky="ew")

        ctk.CTkLabel(card, text="客户端路径", text_color=TEXT_MUTED, font=font(12)).grid(
            row=3, column=0, padx=16, pady=8, sticky="w"
        )
        self.path_var = tk.StringVar(value=cfg.exe_path)
        ctk.CTkEntry(
            card, textvariable=self.path_var, fg_color=BG_ELEVATED,
            border_color=GLASS_BORDER, text_color=TEXT, height=36, corner_radius=10,
        ).grid(row=3, column=1, padx=(16, 4), pady=8, sticky="ew")
        ghost_btn(card, "浏览…", self._browse_exe, width=72).grid(
            row=3, column=2, padx=(4, 16), pady=8
        )

        ctk.CTkLabel(card, text="工作目录", text_color=TEXT_MUTED, font=font(12)).grid(
            row=4, column=0, padx=16, pady=8, sticky="w"
        )
        self.cwd_var = tk.StringVar(value=cfg.cwd)
        ctk.CTkEntry(
            card, textvariable=self.cwd_var, fg_color=BG_ELEVATED,
            border_color=GLASS_BORDER, text_color=TEXT, height=36, corner_radius=10,
        ).grid(row=4, column=1, padx=(16, 4), pady=8, sticky="ew")
        ghost_btn(card, "浏览…", self._browse_cwd, width=72).grid(
            row=4, column=2, padx=(4, 16), pady=8
        )

        tip = ctk.CTkLabel(
            card,
            text="本机回环管理 · 标签默认取文件名 · 路径指向 node_client.exe",
            text_color=TEXT_DIM,
            font=font(11),
            anchor="w",
        )
        tip.grid(row=5, column=0, columnspan=3, padx=16, pady=(4, 8), sticky="ew")

        btns = ctk.CTkFrame(card, fg_color="transparent")
        btns.grid(row=6, column=0, columnspan=3, sticky="s", pady=(8, 18))
        primary_btn(btns, "保存", self._save, width=120).pack(side="left", padx=8)
        ghost_btn(btns, "取消", self.destroy, width=120).pack(side="left", padx=8)

        self.bind("<Return>", lambda _e: self._save())
        self.bind("<Escape>", lambda _e: self.destroy())
        self.after(20, self._show_centered)

    def _show_centered(self) -> None:
        """相对主窗口居中并显示，保证内容与按钮完整可见。"""
        self.update_idletasks()
        w, h = self._W, self._H
        try:
            parent = self.master
            parent.update_idletasks()
            px = int(parent.winfo_rootx())
            py = int(parent.winfo_rooty())
            pw = max(int(parent.winfo_width()), 1)
            ph = max(int(parent.winfo_height()), 1)
        except (tk.TclError, AttributeError):
            px = py = 0
            pw = int(self.winfo_screenwidth())
            ph = int(self.winfo_screenheight())
        x = px + max(0, (pw - w) // 2)
        y = py + max(0, (ph - h) // 2)
        # 避免超出屏幕
        sw = int(self.winfo_screenwidth())
        sh = int(self.winfo_screenheight())
        x = min(max(0, x), max(0, sw - w))
        y = min(max(0, y), max(0, sh - h))
        self.geometry(f"{w}x{h}+{x}+{y}")
        self.deiconify()
        self.lift()
        try:
            self.grab_set()
        except tk.TclError:
            pass
        self.focus_force()

    def _browse_exe(self) -> None:
        path = filedialog.askopenfilename(
            parent=self,
            title="选择 node_client.exe",
            filetypes=[
                ("Executable", "*.exe"),
                ("Python", "*.py"),
                ("All", "*.*"),
            ],
            initialdir=str(Path(self.path_var.get() or ".").parent),
        )
        if not path:
            return
        old_path = self.path_var.get().strip()
        old_cwd = self.cwd_var.get().strip()
        self.path_var.set(path)
        if self.name_var.get().strip() in (
            "",
            default_label_from_path(old_path),
            default_label_from_path(path),
        ):
            self.name_var.set(default_label_from_path(path))
        if not old_cwd or old_cwd == str(Path(old_path).parent):
            self.cwd_var.set(str(Path(path).parent))

    def _browse_cwd(self) -> None:
        path = filedialog.askdirectory(
            parent=self,
            title="选择工作目录",
            initialdir=self.cwd_var.get() or ".",
        )
        if path:
            self.cwd_var.set(path)

    def _save(self) -> None:
        name = self.name_var.get().strip()
        exe = self.path_var.get().strip()
        cwd = self.cwd_var.get().strip()
        if not exe:
            messagebox.showerror("校验失败", "客户端路径不能为空", parent=self)
            return
        if not Path(exe).exists():
            if not messagebox.askyesno(
                "路径不存在",
                f"文件不存在：\n{exe}\n\n仍要保存吗？",
                parent=self,
            ):
                return
        if not name:
            name = default_label_from_path(exe)
        if not cwd:
            cwd = str(Path(exe).parent)
        self._cfg.name = name
        self._cfg.exe_path = exe
        self._cfg.cwd = cwd
        self.result = self._cfg
        self.destroy()


class DashboardApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        apply_theme()
        self.title(f"节点控制台 v{get_version()}")
        apply_window_icon(self, as_default=True)
        self.geometry("1200x760")
        self.minsize(980, 640)
        self.configure(fg_color=BG)

        self.manager = ProcessManager(on_change=self._on_runtime_change)
        self.manager.load(load_instances())
        self._selected_id: str | None = None
        self._list_rows: dict[str, _ListRow] = {}
        self._list_order: tuple[str, ...] = ()
        self._chip_widgets: dict[str, ctk.CTkLabel] = {}
        self._detail_snap: DetailVM | None = None
        self._log_snap: str | None = None
        self._refresh_scheduled = False
        self._log_tick = 0
        self._in_tray = False
        self._quitting = False
        self._tray: TrayController | None = None
        self._tray_hide_job: str | None = None
        self._restore_repair_job: str | None = None
        self._remote_version = ""

        self._build_layout()
        self._bind_list()
        self._bind_detail(refresh_log=True)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        # 最小化进托盘；恢复时修复 CTk Canvas 残影
        self.bind("<Unmap>", self._on_unmap)
        self.bind("<Map>", self._on_map)
        self.after(1000, self._tick_ui)
        self.after(200, self._setup_tray)
        self.after(_VERSION_CHECK_FIRST_MS, self._check_remote_version)

    def _build_layout(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        # —— 顶栏：品牌 + 安全徽章 ——
        header = glass_card(self, corner_radius=0, border_width=0, fg_color=BG_ELEVATED)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_columnconfigure(1, weight=1)

        brand = ctk.CTkFrame(header, fg_color="transparent")
        brand.grid(row=0, column=0, padx=20, pady=14, sticky="w")
        ctk.CTkLabel(
            brand, text="节点控制台", text_color=ACCENT, font=font(22, "bold")
        ).pack(anchor="w")
        ctk.CTkLabel(
            brand,
            text="本机安全控制  ·  节点进程编排与监护",
            text_color=TEXT_MUTED,
            font=font(12),
        ).pack(anchor="w")

        badges = ctk.CTkFrame(header, fg_color="transparent")
        badges.grid(row=0, column=2, padx=20, pady=14, sticky="e")
        self.version_btn = ghost_btn(
            badges, _VERSION_BTN_IDLE, self._open_version_update, width=_VERSION_BTN_W
        )
        self.version_btn.pack(side="left", padx=(0, 8))
        ghost_btn(badges, "连接配置", self._open_connection_config, width=100).pack(
            side="left", padx=(0, 12)
        )
        status_chip(badges, "仅本机回环", ACCENT).pack(side="left", padx=4)
        status_chip(badges, "禁止公网绑定", CYAN).pack(side="left", padx=4)
        status_chip(badges, "支持 TLS/WS", TEXT_MUTED).pack(side="left", padx=4)

        # —— 主体 ——
        body = ctk.CTkFrame(self, fg_color=BG)
        body.grid(row=1, column=0, sticky="nsew", padx=14, pady=14)
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)

        # 左侧实例列表卡片
        left = glass_card(body, width=340)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        left.grid_propagate(False)
        left.grid_rowconfigure(2, weight=1)

        left_head = ctk.CTkFrame(left, fg_color="transparent")
        left_head.grid(row=0, column=0, sticky="ew", padx=14, pady=(14, 4))
        ctk.CTkLabel(
            left_head, text="实例管理", text_color=ACCENT, font=font(11, "bold")
        ).pack(anchor="w")
        ctk.CTkLabel(
            left_head, text="实例列表", text_color=TEXT, font=font(16, "bold")
        ).pack(anchor="w")

        self.list_frame = ctk.CTkScrollableFrame(
            left, fg_color="transparent", scrollbar_button_color=GLASS_BORDER,
            scrollbar_button_hover_color=ACCENT,
        )
        self.list_frame.grid(row=2, column=0, sticky="nsew", padx=10, pady=6)

        btn_row = ctk.CTkFrame(left, fg_color="transparent")
        btn_row.grid(row=3, column=0, sticky="ew", padx=12, pady=(4, 4))
        primary_btn(btn_row, "导入节点", self._import_node, width=120).pack(
            side="left", padx=2, fill="x", expand=True
        )

        # 本地文件操作：不经后端，直接用本机已有的 exe
        btn_row0 = ctk.CTkFrame(left, fg_color="transparent")
        btn_row0.grid(row=6, column=0, sticky="ew", padx=12, pady=(0, 14))
        ghost_btn(btn_row0, "手工添加", self._add_instance, width=120).pack(
            side="left", padx=2
        )
        ghost_btn(btn_row0, "手工替换全部", self._batch_replace, width=120).pack(
            side="left", padx=2
        )

        btn_row2 = ctk.CTkFrame(left, fg_color="transparent")
        btn_row2.grid(row=4, column=0, sticky="ew", padx=12, pady=(0, 4))
        primary_btn(btn_row2, "全部启动", self._batch_start, width=120).pack(
            side="left", padx=2
        )
        danger_btn(btn_row2, "全部停止", self._batch_stop, width=120).pack(
            side="left", padx=2
        )

        btn_row3 = ctk.CTkFrame(left, fg_color="transparent")
        btn_row3.grid(row=5, column=0, sticky="ew", padx=12, pady=(0, 4))
        ghost_btn(btn_row3, "全部开守护", self._batch_daemon_on, width=120).pack(
            side="left", padx=2
        )
        ghost_btn(btn_row3, "全部关守护", self._batch_daemon_off, width=120).pack(
            side="left", padx=2
        )

        # 右侧详情
        right = ctk.CTkFrame(body, fg_color="transparent")
        right.grid(row=0, column=1, sticky="nsew")
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(3, weight=1)

        # 标题卡
        title_card = glass_card(right)
        title_card.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        title_card.grid_columnconfigure(0, weight=1)

        self.detail_title = ctk.CTkLabel(
            title_card, text="未选择实例", text_color=TEXT, font=font(20, "bold"), anchor="w"
        )
        self.detail_title.grid(row=0, column=0, sticky="w", padx=16, pady=(14, 2))
        self.detail_sub = ctk.CTkLabel(
            title_card,
            text="请选择左侧实例，查看安全状态与运行快照",
            text_color=TEXT_MUTED,
            font=font(12),
            anchor="w",
        )
        self.detail_sub.grid(row=1, column=0, sticky="w", padx=16, pady=(0, 8))

        self.chip_row = ctk.CTkFrame(title_card, fg_color="transparent")
        self.chip_row.grid(row=2, column=0, sticky="w", padx=14, pady=(0, 12))

        # 操作卡
        actions_card = glass_card(right)
        actions_card.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        actions = ctk.CTkFrame(actions_card, fg_color="transparent")
        actions.pack(fill="x", padx=12, pady=12)
        self.btn_start = primary_btn(actions, "启动", self._start, width=92)
        self.btn_start.pack(side="left", padx=4)
        self.btn_stop = danger_btn(actions, "停止", self._stop, width=92)
        self.btn_stop.pack(side="left", padx=4)
        ghost_btn(actions, "节点配置", self._edit_node_env, width=100).pack(side="left", padx=4)
        ghost_btn(actions, "刷新健康", self._refresh_health, width=100).pack(side="left", padx=4)
        ghost_btn(actions, "打开目录", self._open_cwd, width=100).pack(side="left", padx=4)
        self.daemon_var = ctk.BooleanVar(value=False)
        self.daemon_switch = ctk.CTkSwitch(
            actions,
            text="守护进程",
            variable=self.daemon_var,
            command=self._toggle_daemon,
            progress_color=ACCENT,
            button_color=TEXT,
            button_hover_color=ACCENT,
            text_color=TEXT,
            font=font(13),
        )
        self.daemon_switch.pack(side="left", padx=14)

        # 摘要卡
        summary_card = soft_card(right)
        summary_card.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        self.summary = ctk.CTkLabel(
            summary_card,
            text="",
            justify="left",
            anchor="w",
            text_color=TEXT_MUTED,
            font=mono(12),
        )
        self.summary.pack(fill="x", padx=14, pady=12)

        # 任务 + 日志
        mid = glass_card(right)
        mid.grid(row=3, column=0, sticky="nsew")
        mid.grid_columnconfigure(0, weight=1)
        mid.grid_rowconfigure(1, weight=1)
        mid.grid_rowconfigure(3, weight=2)

        ctk.CTkLabel(
            mid, text="策略任务", text_color=ACCENT, font=font(11, "bold"), anchor="w"
        ).grid(row=0, column=0, sticky="w", padx=14, pady=(12, 2))
        self.tasks = ctk.CTkTextbox(
            mid,
            height=110,
            fg_color=BG_ELEVATED,
            text_color=TEXT,
            border_color=GLASS_BORDER,
            border_width=1,
            corner_radius=10,
            font=mono(12),
        )
        self.tasks.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 8))

        log_bar = ctk.CTkFrame(mid, fg_color="transparent")
        log_bar.grid(row=2, column=0, sticky="ew", padx=14)
        ctk.CTkLabel(
            log_bar, text="运行日志", text_color=ACCENT, font=font(11, "bold")
        ).pack(side="left")
        ghost_btn(log_bar, "清空显示", self._clear_log, width=96).pack(side="right")
        self.log_box = ctk.CTkTextbox(
            mid,
            fg_color=BG_ELEVATED,
            text_color=TEXT,
            border_color=GLASS_BORDER,
            border_width=1,
            corner_radius=10,
            font=mono(12),
        )
        self.log_box.grid(row=3, column=0, sticky="nsew", padx=12, pady=(4, 12))

    def _persist(self) -> None:
        save_instances(self.manager.configs())

    def _tone_color(self, tone: str, *, alive: bool = True, health: str = "unknown") -> str:
        if tone == "health":
            return health_color(health, alive)
        if tone == "cyan":
            return CYAN
        if tone == "accent":
            return ACCENT
        if tone == "warning":
            return WARNING
        if tone == "muted":
            return TEXT_MUTED
        return DEAD

    def _create_list_row(self, vm: ListItemVM) -> _ListRow:
        color = health_color(vm.health, vm.alive)
        card = ctk.CTkFrame(
            self.list_frame,
            fg_color=ACCENT_SOFT if vm.selected else GLASS_SOFT,
            border_width=1,
            border_color=GLASS_BORDER_ACTIVE if vm.selected else GLASS_BORDER,
            corner_radius=12,
        )
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill="x", padx=12, pady=10)
        top = ctk.CTkFrame(inner, fg_color="transparent")
        top.pack(fill="x", pady=(0, 2))
        title = ctk.CTkLabel(
            top, text=vm.title, text_color=TEXT, font=font(14, "bold"),
            anchor="w", height=22,
        )
        title.pack(side="left")
        dot = ctk.CTkLabel(
            top, text="●", text_color=color, font=font(12), width=18, height=22,
        )
        dot.pack(side="right")
        path = ctk.CTkLabel(
            inner, text=vm.path_short, text_color=TEXT_DIM, font=mono(11),
            anchor="w", height=20,
        )
        path.pack(fill="x", pady=(0, 2))
        status = ctk.CTkLabel(
            inner,
            text=vm.status_line,
            text_color=color,
            font=font(11, "bold"),
            anchor="w",
            height=22,
        )
        status.pack(fill="x")

        def _bind(widget, iid=vm.id):
            widget.bind("<Button-1>", lambda _e, i=iid: self._select(i))
            for child in widget.winfo_children():
                _bind(child, iid)

        _bind(card)
        # 编辑/移除放在列表项内；放在 _bind 之后，避免覆盖按钮自身点击。
        actions = ctk.CTkFrame(inner, fg_color="transparent")
        actions.pack(fill="x", pady=(6, 0))
        ghost_btn(
            actions,
            "编辑",
            lambda iid=vm.id: self._edit_instance(iid),
            width=56,
            height=26,
            font=font(12),
        ).pack(side="left", padx=(0, 4))
        danger_btn(
            actions,
            "移除",
            lambda iid=vm.id: self._remove_instance(iid),
            width=56,
            height=26,
            font=font(12),
        ).pack(side="left")
        return _ListRow(card=card, title=title, dot=dot, path=path, status=status, snapshot=vm)

    def _apply_list_row(self, row: _ListRow, vm: ListItemVM) -> None:
        if row.snapshot == vm:
            return
        color = health_color(vm.health, vm.alive)
        if row.snapshot is None or row.snapshot.title != vm.title:
            row.title.configure(text=vm.title)
        if row.snapshot is None or row.snapshot.path_short != vm.path_short:
            row.path.configure(text=vm.path_short)
        if (
            row.snapshot is None
            or row.snapshot.status_line != vm.status_line
            or row.snapshot.health != vm.health
            or row.snapshot.alive != vm.alive
        ):
            row.status.configure(text=vm.status_line, text_color=color)
            row.dot.configure(text_color=color)
        if row.snapshot is None or row.snapshot.selected != vm.selected:
            row.card.configure(
                fg_color=ACCENT_SOFT if vm.selected else GLASS_SOFT,
                border_color=GLASS_BORDER_ACTIVE if vm.selected else GLASS_BORDER,
            )
        row.snapshot = vm

    def _bind_list(self) -> None:
        """按 ViewModel 差分同步左侧列表：复用卡片，避免整表销毁闪烁。"""
        items = build_list(self.manager, self._selected_id)
        order = list_order(items)
        wanted = {vm.id for vm in items}
        for iid in list(self._list_rows):
            if iid not in wanted:
                self._list_rows[iid].card.destroy()
                del self._list_rows[iid]
        for vm in items:
            row = self._list_rows.get(vm.id)
            if row is None:
                self._list_rows[vm.id] = self._create_list_row(vm)
            else:
                self._apply_list_row(row, vm)
        if order != self._list_order:
            for iid in order:
                self._list_rows[iid].card.pack_forget()
            for iid in order:
                self._list_rows[iid].card.pack(fill="x", pady=5, padx=2)
            self._list_order = order

    def _ensure_chip(self, key: str) -> ctk.CTkLabel:
        chip = self._chip_widgets.get(key)
        if chip is not None:
            return chip
        chip = status_chip(self.chip_row, "", TEXT_DIM)
        self._chip_widgets[key] = chip
        return chip

    def _bind_chips(self, detail: DetailVM) -> None:
        for widget in self._chip_widgets.values():
            widget.pack_forget()
        for chip_vm in detail.chips:
            widget = self._ensure_chip(chip_vm.key)
            if not chip_vm.visible or not chip_vm.text:
                continue
            color = self._tone_color(
                chip_vm.tone, alive=detail.alive, health=detail.health
            )
            widget.configure(text=f"  {chip_vm.text}  ", text_color=color)
            widget.pack(side="left", padx=3)

    def _bind_detail(self, *, refresh_log: bool = False) -> None:
        """按 DetailVM 就地更新右侧；未变化则跳过。"""
        mp = self._selected()
        detail = build_detail(mp)
        prev = self._detail_snap
        if prev != detail:
            self.detail_title.configure(text=detail.title)
            self.detail_sub.configure(text=detail.subtitle)
            if not detail.empty:
                self.daemon_switch.configure(command=None)
                self.daemon_var.set(detail.daemon)
                self.daemon_switch.configure(command=self._toggle_daemon)
            if prev is None or prev.chips != detail.chips:
                self._bind_chips(detail)
            if (
                prev is None
                or prev.busy != detail.busy
                or prev.alive != detail.alive
                or prev.empty != detail.empty
            ):
                self._sync_action_buttons(mp)
            if prev is None or prev.summary != detail.summary:
                self.summary.configure(text=detail.summary)
            if prev is None or prev.tasks_text != detail.tasks_text:
                self.tasks.delete("1.0", "end")
                if detail.tasks_text:
                    self.tasks.insert("end", detail.tasks_text)
            self._detail_snap = detail

        if refresh_log:
            self._bind_log(mp)

    def _bind_log(self, mp) -> None:
        if mp is None:
            if self._log_snap != "":
                self.log_box.delete("1.0", "end")
                self._log_snap = ""
            return
        text = mp.read_log_tail()
        if text == self._log_snap:
            return
        at_end = self.log_box.yview()[1] >= 0.98
        self.log_box.delete("1.0", "end")
        self.log_box.insert("end", text)
        if at_end:
            self.log_box.see("end")
        self._log_snap = text

    def _select(self, instance_id: str) -> None:
        self._selected_id = instance_id
        self._detail_snap = None
        self._log_snap = None
        self._bind_list()
        self._bind_detail(refresh_log=True)

    def _selected(self):
        if not self._selected_id:
            return None
        return self.manager.get(self._selected_id)

    def _sync_action_buttons(self, mp) -> None:
        """启动=青绿强调（可启时）；停止=红色强调（可停时）；否则灰色弱化。"""
        muted = {
            "fg_color": DEAD,
            "hover_color": TEXT_DIM,
            "text_color": TEXT,
            "state": "disabled",
        }
        start_on = {
            "fg_color": ACCENT_DIM,
            "hover_color": ACCENT,
            "text_color": "#04140F",
            "state": "normal",
        }
        stop_on = {
            "fg_color": DANGER_DIM,
            "hover_color": DANGER,
            "text_color": TEXT,
            "state": "normal",
        }
        if mp is None:
            self.btn_start.configure(**muted)
            self.btn_stop.configure(**muted)
            return
        busy = bool(mp.runtime.busy)
        alive = bool(mp.runtime.process_alive)
        if busy:
            self.btn_start.configure(**muted)
            self.btn_stop.configure(**muted)
            return
        if alive:
            self.btn_start.configure(**muted)
            self.btn_stop.configure(**stop_on)
        else:
            self.btn_start.configure(**start_on)
            self.btn_stop.configure(**muted)

    def _on_runtime_change(self, _instance_id: str) -> None:
        if self._refresh_scheduled:
            return
        self._refresh_scheduled = True
        self.after(80, self._flush_runtime_refresh)

    def _flush_runtime_refresh(self) -> None:
        self._refresh_scheduled = False
        self._safe_refresh(refresh_log=False)

    def _safe_refresh(self, *, refresh_log: bool = False) -> None:
        try:
            self._bind_list()
            self._bind_detail(refresh_log=refresh_log)
        except tk.TclError:
            pass

    def _tick_ui(self) -> None:
        try:
            self._bind_list()
            self._bind_detail(refresh_log=True)
        except tk.TclError:
            return
        self.after(2000, self._tick_ui)

    def _refresh_list(self) -> None:
        self._bind_list()

    def _update_detail(self) -> None:
        self._bind_detail(refresh_log=True)

    def _add_instance(self) -> None:
        path = filedialog.askopenfilename(
            title="选择 node_client.exe",
            filetypes=[
                ("Executable", "*.exe"),
                ("Python", "*.py"),
                ("All", "*.*"),
            ],
        )
        if not path:
            return
        exe = Path(path)
        cfg = InstanceConfig.create(
            name=default_label_from_path(str(exe)),
            exe_path=str(exe),
            cwd=str(exe.parent),
        )
        self.manager.add(cfg)
        self._persist()
        self._selected_id = cfg.id
        self._refresh_list()
        self._update_detail()
        self._refresh_version_badge()
        self._edit_instance()

    def _edit_instance(self, instance_id: str | None = None) -> None:
        if instance_id and instance_id != self._selected_id:
            self._select(instance_id)
        mp = self.manager.get(instance_id) if instance_id else self._selected()
        if mp is None:
            messagebox.showinfo("提示", "请先选择一个实例")
            return
        dialog = EditInstanceDialog(self, mp.cfg)
        self.wait_window(dialog)
        if dialog.result is None:
            return
        self._persist()
        self._refresh_list()
        self._update_detail()

    def _batch_start(self) -> None:
        configs = self.manager.configs()
        if not configs:
            messagebox.showinfo("提示", "请先添加至少一个实例")
            return
        if not messagebox.askyesno(
            "全部启动",
            f"将启动全部 {len(configs)} 个实例（已在运行的会跳过）。是否继续？",
        ):
            return

        def job():
            results = self.manager.start_all()
            ok_n = sum(1 for *_rest, ok, _m in results if ok)
            lines = [f"完成：成功 {ok_n}/{len(results)}"]
            for _iid, name, ok, msg in results:
                lines.append(f"{'✓' if ok else '✗'} {name}: {msg}")
            text = "\n".join(lines)
            self.after(0, lambda: messagebox.showinfo("全部启动结果", text))
            self.after(0, self._persist)

        self._run_async("全部启动", job)

    def _batch_stop(self) -> None:
        configs = self.manager.configs()
        if not configs:
            messagebox.showinfo("提示", "请先添加至少一个实例")
            return
        if not messagebox.askyesno(
            "全部停止",
            f"将停止全部 {len(configs)} 个实例（已停止的会跳过）。是否继续？",
        ):
            return

        def job():
            results = self.manager.stop_all()
            ok_n = sum(1 for *_rest, ok, _m in results if ok)
            lines = [f"完成：成功 {ok_n}/{len(results)}"]
            for _iid, name, ok, msg in results:
                lines.append(f"{'✓' if ok else '✗'} {name}: {msg}")
            text = "\n".join(lines)
            self.after(0, lambda: messagebox.showinfo("全部停止结果", text))
            self.after(0, self._persist)

        self._run_async("全部停止", job)

    def _batch_daemon_on(self) -> None:
        self._batch_set_daemon(True)

    def _batch_daemon_off(self) -> None:
        self._batch_set_daemon(False)

    def _batch_set_daemon(self, enabled: bool) -> None:
        configs = self.manager.configs()
        if not configs:
            messagebox.showinfo("提示", "请先添加至少一个实例")
            return
        title = "全部开守护" if enabled else "全部关守护"
        action = "开启" if enabled else "关闭"
        if not messagebox.askyesno(
            title,
            f"将{action}全部 {len(configs)} 个实例的守护进程（状态已符合的会跳过）。是否继续？",
        ):
            return

        def job():
            results = self.manager.set_daemon_all(enabled)
            ok_n = sum(1 for *_rest, ok, _m in results if ok)
            lines = [f"完成：成功 {ok_n}/{len(results)}"]
            for _iid, name, ok, msg in results:
                lines.append(f"{'✓' if ok else '✗'} {name}: {msg}")
            text = "\n".join(lines)
            self.after(0, lambda: messagebox.showinfo(f"{title}结果", text))
            self.after(0, self._persist)
            self.after(0, self._refresh_list)
            self.after(0, self._update_detail)

        self._run_async(title, job)

    def _open_connection_config(self) -> None:
        """配置后端 API 地址与节点令牌（供版本更新等功能访问后端）。"""
        from connection_dialog import ConnectionConfigDialog

        # 改完地址或令牌，原来查不通的后端可能就通了，立刻复检一次
        ConnectionConfigDialog(
            self, self.manager, on_saved=lambda: self._check_remote_version(repeat=False)
        )

    def _import_node(self) -> None:
        """选 MT5 目录 + 指定版本，从后端拉客户端部署成新实例。"""
        from import_dialog import ImportNodeDialog

        def on_created(cfg: InstanceConfig) -> None:
            self._persist()
            self._selected_id = cfg.id
            self._refresh_list()
            self._update_detail()
            self._refresh_version_badge()

        ImportNodeDialog(self, self.manager, on_done=on_created)

    def _edit_node_env(self) -> None:
        """编辑选中实例的 node_client .env。"""
        mp = self._selected()
        if mp is None:
            messagebox.showinfo("提示", "请先选择一个实例")
            return
        from env_dialog import EnvConfigDialog

        dialog = EnvConfigDialog(self, mp.cfg)
        self.wait_window(dialog)
        if not dialog.saved:
            return
        # 配置在 node_client 进程内是一次性加载的，改完必须重启才生效
        alive = mp.runtime.process_alive or mp._status_reachable(timeout=0.3)
        if alive and messagebox.askyesno(
            "重启实例",
            f"「{mp.cfg.name}」正在运行，新配置需要重启后才生效。\n\n现在重启吗？",
        ):
            iid = mp.cfg.id

            def job():
                self.manager.stop(iid)
                self.manager.start(iid)

            self._run_async("重启", job)

    def _open_version_update(self) -> None:
        """打开版本更新对话框：从后端检测版本、批量更新 / 降级 / 本机回滚。"""
        if not self.manager.configs():
            messagebox.showinfo("提示", "请先添加至少一个实例")
            return
        from version_dialog import VersionUpdateDialog

        def on_done() -> None:
            self._persist()
            self._refresh_version_badge()

        VersionUpdateDialog(self, self.manager, on_done=on_done)

    # ------------------------------------------------------------ 版本更新提示

    def _check_remote_version(self, *, repeat: bool = True) -> None:
        """查后端当前发布版本，把「有新版可装」标到顶栏按钮上。

        静默失败：这只是个提示，后端不可达或没配令牌时不该弹窗打断本机运维。
        """
        if repeat:
            self.after(_VERSION_CHECK_INTERVAL_MS, self._check_remote_version)

        configs = self.manager.configs()
        if not configs:
            self._render_version_badge("", 0)
            return

        # 后端入口在主线程解析后再交给后台线程：Tcl 解释器不是线程安全的
        target = vs.resolve_backend(
            load_panel_config(),
            [c.cwd or str(Path(c.exe_path).parent) for c in configs],
        )
        if not target.ready:
            self._render_version_badge("", 0)
            return

        def worker() -> None:
            try:
                data = vs.fetch_current_version(target)
            except vs.VersionServiceError:
                return
            version = str((data or {}).get("version") or "")
            self.after(0, lambda: self._on_remote_version(version))

        threading.Thread(target=worker, name="version-check", daemon=True).start()

    def _on_remote_version(self, version: str) -> None:
        self._remote_version = version
        self._refresh_version_badge()

    def _refresh_version_badge(self) -> None:
        """用已知的后端版本重算本机待更新数；不发请求，供装完/增删实例后调用。"""
        configs = self.manager.configs()
        pending = count_pending_upgrades(
            (read_version_near(c.exe_path) for c in configs), self._remote_version
        )
        self._render_version_badge(self._remote_version, pending)

    def _render_version_badge(self, version: str, pending: int) -> None:
        alert = bool(version) and pending > 0
        try:
            if alert:
                self.version_btn.configure(
                    text=f"有新版 v{version}（{pending}）",
                    width=_VERSION_BTN_W_ALERT,
                    text_color=WARNING,
                    border_color=WARNING,
                )
            else:
                self.version_btn.configure(
                    text=_VERSION_BTN_IDLE,
                    width=_VERSION_BTN_W,
                    text_color=TEXT,
                    border_color=GLASS_BORDER,
                )
        except tk.TclError:
            pass

    def _batch_replace(self) -> None:
        configs = self.manager.configs()
        if not configs:
            messagebox.showinfo("提示", "请先添加至少一个实例")
            return
        source = filedialog.askopenfilename(
            title="选择新的 node_client.exe",
            filetypes=[
                ("Executable", "*.exe"),
                ("All", "*.*"),
            ],
        )
        if not source:
            return
        from client_deploy import read_version_near

        new_ver = read_version_near(source) or "(未知)"
        lines = [
            f"本次将替换全部 {len(configs)} 个实例（不能只选其中几个）：",
            f"源文件: {source}",
            f"源版本: {new_ver}",
            "",
            "目标列表：",
        ]
        for cfg in configs:
            old = read_version_near(cfg.exe_path) or "-"
            lines.append(f"  · {cfg.name}  v{old}  →  {cfg.exe_path}")
        lines.extend([
            "",
            "规则：运行中的实例会先停止 → 备份原 exe 为 .bak → 覆盖 → 再自动启动。",
            "只想更新部分实例、或需要可回滚的版本化备份，请改用顶栏「版本更新」。",
            "是否继续？",
        ])
        if not messagebox.askyesno("手工替换全部 · 确认", "\n".join(lines)):
            return

        def job():
            results = self.manager.batch_replace(source, restart_if_was_running=True)
            ok_n = sum(1 for r in results if r.ok)
            fail = [r for r in results if not r.ok]
            summary = [f"完成：成功 {ok_n}/{len(results)}"]
            for r in results:
                summary.append(
                    f"{'✓' if r.ok else '✗'} {r.name}: "
                    f"v{r.old_version or '-'} → v{r.new_version or '-'} "
                    f"{r.message}"
                )
            text = "\n".join(summary)
            if fail:
                self.after(0, lambda: messagebox.showwarning("手工替换结果", text))
            else:
                self.after(0, lambda: messagebox.showinfo("手工替换结果", text))
            self.after(0, self._persist)
            self.after(0, self._refresh_version_badge)

        self._run_async("手工替换", job)

    def _remove_instance(self, instance_id: str | None = None) -> None:
        mp = self.manager.get(instance_id) if instance_id else self._selected()
        if mp is None:
            return
        if not messagebox.askyesno("确认", f"移除实例「{mp.cfg.name}」？若在运行将先停止。"):
            return
        iid = mp.cfg.id
        if self._selected_id == iid:
            self._selected_id = None

        def job():
            self.manager.remove(iid)
            self.after(0, self._persist)
            self.after(0, self._refresh_version_badge)

        self._run_async("移除", job)

    def _run_async(self, label: str, fn) -> None:
        """启停等可能阻塞的操作丢后台线程，避免卡住 UI。"""
        def worker():
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                # 必须先取出消息：except 块结束时 e 会被解绑，延迟执行的 lambda 里读不到它
                msg = str(e)
                self.after(0, lambda: messagebox.showerror(label, msg))
            finally:
                self.after(0, self._safe_refresh)

        threading.Thread(target=worker, name=f"ui-{label}", daemon=True).start()
        self.after(50, self._safe_refresh)

    def _start(self) -> None:
        mp = self._selected()
        if mp is None:
            return
        if mp.runtime.busy:
            return
        iid = mp.cfg.id

        def job():
            self.manager.start(iid)
            self.after(0, self._persist)

        self._run_async("启动", job)

    def _stop(self) -> None:
        mp = self._selected()
        if mp is None:
            return
        if mp.runtime.busy:
            return
        iid = mp.cfg.id

        def job():
            self.manager.stop(iid)
            self.after(0, self._persist)

        self._run_async("停止", job)

    def _refresh_health(self) -> None:
        mp = self._selected()
        if mp is None:
            return
        if mp.runtime.busy:
            return
        self._run_async("刷新健康", mp.refresh_health)

    def _toggle_daemon(self) -> None:
        mp = self._selected()
        if mp is None:
            return
        mp.set_daemon(self.daemon_var.get())
        self._persist()
        self._refresh_list()
        self._update_detail()

    def _open_cwd(self) -> None:
        mp = self._selected()
        if mp is None:
            return
        path = mp.cfg.cwd or str(Path(mp.cfg.exe_path).parent)
        if os.name == "nt":
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", path])

    def _setup_tray(self) -> None:
        if not tray_available():
            return
        self._tray = TrayController(
            schedule=lambda fn: self.after(0, fn),
            on_show=self._show_from_tray,
            on_quit=self._quit_app,
            title="节点控制台",
        )
        if self._tray.start():
            # 顶栏提示可托盘驻留
            pass

    def _on_unmap(self, event) -> None:
        # 仅处理主窗口最小化（iconic），忽略子控件 Unmap
        if event.widget is not self or self._quitting or self._in_tray:
            return
        try:
            if self.state() == "iconic":
                self._cancel_tray_hide_job()
                # 稍延迟：用户若立刻从任务栏恢复则取消进托盘，避免竞态残影
                self._tray_hide_job = self.after(120, self._hide_to_tray)
        except tk.TclError:
            pass

    def _on_map(self, event) -> None:
        if event.widget is not self or self._quitting:
            return
        # 快速还原：取消「最小化→托盘」
        self._cancel_tray_hide_job()
        if self._in_tray:
            return
        try:
            if self.state() != "normal":
                return
        except tk.TclError:
            return
        self._schedule_restore_repair()

    def _cancel_tray_hide_job(self) -> None:
        job = self._tray_hide_job
        self._tray_hide_job = None
        if job is not None:
            try:
                self.after_cancel(job)
            except Exception:  # noqa: BLE001
                pass

    def _schedule_restore_repair(self) -> None:
        """恢复可见后强制重绘（CTk Canvas 最小化残影）。"""
        if self._restore_repair_job is not None:
            try:
                self.after_cancel(self._restore_repair_job)
            except Exception:  # noqa: BLE001
                pass
        # 双拍：布局稳定后再清一次文字区残影
        self._restore_repair_job = self.after(40, lambda: self._repair_ui_after_restore(pass_no=1))

    def _repair_ui_after_restore(self, *, pass_no: int = 1) -> None:
        self._restore_repair_job = None
        if self._quitting or self._in_tray:
            return
        try:
            self.update_idletasks()
            self._force_ctk_redraw(self)
            # 使快照失效，重绑文本类控件（CTkTextbox 残影主要靠重写内容清除）
            self._detail_snap = None
            self._log_snap = None
            for row in self._list_rows.values():
                row.snapshot = None
            self._bind_list()
            self._bind_detail(refresh_log=True)
            self._nudge_win_composite()
            if pass_no < 2:
                self._restore_repair_job = self.after(
                    120, lambda: self._repair_ui_after_restore(pass_no=2)
                )
        except tk.TclError:
            pass

    def _force_ctk_redraw(self, root) -> None:
        stack = [root]
        while stack:
            w = stack.pop()
            try:
                stack.extend(w.winfo_children())
            except tk.TclError:
                continue
            draw = getattr(w, "_draw", None)
            if not callable(draw):
                continue
            try:
                draw(no_color_updates=False)
            except TypeError:
                try:
                    draw()
                except Exception:  # noqa: BLE001
                    pass
            except Exception:  # noqa: BLE001
                pass

    def _nudge_win_composite(self) -> None:
        """轻触 Windows 合成，缓解恢复后标签黑块/叠影。"""
        if os.name != "nt":
            return
        try:
            self.attributes("-alpha", 0.99)

            def _restore_alpha():
                try:
                    self.attributes("-alpha", 1.0)
                except tk.TclError:
                    pass

            self.after(30, _restore_alpha)
        except tk.TclError:
            pass

    def _hide_to_tray(self) -> None:
        self._tray_hide_job = None
        if self._quitting:
            return
        if self._tray is None or not tray_available():
            # 无托盘依赖时保持原关闭行为
            self._quit_app()
            return
        self._persist()
        self._in_tray = True
        try:
            # 先 normal 再 withdraw，避免停留在 iconic 导致任务栏还原半残状态
            if self.state() == "iconic":
                self.state("withdrawn")
            else:
                self.withdraw()
        except tk.TclError:
            try:
                self.withdraw()
            except tk.TclError:
                pass
        if not getattr(self, "_tray_tip_shown", False):
            self._tray_tip_shown = True
            self._tray.notify("节点控制台", "已最小化到托盘，右键可显示或退出")

    def _show_from_tray(self) -> None:
        if self._quitting:
            return
        self._cancel_tray_hide_job()
        self._in_tray = False
        try:
            self.deiconify()
            self.state("normal")
            self.lift()
            self.focus_force()
        except tk.TclError:
            pass
        self._schedule_restore_repair()

    def _on_close(self) -> None:
        """点关闭：进托盘（不退出）；真正退出走托盘「退出」。"""
        if self._tray is not None and tray_available():
            self._hide_to_tray()
            return
        self._quit_app()

    def _quit_app(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        self._persist()
        if self._tray is not None:
            self._tray.stop()
            self._tray = None
        for mp in self.manager._items.values():
            mp.shutdown_workers()
        try:
            self.destroy()
        except tk.TclError:
            pass

    def _clear_log(self) -> None:
        mp = self._selected()
        if mp is None:
            return
        mp.clear_log_display_file()
        self._update_detail()


def run_app() -> int:
    """启动主界面。返回退出码：0 正常；非 0 异常（供看门狗重启）。"""
    import traceback

    from single_instance import WINDOW_TITLE, acquire, activate_existing, release

    if not acquire():
        activate_existing(WINDOW_TITLE)
        try:
            import tkinter as tk
            from tkinter import messagebox

            tip = tk.Tk()
            tip.withdraw()
            messagebox.showinfo("提示", "节点控制台已在运行，仅支持单实例。\n已尝试切换到已有窗口。")
            tip.destroy()
        except Exception:  # noqa: BLE001
            pass
        return 0
    try:
        app = DashboardApp()
        app.mainloop()
        return 0
    except Exception:  # noqa: BLE001
        try:
            from recovery import _log

            _log("ui exception:\n" + traceback.format_exc())
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        return 1
    finally:
        release()
