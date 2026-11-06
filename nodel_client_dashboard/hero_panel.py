"""顶部 hero 统计卡：节点总数 + 状态分布 + 三个关键指标。

结构对齐参考稿 `UI/控制端UI.html` 外链 CSS 的 `.stat-hero`：

    .stat-hero          圆角 14 / 描边 rgba(217,179,108,.24) / padding 18 18 14
                        背景 = 双径向渐变 + 线性渐变（Canvas 手绘）
    .stat-hero-bg       32px 网格纹理 + 径向遮罩（Canvas 手绘）
    .stat-icon.hero     46px 圆角 12 金色描边 + 金色底
    .stat-value.hero    mono 32/700
    .stat-chip          高 26 胶囊
    .stat-dist-bar      高 6 胶囊条，段色 online/starting/paused/offline
    .stat-hero-metrics  上方 1px 分隔线 + 三格，格间 1px 竖线
    .stat-metric-icon   32px 圆角 9 语义色底

Tk 没有 CSS 渐变/遮罩，参考稿的质感靠 `canvas_fx` 手绘逼近。
"""
from __future__ import annotations

import customtkinter as ctk

import canvas_fx
from theme import (AMBER, BLUE, DIST_TRACK, GOLD, GREEN, METRIC_RULE, MUTE,
                   PANEL2, RED, TEXT, VIOLET, font, metric_cell, mono, status_chip)
from viewmodels import HeroVM

# 分布条的段序（与 viewmodels.badge_for 的色调一一对应）
_STATE_ORDER = (("online", GREEN), ("starting", AMBER), ("offline", MUTE), ("pending", GOLD))

# —— hero 背景的渐变参数（对应 CSS 的两层 radial-gradient）——
# .stat-hero-bg background-size: 32px 32px
_GRID_STEP = 32
# mask: radial-gradient(ellipse at 82% 16%, rgba(0,0,0,.75), transparent 70%)
_MASK_CX = 0.82
_MASK_CY = 0.16
_MASK_RX = 0.70
_MASK_RY = 0.85
# linear-gradient(rgba(148,163,184,.05) 1px, transparent 1px)
_GRID_LINE = "#94a3b8"
_GRID_ALPHA = 0.05

