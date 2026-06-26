"""MT5 交易客户端——操作规则与参考仓库 `mt5_python_connector/mt5_client.py` 保持一致：

- 品种解析：自动尝试券商后缀并 symbol_select 选入行情；
- 填充模式：探测 FOK/IOC/RETURN，遇到 10030(不支持填充模式)自动切换；
- 市价单：TRADE_ACTION_DEAL，带 deviation(滑点)/magic(魔术号)/comment；
- 平仓：用反方向 deal + position(订单号) 在当前价平掉；
- 重试：对瞬时错误(requote/价格变动等)刷新价格后重试。
"""
import logging
import time
from typing import Optional

try:  # MetaTrader5 仅 Windows 可用，这里做守卫，保证模块在任何平台都能 import
    import MetaTrader5 as mt5  # type: ignore
except Exception:  # noqa: BLE001
    mt5 = None  # type: ignore

logger = logging.getLogger("node.mt5")

# —— MT5 返回码(retcode)子集，分类见技术方案 10.7 ——
RET_DONE = 10009            # 成交
RET_DONE_PARTIAL = 10010    # 部分成交
RET_REQUOTE = 10004         # 重新报价
RET_PRICE_CHANGED = 10020   # 价格已变
RET_PRICE_OFF = 10021       # 无报价/价格关闭
RET_TIMEOUT = 10012         # 超时
RET_INVALID_FILL = 10030    # 不支持的填充模式
TRANSIENT = {RET_REQUOTE, RET_PRICE_CHANGED, RET_PRICE_OFF, RET_TIMEOUT}  # 可重试的瞬时错误


class MT5Error(RuntimeError):
    pass


