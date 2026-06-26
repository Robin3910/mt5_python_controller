"""TradingView 信号解析器。

行为逐条对齐参考仓库 `mt5_python_connector/tradingview_parser.py`，以保证“信号
接收规则 / 解析逻辑”与原项目完全一致；相对原文件唯一的改动是 `Config` 的 import 路径。

支持三种入参形态：
1. 结构化 JSON（含 action/symbol 等字段）；
2. 纯文本告警（关键字 + 正则）；
3. JSON 中携带文本字段（text / message / body）时，回退为文本解析。

注意：这里实现的是“参考仓库源码的真实行为”，而非其 README 描述。例如
`{"signal": "buy EURUSD 0.1"}` 因为取不到 symbol 字段，实际会解析失败（返回 None）。
"""
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from .config import Config

logger = logging.getLogger(__name__)


@dataclass
class TradingSignal:
    """解析后的标准交易信号（字段与参考仓库保持一致）。"""

    action: str  # 动作：BUY / SELL / CLOSE
    symbol: str  # 交易品种（已做 TradingView -> MT5 的映射）
    volume: float  # 手数
    stop_loss: Optional[float] = None  # 止损价
    take_profit: Optional[float] = None  # 止盈价
    order_type: Optional[str] = None  # 订单类型：market / limit / stop
    comment: str = ""  # 订单备注
    allow_position: bool = False  # 是否允许“已有持仓时”继续开仓（覆盖持仓过滤）


