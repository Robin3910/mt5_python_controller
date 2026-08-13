"""后端连接配置对话框：API 地址与节点令牌。

配置写入面板数据目录的 `panel_config.json`，供版本更新等需要访问后端的功能使用。
两项都留空时会自动从各实例工作目录下的 node_client `.env` 推导
（`MANAGER_WS_URL` → HTTP 基址，`NODE_TOKEN` → 令牌），因此新装机器上只要实例
配好了 .env 就不必在这里填任何东西。
"""
from __future__ import annotations

import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox

import customtkinter as ctk

import version_service as vs
from dialog_window import hide_while_building, show_centered
from process_manager import ProcessManager
from store import load_panel_config, panel_config_path, save_panel_config
from theme import (
    ACCENT,
    BG,
    BG_ELEVATED,
    DANGER,
    GLASS_BORDER,
    TEXT,
    TEXT_DIM,
    TEXT_MUTED,
    WARNING,
    font,
    ghost_btn,
    glass_card,
    mono,
    primary_btn,
)


class ConnectionConfigDialog(ctk.CTkToplevel):
    """配置后端 API 地址与 NODE_TOKEN，并可当场测试连通性。"""

    _W = 660
    _H = 520

    def __init__(self, master, manager: ProcessManager, on_saved=None) -> None:
        super().__init__(master)
        self.title("后端连接配置")
        self.configure(fg_color=BG)
        self.transient(master)
        self.resizable(False, False)
        hide_while_building(self)

        self.manager = manager
        self._on_saved = on_saved
        self._testing = False

        cfg = load_panel_config()
        self.base_var = tk.StringVar(value=str(cfg.get("backend_base") or ""))
        self.token_var = tk.StringVar(value=str(cfg.get("node_token") or ""))
        self.show_token = tk.BooleanVar(value=False)

        self._build()
        self._render_discovered()
        self._autofill_from_discovered()
        self.bind("<Return>", lambda _e: self._save())
        self.bind("<Escape>", lambda _e: self.destroy())
        self.after(20, self._show_centered)

    # ---------------------------------------------------------------- 布局

    def _build(self) -> None:
        card = glass_card(self)
        card.pack(fill="both", expand=True, padx=16, pady=16)
        card.grid_columnconfigure(1, weight=1)
        card.grid_rowconfigure(7, weight=1)

        ctk.CTkLabel(
            card, text="后端连接", text_color=ACCENT, font=font(11, "bold")
        ).grid(row=0, column=0, columnspan=3, padx=16, pady=(16, 2), sticky="w")
        ctk.CTkLabel(
            card, text="API 地址与节点令牌", text_color=TEXT, font=font(18, "bold")
        ).grid(row=1, column=0, columnspan=3, padx=16, pady=(0, 4), sticky="w")
        ctk.CTkLabel(
            card,
            text="用于向后端查询客户端发布版本并下载安装包；两项留空时自动读取实例 .env。",
            text_color=TEXT_MUTED,
            font=font(12),
            anchor="w",
            justify="left",
            wraplength=600,
        ).grid(row=2, column=0, columnspan=3, padx=16, pady=(0, 10), sticky="ew")

        ctk.CTkLabel(card, text="API 地址", text_color=TEXT_MUTED, font=font(12)).grid(
            row=3, column=0, padx=16, pady=8, sticky="w"
        )
        ctk.CTkEntry(
            card, textvariable=self.base_var, fg_color=BG_ELEVATED,
            border_color=GLASS_BORDER, text_color=TEXT, height=36, corner_radius=10,
            placeholder_text="http://159.75.33.185　或　https://hub.example.com",
        ).grid(row=3, column=1, columnspan=2, padx=(8, 16), pady=8, sticky="ew")

        ctk.CTkLabel(card, text="节点令牌", text_color=TEXT_MUTED, font=font(12)).grid(
            row=4, column=0, padx=16, pady=8, sticky="w"
        )
        self.token_entry = ctk.CTkEntry(
            card, textvariable=self.token_var, fg_color=BG_ELEVATED,
            border_color=GLASS_BORDER, text_color=TEXT, height=36, corner_radius=10,
            show="*", placeholder_text="NODE_TOKEN（后台「配置 → 节点令牌」可复制）",
        )
        self.token_entry.grid(row=4, column=1, padx=(8, 4), pady=8, sticky="ew")
        ctk.CTkCheckBox(
            card, text="显示", variable=self.show_token, command=self._toggle_token,
            width=60, checkbox_width=18, checkbox_height=18,
            fg_color=ACCENT, hover_color=ACCENT, border_color=GLASS_BORDER,
            text_color=TEXT_MUTED, font=font(12),
        ).grid(row=4, column=2, padx=(4, 16), pady=8, sticky="w")

        # 自动发现结果：让用户看清留空时会用到什么，以及来源是哪个实例
        self.discovered = ctk.CTkLabel(
            card, text="", text_color=TEXT_DIM, font=mono(11),
            anchor="w", justify="left", wraplength=600,
        )
        self.discovered.grid(row=5, column=0, columnspan=3, padx=16, pady=(4, 4), sticky="ew")

        self.status = ctk.CTkLabel(
            card, text="", text_color=TEXT_DIM, font=font(12),
            anchor="w", justify="left", wraplength=600,
        )
        self.status.grid(row=6, column=0, columnspan=3, padx=16, pady=(4, 4), sticky="ew")

        ctk.CTkLabel(
            card,
            text=f"配置文件：{panel_config_path()}",
            text_color=TEXT_DIM, font=mono(11), anchor="w", wraplength=600,
        ).grid(row=7, column=0, columnspan=3, padx=16, pady=(4, 8), sticky="new")

        btns = ctk.CTkFrame(card, fg_color="transparent")
        btns.grid(row=8, column=0, columnspan=3, sticky="s", pady=(4, 18))
        primary_btn(btns, "保存", self._save, width=110).pack(side="left", padx=6)
        ghost_btn(btns, "测试连接", self._test, width=110).pack(side="left", padx=6)
        ghost_btn(btns, "清空", self._clear, width=90).pack(side="left", padx=6)
        ghost_btn(btns, "取消", self.destroy, width=90).pack(side="left", padx=6)

    def _show_centered(self) -> None:
        show_centered(self, self._W, self._H)

    # ---------------------------------------------------------------- 数据

    def _toggle_token(self) -> None:
        self.token_entry.configure(show="" if self.show_token.get() else "*")

    def _cwds(self) -> list[str]:
        return [c.cwd or str(Path(c.exe_path).parent) for c in self.manager.configs()]

    def _target(self) -> vs.BackendTarget:
        return vs.resolve_backend(
            {
                "backend_base": self.base_var.get().strip(),
                "node_token": self.token_var.get().strip(),
            },
            self._cwds(),
        )

    def _render_discovered(self) -> None:
        """展示从实例 .env 自动发现到的地址与令牌状态。"""
        lines: list[str] = []
        for cfg in self.manager.configs():
            cwd = cfg.cwd or str(Path(cfg.exe_path).parent)
            found = vs.target_from_env(cwd)
            base = found.base or "（未找到 MANAGER_WS_URL）"
            token = "已配置" if found.token else "（未找到 NODE_TOKEN）"
            lines.append(f"· {cfg.name}：{base}　令牌 {token}")
        if not lines:
            lines.append("尚无实例，无法自动发现；请手工填写上面两项。")
        self.discovered.configure(text="实例 .env 自动发现：\n" + "\n".join(lines))

    def _autofill_from_discovered(self) -> None:
        """手工配置为空时，把自动发现的值回显进输入框。

        只在打开时回显一次：让用户直接看到当前实际生效的连接，不必自己从下方的
        发现列表里抄一遍。「清空」之后不会再回填，否则清空按钮就失效了。
        """
        found = vs.resolve_backend({}, self._cwds())
        filled: list[str] = []
        if not self.base_var.get().strip() and found.base:
            self.base_var.set(found.base)
            filled.append("API 地址")
        if not self.token_var.get().strip() and found.token:
            self.token_var.set(found.token)
            filled.append("节点令牌")
        if not filled:
            return
        self.status.configure(
            text=f"已回显实例 .env 中的{'与'.join(filled)}。"
                 "点「保存」固化为面板配置；想继续跟随 .env 变化则「清空」后再保存。",
            text_color=TEXT_MUTED,
        )

    def _clear(self) -> None:
        self.base_var.set("")
        self.token_var.set("")
        self.status.configure(
            text="已清空手工配置；保存后将回落到实例 .env 自动发现", text_color=TEXT_MUTED
        )

    def _save(self) -> None:
        save_panel_config({
            "backend_base": self.base_var.get().strip().rstrip("/"),
            "node_token": self.token_var.get().strip(),
        })
        self.status.configure(text="已保存", text_color=ACCENT)
        if self._on_saved:
            self._on_saved()
        self.after(400, self.destroy)

    def _test(self) -> None:
        """向后端要一次当前发布版本，验证地址与令牌是否可用。"""
        if self._testing:
            return
        target = self._target()  # Tcl 非线程安全，先在主线程取好
        if not target.ready:
            self.status.configure(
                text="缺少 API 地址或节点令牌：请手工填写，或确保实例 .env 里有 "
                     "MANAGER_WS_URL 与 NODE_TOKEN",
                text_color=DANGER,
            )
            return

        self._testing = True
        self.status.configure(text=f"正在连接 {target.base} …", text_color=TEXT_MUTED)

        def worker():
            try:
                data = vs.fetch_current_version(target)
            except vs.VersionServiceError as e:
                self.after(0, lambda: self.status.configure(text=str(e), text_color=DANGER))
            except Exception as e:  # noqa: BLE001
                self.after(0, lambda: messagebox.showerror("测试连接", str(e), parent=self))
            else:
                def done():
                    if data:
                        self.status.configure(
                            text=f"连接成功（{target.source}）：后端发布版本 v{data.get('version')}",
                            text_color=ACCENT,
                        )
                    else:
                        # 能连通、令牌也对，只是还没发布过任何版本
                        self.status.configure(
                            text=f"连接成功（{target.source}），但后端尚未发布任何客户端版本",
                            text_color=WARNING,
                        )

                self.after(0, done)
            finally:
                self.after(0, lambda: setattr(self, "_testing", False))

        threading.Thread(target=worker, name="conn-test", daemon=True).start()
