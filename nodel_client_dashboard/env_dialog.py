"""节点配置对话框：在面板里编辑实例的 node_client `.env`。

两种模式：
- 常用字段表单 —— 覆盖后端地址、令牌、心跳、观察列表等日常要改的项，带类型校验；
- 原始文本 —— 直接改整份文件，应付表单没覆盖到的键。

保存走 `env_file.update_env_text` 就地合并，注释与自定义键都会留着。改完必须重启
实例才生效：node_client 的配置在进程内是 `@lru_cache` 冻结的。
"""
from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import messagebox

import customtkinter as ctk

import env_file as ef
from dialog_window import hide_while_building, show_centered
from models import InstanceConfig
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


class EnvConfigDialog(ctk.CTkToplevel):
    """编辑某个实例的 .env。保存成功后 `saved` 为 True。"""

    _W = 780
    _H = 700

    def __init__(self, master, cfg: InstanceConfig) -> None:
        super().__init__(master)
        self.title(f"节点配置 · {cfg.name}")
        self.configure(fg_color=BG)
        self.transient(master)
        self.resizable(False, False)
        hide_while_building(self)

        self.cfg = cfg
        self.saved = False
        self._cwd = cfg.cwd or str(Path(cfg.exe_path).parent)
        self._raw_text, values = ef.read_env(self._cwd)
        self._existed = bool(self._raw_text)

        self._vars: dict[str, tk.StringVar] = {}
        self._bools: dict[str, tk.BooleanVar] = {}
        for field in ef.ENV_FIELDS:
            raw = values.get(field.key, "")
            if field.kind == "bool":
                self._bools[field.key] = tk.BooleanVar(value=ef.as_bool(raw))
            else:
                self._vars[field.key] = tk.StringVar(value=raw)
        self.show_secret = tk.BooleanVar(value=False)
        self._secret_entries: list[ctk.CTkEntry] = []

        self._build()
        self.bind("<Escape>", lambda _e: self.destroy())
        self.after(20, self._show_centered)

    # ---------------------------------------------------------------- 布局

    def _build(self) -> None:
        card = glass_card(self)
        card.pack(fill="both", expand=True, padx=16, pady=16)
        card.grid_columnconfigure(0, weight=1)
        card.grid_rowconfigure(3, weight=1)

        head = ctk.CTkFrame(card, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 2))
        ctk.CTkLabel(
            head, text="节点配置", text_color=ACCENT, font=font(11, "bold")
        ).pack(anchor="w")
        ctk.CTkLabel(
            head, text=f"{self.cfg.name} · .env", text_color=TEXT, font=font(18, "bold")
        ).pack(anchor="w")
        ctk.CTkLabel(
            head,
            text=str(ef.env_path(self._cwd))
            + ("" if self._existed else "　（文件不存在，保存后会创建）"),
            text_color=TEXT_DIM, font=mono(11), anchor="w", wraplength=700, justify="left",
        ).pack(anchor="w", pady=(4, 0))

        tabs = ctk.CTkTabview(
            card, fg_color=BG_ELEVATED, segmented_button_fg_color=BG_ELEVATED,
            segmented_button_selected_color=ACCENT,
            segmented_button_selected_hover_color=ACCENT,
            segmented_button_unselected_color=BG_ELEVATED,
            text_color=TEXT, border_color=GLASS_BORDER, border_width=1, corner_radius=12,
        )
        tabs.grid(row=3, column=0, sticky="nsew", padx=12, pady=(10, 6))
        form_tab = tabs.add("常用字段")
        raw_tab = tabs.add("原始文本")
        self._build_form(form_tab)
        self._build_raw(raw_tab)

        self.status = ctk.CTkLabel(
            card, text="", text_color=TEXT_DIM, font=font(12),
            anchor="w", justify="left", wraplength=700,
        )
        self.status.grid(row=4, column=0, sticky="ew", padx=16, pady=(2, 2))

        ctk.CTkLabel(
            card,
            text="改动需重启实例才生效（节点配置在进程内一次性加载）。",
            text_color=WARNING, font=font(11), anchor="w",
        ).grid(row=5, column=0, sticky="ew", padx=16, pady=(0, 4))

        btns = ctk.CTkFrame(card, fg_color="transparent")
        btns.grid(row=6, column=0, pady=(4, 16))
        primary_btn(btns, "保存", self._save, width=110).pack(side="left", padx=6)
        ghost_btn(btns, "重新载入", self._reload, width=110).pack(side="left", padx=6)
        ghost_btn(btns, "取消", self.destroy, width=90).pack(side="left", padx=6)

    def _build_form(self, master) -> None:
        scroll = ctk.CTkScrollableFrame(
            master, fg_color="transparent", scrollbar_button_color=GLASS_BORDER,
            scrollbar_button_hover_color=ACCENT,
        )
        scroll.pack(fill="both", expand=True, padx=4, pady=4)
        scroll.grid_columnconfigure(1, weight=1)

        row = 0
        for field in ef.ENV_FIELDS:
            ctk.CTkLabel(
                scroll, text=field.label, text_color=TEXT_MUTED, font=font(12), anchor="w"
            ).grid(row=row, column=0, padx=(8, 6), pady=6, sticky="w")

            if field.kind == "bool":
                ctk.CTkSwitch(
                    scroll, text="", variable=self._bools[field.key], width=48,
                    progress_color=ACCENT, button_color=TEXT, button_hover_color=ACCENT,
                ).grid(row=row, column=1, padx=8, pady=6, sticky="w")
            elif field.kind == "choice":
                ctk.CTkOptionMenu(
                    scroll, variable=self._vars[field.key], values=list(field.choices),
                    fg_color=BG_ELEVATED, button_color=GLASS_BORDER,
                    button_hover_color=ACCENT, text_color=TEXT, width=160,
                ).grid(row=row, column=1, padx=8, pady=6, sticky="w")
            else:
                entry = ctk.CTkEntry(
                    scroll, textvariable=self._vars[field.key], fg_color=BG_ELEVATED,
                    border_color=GLASS_BORDER, text_color=TEXT, height=34, corner_radius=10,
                    show="*" if field.secret else "",
                )
                entry.grid(row=row, column=1, padx=8, pady=6, sticky="ew")
                if field.secret:
                    self._secret_entries.append(entry)

            if field.help:
                row += 1
                ctk.CTkLabel(
                    scroll, text=field.help, text_color=TEXT_DIM, font=font(11),
                    anchor="w", wraplength=520, justify="left",
                ).grid(row=row, column=1, padx=8, pady=(0, 4), sticky="ew")
            row += 1

        if self._secret_entries:
            ctk.CTkCheckBox(
                scroll, text="显示令牌", variable=self.show_secret,
                command=self._toggle_secret, checkbox_width=18, checkbox_height=18,
                fg_color=ACCENT, hover_color=ACCENT, border_color=GLASS_BORDER,
                text_color=TEXT_MUTED, font=font(12),
            ).grid(row=row, column=1, padx=8, pady=(6, 10), sticky="w")

    def _build_raw(self, master) -> None:
        ctk.CTkLabel(
            master,
            text="直接编辑整份 .env；保存时以这里的内容为准，表单改动会被忽略。",
            text_color=TEXT_DIM, font=font(11), anchor="w",
        ).pack(fill="x", padx=8, pady=(8, 4))
        self.raw_box = ctk.CTkTextbox(
            master, fg_color=BG_ELEVATED, text_color=TEXT, border_color=GLASS_BORDER,
            border_width=1, corner_radius=10, font=mono(12),
        )
        self.raw_box.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.raw_box.insert("1.0", self._raw_text)
        self._raw_baseline = self._raw_text

    def _show_centered(self) -> None:
        show_centered(self, self._W, self._H)

    # ---------------------------------------------------------------- 动作

    def _toggle_secret(self) -> None:
        show = "" if self.show_secret.get() else "*"
        for entry in self._secret_entries:
            entry.configure(show=show)

    def _reload(self) -> None:
        self._raw_text, values = ef.read_env(self._cwd)
        for field in ef.ENV_FIELDS:
            raw = values.get(field.key, "")
            if field.kind == "bool":
                self._bools[field.key].set(ef.as_bool(raw))
            else:
                self._vars[field.key].set(raw)
        self.raw_box.delete("1.0", "end")
        self.raw_box.insert("1.0", self._raw_text)
        self._raw_baseline = self._raw_text
        self.status.configure(text="已从磁盘重新载入", text_color=TEXT_MUTED)

    def _form_values(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for field in ef.ENV_FIELDS:
            if field.kind == "bool":
                out[field.key] = ef.bool_text(self._bools[field.key].get())
            else:
                value = self._vars[field.key].get().strip()
                if field.kind == "choice":
                    value = value.upper()
                # 表单里留空的可选项不写进文件，保留 node_client 的内置默认值
                if value or field.required:
                    out[field.key] = value
        return out

    def _save(self) -> None:
        raw_now = self.raw_box.get("1.0", "end-1c")
        raw_edited = raw_now != self._raw_baseline

        if raw_edited:
            # 原始文本被改过就以它为准：两边都改时无法合并，猜错会丢用户的编辑
            errors = ef.validate_env(ef.parse_env_text(raw_now))
            text = raw_now if raw_now.endswith("\n") else raw_now + "\n"
        else:
            values = self._form_values()
            errors = ef.validate_env(values)
            text = ef.update_env_text(self._raw_text, values)

        if errors:
            self.status.configure(text="；".join(errors), text_color=DANGER)
            return

        try:
            ef.write_env(self._cwd, text)
        except OSError as e:
            messagebox.showerror("保存失败", str(e), parent=self)
            return

        self.saved = True
        self.status.configure(
            text="已保存" + ("（以原始文本为准）" if raw_edited else ""), text_color=ACCENT
        )
        self.after(400, self.destroy)
