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
    # 短信登录链路的 detail（后端 phone_auth / sms_service）。
    # 这些文案本身就是中文，映射过来是为了让面板文案统一，
    # 同时避免把后端原始 detail 直出到界面。
    400: {
        "验证码错误或已过期": "验证码错误或已过期",
        "手机号格式不正确": "请输入正确的 11 位手机号",
        "手机号不能为空": "请输入正确的 11 位手机号",
        "请输入验证码": "请输入验证码",
    },
    403: {
        "账号已被禁用，请联系管理员": "账号已被禁用，请联系管理员",
    },
    404: {
        "该手机号尚未注册": "该手机号尚未注册，请在管理后台注册",
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
    text = str(detail or "").strip()
    known = _HTTP_DETAIL.get(status, {}).get(text)
    if known:
        return known
    # 后端中文 detail 本身就是给用户看的文案（如限频提示「操作过于频繁，请 60 秒后再试」，
    # 短信的「手机号格式不正确」）。这类直接透传，不必逐条硬编码 ——
    # 限频文案带动态数字，逐条枚举必然漏。
    # 英文 detail（如 "not found"）不透传：它不是用户可读文案，
    # 交给下面的状态码兜底。
    if text and _has_cjk(text):
        return text
    return _HTTP_STATUS.get(status, f"后端请求失败（HTTP {status}）")


def _has_cjk(text: str) -> bool:
    """判断字符串里是否含中日韩字符。"""
    return any("一" <= ch <= "鿿" for ch in text)


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

    # ── 手机号验证码登录 ──
    # 后端接口见 backend/app/phone_auth.py：
    #   GET  /api/auth/sms/status          短信服务是否可用
    #   POST /api/auth/sms/send            发验证码
    #   POST /api/auth/sms/verify          校验码 → 票据 + 下一步走向
    #   POST /api/auth/sms/login           票据/码 → JWT（或要求 2FA）
    #
    # 「一次验证码、两种去向」：登录与注册共用同一种验证码，
    # 校验通过后由服务端根据号码是否已注册告知去向（见 next_action）。
    # 面板只做登录，不做注册 —— 未注册时提示去管理后台注册。

    def sms_status(self) -> dict:
        """短信服务可用性。

        后端未配置短信时这个接口返回 404（detail="not found"）——
        那是「没开这个功能」，不是错误，所以这里自己吞掉，
        统一返回 enabled=False，让前端据此隐藏验证码入口。
        """
        try:
            return self.request("/api/auth/sms/status")
        except DashboardAuthError as exc:
            if exc.status in (404, 405):
                return {"enabled": False}
            raise

    def sms_send(self, phone: str, purpose: str = "auth") -> dict:
        """发送验证码。返回 {ok, expire_in, cooldown}。"""
        return self.request(
            "/api/auth/sms/send", method="POST", body={"phone": phone, "purpose": purpose}
        )

    def sms_verify(self, phone: str, code: str, purpose: str = "auth") -> dict:
        """校验验证码。返回 {ticket, next_action, ...}。"""
        return self.request(
            "/api/auth/sms/verify",
            method="POST",
            body={"phone": phone, "code": code, "purpose": purpose},
        )

    def sms_login(self, phone: str, ticket: str = "", code: str = "") -> dict:
        """验证码登录：票据或明文码换 JWT。

        已开启 2FA 的用户返回 {requires_2fa: True, login_token: ...}，
        面板据此转第二步（见 login_2fa）。
        """
        body: dict = {"phone": phone}
        if ticket:
            body["ticket"] = ticket
        if code:
            body["code"] = code
        return self.request("/api/auth/sms/login", method="POST", body=body)

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

    def stats(self, token: str) -> dict[str, dict]:
        """本机面板的汇总统计（今日信号数 / 平均延迟 / 服务器名）。

        按 node_id 索引；后端没这个接口时（旧版本后端）返回空 dict，
        面板据此把指标显示成「暂无数据」而不是 0。
        """
        try:
            data = self.request("/api/node-dashboard/stats", token=token)
        except DashboardAuthError as exc:
            # 只有「后端版本较老、没这个接口」才降级为空；
            # 鉴权失败（401/403）必须原样抛出，否则面板会假装还���登录。
            if exc.status in (404, 405):
                return {}
            raise
        items = data.get("items", []) if isinstance(data, dict) else []
        return {str(item["node_id"]): item for item in (items or []) if item.get("node_id")}

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
