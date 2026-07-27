"""TradingView Webhook 接收端（鉴权 + IP 白名单与参考仓库保持一致）。

处理流程：IP 白名单 -> 解析请求体(JSON/文本) -> token 鉴权 -> 解析信号
-> 校验 -> 按 model 选择分发引擎 -> 去重 -> 生成 signal_id -> 交给分发引擎。

model 字段（可选，枚举 normal / strategy）决定走哪条分发链路：
- 缺失 / 空 / normal：按币种分发（Dispatcher，项目原有默认行为）；
- strategy：按分组分发（GroupDispatcher，规则与 normal 链路完全隔离）。
"""
import json
import logging
import secrets
import time

from fastapi import APIRouter, Depends, HTTPException, Request

from . import group_rules, persist
from .deps import client_ip, get_dispatcher, get_group_dispatcher, get_store
from .dispatcher import Dispatcher
from .group_dispatcher import GroupDispatcher
from .models import SIGNAL_MODELS, SIGNAL_MODEL_STRATEGY
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


def _resolve_model(data) -> str:
    """解析 model 字段（仅结构化 JSON 携带；纯文本告警一律按 normal 处理）。

    字段名大小写不敏感；缺失或为空按 normal 处理；非枚举取值抛 400。
    """
    raw = None
    if isinstance(data, dict):
        for key, value in data.items():
            if str(key).lower() == "model":
                raw = value
                break
    model = group_rules.normalize_signal_model(raw)
    if model is None:
        raise HTTPException(
            status_code=400,
            detail=f"invalid model: {raw}（应为 {' / '.join(SIGNAL_MODELS)}）",
        )
    return model


async def process_signal(
    data,
    *,
    source_ip: str | None,
    source: str,
    store: RedisStore,
    dispatcher: Dispatcher,
    group_dispatcher: GroupDispatcher | None = None,
    raw_payload: str | None = None,
) -> dict:
    """信号处理共享流程：解析 -> 校验 -> 选引擎 -> 去重 -> 分发 -> 响应。

    供 `/webhook`（source=tradingview）与中控台手动触发（source=manual）复用，
    保证两条入口的解析规则、幂等去重与分发决策完全一致。解析/校验失败抛 HTTPException。
    """
    if raw_payload is None:
        raw_payload = _serialize_raw(data, data if isinstance(data, str) else "")

    model = _resolve_model(data)

    # 解析 + 校验
    signal = parser.parse(data)
    if signal is None:
        await persist.record_signal(
            _new_signal_id(), None, source_ip, parsed_ok=False, status="rejected",
            raw_payload=raw_payload, source=source, model=model,
        )
        raise HTTPException(status_code=400, detail="cannot parse signal")

    ok, err = parser.validate_signal(signal)
    if not ok:
        raise HTTPException(status_code=400, detail=f"invalid signal: {err}")

    # 9.7 幂等：在 DEDUP_WINDOW 秒内，相同(处理模型/动作/品种/手数/止盈止损)的信号视为重复；
    # 指纹带上 model，避免同一笔行情的 normal 与 strategy 信号互相误判为重复。
    fp = (
        f"{model}:{signal.action}:{signal.symbol}:{signal.volume}"
        f":{signal.stop_loss}:{signal.take_profit}"
    )
    if await store.seen_signal(fp):
        logger.info("duplicate signal suppressed: %s", fp)
        return {
            "status": "duplicate", "model": model,
            "action": signal.action, "symbol": signal.symbol,
        }

    # 正式分发：按 model 选择分发引擎（两条链路的规则互不影响）
    signal_id = _new_signal_id()
    if model == SIGNAL_MODEL_STRATEGY:
        if group_dispatcher is None:
            raise HTTPException(status_code=503, detail="service not ready")
        engine = group_dispatcher
    else:
        engine = dispatcher
    result = await engine.dispatch(
        signal, signal_id, source_ip=source_ip, raw_payload=raw_payload, source=source,
    )
    if result.get("mode") == "rejected":
        return {
            "status": "rejected",
            "signal_id": signal_id,
            "model": model,
            "action": signal.action,
            "symbol": signal.symbol,
            "volume": signal.volume,
            "reason": result.get("reason"),
            **result,
        }
    return {
        "status": "accepted",
        "signal_id": signal_id,
        "model": model,
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
    group_dispatcher: GroupDispatcher = Depends(get_group_dispatcher),
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
        store=store, dispatcher=dispatcher, group_dispatcher=group_dispatcher,
        raw_payload=raw_payload,
    )
