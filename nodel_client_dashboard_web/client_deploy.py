"""客户端版本读取、备份与安装包覆盖（纯函数，供面板调用）。

覆盖安装的两条硬约束：
- `.env` 永不替换 —— 它承载节点身份（NODE_TOKEN / MT5 账号），覆盖即掉线；
- Windows 上正在运行的 exe 无法覆盖，调用方必须先停进程；即便停了，句柄释放
  也有滞后，所以写入统一走 `_copy_with_retry` 退避重试。
"""
from __future__ import annotations

import os
import re
import json
import shutil
import subprocess
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


def image_is_node_client(image: str, directory: str | Path) -> bool:
    """镜像是否为安装目录里的 node_client.exe。不匹配同目录的 terminal64.exe。"""
    if not image or os.path.basename(image).lower() != "node_client.exe":
        return False
    root = os.path.normcase(os.path.abspath(directory))
    img = os.path.normcase(os.path.abspath(image))
    if img == os.path.normcase(os.path.join(root, "node_client.exe")):
        return True
    from store import canonical_dir

    return canonical_dir(image) == canonical_dir(str(directory))


def select_node_client_pids(images: Iterable[tuple[int, str]], directory: str | Path) -> list[int]:
    me = os.getpid()
    found: list[int] = []
    for pid, image in images:
        if pid <= 0 or pid == me or pid in found:
            continue
        if image_is_node_client(image, directory):
            found.append(pid)
    return found


def file_is_busy(path: str | Path) -> bool:
    """目标能否被独占写入。只有共享冲突（WinError 32）算占用，权限不足不算。"""
    target = Path(path)
    if os.name != "nt" or not target.is_file():
        return False
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    handle = kernel32.CreateFileW(str(target), 0x40000000, 0, None, 3, 0, None)
    if handle != wintypes.HANDLE(-1).value:
        kernel32.CloseHandle(handle)
        return False
    return ctypes.get_last_error() == 32


def list_process_images() -> list[tuple[int, str]]:
    """本机进程 (pid, 可执行文件完整路径)。查询失败的进程跳过。"""
    if os.name != "nt":
        return []
    try:
        return _list_process_images_win()
    except OSError:
        return []


def _list_process_images_win() -> list[tuple[int, str]]:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    TH32CS_SNAPPROCESS = 0x00000002
    INVALID = wintypes.HANDLE(-1).value
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID:
        raise OSError("无法枚举进程")
    images: list[tuple[int, str]] = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            pid = int(entry.th32ProcessID)
            image = _process_image(kernel32, pid)
            if image:
                images.append((pid, image))
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    return images


def _process_image(kernel32, pid: int) -> str:
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if handle == wintypes.HANDLE(-1).value or not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buf))
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return ""
        return buf.value
    finally:
        kernel32.CloseHandle(handle)


def kill_pid_tree(pid: int) -> None:
    if pid <= 0 or pid == os.getpid():
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                creationflags=subprocess.CREATE_NO_WINDOW,  # type: ignore[attr-defined]
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        return
    try:
        os.kill(pid, 15)
    except OSError:
        pass


def _rm_force_shutdown(files: list[Path]) -> None:
    """让重启管理器关掉占用这些文件的程序。只接受节点客户端文件。"""
    if os.name != "nt":
        return
    paths = [str(p) for p in files if p.name.lower() == "node_client.exe" and p.is_file()]
    if not paths:
        return
    import ctypes
    from ctypes import wintypes

    rstrtmgr = ctypes.WinDLL("Rstrtmgr", use_last_error=True)
    rstrtmgr.RmStartSession.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD, ctypes.c_wchar_p]
    rstrtmgr.RmStartSession.restype = wintypes.DWORD
    rstrtmgr.RmRegisterResources.argtypes = [
        wintypes.DWORD, wintypes.UINT, ctypes.POINTER(ctypes.c_wchar_p),
        wintypes.UINT, ctypes.c_void_p, wintypes.UINT, ctypes.c_void_p,
    ]
    rstrtmgr.RmRegisterResources.restype = wintypes.DWORD
    rstrtmgr.RmShutdown.argtypes = [wintypes.DWORD, wintypes.ULONG, ctypes.c_void_p]
    rstrtmgr.RmShutdown.restype = wintypes.DWORD
    rstrtmgr.RmEndSession.argtypes = [wintypes.DWORD]
    rstrtmgr.RmEndSession.restype = wintypes.DWORD

    handle = wintypes.DWORD()
    key = ctypes.create_unicode_buffer(32)
    if rstrtmgr.RmStartSession(ctypes.byref(handle), 0, key) != 0:
        return
    try:
        array = (ctypes.c_wchar_p * len(paths))(*paths)
        if rstrtmgr.RmRegisterResources(handle, len(paths), array, 0, None, 0, None) != 0:
            return
        rstrtmgr.RmShutdown(handle, 0x1, None)
    finally:
        rstrtmgr.RmEndSession(handle)


def release_client_processes(
    directory: str | Path,
    *,
    images: list[tuple[int, str]] | None = None,
    kill=kill_pid_tree,
    busy=file_is_busy,
    shutdown=None,
    timeout: float = 5,
) -> None:
    """结束该目录中的 node_client.exe，并等到 exe 不再被占用。

    找不到进程或结束后文件仍被占用时抛出 RuntimeError，调用方不得开始覆盖。
    """
    directory = Path(directory)
    exe = directory / "node_client.exe"
    if not exe.is_file():
        return
    pids = select_node_client_pids(list_process_images() if images is None else images, directory)
    for pid in pids:
        kill(pid)
    if busy(exe) and shutdown is not False:
        (shutdown or _rm_force_shutdown)([exe])
    deadline = time.time() + timeout
    while busy(exe) and time.time() < deadline:
        time.sleep(0.2)
    if busy(exe):
        raise RuntimeError(
            f"{exe} 仍被占用，导入已中止，没有修改该目录。"
            "请在任务管理器中结束该 node_client.exe 后重试。"
        )


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

    dst_root = exe.parent
    dst_root.mkdir(parents=True, exist_ok=True)
    copies = [(src, dst_root / src.relative_to(src_root)) for src in files if not is_protected(src.name)]
    if not copies:
        return False, "安装包内没有可覆盖的文件"
    busy = [dst for _src, dst in copies if file_is_busy(dst)]
    if busy:
        shown = "、".join(str(path) for path in busy[:5])
        return False, f"文件仍被占用，已中止且未修改客户端：{shown}"

    if backup:
        ok, _version, err = backup_current(exe)
        if not ok and exe.is_file():
            return False, f"备份失败，已放弃替换：{err}"
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
