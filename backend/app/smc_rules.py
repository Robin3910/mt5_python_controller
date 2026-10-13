"""TradingView SMC 价位推送的纯解析与匹配（无 I/O）。

`type=smc-data` 不是开仓信号：只携带某一品种、某一周期上的价位列表，
供逆势分批里 fvg / ob / pdz / fibo 档位按标识挑选。节点不计算这些指标。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .strategy_templates import (
    BATCH_CALC_SMC_TYPES,
    BATCH_TIMEFRAMES,
    RULE_TYPE_COUNTER,
)

SMC_PAYLOAD_TYPE = "smc-data"


@dataclass
class SmcPayload:
    """一份已校验的推送。levels 只保留合法记录，顺序与来源一致。"""

    symbol: str
    tf: str
    levels: list[dict[str, Any]] = field(default_factory=list)
    dropped: int = 0
    received_at: float = 0.0

    def to_store(self) -> dict[str, Any]:
        """写入 Redis / 下发给节点的副本。每次推送整体替换，不按周期合并。"""
        return {
            "symbol": self.symbol,
            "tf": self.tf,
            "levels": list(self.levels),
            "received_at": self.received_at,
        }


def _normalized(data: dict) -> dict[str, Any]:
    return {str(k).lower(): v for k, v in data.items()}


def is_smc_payload(data: object) -> bool:
    """是否 SMC 价位推送。纯文本告警和普通交易信号都返回 False。"""
    if not isinstance(data, dict):
        return False
    raw = _normalized(data).get("type")
    return str(raw or "").strip().lower() == SMC_PAYLOAD_TYPE


def _as_side(value: object) -> Optional[int]:
    """1=支撑，-1=阻力。布尔值和带小数的数一律视为非法。"""
    if isinstance(value, bool) or isinstance(value, float):
        return None
    try:
        side = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return side if side in (1, -1) else None


def _as_price(value: object) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        price = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


def _parse_level(raw: object) -> Optional[dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    item = _normalized(raw)
    calc_type = str(item.get("type") or "").strip().lower()
    if calc_type not in BATCH_CALC_SMC_TYPES:
        return None
    side = _as_side(item.get("side"))
    price = _as_price(item.get("price"))
    if side is None or price is None:
        return None
    return {"type": calc_type, "side": side, "price": price}


def parse_smc_payload(data: object) -> SmcPayload:
    """校验顶层字段。单条记录非法时丢弃并计数，不让整包失败。

    空 data 合法，表示用一份空列表覆写该任务当前副本。
    """
    if not isinstance(data, dict):
        raise ValueError("请求体必须是 JSON 对象")
    body = _normalized(data)
    symbol = str(body.get("symbol") or "").strip().upper()
    if not symbol:
        raise ValueError("缺少 symbol")
    tf = str(body.get("tf") or "").strip().upper()
    if tf not in BATCH_TIMEFRAMES:
        raise ValueError(f"tf 无效：{body.get('tf')}")
    raw_levels = body.get("data")
    if not isinstance(raw_levels, list):
        raise ValueError("data 必须是数组")
    levels: list[dict[str, Any]] = []
    dropped = 0
    for raw in raw_levels:
        level = _parse_level(raw)
        if level is None:
            dropped += 1
        else:
            levels.append(level)
    return SmcPayload(
        symbol=symbol, tf=tf, levels=levels, dropped=dropped, received_at=time.time(),
    )


def _as_int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def snapshot_uses_smc_tf(snapshot: object, tf: str) -> bool:
    """任务快照的逆势分批里，是否有启用中的、周期匹配的 SMC 档位。

    规则关闭或分批未开时节点不会看这些价位，推送不必为它留副本。
    """
    if not isinstance(snapshot, dict):
        return False
    want = str(tf or "").strip().upper()
    rules = snapshot.get("rules")
    if not isinstance(rules, list) or not want:
        return False
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        if _as_int(rule.get("type")) != RULE_TYPE_COUNTER:
            continue
        if not _as_int(rule.get("status")) or not rule.get("batch_enabled"):
            continue
        for level in rule.get("batch_levels") or []:
            if not isinstance(level, dict):
                continue
            calc_type = str(level.get("calc_type") or "").strip().lower()
            if calc_type not in BATCH_CALC_SMC_TYPES:
                continue
            if str(level.get("timeframe") or "").strip().upper() == want:
                return True
    return False
