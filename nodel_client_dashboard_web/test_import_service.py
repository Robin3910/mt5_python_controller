"""导入编排的单测：校验目录、解析账号、进度记录、任务状态机。

跑法：
    python -m pytest test_import_service.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import import_service as svc  # noqa: E402
from import_dialog import ImportController, ImportTask  # noqa: E402


# ─────────────────────────────────────────────── 目录校验


def test_check_directory_empty(tmp_path: Path) -> None:
    res = svc.check_directory("")
    assert res["ok"] is False
    assert "请先选择" in res["message"]


def test_check_directory_missing(tmp_path: Path) -> None:
    res = svc.check_directory(str(tmp_path / "nope"))
    assert res["ok"] is False
    assert "目录不存在" in res["message"]


def test_check_directory_without_terminal(tmp_path: Path) -> None:
    res = svc.check_directory(str(tmp_path))
    assert res["ok"] is False
    assert "不是 MT5 安装目录" in res["message"]


def test_check_directory_ok(tmp_path: Path) -> None:
    (tmp_path / "terminal64.exe").write_bytes(b"MZ")
    res = svc.check_directory(str(tmp_path))
    assert res["ok"] is True, res
    assert res["terminal"] == "terminal64.exe"
    assert res["suggested_name"] == tmp_path.name
    assert res["env_exists"] is False


def test_check_directory_reports_existing_client(tmp_path: Path) -> None:
    (tmp_path / "terminal64.exe").write_bytes(b"MZ")
    (tmp_path / "node_client.exe").write_bytes(b"MZ")
    (tmp_path / ".env").write_text("MANAGER_WS_URL=ws://x/ws/node\n", encoding="utf-8")
    res = svc.check_directory(str(tmp_path))
    assert res["ok"] is True
    assert res["env_exists"] is True
    assert "已有 .env" in res["message"]
    assert "已有客户端" in res["message"]


# ─────────────────────────────────────────────── 账号解析


def test_typed_login_valid() -> None:
    assert svc.resolve_account("C:/x", "50231488") == (50231488, "")


def test_typed_login_invalid() -> None:
    with pytest.raises(Exception):
        svc.resolve_account("C:/x", "abc")


def test_typed_login_blank_reads_terminal(tmp_path: Path, monkeypatch) -> None:
    """留空时读终端当前登录号。"""
    monkeypatch.setattr(
        svc, "read_terminal_account", lambda d: {"login": 778899, "server": "srv-x"}
    )
    login, server = svc.resolve_account(str(tmp_path), "")
    assert login == 778899
    assert server == "srv-x"


def test_typed_login_not_logged_in(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(svc, "read_terminal_account", lambda d: {"login": 0})
    with pytest.raises(svc.ImportError_):
        svc.resolve_account(str(tmp_path), "")


# ─────────────────────────────────────────────── 摘要


def test_summarize_mentions_env_overwrite(tmp_path: Path) -> None:
    (tmp_path / "terminal64.exe").write_bytes(b"MZ")
    (tmp_path / ".env").write_text("X=1\n", encoding="utf-8")
    req = svc.ImportRequest(
        directory=str(tmp_path), version="1.0.0", write_env=True, ws_url="ws://h/ws/node"
    )
    out = svc.summarize(req, login=42)
    assert out["ok"] is True
    assert out["env_exists"] is True
    assert "将整份覆盖" in out["text"]
    assert "MANAGER_WS_URL=ws://h/ws/node" in out["text"]


def test_summarize_without_env_write(tmp_path: Path) -> None:
    (tmp_path / "terminal64.exe").write_bytes(b"MZ")
    req = svc.ImportRequest(directory=str(tmp_path), version="1.0.0", write_env=False)
    out = svc.summarize(req, login=42)
    assert "不写入 .env" in out["text"]


# ─────────────────────────────────────────────── 写 .env 的返回值契约


def test_write_env_result_returns_tuple(tmp_path: Path) -> None:
    """`ef.write_env` 返回 None，包一层后必须给回 (ok, msg) 二元组。"""
    ok, msg = svc._write_env_result(tmp_path, "A=1\n")
    assert ok is True
    assert isinstance(msg, str)
    assert (tmp_path / ".env").read_text(encoding="utf-8") == "A=1\n"


def test_write_env_result_reports_oserror(tmp_path: Path, monkeypatch) -> None:
    """写失败要给出可读原因，而不是抛异常。"""

    def boom(*_a, **_k):
        raise OSError("磁盘只读")

    monkeypatch.setattr(svc.ef, "write_env", boom)
    ok, msg = svc._write_env_result(tmp_path, "A=1\n")
    assert ok is False
    assert "磁盘只读" in msg


def test_run_import_write_env_does_not_unpack_none(tmp_path: Path, monkeypatch) -> None:
    """回归：确认导入时 `ok, msg = perform(...)` 收到 None 会炸。

    这里让 perform 原样返回被调函数的返回值（模拟 session.perform），
    确保 edit_env 分支拿到的是二元组而不是 None。
    """
    (tmp_path / "terminal64.exe").write_bytes(b"MZ")
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "node_client.exe").write_bytes(b"MZ")
    (pkg / ".env.example").write_text("BASE=1\n", encoding="utf-8")

    monkeypatch.setattr(svc.vs, "download_package", lambda *a, **k: None)
    monkeypatch.setattr(svc.vs, "verify_sha256", lambda *a, **k: True)

    def fake_extract(_zip, dest):
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        for src in pkg.iterdir():
            (dest / src.name).write_bytes(src.read_bytes())
        return True, "ok"

    monkeypatch.setattr(svc, "extract_package", fake_extract)
    monkeypatch.setattr(svc, "require_binding", lambda _p: None)
    monkeypatch.setattr(svc, "read_algo_trading_allowed", lambda _d: True)

    def fake_apply(src, exe, backup=True):
        return True, "ok"

    monkeypatch.setattr(svc, "apply_package", fake_apply)

    # perform 原样返回 fn() 的结果——None 就会炸在这里
    def perform(action, cfg, fn, **kwargs):
        return fn()

    req = svc.ImportRequest(
        directory=str(tmp_path),
        name="demo",
        version="1.0.0",
        account_raw="12345",
        write_env=True,
        ws_url="ws://h/ws/node",
    )
    result = svc.run_import(
        req,
        target=object(),
        perform=perform,
        enroll=lambda *a, **k: None,
        stop_for_install=lambda *a, **k: None,
        register=lambda *a, **k: None,
    )
    assert result.ok is True
    assert result.env_written is True
    assert (tmp_path / ".env").is_file()


def test_run_import_edit_env_returning_none_is_tolerated(
    tmp_path: Path, monkeypatch
) -> None:
    """回归：即使 edit_env 的被调函数返回 None，导入也不能炸。

    这是线上报的 "cannot unpack non-iterable NoneType object"：
    `ef.write_env` 返回 None，被 `ok, msg = perform(...)` 直接解包。
    """
    (tmp_path / "terminal64.exe").write_bytes(b"MZ")
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "node_client.exe").write_bytes(b"MZ")

    monkeypatch.setattr(svc.vs, "download_package", lambda *a, **k: None)
    monkeypatch.setattr(svc.vs, "verify_sha256", lambda *a, **k: True)
    monkeypatch.setattr(svc, "require_binding", lambda _p: None)
    monkeypatch.setattr(svc, "read_algo_trading_allowed", lambda _d: True)
    monkeypatch.setattr(svc, "apply_package", lambda *a, **k: (True, "ok"))

    def fake_extract(_zip, dest):
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        for src in pkg.iterdir():
            (dest / src.name).write_bytes(src.read_bytes())
        return True, "ok"

    monkeypatch.setattr(svc, "extract_package", fake_extract)

    # 模拟旧的坏实现：写 .env 的被调函数返回 None
    def perform(action, cfg, fn, **kwargs):
        if action == "edit_env":
            fn()
            return None
        return fn()

    req = svc.ImportRequest(
        directory=str(tmp_path),
        name="demo",
        version="1.0.0",
        account_raw="12345",
        write_env=True,
        ws_url="ws://h/ws/node",
    )
    result = svc.run_import(
        req,
        target=object(),
        perform=perform,
        enroll=lambda *a, **k: None,
        stop_for_install=lambda *a, **k: None,
        register=lambda *a, **k: None,
    )
    assert result.ok is True
    assert result.env_written is True


def test_as_pair_handles_none() -> None:
    """None 不能直接解包——这正是报错的根因。"""
    ok, msg = svc._as_pair(None, "部署")
    assert ok is True
    assert msg == "ok"


def test_as_pair_handles_tuple() -> None:
    assert svc._as_pair((True, "ok"), "部署") == (True, "ok")
    assert svc._as_pair((False, "磁盘满了"), "部署") == (False, "磁盘满了")


def test_as_pair_handles_bool() -> None:
    assert svc._as_pair(True, "部署") == (True, "ok")
    assert svc._as_pair(False, "部署")[0] is False


def test_as_pair_rejects_garbage() -> None:
    ok, msg = svc._as_pair("乱码", "部署")
    assert ok is False
    assert "无法识别" in msg


# ─────────────────────────────────────────────── 任务状态机


class _FakeBridge:
    """够 ImportController 用的最小假桥。"""

    def __init__(self, ready: bool = True) -> None:
        self.session = type("S", (), {"check": lambda self: None})()
        self.manager = type("M", (), {"stop_for_install": lambda self, d: None})()
        self.added: list = []
        self.ready = ready

    def version_target(self):
        import version_service as vs

        if not self.ready:
            # 没配后端地址：base 与 token 都空 -> target.ready 为 False
            return vs.BackendTarget("", "", "用户登录", "Authorization", None)
        return vs.BackendTarget("http://h", "tok", "用户登录", "Authorization", None)


def test_import_controller_versions_without_backend() -> None:
    ctrl = ImportController(_FakeBridge(ready=False))
    out = ctrl.versions()
    assert out["ok"] is False
    assert "后端地址" in out["error"]
    assert out["versions"] == []


def test_import_controller_task_lifecycle() -> None:
    ctrl = ImportController(_FakeBridge())
    task = ImportTask(task_id="t1")
    ctrl._tasks["t1"] = task

    ctrl._record("t1", svc.STEP_DOWNLOAD, "下载安装包", 0.3, "正在下载")
    ctrl._record("t1", svc.STEP_DOWNLOAD, "下载安装包", 0.6, "50%")

    snap = task.snapshot()
    assert snap["task_id"] == "t1"
    assert snap["done"] is False
    assert len(snap["steps"]) == 1
    assert snap["steps"][0]["pct"] == pytest.approx(0.6)
    assert snap["steps"][0]["status"] == "active"
    assert len(snap["log"]) == 2


def test_import_controller_finish_marks_failed_step() -> None:
    ctrl = ImportController(_FakeBridge())
    ctrl._tasks["t2"] = ImportTask(task_id="t2")
    ctrl._record("t2", svc.STEP_DEPLOY, "部署", 0.8, "x")
    ctrl._finish("t2", error="磁盘满了")

    snap = ctrl.task("t2").snapshot()
    assert snap["done"] is True
    assert snap["error"] == "磁盘满了"
    assert snap["steps"][0]["status"] == "failed"


def test_import_controller_active_task_guard() -> None:
    """同一时间只允许一个导入。"""
    ctrl = ImportController(_FakeBridge())
    ctrl._tasks["t3"] = ImportTask(task_id="t3")
    ctrl._active = "t3"
    out = ctrl.start({"directory": "C:/x"})
    assert out["ok"] is False
    assert "还在进行中" in out["error"]


def test_import_controller_progress_missing_task() -> None:
    ctrl = ImportController(_FakeBridge())
    assert ctrl.task("nope") is None
