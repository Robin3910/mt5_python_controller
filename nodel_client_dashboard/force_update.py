"""控制端领取服务器强制更新令。

同一条令只成功执行一次。只升级版本低于发布版本的实例，不降级。
失败不记完成，下一轮版本查询再试。
"""
from __future__ import annotations

import json
import logging
import tempfile
import threading
import time
from pathlib import Path

import version_service as vs
from client_deploy import UPGRADE, classify_update, extract_package, read_version_near
from store import data_dir

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_INFLIGHT = ""
_RETRY_AT = 0.0
_RETRY_WAIT_S = 60.0


def _state_path() -> Path:
    return data_dir() / "force_update.json"


def applied_id() -> str:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("id") or "")


def _remember(force_id: str) -> None:
    _state_path().write_text(
        json.dumps({"id": force_id}, ensure_ascii=False), encoding="utf-8"
    )


def behind_ids(manager, version: str) -> list[str]:
    ids: list[str] = []
    for cfg in manager.configs():
        if classify_update(read_version_near(cfg.exe_path), version) == UPGRADE:
            ids.append(cfg.id)
    return ids


def apply_if_needed(manager, target, data: dict) -> None:
    """有新的强制更新令、且本机有落后实例时，直接下载并覆盖。"""
    global _INFLIGHT, _RETRY_AT
    if not isinstance(data, dict) or not getattr(target, "ready", False):
        return
    force = data.get("force")
    if not isinstance(force, dict):
        return
    force_id = str(force.get("id") or "").strip()
    version = str(force.get("version") or data.get("version") or "").strip()
    sha = str(data.get("sha256") or "").strip()
    if not force_id or not version:
        return
    with _LOCK:
        if time.monotonic() < _RETRY_AT:
            return
        if force_id == applied_id() or force_id == _INFLIGHT:
            return
        _INFLIGHT = force_id
    try:
        ids = behind_ids(manager, version)
        if not ids:
            _remember(force_id)
            return
        results = _download_and_apply(manager, target, version, sha, ids)
        if any(not getattr(item, "ok", False) for item in results):
            raise RuntimeError("部分实例未更新")
        _remember(force_id)
    except Exception:
        logger.warning("force client update %s failed", force_id, exc_info=True)
        with _LOCK:
            _RETRY_AT = time.monotonic() + _RETRY_WAIT_S
    finally:
        with _LOCK:
            if _INFLIGHT == force_id:
                _INFLIGHT = ""


def _download_and_apply(manager, target, version: str, sha: str, ids: list[str]) -> list:
    with tempfile.TemporaryDirectory(prefix="node_client_force_") as tmp:
        pkg = Path(tmp) / f"node_client-{version}.zip"
        vs.download_package(target, version, pkg)
        if sha and not vs.verify_sha256(pkg, sha):
            raise RuntimeError("安装包校验和不匹配，已中止更新")
        extract_dir = Path(tmp) / "extracted"
        ok, msg = extract_package(pkg, extract_dir)
        if not ok:
            raise RuntimeError(msg)
        return manager.batch_update(
            extract_dir, instance_ids=ids, restart_if_was_running=True
        )
