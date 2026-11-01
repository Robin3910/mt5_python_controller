"""人工验收脚本：把「更新客户端」时的转圈效果摆出来看。

不是正式测试（不参与 pytest），是给使用者肉眼确认用的：
    python tools/preview_busy.py

会开一个小窗口，按真实顺序播放各阶段的状态文案：
检测 → 下载（带百分比）→ 校验 → 逐实例更新 → 完成。
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import customtkinter as ctk  # noqa: E402

from busy_status import BusyStatus, ThrottledProgress, format_bytes  # noqa: E402
from theme import ACCENT, BG, TEXT_MUTED, font, glass_card  # noqa: E402

# 每个阶段：(状态文案, 停留秒数)。None 表示用进度回调驱动的下载阶段。
PHASES: list[tuple[str | None, float]] = [
    ("正在检测后端版本…", 0.8),
    (None, 3.0),
    ("正在校验安装包 sha256…", 0.8),
    ("正在更新 (2/5) icmarkets：停止实例…", 0.9),
    ("正在更新 (2/5) icmarkets：覆盖文件…", 0.9),
    ("正在更新 (2/5) icmarkets：重新启动…", 0.9),
]


def main() -> int:
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("dark-blue")
    root = ctk.CTk()
    root.title("转圈效果预览")
    root.geometry("760x300")
    root.configure(fg_color=BG)

    card = glass_card(root)
    card.pack(fill="both", expand=True, padx=20, pady=20)

    ctk.CTkLabel(
        card, text="状态条演示", text_color=ACCENT, font=font(16, "bold")
    ).pack(anchor="w", padx=20, pady=(20, 8))
    status = ctk.CTkLabel(
        card, text="尚未开始", text_color=TEXT_MUTED, font=font(13), anchor="w"
    )
    status.pack(fill="x", padx=20)
    ctk.CTkLabel(
        card,
        text="阶段：检测 → 下载（带百分比）→ 校验 → 逐实例更新 → 完成",
        text_color=TEXT_MUTED, font=font(11), anchor="w",
    ).pack(fill="x", padx=20, pady=(6, 20))

    busy = BusyStatus(status, owner=root)

    def play_download() -> None:
        total = 8 * 1024 * 1024
        step = 0

        def on_progress(done: int, total_bytes: int) -> None:
            text = (
                f"正在下载 v1.3.0　{format_bytes(done)} / {format_bytes(total_bytes)}"
                f"　{done * 100 // total_bytes}%"
            )
            root.after(0, lambda t=text: busy.update(t))

        throttle = ThrottledProgress(on_progress)
        while step <= total:
            throttle.report(step, total)
            step += 96 * 1024
            time.sleep(0.12)

    def run() -> None:
        for text, pause in PHASES:
            if text is None:
                play_download()
            else:
                busy.start(text)
            time.sleep(pause)
        busy.stop("✓ 完成：成功 5/5", ACCENT)

    root.after(500, lambda: threading.Thread(target=run, daemon=True).start())
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
