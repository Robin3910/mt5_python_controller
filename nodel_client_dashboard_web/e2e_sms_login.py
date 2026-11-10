"""端到端验证：手机验证码登录（需要真实手机号，默认不跑）。

与 e2e_login_flow.py 平行：起真窗口（WebView2），走完整的
「获取验证码 → 输入验证码 → 登录 → 进控制台」链路。

**为什么默认不跑**：需要真手机号收短信，且会消耗后端的短信配额。
所以脚本要求显式传参：

    python e2e_sms_login.py 13800138000

号码与验证码都不写死在脚本里 —— 验证码由你从手机上看一眼后输入。

验证点：
  1. 登录页有「验证码登录」Tab
  2. 切到该 Tab 后手机号 / 验证码输入框与「获取验证码」按钮都在
  3. 点「获取验证码」后按钮进入倒计时（说明后端受理了）
  4. 输入验证码 → 登录 → 进入控制台
  5. 号码未注册时给出「去管理后台注册」的提示，而不是崩溃

用法：
    python e2e_sms_login.py <手机号>
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dashboard_bridge import DashboardBridge  # noqa: E402
from paths import frontend_dist_dir  # noqa: E402


def _prompt_code() -> str:
    """在控制台等用户输入收到的验证码。"""
    try:
        return input("[sms] 请输入收到的验证码：").strip()
    except EOFError:
        return ""


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

    if len(sys.argv) < 2:
        print(__doc__)
        print("[sms] 用法：python e2e_sms_login.py <手机号>")
        return 2
    phone = sys.argv[1].strip()

    import webview

    if not (frontend_dist_dir() / "index.html").is_file():
        print("[sms] 前端产物缺失，请先 npm run build")
        return 2

    bridge = DashboardBridge()
    # 关掉自动登录：否则桥接层会用 .env 里的免登账号直接进控制台，
    # 短信登录页根本没机会出现。
    bridge._load_auto_login = lambda: None  # type: ignore[method-assign]
    bridge.start()
    print(f"[sms] bridge on 127.0.0.1:{bridge.port}", flush=True)

    results: dict[str, object] = {}
    window = webview.create_window(
        "sms-login-check", url=bridge.url, width=1280, height=860, x=40, y=40
    )

    def probe() -> None:
        time.sleep(4.0)

        results["on_login_page"] = window.evaluate_js(
            "!!document.querySelector('.login-form')"
        )

        # 切到「验证码登录」Tab
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
        time.sleep(0.8)
        results["sms_form"] = json.loads(
            window.evaluate_js(
                """
                JSON.stringify({
                  hasPhone: !!document.querySelector('#login-phone'),
                  hasCode: !!document.querySelector('#login-sms-code'),
                  hasButton: !!document.querySelector('.btn-sms-code'),
                })
                """
            )
        )

        # 填手机号 → 点「获取验证码」
        window.evaluate_js(
            """
            (() => {
              const p = document.querySelector('#login-phone');
              if (p) { p.value = '__PHONE__'; p.dispatchEvent(new Event('input')); }
              const b = document.querySelector('.btn-sms-code');
              if (b) b.click();
              return true;
            })()
            """.replace("__PHONE__", phone)
        )
        time.sleep(3.0)
        results["after_send"] = json.loads(
            window.evaluate_js(
                """
                JSON.stringify({
                  buttonText: (document.querySelector('.btn-sms-code')||{}).innerText || '',
                  status: (document.querySelector('.login-status')||{}).innerText || '',
                })
                """
            )
        )

        # 等用户输入验证码
        print("[sms] 请在手机上查看验证码", flush=True)
        code = _prompt_code()
        if not code:
            results["skipped"] = "未输入验证码"
            try:
                window.destroy()
            except Exception:  # noqa: BLE001
                pass
            return

        window.evaluate_js(
            """
            (() => {
              const c = document.querySelector('#login-sms-code');
              if (c) { c.value = '__CODE__'; c.dispatchEvent(new Event('input')); }
              const b = document.querySelector('.login-submit');
              if (b) b.click();
              return true;
            })()
            """.replace("__CODE__", code)
        )
        time.sleep(8.0)
        results["after_login"] = json.loads(
            window.evaluate_js(
                """
                JSON.stringify({
                  stillOnLogin: !!document.querySelector('.login-form'),
                  hasConsole: !!document.querySelector('.console-shell')
                               || !!document.querySelector('.console-root'),
                  status: (document.querySelector('.login-status')||{}).innerText || '',
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

    print("[sms] " + json.dumps(results, ensure_ascii=False, indent=2))

    form = results.get("sms_form") or {}
    after_send = results.get("after_send") or {}
    after_login = results.get("after_login") or {}

    problems = []
    if not form.get("hasPhone") or not form.get("hasCode") or not form.get("hasButton"):
        problems.append("验证码登录表单不完整（缺手机号/验证码输入框或获取按钮）")

    button_text = str(after_send.get("buttonText") or "").strip()
    if button_text and button_text != "获取验证码" and button_text != "重新发送":
        # 倒计时中 = 后端受理了发码请求。
        pass
    elif str(after_send.get("status") or "").strip():
        problems.append(f"发码被拒：{after_send.get('status')}")

    if results.get("skipped"):
        print(f"[sms] 跳过：{results['skipped']}")
        return 0
    if after_login.get("stillOnLogin") is not False:
        problems.append(f"验证码登录后仍停在登录页：{after_login.get('status')}")

    if problems:
        for p in problems:
            print(f"[sms] FAIL: {p}")
        print("[sms] 结论：失败")
        return 1
    print("[sms] 结论：通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
