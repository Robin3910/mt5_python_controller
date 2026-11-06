"""深色金融科技主题：设计令牌 + 度量 + 组件工厂。

色板与度量**全部取自**参考稿 `UI/控制端UI.html` 外链的
`assets/index-Cd5AaG_5.css`（实测 CSS，不是目测）。三层结构：

    tokens      颜色令牌，一一对应 CSS 变量（--bg / --panel / --gold …）
    metrics     间距 / 圆角 / 字号，对应 CSS 里的 px 值
    components  控件工厂，封装 CSS 里成套的样式规则

旧的玻璃拟态色名全部保留为别名，11 个调用方不必同时改。

Tk 不支持 CSS 渐变、阴影、遮罩，参考稿靠这些营造的质感无法 1:1 复刻；
本模块的策略是「用实色 + 描边 + 手绘 Canvas」逼近观感。
"""
from __future__ import annotations

import customtkinter as ctk

# ══════════════════════════════════════════════════════════ tokens

# —— 色板（平面深蓝 + 金色主强调）——
BG = "#0a0e17"
PANEL = "#101826"
PANEL2 = "#151f31"
LINE = "#1e2a3d"

GOLD = "#d9b36c"          # 主强调：主按钮、选中态
GOLD_STRONG = "#e9c98a"   # 金色 hover
ON_GOLD = "#101826"       # 金色底上的文字（.btn-sm.primary 用 --panel）
GREEN = "#2fd381"         # 健康语义：在线 / 已连接 / 已鉴权 / 已开启
AMBER = "#f2b84b"         # 启动中 / 警告 / 待审核
RED = "#f2645f"           # 停止 / 错误 / 危险操作
BLUE = "#58a6ff"
VIOLET = "#a78bfa"

TEXT = "#e8ecf4"
DIM = "#8b96ab"
MUTE = "#5a6a82"

# —— CSS 里的半透明色（Tk 无 alpha，预先混成实色）——
# 参考稿的写法是 8 位十六进制（#2fd3811f）或 rgba()，Tk 两种都不认，
# 这里统一按「底色 + 透明度」预先混成实色。底色取该元素实际所在的背景。
_LINE_SOFT_RAW = ("#94a3b8", 0.14)      # --line-soft: rgba(148,163,184,.14)
LINE_SOFT = "#222b3a"                   # 混在 PANEL 上
INFO_CELL_BG = "#18202d"                # #ffffff09  .info-item 底
GREEN_ICO_BG = "#153433"                # #34c77b29 .status-ico.ok
RED_ICO_BG = "#31232d"                  # #ff6b5e24 .status-ico.no
AMBER_ICO_BG = "#36302a"                # #ffb34029 .status-ico.pause
RED_SOFT = "#ff7a6e"                    # .status-ico.no 文字
GOLD_LIGHT = "#ffd98a"                  # .mt5-id-inline 文字
AMBER_BADGE_BG = "#2b2b2a"              # #f2b84b1f  .badge.starting 底
GREEN_BADGE_BG = "#132e31"              # #2fd3811f  .badge.online 底
RED_BADGE_BG = "#2b212c"                # #f2645f1f  .badge.error 底
DIM_BADGE_BG = "#1d2634"                # #94a3b81a  .badge.offline 底
#   .stat-icon / .stat-metric-icon 的语义色底（xx1f）
GREEN_ICON_BG = "#132e31"
BLUE_ICON_BG = "#182940"
VIOLET_ICON_BG = "#22253f"
RED_ICON_BG = "#2b212c"
GOLD_ICON_BG = "#282a2e"
#   .stat-chip 的语义色底（xx1a）
GREEN_CHIP_BG = "#132b2f"
AMBER_CHIP_BG = "#272829"
GOLD_CHIP_BG = "#24272d"
DIM_CHIP_BG = "#1a2231"                 # #94a3b814  .stat-chip 默认底
#   .btn-icon.danger 底 #f2645f12
RED_BTN_BG = "#1f1d2a"
#   .header-user:hover 底 #f2b84b14
GOLD_HOVER_BG = "#212428"
#   .btn-sm:hover / .btn-icon:hover 底 #94a3b866
DIM_HOVER_BG = "#1a2130"
#   .stat-dist-bar 底 #94a3b81f
DIST_TRACK = "#202837"
#   .dist.offline #64748b
DIST_OFFLINE = "#64748b"

