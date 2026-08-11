"""深色金融科技主题：玻璃拟态卡片 + 安全强调色。"""
from __future__ import annotations

import customtkinter as ctk

# —— 色板（深空蓝底 + 青绿安全强调，避免紫系）——
BG = "#070B14"
BG_ELEVATED = "#0C1424"
GLASS = "#121C2E"
GLASS_SOFT = "#162235"
GLASS_BORDER = "#2A3F5F"
GLASS_BORDER_ACTIVE = "#2EE6A8"
ACCENT = "#2EE6A8"          # 安全/健康强调
ACCENT_DIM = "#1A9E72"
ACCENT_SOFT = "#143D32"
CYAN = "#5CE1FF"
TEXT = "#E8EEF8"
TEXT_MUTED = "#8B9BB4"
TEXT_DIM = "#5C6B82"
DANGER = "#FF5C7A"
DANGER_DIM = "#8B2E40"
WARNING = "#F5B942"
OK = "#2EE6A8"
DEAD = "#6B7A90"
CHIP_BG = "#0F1A2C"


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


def glass_card(master, **kwargs) -> ctk.CTkFrame:
    opts = {
        "fg_color": GLASS,
        "border_width": 1,
        "border_color": GLASS_BORDER,
        "corner_radius": 16,
    }
    opts.update(kwargs)
    return ctk.CTkFrame(master, **opts)


def soft_card(master, **kwargs) -> ctk.CTkFrame:
    opts = {
        "fg_color": GLASS_SOFT,
        "border_width": 1,
        "border_color": GLASS_BORDER,
        "corner_radius": 12,
    }
    opts.update(kwargs)
    return ctk.CTkFrame(master, **opts)


def primary_btn(master, text: str, command=None, width: int = 96, **kwargs) -> ctk.CTkButton:
    opts = {
        "text": text,
        "command": command,
        "width": width,
        "height": 34,
        "corner_radius": 10,
        "fg_color": ACCENT_DIM,
        "hover_color": ACCENT,
        "text_color": "#04140F",
        "font": font(13, "bold"),
        "border_width": 0,
    }
    opts.update(kwargs)
    return ctk.CTkButton(master, **opts)


def ghost_btn(master, text: str, command=None, width: int = 96, **kwargs) -> ctk.CTkButton:
    opts = {
        "text": text,
        "command": command,
        "width": width,
        "height": 34,
        "corner_radius": 10,
        "fg_color": "transparent",
        "hover_color": GLASS_SOFT,
        "text_color": TEXT,
        "border_width": 1,
        "border_color": GLASS_BORDER,
        "font": font(13),
    }
    opts.update(kwargs)
    return ctk.CTkButton(master, **opts)


def danger_btn(master, text: str, command=None, width: int = 96, **kwargs) -> ctk.CTkButton:
    opts = {
        "text": text,
        "command": command,
        "width": width,
        "height": 34,
        "corner_radius": 10,
        "fg_color": DANGER_DIM,
        "hover_color": DANGER,
        "text_color": TEXT,
        "font": font(13, "bold"),
    }
    opts.update(kwargs)
    return ctk.CTkButton(master, **opts)


def health_color(health: str, alive: bool) -> str:
    if not alive:
        return DEAD
    h = (health or "").lower()
    if h == "ok":
        return OK
    if h in ("starting", "unknown"):
        return WARNING
    if h == "error":
        return DANGER
    return CYAN


def status_chip(master, text: str, color: str) -> ctk.CTkLabel:
    return ctk.CTkLabel(
        master,
        text=f"  {text}  ",
        fg_color=CHIP_BG,
        text_color=color,
        corner_radius=8,
        font=font(12, "bold"),
        height=26,
    )
