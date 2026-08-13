"""子进程启停、守护、健康轮询与日志落盘。"""
from __future__ import annotations

import json
import locale
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from models import InstanceConfig, RuntimeState
from node_log import DailyLogWriter, latest_log_path, log_path_for_day, node_logs_dir
from ports import find_free_port


OnChange = Callable[[str], None]


def decode_log_bytes(data: bytes) -> str:
    """解码子进程日志：优先 UTF-8，失败再试系统/GBK（中文 Windows 控制台常见）。"""
    if not data:
        return ""
    # 去掉 UTF-8 BOM
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    candidates: list[str] = []
    for enc in (
        "gbk",
        "cp936",
        "mbcs",
        locale.getpreferredencoding(False) or "",
    ):
        if enc and enc.lower() not in {c.lower() for c in candidates}:
            candidates.append(enc)
    for enc in candidates:
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def _http_json(method: str, url: str, timeout: float = 0.8) -> tuple[int, dict]:
    req = Request(url, method=method)
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else {}
    except HTTPError as e:
        body = e.read().decode("utf-8")
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            data = {}
        return e.code, data


class ManagedProcess:
    def __init__(self, cfg: InstanceConfig, on_change: OnChange | None = None) -> None:
        self.cfg = cfg
        self.runtime = RuntimeState()
        self._on_change = on_change or (lambda _id: None)
        self._proc: subprocess.Popen | None = None
        self._log_fp = None  # 兼容旧字段；现改用 DailyLogWriter + 管道泵
        self._log_writer: DailyLogWriter | None = None
        self._log_pump: threading.Thread | None = None
        self._lock = threading.RLock()
        self._want_running = False
        self._op_lock = threading.Lock()  # 串行化 start/stop，避免重入
        self._daemon_thread: threading.Thread | None = None
        self._poll_thread: threading.Thread | None = None
        self._stop_threads = threading.Event()

    @property
    def log_path(self) -> Path:
        """当前应展示/追加的日志文件（按天）。"""
        return log_path_for_day(self.cfg)

    def log_dir(self) -> Path:
        return node_logs_dir(self.cfg)

    def start(self) -> None:
        if not self._op_lock.acquire(blocking=False):
            return
        try:
            self.runtime.busy = "starting"
            self.runtime.last_error = ""
            self._notify()
            with self._lock:
                if self._proc is not None and self._proc.poll() is None:
                    self.runtime.busy = ""
                    self._notify()
                    return
            # 网络探测不持主锁，避免卡住健康轮询
            if self._status_reachable(timeout=0.4):
                data = self._fetch_status(timeout=0.4) or {}
                with self._lock:
                    self._want_running = True
                    self._apply_status_payload(data)
                    self.runtime.process_alive = True
                    self.runtime.health = "ok"
                    self.runtime.busy = ""
                    self._ensure_workers()
                self._notify()
                return
            with self._lock:
                self._want_running = True
                self._spawn()
                self._ensure_workers()
                self.runtime.busy = ""
            self._notify()
        except Exception as e:  # noqa: BLE001
            self.runtime.busy = ""
            self.runtime.health = "error"
            self.runtime.last_error = str(e)
            self._notify()
        finally:
            self._op_lock.release()

    def stop(self, *, graceful: bool = True) -> None:
        if not self._op_lock.acquire(blocking=False):
            return
        try:
            self.runtime.busy = "stopping"
            self._notify()
            with self._lock:
                self._want_running = False
                port = self.cfg.status_port
                proc = self._proc
                pid = self.runtime.pid
            if graceful and port > 0:
                try:
                    _http_json("POST", f"http://127.0.0.1:{port}/stop", timeout=0.6)
                except (URLError, OSError, TimeoutError, json.JSONDecodeError):
                    pass
                # 短轮询：单次探测超时很短，总等待 ≤3s，不阻塞其它实例
                deadline = time.time() + 3.0
                while time.time() < deadline:
                    if not self._status_reachable(timeout=0.25):
                        break
                    if proc is not None and proc.poll() is not None:
                        break
                    time.sleep(0.15)
            with self._lock:
                proc = self._proc
                if proc is not None and proc.poll() is None:
                    try:
                        proc.terminate()
                    except OSError:
                        pass
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        try:
                            proc.kill()
                        except OSError:
                            pass
                        try:
                            proc.wait(timeout=1)
                        except subprocess.TimeoutExpired:
                            pass
                elif pid and self._status_reachable(timeout=0.25):
                    self._force_kill_pid(pid)
                self._cleanup_proc()
                self.runtime.process_alive = False
                self.runtime.pid = None
                self.runtime.health = "dead"
                self.runtime.ws_state = None
                self.runtime.busy = ""
            self._notify()
        except Exception as e:  # noqa: BLE001
            self.runtime.busy = ""
            self.runtime.last_error = str(e)
            self._notify()
        finally:
            self._op_lock.release()

    def recover(self) -> None:
        """面板启动时：按已保存 status_port 探测并接管仍在跑的客户端。"""
        self._ensure_workers()
        self._refresh_file_version()
        # 短超时，避免加载多个实例时卡住 UI 启动
        data = self._fetch_status(timeout=0.3)
        if data is not None:
            self.runtime.process_alive = True
            self.runtime.health = "ok"
            self._apply_status_payload(data)
            self._want_running = True
            self._notify()

    def set_daemon(self, enabled: bool) -> None:
        with self._lock:
            self.cfg.daemon = enabled
            if enabled:
                self._ensure_workers()
            self._notify()

    def refresh_health(self) -> None:
        self._poll_once()

    def read_log_tail(self, max_bytes: int = 64_000) -> str:
        path = latest_log_path(self.cfg)
        if path is None or not path.exists():
            return ""
        data = path.read_bytes()
        if len(data) > max_bytes:
            # 避免截断落在多字节字符中间：向前多留一点再解码
            start = len(data) - max_bytes
            data = data[start:]
            # 若首字节像 UTF-8 续字节，跳过至下一可能边界
            while data and (data[0] & 0xC0) == 0x80:
                data = data[1:]
        return decode_log_bytes(data)

    def clear_log_display_file(self) -> None:
        """清空当日日志文件内容（保留文件）。"""
        path = log_path_for_day(self.cfg)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")

    def shutdown_workers(self) -> None:
        self._stop_threads.set()
        self._want_running = False

    def _fetch_status(self, timeout: float = 0.6) -> dict | None:
        port = self.cfg.status_port
        if port <= 0:
            return None
        try:
            code, data = _http_json(
                "GET", f"http://127.0.0.1:{port}/status", timeout=timeout
            )
            if code == 200 and isinstance(data, dict):
                return data
        except (URLError, OSError, TimeoutError, json.JSONDecodeError):
            return None
        return None

    def _status_reachable(self, timeout: float = 0.4) -> bool:
        data = self._fetch_status(timeout=timeout)
        return bool(data and data.get("ok", True) and data.get("process") == "alive")

    def _refresh_file_version(self) -> None:
        from client_deploy import read_version_near

        ver = read_version_near(self.cfg.exe_path)
        if ver:
            self.runtime.version = ver

    def _apply_status_payload(self, data: dict) -> None:
        self.runtime.ws_state = data.get("ws_state")
        self.runtime.node_id = data.get("node_id")
        self.runtime.mt5_login = data.get("mt5_login")
        self.runtime.uptime_s = data.get("uptime_s")
        ver = data.get("version")
        if ver:
            self.runtime.version = str(ver)
        else:
            self._refresh_file_version()
        runners = data.get("runners") or []
        self.runtime.runners = runners if isinstance(runners, list) else []
        raw_pid = data.get("pid")
        try:
            self.runtime.pid = int(raw_pid) if raw_pid is not None else self.runtime.pid
        except (TypeError, ValueError):
            pass
        self.runtime.last_error = ""

    @staticmethod
    def _force_kill_pid(pid: int) -> None:
        if pid <= 0:
            return
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    creationflags=subprocess.CREATE_NO_WINDOW,  # type: ignore[attr-defined]
                    timeout=3,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
        else:
            try:
                os.kill(pid, 15)
            except OSError:
                pass

    def _spawn(self) -> None:
        exe = Path(self.cfg.exe_path)
        cwd = Path(self.cfg.cwd or exe.parent)
        if not exe.exists():
            self.runtime.health = "error"
            self.runtime.last_error = f"可执行文件不存在: {exe}"
            self._notify()
            return
        if self.cfg.status_port <= 0:
            used = set()
            self.cfg.status_port = find_free_port(preferred=18765)
            # prefer unique — caller should assign; fallback ok
            _ = used

        env = os.environ.copy()
        env["LOCAL_STATUS_HOST"] = "127.0.0.1"
        env["LOCAL_STATUS_PORT"] = str(self.cfg.status_port)
        # 强制子进程 stdout 用 UTF-8，避免中文 Windows 控制台码页写进日志文件
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"

        # 关闭上一轮日志泵
        self._stop_log_pump()
        self._log_writer = DailyLogWriter(self.cfg)
        self._log_writer.write_line(
            f"[panel] starting {exe.name} cwd={cwd} port={self.cfg.status_port}"
        )

        creationflags = 0
        if os.name == "nt":
            # 无控制台后台启动（不弹黑窗）；日志走面板 logs/<节点>/按天
            creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]

        if exe.suffix.lower() == ".py":
            py = os.environ.get("PYTHON") or sys.executable
            cmd = [py, str(exe)]
        else:
            cmd = [str(exe)]

        try:
            self._proc = subprocess.Popen(
                cmd,
                cwd=str(cwd),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
                bufsize=0,
            )
        except OSError as e:
            self.runtime.health = "error"
            self.runtime.last_error = str(e)
            self._cleanup_proc()
            self._notify()
            return

        self._log_pump = threading.Thread(
            target=self._pump_stdout,
            name=f"log-{self.cfg.id}",
            daemon=True,
        )
        self._log_pump.start()

        self.runtime.pid = self._proc.pid
        self.runtime.process_alive = True
        self.runtime.health = "starting"
        self.runtime.last_error = ""
        self._notify()

    def _pump_stdout(self) -> None:
        """实时把子进程 stdout 写入按天轮转日志。"""
        proc = self._proc
        writer = self._log_writer
        if proc is None or proc.stdout is None or writer is None:
            return
        try:
            while True:
                chunk = proc.stdout.read(4096)
                if not chunk:
                    break
                writer.write(chunk)
        except Exception as e:  # noqa: BLE001
            try:
                writer.write_line(f"[panel] log pump error: {e}")
            except Exception:  # noqa: BLE001
                pass
        finally:
            try:
                proc.stdout.close()
            except Exception:  # noqa: BLE001
                pass

    def _stop_log_pump(self) -> None:
        # 等泵读完 stdout 再关 writer，避免截断/竞态
        pump = self._log_pump
        if pump is not None and pump.is_alive() and pump is not threading.current_thread():
            pump.join(timeout=2.0)
        if self._log_writer is not None:
            try:
                self._log_writer.close()
            except Exception:  # noqa: BLE001
                pass
            self._log_writer = None
        self._log_pump = None

    def _cleanup_proc(self) -> None:
        self._proc = None
        self._stop_log_pump()
        if self._log_fp is not None:
            try:
                self._log_fp.close()
            except OSError:
                pass
            self._log_fp = None

    def _ensure_workers(self) -> None:
        self._stop_threads.clear()
        if self._daemon_thread is None or not self._daemon_thread.is_alive():
            self._daemon_thread = threading.Thread(
                target=self._daemon_loop, name=f"daemon-{self.cfg.id}", daemon=True
            )
            self._daemon_thread.start()
        if self._poll_thread is None or not self._poll_thread.is_alive():
            self._poll_thread = threading.Thread(
                target=self._poll_loop, name=f"poll-{self.cfg.id}", daemon=True
            )
            self._poll_thread.start()

    def _daemon_loop(self) -> None:
        while not self._stop_threads.is_set():
            time.sleep(0.5)
            with self._lock:
                want = self._want_running
                daemon = self.cfg.daemon
                delay = max(1.0, float(self.cfg.restart_delay_s or 5.0))
                proc = self._proc
                owned_alive = proc is not None and proc.poll() is None
            if not want:
                continue
            if owned_alive:
                continue
            # 无本面板持有的 Popen：若状态口仍在，视为外部/已接管进程，勿重复拉起
            if self._status_reachable():
                continue
            # process exited
            with self._lock:
                if self._proc is not None and self._proc.poll() is not None:
                    self._cleanup_proc()
                self.runtime.process_alive = False
                self.runtime.pid = None
                if not daemon:
                    self.runtime.health = "dead"
                    self._want_running = False
                    self._notify()
                    continue
                self.runtime.health = "starting"
                self._notify()
            if daemon and self._want_running:
                time.sleep(delay)
                if self._stop_threads.is_set() or not self._want_running:
                    continue
                with self._lock:
                    if self.cfg.daemon and self._want_running and (
                        (self._proc is None or self._proc.poll() is not None)
                        and not self._status_reachable()
                    ):
                        self._spawn()

    def _poll_loop(self) -> None:
        while not self._stop_threads.is_set():
            self._poll_once()
            self._stop_threads.wait(2.0)

    def _poll_once(self) -> None:
        with self._lock:
            proc = self._proc
            port = self.cfg.status_port
            owned_alive = proc is not None and proc.poll() is None
            if owned_alive and proc is not None:
                self.runtime.process_alive = True
                self.runtime.pid = proc.pid
        if owned_alive:
            if port <= 0:
                self.runtime.health = "starting"
                self._notify()
                return
            data = self._fetch_status()
            if data is None:
                self.runtime.health = "starting"
                self.runtime.last_error = "status unreachable"
            else:
                self.runtime.health = "ok"
                self._apply_status_payload(data)
            self._notify()
            return

        # 面板重启后无 Popen：靠 status_port 接管
        data = self._fetch_status() if port > 0 else None
        if data is not None:
            self.runtime.process_alive = True
            self.runtime.health = "ok"
            self._apply_status_payload(data)
            self._notify()
            return

        self.runtime.process_alive = False
        if self.runtime.health not in ("starting", "error") and not self._want_running:
            self.runtime.health = "dead"
        elif self._want_running and self.cfg.daemon:
            self.runtime.health = "starting"
        else:
            self.runtime.health = "dead"
        self.runtime.ws_state = None
        self._refresh_file_version()
        self._notify()

    def _notify(self) -> None:
        try:
            self._on_change(self.cfg.id)
        except Exception:  # noqa: BLE001
            pass


