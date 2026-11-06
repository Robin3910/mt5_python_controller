"""后台账户与 2FA 登录页；不载入节点列表，也不接管节点。

结构对齐参考稿 `UI/控制端UI.html` 外链 CSS 的 `.login-root`：

    .login-root       整屏铺满，垂直水平居中
    .login-bg-grid    48px 网格纹理 + 椭圆径向遮罩（Canvas 手绘）
    .login-bg-chart   右下角 640×300 走势图（Canvas 手绘，透明度 .7）
    .login-frame      最大宽度 420，垂直排列，gap 20
    .login-card       圆角 10 / 描边 --line / padding 32 30 26 / 渐变底
    .login-brand      52px 圆形 logo（金色渐变环）+ 标题 + 副标题
    .login-divider    1px 分隔线，margin 22 0
    .login-field      label(12px, --dim) + input-wrap(40px 输入框 + 左内嵌图标)
    .input-field      高 40 / 圆角 6 / 底 #0a0e17b3 / 聚焦时金色描边
    .btn-login        高 42 / 圆角 6 / 金色渐变底 / 字号 15 / 字距 .3em
    .login-hint       居中提示，11.5px
    .login-footer     11px 页脚

Tk 没有 CSS 渐变/遮罩，背景质感靠 `canvas_fx` 手绘逼近。
"""
from __future__ import annotations

import threading
import tkinter as tk
from pathlib import Path

import customtkinter as ctk

import canvas_fx
from assets_path import logo_path
from auth_service import DashboardAPI, DashboardAuthError
from store import load_app_url, load_panel_config, save_panel_config
from theme import (ACCENT, BG, DANGER, DIM, FS_INPUT, GOLD, GOLD_STRONG, H_INPUT,
                   LINE, MUTE, RADIUS_INPUT, RADIUS_PANEL, TEXT, TEXT_MUTED, W_LOGO_LOGIN,
                   divider, font, mono)

# —— 背景网格（.login-bg-grid）——
_GRID_STEP = 48
_GRID_LINE = "#94a3b8"
_GRID_ALPHA = 0.05
_MASK_CX = 0.50
_MASK_CY = 0.40
_MASK_RX = 0.75
_MASK_RY = 0.75

# —— 走势图（.login-bg-chart）——
# 参考稿的 SVG 数据：viewBox 0 0 640 300
_CHART_VIEW_W = 640
_CHART_VIEW_H = 300
_CHART_POINTS = (
    (0, 240), (70, 210), (140, 228), (210, 168), (280, 190),
    (350, 130), (420, 160), (490, 92), (560, 120), (640, 70),
)
# .login-bg-chart {position:absolute;right:-60px;bottom:-40px;width:min(640px,70vw);height:300px;opacity:.7}
_CHART_OFFSET_X = -60
_CHART_OFFSET_Y = -40
_CHART_OPACITY = 0.7

# logo 图片（参考稿用同一张 logo，圆形裁切）


