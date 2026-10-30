"""下单时延埋点：把「信号到成交」拆成可测量的几段。

背景：一条信号经服务端并发下发给多个节点后，各节点的开仓时刻与开仓价会不一致。
要判断慢在哪一段，必须先有可测量的数据。本模块只做**观测**，不改变任何交易行为。

时间戳一律用节点本地 `time.time()`（同机同进程内可比），跨机不可比——
服务端只用**同一节点内的差值**，跨节点的比较统一走服务端自己的时间轴
（命令里的 `sent_at` → 服务端收到回报的时刻）。

四段分解：

| 字段 | 含义 | 对应根因 |
| --- | --- | --- |
| `recv_at` | 节点从 WS 读到这条命令的时刻 | — |
| `order_at` | 真正调 `order_send` 之前的时刻 | R1 单线程排队 / R2 前置往返 |
| `filled_at` | `order_send` 返回（拿到成交价）的时刻 | R3 重试退避 / R4 券商往返 |
| `queued_ms` | `order_at - recv_at`：**节点内部排队耗时** | R1 / R2 的直接度量 |
| `order_to_fill_ms` | `filled_at - order_at`：券商往返耗时 | R3 / R4 的直接度量 |

`recv_ms`（服务端 `sent_at` 到节点 `recv_at`）无法在节点侧算——两者时钟不同源，
由服务端在收到回报后用服务端自己的时间轴减出来。
"""
from __future__ import annotations

import time
from typing import Optional

# 回报里承载埋点字段的键名（与服务端 persist.py 的取值口径一致）
KEY_RECV_AT = "recv_at"
KEY_ORDER_AT = "order_at"
KEY_FILLED_AT = "filled_at"
KEY_QUEUED_MS = "queued_ms"
KEY_ORDER_TO_FILL_MS = "order_to_fill_ms"


def now() -> float:
    """当前时刻（秒，浮点）。埋点统一从这里取，便于将来做时钟校准。"""
    return time.time()


def build(
    recv_at: Optional[float] = None,
    order_at: Optional[float] = None,
    filled_at: Optional[float] = None,
) -> dict:
    """构造埋点字段。三个时刻都可缺省，缺省项不产出对应字段。

    返回的字典直接并入 `trade_result` 的 data；旧服务端会忽略未知字段，
    新服务端会落库。因此**加字段是安全的**。
    """
    out: dict = {}
    if recv_at is not None:
        out[KEY_RECV_AT] = round(float(recv_at), 6)
    if order_at is not None:
        out[KEY_ORDER_AT] = round(float(order_at), 6)
    if filled_at is not None:
        out[KEY_FILLED_AT] = round(float(filled_at), 6)
    if recv_at is not None and order_at is not None:
        out[KEY_QUEUED_MS] = round(max(0.0, (order_at - recv_at) * 1000.0), 3)
    if order_at is not None and filled_at is not None:
        out[KEY_ORDER_TO_FILL_MS] = round(max(0.0, (filled_at - order_at) * 1000.0), 3)
    return out


def merge(payload: dict, marks: dict) -> dict:
    """把埋点并入回报体（原地修改并返回同一个 dict）。"""
    if marks:
        payload.update(marks)
    return payload
