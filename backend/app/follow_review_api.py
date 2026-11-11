"""带单频道的人工审核和成员控制接口。"""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from . import follow_channel_service as channel, follow_requests, persist
from .db import SessionLocal
from .deps import client_ip, get_store, require_menu
from .permissions import MENU_FOLLOW, Principal

router = APIRouter(prefix="/api/follow-channel", tags=["follow-review"])
follower = require_menu(MENU_FOLLOW)


class RequestIds(BaseModel):
    request_ids: list[int] = Field(min_length=1)


class MemberState(RequestIds):
    paused: bool


class ReviewBody(BaseModel):
    reason: str = Field(default="", max_length=255)


class UiEvent(BaseModel):
    action: Literal["page_view", "search", "filter", "open_picker", "select_nodes", "cancel_dialog", "collapse_group", "refresh", "page", "open_mine"]
    node_ids: list[str] = Field(default_factory=list, max_length=50)
    value: str | None = Field(default=None, max_length=200)


@router.get("/profile")
async def profile(store=Depends(get_store), p: Principal = Depends(follower)):
    return await channel.profile(store, p)


@router.get("/requests")
async def requests(status: Literal["pending", "reviewed"] = "pending", q: str = Query("", max_length=200),
                   page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100), p: Principal = Depends(follower)):
    return await channel.requests(p, status, q, page, page_size)


async def failed(p, action, target, exc, request):
    await persist.audit(p.username, action, str(target), {"detail": exc.detail}, "failed", client_ip(request), category="follow")


async def review(store, p, request, request_id, approve, reason=""):
    try:
        return await follow_requests.review(store, p, request_id, approve=approve, reason=reason, ip=client_ip(request))
    except HTTPException as exc:
        await failed(p, "follow_request_approve" if approve else "follow_request_reject", request_id, exc, request)
        raise


@router.post("/requests/approve-batch")
async def approve_batch(body: RequestIds, request: Request, store=Depends(get_store), p: Principal = Depends(follower)):
    channel.require_leader(p)
    approved, failed_items = [], []
    for request_id in dict.fromkeys(body.request_ids):
        try:
            approved.append(await review(store, p, request, request_id, True))
        except HTTPException as exc:
            failed_items.append({"request_id": request_id, "detail": exc.detail})
    return {"approved": approved, "failed": failed_items}


@router.post("/requests/{request_id}/approve")
async def approve(request_id: int, request: Request, store=Depends(get_store), p: Principal = Depends(follower)):
    return await review(store, p, request, request_id, True)


@router.post("/requests/{request_id}/reject")
async def reject(request_id: int, body: ReviewBody, request: Request, store=Depends(get_store), p: Principal = Depends(follower)):
    return await review(store, p, request, request_id, False, body.reason)


@router.post("/members/state")
async def member_state(body: MemberState, request: Request, store=Depends(get_store), p: Principal = Depends(follower)):
    try:
        return await channel.members(store, p, body.request_ids, paused=body.paused, ip=client_ip(request))
    except HTTPException as exc:
        await failed(p, "follow_channel_state", body.request_ids, exc, request)
        raise


@router.post("/members/remove")
async def remove(body: RequestIds, request: Request, store=Depends(get_store), p: Principal = Depends(follower)):
    try:
        return await channel.members(store, p, body.request_ids, remove=True, ip=client_ip(request))
    except HTTPException as exc:
        await failed(p, "follow_channel_remove", body.request_ids, exc, request)
        raise


@router.post("/ui-events")
async def ui_event(body: UiEvent, request: Request, p: Principal = Depends(follower)):
    async with SessionLocal() as session:
        follow_requests.audit(session, p, f"follow_ui_{body.action}", params=body.model_dump(), ip=client_ip(request))
        await session.commit()
    return {"status": "ok"}