# —— 卡片描边（CSS 8 位透明色，混在 PANEL 底上）——
BORDER_ONLINE = "#164139"     # .node-card.online   #2fd38138
BORDER_STARTING = "#4e4430"   # .node-card.starting #f2b84b47
BORDER_HOVER = "#3e4858"       # .node-card:hover    #94a3b859
#   .stat-metric + .stat-metric 的竖线 rgba(148,163,184,.12)
METRIC_RULE = "#1e2735"

# —— 旧名别名：11 个文件在用，勿删 ——
BG_ELEVATED = PANEL2
GLASS = PANEL
GLASS_SOFT = PANEL2
GLASS_BORDER = LINE
GLASS_BORDER_ACTIVE = GOLD
ACCENT = GOLD
ACCENT_DIM = GOLD
ACCENT_SOFT = "#241d10"
CYAN = BLUE
TEXT_MUTED = DIM
TEXT_DIM = MUTE
DANGER = RED
DANGER_DIM = "#4a2328"
WARNING = AMBER
OK = GREEN
DEAD = MUTE


# ══════════════════════════════════════════════════════════ metrics

# —— 圆角（CSS border-radius）——
RADIUS_CARD = 10         # .node-card
RADIUS_PANEL = 8         # .panel-card
RADIUS_HERO = 14         # .stat-hero
RADIUS_INPUT = 6         # .input-field / .btn-icon / .btn-sm
RADIUS_BADGE = 4         # .badge
RADIUS_PILL = 999        # .stat-chip（Tk 里用大圆角近似）
RADIUS_INFO = 10         # .info-item
RADIUS_ID_CAP = 7        # .mt5-id-inline
RADIUS_ICON = 8          # .stat-icon
RADIUS_ICON_HERO = 12    # .stat-icon.hero
RADIUS_ICON_BOX = 9      # .stat-metric-icon
RADIUS_LOGO = 12         # .stat-icon.hero

# —— 间距（CSS gap / padding）——
GAP_GRID = 12            # .node-grid gap
GAP_CARD = 10            # .node-card gap
GAP_INFO = 6             # .node-info-grid gap
GAP_HERO_METRIC = 16     # .stat-hero-metrics padding-top / margin-top
PAD_HERO = (18, 18, 14)  # .stat-hero padding
PAD_CARD = (13, 14, 11)  # .node-card padding: 13px 14px 11px

# —— 字号（CSS font-size）——
FS_NODE_NAME = 15        # .node-name
FS_META = 12.5           # .meta-text / .mt5-id-inline
FS_INFO_LABEL = 10.5     # .info-label
FS_INFO_VALUE = 12.5     # .info-value
FS_BADGE = 11            # .badge
FS_CHIP = 12             # .stat-chip
FS_BTN_SM = 12           # .btn-sm
FS_STAT_VALUE = 18       # .stat-metric-value
FS_STAT_VALUE_HERO = 32  # .stat-value.hero
FS_STAT_LABEL = 12       # .stat-label
FS_METRIC_LABEL = 11     # .stat-metric-label
FS_HEADER_NAME = 15      # .header-name
FS_HEADER_MODULE = 13    # .header-module
FS_CLOCK = 12            # .header-clock
FS_INPUT = 14            # .input-field

# —— 控件尺寸 ——
H_BTN_SM = 28            # .btn-sm height
H_BTN = 32               # 主按钮高度
W_ICON_BTN = 26          # .btn-icon width/height
W_HEADER_ICON_BTN = 30   # .header-icon-btn
H_DOT = 8                # .status-dot
W_ICO = 19               # .status-ico
H_INPUT = 40             # .input-field height
W_AVATAR = 24            # .avatar
W_LOGO = 32              # .header-logo
H_HEADER = 54            # .console-header height
W_LOGO_LOGIN = 52        # .login-logo


def apply_theme() -> None:
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("dark-blue")


def font(size: int = 13, weight: str = "normal") -> ctk.CTkFont:
    # Windows 优先科技感无衬线；缺失时 CTk 会回退
    # Tk 的 font size 只接受整数，CSS 里的 10.5 / 12.5 得四舍五入
    try:
        return ctk.CTkFont(family="Segoe UI Semibold" if weight == "bold" else "Segoe UI",
                          size=_tk_size(size), weight=weight)
    except Exception:  # noqa: BLE001
        return ctk.CTkFont(size=_tk_size(size), weight=weight)


def mono(size: int = 12, weight: str = "normal") -> ctk.CTkFont:
    """等宽字体。参考稿的节点名 / 账号 / ID / 数字都用 mono。"""
    try:
        return ctk.CTkFont(family="Consolas", size=_tk_size(size), weight=weight)
    except Exception:  # noqa: BLE001
        return ctk.CTkFont(size=_tk_size(size), weight=weight)


