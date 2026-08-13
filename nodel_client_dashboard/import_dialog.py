"""导入节点对话框：选 MT5 安装目录 + 选客户端版本，一键部署并加入实例列表。

流程：校验目录里有 MT5 终端 → 从后端拉版本清单（默认选中当前发布版本）→ 下载、
校验 sha256、解压覆盖到该目录 → 生成 `.env`（用面板的连接配置填地址与令牌，已存在
则不动）→ 注册为实例。

客户端必须与 `terminal64.exe` 同目录，否则 node_client 启动时发现不到终端会直接退出，
所以目录校验放在最前面，不合规不让往下走。
"""
from __future__ import annotations

import tempfile
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

import env_file as ef
import version_service as vs
from client_deploy import apply_package, extract_package, read_version_near
from dialog_window import hide_while_building, show_centered
from models import InstanceConfig, default_label_from_path
from process_manager import ProcessManager
from store import load_panel_config
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
    soft_card,
)

_NO_VERSION = "（尚未检测）"


class ImportNodeDialog(ctk.CTkToplevel):
    """把一个 MT5 目录部署成新的节点实例。成功后 `created` 为新实例配置。"""

    _W = 780
    _H = 620

    def __init__(self, master, manager: ProcessManager, on_done=None) -> None:
        super().__init__(master)
        self.title("导入节点")
        self.configure(fg_color=BG)
        self.transient(master)
        self.resizable(False, False)
        hide_while_building(self)

        self.manager = manager
        self._on_done = on_done
        self.created: InstanceConfig | None = None
        self._busy = False
        self._versions: list[dict] = []
        self._current = ""

        self.dir_var = tk.StringVar()
        self.name_var = tk.StringVar()
        self.version_var = tk.StringVar(value=_NO_VERSION)
        self.write_env_var = tk.BooleanVar(value=True)
        self.start_after_var = tk.BooleanVar(value=False)

        self._build()
        self.bind("<Escape>", lambda _e: self.destroy())
        self.after(20, self._show_centered)
        self.after(120, self._detect_versions)

    # ---------------------------------------------------------------- 布局

    def _build(self) -> None:
        card = glass_card(self)
        card.pack(fill="both", expand=True, padx=16, pady=16)
        card.grid_columnconfigure(1, weight=1)
        card.grid_rowconfigure(8, weight=1)

        ctk.CTkLabel(
            card, text="导入节点", text_color=ACCENT, font=font(11, "bold")
        ).grid(row=0, column=0, columnspan=3, padx=16, pady=(16, 2), sticky="w")
        ctk.CTkLabel(
            card, text="部署客户端到 MT5 目录", text_color=TEXT, font=font(18, "bold")
        ).grid(row=1, column=0, columnspan=3, padx=16, pady=(0, 4), sticky="w")
        ctk.CTkLabel(
            card,
            text="选择 MetaTrader 5 安装目录（须含 terminal64.exe），"
                 "从后端下载指定版本的客户端并注册为实例。",
            text_color=TEXT_MUTED, font=font(12), anchor="w",
            justify="left", wraplength=700,
        ).grid(row=2, column=0, columnspan=3, padx=16, pady=(0, 10), sticky="ew")

        ctk.CTkLabel(card, text="MT5 目录", text_color=TEXT_MUTED, font=font(12)).grid(
            row=3, column=0, padx=16, pady=8, sticky="w"
        )
        ctk.CTkEntry(
            card, textvariable=self.dir_var, fg_color=BG_ELEVATED,
            border_color=GLASS_BORDER, text_color=TEXT, height=36, corner_radius=10,
            placeholder_text=r"如 C:\Program Files\MetaTrader 5",
        ).grid(row=3, column=1, padx=(8, 4), pady=8, sticky="ew")
        ghost_btn(card, "浏览…", self._browse, width=76).grid(
            row=3, column=2, padx=(4, 16), pady=8
        )

        ctk.CTkLabel(card, text="实例标签", text_color=TEXT_MUTED, font=font(12)).grid(
            row=4, column=0, padx=16, pady=8, sticky="w"
        )
        ctk.CTkEntry(
            card, textvariable=self.name_var, fg_color=BG_ELEVATED,
            border_color=GLASS_BORDER, text_color=TEXT, height=36, corner_radius=10,
            placeholder_text="留空则取目录名",
        ).grid(row=4, column=1, columnspan=2, padx=(8, 16), pady=8, sticky="ew")

        ctk.CTkLabel(card, text="客户端版本", text_color=TEXT_MUTED, font=font(12)).grid(
            row=5, column=0, padx=16, pady=8, sticky="w"
        )
        version_row = ctk.CTkFrame(card, fg_color="transparent")
        version_row.grid(row=5, column=1, columnspan=2, padx=(8, 16), pady=8, sticky="ew")
        self.version_menu = ctk.CTkOptionMenu(
            version_row, variable=self.version_var, values=[_NO_VERSION],
            command=lambda _v: self._render_version_note(),
            fg_color=BG_ELEVATED, button_color=GLASS_BORDER,
            button_hover_color=ACCENT, text_color=TEXT, width=220,
        )
        self.version_menu.pack(side="left")
        ghost_btn(version_row, "刷新列表", self._detect_versions, width=100).pack(
            side="left", padx=8
        )
        self.version_note = ctk.CTkLabel(
            version_row, text="", text_color=TEXT_DIM, font=font(11), anchor="w"
        )
        self.version_note.pack(side="left", padx=8, fill="x", expand=True)

        opts = soft_card(card)
        opts.grid(row=6, column=0, columnspan=3, padx=16, pady=(8, 4), sticky="ew")
        inner = ctk.CTkFrame(opts, fg_color="transparent")
        inner.pack(fill="x", padx=12, pady=10)
        ctk.CTkCheckBox(
            inner, text="自动写入 .env（用面板的连接配置填后端地址与令牌）",
            variable=self.write_env_var, checkbox_width=18, checkbox_height=18,
            fg_color=ACCENT, hover_color=ACCENT, border_color=GLASS_BORDER,
            text_color=TEXT, font=font(12),
        ).pack(anchor="w")
        ctk.CTkCheckBox(
            inner, text="导入后立即启动", variable=self.start_after_var,
            checkbox_width=18, checkbox_height=18,
            fg_color=ACCENT, hover_color=ACCENT, border_color=GLASS_BORDER,
            text_color=TEXT, font=font(12),
        ).pack(anchor="w", pady=(6, 0))
        ctk.CTkLabel(
            inner,
            text="已存在的 .env 不会被覆盖；MT5 未登录时节点需要交互输入账号，"
                 "首次建议先不自动启动。",
            text_color=TEXT_DIM, font=font(11), anchor="w",
            justify="left", wraplength=680,
        ).pack(anchor="w", pady=(6, 0))

        self.status = ctk.CTkLabel(
            card, text="", text_color=TEXT_DIM, font=font(12),
            anchor="w", justify="left", wraplength=700,
        )
        self.status.grid(row=7, column=0, columnspan=3, padx=16, pady=(6, 4), sticky="ew")

        self.detail = ctk.CTkLabel(
            card, text="", text_color=TEXT_DIM, font=mono(11),
            anchor="nw", justify="left", wraplength=700,
        )
        self.detail.grid(row=8, column=0, columnspan=3, padx=16, pady=(0, 4), sticky="new")

        btns = ctk.CTkFrame(card, fg_color="transparent")
        btns.grid(row=9, column=0, columnspan=3, pady=(4, 16))
        primary_btn(btns, "开始导入", self._import, width=120).pack(side="left", padx=6)
        ghost_btn(btns, "关闭", self.destroy, width=90).pack(side="left", padx=6)

    def _show_centered(self) -> None:
        show_centered(self, self._W, self._H)

    # ---------------------------------------------------------------- 数据

    def _cwds(self) -> list[str]:
        return [c.cwd or str(Path(c.exe_path).parent) for c in self.manager.configs()]

    def _target(self) -> vs.BackendTarget:
        return vs.resolve_backend(load_panel_config(), self._cwds())

    def _browse(self) -> None:
        path = filedialog.askdirectory(
            parent=self, title="选择 MetaTrader 5 安装目录",
            initialdir=self.dir_var.get() or "/",
        )
        if not path:
            return
        self.dir_var.set(path)
        if not self.name_var.get().strip():
            self.name_var.set(Path(path).name)
        self._check_dir()

    def _check_dir(self) -> bool:
        """校验目录，并把发现结果写进状态栏。"""
        raw = self.dir_var.get().strip()
        if not raw:
            self.status.configure(text="请先选择 MT5 安装目录", text_color=DANGER)
            return False
        directory = Path(raw)
        if not directory.is_dir():
            self.status.configure(text=f"目录不存在：{directory}", text_color=DANGER)
            return False
        terminal = ef.find_terminal(directory)
        if terminal is None:
            self.status.configure(
                text=f"该目录没有 {' / '.join(ef.TERMINAL_NAMES)}，不是 MT5 安装目录。"
                     "客户端必须与终端同目录才能连上 MT5。",
                text_color=DANGER,
            )
            return False

        exe_path = directory / "node_client.exe"
        note = f"已识别 MT5 终端：{terminal.name}"
        if exe_path.is_file():
            existing = read_version_near(exe_path)
            note += f"　该目录已有客户端 v{existing or '未知'}（导入会先备份再覆盖）"
        if ef.env_path(directory).is_file():
            note += "　已有 .env（不会被覆盖）"
        self.status.configure(text=note, text_color=TEXT_MUTED)
        return True

    def _detect_versions(self) -> None:
        target = self._target()  # Tcl 非线程安全，先在主线程取好
        if not target.ready:
            self.status.configure(
                text="尚未配置后端地址或节点令牌：请先在面板顶栏「连接配置」里设置",
                text_color=DANGER,
            )
            return
        self._run_async("检测版本", lambda: self._load_versions(target))

    def _load_versions(self, target: vs.BackendTarget) -> None:
        items, current = vs.fetch_available_versions(target)

        def done():
            self._versions = items
            self._current = current
            names = [str(i.get("version") or "") for i in items if i.get("version")]
            if not names:
                self.version_menu.configure(values=[_NO_VERSION])
                self.version_var.set(_NO_VERSION)
                self.status.configure(
                    text="后端还没有任何客户端安装包，请先在管理后台「客户端版本」上传",
                    text_color=WARNING,
                )
                return
            self.version_menu.configure(values=names)
            # 默认选当前发布版本；没发布过就用最新的一个
            self.version_var.set(current if current in names else names[0])
            self.status.configure(
                text=f"已获取 {len(names)} 个可用版本（{target.source}）", text_color=ACCENT
            )
            self._render_version_note()

        self.after(0, done)

    def _selected_version(self) -> dict | None:
        want = self.version_var.get().strip()
        for item in self._versions:
            if str(item.get("version") or "") == want:
                return item
        return None

    def _render_version_note(self) -> None:
        item = self._selected_version()
        if item is None:
            self.version_note.configure(text="")
            self.detail.configure(text="")
            return
        size_mb = int(item.get("size") or 0) / 1024 / 1024
        tag = "　当前发布版本" if item.get("is_current") else ""
        self.version_note.configure(text=f"{size_mb:.1f} MB{tag}")
        notes = str(item.get("notes") or "")
        self.detail.configure(text=f"更新说明：{notes}" if notes else "")

    # ---------------------------------------------------------------- 导入

    def _run_async(self, label: str, fn) -> None:
        if self._busy:
            messagebox.showinfo("请稍候", "上一个操作还在进行中", parent=self)
            return
        self._busy = True

        def worker():
            try:
                fn()
            except vs.VersionServiceError as e:
                self.after(0, lambda: self.status.configure(text=str(e), text_color=DANGER))
            except Exception as e:  # noqa: BLE001
                self.after(0, lambda: messagebox.showerror(label, str(e), parent=self))
            finally:
                self.after(0, lambda: setattr(self, "_busy", False))

        threading.Thread(target=worker, name=f"import-{label}", daemon=True).start()

    def _import(self) -> None:
        if not self._check_dir():
            return
        directory = Path(self.dir_var.get().strip())
        item = self._selected_version()
        if item is None:
            messagebox.showinfo("提示", "请先选择客户端版本（可点「刷新列表」）", parent=self)
            return
        version = str(item.get("version") or "")

        target = self._target()
        if not target.ready:
            self.status.configure(text="尚未配置后端地址或节点令牌", text_color=DANGER)
            return

        exe_path = directory / "node_client.exe"
        dup = next(
            (c for c in self.manager.configs() if Path(c.exe_path) == exe_path), None
        )
        if dup is not None:
            messagebox.showinfo(
                "已存在",
                f"实例「{dup.name}」已经指向该目录。\n\n"
                "如需更新它的客户端版本，请用顶栏「版本更新」。",
                parent=self,
            )
            return

        name = self.name_var.get().strip() or directory.name
        write_env = self.write_env_var.get()
        env_exists = ef.env_path(directory).is_file()
        ws_url = ef.ws_url_from_backend_base(target.base)

        lines = [
            f"将把客户端 v{version} 部署到：",
            f"  {directory}",
            "",
            f"实例标签：{name}",
            f"MT5 终端：{(ef.find_terminal(directory) or Path('?')).name}",
        ]
        if write_env and not env_exists:
            lines += ["", "将自动生成 .env：", f"  MANAGER_WS_URL={ws_url}", "  NODE_TOKEN=（面板连接配置）"]
        elif env_exists:
            lines += ["", "该目录已有 .env，将保持原样不动。"]
        else:
            lines += ["", "不写入 .env —— 导入后需自行配置，否则节点无法接入。"]
        lines += ["", "是否继续？"]
        if not messagebox.askyesno("导入确认", "\n".join(lines), parent=self):
            return

        sha = str(item.get("sha256") or "")
        self._run_async(
            "导入",
            lambda: self._deploy(target, version, sha, directory, name, write_env, ws_url),
        )

    def _deploy(
        self,
        target: vs.BackendTarget,
        version: str,
        sha256: str,
        directory: Path,
        name: str,
        write_env: bool,
        ws_url: str,
    ) -> None:
        """下载 → 校验 → 解压 → 覆盖到 MT5 目录 → 写 .env → 注册实例。"""
        with tempfile.TemporaryDirectory(prefix="node_client_import_") as tmp:
            tmpdir = Path(tmp)
            pkg = tmpdir / f"node_client-{version}.zip"
            self.after(0, lambda: self.status.configure(
                text=f"正在下载 v{version}…", text_color=TEXT_MUTED
            ))
            vs.download_package(target, version, pkg)
            if not vs.verify_sha256(pkg, sha256):
                raise RuntimeError("安装包校验和不匹配，已中止导入")

            extract_dir = tmpdir / "extracted"
            ok, msg = extract_package(pkg, extract_dir)
            if not ok:
                raise RuntimeError(msg)

            self.after(0, lambda: self.status.configure(
                text="正在部署到 MT5 目录…", text_color=TEXT_MUTED
            ))
            exe_path = directory / "node_client.exe"
            # 目录里可能已有旧客户端，backup=True 让它先进版本化备份
            ok, msg = apply_package(extract_dir, exe_path, backup=True)
            if not ok:
                raise RuntimeError(msg)

            template = ""
            example = extract_dir / ".env.example"
            if example.is_file():
                template = example.read_text(encoding="utf-8-sig", errors="replace")

        env_written = False
        if write_env and not ef.env_path(directory).is_file():
            ef.write_env(
                directory,
                ef.build_initial_env(template, ws_url=ws_url, node_token=target.token),
            )
            env_written = True

        # 注册实例必须回主线程：ProcessManager 与实例列表由 UI 线程持有
        self.after(0, lambda: self._register_instance(directory, name, version, env_written))

    # 不能叫 _register：tkinter.Misc._register 是内部方法，Toplevel 初始化时会用它
    # 注册 WM_DELETE_WINDOW 回调，重名会让整个对话框构造失败
    def _register_instance(
        self, directory: Path, name: str, version: str, env_written: bool
    ) -> None:
        exe_path = directory / "node_client.exe"
        cfg = InstanceConfig.create(
            name=name or default_label_from_path(str(exe_path)),
            exe_path=str(exe_path),
            cwd=str(directory),
        )
        self.manager.add(cfg)
        self.created = cfg
        if self._on_done:
            self._on_done(cfg)

        installed = read_version_near(exe_path) or version
        parts = [f"已导入「{cfg.name}」，客户端 v{installed}"]
        if env_written:
            parts.append(".env 已生成")
        self.status.configure(text="　".join(parts), text_color=ACCENT)

        if self.start_after_var.get():
            self.manager.start(cfg.id)

        messagebox.showinfo(
            "导入完成",
            f"实例「{cfg.name}」已加入列表。\n\n"
            f"客户端版本：v{installed}\n"
            f"目录：{directory}\n\n"
            + ("已生成 .env，可直接启动。" if env_written
               else "请确认 .env 已配置好后端地址与令牌，再启动实例。"),
            parent=self,
        )
