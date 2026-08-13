"""客户端版本更新对话框：检测后端版本、批量更新、指定版本、本机回滚。

后端地址与 NODE_TOKEN 可以在这里手工填写并保存到 panel_config.json；留空时会自动
从各实例工作目录下的 node_client .env 里读取（见 version_service.resolve_backend）。

所有涉及文件替换的动作都在后台线程执行，主线程只负责渲染与二次确认。
"""
from __future__ import annotations

import tempfile
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox

import customtkinter as ctk

import version_service as vs
from client_deploy import (
    DOWNGRADE,
    classify_update,
    extract_package,
    list_local_backups,
    read_version_near,
    update_label,
)
from dialog_window import hide_while_building, show_centered
from process_manager import ProcessManager
from store import load_panel_config, save_panel_config
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
    danger_btn,
    font,
    ghost_btn,
    glass_card,
    mono,
    primary_btn,
    soft_card,
)


class _Row:
    """实例清单里的一行（勾选框 + 版本对比）。"""

    def __init__(self, master, cfg, row_index: int) -> None:
        self.cfg = cfg
        self.var = tk.BooleanVar(value=True)
        self.current = ""

        self.frame = ctk.CTkFrame(master, fg_color="transparent")
        self.frame.grid(row=row_index, column=0, sticky="ew", padx=4, pady=2)
        self.frame.grid_columnconfigure(1, weight=1)

        self.check = ctk.CTkCheckBox(
            self.frame, text="", variable=self.var, width=24,
            checkbox_width=18, checkbox_height=18,
            fg_color=ACCENT, hover_color=ACCENT, border_color=GLASS_BORDER,
        )
        self.check.grid(row=0, column=0, padx=(6, 4))

        self.name = ctk.CTkLabel(
            self.frame, text=cfg.name, text_color=TEXT, font=font(13), anchor="w"
        )
        self.name.grid(row=0, column=1, sticky="ew", padx=4)

        self.version = ctk.CTkLabel(
            self.frame, text="v-", text_color=TEXT_MUTED, font=mono(12), width=150, anchor="w"
        )
        self.version.grid(row=0, column=2, padx=4)

        self.verdict = ctk.CTkLabel(
            self.frame, text="", text_color=TEXT_DIM, font=font(12), width=80, anchor="e"
        )
        self.verdict.grid(row=0, column=3, padx=(4, 10))

    def refresh(self, target_version: str) -> None:
        """按最新的目标版本刷新本行的版本对比与判定。"""
        self.current = read_version_near(self.cfg.exe_path)
        cur = self.current or "?"
        if target_version:
            self.version.configure(text=f"v{cur}  →  v{target_version}")
            kind = classify_update(self.current, target_version)
        else:
            self.version.configure(text=f"v{cur}")
            kind = ""
        if not kind:
            self.verdict.configure(text="", text_color=TEXT_DIM)
            return
        color = {"upgrade": ACCENT, DOWNGRADE: WARNING, "same": TEXT_DIM}.get(kind, TEXT_DIM)
        self.verdict.configure(text=update_label(kind), text_color=color)