class MT5Client:
    def __init__(self, login: int, password: str, server: str, path: str = "",
                 slippage: int = 20, magic: int = 20240615) -> None:
        self.login = int(login)
        self.password = password
        self.server = server
        self.path = path            # 可选：terminal64.exe 路径
        self.slippage = slippage    # 默认滑点（deviation）
        self.magic = magic          # 默认魔术号
        self.connected = False

    # ----------------------- 连接 -----------------------
    def connect(self) -> bool:
        """初始化并登录 MT5 终端；任一步失败返回 False。"""
        if mt5 is None:
            raise MT5Error("MetaTrader5 package not available on this host")
        kwargs = {"path": self.path} if self.path else {}
        if not mt5.initialize(**kwargs):
            logger.error("mt5.initialize failed: %s", mt5.last_error())
            return False
        if self.login:
            if not mt5.login(self.login, password=self.password, server=self.server):
                logger.error("mt5.login failed for %s@%s: %s", self.login, self.server, mt5.last_error())
                mt5.shutdown()
                return False
        self.connected = True
        logger.info("MT5 connected: login=%s server=%s", self.login, self.server)
        return True

    def disconnect(self) -> None:
        if mt5 is not None and self.connected:
            mt5.shutdown()
        self.connected = False

    def ensure(self) -> None:
        """所有交易/查询前的连接断言。"""
        if mt5 is None or not self.connected:
            raise MT5Error("MT5 not connected")

    # ------------------------- 查询 -------------------------
    def account_info(self) -> dict:
        """返回账户概况（余额/净值/保证金/杠杆等）。"""
        self.ensure()
        ai = mt5.account_info()
        if ai is None:
            return {}
        return {
            "login": ai.login,
            "server": getattr(ai, "server", self.server),
            "balance": ai.balance,
            "equity": ai.equity,
            "margin": ai.margin,
            "free_margin": ai.margin_free,
            "leverage": ai.leverage,
            "currency": ai.currency,
        }

    def positions(self) -> list[dict]:
        """返回当前所有持仓（标准化字段，方向转 BUY/SELL 字符串）。"""
        self.ensure()
        out = []
        for p in mt5.positions_get() or []:
            out.append(
                {
                    "ticket": p.ticket,
                    "symbol": p.symbol,
                    "type": "BUY" if p.type == mt5.POSITION_TYPE_BUY else "SELL",
                    "volume": p.volume,
                    "price_open": p.price_open,
                    "price_current": p.price_current,
                    "sl": p.sl,
                    "tp": p.tp,
                    "profit": p.profit,
                    "magic": p.magic,
                    "comment": p.comment,
                    "time": p.time,
                }
            )
        return out

    def _daily_change_pct(self, info, mid: float) -> float:
        """日涨跌幅 %，与 MT5 Market Watch「Daily Change」列一致（SYMBOL_PRICE_CHANGE，相对昨收）。"""
        if info is not None:
            pc = getattr(info, "price_change", None)
            if pc is not None:
                return round(float(pc), 4)
        open_px = float(getattr(info, "session_open", 0) or getattr(info, "price_open", 0) or 0) if info else 0.0
        if open_px:
            return round((mid - open_px) / open_px * 100, 4)
        return 0.0

    def quotes(self, symbols: list[str]) -> dict[str, dict]:
        """批量取观察列表报价详情（买价/卖价/中间价/日变化）。"""
        self.ensure()
        out: dict[str, dict] = {}
        for sym in symbols:
            resolved = self.resolve_symbol(sym)
            if not resolved:
                continue
            tick = mt5.symbol_info_tick(resolved)
            if not tick or not (tick.bid or tick.ask):
                continue
            info = mt5.symbol_info(resolved)
            digits = int(getattr(info, "digits", 5) or 5) if info else 5
            bid = float(tick.bid or 0)
            ask = float(tick.ask or 0)
            mid = round((bid + ask) / 2, digits) if bid and ask else round(ask or bid, digits)
            out[sym] = {
                "bid": round(bid, digits),
                "ask": round(ask, digits),
                "mid": mid,
                "change": self._daily_change_pct(info, mid),
            }
        return out

    def prices(self, symbols: list[str]) -> dict[str, float]:
        """批量取观察列表的中间价（供后端做区间方向过滤）。"""
        return {sym: q["mid"] for sym, q in self.quotes(symbols).items()}

    # --------------------- 品种解析 -------------------
    def resolve_symbol(self, symbol: str) -> Optional[str]:
        """解析为券商实际品种名：原名 -> 常见后缀变体 -> 全量扫描前缀匹配。

        命中后若行情不可见会自动 symbol_select 选入，确保能取到 tick。
        """
        self.ensure()
        candidates = [symbol, symbol.upper()]
        # 常见券商后缀（如 XAUUSD.m / EURUSDmicro 等）
        for suffix in (".m", ".c", "m", "micro", ".pro", ".raw", "."):
            candidates.append(f"{symbol}{suffix}")
        seen = set()
        for cand in candidates:
            if cand in seen:
                continue
            seen.add(cand)
            info = mt5.symbol_info(cand)
            if info is not None:
                if not info.visible:
                    mt5.symbol_select(cand, True)
                return cand
        # 兜底：扫描全部品种做前缀匹配
        base = symbol.upper()
        for info in mt5.symbols_get() or []:
            if info.name.upper().startswith(base):
                mt5.symbol_select(info.name, True)
                return info.name
        logger.warning("symbol not resolved: %s", symbol)
        return None

    def filling_modes(self, symbol: str) -> list[int]:
        """返回该品种可尝试的填充模式顺序（优先用品种支持的，再补全兜底项）。"""
        info = mt5.symbol_info(symbol)
        order = []
        if info is not None:
            fm = info.filling_mode  # 位掩码：1=FOK, 2=IOC
            if fm & 1:
                order.append(mt5.ORDER_FILLING_FOK)
            if fm & 2:
                order.append(mt5.ORDER_FILLING_IOC)
        # 始终保留兜底，覆盖部分券商上报不准的情况
        for f in (mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_RETURN):
            if f not in order:
                order.append(f)
        return order

    # --------------------------- 交易 -------------------------
    def place_market_order(self, symbol: str, action: str, volume: float,
                           sl: Optional[float] = None, tp: Optional[float] = None,
                           comment: str = "", magic: Optional[int] = None,
                           max_retry: int = 3) -> dict:
        """下市价单。内部循环处理：填充模式切换(10030) + 瞬时错误重试(刷新价格)。"""
        self.ensure()
        resolved = self.resolve_symbol(symbol)
        if not resolved:
            return {"success": False, "error": f"symbol not found: {symbol}"}

        order_type = mt5.ORDER_TYPE_BUY if action == "BUY" else mt5.ORDER_TYPE_SELL
        fillings = self.filling_modes(resolved)
        fill_idx = 0     # 当前尝试的填充模式下标
        attempt = 0      # 瞬时错误重试计数
        last = None

        while attempt <= max_retry and fill_idx < len(fillings):
            # 每次都重新取价（重试时价格可能已变）
            tick = mt5.symbol_info_tick(resolved)
            if tick is None:
                return {"success": False, "error": "no tick", "symbol": symbol}
            price = tick.ask if action == "BUY" else tick.bid
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": resolved,
                "volume": float(volume),
                "type": order_type,
                "price": price,
                "deviation": self.slippage,
                "magic": int(magic if magic is not None else self.magic),
                "comment": comment or "tv-signal",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": fillings[fill_idx],
            }
            if sl:
                request["sl"] = float(sl)
            if tp:
                request["tp"] = float(tp)

            result = mt5.order_send(request)
            last = result
            if result is None:
                logger.error("order_send returned None: %s", mt5.last_error())
                attempt += 1
                continue

            rc = result.retcode
            if rc in (RET_DONE, RET_DONE_PARTIAL):
                # 成交
                return {
                    "success": True,
                    "symbol": symbol,
                    "retcode": rc,
                    "order": result.order,
                    "deal": result.deal,
                    "volume": result.volume,
                    "price": result.price,
                }
            if rc == RET_INVALID_FILL:
                fill_idx += 1  # 换下一种填充模式（不消耗重试次数）
                continue
            if rc in TRANSIENT:
                # 瞬时错误：退避后重试（下一轮会刷新价格）
                attempt += 1
                time.sleep(min(0.3 * attempt, 1.5))
                continue
            # 其余视为致命错误，直接返回
            return {"success": False, "symbol": symbol, "retcode": rc, "error": result.comment}

        return {
            "success": False,
            "symbol": symbol,
            "retcode": getattr(last, "retcode", None),
            "error": getattr(last, "comment", "order failed after retries"),
        }

    def close_position(self, pos: dict) -> dict:
        """平掉单个持仓：下反方向 deal，并通过 position=ticket 指定要平的仓位。"""
        self.ensure()
        resolved = pos["symbol"]
        tick = mt5.symbol_info_tick(resolved)
        if tick is None:
            return {"success": False, "error": "no tick", "ticket": pos["ticket"]}
        is_buy = pos["type"] == "BUY"
        close_type = mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY  # 反向平仓
        price = tick.bid if is_buy else tick.ask
        for fill in self.filling_modes(resolved):
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": resolved,
                "volume": float(pos["volume"]),
                "type": close_type,
                "position": pos["ticket"],
                "price": price,
                "deviation": self.slippage,
                "magic": int(pos.get("magic") or self.magic),
                "comment": "close",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": fill,
            }
            result = mt5.order_send(request)
            if result is not None and result.retcode in (RET_DONE, RET_DONE_PARTIAL):
                return {
                    "success": True,
                    "ticket": pos["ticket"],
                    "symbol": pos["symbol"],
                    "action": "CLOSE",
                    "volume": float(pos["volume"]),
                    "position_type": pos["type"],
                    "retcode": result.retcode,
                }
            # 仅在“填充模式不支持”时换下一种，其它错误直接返回
            if result is not None and result.retcode != RET_INVALID_FILL:
                return {
                    "success": False,
                    "ticket": pos["ticket"],
                    "symbol": pos["symbol"],
                    "action": "CLOSE",
                    "volume": float(pos["volume"]),
                    "position_type": pos["type"],
                    "retcode": result.retcode,
                    "error": result.comment,
                }
        return {
            "success": False,
            "ticket": pos["ticket"],
            "symbol": pos["symbol"],
            "action": "CLOSE",
            "error": "close failed",
        }

    def close_ticket(self, ticket: int) -> dict:
        """按订单号平仓。"""
        for p in self.positions():
            if p["ticket"] == ticket:
                return self.close_position(p)
        return {"success": False, "action": "CLOSE", "ticket": ticket, "error": f"position not found: {ticket}"}

    def close_symbol(self, symbol: str) -> dict:
        """平掉某品种的所有持仓（兼容券商后缀）。"""
        base = symbol.upper().replace("/", "")
        results = []
        for p in self.positions():
            if p["symbol"].upper().startswith(base) or base.startswith(p["symbol"].upper()):
                results.append(self.close_position(p))
        ok = all(r.get("success") for r in results) if results else True
        return {
            "success": ok,
            "symbol": symbol.upper(),
            "action": "CLOSE",
            "closed": len(results),
            "results": results,
        }

    def close_all(self) -> dict:
        """平掉账户全部持仓。"""
        results = [self.close_position(p) for p in self.positions()]
        ok = all(r.get("success") for r in results) if results else True
        symbol = results[0]["symbol"] if len(results) == 1 else None
        return {
            "success": ok,
            "symbol": symbol,
            "action": "CLOSE",
            "closed": len(results),
            "results": results,
        }
