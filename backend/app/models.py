"""Pydantic 模型：API 入参/出参、领域对象、以及下发给节点的命令构造。"""
from __future__ import annotations

import time
from typing import Any, Optional

from pydantic import BaseModel, Field


# ----------------------------- 节点 -----------------------------
class NodeCreate(BaseModel):
    """创建节点的入参。"""
    name: Optional[str] = None  # 留空则自动生成 "node-{mt5_login}"
    mt5_login: int = Field(gt=0, description="绑定的 MT5 账户登录号（全局唯一）")
    lot_mode: str = "fixed"  # 手数策略：global / fixed / signal（默认 fixed）
    lot: Optional[float] = 0.01
    follow_sync: bool = True   # 是否参与“全员同步”分发
    follow_poll: bool = True   # 是否参与“轮询领取”分发
    poll_order: int = 0        # 轮询顺序（越小越先）
    filters: Optional[dict] = None  # 节点级区间过滤（可覆盖全局）


class NodeUpdate(BaseModel):
    """更新节点的入参（全部可选，仅更新提供的字段；mt5_login 创建后不可改）。"""
    name: Optional[str] = None
    enabled: Optional[bool] = None
    lot_mode: Optional[str] = None
    lot: Optional[float] = None
    follow_sync: Optional[bool] = None
    follow_poll: Optional[bool] = None
    poll_order: Optional[int] = None
    filters: Optional[dict] = None


class NodeOut(BaseModel):
    """节点对外展示对象（合并了在线状态与账户登录信息）。"""
    node_id: str
    name: str
    enabled: bool = True
    status: str = "offline"  # online / offline
    lot_mode: str = "global"
    lot: Optional[float] = None
    follow_sync: bool = True
    follow_poll: bool = True
    poll_order: int = 0
    mt5_login: Optional[int] = None
    mt5_server: Optional[str] = None
    created_at: float = 0
    last_seen: Optional[float] = None


class NodeTokenInfo(BaseModel):
    """全局节点接入令牌（所有节点共享）。明文存储，便于管理员复制到各节点 .env。"""
    token: str
    updated_at: float = 0


class LotBatch(BaseModel):
    """批量设置节点手数策略。"""
    node_ids: list[str]
    lot_mode: str
    lot: Optional[float] = None


class NodeDispatchRecord(BaseModel):
    """单节点分发/成交明细（信号原始数据 + 本节点处理情况）。"""
    id: int                       # 分发明细行唯一 ID（同一 signal_id 可能有多条，用于前端行级展开）
    signal_id: str
    symbol: Optional[str] = None
    action: Optional[str] = None
    volume: Optional[float] = None
    sl: Optional[float] = None
    tp: Optional[float] = None
    comment: Optional[str] = None
    source_ip: Optional[str] = None
    parsed_ok: Optional[bool] = None
    dispatch_mode: Optional[str] = None
    signal_status: Optional[str] = None
    received_at: Optional[float] = None
    raw_payload: Optional[str] = None
    decided_vol: Optional[float] = None
    gate_result: Optional[str] = None
    skip_reason: Optional[str] = None
    status: str = "pending"
    retcode: Optional[int] = None
    order: Optional[int] = None
    deal: Optional[int] = None
    price: Optional[float] = None
    error: Optional[str] = None
    dispatched_at: Optional[float] = None
    finished_at: Optional[float] = None


class PaginatedNodeDispatches(BaseModel):
    """节点分发/成交明细分页结果。"""
    items: list[NodeDispatchRecord]
    total: int
    page: int
    page_size: int


# --------------------------- 账户 ---------------------------
class Position(BaseModel):
    """单个持仓。"""
    ticket: int
    symbol: str
    type: str  # BUY / SELL
    volume: float
    price_open: float = 0
    price_current: float = 0
    sl: float = 0      # 止损价（0 表示未设置）
    tp: float = 0      # 止盈价（0 表示未设置）
    profit: float = 0
    magic: int = 0
    comment: str = ""
    time: float = 0


class QuoteInfo(BaseModel):
    """单个品种的实时报价。"""
    bid: float = 0       # 买价
    ask: float = 0       # 卖价
    mid: float = 0         # 中间价（供区间过滤）
    change: float = 0    # 日变化 %（MT5 SYMBOL_PRICE_CHANGE，相对昨收，与终端 Daily Change 一致）


class AccountSnapshot(BaseModel):
    """节点上报的账户快照。"""
    node_id: Optional[str] = None
    login: Optional[int] = None
    server: Optional[str] = None
    balance: float = 0
    equity: float = 0
    margin: float = 0
    free_margin: float = 0
    leverage: int = 0
    positions: list[Position] = Field(default_factory=list)
    prices: dict[str, float] = Field(default_factory=dict)  # 品种 -> 中间价（供区间过滤）
    quotes: dict[str, QuoteInfo] = Field(default_factory=dict)  # 品种 -> 完整报价
    updated_at: float = Field(default_factory=lambda: time.time())


# ---------------------------- 配置 ---------------------------
class LotConfig(BaseModel):
    """全局手数配置。"""
    enabled: bool = False
    value: float = 0.1


class IntervalRule(BaseModel):
    """单条价格区间规则：在 [low, high] 内允许哪些方向。"""
    low: float
    high: float
    allow: list[str] = Field(default_factory=list)  # BUY / SELL 的子集


class SymbolFilter(BaseModel):
    """某品种的多区间过滤配置。"""
    enabled: bool = True
    default_action: str = "block"  # 不在任何区间时：block 拦截 / pass 放行
    intervals: list[IntervalRule] = Field(default_factory=list)


class DispatchConfig(BaseModel):
    """分发配置。"""
    mode: str = "sync"  # sync / poll
    position_scope: str = "symbol"  # symbol / account


# ------------------------- 平仓请求 -----------------------
class CloseRequest(BaseModel):
    """远程平仓请求。"""
    target: str = "all"  # all（全平）/ symbol（按品种）/ ticket（按订单）
    symbol: Optional[str] = None
    ticket: Optional[int] = None


class CloseBatchRequest(CloseRequest):
    """对指定节点批量平仓。"""
    node_ids: list[str] = Field(min_length=1)


# ----------------------------- 鉴权 ----------------------------
class LoginRequest(BaseModel):
    username: str
    password: str


class ChangePasswordRequest(BaseModel):
    """修改后台管理员密码（需携带当前密码）。"""
    current_password: str
    new_password: str


class Login2FARequest(BaseModel):
    login_token: str
    totp_code: str


class TwoFACodeRequest(BaseModel):
    totp_code: str


class TwoFAPasswordRequest(BaseModel):
    password: str
    totp_code: Optional[str] = None


def build_open_command(signal_id: str, action: str, symbol: str, volume: float,
                       stop_loss: Optional[float], take_profit: Optional[float],
                       comment: str = "", magic: Optional[int] = None) -> dict[str, Any]:
    """构造下发给节点的“开仓”命令。"""
    return {
        "cmd": "open",
        "signal_id": signal_id,
        "action": action,
        "symbol": symbol,
        "volume": volume,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "comment": comment,
        "magic": magic,
    }


def build_close_command(signal_id: str, target: str, symbol: Optional[str] = None,
                        ticket: Optional[int] = None) -> dict[str, Any]:
    """构造下发给节点的“平仓”命令。"""
    return {
        "cmd": "close",
        "signal_id": signal_id,
        "close_target": target,
        "close_symbol": symbol,
        "close_ticket": ticket,
    }
