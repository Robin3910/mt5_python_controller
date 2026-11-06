"""客户端账户绑定能力校验；不会执行未知客户端探测参数。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def binding_supported(executable: str | Path) -> bool:
    path = Path(executable).resolve()
    trusted_source = Path(__file__).resolve().parent.parent / "node_client" / "node_client.py"
    if path == trusted_source.resolve():
        try:
            return "ACCOUNT_BINDING_SUPPORTED = True" in path.read_text(encoding="utf-8")
        except OSError:
            return False
    try:
        manifest = json.loads((path.parent / "client_capabilities.json").read_text(encoding="utf-8"))
        if manifest.get("account_binding") is not True:
            return False
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest() == str(manifest.get("executable_sha256", "")).lower()
    except (OSError, ValueError, AttributeError):
        return False


def require_binding(executable: str | Path) -> None:
    if not binding_supported(executable):
        raise RuntimeError("该客户端不支持安全账户绑定，请先更新到新版客户端；禁止启动旧客户端")
