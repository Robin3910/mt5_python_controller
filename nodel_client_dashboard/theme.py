"""深色金融科技主题：平面深色面板 + 金色主强调 + 状态语义色。

色板取自参考稿 `UI/控制端UI.html` 的实测 CSS 变量。旧的玻璃拟态色名全部保留为
别名，11 个调用方不必同时改。
"""
from __future__ import annotations

import customtkinter as ctk

# —— 色板（平面深蓝 + 金色主强调，避免紫系）——
BG = "#0a0e17"
PANEL = "#101826"
PANEL2 = "#151f31"
LINE = "#1e2a3d"

GOLD = "#d9b36c"          # 主强调：主按钮、选中态
GOLD_STRONG = "#e9c98a"   # 金色 hover
ON_GOLD = "#1b1408"       # 金色底上的文字
GREEN = "#2fd381"         # 健康语义：在线 / 已连接 / 已鉴权 / 已开启
AMBER = "#f2b84b"         # 启动中 / 警告 / 待审核
RED = "#f2645f"           # 停止 / 错误 / 危险操作
BLUE = "#58a6ff"
VIOLET = "#a78bfa"

TEXT = "#e8ecf4"
DIM = "#8b96ab"
MUTE = "#5a6a82"
CHIP_BG = "#0d1524"

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


def apply_theme() -> None:
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("dark-blue")


def font(size: int = 13, weight: str = "normal") -> ctk.CTkFont:
    # Windows 优先科技感无衬线；缺失时 CTk 会回退
    try:
        return ctk.CTkFont(family="Segoe UI Semibold" if weight == "bold" else "Segoe UI",
                          size=size, weight=weight)
    except Exception:  # noqa: BLE001
        return ctk.CTkFont(size=size, weight=weight)


def mono(size: int = 12) -> ctk.CTkFont:
    try:
        return ctk.CTkFont(family="Consolas", size=size)
    except Exception:  # noqa: BLE001
        return ctk.CTkFont(size=size)


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


def glass_card(master, **kwargs) -> ctk.CTkFrame:
    """主面板卡：面板底 + 细边框 + 10 圆角。"""
    opts = {
        "fg_color": PANEL,
        "border_width": 1,
        "border_color": LINE,
        "corner_radius": 10,
    }
    opts.update(kwargs)
    return ctk.CTkFrame(master, **opts)


def soft_card(master, **kwargs) -> ctk.CTkFrame:
    """次级面板卡：更亮一点的底，用于卡片内的子块。"""
    opts = {
        "fg_color": PANEL2,
        "border_width": 1,
        "border_color": LINE,
        "corner_radius": 10,
    }
    opts.update(kwargs)
    return ctk.CTkFrame(master, **opts)


def primary_btn(master, text: str, command=None, width: int = 96, **kwargs) -> ctk.CTkButton:
    """主操作按钮：金色底 + 深色字。"""
    opts = {
        "text": text,
        "command": command,
        "width": width,
        "height": 32,
        "corner_radius": 6,
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
        "height": 32,
        "corner_radius": 6,
        "fg_color": "transparent",
        "hover_color": PANEL2,
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
        "height": 32,
        "corner_radius": 6,
        "fg_color": "transparent",
        "hover_color": _mix(RED, 0.16),
        "text_color": RED,
        "border_width": 1,
        "border_color": _mix(RED, 0.5),
        "font": font(13, "bold"),
    }
    opts.update(kwargs)
    return ctk.CTkButton(master, **opts)


def toolbar_btn(master, text: str, command=None, width: int = 96, **kwargs) -> ctk.CTkButton:
    """工具条小按钮：高 28、字号 12。"""
    opts = {
        "text": text,
        "command": command,
        "width": width,
        "height": 28,
        "corner_radius": 6,
        "fg_color": PANEL2,
        "hover_color": _mix(GOLD, 0.2),
        "text_color": DIM,
        "border_width": 1,
        "border_color": LINE,
        "font": font(12),
    }
    opts.update(kwargs)
    return ctk.CTkButton(master, **opts)


def status_chip(master, text: str, color: str) -> ctk.CTkLabel:
    """状态小胶囊：深底 + 语义色文字。"""
    return ctk.CTkLabel(
        master,
        text=f"  {text}  ",
        fg_color=CHIP_BG,
        text_color=color,
        corner_radius=5,
        font=font(12, "bold"),
        height=24,
    )


def badge(master, text: str, color: str) -> ctk.CTkLabel:
    """状态徽章：12% 底 + 30% 边框，小圆角。"""
    return ctk.CTkLabel(
        master,
        text=f"  {text}  ",
        fg_color=_mix(color, 0.12),
        text_color=color,
        corner_radius=4,
        font=font(11, "bold"),
        height=20,
    )


def info_cell(master, label: str, value: str, ok: bool) -> ctk.CTkFrame:
    """卡片内的信息格（连接MT5 / 是否鉴权 / 算法交易）。

    结构对齐参考稿：上排是「✓ 标签」，下排是取值。

    返回的 frame 上挂 `value_label` / `mark_label`，供差分更新与测试断言使用。
    """
    color = GREEN if ok else MUTE
    cell = ctk.CTkFrame(
        master,
        fg_color=_mix(TEXT, 0.035),
        border_width=1,
        border_color=_mix(TEXT, 0.09),
        corner_radius=8,
    )
    head = ctk.CTkFrame(cell, fg_color="transparent")
    head.pack(fill="x", padx=8, pady=(6, 0))
    mark_label = ctk.CTkLabel(
        head, text="✓" if ok else "✗", text_color=color, font=font(12, "bold"),
        width=14, height=16,
    )
    mark_label.pack(side="left", padx=(0, 3))
    ctk.CTkLabel(
        head, text=label, text_color=MUTE, font=font(10), height=16, anchor="w"
    ).pack(side="left", fill="x", expand=True)
    value_label = ctk.CTkLabel(
        cell, text=value, text_color=color, font=font(11, "bold"), height=16, anchor="w"
    )
    value_label.pack(fill="x", padx=8, pady=(0, 6))
    cell.value_label = value_label
    cell.mark_label = mark_label
    cell.label_text = label
    return cell


def metric_cell(master, value: str, label: str, accent: str) -> ctk.CTkFrame:
    """hero 底部的指标格：大数值 + 小标签。"""
    cell = ctk.CTkFrame(master, fg_color="transparent")
    value_label = ctk.CTkLabel(
        cell, text=value, text_color=accent, font=font(20, "bold"), height=26
    )
    value_label.pack(anchor="w")
    ctk.CTkLabel(
        cell, text=label, text_color=DIM, font=font(11), height=16
    ).pack(anchor="w")
    cell.value_label = value_label
    return cell


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