class VersionUpdateDialog(ctk.CTkToplevel):
    """版本检测 / 批量更新 / 降级 / 本机回滚。"""

    _W = 860
    _H = 640

    def __init__(self, master, manager: ProcessManager, on_done=None) -> None:
        super().__init__(master)
        self.title("客户端版本更新")
        self.configure(fg_color=BG)
        self.transient(master)
        self.resizable(False, False)
        hide_while_building(self)

        self.manager = manager
        self._on_done = on_done
        self._rows: list[_Row] = []
        self._remote: dict = {}
        self._busy = False

        cfg = load_panel_config()
        self.base_var = tk.StringVar(value=str(cfg.get("backend_base") or ""))
        self.token_var = tk.StringVar(value=str(cfg.get("node_token") or ""))

        self._build()
        self._bind_rows()
        self._autofill_from_env()
        self.bind("<Escape>", lambda _e: self.destroy())
        self.after(20, self._show_centered)

    # ---------------------------------------------------------------- 布局

    def _build(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        conn = glass_card(self)
        conn.grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 8))
        conn.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            conn, text="后端连接", text_color=ACCENT, font=font(11, "bold")
        ).grid(row=0, column=0, columnspan=3, padx=16, pady=(14, 2), sticky="w")

        ctk.CTkLabel(conn, text="后端地址", text_color=TEXT_MUTED, font=font(12)).grid(
            row=1, column=0, padx=16, pady=6, sticky="w"
        )
        ctk.CTkEntry(
            conn, textvariable=self.base_var, fg_color=BG_ELEVATED,
            border_color=GLASS_BORDER, text_color=TEXT, height=34, corner_radius=10,
            placeholder_text="http://159.75.33.185（留空则读实例 .env）",
        ).grid(row=1, column=1, columnspan=2, padx=(8, 16), pady=6, sticky="ew")

        ctk.CTkLabel(conn, text="节点令牌", text_color=TEXT_MUTED, font=font(12)).grid(
            row=2, column=0, padx=16, pady=6, sticky="w"
        )
        ctk.CTkEntry(
            conn, textvariable=self.token_var, fg_color=BG_ELEVATED,
            border_color=GLASS_BORDER, text_color=TEXT, height=34, corner_radius=10,
            show="*", placeholder_text="NODE_TOKEN（留空则读实例 .env）",
        ).grid(row=2, column=1, columnspan=2, padx=(8, 16), pady=6, sticky="ew")

        actions = ctk.CTkFrame(conn, fg_color="transparent")
        actions.grid(row=3, column=0, columnspan=3, padx=12, pady=(6, 14), sticky="ew")
        primary_btn(actions, "检测更新", self._detect, width=110).pack(side="left", padx=4)
        ghost_btn(actions, "保存连接配置", self._save_config, width=130).pack(side="left", padx=4)
        self.status = ctk.CTkLabel(
            actions, text="尚未检测", text_color=TEXT_DIM, font=font(12), anchor="w"
        )
        self.status.pack(side="left", padx=12, fill="x", expand=True)

        # —— 实例清单 ——
        listcard = glass_card(self)
        listcard.grid(row=1, column=0, sticky="nsew", padx=16, pady=8)
        listcard.grid_columnconfigure(0, weight=1)
        listcard.grid_rowconfigure(2, weight=1)

        head = ctk.CTkFrame(listcard, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=14, pady=(14, 2))
        ctk.CTkLabel(
            head, text="实例清单", text_color=ACCENT, font=font(11, "bold")
        ).pack(side="left")
        ghost_btn(head, "全选", lambda: self._select_all(True), width=64).pack(side="right", padx=2)
        ghost_btn(head, "全不选", lambda: self._select_all(False), width=72).pack(side="right", padx=2)

        cols = ctk.CTkFrame(listcard, fg_color="transparent")
        cols.grid(row=1, column=0, sticky="ew", padx=10, pady=(4, 0))
        cols.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(cols, text="", width=24).grid(row=0, column=0, padx=(6, 4))
        ctk.CTkLabel(
            cols, text="实例", text_color=TEXT_DIM, font=font(11), anchor="w"
        ).grid(row=0, column=1, sticky="ew", padx=4)
        ctk.CTkLabel(
            cols, text="版本", text_color=TEXT_DIM, font=font(11), width=150, anchor="w"
        ).grid(row=0, column=2, padx=4)
        ctk.CTkLabel(
            cols, text="判定", text_color=TEXT_DIM, font=font(11), width=80, anchor="e"
        ).grid(row=0, column=3, padx=(4, 10))

        self.rows_frame = ctk.CTkScrollableFrame(
            listcard, fg_color="transparent", scrollbar_button_color=GLASS_BORDER,
            scrollbar_button_hover_color=ACCENT,
        )
        self.rows_frame.grid(row=2, column=0, sticky="nsew", padx=10, pady=8)
        self.rows_frame.grid_columnconfigure(0, weight=1)

        # —— 操作区 ——
        ops = soft_card(self)
        ops.grid(row=2, column=0, sticky="ew", padx=16, pady=(8, 16))

        line1 = ctk.CTkFrame(ops, fg_color="transparent")
        line1.pack(fill="x", padx=12, pady=(12, 6))
        primary_btn(line1, "更新到发布版本", self._update_to_release, width=150).pack(
            side="left", padx=4
        )
        ghost_btn(line1, "更新到指定版本", self._update_to_specific, width=140).pack(
            side="left", padx=4
        )
        ctk.CTkLabel(
            line1,
            text="更新会先停止实例，备份当前版本后覆盖，再自动启动；.env 不会被覆盖。",
            text_color=TEXT_DIM, font=font(11), anchor="w",
        ).pack(side="left", padx=10, fill="x", expand=True)

        line2 = ctk.CTkFrame(ops, fg_color="transparent")
        line2.pack(fill="x", padx=12, pady=(0, 12))
        danger_btn(line2, "回滚到本机备份", self._rollback, width=150).pack(side="left", padx=4)
        ghost_btn(line2, "刷新本机版本", self._refresh_rows, width=130).pack(side="left", padx=4)
        ghost_btn(line2, "关闭", self.destroy, width=90).pack(side="right", padx=4)

    def _show_centered(self) -> None:
        show_centered(self, self._W, self._H)

    # ---------------------------------------------------------------- 数据

    def _bind_rows(self) -> None:
        for row in self._rows:
            row.frame.destroy()
        self._rows = [
            _Row(self.rows_frame, cfg, i) for i, cfg in enumerate(self.manager.configs())
        ]
        self._refresh_rows()

    def _refresh_rows(self) -> None:
        target = str(self._remote.get("version") or "")
        for row in self._rows:
            row.refresh(target)

    def _select_all(self, checked: bool) -> None:
        for row in self._rows:
            row.var.set(checked)

    def _selected_rows(self) -> list[_Row]:
        return [r for r in self._rows if r.var.get()]

    def _autofill_from_env(self) -> None:
        """地址/令牌留空时，用实例 .env 里的值把输入框填上，让用户看得见来源。"""
        target = self._target()
        if not self.base_var.get().strip() and target.base:
            self.base_var.set(target.base)
        if not self.token_var.get().strip() and target.token:
            self.token_var.set(target.token)

    def _target(self) -> vs.BackendTarget:
        cfg = {
            "backend_base": self.base_var.get().strip(),
            "node_token": self.token_var.get().strip(),
        }
        cwds = [c.cwd or str(Path(c.exe_path).parent) for c in self.manager.configs()]
        return vs.resolve_backend(cfg, cwds)

    def _save_config(self) -> None:
        save_panel_config({
            "backend_base": self.base_var.get().strip().rstrip("/"),
            "node_token": self.token_var.get().strip(),
        })
        self.status.configure(text="连接配置已保存", text_color=ACCENT)

    # ---------------------------------------------------------------- 动作

    def _set_busy(self, busy: bool, text: str = "") -> None:
        self._busy = busy
        if text:
            self.status.configure(text=text, text_color=TEXT_MUTED)

    def _run_async(self, label: str, fn) -> None:
        if self._busy:
            messagebox.showinfo("请稍候", "上一个操作还在进行中", parent=self)
            return
        self._set_busy(True, f"{label}中…")

        def worker():
            try:
                fn()
            except vs.VersionServiceError as e:
                self.after(0, lambda: self.status.configure(text=str(e), text_color=DANGER))
            except Exception as e:  # noqa: BLE001
                self.after(0, lambda: messagebox.showerror(label, str(e), parent=self))
            finally:
                self.after(0, lambda: self._set_busy(False))
                self.after(0, self._refresh_rows)
                if self._on_done:
                    self.after(0, self._on_done)

        threading.Thread(target=worker, name=f"ver-{label}", daemon=True).start()

    def _detect(self) -> None:
        target = self._target()
        if not target.ready:
            self.status.configure(
                text="缺少后端地址或节点令牌：请手工填写，或确保实例 .env 里有 MANAGER_WS_URL 与 NODE_TOKEN",
                text_color=DANGER,
            )
            return

        def job():
            data = vs.fetch_current_version(target)
            self._remote = data or {}

            def done():
                if not self._remote:
                    self.status.configure(text="后端尚未发布任何客户端版本", text_color=WARNING)
                    return
                note = self._remote.get("notes") or ""
                text = f"后端发布版本 v{self._remote.get('version')}（来源：{target.source}）"
                self.status.configure(text=f"{text}　{note}".strip(), text_color=ACCENT)

            self.after(0, done)

        self._run_async("检测", job)

    def _confirm(self, title: str, lines: list[str]) -> bool:
        return messagebox.askyesno(title, "\n".join(lines), parent=self)

    def _report(self, title: str, results: list) -> None:
        ok_n = sum(1 for r in results if r.ok)
        lines = [f"完成：成功 {ok_n}/{len(results)}"]
        for r in results:
            lines.append(
                f"{'✓' if r.ok else '✗'} {r.name}: "
                f"v{r.old_version or '-'} → v{r.new_version or '-'}  {r.message}"
            )
        text = "\n".join(lines)
        show = messagebox.showinfo if ok_n == len(results) else messagebox.showwarning
        self.after(0, lambda: show(title, text, parent=self))

    def _download_and_apply(
        self,
        target: vs.BackendTarget,
        version: str,
        sha256: str,
        rows: list[_Row],
        title: str,
    ) -> None:
        """下载 → 校验 → 解压 → 批量覆盖；临时目录用完即清。

        target 由主线程解析后传入：Tcl 解释器不是线程安全的，后台线程不能去读
        输入框里的地址与令牌。
        """
        ids = [r.cfg.id for r in rows]
        with tempfile.TemporaryDirectory(prefix="node_client_pkg_") as tmp:
            tmpdir = Path(tmp)
            pkg = tmpdir / f"node_client-{version}.zip"
            self.after(0, lambda: self.status.configure(
                text=f"正在下载 v{version}…", text_color=TEXT_MUTED
            ))
            vs.download_package(target, version, pkg)
            if not vs.verify_sha256(pkg, sha256):
                raise RuntimeError("安装包校验和不匹配，已中止更新")

            extract_dir = tmpdir / "extracted"
            ok, msg = extract_package(pkg, extract_dir)
            if not ok:
                raise RuntimeError(msg)

            self.after(0, lambda: self.status.configure(
                text=f"正在更新 {len(ids)} 个实例…", text_color=TEXT_MUTED
            ))
            results = self.manager.batch_update(
                extract_dir, instance_ids=ids, restart_if_was_running=True
            )
        self._report(title, results)

    def _update_to_release(self) -> None:
        if not self._remote:
            messagebox.showinfo("提示", "请先点「检测更新」获取后端发布版本", parent=self)
            return
        version = str(self._remote.get("version") or "")
        rows = self._selected_rows()
        if not self._precheck(rows, version, "更新"):
            return
        sha = str(self._remote.get("sha256") or "")
        target = self._target()
        self._run_async(
            "更新", lambda: self._download_and_apply(target, version, sha, rows, "批量更新结果")
        )

    def _update_to_specific(self) -> None:
        """更新到手工指定的版本号（服务端已上传即可，不必是当前发布版本）。"""
        rows = self._selected_rows()
        if not rows:
            messagebox.showinfo("提示", "请先勾选要更新的实例", parent=self)
            return
        dlg = ctk.CTkInputDialog(text="输入目标版本号（例如 1.1.0）", title="更新到指定版本")
        version = (dlg.get_input() or "").strip().lstrip("vV")
        if not version:
            return
        if not self._precheck(rows, version, "更新"):
            return
        target = self._target()
        if not target.ready:
            messagebox.showinfo("提示", "尚未配置后端地址或节点令牌", parent=self)
            return
        # 指定版本的校验和不在手上（未必是当前发布版本），跳过完整性校验
        self._run_async(
            "更新", lambda: self._download_and_apply(target, version, "", rows, "指定版本更新结果")
        )

    def _precheck(self, rows: list[_Row], version: str, action: str) -> bool:
        """勾选校验 + 逐行列出影响面的二次确认；含降级时再加一道确认。"""
        if not rows:
            messagebox.showinfo("提示", f"请先勾选要{action}的实例", parent=self)
            return False
        if not version:
            messagebox.showinfo("提示", "目标版本为空", parent=self)
            return False

        downgrades = [r for r in rows if classify_update(r.current, version) == DOWNGRADE]
        lines = [f"将把以下 {len(rows)} 个实例{action}到 v{version}：", ""]
        for r in rows:
            kind = classify_update(r.current, version)
            lines.append(f"  · {r.cfg.name}  v{r.current or '?'} → v{version}  [{update_label(kind)}]")
        lines += [
            "",
            "运行中的实例会先停止 → 备份当前版本 → 覆盖 → 再自动启动。",
            ".env 不会被覆盖。",
            "是否继续？",
        ]
        if not self._confirm(f"{action}确认", lines):
            return False

        if downgrades:
            names = "\n".join(f"  · {r.cfg.name}  v{r.current} → v{version}" for r in downgrades)
            if not self._confirm(
                "降级二次确认",
                [
                    f"以下 {len(downgrades)} 个实例的目标版本低于当前版本：",
                    "",
                    names,
                    "",
                    "降级可能把已修复的问题带回线上。确认要降级吗？",
                ],
            ):
                return False
        return True

    def _rollback(self) -> None:
        rows = self._selected_rows()
        if not rows:
            messagebox.showinfo("提示", "请先勾选要回滚的实例", parent=self)
            return

        common = set(list_local_backups(rows[0].cfg.exe_path))
        for r in rows[1:]:
            common &= set(list_local_backups(r.cfg.exe_path))
        if not common:
            messagebox.showinfo(
                "无可用备份",
                "所选实例没有共同的本机备份版本。\n\n备份在首次通过面板更新后才会生成。",
                parent=self,
            )
            return

        options = sorted(common, reverse=True)
        dlg = ctk.CTkInputDialog(
            text=f"输入要回滚到的版本号：\n\n可用备份：{', '.join(options)}",
            title="回滚到本机备份",
        )
        version = (dlg.get_input() or "").strip().lstrip("vV")
        if not version:
            return
        if version not in common:
            messagebox.showerror("版本不可用", f"所选实例没有版本 {version} 的共同备份", parent=self)
            return

        lines = [f"将把以下 {len(rows)} 个实例回滚到本机备份 v{version}：", ""]
        for r in rows:
            lines.append(f"  · {r.cfg.name}  v{r.current or '?'} → v{version}")
        lines += ["", "回滚前会先备份当前版本。运行中的实例会先停止，覆盖后自动启动。", "是否继续？"]
        if not self._confirm("回滚确认", lines):
            return

        ids = [r.cfg.id for r in rows]

        def job():
            results = self.manager.batch_rollback(
                version, instance_ids=ids, restart_if_was_running=True
            )
            self._report("回滚结果", results)

        self._run_async("回滚", job)
