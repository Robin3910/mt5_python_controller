"""实例列表与面板配置的 JSON 持久化。"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from models import InstanceConfig


def data_dir() -> Path:
    """面板数据目录：打包后在 exe 旁，开发时在本包目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def instances_path() -> Path:
    return data_dir() / "instances.json"


def panel_config_path() -> Path:
    return data_dir() / "panel_config.json"


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
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
