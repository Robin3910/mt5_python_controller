"""启动诊断：把「界面空白」这类无法自解释的故障落成可读日志。

为什么需要它
------------
打包后的 exe 是 GUI 子系统（`console=False`），`print()` 的内容没有任何
去处。用户看到的就是一个空白窗口，日志全在看不见的地方。

白屏的成因基本就这几类，每一类都要能自证：
  1. WebView2 Runtime 没装 —— pywebview 的 edgechromium 后端起不来；
  2. .NET Framework 缺失 / 版本太低 —— pythonnet 加载不了
     `Microsoft.Web.WebView2.*.dll`，CLR 报错直接白屏；
  3. 冻结产物里没带上 `webview/lib/*.dll`、`ClrLoader.dll`、
     `Python.Runtime.dll` —— 漏打依赖，同样白屏；
  4. 前端产物缺失或没挂载 —— 页面能开但 `#container` 是空的；
  5. 前端 JS 崩了 —— Vue 没挂载成功，一样是白屏；
  6. 本机 HTTP 桥没起来 / 端口被占 —— 页面连不上。

所以这里做两件事：
  - **日志**：`<数据目录>/logs/dashboard.log`，含启动全过程的关键节点；
  - **环境体检**：`environment_report()` 把上面 6 类逐条查一遍并给出结论。

用法
----
    python main.py --diagnose            # 打印体检报告（JSON）
    python main.py --diagnose report.json # 同时写文件

冻结产物上跑同一个参数即可，用户把 `logs/dashboard.log` 与体检报告
一起发回来就能定位。
"""
from __future__ import annotations

import json
import os
import platform
import socket
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

# 日志文件名（放在数据目录的 logs/ 下，与节点日志同目录）
LOG_NAME = "dashboard.log"
REPORT_NAME = "dashboard_diagnose.json"

# 体检报告里要重点关注的「致命项」
_FATAL_KEYS = ("webview2_runtime", "dotnet_release", "frontend_dist")

# 渲染后端：pywebview 选中的浏览器内核。
# mshtml = IE 内核，跑不了 Vue 3 + ES module 的前端，界面必然空白。
RENDERER_MSHTML = "mshtml"


# ─────────────────────────────────────────────── 日志


def data_dir() -> Path:
    """面板数据目录（延迟导入，避免模块级循环依赖）。"""
    try:
        from store import data_dir as _d

        return _d()
    except Exception:  # noqa: BLE001 - 诊断代码不能自己再炸
        return Path(__file__).resolve().parent


def logs_dir() -> Path:
    d = data_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def log_path() -> Path:
    return logs_dir() / LOG_NAME


def _enable_utf8_console() -> None:
    """让中文日志在终端里能正常显示。

    Windows 控制台默认 GBK 代码页。这里要分两种情况：
      - **输出到终端（tty）**：Python 的 stdout 走 `WriteConsoleW`，
        内部就是 UTF-16，任何编码都不会乱码。此时**不能**调
        `reconfigure(encoding='utf-8')` —— 那会把流改成普通字节流，
        反而让中文花掉。
      - **输出被重定向到文件/管道**：接收方（cmd 重定向、PowerShell、
        记事本）多半按 locale 编码（GBK）解读。此时如果 Python 按 UTF-8
        写，接收方就会解出一堆乱码；按 GBK 写才对得上。

    所以非 tty 时**保持 locale 编码**，只把 errors 设成 replace ——
    这样遇到 GBK 装不下的生僻字也不会抛 UnicodeEncodeError 把进程带崩。
    """
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        try:
            if stream.isatty():
                continue
        except Exception:  # noqa: BLE001
            pass
        try:
            stream.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001
            pass

    # 刻意**不改**控制台代码页。
    #
    # Python 输出到管道时用的是 locale 编码（简体中文 Windows 上是 GBK），
    # 接收方（cmd 重定向、PowerShell、记事本）也按当前控制台代码页来解读。
    # 一旦把代码页改成 65001(UTF-8)，接收方就会拿 UTF-8 去解 GBK 字节，
    # 中文全花 —— 比不改还糟。
    #
    # 报告文件另说：diagnostics.write_report 显式用 UTF-8 写盘，
    # 与控制台代码页无关。


