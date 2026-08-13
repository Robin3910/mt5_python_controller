"""客户端版本读取、备份与安装包覆盖（纯函数，供面板调用）。

覆盖安装的两条硬约束：
- `.env` 永不替换 —— 它承载节点身份（NODE_TOKEN / MT5 账号），覆盖即掉线；
- Windows 上正在运行的 exe 无法覆盖，调用方必须先停进程；即便停了，句柄释放
  也有滞后，所以写入统一走 `_copy_with_retry` 退避重试。
"""
from __future__ import annotations

import re
import shutil
import time
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

# 版本化备份目录名；放在 exe 同级，回滚时按版本号取回
BACKUP_DIRNAME = ".backups"

# 覆盖安装时永不替换的文件（小写比较）
PROTECTED_NAMES = frozenset({".env"})

_RETRY_DELAYS = (0.2, 0.5, 1.0, 2.0)

_NUM_RE = re.compile(r"\d+")

UPGRADE = "upgrade"
DOWNGRADE = "downgrade"
SAME = "same"
UNKNOWN = "unknown"


def parse_version(raw: str) -> tuple[int, ...]:
    """取版本串里的数字段用于比较；无数字段返回空元组。

    与后端 `backend/app/client_version.py` 同一套语义 —— 面板要独立打包成 exe，
    不能引用后端代码，所以这里保留一份实现。
    """
    return tuple(int(x) for x in _NUM_RE.findall(raw or ""))


def compare_versions(a: str, b: str) -> int:
    """比较版本：a < b 返回 -1，相等 0，a > b 返回 1。"""
    pa, pb = parse_version(a), parse_version(b)
    if not pa and not pb:
        sa, sb = (a or "").strip(), (b or "").strip()
        return (sa > sb) - (sa < sb)
    width = max(len(pa), len(pb))
    pa += (0,) * (width - len(pa))
    pb += (0,) * (width - len(pb))
    return (pa > pb) - (pa < pb)


def classify_update(current: str, target: str) -> str:
    """判定 current → target 属于升级 / 降级 / 相同；任一侧未知返回 unknown。"""
    cur, tgt = (current or "").strip(), (target or "").strip()
    if not cur or not tgt:
        return UNKNOWN
    diff = compare_versions(cur, tgt)
    if diff < 0:
        return UPGRADE
    return DOWNGRADE if diff > 0 else SAME


def update_label(kind: str) -> str:
    """更新判定的中文展示文案。"""
    return {
        UPGRADE: "需更新",
        DOWNGRADE: "将降级",
        SAME: "已最新",
    }.get(kind, "版本未知")


def count_pending_upgrades(current_versions: Iterable[str], target: str) -> int:
    """统计有多少个实例的版本低于 target，即「有更新可装」。

    版本号读不出来的实例不计入：面板无从判断它是旧是新，算作待更新会让顶栏
    提示常亮，久而久之就没人看了。
    """
    tgt = (target or "").strip()
    if not tgt:
        return 0
    return sum(1 for v in current_versions if classify_update(v, tgt) == UPGRADE)


