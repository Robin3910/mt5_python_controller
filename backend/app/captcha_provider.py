"""阿里云验证码 2.0 的服务端校验封装（人机验证 / 滑块）。

前端流程：引入 `AliyunCaptcha.js` → 用户拖滑块 → 拿到 `captchaVerifyParam`
→ 连同业务参数发给本系统 → 本系统用本文件做**服务端二次校验**。

**为什么必须服务端校验**：前端返回的 `captchaVerifyParam` 是攻击者可控的，
只信前端等于没做验证。阿里云的校验结果还有一次性约束
（同一份 param 重复提交会返回 F008），服务端不校验就拦不住重放。

**失败策略**：厂商接口异常（网络/超时）时**默认放行**，避免阿里云侧抖动
导致正常用户发不出码；只有明确的 `VerifyResult=false`（风控判定不通过）才拦截。

注意：这是与「号码认证服务（PNVS）」**不同的产品**，需要单独开通、单独计费，
号码认证赠送的额度不能用于验证码。
"""
from __future__ import annotations

import base64
import logging
import os
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

from .settings import settings

logger = logging.getLogger(__name__)

# 阿里云验证码服务端校验接口：VerifyIntelligentCaptcha（2023-03-05）
# 旧版 VerifyCaptcha 官方已标注「不推荐」，统一用 Intelligent 版本。
DEFAULT_CAPTCHA_ENDPOINT = "captcha.cn-shanghai.aliyuncs.com"

# AES 参数（阿里云「加密模式」规范）：AES-256-CBC + PKCS7Padding
_AES_KEY_BYTES = 32
_IV_BYTES = 16