def setup_logging() -> Path:
    """配置根 logger：文件（始终）+ 控制台（仅当有真实 stdout 时）。

    冻结后的 GUI exe 没有 stdout，所以文件是唯一可靠的去处。
    这里不能用 `basicConfig(force=True)` 覆盖调用方的配置，
    但本模块是入口第一步调用的，实际没有先例。

    返回日志文件路径。
    """
    import logging
    from logging.handlers import RotatingFileHandler

    _enable_utf8_console()

    path = log_path()
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    already = any(
        isinstance(h, RotatingFileHandler)
        and getattr(h, "baseFilename", "") == str(path)
        for h in root.handlers
    )
    if not already:
        # 先探一下能不能写 —— RotatingFileHandler 是延迟打开文件的，
        # 目录不可写时不会在这里报错，而是等到第一次写日志才炸，
        # 那时用户已经看到白屏了，没时间再看错误。
        candidates = [path]
        try:
            alt_dir = Path(sys.executable).resolve().parent / "logs"
            alt_dir.mkdir(parents=True, exist_ok=True)
            candidates.append(alt_dir / LOG_NAME)
        except OSError:
            pass

        for candidate in candidates:
            try:
                candidate.parent.mkdir(parents=True, exist_ok=True)
                # 先写一个字节，确认真的写得进去。
                candidate.touch(exist_ok=True)
                handler = RotatingFileHandler(
                    candidate, maxBytes=2 * 1024 * 1024, backupCount=3,
                    encoding="utf-8",
                )
                handler.setFormatter(
                    logging.Formatter(
                        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
                    )
                )
                root.addHandler(handler)
                break
            except OSError:
                # 换下一个候选位置（数据目录不可写时退到 exe 同级）。
                continue

    # 只有真有 stdout 时才挂控制台 handler，否则 logging 会往 None 写而报错。
    stream = sys.stdout
    if stream is not None and not any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler)
        for h in root.handlers
    ):
        console = logging.StreamHandler(stream)
        console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        root.addHandler(console)

    return path


def get_logger(name: str = "dashboard"):
    import logging

    return logging.getLogger(name)


def install_excepthook() -> None:
    """未捕获异常也要落盘。

    看门狗（recovery.py）只记「子进程非 0 退出」，具体原因在子进程的
    stderr 里 —— GUI 模式下 stderr 同样无处可去。这里兜住最后一道。
    """

    def _hook(exc_type, exc, tb) -> None:
        log = get_logger("dashboard.crash")
        try:
            log.error(
                "未捕获异常：%s: %s\n%s",
                exc_type.__name__,
                exc,
                "".join(traceback.format_exception(exc_type, exc, tb)),
            )
        finally:
            sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = _hook


def log_startup_banner() -> None:
    """启动横幅：一眼看清这是哪台机器、哪个版本、从哪启动的。"""
    log = get_logger("dashboard.startup")
    frozen = getattr(sys, "frozen", False)
    log.info("=" * 70)
    log.info("启动：python=%s frozen=%s", platform.python_version(), frozen)
    log.info("  可执行文件：%s", sys.executable)
    log.info("  数据目录：%s", data_dir())
    log.info("  日志文件：%s", log_path())
    log.info("  工作目录：%s", os.getcwd())
    log.info("  机器：%s / %s", platform.node(), platform.platform())
    try:
        import _build_info

        log.info("  构建版本：%s（构建于 %s）", _build_info.BUILD_VERSION, _build_info.BUILT_AT)
    except Exception:  # noqa: BLE001 - 开发态没 stamp 过，属正常
        pass


# ─────────────────────────────────────────────── 环境体检


def _registry_value(path: str, name: str) -> str:
    """读注册表字符串值，失败返回空串。"""
    if sys.platform != "win32":
        return ""
    try:
        import winreg
    except ImportError:
        return ""
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
            value, _ = winreg.QueryValueEx(key, name)
            return str(value)
    except OSError:
        return ""


