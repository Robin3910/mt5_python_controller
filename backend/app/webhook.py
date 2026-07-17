"""TradingView Webhook 接收端（鉴权 + IP 白名单与参考仓库保持一致）。

处理流程：IP 白名单 -> 解析请求体(JSON/文本) -> token 鉴权 -> 解析信号
-> 校验 -> 去重 -> 生成 signal_id -> 交给分发引擎。
"""
import json
import logging
import secrets
import time

from fastapi import APIRouter, Depends, HTTPException, Request

from . import persist
from .deps import client_ip, get_dispatcher, get_store
from .dispatcher import Dispatcher
from .parser import TradingViewParser
from .redis_store import RedisStore
from .settings import settings

logger = logging.getLogger(__name__)
router = APIRouter(tags=["webhook"])
parser = TradingViewParser()


def _new_signal_id() -> str:
    """生成全局唯一信号 ID：时间戳(毫秒,十六进制) + 随机串。"""
    return "sig_" + format(int(time.time() * 1000), "x") + secrets.token_hex(3)


def _serialize_raw(data, text: str) -> str:
    """保留 Webhook 原始请求体（JSON 或纯文本）。"""
    if isinstance(data, dict):
        return json.dumps(data, ensure_ascii=False)
    if text:
        return text
    return str(data) if data is not None else ""


def _extract_token(request: Request, data) -> str:
    """从多处提取鉴权 token：请求头 / 查询参数 / JSON 字段 / Bearer。"""
    token = request.headers.get("x-auth-token") or request.query_params.get("token")
    if not token and isinstance(data, dict):
        token = data.get("token") or data.get("auth_token")
    auth = request.headers.get("authorization")
    if not token and auth and auth.lower().startswith("bearer "):
        token = auth.split(" ", 1)[1]
    return token or ""


async def process_signal(
    data,
    *,
    source_ip: str | None,
    source: str,
    store: RedisStore,
    dispatcher: Dispatcher,
    raw_payload: str | None = None,
) -> dict:
    """信号处理共享流程：解析 -> 校验 -> 去重 -> 分发 -> 响应。

    供 `/webhook`（source=tradingview）与中控台手动触发（source=manual）复用，
    保证两条入口的解析规则、幂等去重与分发决策完全一致。解析/校验失败抛 HTTPException。
    """
    if raw_payload is None:
        raw_payload = _serialize_raw(data, data if isinstance(data, str) else "")

    # 解析 + 校验
    signal = parser.parse(data)
    if signal is None:
        await persist.record_signal(
            _new_signal_id(), None, source_ip, parsed_ok=False, status="rejected",
            raw_payload=raw_payload, source=source,
        )
        raise HTTPException(status_code=400, detail="cannot parse signal")

    ok, err = parser.validate_signal(signal)
    if not ok:
        raise HTTPException(status_code=400, detail=f"invalid signal: {err}")

    # 9.7 幂等：在 DEDUP_WINDOW 秒内，相同(动作/品种/手数/止盈止损)的信号视为重复
    fp = f"{signal.action}:{signal.symbol}:{signal.volume}:{signal.stop_loss}:{signal.take_profit}"
    if await store.seen_signal(fp):
        logger.info("duplicate signal suppressed: %s", fp)
        return {"status": "duplicate", "action": signal.action, "symbol": signal.symbol}

    # 正式分发
    signal_id = _new_signal_id()
    result = await dispatcher.dispatch(
        signal, signal_id, source_ip=source_ip, raw_payload=raw_payload, source=source,
    )
    if result.get("mode") == "rejected":
        return {
            "status": "rejected",
            "signal_id": signal_id,
            "action": signal.action,
            "symbol": signal.symbol,
            "volume": signal.volume,
            "reason": result.get("reason"),
            **result,
        }
    return {
        "status": "accepted",
        "signal_id": signal_id,
        "action": signal.action,
        "symbol": signal.symbol,
        "volume": signal.volume,
        **result,
    }


@router.post("/webhook")
async def webhook(
    request: Request,
    store: RedisStore = Depends(get_store),
    dispatcher: Dispatcher = Depends(get_dispatcher),
):
    ip = client_ip(request)

    # IP 白名单（与参考仓库行为一致；经 nginx 时取 X-Forwarded-For）
    if settings.enable_ip_whitelist and ip not in settings.whitelist:
        logger.warning("webhook rejected (ip not allowed): %s", ip)
        raise HTTPException(status_code=403, detail="ip not allowed")

    # 请求体可能是 JSON，也可能是 TradingView 的纯文本告警
    raw = await request.body()
    text = raw.decode("utf-8", errors="ignore").strip()
    try:
        data = json.loads(text) if text else {}
    except json.JSONDecodeError:
        data = text  # 纯文本

    # token 鉴权（仅在 ENABLE_AUTH 开启时校验）
    if settings.enable_auth:
        token = _extract_token(request, data)
        if token != settings.auth_token:
            logger.warning("webhook rejected (bad token) from %s", ip)
            raise HTTPException(status_code=401, detail="invalid token")

    raw_payload = _serialize_raw(data, text)
    return await process_signal(
        data, source_ip=ip, source="tradingview",
        store=store, dispatcher=dispatcher, raw_payload=raw_payload,
    )
