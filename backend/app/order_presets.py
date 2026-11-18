"""开仓参数预设（Order Preset）：我的下单默认值。

一句话：**在【策略配置】里预设好固定参数，每次去【交易】页真实开仓时自动填好，
用户只填随行情变化的实时价格。**

它与「策略实例」（`trading_strategies`）是**两件事**，刻意分开存：

  - 策略实例 = 给节点执行的策略配置，随信号下发，改动有副作用（热推 / 快照冻结）
  - 本表 = 我下单时的初始值，纯前端体验，不下发节点、不产生副作用

**手数不在这里**：手数由节点按「风险金额 ÷ 止损距离」反推（以损定量），
或由网格试算按「最大亏损预算」反推 —— 用户不填手数。

**没有行 = 用默认值**：存量用户零配置即生效，不需要回填。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select

from .db import SessionLocal
from .orm import UserOrderPreset

logger = logging.getLogger(__name__)

# —— 预设的品种维度：'*' = 通用模板（所有品种共用一套）——
WILDCARD_SYMBOL = "*"

# —— 三种开仓方式，各自一份预设，互不污染 ——
STRATEGY_TYPES = ("trend_market", "trend_limit", "grid")
STRATEGY_TREND = ("trend_market", "trend_limit")
STRATEGY_GRID = "grid"

# —— 默认值（没有行 / 没填时用）——
DEFAULT_RISK_AMOUNT = 100.0
DEFAULT_RR_RATIO = 2.5
DEFAULT_BASE_RATIO = 30.0
DEFAULT_BREAKEVEN_TIMES = 0.0
DEFAULT_GRID_MODE = "arithmetic"
DEFAULT_MAX_ALLOW_LOSS = 100.0
DEFAULT_TRAILING_UP = False

GRID_MODES = ("arithmetic", "geometric")

# 底仓比例夹在 (0, 100]：0 会导致底仓无手数、首单无从下手
BASE_RATIO_MIN = 1.0
BASE_RATIO_MAX = 100.0


@dataclass
class OrderPreset:
    """一条预设（对外的稳定结构，与 ORM 行一一对应）。"""

    symbol: str
    strategy_type: str
    risk_amount: float
    rr_ratio: float
    base_ratio: float
    breakeven_times: float
    grid_mode: str
    max_allow_loss: float
    trailing_up: bool

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "strategy_type": self.strategy_type,
            "risk_amount": self.risk_amount,
            "rr_ratio": self.rr_ratio,
            "base_ratio": self.base_ratio,
            "breakeven_times": self.breakeven_times,
            "grid_mode": self.grid_mode,
            "max_allow_loss": self.max_allow_loss,
            "trailing_up": self.trailing_up,
        }


def normalize_symbol(value: object) -> str:
    """品种归一：去空白 + 大写；空 → 通用模板哨兵 '*'。"""
    text = str(value or "").strip().upper()
    return text or WILDCARD_SYMBOL


def normalize_strategy_type(value: object) -> str:
    """开仓方式归一；非法值回落市价开仓。"""
    text = str(value or "").strip().lower()
    return text if text in STRATEGY_TYPES else "trend_market"


def normalize_grid_mode(value: object) -> str:
    """网格模式归一；非法值回落等差。"""
    text = str(value or "").strip().lower()
    return text if text in GRID_MODES else DEFAULT_GRID_MODE


def _clamp_float(
    value: object, default: float, *, minimum: float = 0.0,
    strict_min: bool = False,
) -> float:
    """数值归一。

    - 非法值 / NaN → default
    - 负数 → default（负的风险金额 / 盈亏比没有意义）
    - 小于 minimum → minimum（strict_min=True 时同样回落 default）
    """
    try:
        num = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if num != num:  # NaN
        return default
    if num < 0:
        return default
    if num < minimum:
        return default if strict_min else minimum
    return num


def normalize_preset_fields(payload: dict, *, base: OrderPreset | None = None) -> OrderPreset:
    """把任意入参整理成一条稳定的预设。

    字段级「传什么改什么」：不传的字段保持原值（base），base 为空时用默认值。
    """
    src = payload if isinstance(payload, dict) else {}

    # —— 市价 / 限价 ——
    if base is not None:
        risk_amount = base.risk_amount
        rr_ratio = base.rr_ratio
        base_ratio = base.base_ratio
        breakeven_times = base.breakeven_times
    else:
        risk_amount = DEFAULT_RISK_AMOUNT
        rr_ratio = DEFAULT_RR_RATIO
        base_ratio = DEFAULT_BASE_RATIO
        breakeven_times = DEFAULT_BREAKEVEN_TIMES
    if src.get("risk_amount") is not None:
        risk_amount = _clamp_float(src.get("risk_amount"), risk_amount)
    if src.get("rr_ratio") is not None:
        rr_ratio = _clamp_float(src.get("rr_ratio"), rr_ratio)
    if src.get("base_ratio") is not None:
        base_ratio = _clamp_float(
            src.get("base_ratio"), base_ratio, minimum=BASE_RATIO_MIN
        )
        base_ratio = min(BASE_RATIO_MAX, base_ratio)
    if src.get("breakeven_times") is not None:
        breakeven_times = _clamp_float(src.get("breakeven_times"), breakeven_times)

    # —— 网格 ——
    if base is not None:
        grid_mode = base.grid_mode
        max_allow_loss = base.max_allow_loss
        trailing_up = base.trailing_up
    else:
        grid_mode = DEFAULT_GRID_MODE
        max_allow_loss = DEFAULT_MAX_ALLOW_LOSS
        trailing_up = DEFAULT_TRAILING_UP
    if src.get("grid_mode") is not None:
        grid_mode = normalize_grid_mode(src.get("grid_mode"))
    if src.get("max_allow_loss") is not None:
        max_allow_loss = _clamp_float(src.get("max_allow_loss"), max_allow_loss)
    if src.get("trailing_up") is not None:
        trailing_up = bool(src.get("trailing_up"))

    return OrderPreset(
        symbol=normalize_symbol(src.get("symbol")),
        strategy_type=normalize_strategy_type(src.get("strategy_type")),
        risk_amount=risk_amount,
        rr_ratio=rr_ratio,
        base_ratio=base_ratio,
        breakeven_times=breakeven_times,
        grid_mode=grid_mode,
        max_allow_loss=max_allow_loss,
        trailing_up=trailing_up,
    )


def _row_to_preset(row: UserOrderPreset) -> OrderPreset:
    return OrderPreset(
        symbol=row.symbol,
        strategy_type=row.strategy_type,
        risk_amount=float(row.risk_amount),
        rr_ratio=float(row.rr_ratio),
        base_ratio=float(row.base_ratio),
        breakeven_times=float(row.breakeven_times),
        grid_mode=row.grid_mode,
        max_allow_loss=float(row.max_allow_loss),
        trailing_up=bool(row.trailing_up),
    )


async def list_presets(user_id: int) -> list[OrderPreset]:
    """列出某个用户的全部预设（按 symbol / strategy_type 排序）。"""
    if not user_id or int(user_id) <= 0:
        return []
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(UserOrderPreset).where(UserOrderPreset.user_id == int(user_id))
            )
        ).scalars().all()
    out = [_row_to_preset(r) for r in rows]
    out.sort(key=lambda p: (p.symbol != WILDCARD_SYMBOL, p.symbol, p.strategy_type))
    return out


async def get_preset(user_id: int, symbol: str, strategy_type: str) -> Optional[OrderPreset]:
    """取一条精确匹配的预设；没有则 None。"""
    if not user_id or int(user_id) <= 0:
        return None
    sym = normalize_symbol(symbol)
    st = normalize_strategy_type(strategy_type)
    async with SessionLocal() as session:
        row = await session.scalar(
            select(UserOrderPreset).where(
                UserOrderPreset.user_id == int(user_id),
                UserOrderPreset.symbol == sym,
                UserOrderPreset.strategy_type == st,
            )
        )
    return _row_to_preset(row) if row else None


async def save_preset(user_id: int, payload: dict) -> OrderPreset:
    """保存一条预设（upsert）。字段级「传什么改什么」。"""
    uid = int(user_id)
    sym = normalize_symbol(payload.get("symbol"))
    st = normalize_strategy_type(payload.get("strategy_type"))
    base = await get_preset(uid, sym, st)
    normalized = normalize_preset_fields(payload, base=base)
    async with SessionLocal() as session:
        row = await session.scalar(
            select(UserOrderPreset).where(
                UserOrderPreset.user_id == uid,
                UserOrderPreset.symbol == sym,
                UserOrderPreset.strategy_type == st,
            )
        )
        if row is None:
            row = UserOrderPreset(user_id=uid, symbol=sym, strategy_type=st)
            session.add(row)
        row.risk_amount = normalized.risk_amount
        row.rr_ratio = normalized.rr_ratio
        row.base_ratio = normalized.base_ratio
        row.breakeven_times = normalized.breakeven_times
        row.grid_mode = normalized.grid_mode
        row.max_allow_loss = normalized.max_allow_loss
        row.trailing_up = normalized.trailing_up
        await session.commit()
    return normalized


async def delete_preset(user_id: int, symbol: str, strategy_type: str) -> bool:
    """删除一条预设；返回是否真的删掉了。"""
    uid = int(user_id)
    sym = normalize_symbol(symbol)
    st = normalize_strategy_type(strategy_type)
    async with SessionLocal() as session:
        row = await session.scalar(
            select(UserOrderPreset).where(
                UserOrderPreset.user_id == uid,
                UserOrderPreset.symbol == sym,
                UserOrderPreset.strategy_type == st,
            )
        )
        if row is None:
            return False
        await session.delete(row)
        await session.commit()
    return True


async def resolve_preset(
    user_id: int, symbol: str, strategy_type: str,
) -> Optional[OrderPreset]:
    """按「精确品种优先，其次通用模板」的顺序解析出一条预设。

    解析优先级：精确 symbol > `'*'` 通用模板 > None（调用方回落到策略实例 / 模版默认值）。
    """
    sym = normalize_symbol(symbol)
    exact = await get_preset(user_id, sym, strategy_type)
    if exact is not None:
        return exact
    if sym == WILDCARD_SYMBOL:
        return None
    return await get_preset(user_id, WILDCARD_SYMBOL, strategy_type)
