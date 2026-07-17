"""中控台手动触发信号 API（需管理员鉴权）。

复用 /webhook 的解析与分发流程（webhook.process_signal），以管理员 JWT 鉴权，
避免把 Webhook token / IP 白名单暴露到浏览器；来源标记为 manual，便于事件页区分。
"""
from fastapi import APIRouter, Depends, HTTPException, Request

from . import persist
from .deps import client_ip, get_current_admin, get_dispatcher, get_store
from .dispatcher import Dispatcher
from .models import ManualSignalRequest
from .redis_store import RedisStore
from .webhook import process_signal

router = APIRouter(prefix="/api/console", tags=["console"])


@router.post("/manual-signal")
async def manual_signal(
    body: ManualSignalRequest,
    request: Request,
    store: RedisStore = Depends(get_store),
    dispatcher: Dispatcher = Depends(get_dispatcher),
    admin: str = Depends(get_current_admin),
):
    """手动触发一条开仓信号（BUY / SELL），走与 Webhook 完全一致的分发流程。"""
    action = (body.action or "").strip().upper()
    if action not in ("BUY", "SELL"):
        raise HTTPException(status_code=400, detail="action must be BUY or SELL")
    data = {"action": action, "symbol": body.symbol, "volume": body.volume}
    ip = client_ip(request)
    result = await process_signal(
        data, source_ip=ip, source="manual", store=store, dispatcher=dispatcher,
    )
    await persist.audit(
        admin, "manual_signal", body.symbol, data, result.get("status", "ok"), ip,
        category="console", before=None, after={
            "request": data,
            "result": {
                "status": result.get("status"),
                "signal_id": result.get("signal_id"),
                "mode": result.get("mode"),
                "targets": result.get("targets"),
                "reason": result.get("reason"),
            },
        },
    )
    return result
