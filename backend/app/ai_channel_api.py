"""AI 机器人频道：本人频道的审核、暂停及真实节点删除。"""
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from . import ai_application_store as applications, ai_channel_service as channel, node_deletion
from .ai_channel_models import ApproveBatchRequest, ChannelDeleteRequest, ChannelDeleteScope, MemberStateRequest, ReviewRequest
from .deps import client_ip, get_store, require_menu
from .permissions import MENU_AI_TRADE, Principal

router = APIRouter(prefix="/api/ai-channel", tags=["ai-channel"])
host = require_menu(MENU_AI_TRADE)


@router.get("/profile")
async def profile(store=Depends(get_store), p: Principal = Depends(host)):
    return await channel.profile(store, p)


@router.get("/requests")
async def requests(status: str = Query("pending", pattern="^(pending|reviewed)$"), q: str = Query("", max_length=200),
                   page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100), p: Principal = Depends(host)):
    return await channel.requests(p, status, q, page, page_size)


async def _review(store, p, request, request_id, approve, reason=""):
    try:
        return await applications.review(store, p, request_id, approve=approve, reason=reason, ip=client_ip(request))
    except HTTPException as exc:
        await node_deletion.failed(p, "ai_follow_request_approve" if approve else "ai_follow_request_reject", request_id, exc, client_ip(request))
        raise


@router.post("/requests/approve-batch")
async def approve_batch(body: ApproveBatchRequest, request: Request, store=Depends(get_store), p: Principal = Depends(host)):
    channel.require_host(p)
    approved, failed = [], []
    for request_id in dict.fromkeys(body.request_ids):
        try:
            approved.append(await _review(store, p, request, request_id, True))
        except HTTPException as exc:
            failed.append({"request_id": request_id, "detail": exc.detail})
    return {"approved": approved, "failed": failed}


@router.post("/requests/{request_id}/approve")
async def approve(request_id: int, request: Request, store=Depends(get_store), p: Principal = Depends(host)):
    return await _review(store, p, request, request_id, True)


@router.post("/requests/{request_id}/reject")
async def reject(request_id: int, body: ReviewRequest, request: Request, store=Depends(get_store), p: Principal = Depends(host)):
    return await _review(store, p, request, request_id, False, body.reason)


@router.post("/members/state")
async def member_state(body: MemberStateRequest, request: Request, store=Depends(get_store), p: Principal = Depends(host)):
    try:
        return await channel.set_paused(store, p, body.group_id, body.follower_user_id, body.paused, client_ip(request))
    except HTTPException as exc:
        await node_deletion.failed(p, "ai_channel_state", body.group_id, exc, client_ip(request))
        raise


@router.post("/members/deletion-preview")
async def deletion_preview(body: ChannelDeleteScope, request: Request, store=Depends(get_store), p: Principal = Depends(host)):
    try:
        return await node_deletion.preview(store, p, record=True, ip=client_ip(request), **body.model_dump())
    except HTTPException as exc:
        await node_deletion.failed(p, "preview_ai_channel_deletion", body.group_id, exc, client_ip(request))
        raise


@router.post("/members/delete")
async def delete_members(body: ChannelDeleteRequest, request: Request, store=Depends(get_store), p: Principal = Depends(host)):
    try:
        return await node_deletion.execute(store, p, body.version, body.node_ids, ip=client_ip(request),
            group_id=body.group_id, follower_user_id=body.follower_user_id)
    except HTTPException as exc:
        await node_deletion.failed(p, "delete_ai_channel_nodes", body.group_id, exc, client_ip(request))
        raise