# .stat-hero 的描边 rgba(217,179,108,.24) 混在 PANEL2 上
_HERO_BORDER = "#2e2618"
# hero 圆角
_HERO_RADIUS = 14


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
            "fg_color": PANEL2,
            "border_width": 1,
            "border_color": _HERO_BORDER,
            "corner_radius": _HERO_RADIUS,
        }
        opts.update(kwargs)
        super().__init__(master, **opts)

        # 背景纹理画在最底层：canvas 必须先于内容控件创建，才在下面。
        self.canvas = ctk.CTkCanvas(self, bg=PANEL2, highlightthickness=0, bd=0)
        self.canvas.place(x=0, y=0, relwidth=1, relheight=1)
        self.canvas._on_restore_redraw = self._redraw_texture
        self.canvas.bind("<Configure>", self._redraw_texture)

        # 缓存已绘制的尺寸，避免拖动窗口时反复重画上百个 item
        self._drawn_size: tuple[int, int] = (0, 0)

        self._build()

    # ---------------------------------------------------------------- 构建

    def _build(self) -> None:
        # —— 主行：左边总数，右边状态胶囊 ——
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=18, pady=(16, 0))

        left = ctk.CTkFrame(head, fg_color="transparent")
        left.pack(side="left")

        # .stat-icon.hero {width:46px;height:46px;border-radius:12px;
        #   border:1px solid rgba(217,179,108,.32);background:#d9b36c1f}
        icon_box = ctk.CTkFrame(
            left, fg_color="#282a2e", corner_radius=12, width=46, height=46,
            border_width=1, border_color="#4a4028",
        )
        icon_box.pack(side="left", padx=(0, 12))
        icon_box.pack_propagate(False)
        ctk.CTkLabel(icon_box, text="▤", text_color=GOLD, font=font(20, "bold")).pack()

        title_box = ctk.CTkFrame(left, fg_color="transparent")
        title_box.pack(side="left")
        self._total_label = ctk.CTkLabel(
            title_box, text="0", text_color=TEXT, font=mono(32, "bold"),
            height=38, width=64, anchor="w",
        )
        self._total_label.pack(anchor="w")
        # 必须给定宽度，否则窄容器里「节点总数」会被折成两行
        ctk.CTkLabel(
            title_box, text="节点总数", text_color=MUTE, font=font(12), height=16,
            width=64, anchor="w",
        ).pack(anchor="w")

        # .stat-status-chips {display:flex;flex-wrap:wrap;gap:8px}
        chips = ctk.CTkFrame(head, fg_color="transparent")
        chips.pack(side="right")
        self._chips: dict[str, ctk.CTkFrame] = {}
        for key, label in (("online", "在线"), ("starting", "启动中"),
                           ("offline", "已停止"), ("pending", "待审核")):
            color = dict(_STATE_ORDER)[key]
            chip = status_chip(chips, label, color)
            chip.pack(side="left", padx=(8, 0))
            self._chips[key] = chip

        # —— 分布条 ——
        # .stat-dist-bar {height:6px;border-radius:999px;background:#94a3b81f;margin-top:16px}
        self._dist_frame = ctk.CTkFrame(self, fg_color=DIST_TRACK, corner_radius=3, height=6)
        self._dist_frame.pack(fill="x", padx=18, pady=(16, 0))
        self._dist_frame.pack_propagate(False)
        self._dist_segments: dict[str, ctk.CTkFrame] = {}
        for key, color in _STATE_ORDER:
            seg = ctk.CTkFrame(self._dist_frame, fg_color=color, corner_radius=3)
            self._dist_segments[key] = seg

        # —— 三个指标 ——
        # .stat-hero-metrics {margin-top:16px;padding-top:14px;
        #   border-top:1px solid rgba(148,163,184,.12)}
        sep = ctk.CTkFrame(self, fg_color=METRIC_RULE, height=1)
        sep.pack(fill="x", padx=18, pady=(16, 0))

        metrics = ctk.CTkFrame(self, fg_color="transparent")
        metrics.pack(fill="x", padx=18, pady=(14, 16))
        self._metrics: dict[str, ctk.CTkFrame] = {}
        # 指标语义跟着 viewmodels.HeroVM 走（参考稿的「今日信号/平均延迟」
        # 是它自己的业务数据，这里对应的是已连接/已鉴权/异常提醒）
        metric_specs = (
            ("connected_mt5", "已连接MT5", VIOLET, "◈"),
            ("authenticated", "已鉴权", BLUE, "◷"),
            ("alerts", "异常提醒", RED, "⚠"),
        )
        for index, (key, label, color, glyph) in enumerate(metric_specs):
            cell = metric_cell(metrics, "0", label, color, glyph=glyph)
            # .stat-metric {padding:0 12px}，首尾去掉外侧 padding
            cell.pack(side="left", expand=True, fill="x",
                      padx=(0 if index == 0 else 12, 0 if index == 2 else 12))
            self._metrics[key] = cell
            # .stat-metric + .stat-metric {border-left:1px solid rgba(148,163,184,.12)}
            if index:
                rule = ctk.CTkFrame(metrics, width=1, height=32, fg_color=METRIC_RULE,
                                    border_width=0)
                rule.pack(side="left", fill="y")

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
            chip.label.configure(text=f"{labels[key]} {counts[key]}")

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
        """重画 hero 背景：双径向渐变 + 32px 网格纹理。

        最小化恢复后 CTkCanvas 内容会残影，app_ui 的残影修复流程会回调
        `_on_restore_redraw` 重画这里。
        """
        width = self.canvas.winfo_width()
        height = self.canvas.winfo_height()
        if width <= 1 or height <= 1:
            return
        # 尺寸没变就不重画（<Configure> 在拖动窗口时会疯狂触发）
        if self._drawn_size == (width, height) and self.canvas.find_all():
            return
        self._drawn_size = (width, height)

        self.canvas.delete("all")

        # 先铺圆角底色，否则画布的方角会盖住 hero 的圆角
        canvas_fx.draw_rounded_base(self.canvas, width, height, _HERO_RADIUS, PANEL2)

        # —— 双径向渐变 ——
        # CSS: radial-gradient(130% 170% at 100% 0%, rgba(217,179,108,.14), transparent 55%),
        #       radial-gradient(90% 130% at 0% 100%, rgba(88,166,255,.09), transparent 60%)
        canvas_fx.paint_radial(
            self.canvas, width, height,
            cx=width * 1.0, cy=height * 0.0,
            rx=width * 1.30, ry=height * 1.70,
            color="#d9b36c", alpha=0.14, base=PANEL2,
        )
        canvas_fx.paint_radial(
            self.canvas, width, height,
            cx=width * 0.0, cy=height * 1.0,
            rx=width * 0.90, ry=height * 1.30,
            color="#58a6ff", alpha=0.09, base=PANEL2,
        )

        # —— 32px 网格纹理 ——
        canvas_fx.grid_texture(
            self.canvas, width, height, _GRID_STEP,
            line_color=_GRID_LINE, base=PANEL2, alpha=_GRID_ALPHA,
            mask_cx=_MASK_CX, mask_cy=_MASK_CY,
            mask_rx=_MASK_RX, mask_ry=_MASK_RY,
        )

        # 最后补四个角，把圆角外的方块补回底色
        canvas_fx.paint_corner_patches(self.canvas, width, height, _HERO_RADIUS,
                                        page_bg=self._page_bg())

    def _page_bg(self) -> str:
        """hero 所在页面的底色，用来补圆角外的区域。

        hero 被放在滚动容器里，直接按全局背景色补即可。
        """
        from theme import BG
        return BG
