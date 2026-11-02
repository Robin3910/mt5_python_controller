"""节点卡片：网格中的单个节点单元。

结构对齐参考稿 `UI/控制端UI.html` 的 `.node-card`：

    头部   状态点 + 节点名            右侧 状态徽章
    元信息 服务器账号 + 节点 ID
    信息格 连接MT5 / 是否鉴权 / 算法交易
    底部   运行时长                    右侧 删除 + 启停按钮

卡片只做「就地更新」（`update()`），不重建控件，避免列表刷新时闪烁。
"""
from __future__ import annotations

import tkinter as tk

import customtkinter as ctk

from theme import (AMBER, DIM, GOLD, GREEN, LINE, MUTE, PANEL, PANEL2, RED, TEXT,
                   badge, font, ghost_btn, health_color, info_cell, mono)
from viewmodels import ListItemVM

# 徽章色调 -> 语义色。`badge_tone` 与 hero 分布条同源（viewmodels.badge_for）。
_TONE_COLORS = {
    "online": GREEN,
    "starting": AMBER,
    "pending": GOLD,
    "offline": MUTE,
    "mute": MUTE,
}

# 信息格三列的固定顺序
_CELL_SPECS = (
    ("连接MT5", "mt5_connected", "已连接", "未连接"),
    ("是否鉴权", "mt5_authenticated", "已鉴权", "未鉴权"),
    ("算法交易", "algo_trading_ok", "已开启", "未开启"),
)

_PULSE_INTERVAL = 450


def set_grid_columns(frame: ctk.CTkFrame, columns: int) -> None:
    """按新列数重排网格里的卡片：只改位置与权重，不重建控件。

    必须先清掉旧的 `uniform` —— Tk 一旦给某列设了 `uniform='gridcol'`，
    列数从 3 降到 2 时第 3 列残留的 uniform 会把它的权重并到前两列，
    导致列宽错乱。
    """
    columns = max(1, int(columns))
    for col in range(0, 8):
        try:
            frame.grid_columnconfigure(col, weight=0, uniform="")
        except tk.TclError:
            break
    for col in range(columns):
        frame.grid_columnconfigure(col, weight=1, uniform="gridcol")
    slaves = sorted(
        frame.grid_slaves(),
        key=lambda w: (w.grid_info()["row"], w.grid_info()["column"]),
    )
    for index, widget in enumerate(slaves):
        widget.grid(row=index // columns, column=index % columns,
                    sticky="nsew", padx=6, pady=6)

