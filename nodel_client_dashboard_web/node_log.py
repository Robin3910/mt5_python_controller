"""按节点目录落盘、按天轮转的运行日志。"""
from __future__ import annotations

import re
import threading
from datetime import date, datetime
from pathlib import Path

from models import InstanceConfig
from store import logs_dir


def sanitize_node_key(name: str, instance_id: str) -> str:
    """节点标识符：可读名称 + 短 id，避免重名冲突与非法路径字符。"""
    raw = (name or "").strip() or "node"
    safe = re.sub(r"[^\w\u4e00-\u9fff\-]+", "_", raw, flags=re.UNICODE)
    safe = safe.strip("_") or "node"
    short = (instance_id or "x")[:8]
    return f"{safe}_{short}"


def node_logs_dir(cfg: InstanceConfig) -> Path:
    """程序运行目录/logs/<节点标识符>/"""
    d = logs_dir() / sanitize_node_key(cfg.name, cfg.id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def log_path_for_day(cfg: InstanceConfig, day: date | None = None) -> Path:
    d = day or date.today()
    return node_logs_dir(cfg) / f"{d.isoformat()}.log"


def latest_log_path(cfg: InstanceConfig) -> Path | None:
    """当前日日志优先；否则目录内最新 .log。"""
    today = log_path_for_day(cfg)
    if today.is_file():
        return today
    folder = node_logs_dir(cfg)
    files = sorted(folder.glob("*.log"), key=lambda p: p.name, reverse=True)
    return files[0] if files else None


class DailyLogWriter:
    """追加写入 logs/<node>/YYYY-MM-DD.log，跨日自动切换文件。"""

    def __init__(self, cfg: InstanceConfig) -> None:
        self._cfg = cfg
        self._lock = threading.Lock()
        self._day: date | None = None
        self._fp = None

    @property
    def path(self) -> Path:
        with self._lock:
            self._ensure_open_locked()
            return log_path_for_day(self._cfg, self._day)

    def write(self, data: bytes) -> None:
        if not data:
            return
        with self._lock:
            self._ensure_open_locked()
            assert self._fp is not None
            self._fp.write(data)
            self._fp.flush()

    def write_line(self, text: str) -> None:
        payload = text if text.endswith("\n") else text + "\n"
        self.write(payload.encode("utf-8", errors="replace"))

    def close(self) -> None:
        with self._lock:
            if self._fp is not None:
                try:
                    self._fp.close()
                except OSError:
                    pass
                self._fp = None
                self._day = None

    def _ensure_open_locked(self) -> None:
        today = date.today()
        if self._fp is not None and self._day == today:
            return
        if self._fp is not None:
            try:
                self._fp.close()
            except OSError:
                pass
            self._fp = None
        path = log_path_for_day(self._cfg, today)
        path.parent.mkdir(parents=True, exist_ok=True)
        # 会话分隔，便于实时追踪
        self._fp = open(path, "ab")
        self._day = today
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        header = f"\n===== {stamp} session =====\n".encode("utf-8")
        self._fp.write(header)
        self._fp.flush()
