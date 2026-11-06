"""端到端验证：「确认导入」走真实 HTTP 链路时不会再炸在 None 解包。

背景（线上 bug）：
    `import_service.run_import` 里写 .env 那一步是
        ok, msg = perform("edit_env", cfg, lambda: ef.write_env(...))
    而 `ef.write_env` 返回 None，于是抛
        TypeError: cannot unpack non-iterable NoneType object

本脚本：
    1. 起真实的 DashboardBridge（真 HTTP 服务 + 真 ImportController）；
    2. 用假会话顶掉后端认证，但保留真实的 import 编排；
    3. POST /api/import/start，然后轮询 /api/import/progress；
    4. 断言最终 error 不含 "cannot unpack"，且导入走到写 .env 之后。

打包/下载被 monkeypatch 掉（不打真后端、不改真目录），
但写 .env 那一步是真的——那正是出问题的地方。
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import import_service as svc  # noqa: E402
from dashboard_bridge import DashboardBridge  # noqa: E402
from e2e_support import install_fake_session  # noqa: E402


def main() -> int:
    # ── 造一个假的 MT5 目录 ──
    work = Path(tempfile.mkdtemp(prefix="e2e_import_"))
    terminal_dir = work / "mt5"
    terminal_dir.mkdir()
    (terminal_dir / "terminal64.exe").write_bytes(b"MZ")

    pkg = work / "pkg"
    pkg.mkdir()
    (pkg / "node_client.exe").write_bytes(b"MZ")
    (pkg / ".env.example").write_text("BASE=1\n", encoding="utf-8")

    # ── 打掉真下载/真部署 ──
    svc.vs.download_package = lambda *a, **k: None  # type: ignore[assignment]
    svc.vs.verify_sha256 = lambda *a, **k: True  # type: ignore[assignment]
    svc.require_binding = lambda _p: None  # type: ignore[assignment]
    svc.read_algo_trading_allowed = lambda _d: True  # type: ignore[assignment]

    def fake_extract(_zip, dest):
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        for src in pkg.iterdir():
            (dest / src.name).write_bytes(src.read_bytes())
        return True, "ok"

    svc.extract_package = fake_extract  # type: ignore[assignment]

    def fake_apply(_src, _exe, backup=True):
        return True, "ok"

    svc.apply_package = fake_apply  # type: ignore[assignment]

    bridge = DashboardBridge()
    session = install_fake_session(bridge)
    # manager 自己也持一份 session 引用，注册实例时要用同一个假会话
    bridge.manager.session = session
    # 关掉自动登录：dist\.env 里配了账号密码，bridge.start() 会起后台线程
    # 真去连后端并把假会话顶掉，导致后续请求 403。
    bridge._load_auto_login = lambda: None
    bridge.start()
    base = f"http://127.0.0.1:{bridge.port}"
    token = bridge.token

    def post(path: str, payload: dict):
        url = f"{base}{path}?token={token}"
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def get(path: str):
        # path 可能自带查询串，用 & 拼 token
        sep = "&" if "?" in path else "?"
        url = f"{base}{path}{sep}token={token}"
        with urllib.request.urlopen(url, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))

    out: dict[str, object] = {}
    try:
        status, prepared = post(
            "/api/import/prepare",
            {
                "directory": str(terminal_dir),
                "name": "e2e",
                "version": "1.0.0",
                "account": "50231488",
                "write_env": True,
            },
        )
        out["prepare_status"] = status
        out["prepare"] = prepared

        status, started = post(
            "/api/import/start",
            {
                "directory": str(terminal_dir),
                "name": "e2e",
                "version": "1.0.0",
                "account": "50231488",
                "write_env": True,
            },
        )
        out["start_status"] = status
        out["start"] = started

        task_id = str(started.get("task_id") or "")
        snap: dict[str, object] = {}
        for _ in range(60):
            snap = get(f"/api/import/progress?task_id={task_id}")
            if snap.get("done"):
                break
            time.sleep(0.2)
        out["progress"] = snap
    finally:
        bridge.stop()

    print("[import-e2e] " + json.dumps(out, ensure_ascii=False, indent=2))

    problems: list[str] = []
    if not (out.get("prepare") or {}).get("ok"):
        problems.append(f"prepare 没通过：{(out.get('prepare') or {}).get('error')}")
    if not (out.get("start") or {}).get("ok"):
        problems.append(f"start 没通过：{(out.get('start') or {}).get('error')}")

    snap = out.get("progress") or {}
    error = str(snap.get("error") or "")
    if error:
        problems.append(f"导入报错：{error}")
    if "cannot unpack" in error:
        problems.append("仍然出现 cannot unpack non-iterable NoneType object")

    steps = snap.get("steps") or []
    step_keys = [s.get("key") for s in steps]
    if "env" not in step_keys:
        problems.append(f"没走到写 .env 那一步，步骤={step_keys}")

    # .env 真的写出来了吗
    env_path = terminal_dir / ".env"
    if not env_path.is_file():
        problems.append(".env 没有生成")
    else:
        out["env_text"] = env_path.read_text(encoding="utf-8")

    if problems:
        for p in problems:
            print(f"[import-e2e] FAIL: {p}")
        print("[import-e2e] 结论：失败")
        return 1
    print("[import-e2e] 结论：通过（写 .env 步骤通过，无 None 解包错误）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