def check_webview2_runtime() -> dict[str, Any]:
    """查 Edge WebView2 Runtime 是否可用（随包或系统装的都算）。

    pywebview 的 Windows 后端只走 WebView2（edgechromium）。
    两边都没有 = 它会静默回落 MSHTML(IE 内核) = 界面空白。
    """
    if sys.platform != "win32":
        return {"ok": True, "version": "", "detail": "非 Windows，跳过"}

    clients = (
        r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients"
        r"\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
        r"SOFTWARE\Microsoft\EdgeUpdate\Clients"
        r"\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
    )
    version = ""
    for path in clients:
        version = _registry_value(path, "pv")
        if version:
            break

    # 兜底：Evergreen 安装目录里也能看出版本
    install_dir = ""
    for path in clients:
        install_dir = _registry_value(path, "location")
        if install_dir:
            break

    # 随包运行时：目标机没装系统运行时时的救命稻草
    bundled_path = ""
    bundled_source = ""
    try:
        import webview_runtime

        bundled_path, bundled_source = webview_runtime.resolve_runtime()
    except Exception:  # noqa: BLE001
        pass

    system_ok = bool(version)
    bundled_ok = bool(bundled_path)
    ok = system_ok or bundled_ok

    if bundled_ok and not system_ok:
        detail = (
            f"系统未装 Edge WebView2 Runtime，但找到随包运行时（{bundled_source}），"
            "面板会用它，不会回落到 IE 内核。"
        )
    elif system_ok and bundled_ok:
        detail = (
            f"系统已安装 Edge WebView2 Runtime（{version}），"
            f"另有随包运行时备用（{bundled_source}）。"
        )
    elif system_ok:
        detail = f"已安装 Edge WebView2 Runtime（{version}）"
    else:
        detail = (
            "既没有系统安装的 Edge WebView2 Runtime，也没有随包运行时 —— "
            "面板会静默回落到 MSHTML(IE 内核)，界面必然空白。"
            "请安装 Microsoft Edge WebView2 Evergreen Runtime，"
            "或把 webview2/ 运行时目录放在 exe 旁边。"
        )

    return {
        "ok": ok,
        "version": version,
        "install_dir": install_dir,
        "system_installed": system_ok,
        "bundled_path": str(bundled_path) if bundled_path else "",
        "bundled_source": bundled_source,
        "detail": detail,
    }


def check_dotnet() -> dict[str, Any]:
    """查 .NET Framework 4.x 版本。

    pythonnet 要用它加载 `Microsoft.Web.WebView2.*.dll`；
    4.6.2 以下加载不了，表现同样是白屏。
    """
    if sys.platform != "win32":
        return {"ok": True, "release": "", "detail": "非 Windows，跳过"}

    release = _registry_value(
        r"SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full", "Release"
    )
    if not release:
        release = _registry_value(
            r"SOFTWARE\WOW6432Node\Microsoft\NET Framework Setup\NDP\v4\Full",
            "Release",
        )

    # Release 号与 .NET 4.x 的映射（只列常见的）。
    # 表里没有的按区间兜底 —— 同一 4.8 补丁在不同 Windows 上 release 号不同，
    # 只靠枚举表会把「装了 4.8」误判成「没装」。
    table = {
        "378389": "4.8",
        "461808": "4.8",
        "461814": "4.8.1",
        "462051": "4.8.1",
        "533320": "4.8.1",
        "533325": "4.8.1",
        "528040": "4.8",
        "460798": "4.7.2",
        "461308": "4.7.2",
        "461310": "4.7.2",
        "439443": "4.7.1",
        "439444": "4.7.1",
        "394254": "4.6.2",
        "394255": "4.6.2",
    }
    version = table.get(release, "")
    if not version and release.isdigit():
        num = int(release)
        if num >= 533320:
            version = "4.8.1"
        elif num >= 528040:
            version = "4.8"
        elif num >= 461308:
            version = "4.7.2"
        elif num >= 439443:
            version = "4.7.1"
        elif num >= 394254:
            version = "4.6.2"
    ok = bool(release) and int(release or 0) >= 394254
    return {
        "ok": ok,
        "release": release,
        "version": version,
        "detail": (
            f"已安装 .NET Framework {version or release}"
            if ok else
            "未检测到 .NET Framework 4.6.2+ —— WebView2 托管程序集加载不了，"
            "窗口会空白。"
        ),
    }


def check_frontend_dist() -> dict[str, Any]:
    """查前端产物是否在位（冻结后应解包到 `_MEIxxxx/frontend-console/dist`）。"""
    try:
        from paths import frontend_dist_dir

        dist = frontend_dist_dir()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "dir": "", "index": False, "files": 0,
                "detail": f"解析前端目录失败：{exc!r}"}

    index = dist / "index.html"
    files = list(dist.rglob("*")) if dist.is_dir() else []
    file_count = sum(1 for p in files if p.is_file())
    has_assets = any(p.suffix == ".js" for p in files if p.is_file())
    ok = index.is_file() and has_assets
    return {
        "ok": ok,
        "dir": str(dist),
        "index": index.is_file(),
        "files": file_count,
        "detail": (
            "前端产物齐全" if ok else
            "前端产物缺失或不完整 —— 页面会退化成占位页（正常是提示页，"
            "若整页空白则说明 dist 里的 JS 没打进来）。"
        ),
    }


