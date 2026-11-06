"""WebView 版运维面板入口。

与 TK 版 `nodel_client_dashboard/main.py` 平级、互不影响：
  - TK 版：customtkinter 窗口
  - 本版：WebView2 窗口 + 本机 HTTP/SSE 桥

两者共用同一份实例清单与面板配置（见 `paths.data_dir`），
可以随时切换，TK 版保持为安全回退。

命令行：
  --selftest [报告文件]   跑一次前端加载自检（不显示窗口），供打包后验证。
  --diagnose [报告文件]   跑一次环境体检：查 WebView2 Runtime / .NET /
                          前端产物 / 打包依赖，把结论打印到 stdout 并落盘。
                          界面空白时先跑这个。
  --mt5-probe             验证冻结产物里 MetaTrader5 可用（导入节点要靠它
                          附着本机终端读取当前登录号），输出 JSON 到 stdout。
  --import-probe          验证冻结产物里导入编排能跑通（写 .env 那一步出过
                          "cannot unpack non-iterable NoneType object"），
                          输出 JSON 到 stdout。
  --version-update-probe  验证冻结产物里版本更新编排能跑通（下载 / 校验 /
                          解压 / 覆盖 / 回滚），输出 JSON 到 stdout。
"""
from __future__ import annotations

import json
import sys


def _run_ui() -> int:
    from app_web import run_app

    return run_app()


def _run_diagnose() -> int:
    """环境体检：把「界面空白」的常见成因逐条查一遍。

    打包后的 exe 是 GUI 子系统，stdout 看不到东西 —— 所以既写文件
    （`<数据目录>/logs/dashboard_diagnose.json`），又尽量往 stdout 打印
    （从 cmd 运行时能看到）。
    """
    import diagnostics

    diagnostics.setup_logging()
    # 体检本身也要进日志：用户可能只看日志文件，不看屏幕输出。
    diagnostics.log_startup_banner()
    report = diagnostics.environment_report()
    text = diagnostics.format_report(report)
    print(text, flush=True)

    path = diagnostics.write_report(report)
    print(f"\n报告已保存：{path}", flush=True)
    print(f"日志文件：{diagnostics.log_path()}", flush=True)

    # 有问题时弹窗告知用户去哪儿看日志 —— GUI 模式下这是唯一的提示手段。
    if not report.get("ok"):
        diagnostics.notify_fatal(
            "检测到以下问题：\n\n"
            + "\n".join(f"  · {p}" for p in report.get("problems", []))
            + f"\n\n完整日志：\n{diagnostics.log_path()}",
            log_path_hint=diagnostics.log_path(),
        )
        return 1
    return 0


def _run_mt5_probe() -> int:
    """验证冻结产物里 MetaTrader5 能用。

    这个功能是「静默降级」的：import 失败不会让面板起不来，
    只是导入节点时读不到终端账号，界面提示
    「本机没有 MetaTrader5 接口，请手工填写账号」。
    打包后没有别的办法确认，只能真的跑一次。
    """
    out: dict[str, object] = {}
    try:
        import MetaTrader5 as mt5
    except Exception as exc:  # noqa: BLE001
        out = {"import_ok": False, "error": repr(exc)}
        print(json.dumps(out, ensure_ascii=False))
        return 1

    out["import_ok"] = True
    out["version"] = getattr(mt5, "__version__", "?")
    try:
        import terminal_account as ta

        terminal = ta.ef.find_terminal(r"D:\icmarkets")
        if terminal is None:
            out["error"] = "没找到 terminal64.exe"
        else:
            ok = mt5.initialize(path=str(terminal))
            out["initialize"] = bool(ok)
            out["last_error"] = str(mt5.last_error())
            if ok:
                try:
                    info = mt5.account_info()
                    out["login"] = None if info is None else int(info.login)
                    out["server"] = None if info is None else str(info.server)
                finally:
                    mt5.shutdown()
    except Exception as exc:  # noqa: BLE001
        out["error"] = repr(exc)

    print(json.dumps(out, ensure_ascii=False))
    return 0 if out.get("login") else 1


