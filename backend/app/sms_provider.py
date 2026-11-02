"""短信厂商抽象层：业务代码只认 `SmsProvider` 接口，不认具体厂商。

**这是全系统唯一的厂商耦合点。** 选阿里云短信认证是因为它能当天上线（免资质报备），
不是因为它更好 —— 将来若需要发通知短信、要自定义品牌签名、要合并结算，切到普通
短信服务或腾讯云时只改这一个文件。

验证码采用「模式 B」：**自己生成 + 自己校验**，阿里云只负责下发。
因此本层的 `send_code` 接收明文验证码，由调用方（sms_service）负责哈希与校验。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional, Protocol

from .settings import settings

logger = logging.getLogger(__name__)


def _local_number(phone: str) -> str:
    """把 E.164（+8613800138000）转成阿里云要的国内纯号码（13800138000）。

    阿里云 SendSmsVerifyCode 的 PhoneNumber 只放号码本体，国家码由 CountryCode 传。
    """
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    if digits.startswith("86") and len(digits) == 13:
        digits = digits[2:]
    return digits


@dataclass
class SmsResult:
    """一次下发结果。

    ok=False 时 `error_code` / `error_message` 是厂商返回的原始错误，
    用于落审计与排障；**不要直接透给前端**（会泄露账号 / 余额状态）。
    """

    ok: bool
    provider: str
    request_id: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    biz_id: Optional[str] = None


class SmsProvider(Protocol):
    """厂商抽象：下发一个自己生成的验证码。"""

    name: str

    def send_code(
        self, phone: str, code: str, *, ttl_minutes: int, sign_name: str, template_code: str,
    ) -> SmsResult:
        ...


class AliyunSmsAuthProvider:
    """阿里云「号码认证服务 PNVS」下的「短信认证」子服务。

    API：SendSmsVerifyCode（dypnsapi.aliyuncs.com）。
    赠送签名与赠送模板必须配套使用，不可交叉。
    模板变量固定为 `code` 与 `min` 两个 key；`min` 只填数字（"5"，不是 "5分钟"）。

    客户端单例复用（lru_cache），避免每次请求重新握手。
    """

    name = "aliyun_sms_auth"

    def __init__(self, access_key_id: str, access_key_secret: str, endpoint: str) -> None:
        self._access_key_id = access_key_id
        self._access_key_secret = access_key_secret
        self._endpoint = endpoint

    @lru_cache(maxsize=8)
    def _client(self):
        """构造 SDK 客户端（进程内单例，按凭证+endpoint 缓存）。"""
        from alibabacloud_dypnsapi20170525.client import Client
        from alibabacloud_tea_openapi import models as open_api_models

        config = open_api_models.Config(
            access_key_id=self._access_key_id,
            access_key_secret=self._access_key_secret,
            endpoint=self._endpoint,
        )
        return Client(config)

    def send_code(
        self, phone: str, code: str, *, ttl_minutes: int, sign_name: str, template_code: str,
    ) -> SmsResult:
        import json

        from alibabacloud_dypnsapi20170525 import models as dypns_models

        try:
            client = self._client()
            request = dypns_models.SendSmsVerifyCodeRequest(
                # PhoneNumber 只放国内号码（不带国家码），国家码走 CountryCode。
                # 传 E.164（+86138...）会被阿里云拒掉。
                phone_number=_local_number(phone),
                sign_name=sign_name,
                template_code=template_code,
                # 模式 B：模板变量里直接带我们自己生成的验证码
                template_param=json.dumps(
                    {"code": code, "min": str(int(ttl_minutes))}, ensure_ascii=False
                ),
                valid_time=int(ttl_minutes) * 60,
                country_code="86",
                # 我们的验证码固定 6 位纯数字；不传的话阿里云默认只发 4 位，
                # 短信里 4 位与我们校验的 6 位对不上。
                code_length=6,
                code_type=1,
                # Interval / DuplicatePolicy 由我们自己这层兜住（可配置且不保证），
                # 这里保持厂商默认值，避免两套冷却叠在一起导致用户拿不到码。
            )
            response = client.send_sms_verify_code(request)
            body = getattr(response, "body", None)
            code_value = getattr(body, "code", None)
            request_id = getattr(body, "request_id", None)
            message = getattr(body, "message", None)
            if code_value == "OK":
                return SmsResult(
                    ok=True,
                    provider=self.name,
                    request_id=request_id,
                    biz_id=getattr(body, "biz_id", None),
                )
            return SmsResult(
                ok=False,
                provider=self.name,
                request_id=request_id,
                error_code=str(code_value or "unknown"),
                error_message=str(message or ""),
            )
        except Exception as exc:  # noqa: BLE001
            # 厂商 SDK 抛的异常类型很多（含网络 / 鉴权），统一收敛成失败结果，
            # 由调用方转 503 并落审计；原始错误只进日志，不透给前端。
            logger.exception("aliyun send_sms_verify_code failed: %s", exc)
            return SmsResult(
                ok=False,
                provider=self.name,
                error_code="exception",
                error_message=str(exc),
            )


class FakeSmsProvider:
    """测试用：把验证码记在内存里，不真发短信。

    单元 / 集成测试注入它，既省钱也不依赖阿里云账号配置。
    """

    name = "fake"

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.fail_with: Optional[str] = None

    def send_code(
        self, phone: str, code: str, *, ttl_minutes: int, sign_name: str, template_code: str,
    ) -> SmsResult:
        if self.fail_with:
            return SmsResult(
                ok=False, provider=self.name, error_code=self.fail_with, error_message=self.fail_with
            )
        self.sent.append(
            {
                "phone": phone,
                "code": code,
                "ttl_minutes": ttl_minutes,
                "sign_name": sign_name,
                "template_code": template_code,
            }
        )
        return SmsResult(ok=True, provider=self.name, request_id="fake-request-id")


_provider_cache: dict[tuple[str, str, str], SmsProvider] = {}


def get_provider(provider_name: str, *, endpoint: str) -> SmsProvider:
    """按配置返回厂商实例（进程内缓存，避免重复构造 SDK 客户端）。"""
    key = (provider_name, endpoint, settings.aliyun_access_key_id)
    cached = _provider_cache.get(key)
    if cached is not None:
        return cached
    if provider_name == "tencentcloud_sms":
        # 预留：将来切厂商时在这里加分支即可，业务代码不动
        raise NotImplementedError("腾讯云短信服务尚未接入")
    provider = AliyunSmsAuthProvider(
        settings.aliyun_access_key_id,
        settings.aliyun_access_key_secret,
        endpoint,
    )
    _provider_cache[key] = provider
    return provider


def set_provider(provider: SmsProvider) -> None:
    """注入自定义厂商（测试用）。"""
    _provider_cache[("__override__", "", "")] = provider


def reset_provider_cache() -> None:
    """清空厂商缓存（测试用）。"""
    _provider_cache.clear()


def current_provider(provider_name: str, *, endpoint: str) -> SmsProvider:
    """取当前生效的厂商（测试注入优先）。"""
    override = _provider_cache.get(("__override__", "", ""))
    if override is not None:
        return override
    return get_provider(provider_name, endpoint=endpoint)
