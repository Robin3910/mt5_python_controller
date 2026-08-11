"""客户端版本读取与批量替换（纯函数，供面板调用）。"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path


def read_version_near(exe_path: str | Path) -> str:
    """读取 exe 同目录 version.txt；不存在返回空串。"""
    p = Path(exe_path)
    txt = p.parent / "version.txt"
    if not txt.is_file():
        return ""
    try:
        text = txt.read_text(encoding="utf-8-sig").strip()
    except OSError:
        return ""
    return text.splitlines()[0].strip() if text else ""


def source_bundle_files(source_exe: str | Path) -> list[Path]:
    """替换时一并复制的旁路文件（version.txt）。"""
    src = Path(source_exe)
    out = [src]
    ver = src.parent / "version.txt"
    if ver.is_file():
        out.append(ver)
    return out


@dataclass
class ReplaceResult:
    instance_id: str
    name: str
    target: str
    ok: bool
    message: str
    old_version: str = ""
    new_version: str = ""
    was_running: bool = False
    restarted: bool = False


def replace_exe_file(
    *,
    source_exe: Path,
    target_exe: Path,
    backup: bool = True,
) -> tuple[bool, str]:
    """把 source 覆盖到 target；可选备份为 .bak。返回 (ok, message)。"""
    if not source_exe.is_file():
        return False, f"源文件不存在: {source_exe}"
    target_exe.parent.mkdir(parents=True, exist_ok=True)
    try:
        if backup and target_exe.is_file():
            bak = target_exe.with_suffix(target_exe.suffix + ".bak")
            shutil.copy2(target_exe, bak)
        shutil.copy2(source_exe, target_exe)
        # 同步 version.txt
        src_ver = source_exe.parent / "version.txt"
        dst_ver = target_exe.parent / "version.txt"
        if src_ver.is_file():
            shutil.copy2(src_ver, dst_ver)
        return True, "ok"
    except OSError as e:
        return False, str(e)
