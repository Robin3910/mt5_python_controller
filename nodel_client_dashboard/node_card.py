"""节点卡片：网格中的单个节点单元。

结构与规格**逐条对齐**参考稿 `UI/控制端UI.html` 外链 CSS
(`assets/index-Cd5AaG_5.css`) 里的 `.node-card`：

    .node-card          PANEL 底 / LINE 边 / 圆角 10 / padding 13px 14px 11px / gap 10
    .node-card.online   描边 #2fd38138
    .node-card.starting 描边 #f2b84b47
    .node-card.offline  整体 opacity .62
    .node-card-head     状态点 8px + .node-name(mono 15/700)   右侧 .badge
    .node-card-meta     .meta-server(mono 12.5) | .meta-sep 竖线 |
                        .mt5-id-inline 金色胶囊；padding-bottom 9 + border-bottom
    .node-info-grid     三格 gap 6
    .info-item          圆角 10 / 底 #ffffff09 / 描边 line-soft / 垂直居中
    .status-ico         19px 圆底，ok=绿 / no=红
    .info-value         12.5/600，ok=绿，no=--dim(灰)
    .node-card-foot     padding-top 8 + border-top；左侧运行时长(mono 12)
                        右侧 .btn-icon.danger(26) + 启停按钮
    .row-actions        gap 6

卡片只做「就地更新」（`update()`），不重建控件，避免列表刷新时闪烁。
"""
from __future__ import annotations

import tkinter as tk

import customtkinter as ctk

from theme import (AMBER, AMBER_BADGE_BG, BORDER_HOVER, BORDER_ONLINE,
                   BORDER_STARTING, DIM, GOLD, GOLD_LIGHT, GREEN, GREEN_ICO_BG,
                   H_DOT, LINE, MUTE, PANEL, RED, RED_ICO_BG, RED_SOFT, TEXT,
                   badge, badge_bg_for, danger_icon_btn, divider, font,
                   ghost_btn, health_color, info_cell, mono)
from viewmodels import ListItemVM

# 徽章色调 -> 语义色。`badge_tone` 与 hero 分布条同源（viewmodels.badge_for）。
_TONE_COLORS = {
    "online": GREEN,
    "starting": AMBER,
    "pending": GOLD,
    "offline": MUTE,
    "mute": MUTE,
}

# 卡片描边色（按状态）。参考稿：
#   .node-card.online   {border-color:#2fd38138}
#   .node-card.starting {border-color:#f2b84b47}
#   .node-card.offline  opacity .62（描边仍是 --line）
# 实色值见 theme.py（CSS 的 8 位透明色已预先混好）

# 信息格三列的固定顺序：标签、取值属性、ok 文案、no 文案
_CELL_SPECS = (
    ("连接MT5", "mt5_connected", "已连接", "未连接"),
    ("是否鉴权", "mt5_authenticated", "已鉴权", "未鉴权"),
    ("算法交易", "algo_trading_ok", "已开启", "未开启"),
)

_PULSE_INTERVAL = 450

