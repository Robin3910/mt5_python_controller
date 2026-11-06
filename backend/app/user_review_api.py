"""注册用户 KYC 实名审核 API（仅超管）。

聚合「用户 + 审核单 + 名下节点」给前端审核页；提供通过 / 驳回 / 批量通过。

**与 `users.is_active` 的分工**（见 user_review_service 模块文档）：
审核状态在 `user_reviews.status`，`is_active` 只是登录闸门。审核通过会同时
把 `is_active` 置 True；驳回不动它（待审核态本来就登录不了）。

设计文档：`docs/superpowers/specs/2026-11-04-user-registration-review-design.md`
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from . import permissions, persist, rbac_service, user_review_service, user_service
from .connections import manager
from .deps import client_ip, get_store, require_admin
from .models import (
    UserReviewApproveRequest,
    UserReviewApproveResult,
    UserReviewBulkApproveRequest,
    UserReviewBulkApproveResult,
    UserReviewList,
    UserReviewRejectRequest,
    UserReviewStats,
)
from .permissions import Principal
from .redis_store import RedisStore
from .sms_service import mask_phone

router = APIRouter(prefix="/api/user-reviews", tags=["user-reviews"])


# ---------------------------------------------------------------------------
# 聚合
# ---------------------------------------------------------------------------


async def _collect(store: RedisStore) -> tuple[list, dict]:
    """把「用户 + 审核单 + 名下节点」聚合成 (items, stats)。

    返回的 items 每个人**只取最新一张审核单** —— 一个人被驳回后重提会有多张
    单，但列表页只关心当前状态（历史在详情里查）。

    节点归属按 owner_user_id 分组；在线态取内存连接表；余额取 Redis 账户快照
    （无快照 = 0，与原型「离线节点余额 0」一致）。
    """
    users = {u.id: u for u in await user_service.list_users()}
    reviews = await user_review_service.list_reviews()

    # 每个用户取最新一张单（按 id 倒序第一个）
    latest: dict[int, object] = {}
    for r in reviews:
        if r.user_id not in latest or r.id > latest[r.user_id].id:
            latest[r.user_id] = r

    # 节点按所有者分组
    nodes_by_owner: dict[Optional[int], list[dict]] = {}
    all_nodes = await store.all_nodes()
    for n in all_nodes:
        owner = permissions.normalize_owner(n.get("owner_user_id"))
        nodes_by_owner.setdefault(owner, []).append(n)

    stats = UserReviewStats(
        total=len(reviews),
        pending=sum(1 for r in reviews if r.status == user_review_service.STATUS_PENDING),
        approved=sum(1 for r in reviews if r.status == user_review_service.STATUS_APPROVED),
        rejected=sum(1 for r in reviews if r.status == user_review_service.STATUS_REJECTED),
        node_total=len(all_nodes),
        node_online=sum(1 for n in all_nodes if manager.is_node_online(n.get("node_id", ""))),
    )

    items = []
    for user_id, review in latest.items():
        user = users.get(user_id)
        if user is None:
            # 用户被删了但审核单还在：跳过，不让前端拿到孤儿记录
            continue
        node_items = []
        for n in nodes_by_owner.get(user_id, []):
            node_id = n.get("node_id", "")
            acct = await store.get_account(node_id) or {}
            node_items.append({
                "node_id": node_id,
                "name": n.get("name") or node_id,
                "status": "online" if manager.is_node_online(node_id) else "offline",
                "balance": float(acct.get("balance") or 0.0),
            })
        node_items.sort(key=lambda x: x["node_id"])

        items.append({
            "id": review.id,
            "user_id": user.id,
            "username": user.username,
            "display_name": user.display_name,
            "account": f"U-{user.id}",
            # 手机号取**审核单快照**（提交那一刻的号码），没有快照时回退
            # 到用户当前绑定的号码 —— 两者都是脱敏值。
            "phone": review.phone or (mask_phone(user.phone) if user.phone else None),
            "email": review.email or getattr(user, "email", None),
            "real_name": review.real_name,
            "kyc_submitted": bool(review.kyc_submitted),
            "status": review.status,
            "register_time": _ts(user.created_at),
            "submit_time": _ts(review.submit_time),
            "audit_time": _ts(review.audited_at),
            "audited_by": review.audited_by,
            "reject_reason": review.reject_reason,
            "nodes": node_items,
        })
    items.sort(key=lambda x: x["id"], reverse=True)
    return items, stats


def _ts(value) -> Optional[float]:
    """datetime → 时间戳秒；None / 非 datetime 返回 None。"""
    if value is None:
        return None
    try:
        return value.timestamp()
    except (AttributeError, OSError, OverflowError):
        return None


def _match_keyword(item: dict, keyword: str) -> bool:
    """关键词匹配：4 个维度 —— 用户名 / 账号 / 手机号 / 名下节点 ID。

    大小写不敏感。手机号同时试**脱敏串**与**原串**（管理员可能照着列表里的
    `138****8000` 搜，也可能记得完整号码）。
    """
    term = keyword.strip().lower()
    if not term:
        return True
    haystacks = [
        item.get("username") or "",
        item.get("display_name") or "",
        item.get("account") or "",
        item.get("phone") or "",
        item.get("real_name") or "",
    ]
    for n in item.get("nodes") or []:
        haystacks.append(n.get("node_id") or "")
        haystacks.append(n.get("name") or "")
    return any(term in h.lower() for h in haystacks)


# ---------------------------------------------------------------------------
# 列表
# ---------------------------------------------------------------------------


@router.get("", response_model=UserReviewList)
async def list_user_reviews(
    status: str | None = None,
    keyword: str | None = None,
    store: RedisStore = Depends(get_store),
    _: Principal = Depends(require_admin),
):
    """审核列表：聚合用户 / 审核单 / 名下节点。

    `status` ∈ {pending, approved, rejected, all}；非法值 400。
    `keyword` 对用户名 / 账号 / 手机号 / 节点 ID 做大小写不敏感的子串匹配。

    **stats 是全局数**（不受 status / keyword 影响）—— Tab 上的计数与顶部
    统计块始终反映全局，否则用户会以为「还有 N 个待审但列表里没有」。
    """
    if status and status not in ("all", *user_review_service.REVIEW_STATUSES):
        raise HTTPException(status_code=400, detail=f"未知的审核状态：{status}")
    items, stats = await _collect(store)
    filtered = [
        it for it in items
        if (not status or status == "all" or it["status"] == status)
        and _match_keyword(it, keyword or "")
    ]
    return UserReviewList(stats=stats, items=filtered)


# ---------------------------------------------------------------------------
# 通过 / 驳回
# ---------------------------------------------------------------------------


@router.post("/bulk-approve", response_model=UserReviewBulkApproveResult)
async def bulk_approve_reviews(
    body: UserReviewBulkApproveRequest,
    request: Request,
    store: RedisStore = Depends(get_store),
    p: Principal = Depends(require_admin),
):
    """批量通过；`review_ids` 为空 = 全部 pending。

    单个失败**不中断**其余，返回 `{ok, approved, failed[]}`。

    批量场景不分配节点 —— 逐单选节点无法在批量操作里表达。
    """
    if body.review_ids:
        targets = list(body.review_ids)
    else:
        targets = [
            r.id for r in await user_review_service.list_reviews(
                user_review_service.STATUS_PENDING
            )
        ]
    approved = 0
    failed: list[dict] = []
    usernames: list[str] = []
    for review_id in targets:
        try:
            ok = await _approve_one(review_id, node_ids=[], confirm=False,
                                    p=p, ip=client_ip(request), store=store,
                                    audit=False)
        except HTTPException as e:
            failed.append({"review_id": review_id, "reason": _detail_text(e)})
            continue
        if ok is None:
            failed.append({"review_id": review_id, "reason": "审核单不存在"})
            continue
        approved += 1
        usernames.append(ok["username"])
    await persist.audit(
        p.username, "user_review_bulk_approve", None,
        {"review_ids": targets}, "ok" if not failed else "partial",
        client_ip(request), category="auth",
        before=None, after={"approved": approved, "failed": failed, "usernames": usernames},
    )
    return UserReviewBulkApproveResult(ok=True, approved=approved, failed=failed)


def _detail_text(e: HTTPException) -> str:
    """把 HTTPException.detail 压成一句可读文本（detail 可能是 str 或 dict）。"""
    detail = e.detail
    if isinstance(detail, str):
        return detail
    if isinstance(detail, dict):
        return str(detail.get("message") or detail.get("reason") or "操作失败")
    return "操作失败"


@router.post("/{review_id}/approve", response_model=UserReviewApproveResult)
async def approve_review(
    review_id: int,
    body: UserReviewApproveRequest,
    request: Request,
    store: RedisStore = Depends(get_store),
    p: Principal = Depends(require_admin),
):
    """审核通过。

    **操作顺序很关键**：先分配节点 → 成功后再改审核单 → 最后置 `is_active=True`。
    反过来会留下「审核通过但用户登录不了」或「用户能登录但节点没分配」的
    中间态。分配节点失败（409）时，审核单状态**仍是 pending**、`is_active`
    **仍是 False** —— 整个操作干净地没发生。

    `node_ids` 为空 = 不改节点归属（沿用现状）。
    """
    result = await _approve_one(
        review_id, node_ids=body.node_ids, confirm=body.confirm,
        p=p, ip=client_ip(request), store=store,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="审核单不存在")
    return UserReviewApproveResult(
        ok=True,
        user_id=result["user_id"],
        is_active=result["is_active"],
        assigned_node_ids=result["assigned_node_ids"],
    )


async def _approve_one(
    review_id: int,
    *,
    node_ids: list[str],
    confirm: bool,
    p: Principal,
    ip: str | None,
    store: RedisStore,
    audit: bool = True,
) -> Optional[dict]:
    """通过一张审核单的核心逻辑（单个 / 批量共用）。

    返回 None 表示审核单不存在；HTTPException 表示前置校验失败（此时**状态
    未发生任何改变**）。
    """
    review = await user_review_service.get_review(review_id)
    if review is None:
        return None
    if review.status != user_review_service.STATUS_PENDING:
        raise HTTPException(status_code=409, detail="该审核单已处理")
    user = await _user_or_404(review.user_id)

    # ① 先分配节点（可能抛 409）。失败 → 下面两步都不执行，状态保持干净。
    assigned: list[str] = []
    if node_ids:
        assign_result = await _assign_nodes(
            review.user_id, node_ids, confirm=confirm, p=p, ip=ip, store=store
        )
        assigned = list(assign_result.get("added") or [])

    # ② 审核单改状态
    await user_review_service.approve_review(review_id, auditor=p.username)

    # ③ 开登录闸门
    await user_service.update_user(user.id, is_active=True)

    # ④ 让该用户的后台连接重新拉权限
    await rbac_service.refresh_sessions(store, [user.username])

    if audit:
        await persist.audit(
            p.username, "user_review_approve", user.username,
            {"review_id": review_id}, "ok", ip, category="auth",
            before={"review_id": review_id, "status": user_review_service.STATUS_PENDING},
            after={
                "review_id": review_id,
                "status": user_review_service.STATUS_APPROVED,
                "assigned_node_ids": assigned,
            },
        )
    return {
        "user_id": user.id,
        "username": user.username,
        "is_active": True,
        "assigned_node_ids": assigned,
    }


async def _assign_nodes(
    user_id: int,
    node_ids: list[str],
    *,
    confirm: bool,
    p: Principal,
    ip: str | None,
    store: RedisStore,
) -> dict:
    """分配节点（复用 users_api.assign_nodes_core，保证校验口径一致）。

    延迟 import：users_api 依赖本模块的反向引用会成环。
    """
    from . import users_api

    return await users_api.assign_nodes_core(
        user_id, node_ids, confirm=confirm, p=p, ip=ip, store=store
    )


@router.post("/{review_id}/reject")
async def reject_review(
    review_id: int,
    body: UserReviewRejectRequest,
    request: Request,
    store: RedisStore = Depends(get_store),
    p: Principal = Depends(require_admin),
):
    """审核驳回：记原因、记审核人。

    **不动 `users.is_active`** —— 待审核态本来就不能登录，驳回后依然不能。
    用户可在注册页重新提交（详见 phone_auth.register_by_phone）。
    """
    review = await user_review_service.get_review(review_id)
    if review is None:
        raise HTTPException(status_code=404, detail="审核单不存在")
    if review.status != user_review_service.STATUS_PENDING:
        raise HTTPException(status_code=409, detail="该审核单已处理")
    reason = (body.reason or "").strip()
    if not reason:
        raise HTTPException(status_code=400, detail="请填写驳回原因")
    user = await _user_or_404(review.user_id)
    await user_review_service.reject_review(review_id, auditor=p.username, reason=reason)
    await persist.audit(
        p.username, "user_review_reject", user.username,
        {"review_id": review_id}, "ok", client_ip(request), category="auth",
        before={"review_id": review_id, "status": user_review_service.STATUS_PENDING},
        after={"review_id": review_id, "status": user_review_service.STATUS_REJECTED,
               "reason": reason},
    )
    return {"ok": True, "user_id": user.id, "is_active": False}


async def _user_or_404(user_id: int):
    """取用户；不存在 → 404。"""
    users = {u.id: u for u in await user_service.list_users()}
    user = users.get(int(user_id))
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    return user