def check_webview_package() -> dict[str, Any]:
    """查 pywebview / pythonnet 是否可导入，以及关键 DLL 是否在位。"""
    info: dict[str, Any] = {
        "webview_ok": False,
        "webview_version": "",
        "pythonnet_ok": False,
        "clr_loader_ok": False,
        "webview_lib": "",
        "webview_dlls": [],
        "missing_dlls": [],
        "detail": "",
    }
    try:
        import webview

        info["webview_ok"] = True
        info["webview_version"] = str(getattr(webview, "__version__", "?"))
    except Exception as exc:  # noqa: BLE001
        info["detail"] = f"import webview 失败：{exc!r}"
        return info

    for name, key in (("pythonnet", "pythonnet_ok"), ("clr_loader", "clr_loader_ok")):
        try:
            __import__(name)
            info[key] = True
        except Exception:  # noqa: BLE001
            info[key] = False

    # webview/lib 下的 WebView2 托管程序集与原生加载器
    try:
        import webview as _w

        lib = Path(_w.__file__).resolve().parent / "lib"
        info["webview_lib"] = str(lib)
        if lib.is_dir():
            dlls = sorted(p.name for p in lib.iterdir() if p.suffix == ".dll")
            info["webview_dlls"] = dlls
            # WebView2Loader.dll 不在 lib/ 根下，而是按架构放在
            # lib/win-x64/native/ 之类的子目录里 —— 只看根目录会误报缺失。
            loader_found = any(p.name == "WebView2Loader.dll" for p in lib.rglob("*.dll"))
            need = [
                "Microsoft.Web.WebView2.Core.dll",
                "Microsoft.Web.WebView2.WinForms.dll",
            ]
            missing = [n for n in need if n not in dlls]
            if not loader_found:
                missing.append("WebView2Loader.dll")
            info["missing_dlls"] = missing
    except Exception:  # noqa: BLE001
        pass

    if info["missing_dlls"]:
        info["detail"] = (
            "缺少 WebView2 相关 DLL：" + "、".join(info["missing_dlls"])
            + " —— 打包时漏打了，CLR 加载不到程序集，窗口会空白。"
        )
    elif not info["pythonnet_ok"] or not info["clr_loader_ok"]:
        info["detail"] = "pythonnet / clr_loader 不可用 —— CLR 加载链路断了，窗口会空白。"
    else:
        info["detail"] = "pywebview 与 CLR 依赖齐备"
    return info


def check_port_binding() -> dict[str, Any]:
    """本机 HTTP 桥能不能绑一个随机端口。"""
    try:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        return {"ok": True, "port": port, "detail": "本机回环端口可绑定"}
    except OSError as exc:
        return {"ok": False, "port": 0, "detail": f"绑定回环端口失败：{exc!r}"}


def check_config_files() -> dict[str, Any]:
    """查实例清单与 .env 是否在位（缺了不影响白屏，但会让人以为坏了）。"""
    d = data_dir()
    info: dict[str, Any] = {}
    for name in ("instances.json", "panel_config.json", ".env"):
        p = d / name
        info[name] = p.is_file()
    try:
        from store import load_app_url

        info["app_url"] = load_app_url()
    except Exception:  # noqa: BLE001
        info["app_url"] = ""
    return info


def check_renderer() -> dict[str, Any]:
    """查 pywebview 实际会用哪个渲染后端。

    只有 winforms 模块已导入才有值（`webview.start()` 之后）。
    未启动时返回 unknown —— 此时判断依据是运行时是否可用。
    """
    try:
        import webview_runtime

        renderer = webview_runtime.renderer_in_use()
    except Exception:  # noqa: BLE001
        renderer = ""

    if renderer == RENDERER_MSHTML:
        ok = False
        detail = (
            "当前使用 MSHTML(IE 内核) —— 它不支持 ES module，"
            "本面板的前端跑不起来，界面必然空白。"
        )
    elif renderer:
        ok = True
        detail = f"渲染后端：{renderer}"
    else:
        ok = True
        detail = "渲染后端尚未确定（窗口启动后才是 edgechromium）"

    return {"ok": ok, "renderer": renderer or "unknown", "detail": detail}


