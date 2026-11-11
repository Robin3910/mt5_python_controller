"""个人中心的「财经日历」API：黑名单设置 + 事件列表 + 当前状态。

三个个人接口（登录即可，不挂菜单码 —— 与个人中心其它区块一致）：

- `GET  /api/me/news`            我的设置 + 当前状态 + 未来事件列表（核心接口）
- `PUT  /api/me/news/settings`  保存设置（开关 / 窗口 / 币种 / 手动急停）
- `POST /api/me/news/refresh`   手动强制刷新日历（排障用，限频）

外加一个管理员接口：

- `GET  /api/admin/news/events` 全局事件表 + 抓取状态（排查「为什么没拦住」）

**user_id 一律取自 Principal**，绝不接受请求体里的 user_id —— 否则任何人都能
改别人的黑名单开关。管理员接口只能看全局事件表，**不能替普通用户改开关**
（全局强制黑名单是另一个权限模型，不在本需求范围内）。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from . import economic_calendar, news_guard, persist
from .deps import client_ip, get_principal, require_admin
from .permissions import Principal

router = APIRouter(prefix="/api/me/news", tags=["news"])

# 事件列表往前看多少天（与 economic_calendar.LOOKAHEAD_DAYS 对齐）
LIST_DAYS = 7

# 手动刷新的最小间隔（秒）—— 防止有人狂点，把外网站点打挂
REFRESH_COOLDOWN_SEC = 60

# 进程内记录上一次手动刷新的时刻（限频用；多实例部署时各自计数，够用）
_last_manual_refresh: float = 0.0

TZ_BEIJING = timezone(timedelta(hours=8))


def _fmt_beijing(ts: int) -> str:
    """epoch 秒 → 北京时间字符串（前端展示口径统一）。"""
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone(TZ_BEIJING).strftime(
        "%Y-%m-%d %H:%M"
    )


def _event_block_state(
    event: dict, settings: news_guard.NewsSettings, symbol: str,
) -> tuple[bool, Optional[str]]:
    """这条事件是否落入「我的黑名单」；不落入时说明原因。

    判定口径与运行时判定（find_blocking_event）保持一致：等级达标 + 币种匹配。
    """
    impact = str(event.get("impact") or "")
    if not economic_calendar.impact_at_least(impact, settings.min_impact):
        label = {"high": "高", "medium": "中", "low": "低", "holiday": "休市"}.get(impact, impact)
        return False, f"影响等级 {label}，低于设定阈值"
    country = str(event.get("country") or "")
    if not news_guard.event_matches_currency(country, settings.currencies):
        return False, f"币种 {country} 不在关注范围"
    if settings.symbols and not news_guard.event_matches_symbol(country, symbol, settings):
        return False, "不在该品种的覆盖范围"
    return True, None


@router.get("")
async def get_my_news(
    symbol: str = "",
    p: Principal = Depends(get_principal),
):
    """我的财经日历设置 + 当前黑名单状态 + 未来事件列表。

    `symbol` 可选：传了就能看到「这个品种当前是否被拦」以及事件的品种级判定。
    """
    settings = await news_guard.get_settings(p.user_id)
    now = time.time()
    status = await news_guard.current_status(p.user_id, symbol, now=now)

    # 事件列表：往前 1 天到往后 LIST_DAYS 天，覆盖「正在进行中的黑名单」
    start_ts = int(now - 86400)
    end_ts = int(now + LIST_DAYS * 86400)
    events = await economic_calendar.list_events(start_ts, end_ts)

    out_events = []
    for ev in events:
        in_blackout, why = _event_block_state(ev, settings, symbol)
        out_events.append({
            "event_id": ev["event_id"],
            "event_time": ev["event_time"],
            "event_time_text": _fmt_beijing(ev["event_time"]),
            "title": ev["title"],
            "country": ev["country"],
            "impact": ev["impact"],
            "forecast": ev.get("forecast"),
            "previous": ev.get("previous"),
            "in_blackout": in_blackout,
            "block_reason": why,
        })

    fetched_at = await economic_calendar.latest_fetched_at()
    # 数据陈旧判定：最近一条事件数据超过 2 个刷新周期没更新 = 可能拉不动了
    stale = (fetched_at <= 0) or ((now - fetched_at) > economic_calendar.FETCH_INTERVAL_SEC * 2)

    # 顶部滚动提示条状态：今天有没有黑名单时段、是否已被关闭
    banner = await news_guard.banner_state(p.user_id, symbol, now=now)

    return {
        "settings": settings.to_dict(),
        "status": status,
        "banner": banner,
        "events": out_events,
        "calendar": {
            "fetched_at": fetched_at,
            "fetched_at_text": _fmt_beijing(int(fetched_at)) if fetched_at else None,
            "stale": stale,
            "event_count": len(out_events),
            "timezone": "北京时间 UTC+8",
        },
    }


@router.post("/banner/dismiss")
async def dismiss_my_news_banner(
    body: dict,
    request: Request,
    p: Principal = Depends(get_principal),
):
    """关闭顶部滚动提示条：**当天**不再显示，次日自动重新提示。

    请求体可传 `{"day": "2026-11-11"}`（北京时间日期）；不传就用服务器当前日期。

    只记「用户关了提示条」这一件事，不碰任何开仓设置 —— 关提示条 ≠ 停止做单。
    """
    day = ""
    if isinstance(body, dict):
        day = str(body.get("day") or "").strip()
    if day and not (len(day) == 10 and day[4] == "-" and day[7] == "-"):
        raise HTTPException(status_code=400, detail="日期格式不正确")

    saved_day = await news_guard.dismiss_banner(p.user_id, day or None)

    await persist.audit(
        p.username, "dismiss_news_banner", p.username,
        {"day": saved_day}, "ok", client_ip(request), category="system",
    )
    return {"ok": True, "day": saved_day}


@router.put("/settings")
async def put_my_news_settings(
    body: dict,
    request: Request,
    p: Principal = Depends(get_principal),
):
    """保存我的黑名单设置。

    字段级「传什么改什么」：不传的字段保持原值。整份传则是全量覆盖。
    """
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="请求体格式不正确")

    before = await news_guard.get_settings(p.user_id)
    payload = {
        "enabled": body.get("enabled", before.enabled),
        "before_min": body.get("before_min", before.before_min),
        "after_min": body.get("after_min", before.after_min),
        "min_impact": body.get("min_impact", before.min_impact),
        "currencies": body.get("currencies", list(before.currencies)),
        "symbols": body.get("symbols", list(before.symbols)),
        "manual_paused": body.get("manual_paused", before.manual_paused),
        "manual_pause_note": body.get("manual_pause_note", before.manual_pause_note),
    }
    if payload["manual_paused"] and not before.manual_paused:
        # 记录暂停时刻（用于「已暂停多久」的展示）
        payload["manual_paused_at"] = time.time()

    try:
        saved = await news_guard.save_settings(p.user_id, payload)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail="保存失败，请稍后重试") from exc

    await persist.audit(
        p.username, "update_news_settings", p.username,
        {
            "enabled": saved.enabled,
            "before_min": saved.before_min,
            "after_min": saved.after_min,
            "min_impact": saved.min_impact,
            "currencies": list(saved.currencies),
            "manual_paused": saved.manual_paused,
        },
        "ok", client_ip(request), category="system",
        before=before.to_dict(), after=saved.to_dict(),
    )
    return {"settings": saved.to_dict()}


@router.post("/refresh")
async def refresh_my_news(
    request: Request,
    p: Principal = Depends(get_principal),
):
    """手动强制刷新一次财经日历（排障用，限频 60 秒一次）。"""
    global _last_manual_refresh
    now = time.time()
    if now - _last_manual_refresh < REFRESH_COOLDOWN_SEC:
        raise HTTPException(status_code=429, detail="刷新太频繁，请稍后再试")
    _last_manual_refresh = now

    from .state import state

    result: dict
    worker = getattr(state, "calendar_worker", None)
    if worker is not None:
        result = await worker.refresh_now()
    else:
        result = await economic_calendar.refresh_events()

    await persist.audit(
        p.username, "refresh_news_calendar", p.username,
        {"ok": bool(result.get("ok"))}, "ok" if result.get("ok") else "failed",
        client_ip(request), category="system",
    )
    return {
        "ok": bool(result.get("ok")),
        "count": int(result.get("count") or 0),
        "fetched_at": float(result.get("fetched_at") or time.time()),
        "stale": not bool(result.get("ok")),
        "message": "日历已更新" if result.get("ok") else "抓取失败，保留上次数据（黑名单可能不完整）",
    }


# ---------------------------------------------------------------------------
# 管理员：全局事件表 + 抓取状态
# ---------------------------------------------------------------------------
admin_router = APIRouter(prefix="/api/admin/news", tags=["news"])


@admin_router.get("/events")
async def admin_news_events(
    days: int = 7,
    p: Principal = Depends(require_admin),
):
    """全局事件表（管理员排障用）：看事件有没有抓到、影响等级对不对。"""
    days = max(1, min(30, int(days or 7)))
    now = time.time()
    events = await economic_calendar.list_events(
        int(now - 86400), int(now + days * 86400),
    )
    fetched_at = await economic_calendar.latest_fetched_at()
    return {
        "events": [
            {
                "event_id": ev["event_id"],
                "event_time": ev["event_time"],
                "event_time_text": _fmt_beijing(ev["event_time"]),
                "title": ev["title"],
                "country": ev["country"],
                "impact": ev["impact"],
                "forecast": ev.get("forecast"),
                "previous": ev.get("previous"),
            }
            for ev in events
        ],
        "calendar": {
            "fetched_at": fetched_at,
            "fetched_at_text": _fmt_beijing(int(fetched_at)) if fetched_at else None,
            "stale": (fetched_at <= 0)
            or ((now - fetched_at) > economic_calendar.FETCH_INTERVAL_SEC * 2),
            "event_count": len(events),
            "timezone": "北京时间 UTC+8",
        },
    }