class LoginView(ctk.CTkFrame):
    """登录页。整屏铺满，背景是 Canvas 手绘的网格 + 走势图。"""

    def __init__(self, master, on_authenticated, reason: str = ""):
        super().__init__(master, fg_color=BG)
        self._on_authenticated = on_authenticated
        self._busy = False
        self._pending_token = ""
        self._attempt = 0
        config = load_panel_config()
        self.username = tk.StringVar(value=str(config.get("last_username") or ""))
        self.password = tk.StringVar()
        self.code = tk.StringVar()

        # 父容器用 grid 管理主/登录页的切换，这里必须自己撑满整格
        self.grid(row=0, column=0, rowspan=2, sticky="nsew")

        # —— 背景层：网格 + 走势图（先创建，才在最下面）——
        self.bg_canvas = ctk.CTkCanvas(self, bg=BG, highlightthickness=0, bd=0)
        self.bg_canvas.place(x=0, y=0, relwidth=1, relheight=1)
        self.bg_canvas._on_restore_redraw = self._redraw_background
        self.bg_canvas.bind("<Configure>", self._redraw_background)
        self._drawn_size: tuple[int, int] = (0, 0)

        # —— 内容层：.login-frame（最大宽度 420）——
        # 注意：这里不能用 pack_propagate(False)——它会把高度锁死成默认值，
        # 导致卡片内的输入框被压缩成 0 高度（看不见）。宽度固定、高度自适应。
        frame = ctk.CTkFrame(self, fg_color="transparent")
        frame.place(relx=0.5, rely=0.5, anchor="center")
        frame.configure(width=420)
        frame.pack_propagate(True)

        self._build_card(frame, reason)
    # ---------------------------------------------------------------- 构建

    def _build_card(self, frame: ctk.CTkFrame, reason: str = "") -> None:
        """构建 .login-card 及其内部结构。"""
        # .login-card {background:linear-gradient(180deg,#151f31eb,#0d131ff5);
        #   border:1px solid var(--line);border-radius:10px;padding:32px 30px 26px}
        card = ctk.CTkFrame(
            frame, fg_color="#131a29", border_width=1, border_color=LINE,
            corner_radius=RADIUS_PANEL,
        )
        card.pack(fill="both", expand=True)
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=30, pady=(32, 26))

        # —— .login-brand：52px 圆形 logo + 标题 + 副标题 ——
        brand = ctk.CTkFrame(inner, fg_color="transparent")
        brand.pack(fill="x")

        # .login-logo {width:52px;height:52px;border-radius:50%;padding:3px;
        #   background:linear-gradient(135deg,var(--gold-strong),var(--gold))}
        logo_outer = ctk.CTkFrame(
            brand, width=W_LOGO_LOGIN, height=W_LOGO_LOGIN, corner_radius=W_LOGO_LOGIN // 2,
            fg_color=GOLD, border_width=0,
        )
        logo_outer.pack(side="left", padx=(0, 14))
        logo_outer.pack_propagate(False)
        logo_inner = ctk.CTkFrame(
            logo_outer, fg_color="#0b0f1a", corner_radius=W_LOGO_LOGIN // 2 - 2,
            border_width=0,
        )
        logo_inner.pack(fill="both", expand=True, padx=3, pady=3)
        logo_inner.pack_propagate(False)
        self._logo_label = ctk.CTkLabel(logo_inner, text="")
        self._logo_label.pack(fill="both", expand=True)
        self._load_logo()

        title_box = ctk.CTkFrame(brand, fg_color="transparent")
        title_box.pack(side="left", fill="x", expand=True)
        # .login-title {font-size:21px;font-weight:700;letter-spacing:.06em}
        ctk.CTkLabel(
            title_box, text="亿点点交易", text_color=TEXT, font=font(21, "bold"),
            anchor="w", height=30,
        ).pack(anchor="w")
        # .login-subtitle {font-size:11px;letter-spacing:.12em;font-family:var(--mono)}
        ctk.CTkLabel(
            title_box, text="亿点点交易终端 · YD TRADING TERMINAL",
            text_color=MUTE, font=mono(11), anchor="w", height=16,
        ).pack(anchor="w")

        # —— .login-divider ——
        divider(inner).pack(fill="x", pady=22)

        # —— 后端地址 ——
        self._base_label = ctk.CTkLabel(
            inner, text=self._backend_text(), text_color=TEXT, anchor="w",
            wraplength=340, justify="left", font=mono(11),
        )
        self._base_label.pack(fill="x", pady=(0, 14))

        # —— 三个输入字段 ——
        for label, variable, secret in (
            ("账户", self.username, False),
            ("密码", self.password, True),
            ("谷歌验证码", self.code, False),
        ):
            self._build_field(inner, label, variable, secret)

        # —— 状态 / 错误提示 ——
        missing = not load_app_url()
        hint = "未配置后端地址：请在程序目录的 .env 中设置 APP_URL" if missing else "请输入账户名和密码"
        self.status = ctk.CTkLabel(
            inner, text=reason or hint,
            text_color=DANGER if missing and not reason else TEXT_MUTED,
            wraplength=340, justify="left", anchor="w",
        )
        self.status.pack(fill="x", pady=(2, 14))

        # —— .btn-login {height:42px;border-radius:6px;金色渐变底;font-size:15px;
        #   font-weight:700;letter-spacing:.3em} ——
        self.button = ctk.CTkButton(
            inner, text="登 录", command=self._login, width=360, height=42,
            corner_radius=RADIUS_INPUT, fg_color=GOLD, hover_color=GOLD_STRONG,
            text_color="#101826", font=font(15, "bold"), border_width=0,
        )
        self.button.pack(fill="x")

        # —— .login-hint ——
        ctk.CTkLabel(
            inner, text="本地演示环境 · 账户 admin / 密码 admin / 验证码 123456",
            text_color=MUTE, font=font(11), anchor="center",
        ).pack(fill="x", pady=(18, 0))

        # —— .login-footer ——
        ctk.CTkLabel(
            frame, text="亿点点交易 © 2026 · v1.2.0 · 仅供演示使用",
            text_color=MUTE, font=font(11), anchor="center",
        ).pack(pady=(18, 0))

    def _build_field(self, master, label: str, variable, secret: bool) -> None:
        """一个 .login-field：标签 + 左侧内嵌图标的 40px 输入框。"""
        # .login-field {margin-bottom:16px}
        # .login-field label {font-size:12px;color:var(--dim);margin-bottom:7px}
        ctk.CTkLabel(
            master, text=label, text_color=DIM, font=font(12), anchor="w", height=16,
        ).pack(fill="x", pady=(0, 7))

        # .input-wrap {position:relative;display:flex;align-items:center}
        wrap = ctk.CTkFrame(master, fg_color="transparent", height=H_INPUT)
        wrap.pack(fill="x")
        wrap.pack_propagate(False)

        # .input-icon {position:absolute;left:12px;color:var(--mute)}
        icon = ctk.CTkLabel(
            wrap, text="●", text_color=MUTE, font=font(11), width=16, height=16,
        )
        icon.place(x=12, rely=0.5, anchor="w")

        # .input-field {height:40px;border-radius:6px;background:#0a0e17b3;
        #   border:1px solid var(--line);padding-left:36px}
        entry = ctk.CTkEntry(
            wrap, textvariable=variable, show="*" if secret else "",
            height=H_INPUT, fg_color="#0b1119", text_color=TEXT,
            border_color=LINE, border_width=1, corner_radius=RADIUS_INPUT,
            font=font(FS_INPUT),
        )
        entry.pack(side="left", fill="both", expand=True, padx=(32, 0))
        entry.bind("<Return>", lambda _event: self._login())

        if secret:
            self._password_entry = entry
        elif label == "账户":
            self._account_entry = entry
        else:
            self._code_entry = entry

    def _load_logo(self) -> None:
        """把 logo 裁成圆形贴到 .login-logo 里（参考稿是圆形裁切）。"""
        path = logo_path()
        if path is None:
            self._logo_label.configure(text="亿", text_color=GOLD, font=font(22, "bold"))
            return
        try:
            from PIL import Image, ImageDraw, ImageOps
            from customtkinter import CTkImage

            size = W_LOGO_LOGIN * 2
            image = Image.open(path).convert("RGBA")
            image = ImageOps.fit(image, (size, size), method=Image.LANCZOS)
            mask = Image.new("L", (size, size), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
            image.putalpha(mask)
            self._logo_image = CTkImage(light_image=image, dark_image=image, size=(W_LOGO_LOGIN, W_LOGO_LOGIN))
            self._logo_label.configure(image=self._logo_image, text="")
        except Exception:  # noqa: BLE001
            self._logo_label.configure(text="亿", text_color=GOLD, font=font(22, "bold"))

    # ---------------------------------------------------------------- 背景

    def _redraw_background(self, _event=None) -> None:
        """重画背景：48px 网格纹理 + 右下角走势图。

        最小化恢复后 CTkCanvas 内容会残影，app_ui 的残影修复流程会回调
        `_on_restore_redraw` 重画这里。
        """
        width = self.bg_canvas.winfo_width()
        height = self.bg_canvas.winfo_height()
        if width <= 1 or height <= 1:
            return
        if self._drawn_size == (width, height) and self.bg_canvas.find_all():
            return
        self._drawn_size = (width, height)

        self.bg_canvas.delete("all")

        # —— .login-bg-grid：48px 网格 + 椭圆径向遮罩 ——
        canvas_fx.grid_texture(
            self.bg_canvas, width, height, _GRID_STEP,
            line_color=_GRID_LINE, base=BG, alpha=_GRID_ALPHA,
            mask_cx=_MASK_CX, mask_cy=_MASK_CY,
            mask_rx=_MASK_RX, mask_ry=_MASK_RY,
        )

        # —— .login-bg-chart：右下角的走势图 ——
        self._paint_chart(width, height)

    def _paint_chart(self, width: int, height: int) -> None:
        """画参考稿的走势图。

        SVG 的 `viewBox="0 0 640 300"` + `preserveAspectRatio="none"`，
        画布尺寸是 `min(640px, 70vw)` × 300，定位在 `right:-60px; bottom:-40px`。
        """
        chart_w = min(640, int(width * 0.70))
        chart_h = 300
        # 定位：右下角外移
        origin_x = width - chart_w + _CHART_OFFSET_X
        origin_y = height - chart_h + _CHART_OFFSET_Y

        def to_canvas(point: tuple[float, float]) -> tuple[float, float]:
            """SVG 视口坐标 -> 画布坐标（等比铺满，无保持宽高比）。"""
            x, y = point
            return (origin_x + x / _CHART_VIEW_W * chart_w,
                    origin_y + y / _CHART_VIEW_H * chart_h)

        points = [to_canvas(p) for p in _CHART_POINTS]
        flat: list[float] = []
        for px, py in points:
            flat.extend((px, py))

        # 面积填充：polygon 的竖直渐变（顶 #d9b36c@.18 -> 底透明）
        # Tk 没有渐变填充，用「一层层向下淡出的水平细条」近似。
        self._paint_chart_fill(points, origin_x, origin_y, chart_w, chart_h)

        # 折线：stroke #d9b36c, opacity .5, width 1.5
        line_color = canvas_fx.mix("#d9b36c", _CHART_OPACITY * 0.5, BG)
        self.bg_canvas.create_line(
            flat, fill=line_color, width=2, smooth=False, capstyle="round",
        )

    def _paint_chart_fill(self, points, origin_x, origin_y, chart_w, chart_h) -> None:
        """画走势图下方的竖直渐变填充。"""
        top_y = min(p[1] for p in points)
        bottom_y = origin_y + chart_h
        steps = 24
        for i in range(steps):
            t0 = i / steps
            t1 = (i + 1) / steps
            y0 = top_y + (bottom_y - top_y) * t0
            y1 = top_y + (bottom_y - top_y) * t1
            # alpha 从顶部的 .18 线性衰减到 0
            alpha = 0.18 * (1.0 - t0) * _CHART_OPACITY
            if alpha <= 0.004:
                continue
            fill = canvas_fx.mix("#d9b36c", alpha, BG)
            # 该横条与折线交点的左右边界
            left, right = self._span_at(points, (y0 + y1) / 2, origin_x, chart_w)
            if right <= left:
                continue
            self.bg_canvas.create_rectangle(left, y0, right, y1, fill=fill, outline="")

    @staticmethod
    def _span_at(points, y: float, origin_x: float, chart_w: float) -> tuple[float, float]:
        """求折线在给定高度上的左右交点（用于裁出填充区域）。"""
        xs: list[float] = []
        for (x0, y0), (x1, y1) in zip(points, points[1:]):
            if (y0 - y) * (y1 - y) <= 0 and y0 != y1:
                t = (y - y0) / (y1 - y0)
                xs.append(x0 + (x1 - x0) * t)
        if not xs:
            return origin_x, origin_x + chart_w
        return min(xs), max(xs)

    # ---------------------------------------------------------------- 登录

    def _backend_text(self) -> str:
        return load_app_url() or "未配置"

    def _login(self):
        if self._busy:
            return
        configured = load_app_url()
        self._base_label.configure(text=configured or "未配置", text_color=TEXT if configured else DANGER)
        if not configured:
            self.status.configure(text="未配置后端地址：请在程序目录的 .env 中设置 APP_URL", text_color=DANGER)
            return
        try:
            api = DashboardAPI(configured)
        except DashboardAuthError as exc:
            self.status.configure(text=str(exc), text_color=DANGER)
            return
        username, password, code = self.username.get().strip(), self.password.get(), self.code.get().strip()
        pending = self._pending_token
        generation = self.master.session.generation
        self.password.set("")
        self.code.set("")
        self._busy = True
        self._attempt += 1
        attempt = self._attempt
        self.button.configure(state="disabled")
        self.status.configure(text="正在验证…", text_color=TEXT_MUTED)
        def work():
            try:
                result = api.login_2fa(pending, code) if pending else api.login(username, password)
                if result.get("requires_2fa"):
                    def second_step():
                        self._pending_token = str(result["login_token"])
                        self.status.configure(text="请输入 2FA 验证码后再次点击登录", text_color=ACCENT)
                    schedule(second_step)
                else:
                    token = str(result["token"])
                    principal = api.me(token)
                    nodes = api.nodes(token)
                    def done():
                        try:
                            self.master.session.accept_login(api, token, principal, nodes, expected_generation=generation)
                        except DashboardAuthError as exc:
                            self.status.configure(text=str(exc), text_color=DANGER)
                            self._busy = False
                            self.button.configure(state="normal")
                            return
                        self._pending_token = ""
                        save_panel_config({"backend_base": api.base, "last_username": username})
                        self._on_authenticated()
                    schedule(done)
            except (DashboardAuthError, KeyError, ValueError) as exc:
                error = str(exc) if isinstance(exc, DashboardAuthError) else "登录响应无效"
                def failed():
                    self._pending_token = ""
                    self.status.configure(text=error, text_color=DANGER)
                schedule(failed)
            finally:
                def idle():
                    self._busy = False
                    self.button.configure(state="normal")
                schedule(idle)
        def schedule(callback):
            def current():
                if not self.winfo_exists() or self._attempt != attempt:
                    return
                # 登录是一个新交互；Tk 模态重入期间不能继承旧动作的线程局部世代。
                previous = getattr(self.master.session._local, "generation", None)
                self.master.session._local.generation = None
                try:
                    callback()
                finally:
                    self.master.session._local.generation = previous
            try:
                self.after(0, current)
            except (tk.TclError, RuntimeError):
                pass
        threading.Thread(target=work, name="dashboard-login", daemon=True).start()

    def destroy(self):
        self._attempt += 1
        self._pending_token = ""
        self.password.set("")
        self.code.set("")
        super().destroy()
