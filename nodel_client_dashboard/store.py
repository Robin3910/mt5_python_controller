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


def canonical_dir(path: str) -> str:
    """实例目录的比较键。把 UAC 虚拟化目录还原到真实的 Program Files 路径。"""
    raw = os.path.normpath(os.path.abspath(path or "."))
    if raw.lower().endswith(".exe"):
        raw = os.path.dirname(raw)
    marker = f"{os.sep}AppData{os.sep}Local{os.sep}VirtualStore{os.sep}"
    folded = raw.lower()
    idx = folded.find(marker.lower())
    if idx != -1:
        tail = raw[idx + len(marker):]
        drive = os.path.splitdrive(raw)[0] or os.environ.get("SystemDrive", "C:")
        raw = os.path.normpath(os.path.join(drive + os.sep, tail))
    return os.path.normcase(raw)


def same_instance(left: InstanceConfig, right: InstanceConfig) -> bool:
    """同一后台节点、同一交易账号，或同一安装目录（含虚拟化副本）视为同一个实例。"""
    if left.id == right.id:
        return False
    if left.node_id and left.node_id == right.node_id:
        return True
    if left.mt5_login and left.mt5_login == right.mt5_login:
        return True
    left_dir = canonical_dir(left.exe_path or left.cwd)
    right_dir = canonical_dir(right.exe_path or right.cwd)
    return bool(left_dir and left_dir == right_dir)


def _prefer_instance(members: list[InstanceConfig]) -> InstanceConfig:
    """真实安装目录优先于 VirtualStore 副本；同样真实时保留后写入的那条。"""
    chosen = members[0]
    for cfg in members[1:]:
        chosen_virtual = "\\virtualstore\\" in chosen.exe_path.lower().replace("/", "\\")
        cfg_virtual = "\\virtualstore\\" in cfg.exe_path.lower().replace("/", "\\")
        if chosen_virtual and not cfg_virtual:
            chosen = cfg
        elif chosen_virtual == cfg_virtual:
            chosen = cfg
    return chosen


def dedupe_instances(configs: list[InstanceConfig]) -> tuple[list[InstanceConfig], list[InstanceConfig]]:
    """合并重复实例。返回 (保留, 丢弃)，保留项维持原有顺序。"""
    parent = list(range(len(configs)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    for i in range(len(configs)):
        for j in range(i):
            if same_instance(configs[i], configs[j]):
                union(i, j)
    groups: dict[int, list[InstanceConfig]] = {}
    for i, cfg in enumerate(configs):
        groups.setdefault(find(i), []).append(cfg)
    drop_ids = set()
    for members in groups.values():
        if len(members) < 2:
            continue
        winner = _prefer_instance(members)
        drop_ids.update(cfg.id for cfg in members if cfg.id != winner.id)
    kept = [cfg for cfg in configs if cfg.id not in drop_ids]
    dropped = [cfg for cfg in configs if cfg.id in drop_ids]
    return kept, dropped


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
