"""WebView 窗口：承载本机 HTTP/SSE 桥提供的控制台前端。

窗口尺寸与最小尺寸按参考稿的桌面控制台比例设定；
`bridge.url` 带一次性 session token，前端用它调 API 并建立 SSE 连接。

`--selftest` 模式：不起可见窗口，验证「前端产物可提供 + 页面能挂载 +
样式已生效」，把结果写成 JSON 文件。打包后可用它确认冻结产物确实可用。
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

from dashboard_bridge import DashboardBridge
from paths import frontend_dist_dir
from webview_runtime import apply_runtime

WINDOW_WIDTH = 1440
WINDOW_HEIGHT = 900
MIN_WIDTH = 1080
MIN_HEIGHT = 700

SELFTEST_FLAG = "--selftest"


def _dist_ready() -> bool:
    return (frontend_dist_dir() / "index.html").is_file()


def _run_selftest(bridge: DashboardBridge, out_path: Path | None) -> int:
    """自检：确认静态资源可提供、页面能挂载、样式已生效。

    不弹可见窗口，用 hidden 窗口做同样的加载验证。
    """
    import webview

    # 自检也必须指定运行时，否则目标机上它同样会回落 MSHTML，
    # 然后报一个「页面没挂载」的白屏式失败，掩盖真正的原因。
    runtime_info = apply_runtime(webview)

    report: dict[str, object] = {
        "dist_dir": str(frontend_dist_dir()),
        "dist_ready": _dist_ready(),
        "runtime_source": runtime_info.get("source", ""),
        "runtime_path": runtime_info.get("path", ""),
    }

    window = webview.create_window(
        "selftest",
        url=bridge.url,
        width=1280,
        height=820,
        hidden=True,
    )

    def worker() -> None:
        time.sleep(3.5)
        try:
            js = """
            (() => {
              const q = (s) => document.querySelector(s);
              return JSON.stringify({
                mounted: !!q('#container') && q('#container').children.length > 0,
                hasUser: !!q('#login-user'),
                hasPass: !!q('#login-pass'),
                loginFrame: !!q('.login-frame'),
                // 已登录时渲染的是控制台：Hero 指标 + 节点卡片
                consoleRoot: !!q('.console-root'),
                heroMetrics: Array.from(document.querySelectorAll('.stat-metric-label'))
                  .map((e) => e.textContent.trim()),
                cardCount: document.querySelectorAll('.node-card').length,
                bodyBg: getComputedStyle(document.body).backgroundColor,
                title: document.title,
                // 验证码登录入口：Tab（短信不可用时前端不渲染）
                smsTabs: document.querySelectorAll('.login-tab').length,
                smsTabLabels: Array.from(document.querySelectorAll('.login-tab'))
                  .map((e) => e.textContent.trim()),
              });
            })()
            """
            report["dom"] = json.loads(window.evaluate_js(js))
        except Exception as exc:  # noqa: BLE001
            report["dom_error"] = repr(exc)

        # 切到「验证码登录」Tab 再探一次：手机号 / 验证码输入框
        # 只在默认渲染的 Tab 里，点一下才会出现。
        dom = report.get("dom")
        if isinstance(dom, dict) and int(dom.get("smsTabs") or 0) >= 2:
            try:
                window.evaluate_js(
                    """
                    (() => {
                      const tabs = Array.from(document.querySelectorAll('.login-tab'));
                      const target = tabs.find((t) => t.textContent.includes('验证码'));
                      if (target) target.click();
                      return true;
                    })()
                    """
                )
                time.sleep(0.6)
                report["dom_sms"] = json.loads(
                    window.evaluate_js(
                        """
                        JSON.stringify({
                          hasPhone: !!document.querySelector('#login-phone'),
                          hasSmsCode: !!document.querySelector('#login-sms-code'),
                          hasSmsButton: !!document.querySelector('.btn-sms-code'),
                        })
                        """
                    )
                )
            except Exception as exc:  # noqa: BLE001
                report["dom_sms_error"] = repr(exc)

        try:
            window.destroy()
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=worker, daemon=True).start()
    webview.start()
    bridge.stop()

    from webview_runtime import renderer_in_use

    renderer = renderer_in_use()
    report["renderer"] = renderer or "unknown"
    mshtml_fallback = renderer == "mshtml"
    if mshtml_fallback:
        report["error"] = (
            "回退到了 MSHTML(IE 内核)：本机缺 Edge WebView2 Runtime 且没有随包运行时。"
            "IE 内核跑不了本面板的前端，页面必然空白。"
        )

    dom = report.get("dom")
    # 页面可能停在登录页（没配自动登录 / 后端不可达），也可能直接进控制台
    # （.env 里配了免登账号且后端可达）—— 两种都是「前端产物可用」，
    # 所以只要页面挂载成功、且是这两个分支之一，就算通过。
    if isinstance(dom, dict):
        login_view = bool(dom.get("hasUser") and dom.get("hasPass") and dom.get("loginFrame"))
        console_view = bool(dom.get("consoleRoot"))
    else:
        login_view = console_view = False
    report["view"] = "login" if login_view else ("console" if console_view else "unknown")

    # 验证码登录入口的检查：
    # - 短信不可用（后端没开 / 面板开关关掉）→ 没有 Tab 是**正常**的，不算失败；
    # - 短信可用 → 必须有 Tab，且切过去后手机号/验证码输入框都在。
    #   缺任何一个都说明前端只做了一半，点「获取验证码」会像没反应。
    sms_dom = report.get("dom_sms")
    sms_tabs = int(dom.get("smsTabs") or 0) if isinstance(dom, dict) else 0
    if sms_tabs >= 2:
        report["sms_ui"] = "ok"
        if not isinstance(sms_dom, dict) or not (
            sms_dom.get("hasPhone")
            and sms_dom.get("hasSmsCode")
            and sms_dom.get("hasSmsButton")
        ):
            report["error"] = "验证码登录界面不完整：手机号/验证码输入框或获取按钮缺失"
    else:
        report["sms_ui"] = "absent"

    report["ok"] = bool(
        not mshtml_fallback
        and report.get("dist_ready")
        and isinstance(dom, dict)
        and dom.get("mounted")
        and (login_view or console_view)
        and dom.get("bodyBg") == "rgb(10, 14, 23)"
        and report["sms_ui"] in ("ok", "absent")
        and not report.get("error")
    )

    text = json.dumps(report, ensure_ascii=False, indent=2)
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
    print(text, flush=True)
    return 0 if report["ok"] else 1


def run_app(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])

    import diagnostics

    log = diagnostics.get_logger("dashboard.web")

    # ── pywebview 导入 ──
    # 缺 pywebview / pythonnet / CLR 时，这里就炸，窗口根本起不来。
    # 必须在开窗前把真实原因抓住并弹窗告知用户。
    try:
        import webview
    except Exception as exc:  # noqa: BLE001
        diagnostics.notify_fatal(
            "无法加载 pywebview（WebView 窗口组件）：\n"
            f"{type(exc).__name__}: {exc}\n\n"
            "如果这是打包后的 exe，通常是打包时漏了依赖；"
            "请在开发目录执行 build_exe.bat 重新打包。",
            log_path_hint=diagnostics.log_path(),
        )
        log.error("import webview 失败：%r", exc)
        return 1
    log.info("pywebview %s 已加载", getattr(webview, "__version__", "?"))

    # ── WebView2 运行时 ──
    # 必须在 webview.start() 之前设置：pywebview 的 winforms 后端在 start()
    # 那一刻才导入，模块级的 is_chromium = _is_chromium() 也在那时才求值。
    # 设置晚了就来不及，pywebview 会静默回落到 MSHTML(IE 内核) → 白屏。
    runtime_info = apply_runtime(webview)
    if runtime_info.get("applied"):
        log.info(
            "WebView2 运行时：%s（%s）", runtime_info["source"], runtime_info["path"]
        )
    elif runtime_info.get("source") == "系统安装的 WebView2":
        log.info("WebView2 运行时：系统安装的 WebView2")
    else:
        # 既没随包运行时，系统也没装 —— 一定会白屏。
        log.error(
            "未找到 WebView2 运行时：%s。面板将回落到 MSHTML(IE 内核)，"
            "界面白屏。",
            runtime_info.get("detail", "未知"),
        )

    # ── 本机 HTTP 桥 ──
    try:
        bridge = DashboardBridge()
        port = bridge.start()
    except Exception as exc:  # noqa: BLE001
        diagnostics.notify_fatal(
            f"本机 HTTP 桥启动失败：\n{type(exc).__name__}: {exc}",
            log_path_hint=diagnostics.log_path(),
        )
        log.error("桥接层启动失败：%r", exc)
        return 1

    url = bridge.url
    log.info("桥接层已就绪：%s", url)

    if SELFTEST_FLAG in argv:
        idx = argv.index(SELFTEST_FLAG)
        out = None
        if len(argv) > idx + 1:
            out = Path(argv[idx + 1])
        return _run_selftest(bridge, out)

    print(f"[web] bridge on http://127.0.0.1:{port}", flush=True)

    dist = frontend_dist_dir()
    if not _dist_ready():
        log.warning("前端产物缺失：%s", dist)
        print(
            f"[web] 前端产物缺失：{dist}\n"
            "      请先在 frontend-console 目录执行 `npm run build`。",
            flush=True,
        )
    else:
        log.info("前端产物：%s", dist)

    try:
        window = webview.create_window(
            "亿点点 · 交易控制台",
            url=url,
            width=WINDOW_WIDTH,
            height=WINDOW_HEIGHT,
            min_size=(MIN_WIDTH, MIN_HEIGHT),
            confirm_close=False,
            text_select=True,
        )
    except Exception as exc:  # noqa: BLE001
        diagnostics.notify_fatal(
            f"创建窗口失败：\n{type(exc).__name__}: {exc}\n\n"
            "常见原因：目标机缺少 Edge WebView2 Runtime。",
            log_path_hint=diagnostics.log_path(),
        )
        log.error("create_window 失败：%r", exc)
        bridge.stop()
        return 1

    log.info("窗口已创建，等待页面加载")

    # 原生对话框（目录选择器）必须在 GUI 线程弹，而桥接层的 HTTP 线程是
    # 后台线程；native_dialogs 依赖这个登记来做线程 marshal。
    import native_dialogs

    native_dialogs.set_window(window)

    def _watch() -> None:
        """窗口关闭后清理桥接层，避免端口泄漏与后台线程残留。

        用 `window.events.closed` 而不是轮询 `window.exists()`——
        后者在这个 pywebview 版本里并不存在（`Window` 只有 `destroy`/`state`），
        调用会抛 AttributeError，若被吞掉就会在窗口刚要加载时就把
        HTTP 桥关掉，用户看到的是「无法访问此页面」。
        """
        window.events.closed.wait()
        log.info("窗口已关闭，停止桥接层")
        bridge.stop()

    threading.Thread(target=_watch, name="web-window-watch", daemon=True).start()

    # 运行时判定必须在 start() 之后才有意义（那时 winforms 才导入）。
    # 一旦发现回落到了 MSHTML，立刻弹窗告知用户 —— 否则就是无声的白屏。
    from webview_runtime import renderer_in_use

    try:
        webview.start()
    except Exception as exc:  # noqa: BLE001
        log.error("webview.start 失败：%r", exc)
        diagnostics.notify_fatal(
            f"WebView 启动失败：\n{type(exc).__name__}: {exc}\n\n"
            "常见原因：目标机缺少 Edge WebView2 Runtime，"
            "或 .NET Framework 版本过低。",
            log_path_hint=diagnostics.log_path(),
        )
        bridge.stop()
        return 1
    finally:
        renderer = renderer_in_use()
        log.info("渲染后端：%s", renderer or "未知")
        if renderer == "mshtml":
            log.error(
                "渲染后端回落到了 MSHTML(IE 内核) —— 它不支持前端用的 "
                "ES module / 异步语法，界面必然空白。"
            )
            diagnostics.notify_fatal(
                "界面无法显示：WebView 回退到了 IE 内核（MSHTML）。\n\n"
                "本机缺少 Edge WebView2 Runtime，且未找到随包运行时。\n"
                "IE 内核无法运行本面板的前端（Vue 3 + ES module），所以窗口是空的。\n\n"
                "解决办法（任选其一）：\n"
                "  1. 安装 Microsoft Edge WebView2 Evergreen Runtime；\n"
                "  2. 把 webview2/ 运行时目录放在 exe 旁边（可携带包已包含）；\n"
                "  3. 确认 .NET Framework 4.6.2+ 已安装。\n\n"
                f"日志文件：\n{diagnostics.log_path()}",
                log_path_hint=diagnostics.log_path(),
            )
        bridge.stop()
    log.info("面板退出")
    return 0


if __name__ == "__main__":
    sys.exit(run_app())
