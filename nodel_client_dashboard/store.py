"""实例列表 JSON 持久化。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from models import InstanceConfig


def data_dir() -> Path:
    """面板数据目录：打包后在 exe 旁，开发时在本包目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def instances_path() -> Path:
    return data_dir() / "instances.json"


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
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
