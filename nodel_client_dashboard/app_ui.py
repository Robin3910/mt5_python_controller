"""CustomTkinter 主界面：深色金融科技风 + 玻璃拟态卡片。"""
from __future__ import annotations

import os
import subprocess
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog

import customtkinter as ctk

import version_service as vs
from busy_status import BusyStatus
from client_deploy import count_pending_upgrades, read_version_near
from models import InstanceConfig, default_label_from_path, shorten_path
from process_manager import ProcessManager
from dashboard_auth_ui import DashboardAuthUI
from auth_service import DashboardAuthError
from session_ui import SessionDialog, secured, requires_session
from store import load_instances, load_panel_config, save_instances
from terminal_account import read_algo_trading_allowed
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
    DIM,
    GLASS_BORDER,
    GLASS_BORDER_ACTIVE,
    GLASS_SOFT,
    GOLD,
    LINE,
    MUTE,
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
    toolbar_btn,
)
from viewmodels import (
    ALGO_TRADING_OFF_LABEL,
    DetailVM,
    ListItemVM,
    build_detail,
    build_hero,
    build_list,
    list_order,
)
from hero_panel import HeroPanel
from node_card import NodeCard, set_grid_columns


# 顶栏「版本更新」按钮：无更新时的常态文案与宽度
_VERSION_BTN_IDLE = "版本更新"
_VERSION_BTN_W = 100
_VERSION_BTN_W_ALERT = 190

# 节点卡右键菜单的项（顺序即菜单顺序）
CARD_MENU_ITEMS = ("打开目录", "刷新健康", "移除实例")

# 启动约 3 秒后查一次后端发布版本，之后每 1 分钟复检
_VERSION_CHECK_INTERVAL_MS = 60 * 1000
_VERSION_CHECK_FIRST_MS = 3000

# 手工替换的阶段文案：停 → 覆盖 → 重启
_SWAP_PHASE_LABELS = {
    "stop": "停止实例…",
    "apply": "覆盖文件…",
    "restart": "重新启动…",
}


@dataclass
class _ListRow:
    """旧列表行的结构定义。

    节点网格已改用 `node_card.NodeCard`，这里只保留类型占位以兼容旧引用。
    """

    card: ctk.CTkFrame
    title: ctk.CTkLabel
    dot: ctk.CTkLabel
    path: ctk.CTkLabel
    status: ctk.CTkLabel
    account: ctk.CTkLabel | None = None
    algo_warning: ctk.CTkLabel | None = None
    snapshot: ListItemVM | None = None



