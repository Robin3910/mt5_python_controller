"""注册用户的 KYC 实名审核单：持久化 + 实名材料校验。

设计要点见 `docs/superpowers/specs/2026-11-04-user-registration-review-design.md`：

- **审核状态与登录闸门分离**：本表的 `status` 表达业务状态（pending / approved /
  rejected），`users.is_active` 表达「能不能登录」。`is_active` 只有启用 / 禁用
  两态，表达不了「已驳回」，也没有地方存审核时间、审核人、驳回原因。
- **实名材料只有三项**：姓名 / 邮箱 / 手机号（2026-11-05 简化，见下文）。
- **一人可有多张单**：被驳回后重新提交会新建一张，旧单保留成历史。

## 为什么不再收集证件号（2026-11-05）

原方案收集「证件类型 + 证件号」，明文不入库、只存掩码 + sha256 哈希做去重。
现已**完全去掉**：审核材料只看姓名 / 邮箱 / 手机号。

去掉之后：
- 不再有证件类型白名单与按类型的正则格式校验；
- 不再有掩码函数与哈希函数；
- 不再有「同一证件号不能重复注册」的去重检查（也不再有对应的 409）；
- `user_reviews` 表少了 `cert_type` / `cert_no_masked` / `cert_no_hash` 三列，
  多了一列 `phone`（提交时的手机号快照，脱敏后存）。

手机号本来就有唯一索引（`users.phone`），「一个号码一个账号」这条不变。
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from sqlalchemy import select

from .db import SessionLocal
from .orm import User, UserReview
from .sms_service import mask_phone

# 审核单状态
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
REVIEW_STATUSES: tuple[str, ...] = (STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED)

# 姓名的最小长度（1 个汉字 / 1 个字母都算合法，但空串不算）
_REAL_NAME_MIN = 1
_REAL_NAME_MAX = 64

# 邮箱的粗校验（只挡明显不合理的；真正的验证是「收到确认邮件」）
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class KycInvalid(ValueError):
    """实名材料不合法（姓名为空 / 邮箱格式错）。"""


# ---------------------------------------------------------------------------
# 实名材料校验
# ---------------------------------------------------------------------------


def validate_kyc(
    real_name: str | None,
    email: str | None,
) -> Optional[dict]:
    """校验并归一化一份实名材料（姓名 / 邮箱）。

    **姓名要么填要么不填**：不填返回 None（用户未提交实名材料，管理员可
    在审核时手工补录）；填了就必须有名字且长度合法。

    邮箱**独立**：可以填姓名不填邮箱，也可以填邮箱不填姓名 —— 邮箱只是
    联系渠道，与姓名不是一组必填项。

    返回 `{"real_name", "email"}`；未提交姓名时返回 None。
    """
    name = (real_name or "").strip()
    mail = (email or "").strip()

    # 邮箱**独立于姓名**校验：姓名没填但邮箱填了，邮箱格式照样要挡。
    # （顺序很重要 —— 先校验邮箱，再决定要不要因为「没填姓名」返回 None，
    #   否则「没填姓名 + 邮箱非法」会被 early return 放过去，脏数据进库。）
    if mail and not _EMAIL_RE.match(mail):
        raise KycInvalid("邮箱格式不正确")

    if not name:
        # 没填姓名 → 不算提交了实名材料（邮箱可以单独填）
        return None
    if not (_REAL_NAME_MIN <= len(name) <= _REAL_NAME_MAX):
        raise KycInvalid("姓名长度不正确")
    return {"real_name": name, "email": mail or None}


def mask_phone_snapshot(phone: str | None) -> str | None:
    """审核单里存的手机号快照（脱敏）：13800138000 -> 138****8000。

    直接复用 `sms_service.mask_phone`（同一口径：去国家码前缀，前 3 后 4），
    不另写一份 —— 两处脱敏规则若不一致，同一个号码在审核页与审计里会
    长得不一样。
    """
    if not phone:
        return None
    return mask_phone(phone)


# ---------------------------------------------------------------------------
# 持久化
# ---------------------------------------------------------------------------


async def create_review(
    user_id: int,
    *,
    real_name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    kyc_submitted: bool = True,
    status: str = STATUS_PENDING,
) -> UserReview:
    """新建一张审核单。

    `phone` 传**脱敏后**的串（`138****8000`）—— 展示用，明文号码在
    `users.phone` 里（那里有唯一索引，是登录凭据）。

    `kyc_submitted` = 用户是否自填了姓名。管理员手工补录时传 False。
    """
    async with SessionLocal() as s:
        review = UserReview(
            user_id=int(user_id),
            status=status,
            real_name=(real_name or "").strip() or None,
            email=(email or "").strip() or None,
            phone=(phone or "").strip() or None,
            kyc_submitted=bool(kyc_submitted),
            submit_time=datetime.now(),
        )
        s.add(review)
        await s.commit()
        await s.refresh(review)
        return review


async def get_review(review_id: int) -> Optional[UserReview]:
    async with SessionLocal() as s:
        return await s.get(UserReview, int(review_id))


async def latest_review(user_id: int) -> Optional[UserReview]:
    """该用户最新的一张审核单。

    按 id 倒序取第一行 —— 不用时间排序：同一秒内提交的两张单时间戳可能
    相同，而 id 严格递增。
    """
    async with SessionLocal() as s:
        stmt = (
            select(UserReview)
            .where(UserReview.user_id == int(user_id))
            .order_by(UserReview.id.desc())
            .limit(1)
        )
        return (await s.execute(stmt)).scalars().first()


async def list_reviews(status: str | None = None) -> list[UserReview]:
    """全部审核单（按 id 升序）。`status` 为 None 时不过滤。"""
    async with SessionLocal() as s:
        stmt = select(UserReview).order_by(UserReview.id.asc())
        if status:
            stmt = stmt.where(UserReview.status == status)
        return list((await s.execute(stmt)).scalars().all())


async def approve_review(review_id: int, *, auditor: str) -> Optional[UserReview]:
    """审核通过：status='approved'，记录审核人与时间，清掉驳回原因。

    **不改 `users.is_active`** —— 那是 API 层的编排（要连带会话吊销 + 审计）。
    """
    async with SessionLocal() as s:
        review = await s.get(UserReview, int(review_id))
        if review is None:
            return None
        review.status = STATUS_APPROVED
        review.audited_at = datetime.now()
        review.audited_by = auditor
        review.reject_reason = None
        await s.commit()
        await s.refresh(review)
        return review


async def reject_review(
    review_id: int, *, auditor: str, reason: str
) -> Optional[UserReview]:
    """审核驳回：status='rejected'，记录原因与审核人。

    **不改 `users.is_active`** —— 驳回时用户本就不可登录，保持 False 即可。
    """
    async with SessionLocal() as s:
        review = await s.get(UserReview, int(review_id))
        if review is None:
            return None
        review.status = STATUS_REJECTED
        review.audited_at = datetime.now()
        review.audited_by = auditor
        review.reject_reason = (reason or "").strip() or None
        await s.commit()
        await s.refresh(review)
        return review


async def user_display_name(user_id: int) -> Optional[str]:
    """用户的展示名（display_name 优先，回退 username）。"""
    async with SessionLocal() as s:
        user = await s.get(User, int(user_id))
        if user is None:
            return None
        return user.display_name or user.username