class NodeCard(ctk.CTkFrame):
    """一张节点卡片。`self.card` 是自己（外框），供选中态换边框色。"""

    def __init__(
        self,
        master,
        vm: ListItemVM,
        *,
        on_select=None,
        on_open=None,
        on_start=None,
        on_stop=None,
        on_remove=None,
        on_context=None,
    ) -> None:
        super().__init__(
            master,
            fg_color=PANEL,
            border_width=1,
            border_color=GOLD if vm.selected else LINE,
            corner_radius=10,
        )
        self.card = self
        # CTk 控件的真实点击目标是内部 canvas，暴露出来便于程序化触发。
        self.canvas = getattr(self, "_canvas", None)
        self._vm = vm
        self._on_select = on_select
        self._on_open = on_open
        self._on_start = on_start
        self._on_stop = on_stop
        self._on_remove = on_remove
        self._on_context = on_context
        self._pulse_job: str | None = None
        self._pulse_on = False

        self._build(vm)
        # 初始状态也要起脉冲（构造时还没走过 update）
        self._sync_pulse(vm)

    # —— 构建 ——

    def _build(self, vm: ListItemVM) -> None:
        inner = ctk.CTkFrame(self, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=12, pady=11)

        # 头部：状态点 + 名称 + 徽章
        head = ctk.CTkFrame(inner, fg_color="transparent")
        head.pack(fill="x")
        self._dot = ctk.CTkFrame(
            head, width=10, height=10, corner_radius=5,
            fg_color=self._state_color(vm), border_width=0,
        )
        self._dot.pack(side="left", pady=6)
        self._name_label = ctk.CTkLabel(
            head, text=vm.title, text_color=TEXT, font=font(14, "bold"),
            anchor="w", height=22,
        )
        self._name_label.pack(side="left", padx=(8, 6), fill="x", expand=True)
        self._badge = badge(head, vm.badge_text, self._tone_color(vm))
        self._badge.pack(side="right")

        # 元信息：账号 + 节点 ID
        meta = ctk.CTkFrame(inner, fg_color="transparent")
        meta.pack(fill="x", pady=(2, 8))
        self._server_label = ctk.CTkLabel(
            meta, text=f"▤  {vm.server_label}", text_color=DIM, font=font(11),
            anchor="w", height=18,
        )
        self._server_label.pack(side="left")
        self._id_label = ctk.CTkLabel(
            meta, text=f"ID {vm.node_id}" if vm.node_id else "", text_color=MUTE,
            font=mono(11), anchor="e", height=18,
        )
        self._id_label.pack(side="right")

        # 三格信息
        grid = ctk.CTkFrame(inner, fg_color="transparent")
        grid.pack(fill="x")
        for i in range(3):
            grid.grid_columnconfigure(i, weight=1, uniform="info")
        self._cells = []
        for col, (label, attr, ok_text, bad_text) in enumerate(_CELL_SPECS):
            ok = bool(getattr(vm, attr))
            if attr == "algo_trading_ok":
                # 算法交易状态未知时不能算「已开启」
                ok = ok and vm.algo_trading_known
            cell = info_cell(grid, label, ok_text if ok else bad_text, ok)
            cell.grid(row=0, column=col, sticky="nsew", padx=(0 if col == 0 else 4, 0))
            self._cells.append(cell)

        # 底部：运行时长 + 按钮组
        foot = ctk.CTkFrame(inner, fg_color="transparent")
        foot.pack(fill="x", pady=(10, 0))
        ctk.CTkLabel(
            foot, text="⏱", text_color=MUTE, font=font(11), width=14, height=18
        ).pack(side="left")
        self._uptime_label = ctk.CTkLabel(
            foot, text=vm.uptime_text, text_color=DIM, font=font(11),
            anchor="w", height=18,
        )
        self._uptime_label.pack(side="left", padx=(2, 0))

        actions = ctk.CTkFrame(foot, fg_color="transparent")
        actions.pack(side="right")
        ghost_btn(
            actions, "删除", command=self._handle_remove, width=48, height=26,
            font=font(11), text_color=RED, border_color=RED,
        ).pack(side="left", padx=(0, 6))
        self._toggle_btn = ghost_btn(
            actions, self._toggle_text(vm), command=self._handle_toggle,
            width=72, height=26, font=font(11),
            text_color=GOLD, border_color=GOLD,
        )
        self._toggle_btn.pack(side="left")

        self._bind_interactions()

    # —— 交互 ——

    def _bind_interactions(self) -> None:
        """把单击/双击/右键递归绑到整张卡片（按钮自身除外）。

        CTk 控件的 `bind` 实际绑在内部 canvas 上（CTk 自己的文档如此写），
        所以真实鼠标点击会命中；这里额外把 `self.canvas` 暴露出来，
        便于程序化触发与测试。
        """
        bindings = (
            ("<Button-1>", self._handle_select),
            ("<Double-Button-1>", self._handle_open),
            ("<Button-3>", self._handle_context),
        )

        def walk(widget):
            if not isinstance(widget, ctk.CTkButton):
                for sequence, handler in bindings:
                    widget.bind(sequence, handler, add="+")
            for child in widget.winfo_children():
                walk(child)

        walk(self)

    def _handle_select(self, _event=None) -> None:
        if self._on_select is not None:
            self._on_select(self._vm.id)

    def _handle_open(self, _event=None) -> None:
        if self._on_open is not None:
            self._on_open(self._vm.id)

    def _handle_context(self, event=None) -> None:
        if self._on_context is not None:
            self._on_context(self._vm.id, event)

    def _handle_remove(self) -> None:
        if self._on_remove is not None:
            self._on_remove(self._vm.id)

    def _handle_toggle(self) -> None:
        if self._vm.alive:
            if self._on_stop is not None:
                self._on_stop(self._vm.id)
        elif self._on_start is not None:
            self._on_start(self._vm.id)

    # —— 状态工具 ——

    def _state_color(self, vm: ListItemVM) -> str:
        return health_color(vm.health, vm.alive)

    def _tone_color(self, vm: ListItemVM) -> str:
        return _TONE_COLORS.get(vm.badge_tone, MUTE)

    def _toggle_text(self, vm: ListItemVM) -> str:
        if vm.badge_tone == "starting":
            return "启动中…"
        return "停止" if vm.alive else "启动"

    # —— 差分更新 ——

    def update(self, vm: ListItemVM) -> None:
        """就地更新卡片。**不重建任何控件。**"""
        old = self._vm
        self._vm = vm

        if old.title != vm.title:
            self._name_label.configure(text=vm.title)
        if old.server_label != vm.server_label:
            self._server_label.configure(text=f"▤  {vm.server_label}")
        if old.node_id != vm.node_id:
            self._id_label.configure(text=f"ID {vm.node_id}" if vm.node_id else "")
        if old.uptime_text != vm.uptime_text:
            self._uptime_label.configure(text=vm.uptime_text)

        if old.badge_text != vm.badge_text or old.badge_tone != vm.badge_tone:
            self._badge.configure(text=f"  {vm.badge_text}  ",
                                   text_color=self._tone_color(vm),
                                   fg_color=self._badge_bg(vm))

        if old.health != vm.health or old.alive != vm.alive:
            self._dot.configure(fg_color=self._state_color(vm))

        for col, (_label, attr, ok_text, bad_text) in enumerate(_CELL_SPECS):
            ok = bool(getattr(vm, attr))
            if attr == "algo_trading_ok":
                ok = ok and vm.algo_trading_known
            cell = self._cells[col]
            want = ok_text if ok else bad_text
            if cell.value_label.cget("text") != want:
                cell.value_label.configure(text=want)
            color = GREEN if ok else MUTE
            if cell.value_label.cget("text_color") != color:
                cell.value_label.configure(text_color=color)
            mark = cell.mark_label
            if mark.cget("text") != ("✓" if ok else "✗"):
                mark.configure(text="✓" if ok else "✗")
            if mark.cget("text_color") != color:
                mark.configure(text_color=color)

        if old.alive != vm.alive or old.badge_tone != vm.badge_tone:
            self._toggle_btn.configure(text=self._toggle_text(vm))

        if old.selected != vm.selected:
            self.card.configure(border_color=GOLD if vm.selected else LINE)

        self._sync_pulse(vm)

    def _badge_bg(self, vm: ListItemVM) -> str:
        from theme import _mix
        return _mix(self._tone_color(vm), 0.12)

    # —— 状态点脉冲 ——

    def _sync_pulse(self, vm: ListItemVM) -> None:
        """「启动中」的状态点脉冲；其它状态静止。"""
        if vm.badge_tone == "starting":
            if self._pulse_job is None:
                self._pulse_on = True
                self._pulse_job = self.after(_PULSE_INTERVAL, self._pulse_step)
        else:
            self.stop_animation()
            self._dot.configure(fg_color=self._state_color(vm))

    def _pulse_step(self) -> None:
        self._pulse_job = None
        if not self.winfo_exists():
            return
        self._pulse_on = not self._pulse_on
        self._dot.configure(fg_color=AMBER if self._pulse_on else MUTE)
        self._pulse_job = self.after(_PULSE_INTERVAL, self._pulse_step)

    def stop_animation(self) -> None:
        """停掉脉冲动画（幂等）。"""
        if self._pulse_job is not None:
            try:
                self.after_cancel(self._pulse_job)
            except (tk.TclError, ValueError):
                pass
            self._pulse_job = None

    def destroy(self) -> None:
        self.stop_animation()
        super().destroy()