class EditInstanceDialog(SessionDialog):
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
            card, text="安全编辑", text_color=GOLD, font=font(11, "bold")
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
            border_color=GLASS_BORDER, text_color=TEXT, height=34, corner_radius=6,
        ).grid(row=2, column=1, columnspan=2, padx=16, pady=8, sticky="ew")

        ctk.CTkLabel(card, text="客户端路径", text_color=TEXT_MUTED, font=font(12)).grid(
            row=3, column=0, padx=16, pady=8, sticky="w"
        )
        self.path_var = tk.StringVar(value=cfg.exe_path)
        ctk.CTkEntry(
            card, textvariable=self.path_var, fg_color=BG_ELEVATED,
            border_color=GLASS_BORDER, text_color=TEXT, height=34, corner_radius=6,
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
            border_color=GLASS_BORDER, text_color=TEXT, height=34, corner_radius=6,
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

    @secured("edit_instance", lambda self: self._cfg)
    def _save(self) -> None:
        name = self.name_var.get().strip()
        exe = self.path_var.get().strip()
        cwd = self.cwd_var.get().strip()
        if not exe:
            messagebox.showerror("校验失败", "客户端路径不能为空", parent=self)
            return False
        if not Path(exe).exists():
            if not messagebox.askyesno(
                "路径不存在",
                f"文件不存在：\n{exe}\n\n仍要保存吗？",
                parent=self,
            ):
                return False
        if not name:
            name = default_label_from_path(exe)
        if not cwd:
            cwd = str(Path(exe).parent)
        self.dashboard.validate_instance_location(self._cfg, exe, cwd)
        if (exe, cwd) != (self._cfg.exe_path, self._cfg.cwd):
            mp = self.dashboard.manager.get(self._cfg.id)
            if mp is not None and mp.daemon_grant is not None:
                mp.daemon_grant.allowed = False
        self._cfg.name = name
        self._cfg.exe_path = exe
        self._cfg.cwd = cwd
        self.result = self._cfg
        self.destroy()


class DashboardApp(DashboardAuthUI, ctk.CTk):
    def after(self, ms, func=None, *args):
        if func is None or not hasattr(self, "session"):
            return super().after(ms, func, *args)
        generation = getattr(self.session._local, "generation", None)
        if generation is None:
            generation = self.session.generation
        def current():
            if generation == self.session.generation and not self._quitting:
                previous = getattr(self.session._local, "generation", None)
                self.session._local.generation = generation
                try:
                    return func(*args)
                finally:
                    self.session._local.generation = previous
        try:
            return super().after(ms, current)
        except (tk.TclError, RuntimeError):
            return None

    def __init__(self) -> None:
        super().__init__()
        apply_theme()
        self.title(f"亿点点 v{get_version()}")
        apply_window_icon(self, as_default=True)
        self.geometry("1200x760")
        self.minsize(980, 640)
        self.configure(fg_color=BG)

        self._auth_init()
        self.manager = ProcessManager(on_change=self._on_runtime_change, session=self.session)
        self.manager.set_visible(set())
        self._selected_id: str | None = None
        self._list_rows: dict[str, NodeCard] = {}
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
        self._header_busy_status: BusyStatus | None = None
        self._header_clear_job: str | None = None
        # 顶栏时钟与详情弹窗：布局重建前也可能被定时器碰到，先置空
        self._clock_label: ctk.CTkLabel | None = None
        self._detail_dialog = None

        self._show_login()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        # 最小化进托盘；恢复时修复 CTk Canvas 残影
        self.bind("<Unmap>", self._on_unmap)
        self.bind("<Map>", self._on_map)
        self.after(1000, self._tick_ui)
        self.after(1000, self._tick_clock)
        self.after(200, self._setup_tray)

    def _build_layout(self) -> None:
        """主窗口布局：顶栏 / hero 统计 / 节点卡片网格。

        对齐参考稿 `UI/控制端UI.html`：左侧「亿点点 | 节点控制台」，右侧时钟、
        用户名与操作按钮；主体是 hero 统计卡 + 节点网格。节点详情改由双击
        弹出的弹窗承载（原右侧详情栏已移除）。
        """
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        # —— 顶栏 ——
        header = glass_card(self, corner_radius=0, border_width=0, fg_color=BG_ELEVATED)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_columnconfigure(1, weight=1)

        brand = ctk.CTkFrame(header, fg_color="transparent")
        brand.grid(row=0, column=0, padx=20, pady=12, sticky="w")
        ctk.CTkLabel(
            brand, text="亿点点", text_color=GOLD, font=font(20, "bold")
        ).pack(side="left")
        ctk.CTkLabel(
            brand, text="│", text_color=LINE, font=font(18)
        ).pack(side="left", padx=10)
        ctk.CTkLabel(
            brand, text="节点控制台", text_color=TEXT, font=font(15, "bold")
        ).pack(side="left")

        right = ctk.CTkFrame(header, fg_color="transparent")
        right.grid(row=0, column=2, padx=20, pady=12, sticky="e")
        # 长耗时操作（批量替换等）的状态条：转圈 + 文案，空闲时清空不占视觉
        self.header_status = ctk.CTkLabel(
            right, text="", text_color=TEXT_MUTED, font=font(12), anchor="e"
        )
        self.header_status.pack(side="left", padx=(0, 12))
        # 布局会随重新登录重建，旧指示器指向已销毁的标签，必须重置
        self._header_busy_status = None
        self._clock_label = ctk.CTkLabel(
            right,
            text=time.strftime("%Y-%m-%d %H:%M:%S"),
            text_color=DIM,
            font=mono(12),
            anchor="e",
        )
        self._clock_label.pack(side="left", padx=(0, 14))
        self._user_label = ctk.CTkLabel(
            right, text=self._session_username(), text_color=TEXT, font=font(12)
        )
        self._user_label.pack(side="left", padx=(0, 14))
        self.version_btn = ghost_btn(
            right, _VERSION_BTN_IDLE, self._open_version_update, width=_VERSION_BTN_W
        )
        self.version_btn.pack(side="left", padx=(0, 8))
        ghost_btn(right, "退出登录", self._logout, width=90).pack(side="left", padx=4)
        ghost_btn(right, "连接配置", self._open_connection_config, width=100).pack(
            side="left", padx=(0, 4)
        )

        # —— 主体：hero + 节点网格 ——
        body = ctk.CTkScrollableFrame(
            self,
            fg_color=BG,
            scrollbar_button_color=LINE,
            scrollbar_button_hover_color=GOLD,
        )
        body.grid(row=1, column=0, sticky="nsew")
        # 滚动区内层 frame 必须自己撑满宽度，否则内层网格会挤在左侧
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(3, weight=1)

        self.hero_panel = HeroPanel(body)
        self.hero_panel.grid(row=0, column=0, sticky="ew", padx=14, pady=(14, 0))

        ctk.CTkLabel(
            body,
            text="仅本机回环 · 禁止公网绑定 · 支持 TLS/WS",
            text_color=MUTE,
            font=font(11),
            anchor="w",
        ).grid(row=1, column=0, sticky="w", padx=18, pady=(8, 0))

        # —— 区段头：标题 + 工具条 ——
        head = ctk.CTkFrame(body, fg_color="transparent")
        head.grid(row=2, column=0, sticky="ew", padx=14, pady=(16, 4))
        head.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            head, text="MT5 节点", text_color=TEXT, font=font(16, "bold"), anchor="w"
        ).grid(row=0, column=0, sticky="w")

        tools = ctk.CTkFrame(head, fg_color="transparent")
        tools.grid(row=0, column=1, sticky="e")
        for text, command, width in (
            ("全部启动", self._batch_start, 80),
            ("全部停止", self._batch_stop, 80),
            ("全部开守护", self._batch_daemon_on, 92),
            ("全部关守护", self._batch_daemon_off, 92),
            ("导入节点", self._import_node, 80),
            ("手工添加", self._add_instance, 80),
            ("手工替换全部", self._batch_replace, 104),
        ):
            toolbar_btn(tools, text, command, width=width).pack(side="left", padx=3)

        account_tools = ctk.CTkFrame(head, fg_color="transparent")
        account_tools.grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ghost_btn(
            account_tools, "绑定后台节点", self._bind_instance, width=112, height=26
        ).pack(side="left", padx=(0, 6))
        ghost_btn(
            account_tools, "重置专属令牌", self._rotate_credential, width=112, height=26
        ).pack(side="left", padx=(0, 6))

        # —— 节点网格 ——
        self.node_grid = ctk.CTkFrame(body, fg_color="transparent")
        self.node_grid.grid(row=3, column=0, sticky="nsew")
        for col in range(3):
            self.node_grid.grid_columnconfigure(col, weight=1, uniform="gridcol")

        self._empty_label = ctk.CTkLabel(
            self.node_grid,
            text="暂无节点 · 点击「导入节点」或「手工添加」开始",
            text_color=MUTE,
            font=font(13),
        )

        # 旧左右分栏的控件已移除，详情改由弹窗承载；保留同名占位避免
        # 其它逻辑访问时 AttributeError。
        self.detail_title = None
        self.detail_sub = None
        self.chip_row = None
        self.summary = None
        self.tasks = None
        self.log_box = None
        self.daemon_var = ctk.BooleanVar(value=False)

        # 兼容旧引用：列表容器即节点网格
        self.list_frame = self.node_grid

        self._grid_columns_cache = 0
        self.bind("<Configure>", self._on_window_resize, add="+")

    def _session_username(self) -> str:
        """顶栏显示的当前登录用户名。"""
        principal = getattr(self.session, "principal", None) or {}
        return str(principal.get("username") or "")

    def _on_window_resize(self, event) -> None:
        """窗口尺寸变化时按断点重排网格列数。"""
        if event.widget is not self:
            return
        columns = self._grid_columns()
        if columns == self._grid_columns_cache:
            return
        self._relayout_grid()

    def _grid_columns(self) -> int:
        """按窗口宽度算列数：>=1180 三列，>=820 两列，否则一列。

        CTk 会做 DPI 缩放，`winfo_width()` 返回的是物理像素；必须换算回
        逻辑像素，否则在 125% 缩放的机器上永远算成三列。
        """
        width = self.winfo_width() or 1200
        scaling = self._widget_scaling()
        if scaling > 1.0:
            width = int(width / scaling)
        if width >= 1180:
            return 3
        if width >= 820:
            return 2
        return 1

    def _widget_scaling(self) -> float:
        """当前控件的 DPI 缩放比；取不到时按 1.0 处理。

        `ctk.CTk`（tkinter.Tk）本身不带缩放访问器，要问 ScalingTracker。
        """
        try:
            return float(ctk.ScalingTracker.get_widget_scaling(self))
        except Exception:  # noqa: BLE001
            return 1.0

    def _relayout_grid(self) -> None:
        """按窗口宽度重排网格列数。只改位置，不重建控件。"""
        columns = self._grid_columns()
        self._grid_columns_cache = columns
        set_grid_columns(self.node_grid, columns)
        self._place_cards(columns)
        # 空态提示要跟着列数走，否则窄窗口下会被挤到网格外面
        if self._empty_label.winfo_ismapped():
            self._empty_label.configure(columnspan=max(1, columns))

    def _place_cards(self, columns: int) -> None:
        """按 `self._list_order` 把卡片摆进网格。"""
        for index, iid in enumerate(self._list_order):
            card = self._list_rows.get(iid)
            if card is None:
                continue
            row, col = divmod(index, columns)
            card.grid(row=row, column=col, sticky="nsew", padx=6, pady=6)

    def _persist(self) -> None:
        DashboardAuthUI._persist(self)

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

    def _create_node_card(self, vm: ListItemVM) -> NodeCard:
        """建一张节点卡：单击选中、双击开详情、右键出菜单、启停与删除走卡片按钮。"""
        return NodeCard(
            self.node_grid,
            vm,
            on_select=self._select,
            on_open=lambda iid=vm.id: self._open_detail_for(iid),
            on_start=lambda iid=vm.id: self._start_by_id(iid),
            on_stop=lambda iid=vm.id: self._stop_by_id(iid),
            on_remove=lambda iid=vm.id: self._remove_instance(iid),
            on_context=lambda iid=vm.id, ev=None: self._show_card_menu(iid, ev),
        )

    def _apply_node_card(self, card: NodeCard, vm: ListItemVM) -> None:
        card.update(vm)

    def _bind_list(self) -> None:
        """按 ViewModel 差分同步节点网格：复用卡片，避免整表销毁闪烁。"""
        if not self.session.active:
            return
        items = build_list(self.manager, self._selected_id)
        order = list_order(items)
        wanted = {vm.id for vm in items}
        for iid in list(self._list_rows):
            if iid not in wanted:
                self._list_rows[iid].destroy()
                del self._list_rows[iid]
        for vm in items:
            card = self._list_rows.get(vm.id)
            if card is None:
                self._list_rows[vm.id] = self._create_node_card(vm)
            else:
                self._apply_node_card(card, vm)
        if order != self._list_order:
            self._list_order = order
            self._place_cards(self._grid_columns())
        # 空态提示
        if not items:
            if not self._empty_label.winfo_ismapped():
                self._empty_label.grid(row=0, column=0, columnspan=3, pady=40)
        elif self._empty_label.winfo_ismapped():
            self._empty_label.grid_remove()
        # hero 统计
        try:
            self.hero_panel.update(build_hero(self.manager))
        except tk.TclError:
            pass

    def _bind_detail(self, *, refresh_log: bool = False) -> None:
        """更新详情快照。

        右侧详情栏已移除，详情改由弹窗承载；这里只维护 `_detail_snap`，
        供弹窗读取数据。
        """
        if not self.session.active:
            return
        mp = self._selected()
        detail = build_detail(mp)
        if not detail.empty:
            self.daemon_var.set(detail.daemon)
        self._detail_snap = detail

        # 运行日志轮询入口已隐藏（任务 + 日志卡片整块被注释）。
        if False:
            self._bind_log(mp)

    def _bind_log(self, mp) -> None:
        # 策略任务 + 运行日志 卡片当前已隐藏，文本控件占位为 None；不读、不写。
        if getattr(self, "log_box", None) is None:
            return
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
        self.session.check()
        mp = self.manager.get(instance_id)
        if mp is None:
            return
        self.session.perform("view_status", mp.cfg, lambda: None)
        self._selected_id = instance_id
        self._detail_snap = None
        self._log_snap = None
        self._bind_list()
        self._bind_detail(refresh_log=True)

    def _selected(self):
        if not self._selected_id:
            return None
        return self.manager.get(self._selected_id)

    def _on_runtime_change(self, _instance_id: str) -> None:
        if self._refresh_scheduled:
            return
        self._refresh_scheduled = True
        self.after(80, self._flush_runtime_refresh)

    def _flush_runtime_refresh(self) -> None:
        self._refresh_scheduled = False
        self._safe_refresh(refresh_log=False)

    def _safe_refresh(self, *, refresh_log: bool = False) -> None:
        if not self.session.active:
            return
        try:
            self._bind_list()
            self._bind_detail(refresh_log=refresh_log)
        except tk.TclError:
            pass

    def _tick_ui(self) -> None:
        if not self.session.active:
            if not self._quitting:
                self.after(2000, self._tick_ui)
            return
        try:
            self._bind_list()
            self._bind_detail(refresh_log=True)
        except tk.TclError:
            return
        self._probe_algo_trading()
        self.after(2000, self._tick_ui)

    def _probe_algo_trading(self) -> None:
        """直接读各实例目录里的 MT5 终端，不依赖节点客户端是否上报。"""
        if getattr(self, "_algo_probe_running", False) or not self.session.active:
            return
        targets = [
            (cfg.id, cfg.cwd or str(Path(cfg.exe_path).parent))
            for cfg in self.manager.configs()
        ]
        if not targets:
            return
        self._algo_probe_running = True
        generation = self.session.generation

        def work() -> None:
            results: list[tuple[str, bool]] = []
            for instance_id, directory in targets:
                try:
                    allowed = read_algo_trading_allowed(directory)
                except Exception:  # noqa: BLE001
                    allowed = None
                if isinstance(allowed, bool):
                    results.append((instance_id, allowed))

            def apply() -> None:
                self._algo_probe_running = False
                if generation != self.session.generation or not self.session.active:
                    return
                changed = False
                for instance_id, allowed in results:
                    mp = self.manager.get(instance_id)
                    if mp is None or mp.runtime.algo_trading_allowed == allowed:
                        continue
                    mp.runtime.algo_trading_allowed = allowed
                    changed = True
                if changed:
                    self._safe_refresh()

            try:
                self.after(0, apply)
            except (tk.TclError, RuntimeError):
                self._algo_probe_running = False

        threading.Thread(target=work, name="algo-trading-probe", daemon=True).start()

    def _refresh_list(self) -> None:
        self._bind_list()

    def _update_detail(self) -> None:
        self._bind_detail(refresh_log=True)

    @requires_session
    def _add_instance(self) -> None:
        self.session.check()
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
        login = simpledialog.askinteger("申请节点", "请输入此 MT5 终端的账号（管理员审核开通后才能启动）：", minvalue=1, parent=self)
        if login is None:
            return
        self.validate_instance_location(cfg, cfg.exe_path, cfg.cwd)
        self._enroll_instance(cfg, login)
        self.manager.add(cfg)
        self._persist()
        self._selected_id = cfg.id
        self._refresh_list()
        self._update_detail()
        self._refresh_version_badge()
        self._edit_instance()

    @requires_session
    def _edit_instance(self, instance_id: str | None = None) -> None:
        if instance_id and instance_id != self._selected_id:
            self._select(instance_id)
        mp = self.manager.get(instance_id) if instance_id else self._selected()
        if mp is None:
            messagebox.showinfo("提示", "请先选择一个实例")
            return
        self.session.perform("view_status", mp.cfg, lambda: None)
        dialog = EditInstanceDialog(self, mp.cfg)
        self.wait_window(dialog)
        if dialog.result is None:
            return
        self._persist()
        self._refresh_list()
        self._update_detail()

    @requires_session
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

    @requires_session
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

    @requires_session
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

    @requires_session
    def _open_connection_config(self) -> None:
        """查看当前登录后端。地址来自程序目录 .env 的 APP_URL。"""
        self.session.check()
        if not self.session.is_admin:
            messagebox.showinfo("当前连接", self.session.api.base + "\n后端地址由程序目录 .env 的 APP_URL 决定", parent=self)
            return
        from connection_dialog import ConnectionConfigDialog

        # 改完地址或令牌，原来查不通的后端可能就通了，立刻复检一次
        ConnectionConfigDialog(
            self, self.manager, on_saved=lambda: self._check_remote_version(repeat=False)
        )

    @requires_session
    def _import_node(self) -> None:
        self.session.check()
        """选 MT5 目录 + 指定版本，从后端拉客户端部署成新实例。"""
        from import_dialog import ImportNodeDialog

        def on_created(cfg: InstanceConfig) -> None:
            self._persist()
            self._selected_id = cfg.id
            self._refresh_list()
            self._update_detail()
            self._refresh_version_badge()

        ImportNodeDialog(self, self.manager, on_done=on_created)

    @requires_session
    def _edit_node_env(self) -> None:
        self.session.check()
        """编辑选中实例的 node_client .env。"""
        mp = self._selected()
        if mp is None:
            messagebox.showinfo("提示", "请先选择一个实例")
            return
        self.session.perform("view_status", mp.cfg, lambda: None)
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

    @requires_session
    def _open_version_update(self) -> None:
        self.session.check()
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
        if not self.session.active:
            return
        generation = self.session.generation
        if repeat:
            self.after(_VERSION_CHECK_INTERVAL_MS, self._check_remote_version)

        configs = self.manager.configs()
        if not configs:
            self._render_version_badge("", 0)
            return

        # 后端入口在主线程解析后再交给后台线程：Tcl 解释器不是线程安全的
        target = vs.BackendTarget(self.session.api.base, self.session.token, "用户登录", "Authorization")
        if not target.ready:
            self._render_version_badge("", 0)
            return

        def worker() -> None:
            try:
                data = vs.fetch_current_version(target)
            except vs.VersionServiceError:
                return
            version = str((data or {}).get("version") or "")
            self.after(0, lambda: self._on_remote_version(version) if generation == self.session.generation and self.session.active else None)

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

    @requires_session
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

        def on_instance(index: int, total: int, name: str, phase: str) -> None:
            label = _SWAP_PHASE_LABELS.get(phase, "处理中…")
            self.after(0, lambda t=f"正在替换 ({index}/{total}) {name}：{label}": self._header_busy_update(t))

        def job():
            results = self.manager.batch_replace(
                source, restart_if_was_running=True, on_progress=on_instance
            )
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

        self._run_async("手工替换", job, message=f"正在批量替换 {len(configs)} 个实例…")

    @requires_session
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
            self._inventory.pop(iid, None)
            self.after(0, self._persist)
            self.after(0, self._refresh_version_badge)

        self._run_async("移除", job)

    def _run_async(self, label: str, fn, message: str | None = None) -> None:
        """启停等可能阻塞的操作丢后台线程，避免卡住 UI。

        `message` 非空时在顶栏显示转圈状态（如「正在批量替换…」），让用户知道
        程序还在干活而不是卡死；不给就保持顶栏干净。
        """
        self.session.check()
        generation = self.session.generation
        if message:
            self._header_busy_start(message)
        def worker():
            try:
                self.session._local.generation = generation
                self.session.check(generation)
                fn()
            except Exception as e:  # noqa: BLE001
                # 必须先取出消息：except 块结束时 e 会被解绑，延迟执行的 lambda 里读不到它
                msg = str(e)
                self.after(0, lambda: messagebox.showerror(label, msg) if generation == self.session.generation and self.session.active else None)
            finally:
                self.session._local.generation = None
                self.after(0, self._safe_refresh)
                if message:
                    self.after(0, self._header_busy_stop)

        threading.Thread(target=worker, name=f"ui-{label}", daemon=True).start()
        self.after(50, self._safe_refresh)

    # ------------------------------------------------------------ 顶栏状态条

    def _header_busy(self) -> BusyStatus | None:
        """顶栏转圈指示器；界面还没建好时返回 None。"""
        label = getattr(self, "header_status", None)
        if label is None:
            return None
        if getattr(self, "_header_busy_status", None) is None:
            self._header_busy_status = BusyStatus(label, owner=self)
        return self._header_busy_status

    def _header_busy_start(self, text: str) -> None:
        # 新的操作开始时，作废上一个「延迟清空」的定时器，
        # 否则旧定时器会把新操作的状态条一起抹掉。
        self._header_clear_job = None
        busy = self._header_busy()
        if busy is not None:
            busy.start(text, TEXT_MUTED)

    def _header_busy_update(self, text: str) -> None:
        busy = self._header_busy()
        if busy is not None:
            busy.update(text)

    def _header_busy_stop(self, text: str = "", color: str | None = None) -> None:
        """停掉顶栏动画；给了终态文案就显示几秒再清空。"""
        busy = self._header_busy()
        if busy is None:
            return
        busy.stop(text, color)
        if text:
            self._header_clear_job = self.after(3000, self._header_busy_clear)

    def _header_busy_clear(self) -> None:
        self._header_clear_job = None
        busy = self._header_busy()
        if busy is not None:
            busy.clear()

    @requires_session
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

    @requires_session
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

    @requires_session
    def _refresh_health(self) -> None:
        mp = self._selected()
        if mp is None:
            return
        if mp.runtime.busy:
            return
        self._run_async("刷新健康", lambda: self.session.perform("refresh_health", mp.cfg, mp.refresh_health))

    @requires_session
    def _toggle_daemon(self) -> None:
        mp = self._selected()
        if mp is None:
            return
        self.manager.set_daemon(mp.cfg.id, self.daemon_var.get())
        self._persist()
        self._refresh_list()
        self._update_detail()

    @requires_session
    def _open_cwd(self) -> None:
        mp = self._selected()
        if mp is None:
            return
        path = mp.cfg.cwd or str(Path(mp.cfg.exe_path).parent)
        def open_folder():
            if os.name == "nt":
                os.startfile(path)  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", path])
        self.session.perform("open_cwd", mp.cfg, open_folder)

    # —— 按 id 的操作入口（节点卡的按钮与右键菜单用）——

    def _open_detail_for(self, instance_id: str) -> None:
        """双击节点卡：打开该节点的详情弹窗。"""
        self._select(instance_id)
        self._open_detail_dialog()

    def _open_detail_dialog(self) -> None:
        """打开当前选中节点的详情弹窗。"""
        mp = self._selected()
        if mp is None:
            return
        from node_detail_dialog import NodeDetailDialog
        if self._detail_dialog is not None:
            try:
                self._detail_dialog.destroy()
            except tk.TclError:
                pass
        dlg = NodeDetailDialog(self, mp, on_refresh=self._safe_refresh)
        self._detail_dialog = dlg

    def _start_by_id(self, instance_id: str) -> None:
        self._selected_id = instance_id
        self._start()

    def _stop_by_id(self, instance_id: str) -> None:
        self._selected_id = instance_id
        self._stop()

    def _open_cwd_by_id(self, instance_id: str) -> None:
        self._selected_id = instance_id
        self._open_cwd()

    def _refresh_health_by_id(self, instance_id: str) -> None:
        self._selected_id = instance_id
        self._refresh_health()

    # —— 节点卡右键菜单 ——

    def _build_card_menu(self, instance_id: str) -> tk.Menu:
        """构造节点卡右键菜单：打开目录 / 刷新健康 / 移除实例。"""
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(
            label=CARD_MENU_ITEMS[0], command=lambda: self._open_cwd_by_id(instance_id)
        )
        menu.add_command(
            label=CARD_MENU_ITEMS[1], command=lambda: self._refresh_health_by_id(instance_id)
        )
        menu.add_separator()
        menu.add_command(
            label=CARD_MENU_ITEMS[2], command=lambda: self._remove_instance(instance_id)
        )
        return menu

    def _show_card_menu(self, instance_id: str, event) -> None:
        """弹出节点卡右键菜单。"""
        menu = self._build_card_menu(instance_id)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            # 必须释放 grab，否则主窗口会失去输入权
            menu.grab_release()

    # —— 顶栏时钟 ——

    def _tick_clock(self) -> None:
        """顶栏时钟：每秒走动。"""
        if self._quitting:
            return
        try:
            label = self._clock_label
            if label is not None and label.winfo_exists():
                label.configure(text=time.strftime("%Y-%m-%d %H:%M:%S"))
        except tk.TclError:
            return
        self.after(1000, self._tick_clock)

    def _setup_tray(self) -> None:
        if not tray_available():
            return
        self._tray = TrayController(
            schedule=lambda fn: self.after(0, fn),
            on_show=self._show_from_tray,
            on_quit=self._quit_app,
            title="亿点点",
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
        if not self.session.active:
            return
        """恢复可见后强制重绘（CTk Canvas 最小化残影）。"""
        if self._restore_repair_job is not None:
            try:
                self.after_cancel(self._restore_repair_job)
            except Exception:  # noqa: BLE001
                pass
        # 双拍：布局稳定后再清一次文字区残影
        self._restore_repair_job = self.after(40, lambda: self._repair_ui_after_restore(pass_no=1))

    def _repair_ui_after_restore(self, *, pass_no: int = 1) -> None:
        if not self.session.active:
            return
        self._restore_repair_job = None
        if self._quitting or self._in_tray:
            return
        try:
            self.update_idletasks()
            self._force_ctk_redraw(self)
            self._redraw_canvases(self)
            # 使快照失效，重绑文本类控件（CTkTextbox 残影主要靠重写内容清除）
            self._detail_snap = None
            self._log_snap = None
            self._bind_list()
            self._bind_detail(refresh_log=True)
            self._nudge_win_composite()
            if pass_no < 2:
                self._restore_repair_job = self.after(
                    120, lambda: self._repair_ui_after_restore(pass_no=2)
                )
        except tk.TclError:
            pass

    def _redraw_canvases(self, root) -> None:
        """重画 CTkCanvas：CTkCanvas 没有 `_draw`，最小化恢复后需手工重建内容。

        hero 面板的网格纹理挂在 canvas 的 `_on_restore_redraw` 上。
        """
        stack = [root]
        while stack:
            w = stack.pop()
            try:
                stack.extend(w.winfo_children())
            except tk.TclError:
                continue
            if not isinstance(w, ctk.CTkCanvas):
                continue
            redraw = getattr(w, "_on_restore_redraw", None)
            if callable(redraw):
                try:
                    redraw()
                except Exception:  # noqa: BLE001
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
            self._tray.notify("亿点点", "已最小化到托盘，右键可显示或退出")

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
        if self.session.active and self._tray is not None and tray_available():
            self._hide_to_tray()
            return
        self._quit_app()

    def _quit_app(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        self.session.token = ""
        self.session.generation += 1
        self._persist()
        if self._tray is not None:
            self._tray.stop()
            self._tray = None
        # 关掉残留的详情弹窗，避免退出时留窗口
        if getattr(self, "_detail_dialog", None) is not None:
            try:
                self._detail_dialog.destroy()
            except tk.TclError:
                pass
            self._detail_dialog = None
        for mp in self.manager._items.values():
            mp.shutdown_workers()
        try:
            self.destroy()
        except tk.TclError:
            pass

    @requires_session
    def _clear_log(self) -> None:
        mp = self._selected()
        if mp is None:
            return
        if not messagebox.askyesno("清空节点日志", "将清空当日节点日志，操作会记录审计。是否继续？", parent=self):
            return
        self.session.perform("clear_log", mp.cfg, mp.clear_log_display_file)
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
            messagebox.showinfo("提示", "亿点点已在运行，仅支持单实例。\n已尝试切换到已有窗口。")
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
