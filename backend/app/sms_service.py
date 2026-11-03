"""短信业务编排：号码归一化、多维限频、验证码生成与生命周期。

与「注册还是登录」的业务语义解耦：`phone_auth` 只做编排，验证码怎么生成、
怎么存、怎么限频都收敛在这里。

**验证码采用「模式 B」（自己生成 + 自己校验）**，阿里云只负责下发。
好处：register / login 的验证码能按用途隔离、能限制错误次数、能自定义有效期。
代价：`CheckSmsVerifyCode` 不再适用，核验逻辑就是本文件里的哈希比对。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from .redis_store import (
    K_SMS_CAPTCHA_FAIL,
    K_SMS_SEND_IP,
    K_SMS_SEND_IP_DAY,
    K_SMS_SEND_PHONE,
    K_SMS_SEND_TOTAL,
    RedisStore,
)
from .security import compare_secret
from .settings import settings
from .sms_config import PURPOSE_AUTH, SMS_PURPOSES
from .sms_provider import SmsResult, current_provider

logger = logging.getLogger(__name__)

# 中国大陆手机号：1 开头、第二位 3-9、共 11 位
CN_PHONE_RE = re.compile(r"^1[3-9]\d{9}$")
CODE_LENGTH = 6
CODE_TTL_SECONDS = 300
MAX_VERIFY_ATTEMPTS = 5

# 限频命中的统一文案（前端直接展示）
class SmsRateLimited(Exception):
    """限频命中。retry_after 为建议等待秒数。"""

    def __init__(self, retry_after: int, message: str = "") -> None:
        self.retry_after = int(retry_after)
        self.message = message or f"操作过于频繁，请 {self.retry_after} 秒后再试"
        super().__init__(self.message)


class SmsInvalidPhone(Exception):
    """手机号格式非法。"""


def normalize_phone(raw: str) -> str:
    """把各种写法的手机号归一化成 E.164（+86xxxxxxxxxxx）。

    接受：`13800138000` / `+8613800138000` / `8613800138000` / `138 0013 8000` /
    `138-0013-8000`。只支持中国大陆号码（阿里云短信认证也只覆盖大陆）。
    非法号码抛 SmsInvalidPhone。
    """
    text = str(raw or "").strip()
    if not text:
        raise SmsInvalidPhone("手机号不能为空")
    # 去掉分隔符（空格 / 短横线）
    text = re.sub(r"[\s\-]", "", text)
    if text.startswith("+86"):
        text = text[3:]
    elif text.startswith("86") and len(text) == 13:
        text = text[2:]
    if not CN_PHONE_RE.match(text):
        raise SmsInvalidPhone("手机号格式不正确")
    return f"+86{text}"


def mask_phone(phone: str) -> str:
    """脱敏：+8613800138000 -> 138****8000（审计里只放脱敏后的号码）。

    去掉国家码前缀再脱敏 —— 前 3 位是号段（138），后 4 位是用户号，
    中间 4 位打码。
    """
    digits = re.sub(r"\D", "", phone or "")
    if digits.startswith("86") and len(digits) == 13:
        digits = digits[2:]
    if len(digits) < 7:
        return "***"
    return f"{digits[:3]}****{digits[-4:]}"


def _salt() -> str:
    """验证码哈希用的盐：优先 env 里的独立盐，缺省退回 jwt_secret。

    独立盐的好处：轮换 jwt_secret（安全整改）不会让存量验证码集体失效。
    """
    return settings.sms_code_salt or settings.jwt_secret


def hash_code(code: str) -> str:
    """验证码哈希（与项目现有 hash_token 风格一致：sha256 十六进制）。"""
    return hashlib.sha256(f"{code}{_salt()}".encode("utf-8")).hexdigest()


def generate_code() -> str:
    """生成 6 位纯数字验证码（用 secrets 而非 random，避免可预测）。"""
    return f"{secrets.randbelow(10 ** CODE_LENGTH):0{CODE_LENGTH}d}"


def _day_stamp(now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc).astimezone()
    return now.strftime("%Y%m%d")


def _hour_stamp(now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc).astimezone()
    return now.strftime("%Y%m%d%H")


def _end_of_day_seconds(now: Optional[datetime] = None) -> int:
    """到当天结束的秒数（用于给每日计数 key 设 TTL）。"""
    now = now or datetime.now(timezone.utc).astimezone()
    end = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), tzinfo=now.tzinfo)
    return max(60, int((end - now).total_seconds()))


async def check_rate_limits(
    store: RedisStore, *, phone: str, ip: str, cfg: dict, now: Optional[datetime] = None,
) -> None:
    """发送前的多维限频检查；命中任一维就抛 SmsRateLimited。

    维度（默认阈值见 sms_config.DEFAULTS）：
    - 同手机号发送间隔：send_interval_seconds（防连点）
    - 同手机号每日发送：daily_limit_per_phone（防刷费）
    - 同 IP 每小时发送：hourly_limit_per_ip（防批量轰炸）
    - 同 IP 每日发送：daily_limit_per_ip（换 IP 也要付出代价）
    - 全局每日发送：daily_limit_total（费用失控的最后一道闸；0 = 不限）
    """
    interval = int(cfg.get("send_interval_seconds") or 60)
    daily = int(cfg.get("daily_limit_per_phone") or 10)
    hourly = int(cfg.get("hourly_limit_per_ip") or 20)
    daily_ip = int(cfg.get("daily_limit_per_ip") or 0)
    daily_total = int(cfg.get("daily_limit_total") or 0)

    # 冷却锁：存在即说明上一次发送还没过间隔
    if not await store.acquire_sms_lock(phone, interval):
        raise SmsRateLimited(interval)

    day = _day_stamp(now)
    hour = _hour_stamp(now)
    phone_count = await store.incr_sms_send_counter(
        K_SMS_SEND_PHONE.format(phone, day), _end_of_day_seconds(now)
    )
    ip_count = await store.incr_sms_send_counter(
        K_SMS_SEND_IP.format(ip or "unknown", hour), 3600
    )
    total_count = await store.incr_sms_send_counter(
        K_SMS_SEND_TOTAL.format(day), _end_of_day_seconds(now)
    )
    try:
        if phone_count > daily:
            raise SmsRateLimited(interval, f"该手机号今日发送次数已达上限（{daily} 条）")
        if ip_count > hourly:
            raise SmsRateLimited(interval, f"当前来源今日发送次数已达上限（{hourly} 条）")
        if daily_ip:
            ip_day_count = await store.incr_sms_send_counter(
                K_SMS_SEND_IP_DAY.format(ip or "unknown", day), _end_of_day_seconds(now)
            )
            if ip_day_count > daily_ip:
                raise SmsRateLimited(
                    interval, f"当前来源今日发送次数已达上限（{daily_ip} 条）"
                )
        if daily_total and total_count > daily_total:
            raise SmsRateLimited(interval, "今日短信发送总量已达上限，请稍后再试")
    except SmsRateLimited:
        # 超限时把刚加的计数退回去一点没意义（宁可略严），但冷却锁要放开，
        # 否则用户要等 interval 才能重试。
        await store.release_sms_lock(phone)
        raise


async def today_send_count(store: RedisStore, now: Optional[datetime] = None) -> int:
    """今日已发送条数（配置页展示用，只读）。"""
    return await store.get_sms_send_counter(K_SMS_SEND_TOTAL.format(_day_stamp(now)))


async def needs_captcha(
    store: RedisStore, *, phone: str, ip: str, cfg: dict, now: Optional[datetime] = None,
) -> bool:
    """自适应人机验证判定：只在「有风险」时才要求滑块，正常用户无感放行。

    触发条件（任一命中即需要）：
    - 该号码 24h 内已发送 >= captcha_phone_threshold 次
    - 该 IP 1h 内已发送 >= captcha_ip_threshold 次
    - 该号码是刚注册的新账号（< 24h）且 captcha_new_account 开启
    - 该 IP 近期人机验证连续失败过多（疑似脚本）

    captcha_enabled=false 或未配 prefix/scene_id 时永远返回 False
    （没开通验证码产品时不能让发码功能挂掉）。
    """
    if not cfg.get("captcha_enabled"):
        return False
    if not (cfg.get("captcha_prefix") and cfg.get("captcha_scene_id")):
        return False

    phone_threshold = int(cfg.get("captcha_phone_threshold") or 0)
    ip_threshold = int(cfg.get("captcha_ip_threshold") or 0)

    if phone_threshold:
        phone_day = await store.get_sms_send_counter(
            K_SMS_SEND_PHONE.format(phone, _day_stamp(now))
        )
        if phone_day >= phone_threshold:
            return True

    if ip_threshold:
        ip_hour = await store.get_sms_send_counter(
            K_SMS_SEND_IP.format(ip or "unknown", _hour_stamp(now))
        )
        if ip_hour >= ip_threshold:
            return True

    if cfg.get("captcha_new_account"):
        if await _is_new_account(store, phone):
            return True

    # 人机验证连续失败多次：疑似脚本，直接要求滑块（而不是继续放行）
    fail_count = await store.get_sms_send_counter(
        K_SMS_CAPTCHA_FAIL.format(ip or "unknown", _hour_stamp(now))
    )
    if fail_count >= 5:
        return True

    return False


async def _is_new_account(store: RedisStore, phone: str) -> bool:
    """该手机号对应账号是否是「刚注册的新号」（24h 内注册）。

    新号是最典型的刷量工具（注册一个号就能开始发码），所以要额外加人机验证。

    查不到用户（还没注册）不算新号 —— 未注册号码走的是「注册后」的路径。
    """
    from . import user_service

    try:
        user = await user_service.get_user_by_phone(phone)
    except Exception:  # noqa: BLE001 - 用户表异常不该阻断发码
        return False
    if user is None:
        return False
    created = getattr(user, "created_at", None)
    if created is None:
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - created
    return age < timedelta(hours=24)


async def record_captcha_failure(store: RedisStore, ip: str, now: Optional[datetime] = None) -> None:
    """记一次人机验证失败（按 IP 按小时计数）。"""
    await store.incr_sms_send_counter(
        K_SMS_CAPTCHA_FAIL.format(ip or "unknown", _hour_stamp(now)), 3600
    )


async def issue_code(
    store: RedisStore, *, phone: str, purpose: str, cfg: dict,
) -> tuple[str, SmsResult]:
    """生成验证码、落 Redis（存哈希）、并调用厂商下发。

    返回 (明文验证码, 下发结果)。明文只在内存里走一趟，不落盘不落库。
    """
    if purpose not in SMS_PURPOSES:
        raise ValueError(f"unsupported purpose: {purpose}")
    code = generate_code()
    ttl = int(cfg.get("code_ttl_seconds") or CODE_TTL_SECONDS)
    payload = {
        "code_hash": hash_code(code),
        "attempts": 0,
        "issued_at": int(time.time()),
    }
    await store.save_sms_code(purpose, phone, payload, ttl)

    provider = current_provider(str(cfg.get("provider") or ""), endpoint=str(cfg.get("endpoint") or ""))
    # 厂商 SDK 是同步的，丢到线程里跑，别把事件循环堵住
    result = await asyncio.to_thread(
        provider.send_code,
        phone,
        code,
        ttl_minutes=max(1, ttl // 60),
        sign_name=str(cfg.get("sign_name") or ""),
        template_code=str(cfg.get("template_code") or ""),
    )
    if not result.ok:
        # 下发失败就把刚存的验证码删掉，避免用户拿到一个永远收不到的码
        await store.delete_sms_code(purpose, phone)
    return code, result


async def verify_code(
    store: RedisStore, *, phone: str, code: str, purpose: str, cfg: dict,
) -> bool:
    """校验验证码；通过即焚（一次性），失败则累加次数，超限立即作废。

    purpose 进 key —— 不同用途的码不能互相使用。
    """
    if purpose not in SMS_PURPOSES:
        return False
    payload = await store.get_sms_code(purpose, phone)
    if not payload:
        return False
    attempts = int(payload.get("attempts") or 0)
    max_attempts = int(cfg.get("max_verify_attempts") or MAX_VERIFY_ATTEMPTS)
    if attempts >= max_attempts:
        # 已超限的码直接作废
        await store.delete_sms_code(purpose, phone)
        return False
    if compare_secret(hash_code(str(code or "").strip()), str(payload.get("code_hash") or "")):
        await store.delete_sms_code(purpose, phone)
        return True
    attempts += 1
    if attempts >= max_attempts:
        await store.delete_sms_code(purpose, phone)
    else:
        payload["attempts"] = attempts
        # 剩余有效期用 issued_at 推算（Redis 自身的 TTL 才是权威，这里只是不续期）
        issued = int(payload.get("issued_at") or int(time.time()))
        ttl = int(cfg.get("code_ttl_seconds") or CODE_TTL_SECONDS)
        await store.save_sms_code(purpose, phone, payload, max(1, ttl - (int(time.time()) - issued)))
    return False


__all__ = [
    "CN_PHONE_RE",
    "CODE_LENGTH",
    "CODE_TTL_SECONDS",
    "MAX_VERIFY_ATTEMPTS",
    "PURPOSE_AUTH",
    "SMS_PURPOSES",
    "SmsInvalidPhone",
    "SmsRateLimited",
    "check_rate_limits",
    "generate_code",
    "hash_code",
    "issue_code",
    "mask_phone",
    "needs_captcha",
    "normalize_phone",
    "record_captcha_failure",
    "today_send_count",
    "verify_code",
]
