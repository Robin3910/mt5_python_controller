"""启动诊断的回归测试：体检报告、前端错误回传、日志落盘。

跑法：
    python -m pytest test_diagnostics.py
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import diagnostics  # noqa: E402
from dashboard_bridge import DashboardBridge  # noqa: E402


def _get(bridge: DashboardBridge, path: str, timeout: float = 5.0):
    url = f"http://127.0.0.1:{bridge.port}{path}"
    sep = "&" if "?" in path else "?"
    url += f"{sep}token={bridge.token}"
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post(bridge: DashboardBridge, path: str, payload: dict):
    url = f"http://127.0.0.1:{bridge.port}{path}?token={bridge.token}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=5.0) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ── 体检报告 ──


def test_environment_report_has_all_checks() -> None:
    """体检报告必须覆盖白屏的每一类成因。"""
    report = diagnostics.environment_report()
    for key in (
        "webview2_runtime",
        "dotnet",
        "frontend_dist",
        "webview_package",
        "port_binding",
        "config_files",
    ):
        assert key in report, f"体检报告缺少 {key} 项"
    assert "problems" in report, report.keys()
    assert "verdict" in report, report.keys()
    assert isinstance(report["problems"], list)
    print("[ok] 体检报告覆盖了全部检查项")


def test_environment_report_is_json_serializable() -> None:
    """报告要能落盘成 JSON —— 用户要把它发回来。"""
    report = diagnostics.environment_report()
    text = json.dumps(report, ensure_ascii=False, indent=2)
    assert json.loads(text)["ok"] == report["ok"]
    print("[ok] 体检报告可序列化为 JSON")


def test_dotnet_release_is_not_misjudged() -> None:
    """装了 .NET 4.8 却报「未安装」是最误导的一种误报，必须避免。

    同一 4.8 补丁在不同 Windows 上 release 号不同，只靠枚举表会误判。
    """
    result = diagnostics.check_dotnet()
    if result["release"]:
        assert result["ok"] is True, result
    print("[ok] .NET 检测不误判已安装的版本")


def test_frontend_dist_check() -> None:
    """前端产物检查要能分辨「有」与「没有」。"""
    result = diagnostics.check_frontend_dist()
    assert "ok" in result, result
    assert "dir" in result, result
    # 有 index.html 却少 JS，说明打包漏了资源，必须报不通过。
    assert result["ok"] == (result["index"] and result["files"] >= 2), result
    print("[ok] 前端产物检查结果自洽")


# ── 前端错误回传 ──


def test_client_log_endpoint_records_entries() -> None:
    """前端回传的脚本错误要能被桥接层收下。"""
    bridge = DashboardBridge()
    bridge.start()
    try:
        res = _post(
            bridge,
            "/api/client-log",
            {
                "entries": [
                    {
                        "level": "error",
                        "message": "TypeError: x is not a function",
                        "source": "http://127.0.0.1/index.js",
                        "line": 12,
                        "column": 5,
                        "stack": "at foo (index.js:12:5)",
                    }
                ]
            },
        )
        assert res["ok"] is True, res
        assert res["recorded"] == 1, res

        listed = _get(bridge, "/api/client-logs")
        assert listed["count"] == 1, listed
        entry = listed["entries"][0]
        assert entry["message"] == "TypeError: x is not a function", entry
        assert entry["line"] == 12, entry
    finally:
        bridge.stop()
    print("[ok] 前端脚本错误能被收集并回查")


def test_client_log_endpoint_rejects_garbage() -> None:
    """前端传了乱七八糟的东西也不能把桥接层搞崩。"""
    bridge = DashboardBridge()
    bridge.start()
    try:
        for payload in (
            {},
            {"entries": "not a list"},
            {"entries": [None, 123, "x"]},
        ):
            res = _post(bridge, "/api/client-log", payload)
            assert res["ok"] is True, (payload, res)
    finally:
        bridge.stop()
    print("[ok] 非法的前端日志载荷不会导致崩溃")


def test_client_log_requires_token() -> None:
    """日志接口也要带会话令牌，否则本机任意进程都能往里塞东西。"""
    bridge = DashboardBridge()
    bridge.start()
    try:
        url = (
            f"http://127.0.0.1:{bridge.port}/api/client-log"
            "?token=wrong&entries=[]"
        )
        req = urllib.request.Request(
            url,
            data=json.dumps({"entries": [{"message": "x"}]}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=5.0)
        except urllib.error.HTTPError as exc:
            assert exc.code in (401, 403), exc.code
        else:
            raise AssertionError("令牌错误的日志请求竟返回了 200")
    finally:
        bridge.stop()
    print("[ok] 日志接口拒绝无效的会话令牌")


# ── 体检接口 ──


def test_diagnose_endpoint() -> None:
    """桥接层要能直接给出体检报告（设置面板用得上）。"""
    bridge = DashboardBridge()
    bridge.start()
    try:
        res = _get(bridge, "/api/diagnose")
        assert "webview2_runtime" in res, res.keys()
        assert "verdict" in res, res.keys()
    finally:
        bridge.stop()
    print("[ok] 桥接层能提供体检报告")


if __name__ == "__main__":
    tests = [
        test_environment_report_has_all_checks,
        test_environment_report_is_json_serializable,
        test_dotnet_release_is_not_misjudged,
        test_frontend_dist_check,
        test_client_log_endpoint_records_entries,
        test_client_log_endpoint_rejects_garbage,
        test_client_log_requires_token,
        test_diagnose_endpoint,
    ]
    failures = 0
    for fn in tests:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"[FAIL] {fn.__name__}: {exc}")
    print("-" * 50)
    print(f"失败 {failures} / {len(tests)}")
    sys.exit(1 if failures else 0)