class TradingViewParser:
    """TradingView Webhook 信号解析器。"""

    def __init__(self) -> None:
        # 关键字 / 品种映射均来自 Config，便于与参考仓库保持同源
        self.symbols = Config.SYMBOLS
        self.buy_keywords = Config.BUY_KEYWORDS
        self.sell_keywords = Config.SELL_KEYWORDS
        self.close_keywords = Config.CLOSE_KEYWORDS

    def parse(self, data: Any) -> Optional[TradingSignal]:
        """把 Webhook 原始数据解析为 TradingSignal；无法解析时返回 None。"""
        # 1) 纯字符串：直接走文本解析
        if isinstance(data, str):
            return self._parse_text(data)

        # 2) 非 dict 一律不支持
        if not isinstance(data, dict):
            logger.warning(f"Unsupported data type: {type(data)}")
            return None

        # 3) 先尝试结构化 JSON 解析
        signal = self._parse_json(data)
        if signal:
            return signal

        # 4) JSON 里若带有文本字段，则回退到文本解析
        if "text" in data or "message" in data or "body" in data:
            text = data.get("text") or data.get("message") or data.get("body", "")
            return self._parse_text(str(text))

        logger.warning(f"Could not parse signal data: {data}")
        return None

    def _parse_json(self, data: Dict[str, Any]) -> Optional[TradingSignal]:
        """解析结构化 JSON：action 与 symbol 缺一不可。"""
        # 字段名统一转小写，做大小写无关匹配
        normalized = {k.lower(): v for k, v in data.items()}

        # 动作与品种是必需项，任一缺失即判定为不可解析
        action = self._extract_action(normalized)
        if not action:
            return None

        symbol = self._extract_symbol(normalized)
        if not symbol:
            return None

        volume = self._extract_volume(normalized)

        # 止损：兼容 sl / stoploss / stop_loss / stop 等多种写法
        stop_loss = self._extract_price(
            normalized.get("sl")
            or normalized.get("stoploss")
            or normalized.get("stop_loss")
            or normalized.get("stop")
        )
        # 止盈：兼容 tp / takeprofit / take_profit / target 等写法
        take_profit = self._extract_price(
            normalized.get("tp")
            or normalized.get("takeprofit")
            or normalized.get("take_profit")
            or normalized.get("target")
        )

        comment = str(normalized.get("comment", "") or "")
        order_type = normalized.get("type") or normalized.get("ordertype") or "market"
        order_type = str(order_type).lower() if order_type else "market"
        allow_position = self._extract_allow_position(normalized)

        return TradingSignal(
            action=action,
            symbol=symbol,
            volume=volume,
            stop_loss=stop_loss,
            take_profit=take_profit,
            order_type=order_type,
            comment=comment,
            allow_position=allow_position,
        )

    def _parse_text(self, text: str) -> Optional[TradingSignal]:
        """解析纯文本告警：通过关键字判定方向、正则提取品种/手数/止盈止损。"""
        text_lower = text.lower()

        # 动作判定的优先级：CLOSE > BUY > SELL（平仓关键字优先，避免“close buy”被判成开仓）
        action = None
        for keyword in self.close_keywords:
            if keyword.lower() in text_lower:
                action = "CLOSE"
                break

        if not action:
            for keyword in self.buy_keywords:
                if keyword.lower() in text_lower:
                    action = "BUY"
                    break

        if not action:
            for keyword in self.sell_keywords:
                if keyword.lower() in text_lower:
                    action = "SELL"
                    break

        if not action:
            logger.warning(f"No valid action found in text: {text}")
            return None

        symbol = self._extract_symbol_from_text(text)
        if not symbol:
            return None

        volume = self._extract_volume_from_text(text)
        stop_loss = self._extract_sl_tp_from_text(text, "sl")
        take_profit = self._extract_sl_tp_from_text(text, "tp")

        return TradingSignal(
            action=action,
            symbol=symbol,
            volume=volume,
            stop_loss=stop_loss,
            take_profit=take_profit,
        )

    def _extract_action(self, data: Dict[str, Any]) -> Optional[str]:
        """从 action/signal/direction/cmd 字段里识别动作关键字。"""
        action_field = (
            data.get("action")
            or data.get("signal")
            or data.get("direction")
            or data.get("cmd")
        )
        if not action_field:
            return None

        try:
            action_str = str(action_field).lower()
        except (ValueError, TypeError):
            return None

        # 同样遵循 CLOSE > BUY > SELL 的优先级
        for keyword in self.close_keywords:
            if keyword.lower() in action_str:
                return "CLOSE"
        for keyword in self.buy_keywords:
            if keyword.lower() in action_str:
                return "BUY"
        for keyword in self.sell_keywords:
            if keyword.lower() in action_str:
                return "SELL"
        return None

    def _extract_symbol(self, data: Dict[str, Any]) -> Optional[str]:
        """从 symbol/ticker/s/sym 字段取品种，并做 TradingView -> MT5 映射。"""
        raw_symbol = (
            data.get("symbol") or data.get("ticker") or data.get("s") or data.get("sym")
        )
        if not raw_symbol:
            return None

        try:
            symbol = str(raw_symbol).upper().strip()
        except (ValueError, TypeError):
            logger.warning(f"Invalid symbol value: {raw_symbol}")
            return None

        if not symbol or symbol == "NONE":
            return None

        # 命中映射表则返回映射后的 MT5 品种；否则原样返回（交由节点端再做后缀解析）
        if symbol in self.symbols:
            return self.symbols[symbol]
        if symbol in self.symbols.values():
            return symbol
        for tv_symbol, mt5_symbol in self.symbols.items():
            if symbol == tv_symbol or symbol == mt5_symbol:
                return mt5_symbol
        return symbol

    def _extract_symbol_from_text(self, text: str) -> Optional[str]:
        """文本场景下的品种提取：依次尝试 SYMBOL=/TICKER= 标签、6 位品种、EUR/USD 形式。"""
        text_upper = text.upper()
        patterns = [
            r"\bSYMBOL[:\s=]+([A-Z]{3,6}[A-Z0-9]*)\b",
            r"\bTICKER[:\s=]+([A-Z]{3,6}[A-Z0-9]*)\b",
            r"\b([A-Z]{6})\b",  # 形如 EURUSD 的 6 位品种
            r"\b([A-Z]{3}[/][A-Z]{3})\b",  # 形如 EUR/USD 的外汇写法
        ]
        for pattern in patterns:
            match = re.search(pattern, text_upper)
            if match:
                symbol = match.group(1).replace("/", "")
                if symbol in self.symbols or symbol in self.symbols.values():
                    return self.symbols.get(symbol, symbol)
                if len(symbol) >= 6:
                    return symbol
        return None

    def _extract_volume(self, data: Dict[str, Any]) -> float:
        """从 volume/lotsize/lot/v/q 字段取手数；缺失或非法则回退默认手数，并按上限封顶。"""
        volume_field = (
            data.get("volume")
            or data.get("lotsize")
            or data.get("lot")
            or data.get("v")
            or data.get("q")
        )
        if volume_field is None:
            return Config.DEFAULT_LOT
        try:
            volume = float(volume_field)
            if volume <= 0:
                return Config.DEFAULT_LOT
            return min(volume, Config.MAX_LOT_SIZE)  # 不得超过单笔最大手数
        except (ValueError, TypeError):
            return Config.DEFAULT_LOT

    def _extract_volume_from_text(self, text: str) -> float:
        """文本场景下的手数提取：必须带 VOLUME/LOT 关键字，否则用默认手数。"""
        patterns = [
            r"\bVOLUME[:\s=]+([0-9.]+)",
            r"\bLOT[S]?[:\s=]+([0-9.]+)",
            r"\b([0-9.]+)\s*LOT",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                try:
                    volume = float(match.group(1))
                    return min(volume, Config.MAX_LOT_SIZE)
                except ValueError:
                    continue
        return Config.DEFAULT_LOT

    def _extract_price(self, value: Any) -> Optional[float]:
        """把价格字段转成正浮点数；0 / 空 / 非法一律视为无（None）。"""
        if not value or value == 0:
            return None
        try:
            price = float(value)
            return price if price > 0 else None
        except (ValueError, TypeError):
            return None

    def _extract_sl_tp_from_text(self, text: str, field: str) -> Optional[float]:
        """文本场景下提取止损(sl)/止盈(tp)。"""
        patterns = {
            "sl": [r"\bSL[:\s=]+([0-9.]+)", r"\bSTOP\s*LOSS[:\s=]+([0-9.]+)"],
            "tp": [
                r"\bTP[:\s=]+([0-9.]+)",
                r"\bTAKE\s*PROFIT[:\s=]+([0-9.]+)",
                r"\bTARGET[:\s=]+([0-9.]+)",
            ],
        }
        for pattern in patterns.get(field, []):
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                try:
                    return float(match.group(1))
                except ValueError:
                    continue
        return None

    def _extract_allow_position(self, data: Dict[str, Any]) -> bool:
        """解析 allow_position 标志：仅当取值等于 1 时为 True。"""
        value = (
            data.get("allow_position")
            or data.get("allowposition")
            or data.get("position_allowed")
        )
        if value is None:
            return False
        try:
            return bool(int(value)) == 1
        except (ValueError, TypeError):
            return False

    def validate_signal(self, signal: TradingSignal) -> Tuple[bool, Optional[str]]:
        """对解析结果做基本校验，返回 (是否合法, 错误原因)。"""
        if not signal.action:
            return False, "Missing action"
        if not signal.symbol:
            return False, "Missing symbol"
        if signal.volume <= 0:
            return False, "Invalid volume"
        if signal.stop_loss and signal.stop_loss <= 0:
            return False, "Invalid stop loss"
        if signal.take_profit and signal.take_profit <= 0:
            return False, "Invalid take profit"
        return True, None
