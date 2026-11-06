"""短信服务配置（运行期可切换）：开关、厂商、签名 / 模板、限频阈值。

配置落 `system_setting.sms_config`（JSON），密钥永远不进 DB，只从 env 读。

**为什么开关与模板放 DB、密钥放 env：**
- 开关放 DB：出故障能一键关停注册，不用重启；
- 模板 Code 放 DB：不重启就能换模板（赠送模板固定 100001，但仍留出余地）；
- 密钥放 env：DB 与备份里的密钥泄露风险被彻底排除。

配置缺失时（sign_name / template_code 为空）视为「未配置」——
所有短信接口返回 404，前端自动隐藏验证码入口。
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from .db import SessionLocal
from .orm import SystemSetting
from .settings import settings

logger = logging.getLogger(__name__)

KEY_SMS_CONFIG = "sms_config"

# 厂商标识；provider 字段是为将来切换厂商预留的（当前只实现了阿里云短信认证）
PROVIDER_ALIYUN_SMS_AUTH = "aliyun_sms_auth"
PROVIDER_TENCENTCLOUD_SMS = "tencentcloud_sms"
SUPPORTED_PROVIDERS = (PROVIDER_ALIYUN_SMS_AUTH, PROVIDER_TENCENTCLOUD_SMS)

# 用途：验证码的用途隔离。Redis key 里带 purpose，防止跨用途复用。
#
# **只有一个用途 auth**：登录与注册共用同一种验证码。
#
# 为什么合并（而不是 register / login 各一个）：
# 用户从登录入口点「获取验证码」时，还不知道这个号有没有注册过 —— 如果登录发 login 码、
# 未注册再发 register 码，用户就得收两次短信。合并成一个用途后，一次验证码即可
# 同时支撑「登录」与「注册」，由服务端在校验通过后根据该号码是否已注册来决定走向。
#
# 用途隔离本身仍然保留（key 里仍带 purpose），只是取值从两个收敛为一个。
# 真正的防跨用改由「票据」承担：登录票据与注册票据的允许用途不同（见 security.py）。
PURPOSE_AUTH = "auth"
SMS_PURPOSES = (PURPOSE_AUTH,)

DEFAULTS: dict = {
    "enabled": False,
    "provider": PROVIDER_ALIYUN_SMS_AUTH,
    "endpoint": "dypnsapi.aliyuncs.com",
    "sign_name": "",
    "template_code": "100001",
    # 新注册用户是否需要管理员审核后才启用（默认需要）
    "require_approval": True,
    "code_ttl_seconds": 300,
    "send_interval_seconds": 60,
    "daily_limit_per_phone": 10,
    "hourly_limit_per_ip": 20,
    "max_verify_attempts": 5,
    # —— 全局每日发送上限（费用失控的最后一道闸；0 = 不限）——
    "daily_limit_total": 500,
    # —— 每 IP 每日发送上限（换 IP 也绕不开号/日的双闸）——
    "daily_limit_per_ip": 50,
    # —— 阿里云验证码（人机验证）——
    # 未开通时 captcha_enabled=false，整条人机验证链路不生效。
    "captcha_enabled": False,
    "captcha_prefix": "",       # 身份标（控制台概览页获取）
    "captcha_scene_id": "",     # 场景 ID（控制台场景管理获取）
    "captcha_endpoint": "captcha.cn-shanghai.aliyuncs.com",
    # 自适应：达到这些阈值才要求人机验证（低于阈值直接放行，正常用户无感）
    "captcha_phone_threshold": 3,   # 该号码 24h 内已发送次数
    "captcha_ip_threshold": 5,      # 该 IP 1h 内已发送次数
    "captcha_new_account": True,    # 新注册账号（< 24h）发码也要人机验证
}

# 配置项的合法区间；越界值夹回区间，避免运维手滑写出一个能把自己锁死的配置。
_LIMITS: dict[str, tuple[int, int]] = {
    "code_ttl_seconds": (60, 1800),
    "send_interval_seconds": (10, 3600),
    "daily_limit_per_phone": (1, 1000),
    "hourly_limit_per_ip": (1, 5000),
    "max_verify_attempts": (1, 20),
    # 0 = 不限（允许关掉总闸，但配置页会提示风险）
    "daily_limit_total": (0, 1000000),
    "daily_limit_per_ip": (0, 100000),
    # 自适应阈值：0 = 该维度永远不触发人机验证
    "captcha_phone_threshold": (0, 100),
    "captcha_ip_threshold": (0, 1000),
}


def _clamp(name: str, value) -> int:
    lo, hi = _LIMITS[name]
    try:
        num = int(value)
    except (TypeError, ValueError):
        return int(DEFAULTS[name])
    return max(lo, min(hi, num))


def normalize_config(raw: Optional[dict]) -> dict:
    """把任意来源的配置规整成一份完整、合法、可直接使用的字典。

    字段缺失用默认值补齐；数值越界夹回区间；provider 不认识则回落默认厂商。
    """
    src = raw if isinstance(raw, dict) else {}
    out = dict(DEFAULTS)
    out["enabled"] = bool(src.get("enabled", DEFAULTS["enabled"]))
    out["require_approval"] = bool(src.get("require_approval", DEFAULTS["require_approval"]))
    out["sign_name"] = str(src.get("sign_name") or "").strip()
    out["template_code"] = str(src.get("template_code") or "").strip()
    out["endpoint"] = str(src.get("endpoint") or DEFAULTS["endpoint"]).strip()
    provider = str(src.get("provider") or DEFAULTS["provider"]).strip()
    out["provider"] = provider if provider in SUPPORTED_PROVIDERS else DEFAULTS["provider"]
    # 人机验证相关
    out["captcha_enabled"] = bool(src.get("captcha_enabled", DEFAULTS["captcha_enabled"]))
    out["captcha_new_account"] = bool(
        src.get("captcha_new_account", DEFAULTS["captcha_new_account"])
    )
    out["captcha_prefix"] = str(src.get("captcha_prefix") or "").strip()
    out["captcha_scene_id"] = str(src.get("captcha_scene_id") or "").strip()
    out["captcha_endpoint"] = str(
        src.get("captcha_endpoint") or DEFAULTS["captcha_endpoint"]
    ).strip()
    for name in _LIMITS:
        out[name] = _clamp(name, src.get(name, DEFAULTS[name]))
    return out


def is_configured(cfg: dict) -> bool:
    """配置是否「可用」：签名与模板码都必须有值，缺一即视为未配置。"""
    return bool(cfg.get("sign_name") and cfg.get("template_code"))


def has_credentials() -> bool:
    """阿里云 AccessKey 是否已注入 env（没注入就发不出短信）。"""
    return bool(settings.aliyun_access_key_id and settings.aliyun_access_key_secret)


async def _get_setting(key: str) -> Optional[SystemSetting]:
    async with SessionLocal() as s:
        return await s.get(SystemSetting, key)


async def _upsert_setting(key: str, value: str) -> None:
    async with SessionLocal() as s:
        row = await s.get(SystemSetting, key)
        if row is None:
            s.add(SystemSetting(key=key, value=value))
        else:
            row.value = value
        await s.commit()


async def get_sms_config() -> dict:
    """读取当前短信配置（规整后的完整字典）。从未配置过返回默认值。"""
    row = await _get_setting(KEY_SMS_CONFIG)
    raw: dict = {}
    if row and row.value:
        try:
            parsed = json.loads(row.value)
            if isinstance(parsed, dict):
                raw = parsed
        except json.JSONDecodeError:
            logger.warning("sms_config JSON 损坏，按默认值处理")
    return normalize_config(raw)


async def set_sms_config(cfg: Optional[dict]) -> dict:
    """写入短信配置（先规整再落库）；返回规整后的结果。"""
    normalized = normalize_config(cfg)
    await _upsert_setting(
        KEY_SMS_CONFIG,
        json.dumps(normalized, ensure_ascii=False, separators=(",", ":")),
    )
    return normalized
