"""MT5 链路状态（终端 ↔ 券商）的服务端侧判定。

链路口径与节点侧一致：`terminal_info().connected` 为 False 表示终端当前
没有连着交易服务器。下单回执在这种状态下不可靠（order_send 可能返回
None，也可能超时），订单却可能已在券商侧成交 —— 这正是孤儿单的成因，
所以断联期间不该下发开仓指令。

服务端看到的是**滞后状态**（节点上报的快照），所以判定必须保守：
- 状态缺失 / 非法 → 放行（不能因为读不到就误伤节点）；
- 状态过期 → 放行（节点可能早就恢复了）。
真正把关的是节点侧闸门（离下单最近），服务端这层只是提前可见、少发一次。
"""
from __future__ import annotations

import time
from typing import Optional

# 断联状态的最长有效期（秒）：上报快照超过这个时间没更新，
# 就不能再拿「断联」当依据。
LINK_OFFLINE_TTL = 30.0

LINK_ONLINE = "online"
LINK_OFFLINE = "offline"
LINK_UNKNOWN = "unknown"

LINK_OFFLINE_REASON = "MT5 终端与券商断连，本次未下发"


def normalize_link_state(value: object) -> str:
    """把上报值规范成三态；非法值一律按 unknown。"""
    text = str(value or "").strip().lower()
    if text in (LINK_ONLINE, LINK_OFFLINE, LINK_UNKNOWN):
        return text
    return LINK_UNKNOWN


def link_offline_reason(snapshot: Optional[dict], *, ttl: float = LINK_OFFLINE_TTL) -> Optional[str]:
    """快照显示终端断联时返回拦截原因；否则返回 None（放行）。

    ttl：快照的最长有效期。超时的「断联」不再作数 —— 节点可能早就恢复。
    """
    if not isinstance(snapshot, dict):
        return None
    if normalize_link_state(snapshot.get("mt5_link")) != LINK_OFFLINE:
        return None
    try:
        updated_at = float(snapshot.get("updated_at") or 0.0)
    except (TypeError, ValueError):
        return None
    if not updated_at or (time.time() - updated_at) > ttl:
        return None
    return LINK_OFFLINE_REASON
