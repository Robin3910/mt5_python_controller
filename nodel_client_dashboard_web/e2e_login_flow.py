"""端到端验证：手动登录流程（关闭自动登录）。

覆盖「点登录没反应」的前端 bug：
  LoginView 用 `await emit('login')`，而 Vue 的 emit() 返回 undefined，
  父组件里 api.login() 的异常变成未处理的 Promise 拒绝，
  本组件的 try/catch 接不到 -> 界面毫无反馈。

本脚本起真窗口，模拟点击「登录」，验证：
  - 登录页不显示后端地址；
  - 错误密码给出可见反馈（不静默）；
  - 正确密码能进入控制台。

账号密码从面板 .env 读，不写死在脚本里。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dashboard_bridge import DashboardBridge  # noqa: E402
from paths import frontend_dist_dir  # noqa: E402


def _credentials() -> tuple[str, str]:
    """从面板 .env 读 APP_URL 与登录账号密码。"""
    from store import dashboard_env_path
    from env_file import parse_env_text

    try:
        text = dashboard_env_path().read_text(encoding="utf-8-sig")
    except OSError:
        return "", ""
    values = parse_env_text(text)
    return (
        values.get("DASHBOARD_LOGIN_USER", "").strip(),
        values.get("DASHBOARD_LOGIN_PASSWORD", "").strip(),
    )


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

    import webview

    if not (frontend_dist_dir() / "index.html").is_file():
        print("[login] 前端产物缺失")
        return 2

    user, password = _credentials()
    if not user or not password:
        print("[login] 面板 .env 里没有 DASHBOARD_LOGIN_USER / DASHBOARD_LOGIN_PASSWORD")
        return 2
    print(f"[login] 使用账号 {user}", flush=True)

    bridge = DashboardBridge()
    # 让自动登录不生效，强制走手动登录路径
    bridge._load_auto_login = lambda: None  # type: ignore[method-assign]
    bridge.start()
    print(f"[login] bridge on 127.0.0.1:{bridge.port}", flush=True)

    results: dict[str, object] = {}
    window = webview.create_window(
        "login-check", url=bridge.url, width=1280, height=860, x=40, y=40
    )

    def probe() -> None:
        time.sleep(4.0)

        results["on_login_page"] = window.evaluate_js(
            "!!document.querySelector('.login-form')"
        )
        # 登录页不显示后端地址
        results["backend_shown"] = window.evaluate_js(
            "(document.querySelector('.login-backend-value')||{}).innerText || ''"
        )

        # 错误密码
        window.evaluate_js(
            """
            (() => {
              const u = document.querySelector('#login-user');
              const p = document.querySelector('#login-pass');
              if (u) { u.value = '__USER__'; u.dispatchEvent(new Event('input')); }
              if (p) { p.value = 'definitely-wrong-password'; p.dispatchEvent(new Event('input')); }
              const b = document.querySelector('.login-submit');
              if (b) b.click();
              return true;
            })()
            """.replace("__USER__", user)
        )
        time.sleep(5.0)
        results["after_bad_login"] = json.loads(
            window.evaluate_js(
                """
                JSON.stringify({
                  status: (document.querySelector('.login-status')||{}).innerText || '',
                  error: (document.querySelector('.login-error')||{}).innerText || '',
                  stillOnLogin: !!document.querySelector('.login-form'),
                })
                """
            )
        )

        # 正确密码
        window.evaluate_js(
            """
            (() => {
              const p = document.querySelector('#login-pass');
              if (p) { p.value = '__PASS__'; p.dispatchEvent(new Event('input')); }
              const b = document.querySelector('.login-submit');
              if (b) b.click();
              return true;
            })()
            """.replace("__PASS__", password)
        )
        time.sleep(8.0)
        results["after_good_login"] = json.loads(
            window.evaluate_js(
                """
                JSON.stringify({
                  stillOnLogin: !!document.querySelector('.login-form'),
                  hasConsole: !!document.querySelector('.console-shell')
                               || !!document.querySelector('.console-root'),
                })
                """
            )
        )

        try:
            window.destroy()
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=probe, daemon=True).start()
    webview.start()
    bridge.stop()

    print("[login] " + json.dumps(results, ensure_ascii=False, indent=2))

    backend = str(results.get("backend_shown") or "")
    bad = results.get("after_bad_login") or {}
    good = results.get("after_good_login") or {}

    problems = []
    if backend.strip():
        problems.append(f"登录页不应显示后端地址（got {backend!r}）")
    if not (str(bad.get("status") or "").strip() or str(bad.get("error") or "").strip()):
        problems.append("错误密码没有可见反馈（点登录像没反应）")
    if good.get("stillOnLogin") is not False:
        problems.append("正确密码后仍停在登录页")

    if problems:
        for p in problems:
            print(f"[login] FAIL: {p}")
        print("[login] 结论：失败")
        return 1
    print("[login] 结论：通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
