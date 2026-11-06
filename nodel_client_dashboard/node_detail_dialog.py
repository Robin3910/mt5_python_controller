"""节点详情弹窗：承接原右侧详情区的全部信息与操作。

双击节点卡时打开。布局自上而下：

    头部卡   节点名 + 路径/工作目录 + 状态芯片行
    操作卡   启动 / 停止 / 节点配置 / 刷新健康 / 打开目录 / 守护开关
    摘要卡   运行快照（等宽字体，左对齐）
    任务区   策略任务 + 运行日志（默认收起）
    底部行   编辑实例 / 绑定后台节点 / 移除实例 / 关闭
"""
from __future__ import annotations

import tkinter as tk

import customtkinter as ctk

from dialog_window import show_centered
from session_ui import SessionDialog
from theme import (BG, DANGER, DIM, GOLD, GREEN, LINE, MUTE, PANEL, PANEL2, RED, TEXT,
                   danger_btn, font, ghost_btn, glass_card, health_color, mono,
                   primary_btn, soft_card, status_chip)
from viewmodels import DetailVM, build_detail


class NodeDetailDialog(SessionDialog):
    """节点详情。宽度 720、高度 560。"""

    _W = 720
    _H = 560

    def __init__(self, master, mp, *, on_refresh=None) -> None:
        super().__init__(master)
        self.title(f"节点详情 · {mp.cfg.name}")
        self.resizable(False, False)
        # CTkToplevel 不接受 transparent 底色，用主题底色
        self.configure(fg_color=BG)
        self.mp = mp
        self.on_refresh = on_refresh
        self.detail: DetailVM = build_detail(mp)
        self._chip_widgets: dict[str, ctk.CTkLabel] = {}
        self._build()
        self.after(20, self._show_centered)

    # ---------------------------------------------------------------- 构建

    def _build(self) -> None:
        root = ctk.CTkFrame(self, fg_color="transparent")
        root.pack(fill="both", expand=True, padx=14, pady=14)

        # —— 头部卡 ——
        head = glass_card(root)
        head.pack(fill="x")
        head.grid_columnconfigure(0, weight=1)

        self.title_label = ctk.CTkLabel(
            head, text=self.detail.title, text_color=TEXT, font=font(18, "bold"),
            anchor="w",
        )
        self.title_label.grid(row=0, column=0, sticky="w", padx=16, pady=(14, 2))
        self.subtitle_label = ctk.CTkLabel(
            head, text=self.detail.subtitle, text_color=DIM, font=font(11),
            anchor="w", justify="left",
        )
        self.subtitle_label.grid(row=1, column=0, sticky="w", padx=16, pady=(0, 8))

        self.chip_row = ctk.CTkFrame(head, fg_color="transparent")
        self.chip_row.grid(row=2, column=0, sticky="w", padx=14, pady=(0, 12))
        self._render_chips(self.detail)

        # —— 操作卡 ——
        actions_card = glass_card(root)
        actions_card.pack(fill="x", pady=(10, 0))
        actions = ctk.CTkFrame(actions_card, fg_color="transparent")
        actions.pack(fill="x", padx=12, pady=12)
        self.actions = actions

        self.btn_start = primary_btn(actions, "启动", self._start, width=80)
        self.btn_start.pack(side="left", padx=4)
        self.btn_stop = danger_btn(actions, "停止", self._stop, width=80)
        self.btn_stop.pack(side="left", padx=4)
        ghost_btn(actions, "节点配置", self._edit_node_env, width=92).pack(side="left", padx=4)
        ghost_btn(actions, "刷新健康", self._refresh_health, width=92).pack(side="left", padx=4)
        ghost_btn(actions, "打开目录", self._open_cwd, width=92).pack(side="left", padx=4)
        self.daemon_var = ctk.BooleanVar(value=self.detail.daemon)
        self.daemon_switch = ctk.CTkSwitch(
            actions,
            text="守护进程",
            variable=self.daemon_var,
            command=self._toggle_daemon,
            progress_color=GOLD,
            button_color=TEXT,
            button_hover_color=GOLD,
            text_color=TEXT,
            font=font(13),
        )
        self.daemon_switch.pack(side="left", padx=14)

        # —— 摘要卡 ——
        summary_card = soft_card(root)
        summary_card.pack(fill="both", expand=True, pady=(10, 0))
        self.summary = ctk.CTkLabel(
            summary_card,
            text=self.detail.summary,
            justify="left",
            anchor="nw",
            text_color=DIM,
            font=mono(12),
        )
        self.summary.pack(fill="both", expand=True, padx=14, pady=12)

        # —— 策略任务 + 运行日志（默认收起，行为与改造前一致）——
        self.tasks = None
        self.log_box = None
        self._extra = ctk.CTkFrame(root, fg_color="transparent")
        self._extra.pack(fill="x", pady=(10, 0))

        # —— 底部按钮行 ——
        foot = ctk.CTkFrame(root, fg_color="transparent")
        foot.pack(fill="x", pady=(10, 0))
        ghost_btn(foot, "编辑实例", self._edit_instance, width=88).pack(side="left", padx=3)
        ghost_btn(foot, "绑定后台节点", self._bind_instance, width=112).pack(side="left", padx=3)
        ghost_btn(
            foot, "移除实例", self._remove_instance, width=92,
            text_color=RED, border_color=RED,
        ).pack(side="left", padx=3)
        ghost_btn(foot, "关闭", self.destroy, width=72).pack(side="right", padx=3)

        self._sync_action_buttons()

    def _show_centered(self) -> None:
        show_centered(self, self._W, self._H)

    # ---------------------------------------------------------------- 更新

    def refresh(self) -> None:
        """重新读 `build_detail(mp)` 并就地更新。"""
        if not self.winfo_exists():
            return
        self.detail = build_detail(self.mp)
        self.title_label.configure(text=self.detail.title)
        self.subtitle_label.configure(text=self.detail.subtitle)
        self.summary.configure(text=self.detail.summary)
        self.daemon_var.set(self.detail.daemon)
        self._render_chips(self.detail)
        self._sync_action_buttons()
        if self.on_refresh is not None:
            try:
                self.on_refresh()
            except tk.TclError:
                pass

    def _render_chips(self, detail: DetailVM) -> None:
        for widget in self._chip_widgets.values():
            widget.pack_forget()
        for chip_vm in detail.chips:
            if not chip_vm.visible or not chip_vm.text:
                continue
            color = self._tone_color(
                chip_vm.tone, alive=detail.alive, health=detail.health
            )
            widget = self._chip_widgets.get(chip_vm.key)
            if widget is None:
                widget = status_chip(self.chip_row, "", color)
                self._chip_widgets[chip_vm.key] = widget
            widget.label.configure(text=chip_vm.text)
            widget.dot.configure(fg_color=color)
            widget.label.configure(text_color=color)
            widget.pack(side="left", padx=3)

    def _tone_color(self, tone: str, *, alive: bool = True, health: str = "unknown") -> str:
        if tone == "health":
            return health_color(health, alive)
        if tone == "cyan":
            return DIM
        if tone == "accent":
            return GOLD
        if tone == "warning":
            return GOLD
        if tone == "muted":
            return DIM
        return MUTE

    def _sync_action_buttons(self) -> None:
        """启动/停止按钮的可用性由弹窗自己根据运行时状态判定。"""
        mp = self.mp
        alive = bool(mp.runtime.process_alive)
        busy = bool(mp.runtime.busy)
        if busy:
            self.btn_start.configure(state="disabled")
            self.btn_stop.configure(state="disabled")
            return
        self.btn_start.configure(state="disabled" if alive else "normal")
        self.btn_stop.configure(state="normal" if alive else "disabled")

    # ---------------------------------------------------------------- 操作

    def _check_instance_alive(self) -> None:
        """实例已被移除时关闭弹窗，避免悬在登录页之上。"""
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        dashboard = self.dashboard
        selected = None
        try:
            selected = dashboard._selected()
        except Exception:  # noqa: BLE001
            selected = None
        if selected is None or selected.cfg.id != self.mp.cfg.id:
            self.destroy()

    def _after_action(self) -> None:
        """操作完成后的收尾：刷新弹窗并检查实例是否还在。"""
        self._check_instance_alive()
        if self.winfo_exists():
            self.refresh()

    def _start(self) -> None:
        dashboard = self.dashboard
        dashboard._selected_id = self.mp.cfg.id
        dashboard._start()
        self.after(200, self._after_action)

    def _stop(self) -> None:
        dashboard = self.dashboard
        dashboard._selected_id = self.mp.cfg.id
        dashboard._stop()
        self.after(200, self._after_action)

    def _refresh_health(self) -> None:
        dashboard = self.dashboard
        dashboard._selected_id = self.mp.cfg.id
        dashboard._refresh_health()
        self.after(200, self._after_action)

    def _open_cwd(self) -> None:
        dashboard = self.dashboard
        dashboard._selected_id = self.mp.cfg.id
        dashboard._open_cwd()

    def _toggle_daemon(self) -> None:
        dashboard = self.dashboard
        dashboard._selected_id = self.mp.cfg.id
        dashboard.daemon_var = self.daemon_var
        dashboard._toggle_daemon()
        self.after(200, self._after_action)

    def _edit_node_env(self) -> None:
        dashboard = self.dashboard
        dashboard._selected_id = self.mp.cfg.id
        dashboard._edit_node_env()

    def _edit_instance(self) -> None:
        dashboard = self.dashboard
        dashboard._edit_instance(self.mp.cfg.id)

    def _bind_instance(self) -> None:
        dashboard = self.dashboard
        dashboard._selected_id = self.mp.cfg.id
        dashboard._bind_instance()

    def _remove_instance(self) -> None:
        dashboard = self.dashboard
        dashboard._remove_instance(self.mp.cfg.id)
        self._check_instance_alive()