def _safe_members(zf: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    """过滤掉绝对路径与 .. 上跳的成员（zip slip 防护）。"""
    out: list[zipfile.ZipInfo] = []
    for info in zf.infolist():
        if info.is_dir():
            continue
        name = info.filename.replace("\\", "/").strip()
        parts = name.split("/")
        if not name or name.startswith("/") or ":" in parts[0] or ".." in parts:
            continue
        out.append(info)
    return out


def extract_package(zip_path: str | Path, dest_dir: str | Path) -> tuple[bool, str]:
    """把安装包解压到 dest_dir（已做 zip slip 过滤）。返回 (ok, message)。"""
    src, dest = Path(zip_path), Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(src) as zf:
            members = _safe_members(zf)
            if not members:
                return False, "安装包内没有可用文件"
            for info in members:
                rel = Path(info.filename.replace("\\", "/"))
                out = dest / rel
                out.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as fsrc, out.open("wb") as fdst:
                    shutil.copyfileobj(fsrc, fdst)
    except zipfile.BadZipFile:
        return False, "安装包不是有效的 zip"
    except OSError as e:
        return False, str(e)
    return True, "ok"


def _copy_with_retry(src: Path, dst: Path) -> None:
    """复制文件；遇到 Windows 文件占用（WinError 32）时退避重试。"""
    last: OSError | None = None
    for delay in (0.0, *_RETRY_DELAYS):
        if delay:
            time.sleep(delay)
        try:
            shutil.copy2(src, dst)
            return
        except PermissionError as e:  # 目标正被占用，等句柄释放
            last = e
        except OSError as e:
            if getattr(e, "winerror", None) != 32:
                raise
            last = e
    raise last if last else OSError(f"复制失败: {src} -> {dst}")


def is_protected(name: str) -> bool:
    """该文件是否必须保留目标机上的原件（不被安装包同名文件覆盖）。"""
    return Path(name).name.lower() in PROTECTED_NAMES


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


def backup_root(target_exe: str | Path) -> Path:
    """某个实例的备份根目录（exe 同级的 .backups）。"""
    return Path(target_exe).parent / BACKUP_DIRNAME


def backup_dir_for(target_exe: str | Path, version: str) -> Path:
    """某个版本的备份目录；版本号为空时归入 unknown。"""
    return backup_root(target_exe) / (str(version).strip() or "unknown")


def list_local_backups(target_exe: str | Path) -> list[str]:
    """列出本机可回滚的版本（按名称倒序，新版在前）。"""
    root = backup_root(target_exe)
    if not root.is_dir():
        return []
    names = [d.name for d in root.iterdir() if d.is_dir() and (d / Path(target_exe).name).is_file()]
    return sorted(names, reverse=True)


def backup_current(target_exe: str | Path) -> tuple[bool, str, str]:
    """把当前 exe 与 version.txt 一起备份到 .backups/<当前版本>/。

    两个文件必须一起备份：只存 exe 的话，恢复后 version.txt 还停留在新版本号，
    面板与后台看到的版本会与实际运行的程序对不上。

    返回 (是否成功, 备份的版本号, 失败原因)。
    """
    exe = Path(target_exe)
    if not exe.is_file():
        return False, "", f"目标程序不存在: {exe}"
    version = read_version_near(exe)
    dest = backup_dir_for(exe, version)
    try:
        dest.mkdir(parents=True, exist_ok=True)
        _copy_with_retry(exe, dest / exe.name)
        ver_txt = exe.parent / "version.txt"
        if ver_txt.is_file():
            _copy_with_retry(ver_txt, dest / "version.txt")
    except OSError as e:
        return False, version, str(e)
    return True, version, ""


def apply_package(
    package_dir: str | Path,
    target_exe: str | Path,
    *,
    backup: bool = True,
) -> tuple[bool, str]:
    """把解压后的安装包目录按文件树覆盖到目标程序所在目录。

    保留 `.env`；覆盖前先做一次版本化备份，供本机回滚。返回 (ok, message)。
    """
    src_root = Path(package_dir)
    exe = Path(target_exe)
    if not src_root.is_dir():
        return False, f"安装包目录不存在: {src_root}"

    files = [p for p in src_root.rglob("*") if p.is_file()]
    if not files:
        return False, "安装包内没有文件"

    if backup:
        ok, _version, err = backup_current(exe)
        if not ok and exe.is_file():
            return False, f"备份失败，已放弃替换：{err}"

    dst_root = exe.parent
    dst_root.mkdir(parents=True, exist_ok=True)
    copied = 0
    try:
        for src in files:
            rel = src.relative_to(src_root)
            if is_protected(rel.name):
                continue
            dst = dst_root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            _copy_with_retry(src, dst)
            copied += 1
    except OSError as e:
        return False, str(e)
    if copied == 0:
        return False, "安装包内没有可覆盖的文件"
    return True, f"已覆盖 {copied} 个文件"


def restore_backup(target_exe: str | Path, version: str) -> tuple[bool, str]:
    """从 .backups/<version>/ 恢复到目标目录（本机回滚）。

    恢复前先把当前版本另存一份，避免回滚后再想回到新版时无处可取。
    """
    exe = Path(target_exe)
    src = backup_dir_for(exe, version)
    if not (src / exe.name).is_file():
        return False, f"本机没有版本 {version} 的备份"
    if exe.is_file():
        backup_current(exe)
    return apply_package(src, exe, backup=False)


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