def _tk_size(size: float) -> int:
    """Tk 只接受整数字号；CSS 的 .5 用四舍五入（12.5 -> 13）。"""
    return max(1, int(float(size) + 0.5))


def _mix(color: str, ratio: float, base: str = BG) -> str:
    """把颜色按比例混到底色上，得到实色的「半透明」效果。

    Tk 不支持 8 位十六进制透明度，参考稿的 `#2fd3811f` 这类写法得手工混。
    """
    def _parts(value: str) -> tuple[int, int, int]:
        value = value.lstrip("#")
        return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)

    r, g, b = _parts(color)
    br, bg_, bb = _parts(base)
    ratio = max(0.0, min(1.0, ratio))
    return "#{:02x}{:02x}{:02x}".format(
        int(br + (r - br) * ratio),
        int(bg_ + (g - bg_) * ratio),
        int(bb + (b - bb) * ratio),
    )


# ══════════════════════════════════════════════════════════ components


def glass_card(master, **kwargs) -> ctk.CTkFrame:
    """主面板卡：面板底 + 细边框 + 8 圆角。"""
    opts = {
        "fg_color": PANEL,
        "border_width": 1,
        "border_color": LINE,
        "corner_radius": RADIUS_PANEL,
    }
    opts.update(kwargs)
    return ctk.CTkFrame(master, **opts)


def soft_card(master, **kwargs) -> ctk.CTkFrame:
    """次级面板卡：更亮一点的底，用于卡片内的子块。"""
    opts = {
        "fg_color": PANEL2,
        "border_width": 1,
        "border_color": LINE,
        "corner_radius": RADIUS_PANEL,
    }
    opts.update(kwargs)
    return ctk.CTkFrame(master, **opts)


def primary_btn(master, text: str, command=None, width: int = 96, **kwargs) -> ctk.CTkButton:
    """主操作按钮：金色底 + 深色字。"""
    opts = {
        "text": text,
        "command": command,
        "width": width,
        "height": H_BTN,
        "corner_radius": RADIUS_INPUT,
        "fg_color": GOLD,
        "hover_color": GOLD_STRONG,
        "text_color": ON_GOLD,
        "font": font(13, "bold"),
        "border_width": 0,
    }
    opts.update(kwargs)
    return ctk.CTkButton(master, **opts)


def ghost_btn(master, text: str, command=None, width: int = 96, **kwargs) -> ctk.CTkButton:
    """次级按钮：透明底 + 细边框。"""
    opts = {
        "text": text,
        "command": command,
        "width": width,
        "height": H_BTN,
        "corner_radius": RADIUS_INPUT,
        "fg_color": "transparent",
        "hover_color": DIM_HOVER_BG,
        "text_color": DIM,
        "border_width": 1,
        "border_color": LINE,
        "font": font(13),
    }
    opts.update(kwargs)
    return ctk.CTkButton(master, **opts)


def danger_btn(master, text: str, command=None, width: int = 96, **kwargs) -> ctk.CTkButton:
    """危险操作按钮：红色描边 + 红字（不用实底，避免和主按钮抢注意力）。"""
    opts = {
        "text": text,
        "command": command,
        "width": width,
        "height": H_BTN,
        "corner_radius": RADIUS_INPUT,
        "fg_color": "transparent",
        "hover_color": RED_ICON_BG,
        "text_color": RED,
        "border_width": 1,
        "border_color": _mix(RED, 0.35),
        "font": font(13, "bold"),
    }
    opts.update(kwargs)
    return ctk.CTkButton(master, **opts)


def toolbar_btn(master, text: str, command=None, width: int = 96, **kwargs) -> ctk.CTkButton:
    """工具条小按钮 `.btn-sm`：高 28、字号 12、面板底 + 细边框。"""
    opts = {
        "text": text,
        "command": command,
        "width": width,
        "height": H_BTN_SM,
        "corner_radius": RADIUS_INPUT,
        "fg_color": PANEL2,
        "hover_color": DIM_HOVER_BG,
        "text_color": DIM,
        "border_width": 1,
        "border_color": LINE,
        "font": font(FS_BTN_SM),
    }
    opts.update(kwargs)
    return ctk.CTkButton(master, **opts)


def amber_btn(master, text: str, command=None, width: int = 96, **kwargs) -> ctk.CTkButton:
    """琥珀主操作 `.btn-sm.amber`：琥珀描边 + 琥珀字 + 淡琥珀底。"""
    opts = {
        "text": text,
        "command": command,
        "width": width,
        "height": H_BTN_SM,
        "corner_radius": RADIUS_INPUT,
        "fg_color": AMBER_CHIP_BG,
        "hover_color": "#2a2317",
        "text_color": AMBER,
        "border_width": 1,
        "border_color": _mix(AMBER, 0.4),
        "font": font(FS_BTN_SM, "bold"),
    }
    opts.update(kwargs)
    return ctk.CTkButton(master, **opts)


