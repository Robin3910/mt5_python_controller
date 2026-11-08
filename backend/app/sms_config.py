"""短信服务配置（运行期可切换）：开关、厂商、限频阈值。

配置落 `system_setting.sms_config`（JSON），密钥永远不进 DB，只从 env 读。

**为什么开关放 DB、密钥放 env：**
- 开关放 DB：出故障能一键关停注册，不用重启；
- 密钥放 env：DB 与备份里的密钥泄露风险被彻底排除。

**签名与模板码是写死的常量（FIXED_SIGN_NAME / FIXED_TEMPLATE_CODE）**：
这两个值对应阿里云那边已审核通过、已绑定本账号的那一份签名与模板，改了会直接
被阿里云拒掉（InvalidSignName / InvalidTemplateCode），发不出短信。所以它们
既不在 DB 里、也不给管理员改 —— 前端只读展示，后端写入时一律覆盖为常量。
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

# —— 写死的短信签名与模板码 ——————————————————————————————
# 阿里云侧的签名 / 模板是按本账号审核通过并绑定的，改了就发不出短信。
# 这里的常量是唯一事实来源：DB 里存的旧值会被 normalize_config 覆盖掉。
FIXED_SIGN_NAME = "恒创联众"
FIXED_TEMPLATE_CODE = "100001"

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
#
# —— 改绑手机号用的两个用途 ——
# rebind_new：验证**新**号所有权（码发到新号）
# rebind_old：验证**旧**号所有权（码发到旧号，用于「我不是被抢号的人」之外的
#             第二条身份确认路径）
# 这两个用途必须与 auth 隔离：登录用的码不能拿来完成改绑，反之亦然 ——
# 否则「能验证码登录」就等于「能改绑手机号」，等于把账号控制权交给短信通道。
PURPOSE_AUTH = "auth"
PURPOSE_REBIND_NEW = "rebind_new"
PURPOSE_REBIND_OLD = "rebind_old"
SMS_PURPOSES = (PURPOSE_AUTH, PURPOSE_REBIND_NEW, PURPOSE_REBIND_OLD)

DEFAULTS: dict = {
    # 默认启用：短信服务是注册 / 验证码登录的主链路，装好 AccessKey 就能用，
    # 不需要管理员先来配置页点一下开关。停用仍然随时可以（出故障一键止血）。
    "enabled": True,
    "provider": PROVIDER_ALIYUN_SMS_AUTH,
    "endpoint": "dypnsapi.aliyuncs.com",
    # 写死：管理员不可改，见模块顶部说明
    "sign_name": FIXED_SIGN_NAME,
    "template_code": FIXED_TEMPLATE_CODE,
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

    字段缺失用默认值补齐；数值越界夹回区间；provider 不认识则回落默认厂商；
    **sign_name / template_code 一律覆盖为写死的常量**（管理员传什么都无效）。
    """
    src = raw if isinstance(raw, dict) else {}
    out = dict(DEFAULTS)
    out["enabled"] = bool(src.get("enabled", DEFAULTS["enabled"]))
    out["require_approval"] = bool(src.get("require_approval", DEFAULTS["require_approval"]))
    # 签名 / 模板码写死，不接受外部输入 —— 改了阿里云会拒掉
    out["sign_name"] = FIXED_SIGN_NAME
    out["template_code"] = FIXED_TEMPLATE_CODE
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
    """配置是否「齐全」：签名与模板码都有值。

    目前签名与模板码是写死的常量（恒非空），所以这个判断恒为 True；保留它是为了
    将来换厂商时（签名 / 模板需要重新配）不用改调用点。
    """
    return bool(cfg.get("sign_name") and cfg.get("template_code"))


def is_usable(cfg: dict) -> bool:
    """短信服务是否**真的能用**：开关打开 + 配置齐全 + AccessKey 已注入。

    签名 / 模板码写死后，唯一还能让服务发不出短信的前置条件就是「没注入
    AccessKey」—— 那种情况下前端不该显示验证码入口（点了也是 503），所以这里
    把它也算进来。
    """
    return bool(cfg.get("enabled")) and is_configured(cfg) and has_credentials()


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
