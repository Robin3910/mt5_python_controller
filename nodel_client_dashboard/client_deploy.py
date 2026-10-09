"""客户端版本读取、备份与安装包覆盖（纯函数，供面板调用）。

覆盖安装的两条硬约束：
- `.env` 永不替换 —— 它承载节点身份（NODE_TOKEN / MT5 账号），覆盖即掉线；
- Windows 上正在运行的 exe 无法覆盖，调用方必须先停进程；即便停了，句柄释放
  也有滞后，所以写入统一走 `_copy_with_retry` 退避重试。
"""
from __future__ import annotations

import re
import json
import shutil
import tempfile
import time
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

# 版本化备份目录名；放在 exe 同级，回滚时按版本号取回
BACKUP_DIRNAME = ".backups"
CAPABILITIES_NAME = "client_capabilities.json"
SIDECAR_NAMES = ("version.txt", CAPABILITIES_NAME)

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


def _copy_reversibly(
    copies: list[tuple[Path, Path]], *, remove: tuple[Path, ...] = (),
) -> tuple[bool, str]:
    """先暂存本次所有目标原件；失败时撤回文件修改，不读取节点 .env。"""
    targets = list(dict.fromkeys([dst for _, dst in copies] + list(remove)))
    if any(is_protected(target.name) for target in targets):
        return False, "更新目标包含受保护的节点配置，已中止"
    temporary = Path(tempfile.mkdtemp(prefix="node_client_restore_")).resolve()
    if temporary.parent != Path(tempfile.gettempdir()).resolve() or not temporary.name.startswith("node_client_restore_"):
        raise RuntimeError("更新恢复目录不在预期临时目录内")
    keep_snapshot = False
    try:
        snapshots: dict[Path, Path | None] = {}
        try:
            for index, target in enumerate(targets):
                if target.exists():
                    snapshot = Path(temporary) / str(index)
                    shutil.copy2(target, snapshot)
                    snapshots[target] = snapshot
                else:
                    snapshots[target] = None
            (temporary / "recovery.json").write_text(json.dumps({str(target): str(snapshot) if snapshot else None for target, snapshot in snapshots.items()}, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            return False, f"准备更新恢复快照失败，未修改客户端：{exc}"

        touched: list[Path] = []
        try:
            for source, target in copies:
                target.parent.mkdir(parents=True, exist_ok=True)
                touched.append(target)  # 复制失败也可能已经截断目标文件。
                _copy_with_retry(source, target)
            for target in remove:
                touched.append(target)
                target.unlink(missing_ok=True)
        except OSError as exc:
            recovery_errors = []
            for target in reversed(list(dict.fromkeys(touched))):
                try:
                    snapshot = snapshots[target]
                    if snapshot is None:
                        target.unlink(missing_ok=True)
                    else:
                        _copy_with_retry(snapshot, target)
                except OSError as recovery_exc:
                    recovery_errors.append(str(recovery_exc))
            if recovery_errors:
                keep_snapshot = True
                return False, f"更新失败且文件恢复失败：{exc}；{'；'.join(recovery_errors)}；恢复原件保留在：{temporary}"
            return False, f"更新失败，已恢复原客户端文件：{exc}"
    finally:
        if not keep_snapshot:
            shutil.rmtree(temporary)
    return True, "ok"


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
    """替换时一并复制版本与绑定能力清单，保证清单哈希对应当前 exe。"""
    src = Path(source_exe)
    out = [src]
    for name in SIDECAR_NAMES:
        sidecar = src.parent / name
        if sidecar.is_file():
            out.append(sidecar)
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
    """把当前 exe、version.txt 与能力清单一起备份到 .backups/<当前版本>/。

    这些文件必须一起备份，保证恢复后版本号、清单哈希与程序对应。

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
        for name in SIDECAR_NAMES:
            source = exe.parent / name
            if source.is_file():
                _copy_with_retry(source, dest / name)
            else:
                # 同版本重做备份时，不能让上一次的能力清单伪装成本次 exe 的清单。
                (dest / name).unlink(missing_ok=True)
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

    files = sorted((p for p in src_root.rglob("*") if p.is_file()), key=lambda p: p.relative_to(src_root).as_posix().lower())
    if not files:
        return False, "安装包内没有文件"

    if backup:
        ok, _version, err = backup_current(exe)
        if not ok and exe.is_file():
            return False, f"备份失败，已放弃替换：{err}"

    dst_root = exe.parent
    dst_root.mkdir(parents=True, exist_ok=True)
    copies = [(src, dst_root / src.relative_to(src_root)) for src in files if not is_protected(src.name)]
    if not copies:
        return False, "安装包内没有可覆盖的文件"
    remove = ()
    if (src_root / exe.name).is_file() and not (src_root / CAPABILITIES_NAME).is_file():
        # 旧包不能继承新能力清单；该删除也必须能随失败撤回。
        remove = (dst_root / CAPABILITIES_NAME,)
    ok, message = _copy_reversibly(copies, remove=remove)
    return (True, f"已覆盖 {len(copies)} 个文件") if ok else (False, message)


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
    copies = []
    if backup and target_exe.is_file():
        copies.append((target_exe, target_exe.with_suffix(target_exe.suffix + ".bak")))
    copies.append((source_exe, target_exe))
    # 同步 version.txt 与当前 exe 对应的能力清单。
    for name in SIDECAR_NAMES:
        source = source_exe.parent / name
        if source.is_file():
            copies.append((source, target_exe.parent / name))
    remove = () if (source_exe.parent / CAPABILITIES_NAME).is_file() else (target_exe.parent / CAPABILITIES_NAME,)
    return _copy_reversibly(copies, remove=remove)