def encrypt_scene_id(scene_id: str, ekey: str, ttl_seconds: int = 3600) -> str:
    """生成 EncryptedSceneId：防 SceneId 泄露被他人盗用你的验证码额度。

    算法（与阿里云官方文档一致）：
    1. 明文 = f"{scene_id}&{timestamp}&{expireTime}"
       timestamp 是当前 Unix 秒；expireTime 是有效期秒数（1~86400）
    2. AES-256-CBC + PKCS7Padding 加密，密钥是控制台发的 ekey
       **每次必须用全新的随机 IV**（复用 IV 会让相同明文产生相同密文，
        等于把 SceneId 泄露出去）
    3. 拼接 IV(16B) + 密文，标准 Base64 编码

    ekey 必须解码为 32 字节 —— 控制台发的就是 32 字节的 Base64 串。
    """
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    raw_key = base64.b64decode(ekey)
    if len(raw_key) != _AES_KEY_BYTES:
        raise ValueError(
            f"ekey 长度不对：解码后 {len(raw_key)} 字节，应为 {_AES_KEY_BYTES} 字节"
        )

    ttl = max(1, min(86400, int(ttl_seconds)))
    plaintext = f"{scene_id}&{int(time.time())}&{ttl}".encode("utf-8")

    # 随机 IV —— 必须每次都不同
    iv = os.urandom(_IV_BYTES)

    padder = padding.PKCS7(algorithms.AES.block_size).padder()
    padded = padder.update(plaintext) + padder.finalize()

    encryptor = Cipher(algorithms.AES(raw_key), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()

    return base64.b64encode(iv + ciphertext).decode("ascii")



@dataclass
class CaptchaResult:
    """一次人机验证校验结果。

    ok=True 表示放行；ok=False 表示拦截（error_code 是阿里云的 VerifyCode）。
    """

    ok: bool
    verify_code: Optional[str] = None
    error_message: Optional[str] = None
    request_id: Optional[str] = None
    #: True 表示厂商接口本身异常（网络/超时），按「失败放行」策略处理
    degraded: bool = False


class CaptchaVerifier:
    """阿里云验证码服务端校验器。"""

    name = "aliyun_captcha"

    def __init__(self, access_key_id: str, access_key_secret: str, endpoint: str) -> None:
        self._access_key_id = access_key_id
        self._access_key_secret = access_key_secret
        self._endpoint = endpoint or DEFAULT_CAPTCHA_ENDPOINT

    @lru_cache(maxsize=8)
    def _client(self):
        """构造 SDK 客户端（进程内单例，按凭证+endpoint 缓存）。"""
        from alibabacloud_captcha20230305.client import Client
        from alibabacloud_tea_openapi import models as open_api_models

        config = open_api_models.Config(
            access_key_id=self._access_key_id,
            access_key_secret=self._access_key_secret,
            endpoint=self._endpoint,
        )
        return Client(config)

    def verify(self, captcha_verify_param: str, scene_id: str) -> CaptchaResult:
        """校验前端回传的 captchaVerifyParam。

        captcha_verify_param 必须**原样透传**给阿里云，任何修改都会导致校验失败
        （官方返回 F003）。
        """
        if not captcha_verify_param or not captcha_verify_param.strip():
            return CaptchaResult(
                ok=False,
                verify_code="F002",
                error_message="缺少人机验证参数",
            )
        try:
            from alibabacloud_captcha20230305 import models as captcha_models

            request = captcha_models.VerifyIntelligentCaptchaRequest(
                captcha_verify_param=captcha_verify_param,
                scene_id=scene_id or None,
            )
            client = self._client()
            response = client.verify_intelligent_captcha(request)
            body = response.body
            result = getattr(body, "result", None)
            verify_result = bool(getattr(result, "verify_result", False))
            verify_code = getattr(result, "verify_code", None)
            return CaptchaResult(
                ok=verify_result,
                verify_code=verify_code,
                error_message=None if verify_result else "人机验证未通过",
                request_id=getattr(body, "request_id", None),
            )
        except Exception as exc:  # noqa: BLE001 - 厂商异常按「失败放行」处理
            # 阿里云官方示例同样是「接口调用异常，默认放行」：
            # 验证码是风控增强手段，不该因为厂商抖动就把正常用户挡在门外。
            logger.error("Captcha verify failed, fallback to pass: %s", exc)
            return CaptchaResult(
                ok=True,
                verify_code="DEGRADED",
                error_message=str(exc),
                degraded=True,
            )


class FakeCaptchaVerifier:
    """测试用：恒定放行（也可配置成恒定拒绝以测拦截路径）。"""

    name = "fake"

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[str] = []

    def verify(self, captcha_verify_param: str, scene_id: str) -> CaptchaResult:
        self.calls.append(captcha_verify_param)
        if self.ok:
            return CaptchaResult(ok=True, verify_code="T001")
        return CaptchaResult(ok=False, verify_code="F001", error_message="人机验证未通过")


_verifier_cache: dict[tuple[str, str], CaptchaVerifier] = {}
_override: Optional[FakeCaptchaVerifier] = None


def get_verifier(endpoint: str = "") -> CaptchaVerifier:
    """按配置返回校验器实例（进程内缓存，避免重复构造 SDK 客户端）。"""
    if _override is not None:
        return _override
    key = (settings.aliyun_access_key_id, endpoint or DEFAULT_CAPTCHA_ENDPOINT)
    cached = _verifier_cache.get(key)
    if cached is None:
        cached = CaptchaVerifier(
            settings.aliyun_access_key_id,
            settings.aliyun_access_key_secret,
            key[1],
        )
        _verifier_cache[key] = cached
    return cached


def set_verifier(verifier: Optional[FakeCaptchaVerifier]) -> None:
    """测试注入：传 None 恢复真实实现。"""
    global _override
    _override = verifier


def reset_verifier_cache() -> None:
    """清掉实例缓存与测试覆盖（测试用例之间隔离用）。"""
    global _override
    _override = None
    _verifier_cache.clear()


__all__ = [
    "DEFAULT_CAPTCHA_ENDPOINT",
    "CaptchaResult",
    "CaptchaVerifier",
    "FakeCaptchaVerifier",
    "encrypt_scene_id",
    "get_verifier",
    "reset_verifier_cache",
    "set_verifier",
]