def _run_import_probe() -> int:
    """验证冻结产物里「导入」编排能跑通（不连后端、不改真目录）。

    这条路径出过 "cannot unpack non-iterable NoneType object"：
    写 .env 那一步把 `ef.write_env` 的返回值（None）直接解包。
    单测覆盖不到冻结产物，所以打包后要真的跑一次。
    """
    out: dict[str, object] = {}
    import tempfile
    from pathlib import Path

    import import_service as svc

    work = Path(tempfile.mkdtemp(prefix="import_probe_"))
    terminal_dir = work / "mt5"
    terminal_dir.mkdir()
    (terminal_dir / "terminal64.exe").write_bytes(b"MZ")

    pkg = work / "pkg"
    pkg.mkdir()
    (pkg / "node_client.exe").write_bytes(b"MZ")
    (pkg / ".env.example").write_text("BASE=1\n", encoding="utf-8")

    svc.vs.download_package = lambda *a, **k: None
    svc.vs.verify_sha256 = lambda *a, **k: True
    svc.require_binding = lambda _p: None
    svc.read_algo_trading_allowed = lambda _d: True

    def _extract(_zip, dest):
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        for src in pkg.iterdir():
            (dest / src.name).write_bytes(src.read_bytes())
        return True, "ok"

    svc.extract_package = _extract
    svc.apply_package = lambda *a, **k: (True, "ok")

    def _perform(action, cfg, fn, **kwargs):
        # 复现线上那个坑：写 .env 的被调函数返回 None。
        # 旧实现是 `ok, msg = perform("edit_env", ...)`，收到 None 直接炸
        # "cannot unpack non-iterable NoneType object"。
        # 现在编排层必须兜住它。
        if action == "edit_env":
            fn()
            return None
        return fn()

    req = svc.ImportRequest(
        directory=str(terminal_dir),
        name="probe",
        version="1.0.0",
        account_raw="50231488",
        write_env=True,
        ws_url="ws://127.0.0.1:9/ws/node",
    )
    try:
        result = svc.run_import(
            req,
            target=object(),
            perform=_perform,
            enroll=lambda *a, **k: None,
            stop_for_install=lambda *a, **k: None,
            register=lambda *a, **k: None,
        )
    except Exception as exc:  # noqa: BLE001
        out = {"ok": False, "error": repr(exc)}
        print(json.dumps(out, ensure_ascii=False))
        return 1

    out = {
        "ok": bool(result.ok),
        "env_written": bool(result.env_written),
        "env_exists": (terminal_dir / ".env").is_file(),
        "error": result.message if not result.ok else "",
    }
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out["ok"] and out["env_exists"] else 1