def gold_btn(master, text: str, command=None, width: int = 96, **kwargs) -> ctk.CTkButton:
    """金色主操作 `.btn-sm.primary`：金色底 + 深色字。"""
    opts = {
        "text": text,
        "command": command,
        "width": width,
        "height": H_BTN_SM,
        "corner_radius": RADIUS_INPUT,
        "fg_color": GOLD,
        "hover_color": GOLD_STRONG,
        "text_color": ON_GOLD,
        "border_width": 0,
        "font": font(FS_BTN_SM, "bold"),
    }
    opts.update(kwargs)
    return ctk.CTkButton(master, **opts)


def icon_btn(master, glyph: str, command=None, **kwargs) -> ctk.CTkButton:
    """图标按钮 `.btn-icon`：26×26 方形，PANEL2 底 + 细边框。"""
    opts = {
        "text": glyph,
        "command": command,
        "width": W_ICON_BTN,
        "height": W_ICON_BTN,
        "corner_radius": RADIUS_INPUT,
        "fg_color": PANEL2,
        "hover_color": DIM_HOVER_BG,
        "text_color": MUTE,
        "border_width": 1,
        "border_color": LINE,
        "font": font(12),
    }
    opts.update(kwargs)
    return ctk.CTkButton(master, **opts)


def danger_icon_btn(master, glyph: str, command=None, **kwargs) -> ctk.CTkButton:
    """危险图标按钮 `.btn-icon.danger`：红字 + 红底 + 红边。"""
    opts = {
        "text": glyph,
        "command": command,
        "width": W_ICON_BTN,
        "height": W_ICON_BTN,
        "corner_radius": RADIUS_INPUT,
        "fg_color": RED_BTN_BG,
        "hover_color": _mix(RED, 0.16),
        "text_color": RED,
        "border_width": 1,
        "border_color": _mix(RED, 0.3),
        "font": font(12),
    }
    opts.update(kwargs)
    return ctk.CTkButton(master, **opts)


def status_chip(master, text: str, color: str, **kwargs) -> ctk.CTkFrame:
    """状态小胶囊 `.stat-chip`：胶囊圆角 + 语义色文字 + 语义色底 + 7px 圆点。

    参考稿的结构是 `<span class="stat-chip online"><i></i>在线 3</span>`，
    那个 `<i>` 是一个 7px 的圆点。Tk 的 Label 塞不进子控件，所以这里用一个
    `CTkFrame` 当胶囊（圆角由 CTk 画），里面放一个圆点 frame + 文字 label。

    返回的是 frame，上面挂了 `dot`（圆点）和 `label`（文字），
    调用方改文案用 `chip.label.configure(text=...)`。
    """
    dot_size = 7
    chip = ctk.CTkFrame(
        master,
        fg_color=_mix(color, 0.1),
        corner_radius=RADIUS_PILL,
        height=26,
        border_width=0,
    )
    chip.pack_propagate(False)
    dot = ctk.CTkFrame(
        chip, width=dot_size, height=dot_size, corner_radius=dot_size // 2,
        fg_color=color, border_width=0,
    )
    dot.pack(side="left", padx=(10, 6))
    dot.pack_propagate(False)
    label = ctk.CTkLabel(
        chip, text=text, text_color=color, font=font(FS_CHIP, "bold"), height=26,
    )
    label.pack(side="left", padx=(0, 10))
    chip.dot = dot
    chip.label = label
    chip.chip_color = color
    return chip


def badge(master, text: str, color: str, **kwargs) -> ctk.CTkLabel:
    """状态徽章 `.badge`：12% 底 + 30% 边框，小圆角。"""
    opts = {
        "text": f"  {text}  ",
        "fg_color": _mix(color, 0.12),
        "text_color": color,
        "corner_radius": RADIUS_BADGE,
        "font": font(FS_BADGE, "bold"),
        "height": 20,
    }
    opts.update(kwargs)
    return ctk.CTkLabel(master, **opts)


def badge_bg_for(color: str) -> str:
    """徽章底色：按语义色取对应的预混色。"""
    return {
        GREEN: GREEN_BADGE_BG,
        AMBER: AMBER_BADGE_BG,
        RED: RED_BADGE_BG,
        MUTE: DIM_BADGE_BG,
        GOLD: AMBER_BADGE_BG,
    }.get(color, _mix(color, 0.12))


