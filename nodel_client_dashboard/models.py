"""运维面板实例配置模型。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4


def new_instance_id() -> str:
    return uuid4().hex[:12]


def default_label_from_path(exe_path: str) -> str:
    """由可执行路径生成默认标签（文件名去扩展名）。"""
    stem = Path(exe_path or "").stem.strip()
    return stem or "node_client"


def shorten_path(path: str, max_len: int = 42) -> str:
    """列表展示用短路径：过长时保留头尾。"""
    text = (path or "").strip()
    if len(text) <= max_len:
        return text
    keep = max_len - 3
    head = keep // 2
    tail = keep - head
    return f"{text[:head]}...{text[-tail:]}"


@dataclass
class InstanceConfig:
    id: str
    name: str
    exe_path: str
    cwd: str
    status_port: int = 0
    daemon: bool = False
    restart_delay_s: float = 5.0
    enabled: bool = True

    @classmethod
    def create(
        cls,
        *,
        name: str = "",
        exe_path: str,
        cwd: str = "",
        status_port: int = 0,
        daemon: bool = False,
        restart_delay_s: float = 5.0,
    ) -> "InstanceConfig":
        exe = str(exe_path or "").strip()
        label = (name or "").strip() or default_label_from_path(exe)
        workdir = (cwd or "").strip() or (str(Path(exe).parent) if exe else "")
        return cls(
            id=new_instance_id(),
            name=label,
            exe_path=exe,
            cwd=workdir,
            status_port=status_port,
            daemon=daemon,
            restart_delay_s=restart_delay_s,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "InstanceConfig":
        exe = str(data.get("exe_path") or "")
        name = str(data.get("name") or "").strip() or default_label_from_path(exe)
        cwd = str(data.get("cwd") or "").strip()
        if not cwd and exe:
            cwd = str(Path(exe).parent)
        return cls(
            id=str(data.get("id") or new_instance_id()),
            name=name,
            exe_path=exe,
            cwd=cwd,
            status_port=int(data.get("status_port") or 0),
            daemon=bool(data.get("daemon")),
            restart_delay_s=float(data.get("restart_delay_s") or 5.0),
            enabled=bool(data.get("enabled", True)),
        )


@dataclass
class RuntimeState:
    """运行时态（不落盘）。"""
    pid: int | None = None
    process_alive: bool = False
    health: str = "unknown"  # unknown|starting|ok|dead|error
    ws_state: str | None = None
    node_id: int | None = None
    mt5_login: int | None = None
    runners: list[dict[str, Any]] = field(default_factory=list)
    last_error: str = ""
    uptime_s: float | None = None
    busy: str = ""  # "" | starting | stopping
    version: str = ""