# 图标字形：Tk 没有内联 SVG，用最接近的 Unicode 字形
_GLYPH_TRASH = "🗑"
_GLYPH_PAUSE = "⏸"
_GLYPH_PLAY = "▶"


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

    def __init__(self, master, vm: ListItemVM, **kwargs) -> None:
        super().__init__(
            master,
            fg_color=PANEL,
            border_width=1,
            border_color=GOLD if vm.selected else self._state_border_color(vm),
            corner_radius=10,
        )
        self.card = self
        # CTk 控件的真实点击目标是内部 canvas，暴露出来便于程序化触发。
        self.canvas = getattr(self, "_canvas", None)
        self._vm = vm
        self._on_select = kwargs.pop("on_select", None)
        self._on_open = kwargs.pop("on_open", None)
        self._on_start = kwargs.pop("on_start", None)
        self._on_stop = kwargs.pop("on_stop", None)
        self._on_remove = kwargs.pop("on_remove", None)
        self._on_context = kwargs.pop("on_context", None)
        self._pulse_job: str | None = None
        self._pulse_on = False
        # hover 时的边框色（参考稿 .node-card:hover）
        self._hover = False

        self._build(vm)
        self._bind_interactions()
        self._bind_hover()
        # 初始状态也要起脉冲（构造时还没走过 update）
        self._sync_pulse(vm)

    # —— 构建 ——

    def _build(self, vm: ListItemVM) -> None:
        # .node-card padding: 13px 14px 11px
        inner = ctk.CTkFrame(self, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=14, pady=(13, 11))

        # —— 头部：状态点 + 名称 + 徽章 ——
        head = ctk.CTkFrame(inner, fg_color="transparent")
        head.pack(fill="x")
        self._dot = ctk.CTkFrame(
            head, width=H_DOT, height=H_DOT, corner_radius=H_DOT // 2,
            fg_color=self._state_color(vm), border_width=0,
        )
        self._dot.pack(side="left", pady=7)
        self._name_label = ctk.CTkLabel(
            head, text=vm.title, text_color=TEXT, font=mono(15, "bold"),
            anchor="w", height=22,
        )
        self._name_label.pack(side="left", padx=(8, 6), fill="x", expand=True)
        self._badge = badge(head, vm.badge_text, self._tone_color(vm))
        self._badge.pack(side="right")

        # —— 元信息：券商服务器 + 竖分隔线 + 交易账号金色胶囊 ——
        meta = ctk.CTkFrame(inner, fg_color="transparent")
        meta.pack(fill="x", pady=(4, 0))
        self._server_label = ctk.CTkLabel(
            meta, text=self._server_text(vm), text_color=DIM, font=mono(12),
            anchor="w", height=18,
        )
        self._server_label.pack(side="left", fill="x", expand=True)
        # .meta-sep {width:1px;height:12px;background:var(--line)}
        sep = ctk.CTkFrame(meta, width=1, height=12, fg_color=LINE, border_width=0)
        sep.pack(side="left", padx=6)
        # .mt5-id-inline：金色字 + 7px 圆角 + 淡琥珀底 + 琥珀描边
        self._id_label = ctk.CTkLabel(
            meta,
            text=self._id_text(vm),
            text_color=GOLD_LIGHT,
            fg_color=AMBER_BADGE_BG,
            font=mono(12, "bold"),
            corner_radius=7,
            height=24,
            padx=8,
        )
        self._id_label.pack(side="right")

        # .node-card-meta {padding-bottom:9px;border-bottom:1px solid var(--line-soft)}
        divider(inner).pack(fill="x", pady=(9, 10))

        # —— 三格信息 ——
        grid = ctk.CTkFrame(inner, fg_color="transparent")
        grid.pack(fill="x")
        for i in range(3):
            grid.grid_columnconfigure(i, weight=1, uniform="info")
        self._cells = []
        for col, (label, attr, ok_text, bad_text) in enumerate(_CELL_SPECS):
            ok = self._cell_ok(vm, attr)
            cell = info_cell(grid, label, ok_text if ok else bad_text, ok)
            # .node-info-grid gap: 6px
            cell.grid(row=0, column=col, sticky="nsew",
                      padx=(0 if col == 0 else 3, 0 if col == 2 else 3))
            self._cells.append(cell)

        # —— 底部：运行时长 + 按钮组 ——
        foot = ctk.CTkFrame(inner, fg_color="transparent")
        foot.pack(fill="x", pady=(10, 0))
        # .node-card-foot {padding-top:8px;border-top:1px solid var(--line-soft)}
        divider(foot).pack(fill="x", side="top", pady=(0, 8))

        # .node-uptime {display:inline-flex;align-items:center;gap:5px;font-size:12px;
        #               color:var(--mute);font-family:var(--mono)}
        ctk.CTkLabel(
            foot, text="⏱", text_color=MUTE, font=font(11), width=14, height=18
        ).pack(side="left")
        self._uptime_label = ctk.CTkLabel(
            foot, text=vm.uptime_text, text_color=MUTE, font=mono(12),
            anchor="w", height=18,
        )
        self._uptime_label.pack(side="left", padx=(2, 0))

        # .row-actions {display:flex;gap:6px;align-items:center}
        actions = ctk.CTkFrame(foot, fg_color="transparent")
        actions.pack(side="right")
        danger_icon_btn(
            actions, _GLYPH_TRASH, command=self._handle_remove,
        ).pack(side="left", padx=(0, 6))
        self._toggle_btn = ghost_btn(
            actions, self._toggle_text(vm), command=self._handle_toggle,
            width=72, height=28, font=font(12),
            text_color=GOLD, border_color=BORDER_STARTING,
            fg_color=AMBER_BADGE_BG, hover_color="#2a2317",
        )
        self._toggle_btn.pack(side="left")

    # —— 交互 ——

    def _bind_interactions(self) -> None:
        """把单击/双击/右键递归绑到整张卡片（按钮自身除外）。"""
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

    def _bind_hover(self) -> None:
        """鼠标移入时把边框改成参考稿的 hover 色。"""
        def on_enter(_event=None):
            self._hover = True
            self._apply_border()

        def on_leave(_event=None):
            self._hover = False
            self._apply_border()

        for widget in (self, self.canvas):
            if widget is None:
                continue
            try:
                widget.bind("<Enter>", on_enter, add="+")
                widget.bind("<Leave>", on_leave, add="+")
            except tk.TclError:
                pass

    def _apply_border(self) -> None:
        """按 选中 > hover > 状态 的优先级决定边框色。"""
        if self._vm.selected:
            color = GOLD
        elif self._hover:
            color = BORDER_HOVER
        else:
            color = self._state_border_color(self._vm)
        try:
            self.card.configure(border_color=color)
        except tk.TclError:
            pass

    @staticmethod
    def _state_border_color(vm: ListItemVM) -> str:
        """按状态给卡片描边（参考稿 .node-card.online / .starting）。"""
        if vm.badge_tone == "starting":
            return BORDER_STARTING
        if vm.alive:
            return BORDER_ONLINE
        return LINE

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

    def _cell_ok(self, vm: ListItemVM, attr: str) -> bool:
        ok = bool(getattr(vm, attr))
        if attr == "algo_trading_ok":
            # 算法交易状态未知时不能算「已开启」
            ok = ok and vm.algo_trading_known
        return ok

    def _id_text(self, vm: ListItemVM) -> str:
        """金色胶囊里的交易账号（参考稿的 mt5-id-inline 位）。"""
        return f"ID {vm.account_label or '-'}"

    @staticmethod
    def _server_text(vm: ListItemVM) -> str:
        """元信息左位：券商服务器名（如 trademaxglobal）。

        读不到时显示占位符（参考稿的「服务器 -」），不拿账号顶替 ——
        账号在右边的金色胶囊里已经有一份，重复反而看不出服务器是未知。
        """
        return f"▤  {vm.server_label or '服务器 -'}"

    def _toggle_text(self, vm: ListItemVM) -> str:
        if vm.badge_tone == "starting":
            return "启动中…"
        # 参考稿的文案是「暂停」/「启动」
        return "暂停" if vm.alive else "启动"

    # —— 差分更新 ——

    def update(self, vm: ListItemVM) -> None:
        """就地更新卡片。**不重建任何控件。**"""
        old = self._vm
        self._vm = vm

        if old.title != vm.title:
            self._name_label.configure(text=vm.title)
        if old.server_label != vm.server_label or old.account_label != vm.account_label:
            self._server_label.configure(text=self._server_text(vm))
        if old.account_label != vm.account_label:
            self._id_label.configure(text=self._id_text(vm))
        if old.uptime_text != vm.uptime_text:
            self._uptime_label.configure(text=vm.uptime_text)

        if old.badge_text != vm.badge_text or old.badge_tone != vm.badge_tone:
            self._badge.configure(text=f"  {vm.badge_text}  ",
                                   text_color=self._tone_color(vm),
                                   fg_color=badge_bg_for(self._tone_color(vm)))

        if old.health != vm.health or old.alive != vm.alive:
            self._dot.configure(fg_color=self._state_color(vm))

        for col, (_label, attr, ok_text, bad_text) in enumerate(_CELL_SPECS):
            ok = self._cell_ok(vm, attr)
            cell = self._cells[col]
            want = ok_text if ok else bad_text
            if cell.value_label.cget("text") != want:
                cell.value_label.configure(text=want)
            # .info-value.ok 是绿色，.no 是 --dim(灰) —— 失败态刻意弱化
            color = GREEN if ok else DIM
            if cell.value_label.cget("text_color") != color:
                cell.value_label.configure(text_color=color)
            mark = cell.mark_label
            if mark.cget("text") != ("✓" if ok else "✕"):
                mark.configure(text="✓" if ok else "✕")
            mark_color = GREEN if ok else RED_SOFT
            if mark.cget("text_color") != mark_color:
                mark.configure(text_color=mark_color)
            mark_bg = cell.mark_frame.cget("fg_color")
            want_bg = GREEN_ICO_BG if ok else RED_ICO_BG
            if mark_bg != want_bg:
                cell.mark_frame.configure(fg_color=want_bg)

        if old.alive != vm.alive or old.badge_tone != vm.badge_tone:
            self._toggle_btn.configure(text=self._toggle_text(vm))

        if (old.selected != vm.selected or old.alive != vm.alive
                or old.badge_tone != vm.badge_tone):
            self._apply_border()

        self._sync_pulse(vm)

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
        # 参考稿是 opacity 1↔.35；Tk 无 alpha，用「亮色↔暗色」近似
        self._dot.configure(fg_color=AMBER if self._pulse_on else "#6b5325")
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