class ProcessManager:
    def __init__(self, on_change: OnChange | None = None) -> None:
        self._on_change = on_change or (lambda _id: None)
        self._items: dict[str, ManagedProcess] = {}

    def load(self, configs: list[InstanceConfig]) -> None:
        for cfg in configs:
            if cfg.id not in self._items:
                self._items[cfg.id] = ManagedProcess(cfg, on_change=self._on_change)
            self._items[cfg.id].recover()

    def configs(self) -> list[InstanceConfig]:
        return [m.cfg for m in self._items.values()]

    def get(self, instance_id: str) -> ManagedProcess | None:
        return self._items.get(instance_id)

    def add(self, cfg: InstanceConfig) -> ManagedProcess:
        used = {m.cfg.status_port for m in self._items.values() if m.cfg.status_port > 0}
        if cfg.status_port <= 0 or cfg.status_port in used:
            from ports import allocate_ports

            cfg.status_port = allocate_ports(used, 1)[0]
        mp = ManagedProcess(cfg, on_change=self._on_change)
        self._items[cfg.id] = mp
        return mp

    def remove(self, instance_id: str) -> None:
        mp = self._items.pop(instance_id, None)
        if mp is None:
            return
        mp.stop(graceful=True)
        mp.shutdown_workers()

    def start(self, instance_id: str) -> None:
        mp = self._items.get(instance_id)
        if mp:
            mp.start()

    def stop(self, instance_id: str) -> None:
        mp = self._items.get(instance_id)
        if mp:
            mp.stop(graceful=True)

    def start_all(self) -> list[tuple[str, str, bool, str]]:
        """批量启动全部实例。返回 [(id, name, ok, message), ...]。"""
        results: list[tuple[str, str, bool, str]] = []
        for mp in list(self._items.values()):
            name = mp.cfg.name
            iid = mp.cfg.id
            try:
                if mp.runtime.busy:
                    results.append((iid, name, False, "忙碌中，已跳过"))
                    continue
                if mp.runtime.process_alive or mp._status_reachable(timeout=0.3):
                    results.append((iid, name, True, "已在运行"))
                    continue
                mp.start()
                results.append((iid, name, True, "已启动"))
            except Exception as e:  # noqa: BLE001
                results.append((iid, name, False, str(e)))
        return results

    def stop_all(self) -> list[tuple[str, str, bool, str]]:
        """批量停止全部实例。返回 [(id, name, ok, message), ...]。"""
        results: list[tuple[str, str, bool, str]] = []
        for mp in list(self._items.values()):
            name = mp.cfg.name
            iid = mp.cfg.id
            try:
                if mp.runtime.busy:
                    results.append((iid, name, False, "忙碌中，已跳过"))
                    continue
                alive = (
                    mp.runtime.process_alive
                    or (mp._proc is not None and mp._proc.poll() is None)
                    or mp._status_reachable(timeout=0.3)
                )
                if not alive:
                    results.append((iid, name, True, "已是停止状态"))
                    continue
                mp.stop(graceful=True)
                results.append((iid, name, True, "已停止"))
            except Exception as e:  # noqa: BLE001
                results.append((iid, name, False, str(e)))
        return results

    def set_daemon_all(self, enabled: bool) -> list[tuple[str, str, bool, str]]:
        """批量开启/关闭全部实例的守护进程。返回 [(id, name, ok, message), ...]。"""
        results: list[tuple[str, str, bool, str]] = []
        label_on = "已开启守护"
        label_off = "已关闭守护"
        already = "已是开启" if enabled else "已是关闭"
        for mp in list(self._items.values()):
            name = mp.cfg.name
            iid = mp.cfg.id
            try:
                if bool(mp.cfg.daemon) is bool(enabled):
                    results.append((iid, name, True, already))
                    continue
                mp.set_daemon(enabled)
                results.append((iid, name, True, label_on if enabled else label_off))
            except Exception as e:  # noqa: BLE001
                results.append((iid, name, False, str(e)))
        return results

    def _select(self, instance_ids: list[str] | None) -> list[ManagedProcess]:
        targets = list(self._items.values())
        if instance_ids is None:
            return targets
        id_set = set(instance_ids)
        return [m for m in targets if m.cfg.id in id_set]

    def _swap(
        self,
        mp: ManagedProcess,
        apply_fn: Callable[[Path], tuple[bool, str]],
        *,
        restart_if_was_running: bool,
        ok_label: str,
    ):
        """单实例的「停 → 覆盖 → 按需重启」流程；apply_fn 负责实际写文件。"""
        from client_deploy import ReplaceResult, read_version_near

        old_ver = read_version_near(mp.cfg.exe_path) or mp.runtime.version
        was_running = bool(
            mp.runtime.process_alive
            or (mp._proc is not None and mp._proc.poll() is None)
            or mp._status_reachable(timeout=0.3)
        )
        if was_running:
            mp.stop(graceful=True)
            # 等文件句柄释放（Windows 上正在运行的 exe 无法覆盖）
            time.sleep(0.4)

        ok, msg = apply_fn(Path(mp.cfg.exe_path))
        restarted = False
        if ok:
            mp._refresh_file_version()
            if was_running and restart_if_was_running:
                mp.start()
                restarted = True
        result = ReplaceResult(
            instance_id=mp.cfg.id,
            name=mp.cfg.name,
            target=mp.cfg.exe_path,
            ok=ok,
            message=msg if not ok else (ok_label + ("并重启" if restarted else "")),
            old_version=old_ver,
            new_version=read_version_near(mp.cfg.exe_path),
            was_running=was_running,
            restarted=restarted,
        )
        mp._notify()
        return result

    def _busy_skip(self, mp: ManagedProcess):
        """启停中的实例不参与替换，避免与 start/stop 抢同一个 exe 文件。"""
        from client_deploy import ReplaceResult, read_version_near

        return ReplaceResult(
            instance_id=mp.cfg.id,
            name=mp.cfg.name,
            target=mp.cfg.exe_path,
            ok=False,
            message="忙碌中，已跳过",
            old_version=read_version_near(mp.cfg.exe_path) or mp.runtime.version,
        )

    def batch_replace(
        self,
        source_exe: str | Path,
        *,
        instance_ids: list[str] | None = None,
        restart_if_was_running: bool = True,
    ) -> list:
        """批量替换客户端 exe：停 → 备份覆盖 →（可选）再启。"""
        from client_deploy import replace_exe_file

        src = Path(source_exe)
        results = []
        for mp in self._select(instance_ids):
            if mp.runtime.busy:
                results.append(self._busy_skip(mp))
                continue
            results.append(
                self._swap(
                    mp,
                    lambda target: replace_exe_file(
                        source_exe=src, target_exe=target, backup=True
                    ),
                    restart_if_was_running=restart_if_was_running,
                    ok_label="已替换",
                )
            )
        return results

    def batch_update(
        self,
        package_dir: str | Path,
        *,
        instance_ids: list[str] | None = None,
        restart_if_was_running: bool = True,
    ) -> list:
        """用解压好的安装包目录批量更新：停 → 版本化备份 → 按文件树覆盖 → 按需再启。

        与 batch_replace 的区别是覆盖粒度：这里按目录整体覆盖（onedir 形态也能更新），
        并且保留 .env。
        """
        from client_deploy import apply_package

        src = Path(package_dir)
        results = []
        for mp in self._select(instance_ids):
            if mp.runtime.busy:
                results.append(self._busy_skip(mp))
                continue
            results.append(
                self._swap(
                    mp,
                    lambda target: apply_package(src, target, backup=True),
                    restart_if_was_running=restart_if_was_running,
                    ok_label="已更新",
                )
            )
        return results

    def batch_rollback(
        self,
        version: str,
        *,
        instance_ids: list[str] | None = None,
        restart_if_was_running: bool = True,
    ) -> list:
        """从本机备份目录回滚到指定版本（不依赖网络）。"""
        from client_deploy import restore_backup

        results = []
        for mp in self._select(instance_ids):
            if mp.runtime.busy:
                results.append(self._busy_skip(mp))
                continue
            results.append(
                self._swap(
                    mp,
                    lambda target: restore_backup(target, version),
                    restart_if_was_running=restart_if_was_running,
                    ok_label=f"已回滚到 {version}",
                )
            )
        return results

    def shutdown_all(self) -> None:
        for mp in list(self._items.values()):
            mp.shutdown_workers()
            if mp.runtime.process_alive:
                mp.stop(graceful=True)
