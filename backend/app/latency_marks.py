"""时延埋点：服务端侧的时间戳记录与换算。

节点的埋点字段（见 `node_client/latency_marks.py`）用的是**节点本地时钟**，
与本机时钟不同源，因此跨机的绝对时刻不可比。本模块只负责：

1. **记录**服务端自己的时刻（发包、收到回报）——这是唯一可跨节点比较的时间轴；
2. **落库**节点上报的原始时刻（`recv_at` / `order_at` / `filled_at`），
   供同机内的差值分析（排队耗时、券商往返耗时）。

时钟不可比带来的坑：`recv_at` 直接落库会让人误以为它和 `sent_at` 能相减。
因此 API 层**只**暴露两段同机差值（`queued_ms` / `order_to_fill_ms`），
跨节点比较一律走服务端时间轴（`sent_at` → 收到回报的时刻）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

# 节点上报的埋点字段名（与 node_client/latency_marks.py 的键名一致）
KEY_RECV_AT = "recv_at"
KEY_ORDER_AT = "order_at"
KEY_FILLED_AT = "filled_at"
KEY_QUEUED_MS = "queued_ms"
KEY_ORDER_TO_FILL_MS = "order_to_fill_ms"

# 节点上报里可能出现、但服务端不应落库的内部键（MT5 客户端的观测字段）
INTERNAL_KEYS = ("_order_at", "_filled_at")


def utcnow() -> datetime:
    """服务端当前时刻（本地时区的 naive datetime，与库里其它时间列同口径）。"""
    return datetime.now()


def as_datetime(value: object) -> Optional[datetime]:
    """把节点上报的时刻（Unix 秒，浮点）转成 datetime。

    非法值一律当作缺失——埋点坏了不该影响交易链路。
    """
    if value is None:
        return None
    try:
        ts = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    try:
        return datetime.fromtimestamp(ts)
    except (OverflowError, OSError, ValueError):
        return None


def as_float(value: object) -> Optional[float]:
    """把节点上报的耗时（毫秒）转成 float；非法值当作缺失。"""
    if value is None:
        return None
    try:
        num = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return num


def extract(result: dict) -> dict:
    """从节点回报里抽出埋点字段，转成可直接落库的值。

    返回形如：
    ```
    {
        "recv_at": datetime | None,
        "order_at": datetime | None,
        "filled_at": datetime | None,
        "queued_ms": float | None,
        "order_to_fill_ms": float | None,
    }
    ```
    """
    result = result or {}
    return {
        KEY_RECV_AT: as_datetime(result.get(KEY_RECV_AT)),
        KEY_ORDER_AT: as_datetime(result.get(KEY_ORDER_AT)),
        KEY_FILLED_AT: as_datetime(result.get(KEY_FILLED_AT)),
        KEY_QUEUED_MS: as_float(result.get(KEY_QUEUED_MS)),
        KEY_ORDER_TO_FILL_MS: as_float(result.get(KEY_ORDER_TO_FILL_MS)),
    }