def _run_version_update_probe() -> int:
    """验证冻结产物里「版本更新」编排能跑通（不连后端、不改真目录）。

    覆盖安装会写客户端目录，单测覆盖不到冻结产物（模块漏打、
    依赖漏打都会在这里现形），所以打包后要真的跑一次。
    """
    out: dict[str, object] = {}
    import hashlib
    import json as _json
    import tempfile
    import zipfile
    from pathlib import Path

    import version_update as vu

    work = Path(tempfile.mkdtemp(prefix="ver_probe_"))
    instance_dir = work / "prod-01"
    instance_dir.mkdir()
    old_exe = b"probe-old-exe"
    (instance_dir / "node_client.exe").write_bytes(old_exe)
    (instance_dir / "client_capabilities.json").write_text(
        _json.dumps({
            "account_binding": True,
            "executable_sha256": hashlib.sha256(old_exe).hexdigest(),
        }),
        encoding="utf-8",
    )
    (instance_dir / "version.txt").write_text("1.0.0-20260101000000\n", encoding="utf-8")

    pkg = work / "pkg.zip"
    new_exe = b"probe-new-exe"
    with zipfile.ZipFile(pkg, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("node_client.exe", new_exe)
        zf.writestr("client_capabilities.json", _json.dumps({
            "account_binding": True,
            "executable_sha256": hashlib.sha256(new_exe).hexdigest(),
        }))
        zf.writestr("version.txt", "2.0.0-20260102000000\n")
    sha = hashlib.sha256(pkg.read_bytes()).hexdigest()

    # 假桥接层：只提供版本更新编排要用到的那几个成员
    class _FakeManager:
        def configs(self):
            from models import InstanceConfig

            return [InstanceConfig.create(
                name="probe-01",
                exe_path=str(instance_dir / "node_client.exe"),
                cwd=str(instance_dir),
            )]

        def batch_update(self, package_dir, *, instance_ids=None, **kwargs):
            from client_deploy import apply_package

            results = []
            for cfg in self.configs():
                ok, msg = apply_package(package_dir, cfg.exe_path, backup=True)
                results.append(_FakeResult(cfg, ok, msg))
            return results

        def batch_rollback(self, version, *, instance_ids=None, **kwargs):
            from client_deploy import restore_backup

            results = []
            for cfg in self.configs():
                ok, msg = restore_backup(cfg.exe_path, version)
                results.append(_FakeResult(cfg, ok, msg))
            return results

    class _FakeResult:
        def __init__(self, cfg, ok, msg):
            self.instance_id = cfg.id
            self.name = cfg.name
            self.ok = ok
            self.message = msg
            self.old_version = "1.0.0-20260101000000"
            self.new_version = "2.0.0-20260102000000"

    class _FakeTarget:
        base = "http://127.0.0.1:9/probe"
        token = "probe"
        ready = True

    class _FakeBridge:
        session = None
        manager = _FakeManager()

        def version_target(self):
            return _FakeTarget()

    vu.vs.download_package = lambda target, version, dest, on_progress=None: Path(dest).write_bytes(pkg.read_bytes())
    vu.vs.verify_sha256 = lambda path, expected: True

    controller = vu.VersionUpdateController(_FakeBridge())
    try:
        results = controller._run_update("probe", "2.0.0-20260102000000", sha, ["probe-01"])
        ok = bool(results and results[0].ok)
        new_version = (instance_dir / "version.txt").read_text(encoding="utf-8").strip()
        backups = vu.list_local_backups(instance_dir / "node_client.exe")
        out = {
            "ok": ok,
            "new_version": new_version,
            "backups": backups,
            "error": "" if ok else "batch_update failed",
        }
        print(json.dumps(out, ensure_ascii=False))
        return 0 if ok and new_version == "2.0.0-20260102000000" else 1
    except Exception as exc:  # noqa: BLE001
        out = {"ok": False, "error": repr(exc)}
        print(json.dumps(out, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    # 诊断模式不需要管理员权限，也不套看门狗。
    if "--diagnose" in sys.argv:
        import diagnostics

        diagnostics.setup_logging()
        sys.exit(_run_diagnose())
    if "--mt5-probe" in sys.argv:
        sys.exit(_run_mt5_probe())
    if "--import-probe" in sys.argv:
        sys.exit(_run_import_probe())
    if "--version-update-probe" in sys.argv:
        sys.exit(_run_version_update_probe())
    if "--selftest" in sys.argv:
        sys.exit(_run_ui())

    # 正常启动：先铺好日志，再做后面的事。
    # GUI 子系统的 exe 没有 stdout，日志文件是唯一的线索。
    import diagnostics

    diagnostics.setup_logging()
    diagnostics.install_excepthook()
    diagnostics.log_startup_banner()

    from admin_elevate import ensure_admin
    from recovery import main as recovery_main

    if not ensure_admin():
        diagnostics.get_logger("dashboard.startup").info("未获得管理员权限，退出")
        sys.exit(0)
    sys.exit(recovery_main(_run_ui))