def divider(master, **kwargs) -> ctk.CTkFrame:
    """细分隔线（.node-card-meta / .node-card-foot 的 border）。"""
    opts = {
        "fg_color": LINE_SOFT,
        "height": 1,
        "corner_radius": 0,
    }
    opts.update(kwargs)
    return ctk.CTkFrame(master, **opts)


def info_cell(master, label: str, value: str, ok: bool) -> ctk.CTkFrame:
    """卡片内的信息格 `.info-item`（连接MT5 / 是否鉴权 / 算法交易）。

    参考稿结构：垂直居中，上排是「19px 圆形图标 + 标签」，下排是取值。
    `.info-value.no` 的颜色是 `--dim`（灰），不是红色 —— 失败态刻意弱化。

    返回的 frame 上挂 `value_label` / `mark_label` / `mark_frame`，供差分更新
    与测试断言使用。
    """
    cell = ctk.CTkFrame(
        master,
        fg_color=INFO_CELL_BG,
        border_width=1,
        border_color=LINE_SOFT,
        corner_radius=RADIUS_INFO,
    )
    head = ctk.CTkFrame(cell, fg_color="transparent")
    head.pack(pady=(7, 0))
    # 图标是 19px 圆底，不是裸文字
    mark_frame = ctk.CTkFrame(
        head, width=W_ICO, height=W_ICO, corner_radius=W_ICO // 2,
        fg_color=GREEN_ICO_BG if ok else RED_ICO_BG,
        border_width=0,
    )
    mark_frame.pack(side="left", padx=(0, 4))
    mark_frame.pack_propagate(False)
    mark_label = ctk.CTkLabel(
        mark_frame, text="✓" if ok else "✕", text_color=GREEN if ok else RED_SOFT,
        font=font(10, "bold"), width=W_ICO, height=W_ICO,
    )
    mark_label.pack()
    ctk.CTkLabel(
        head, text=label, text_color=MUTE, font=font(FS_INFO_LABEL), height=16
    ).pack(side="left")
    value_label = ctk.CTkLabel(
        cell, text=value, text_color=GREEN if ok else DIM, font=font(FS_INFO_VALUE, "bold"),
        height=16,
    )
    value_label.pack(pady=(4, 7))
    cell.value_label = value_label
    cell.mark_label = mark_label
    cell.mark_frame = mark_frame
    cell.label_text = label
    return cell


def metric_cell(master, value: str, label: str, accent: str, glyph: str = "") -> ctk.CTkFrame:
    """hero 底部的指标格 `.stat-metric`：32px 圆角图标块 + 数值 + 标签。

    参考稿里相邻指标之间有一条 1px 竖线（`.stat-metric + .stat-metric`），
    竖线由调用方负责画。
    """
    cell = ctk.CTkFrame(master, fg_color="transparent")
    row = ctk.CTkFrame(cell, fg_color="transparent")
    row.pack(fill="x")

    if glyph:
        icon_box = ctk.CTkFrame(
            row, width=32, height=32, corner_radius=RADIUS_ICON_BOX,
            fg_color=_icon_bg_for(accent), border_width=0,
        )
        icon_box.pack(side="left", padx=(0, 10))
        icon_box.pack_propagate(False)
        ctk.CTkLabel(
            icon_box, text=glyph, text_color=accent, font=font(14), width=32, height=32
        ).pack()

    text_box = ctk.CTkFrame(row, fg_color="transparent")
    text_box.pack(side="left", fill="x", expand=True)
    value_label = ctk.CTkLabel(
        text_box, text=value, text_color=TEXT, font=mono(FS_STAT_VALUE, "bold"), height=22
    )
    value_label.pack(anchor="w")
    ctk.CTkLabel(
        text_box, text=label, text_color=MUTE, font=font(FS_METRIC_LABEL), height=15
    ).pack(anchor="w")
    cell.value_label = value_label
    return cell


def _icon_bg_for(accent: str) -> str:
    return {
        VIOLET: VIOLET_ICON_BG,
        BLUE: BLUE_ICON_BG,
        RED: RED_ICON_BG,
        GREEN: GREEN_ICON_BG,
        GOLD: GOLD_ICON_BG,
    }.get(accent, PANEL2)


def health_color(health: str, alive: bool) -> str:
    """健康态取语义色。进程不在时一律灰。"""
    if not alive:
        return MUTE
    h = (health or "").lower()
    if h == "ok":
        return GREEN
    if h in ("starting", "unknown"):
        return AMBER
    if h == "error":
        return RED
    return BLUE
