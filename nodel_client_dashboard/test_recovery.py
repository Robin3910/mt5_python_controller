"""recovery 看门狗策略纯函数测试。"""
from __future__ import annotations

from recovery import (
    DEFAULT_MAX_RESTARTS,
    RecoveryPolicy,
    next_crash_count,
    should_restart,
)


def test_should_restart_limits():
    assert should_restart(0, 1, 3) is False
    assert should_restart(1, 1, 3) is True
    assert should_restart(1, 3, 3) is True
    assert should_restart(1, 4, 3) is False


def test_next_crash_count_and_stable_reset():
    assert next_crash_count(exit_code=0, prev_count=2, ran_s=1, stable_s=60) == 0
    assert next_crash_count(exit_code=1, prev_count=0, ran_s=1, stable_s=60) == 1
    assert next_crash_count(exit_code=1, prev_count=1, ran_s=1, stable_s=60) == 2
    # 稳定运行后再崩：重新从 1 计
    assert next_crash_count(exit_code=1, prev_count=3, ran_s=120, stable_s=60) == 1


def test_policy_defaults_and_env(monkeypatch):
    p = RecoveryPolicy.from_env()
    assert p.max_restarts == DEFAULT_MAX_RESTARTS
    assert p.delay_s == 5.0
    monkeypatch.setenv("DASHBOARD_RESTART_MAX", "5")
    monkeypatch.setenv("DASHBOARD_RESTART_DELAY_S", "2.5")
    p2 = RecoveryPolicy.from_env()
    assert p2.max_restarts == 5
    assert p2.delay_s == 2.5


def test_supervisor_restarts_then_gives_up(monkeypatch, tmp_path):
    import recovery as rec

    monkeypatch.setattr(rec, "supervisor_log_path", lambda: tmp_path / "sup.log")
    monkeypatch.setattr(rec, "child_command", lambda: [sys_exe(), "-c", "raise SystemExit(1)"])

    # 用假 Popen：第一次和第二次非 0，第三次仍非 0 后放弃
    calls = {"n": 0}

    class FakeProc:
        def wait(self):
            calls["n"] += 1
            return 1

    monkeypatch.setattr(
        rec.subprocess,
        "Popen",
        lambda *_a, **_k: FakeProc(),
    )
    monkeypatch.setattr(rec.time, "sleep", lambda _s: None)
    code = rec.run_supervisor(RecoveryPolicy(max_restarts=3, delay_s=0, stable_s=999))
    assert code == 1
    assert calls["n"] == 4  # 初始 1 次 + 重启 3 次


def test_supervisor_stops_on_clean_exit(monkeypatch, tmp_path):
    import recovery as rec

    monkeypatch.setattr(rec, "supervisor_log_path", lambda: tmp_path / "sup.log")

    class FakeProc:
        def wait(self):
            return 0

    monkeypatch.setattr(rec.subprocess, "Popen", lambda *_a, **_k: FakeProc())
    code = rec.run_supervisor(RecoveryPolicy(max_restarts=3, delay_s=0))
    assert code == 0


def sys_exe() -> str:
    import sys

    return sys.executable
