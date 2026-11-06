"""Canvas 特效层：用 `CTkCanvas` 手绘参考稿里 CSS 渐变 / 遮罩营造的效果。

Tk 没有 CSS 的 `linear-gradient` / `radial-gradient` / `mask-image`，参考稿
`UI/控制端UI.html` 的两处质感只能靠画布逼近：

    .stat-hero-bg   32px 网格纹理 + radial-gradient 椭圆遮罩
    .login-bg-grid  48px 网格纹理 + radial-gradient 椭圆遮罩

两条实现要点：

1. **圆角**：CTk 的圆角靠「父控件底色 + 圆角多边形」实现，画布盖上去会把
   四个角填成方块。这里先画圆角底色，最后再用「角补丁」把圆弧外的区域
   补回页面底色。
2. **渐变**：Tk 只能画实色，于是把 CSS 的 alpha 预先混到底色上，再用一圈圈
   递减 alpha 的同心椭圆逼近径向渐变。
"""
from __future__ import annotations

import tkinter as tk

# 渐变圈层数：越多越平滑，但 Canvas item 也会变多
GRADIENT_RINGS = 36

# 圆角弧的采样点数
_ARC_STEPS = 6


def rgb(color: str) -> tuple[int, int, int]:
    """`#rrggbb` -> (r, g, b)。"""
    color = color.lstrip("#")
    return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)


def mix_rgb(color: tuple[int, int, int], base: str, ratio: float) -> str:
    """把 RGB 按比例混到 base 上，得到实色的「半透明」效果。

    CSS 的 `#2fd3811f` / `rgba(217,179,108,.14)` 在 Tk 里都不认，
    只能预先混成实色。
    """
    br, bg, bb = rgb(base)
    ratio = max(0.0, min(1.0, ratio))
    return "#{:02x}{:02x}{:02x}".format(
        int(br + (color[0] - br) * ratio),
        int(bg + (color[1] - bg) * ratio),
        int(bb + (color[2] - bb) * ratio),
    )


def mix(color: str, ratio: float, base: str) -> str:
    """把 `color` 按 `ratio` 混到 `base` 上，得到实色的「半透明」效果。

    与 `theme._mix` 同签名，方便两个模块之间互换。
    """
    return mix_rgb(rgb(color), base, ratio)


def rounded_rect_points(width: int, height: int, radius: int) -> list[float]:
    """采样一个圆角矩形的轮廓点（供 `create_polygon` 用）。

    四个角各取 `_ARC_STEPS + 1` 个点，角与角之间是直线段。
    """
    radius = max(0, min(radius, min(width, height) // 2))
    if radius == 0:
        return [0, 0, width, 0, width, height, 0, height]
    points: list[float] = []
    corners = (
        # (圆心 x, 圆心 y, 起始角)
        (radius, radius, 180.0),              # 左上
        (width - radius, radius, 270.0),      # 右上
        (width - radius, height - radius, 0.0),  # 右下
        (radius, height - radius, 90.0),      # 左下
    )
    import math
    for cx, cy, start in corners:
        for step in range(_ARC_STEPS + 1):
            angle = math.radians(start + 90.0 * step / _ARC_STEPS)
            points.append(cx + radius * math.cos(angle))
            points.append(cy + radius * math.sin(angle))
    return points


def draw_rounded_base(canvas: tk.Canvas, width: int, height: int, radius: int,
                      color: str) -> None:
    """画圆角底色。"""
    canvas.create_polygon(
        rounded_rect_points(width, height, radius),
        fill=color, outline="",
    )


def paint_corner_patches(canvas: tk.Canvas, width: int, height: int, radius: int,
                         page_bg: str) -> None:
    """把圆角矩形四个角之外的区域补成页面底色。

    画布本身是矩形，不做这一步的话圆角就会被方角盖住。
    """
    if radius <= 0:
        return
    import math
    patches = (
        # (圆心 x, 圆心 y, 起始角)：与 rounded_rect_points 的角对应
        (radius, radius, 180.0),
        (width - radius, radius, 270.0),
        (width - radius, height - radius, 0.0),
        (radius, height - radius, 90.0),
    )
    for cx, cy, start in patches:
        # 圆角矩形之外的部分 = 角上的正方形减去四分之一圆
        # 采样圆弧（从正方形的两条边端点之间），再和角点一起组成多边形
        arc: list[float] = []
        for step in range(_ARC_STEPS + 1):
            angle = math.radians(start + 90.0 * step / _ARC_STEPS)
            arc.append(cx + radius * math.cos(angle))
            arc.append(cy + radius * math.sin(angle))
        # 角点坐标：圆弧中心对称过去的那个角
        corner_x = 0 if cx == radius else width
        corner_y = 0 if cy == radius else height
        canvas.create_polygon(
            [corner_x, corner_y] + arc,
            fill=page_bg, outline="",
        )


def paint_radial(canvas: tk.Canvas, width: int, height: int, cx: float, cy: float,
                 rx: float, ry: float, color: str, alpha: float, base: str,
                 rings: int = GRADIENT_RINGS) -> None:
    """用同心椭圆堆出径向渐变。

    CSS 的渐变是从中心向外的连续 alpha；Tk 只能画实色椭圆，
    于是画 N 层「从外到内、alpha 递增」的椭圆，视觉上接近。
    """
    base_rgb = rgb(base)
    tint = rgb(color)
    for i in range(rings, 0, -1):
        # 该层的 alpha：从中心的 alpha 线性衰减到 0
        a = alpha * (1.0 - i / rings) ** 1.4
        if a <= 0.003:
            continue
        frac = i / rings
        canvas.create_oval(
            cx - rx * frac, cy - ry * frac,
            cx + rx * frac, cy + ry * frac,
            outline="", fill=mix_rgb(tint, base, a),
        )


def grid_texture(canvas: tk.Canvas, width: int, height: int, step: int,
                 line_color: str, base: str, alpha: float,
                 mask_cx: float = 0.5, mask_cy: float = 0.5,
                 mask_rx: float = 0.75, mask_ry: float = 0.75) -> None:
    """画网格纹理，并用径向遮罩让边缘淡出。

    对应 CSS：
        background-image: linear-gradient(rgba(148,163,184,.05) 1px, transparent 1px),
                          linear-gradient(90deg, rgba(148,163,184,.05) 1px, transparent 1px);
        background-size: 48px 48px;
        mask-image: radial-gradient(ellipse at 50% 40%, rgba(0,0,0,.9), transparent 75%)

    Tk 没法真正做遮罩，这里用「离遮罩中心越远，线色越接近底色」近似。
    """
    tint = rgb(line_color)
    cx, cy = width * mask_cx, height * mask_cy
    rx = max(1.0, width * mask_rx)
    ry = max(1.0, height * mask_ry)

    def line_color_at(dx: float, dy: float) -> str:
        dist = (dx * dx + dy * dy) ** 0.5
        if dist >= 1.0:
            return base
        strength = (1.0 - dist) ** 1.2
        return mix_rgb(tint, base, alpha * strength)

    for x in range(0, width + step, step):
        canvas.create_line(x, 0, x, height,
                           fill=line_color_at((x - cx) / rx, 0.0))
    for y in range(0, height + step, step):
        canvas.create_line(0, y, width, y,
                           fill=line_color_at(0.0, (y - cy) / ry))
