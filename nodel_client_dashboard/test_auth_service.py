"""登录失败与会话失效的文案必须分开。"""
from email.message import Message
from io import BytesIO

import pytest
from urllib.error import HTTPError

import auth_service
from auth_service import DashboardAPI, DashboardAuthError


def _http_error(status: int, body: bytes) -> HTTPError:
    return HTTPError("http://127.0.0.1:9527/api/login", status, "error", Message(), BytesIO(body))


def test_login_401_is_bad_credentials_not_expired_session(monkeypatch):
    def reject(*_args, **_kwargs):
        raise _http_error(401, b'{"detail":"invalid credentials"}')

    monkeypatch.setattr(auth_service, "urlopen", reject)
    with pytest.raises(DashboardAuthError, match="账号或密码错误") as exc:
        DashboardAPI("http://127.0.0.1:9527").login("admin", "wrong")
    assert exc.value.status == 401


def test_expired_token_stays_session_message(monkeypatch):
    def reject(*_args, **_kwargs):
        raise _http_error(401, b'{"detail":"invalid or expired token"}')

    monkeypatch.setattr(auth_service, "urlopen", reject)
    with pytest.raises(DashboardAuthError, match="登录已失效，请重新登录"):
        DashboardAPI("http://127.0.0.1:9527").me("synthetic-token")