def environment_report() -> dict[str, Any]:
    """体检报告：逐条列出白屏相关的每一项。"""
    report: dict[str, Any] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "python": platform.python_version(),
        "executable": sys.executable,
        "frozen": bool(getattr(sys, "frozen", False)),
        "platform": platform.platform(),
        "machine": platform.node(),
        "data_dir": str(data_dir()),
        "log_file": str(log_path()),
        "webview2_runtime": check_webview2_runtime(),
        "dotnet": check_dotnet(),
        "frontend_dist": check_frontend_dist(),
        "webview_package": check_webview_package(),
        "renderer": check_renderer(),
        "port_binding": check_port_binding(),
        "config_files": check_config_files(),
    }

    # 汇总结论：先说最可能导致白屏的
    problems: list[str] = []
    if not report["webview2_runtime"]["ok"]:
        problems.append(
            "缺少 Edge WebView2 Runtime（系统未装且没有随包运行时）—— "
            "面板会回落到 IE 内核并白屏"
        )
    if not report["dotnet"]["ok"]:
        problems.append(".NET Framework 4.6.2+ 缺失或过低")
    if not report["frontend_dist"]["ok"]:
        problems.append("前端产物缺失或不完整")
    pkg = report["webview_package"]
    if pkg.get("missing_dlls"):
        problems.append("打包漏打 WebView2 DLL：" + "、".join(pkg["missing_dlls"]))
    elif not pkg.get("webview_ok"):
        problems.append("pywebview 不可导入")
    if not report["port_binding"]["ok"]:
        problems.append("本机回环端口不可绑定")

    report["problems"] = problems
    report["ok"] = not problems
    report["verdict"] = (
        "未发现明显问题 —— 若界面仍空白，请把 dashboard.log 里的 "
        "「前端脚本错误」段一并发回。"
        if not problems else
        "发现 " + str(len(problems)) + " 个问题：" + "；".join(problems)
    )

    # 体检结论也写进日志：用户可能只把日志文件发回来。
    log = get_logger("dashboard.diagnose")
    log.info("体检结论：%s", report["verdict"])
    for problem in problems:
        log.warning("问题：%s", problem)
    for name in ("webview2_runtime", "dotnet", "frontend_dist", "webview_package"):
        log.info("%s：%s", name, report[name].get("detail", ""))

    return report

def write_report(report: dict[str, Any], out_path: Path | None = None) -> Path:
    """把体检报告写成 JSON 文件。"""
    path = out_path or (logs_dir() / REPORT_NAME)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def format_report(report: dict[str, Any]) -> str:
    """人读版体检报告。"""
    lines: list[str] = []
    add = lines.append
    add("=" * 66)
    add("面板启动体检报告")
    add("=" * 66)
    add(f"生成时间   : {report.get('generated_at')}")
    add(f"Python     : {report.get('python')}  (frozen={report.get('frozen')})")
    add(f"可执行文件 : {report.get('executable')}")
    add(f"系统       : {report.get('platform')}")
    add(f"数据目录   : {report.get('data_dir')}")
    add(f"日志文件   : {report.get('log_file')}")
    add("")

    def _block(title: str, data: dict[str, Any], ok_key: str = "ok") -> None:
        ok = bool(data.get(ok_key))
        mark = "[OK]  " if ok else "[!!]  "
        add(f"{mark}{title}")
        for key, value in data.items():
            if key in (ok_key, "detail"):
                continue
            if isinstance(value, list):
                value = "、".join(str(v) for v in value) if value else "（空）"
            add(f"        {key}: {value}")
        detail = str(data.get("detail") or "")
        if detail:
            add(f"        -> {detail}")
        add("")

    _block("Edge WebView2 Runtime", report["webview2_runtime"])
    _block(".NET Framework", report["dotnet"])
    _block("前端产物", report["frontend_dist"])
    _block("pywebview / CLR 依赖", report["webview_package"], ok_key="webview_ok")
    _block("渲染后端", report["renderer"])
    _block("本机端口", report["port_binding"])
    _block("配置文件", report["config_files"], ok_key="instances.json")

    add("-" * 66)
    add(f"结论：{report.get('verdict')}")
    return "\n".join(lines)


def show_error_dialog(title: str, message: str) -> None:
    """把错误弹到桌面上。

    GUI 子系统的 exe 没有 stdout，用户看不到任何输出。
    启动就失败时必须弹窗告诉他「日志在哪」。

    但只在「没有可读的 stdout」时才弹：用户既然在 cmd 里跑 `--diagnose`，
    就能直接看终端输出，此时再弹一个模态框会把命令卡住不动
    （MessageBoxW 是阻塞调用，无人点「确定」就永远等在那儿）。
    """
    if sys.platform != "win32":
        return
    if sys.stdout is not None:
        # 有终端可读：文本已经打在屏幕上了，不必再弹窗。
        return
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, title, 0x10)
    except Exception:  # noqa: BLE001
        pass


def notify_fatal(message: str, *, log_path_hint: Path | None = None) -> None:
    """致命错误：记日志 + 弹窗 + 让进程非 0 退出（看门狗会记录）。"""
    log = get_logger("dashboard.fatal")
    log.error(message)
    text = message
    if log_path_hint is not None:
        text += f"\n\n日志文件：\n{log_path_hint}"
    show_error_dialog("节点控制台 · 启动失败", text)
