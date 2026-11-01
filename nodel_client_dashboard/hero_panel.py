"""顶部 hero 统计卡：节点总数 + 状态分布 + 三个关键指标。

结构对齐参考稿 `UI/控制端UI.html` 的 `.stat-hero`：

    节点总数（大数字）        [在线 3][启动中 1][已停止 1][待审核 0]
    ▓▓▓▓▓▓░░░░░░  状态分布条
    ─────────────────────────────────────────
    [已连接MT5 3][已鉴权 2][异常提醒 1]

背景的 32px 网格纹理用 CTkCanvas 手绘（Tk 没有 CSS 渐变/遮罩）。
"""
from __future__ import annotations

import customtkinter as ctk

from theme import (AMBER, BLUE, DIM, GOLD, GREEN, LINE, MUTE, PANEL, PANEL2,
                   RED, TEXT, font, metric_cell, mono, status_chip)
from viewmodels import HeroVM

# 分布条的段序（与 viewmodels.badge_for 的色调一一对应）
_STATE_ORDER = (("online", GREEN), ("starting", AMBER), ("offline", MUTE), ("pending", GOLD))

# 网格纹理线色：比边框略亮一点，隐约可见但不抢视线
_GRID_LINE = "#182338"


def distribution_segments(hero: HeroVM) -> list[tuple[str, float]]:
    """按状态占比返回 [(状态, 比例)]，占比为 0 的状态不出现。总数为 0 时返回空表。"""
    counts = {
        "online": hero.online,
        "starting": hero.starting,
        "offline": hero.stopped,
        "pending": hero.pending,
    }
    if hero.total <= 0:
        return []
    return [(k, counts[k] / hero.total) for k, _c in _STATE_ORDER if counts[k] > 0]


class HeroPanel(ctk.CTkFrame):
    """顶部统计卡。宽度随父容器，高度随内容。"""

    def __init__(self, master, **kwargs) -> None:
        opts = {
            "fg_color": PANEL,
            "border_width": 1,
            "border_color": LINE,
            "corner_radius": 14,
        }
        opts.update(kwargs)
        super().__init__(master, **opts)

        # 背景纹理画在最底层
        self.canvas = ctk.CTkCanvas(self, height=96, bg=PANEL, highlightthickness=0, bd=0)
        self.canvas.place(x=0, y=0, relwidth=1, relheight=0.55)
        self.canvas._on_restore_redraw = self._redraw_texture
        self.canvas.bind("<Configure>", self._redraw_texture)

        self._build()

    # ---------------------------------------------------------------- 构建

    def _build(self) -> None:
        # —— 主行：左边总数，右边状态胶囊 ——
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=18, pady=(16, 0))

        left = ctk.CTkFrame(head, fg_color="transparent")
        left.pack(side="left")

        icon_box = ctk.CTkFrame(left, fg_color="#1e1a12", corner_radius=8, width=34, height=34)
        icon_box.pack(side="left", padx=(0, 10))
        icon_box.pack_propagate(False)
        ctk.CTkLabel(icon_box, text="▤", text_color=GOLD, font=font(16, "bold")).pack()

        title_box = ctk.CTkFrame(left, fg_color="transparent")
        title_box.pack(side="left")
        self._total_label = ctk.CTkLabel(
            title_box, text="0", text_color=TEXT, font=font(30, "bold"), height=36
        )
        self._total_label.pack(anchor="w")
        ctk.CTkLabel(
            title_box, text="节点总数", text_color=DIM, font=font(12), height=16
        ).pack(anchor="w")

        chips = ctk.CTkFrame(head, fg_color="transparent")
        chips.pack(side="right")
        self._chips: dict[str, ctk.CTkLabel] = {}
        for key, label in (("online", "在线"), ("starting", "启动中"),
                           ("offline", "已停止"), ("pending", "待审核")):
            color = dict(_STATE_ORDER)[key]
            chip = status_chip(chips, label, color)
            chip.pack(side="left", padx=(6, 0))
            self._chips[key] = chip

        # —— 分布条 ——
        self._dist_frame = ctk.CTkFrame(self, fg_color=PANEL2, corner_radius=3, height=6)
        self._dist_frame.pack(fill="x", padx=18, pady=(14, 0))
        self._dist_frame.pack_propagate(False)
        self._dist_segments: dict[str, ctk.CTkFrame] = {}
        for key, color in _STATE_ORDER:
            seg = ctk.CTkFrame(self._dist_frame, fg_color=color, corner_radius=3)
            self._dist_segments[key] = seg

        # —— 三个指标 ——
        sep = ctk.CTkFrame(self, fg_color=LINE, height=1)
        sep.pack(fill="x", padx=18, pady=(14, 0))

        metrics = ctk.CTkFrame(self, fg_color="transparent")
        metrics.pack(fill="x", padx=18, pady=(12, 16))
        self._metrics: dict[str, ctk.CTkFrame] = {}
        for key, label, color in (("connected_mt5", "已连接MT5", BLUE),
                                  ("authenticated", "已鉴权", GREEN),
                                  ("alerts", "异常提醒", RED)):
            cell = metric_cell(metrics, "0", label, color)
            cell.pack(side="left", expand=True, fill="x")
            self._metrics[key] = cell

    # ---------------------------------------------------------------- 更新

    def update(self, hero: HeroVM) -> None:
        """就地更新统计数字。只改文案与位置，不重建控件。"""
        self._total_label.configure(text=str(hero.total))

        counts = {
            "online": hero.online,
            "starting": hero.starting,
            "offline": hero.stopped,
            "pending": hero.pending,
        }
        labels = {"online": "在线", "starting": "启动中",
                  "offline": "已停止", "pending": "待审核"}
        for key, chip in self._chips.items():
            chip.configure(text=f"  {labels[key]} {counts[key]}  ")

        self._update_distribution(hero)

        values = {
            "connected_mt5": str(hero.connected_mt5),
            "authenticated": str(hero.authenticated),
            "alerts": str(hero.alerts),
        }
        for key, cell in self._metrics.items():
            cell.value_label.configure(text=values[key])

    def _update_distribution(self, hero: HeroVM) -> None:
        """按比例摆放各段。比例为 0 的段隐藏。"""
        segments = distribution_segments(hero)
        for key, _color in _STATE_ORDER:
            self._dist_segments[key].place_forget()
        offset = 0.0
        for key, ratio in segments:
            seg = self._dist_segments[key]
            seg.place(relx=offset, rely=0, relwidth=ratio, relheight=1)
            offset += ratio

    # ---------------------------------------------------------------- 纹理

    def _redraw_texture(self, _event=None) -> None:
        """重画 hero 背景的 32px 网格纹理。

        最小化恢复后 CTkCanvas 内容会残影，app_ui 的残影修复流程会回调
        `_on_restore_redraw` 重画这里。
        """
        self.canvas.delete("all")
        width = self.canvas.winfo_width()
        height = self.canvas.winfo_height()
        if width <= 1 or height <= 1:
            return
        for x in range(0, width, 32):
            self.canvas.create_line(x, 0, x, height, fill=_GRID_LINE)
        for y in range(0, height, 32):
            self.canvas.create_line(0, y, width, y, fill=_GRID_LINE)
        # 右上角一抹淡金，做参考稿里的径向渐变暗示
        radius = min(width, height) * 0.9
        cx, cy = width * 0.86, height * 0.24
        self.canvas.create_oval(
            cx - radius, cy - radius, cx + radius, cy + radius,
            outline="", fill="#1a1a22",
        )
