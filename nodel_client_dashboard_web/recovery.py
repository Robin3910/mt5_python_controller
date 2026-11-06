"""运维面板本体崩溃自恢复（看门狗）。

父进程监督子进程：非 0 退出视为崩溃，按间隔重启，默认最多 3 次。
正常退出（托盘「退出」、单实例提示后退出）不重启。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


ENV_CHILD = "DASHBOARD_SUPERVISED_CHILD"
ENV_NO_SUPERVISOR = "DASHBOARD_NO_SUPERVISOR"
ENV_MAX = "DASHBOARD_RESTART_MAX"
ENV_DELAY = "DASHBOARD_RESTART_DELAY_S"
ENV_STABLE = "DASHBOARD_RESTART_STABLE_S"

DEFAULT_MAX_RESTARTS = 3
DEFAULT_DELAY_S = 5.0
DEFAULT_STABLE_S = 60.0  # 连续运行超过该秒数后，崩溃计数清零再计


@dataclass(frozen=True)
class RecoveryPolicy:
    max_restarts: int = DEFAULT_MAX_RESTARTS
    delay_s: float = DEFAULT_DELAY_S
    stable_s: float = DEFAULT_STABLE_S

    @classmethod
    def from_env(cls) -> "RecoveryPolicy":
        return cls(
            max_restarts=max(0, _env_int(ENV_MAX, DEFAULT_MAX_RESTARTS)),
            delay_s=max(0.0, _env_float(ENV_DELAY, DEFAULT_DELAY_S)),
            stable_s=max(0.0, _env_float(ENV_STABLE, DEFAULT_STABLE_S)),
        )


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def is_supervised_child() -> bool:
    return os.environ.get(ENV_CHILD, "").strip() == "1"


def supervisor_disabled() -> bool:
    return os.environ.get(ENV_NO_SUPERVISOR, "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def should_restart(exit_code: int, crash_count: int, max_restarts: int) -> bool:
    """exit_code==0 视为正常退出；crash_count 为「已发生的崩溃次数」。"""
    if exit_code == 0:
        return False
    return crash_count <= max_restarts


def next_crash_count(
    *,
    exit_code: int,
    prev_count: int,
    ran_s: float,
    stable_s: float,
) -> int:
    """根据本次退出更新崩溃计数；稳定运行后重新从 1 计。"""
    if exit_code == 0:
        return 0
    if ran_s >= stable_s > 0:
        return 1
    return prev_count + 1


def child_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable]
    # 开发态：以本包 main 为入口
    main_py = Path(__file__).resolve().parent / "main.py"
    return [sys.executable, str(main_py)]


def supervisor_log_path() -> Path:
    from store import data_dir

    d = data_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d / "dashboard_supervisor.log"


def _log(line: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    text = f"[{stamp}] {line}\n"
    try:
        with open(supervisor_log_path(), "a", encoding="utf-8") as fp:
            fp.write(text)
    except OSError:
        pass


def run_supervised_child(run_app) -> int:
    """子进程：跑 UI，返回退出码（0=正常）。"""
    try:
        code = run_app()
        return int(code if code is not None else 0)
    except SystemExit as e:
        code = e.code
        if code is None:
            return 0
        if isinstance(code, int):
            return code
        return 1
    except BaseException as e:  # noqa: BLE001 — 崩溃需上报非 0
        _log(f"child crash: {type(e).__name__}: {e}")
        return 1


def run_supervisor(policy: RecoveryPolicy | None = None) -> int:
    """父进程：拉起子进程，崩溃则按策略重启。"""
    policy = policy or RecoveryPolicy.from_env()
    crash_count = 0
    cmd = child_command()
    _log(
        f"supervisor start max={policy.max_restarts} delay={policy.delay_s}s "
        f"stable={policy.stable_s}s cmd={cmd}"
    )

    while True:
        env = os.environ.copy()
        env[ENV_CHILD] = "1"
        # 避免孙子进程再套一层看门狗
        env.pop(ENV_NO_SUPERVISOR, None)
        creationflags = 0
        if os.name == "nt" and getattr(sys, "frozen", False):
            # 子进程同样是 GUI 子系统，无需 CREATE_NO_WINDOW
            creationflags = 0

        started = time.monotonic()
        try:
            proc = subprocess.Popen(cmd, env=env, creationflags=creationflags)
        except OSError as e:
            _log(f"spawn failed: {e}")
            return 1

        exit_code = int(proc.wait())
        ran_s = time.monotonic() - started
        _log(f"child exit code={exit_code} ran={ran_s:.1f}s crashes={crash_count}")

        if exit_code == 0:
            _log("clean exit, supervisor stop")
            return 0

        crash_count = next_crash_count(
            exit_code=exit_code,
            prev_count=crash_count,
            ran_s=ran_s,
            stable_s=policy.stable_s,
        )
        if crash_count > policy.max_restarts:
            _log(
                f"give up after {crash_count} crashes "
                f"(limit={policy.max_restarts})"
            )
            return exit_code or 1

        _log(
            f"restart #{crash_count}/{policy.max_restarts} "
            f"in {policy.delay_s}s"
        )
        if policy.delay_s > 0:
            time.sleep(policy.delay_s)


def main(run_app) -> int:
    """入口分流：子进程跑 UI，父进程做看门狗。"""
    if supervisor_disabled() or is_supervised_child():
        return run_supervised_child(run_app)
    return run_supervisor()
