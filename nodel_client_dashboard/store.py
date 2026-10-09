"""实例列表与面板配置的 JSON 持久化。"""
from __future__ import annotations

import json
import os
import threading
import sys
from pathlib import Path
from typing import Any

from env_file import parse_env_text
from models import InstanceConfig

_WRITE_LOCK = threading.RLock()


def data_dir() -> Path:
    """面板数据目录：打包后在 exe 旁，开发时在本包目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def instances_path() -> Path:
    return data_dir() / "instances.json"


def panel_config_path() -> Path:
    return data_dir() / "panel_config.json"


def dashboard_env_path() -> Path:
    """面板自己的 .env：与 exe 同目录，只提供登录用的 APP_URL。"""
    return data_dir() / ".env"


def load_app_url(path: Path | None = None) -> str:
    """读取面板 .env 的 APP_URL。文件缺失、读失败或未配置时返回空串。"""
    p = path or dashboard_env_path()
    try:
        text = p.read_text(encoding="utf-8-sig")
    except OSError:
        return ""
    return parse_env_text(text).get("APP_URL", "").strip()


def logs_dir() -> Path:
    d = data_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_instances(path: Path | None = None) -> list[InstanceConfig]:
    p = path or instances_path()
    if not p.exists():
        return []
    raw = json.loads(p.read_text(encoding="utf-8-sig"))
    items = raw.get("instances") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []
    return [InstanceConfig.from_dict(x) for x in items if isinstance(x, dict)]


def save_instances(instances: list[InstanceConfig], path: Path | None = None) -> None:
    p = path or instances_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"instances": [i.to_dict() for i in instances]}
    _atomic_json(p, payload)


def load_panel_config(path: Path | None = None) -> dict[str, Any]:
    """读取面板级配置（后端地址与节点令牌）；不存在或损坏都返回空字典。

    这里存的 NODE_TOKEN 与节点 .env 里的是同一份，只用于向后端查询版本和下载
    安装包，不能用它做任何管理操作。
    """
    p = path or panel_config_path()
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def save_panel_config(cfg: dict[str, Any], path: Path | None = None) -> None:
    p = path or panel_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    # JWT、密码、2FA 中间态与 daemon grant 永不落盘。
    safe = {k: v for k, v in cfg.items() if k in {"backend_base", "last_username", "node_token"}}
    _atomic_json(p, safe)


def _atomic_json(path: Path, payload: Any) -> None:
    with _WRITE_LOCK:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)


def load_audit_queue(path: Path | None = None) -> list[dict]:
    p = path or data_dir() / "audit_results.json"
    if not p.is_file():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        return raw if isinstance(raw, list) else []
    except (OSError, ValueError):
        # 损坏不能被静默视为没有审计。
        raise RuntimeError("操作审计队列损坏，请管理员检查后恢复")


def save_audit_queue(records: list[dict], path: Path | None = None) -> None:
    p = path or data_dir() / "audit_results.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    _atomic_json(p, records)


def load_daemon_events() -> list[dict]:
    return load_audit_queue(data_dir() / "daemon_events.json")


def save_daemon_events(records: list[dict]) -> None:
    save_audit_queue(records, data_dir() / "daemon_events.json")
