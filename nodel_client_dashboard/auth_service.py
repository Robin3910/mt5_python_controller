"""用户登录与面板 API。凭证仅由内存会话持有，不从节点配置发现鉴权。"""
from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


class DashboardAuthError(RuntimeError):
    def __init__(self, message: str, *, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


# 只翻译已知的 detail，不把后端原始正文展示出来。
_HTTP_DETAIL = {
    401: {
        "invalid credentials": "账号或密码错误",
        "invalid totp code": "验证码错误或已过期",
        "invalid or expired login token": "登录已过期，请重新输入账号密码",
        "invalid current password": "当前密码不正确",
        "invalid password": "密码不正确",
        "missing bearer token": "登录已失效，请重新登录",
        "invalid or expired token": "登录已失效，请重新登录",
    },
}
_HTTP_STATUS = {
    401: "登录已失效，请重新登录",
    403: "无权操作该节点或尚未开通",
    404: "节点不存在或不可访问",
    409: "状态已变化，请刷新后重试",
}


def message_for_status(status: int, detail: str = "") -> str:
    """把后端状态码翻成面板文案。登录失败不能显示成会话过期。"""
    known = _HTTP_DETAIL.get(status, {}).get(str(detail or "").strip())
    if known:
        return known
    return _HTTP_STATUS.get(status, f"后端请求失败（HTTP {status}）")


def _error_detail(exc: HTTPError) -> str:
    try:
        payload = json.loads(exc.read().decode("utf-8"))
    except (OSError, ValueError, UnicodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    detail = payload.get("detail")
    return detail if isinstance(detail, str) else ""


def normalize_backend(base: str) -> str:
    parsed = urlparse(str(base or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password:
        raise DashboardAuthError("后端地址必须是 http:// 或 https://，且不能包含账号密码")
    if parsed.query or parsed.fragment:
        raise DashboardAuthError("后端地址不能包含查询参数或片段")
    return str(base).strip().rstrip("/")


class DashboardAPI:
    def __init__(self, base: str, *, timeout: float = 10.0) -> None:
        self.base = normalize_backend(base)
        self.timeout = timeout

    def request(self, path: str, *, token: str = "", body: dict | None = None, method: str = "GET"):
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
            return json.loads(raw.decode("utf-8")) if raw else {}
        except HTTPError as exc:
            # 不展示后端原始 body：它可能包含用户配置或凭证。
            raise DashboardAuthError(message_for_status(exc.code, _error_detail(exc)), status=exc.code) from exc
        except (URLError, OSError, TimeoutError) as exc:
            raise DashboardAuthError("无法连接后端；面板已锁定，运行中的节点与既有守护继续") from exc
        except (ValueError, UnicodeError) as exc:
            raise DashboardAuthError("后端返回数据无法解析") from exc

    def login(self, username: str, password: str) -> dict:
        return self.request("/api/login", method="POST", body={"username": username, "password": password})

    def login_2fa(self, login_token: str, code: str) -> dict:
        return self.request("/api/login/2fa", method="POST", body={"login_token": login_token, "totp_code": code})

    def me(self, token: str) -> dict:
        return self.request("/api/me", token=token)

    def nodes(self, token: str) -> list[dict]:
        nodes = self.request("/api/nodes", token=token)
        enrollments = self.request("/api/node-dashboard/enrollments", token=token)
        items = nodes.get("items", []) if isinstance(nodes, dict) else nodes
        merged = {str(n["node_id"]): n for n in (items or [])}
        for item in enrollments.get("items", []):
            merged[str(item["node_id"])] = item
        return list(merged.values())

    def enroll(self, token: str, mt5_login: int, name: str, server: str = "") -> dict:
        return self.request("/api/node-dashboard/enrollments", token=token, method="POST", body={"mt5_login": mt5_login, "name": name, "mt5_server": server})

    def authorize(self, token: str, payload: dict) -> dict:
        return self.request("/api/node-dashboard/actions", token=token, method="POST", body=payload)

    def result(self, token: str, operation_id: int, payload: dict) -> dict:
        return self.request(f"/api/node-dashboard/actions/{operation_id}/result", token=token, method="POST", body=payload)

    def credential(self, token: str, kind: str, payload: dict) -> dict:
        return self.request(f"/api/node-dashboard/credentials/{kind}", token=token, method="POST", body=payload)

    def daemon_grant(self, token: str, node_id: str) -> dict:
        return self.request("/api/node-dashboard/daemon-grants", token=token, method="POST", body={"node_id": node_id})

    def check_grant(self, grant_token: str) -> dict:
        return self.request("/api/node-dashboard/daemon-grants/check", method="POST", body={"grant_token": grant_token})

    def daemon_event(self, grant_token: str, payload: dict) -> dict:
        return self.request("/api/node-dashboard/daemon-grants/events", method="POST", body={"grant_token": grant_token, **payload})

    def replay_daemon_event(self, token: str, payload: dict) -> dict:
        return self.request("/api/node-dashboard/daemon-events/replay", token=token, method="POST", body=payload)
