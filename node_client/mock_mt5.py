"""MT5 客户端的内存模拟实现，便于无真实 MT5 终端时开发联调。

通过 MT5_MOCK=true 启用。接口与 MT5Client 完全一致（node_client 无需区分）。
开仓即在内存中新增一笔持仓，平仓即移除，账户净值随浮盈联动。
"""
import itertools
import logging
import time

logger = logging.getLogger("node.mock")

# 模拟报价（用于持仓建仓价与观察列表）
_DEFAULT_PRICES = {
    "XAUUSD": 2330.0,
    "EURUSD": 1.0742,
    "GBPUSD": 1.2700,
    "USDJPY": 157.20,
    "US30": 39000.0,
    "US100": 19000.0,
}

# 合成 K 线的周期秒数，仅用于让模拟数据的时间轴看起来合理
_TIMEFRAME_SECONDS = {
    "M1": 60, "M5": 300, "M15": 900, "M30": 1800,
    "H1": 3600, "H4": 14400, "D1": 86400, "W1": 604800, "MN": 2592000,
}


class MockMT5Client:
    def __init__(self, login=0, password="", server="", path="", slippage=20, magic=20240615):
        self.login = int(login) or 90000001
        self.server = server or "Mock-Demo"
        self.magic = magic
        self.balance = 10000.0
        self.connected = False
        self._tickets = itertools.count(1000)   # 自增订单号
        self._positions: list[dict] = []         # 内存持仓
        self.prices_map = dict(_DEFAULT_PRICES)

    def connect(self) -> bool:
        self.connected = True
        logger.info("MOCK MT5 connected (login=%s server=%s)", self.login, self.server)
        return True

    def disconnect(self) -> None:
        self.connected = False

    def account_info(self) -> dict:
        # 净值 = 余额 + 浮动盈亏（这里浮盈恒为 0，简化处理）
        floating = sum(p["profit"] for p in self._positions)
        return {
            "login": self.login,
            "server": self.server,
            "balance": self.balance,
            "equity": self.balance + floating,
            "margin": sum(p["volume"] * 100 for p in self._positions),
            "free_margin": self.balance + floating,
            "leverage": 100,
            "currency": "USD",
        }

    def positions(self) -> list[dict]:
        return [dict(p) for p in self._positions]

    def quotes(self, symbols: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for s in symbols:
            sym = s.upper()
            mid = float(self.prices_map.get(sym, 1.0))
            spread = mid * 0.0001 if mid < 100 else mid * 0.00005
            bid = round(mid - spread / 2, 6)
            ask = round(mid + spread / 2, 6)
            out[s] = {"bid": bid, "ask": ask, "mid": mid, "change": 0.12}
        return out

    def prices(self, symbols: list[str]) -> dict[str, float]:
        return {sym: q["mid"] for sym, q in self.quotes(symbols).items()}

    def resolve_symbol(self, symbol: str):
        return symbol.upper()

    def place_market_order(self, symbol, action, volume, sl=None, tp=None,
                           comment="", magic=None, max_retry=3) -> dict:
        # 模拟“立即成交”，新增一笔持仓
        tk = next(self._tickets)
        price = self.prices_map.get(symbol.upper(), 1.0)
        self._positions.append(
            {
                "ticket": tk,
                "symbol": symbol.upper(),
                "type": action,
                "volume": float(volume),
                "price_open": price,
                "price_current": price,
                "sl": float(sl) if sl else 0.0,
                "tp": float(tp) if tp else 0.0,
                "profit": 0.0,
                "magic": int(magic or self.magic),
                "comment": comment,
                "time": time.time(),
            }
        )
        logger.info("MOCK open %s %s %.2f @ %.5f -> ticket %s", action, symbol, volume, price, tk)
        return {"success": True, "symbol": symbol, "retcode": 10009,
                "order": tk, "deal": tk, "volume": float(volume), "price": price}

    def close_ticket(self, ticket: int) -> dict:
        pos = next((p for p in self._positions if p["ticket"] == ticket), None)
        if not pos:
            return {"success": False, "action": "CLOSE", "ticket": ticket, "error": f"position not found: {ticket}"}
        symbol = pos["symbol"]
        volume = float(pos["volume"])
        self._positions = [p for p in self._positions if p["ticket"] != ticket]
        return {
            "success": True,
            "ticket": ticket,
            "symbol": symbol,
            "action": "CLOSE",
            "volume": volume,
            "position_type": pos["type"],
            "closed": 1,
        }

    def positions_by_magic(self, magic: int) -> list[dict]:
        target = int(magic)
        return [dict(p) for p in self._positions if int(p.get("magic") or 0) == target]

    def close_by_magic(self, magic: int) -> dict:
        target = int(magic)
        closed = sum(1 for p in self._positions if int(p.get("magic") or 0) == target)
        self._positions = [
            p for p in self._positions if int(p.get("magic") or 0) != target
        ]
        return {"success": True, "action": "CLOSE", "magic": target, "closed": closed}

    def symbol_point(self, symbol: str) -> float:
        mid = float(self.prices_map.get(symbol.upper(), 1.0))
        # 与常见券商一致：五位报价品种 0.00001，金/指数类 0.01
        return 0.01 if mid >= 100 else 0.00001

    def symbol_spec(self, symbol: str) -> dict:
        """模拟品种规格：tick_size = point，tick_value 取一手一 point 的常见量级。"""
        point = self.symbol_point(symbol)
        mid = float(self.prices_map.get(symbol.upper(), 1.0))
        return {
            "symbol": symbol.upper(),
            "point": point,
            "digits": 2 if mid >= 100 else 5,
            "tick_size": point,
            "tick_value": 1.0 if mid >= 100 else 1.0,
            "volume_min": 0.01,
            "volume_step": 0.01,
            "volume_max": 100.0,
        }

    def modify_position_sl(self, ticket: int, sl: float, tp=None) -> dict:
        target = int(ticket)
        pos = next((p for p in self._positions if int(p["ticket"]) == target), None)
        if not pos:
            return {"success": False, "ticket": target, "error": f"position not found: {target}"}
        pos["sl"] = float(sl)
        if tp is not None:
            pos["tp"] = float(tp)
        return {
            "success": True,
            "ticket": target,
            "symbol": pos["symbol"],
            "sl": pos["sl"],
            "tp": pos["tp"],
        }

    def modify_sl_by_magic(self, magic: int, sl: float) -> dict:
        results = [
            self.modify_position_sl(int(p["ticket"]), sl)
            for p in self.positions_by_magic(magic)
        ]
        return {
            "success": all(r.get("success") for r in results) if results else False,
            "magic": int(magic),
            "sl": float(sl),
            "modified": sum(1 for r in results if r.get("success")),
            "results": results,
        }

    def closed_bars(self, symbol: str, timeframe: str, count: int) -> list[dict]:
        """合成一段已收盘 K 线：围绕当前中间价做固定振幅，便于联调 ATR / 波幅。

        振幅取 200 个 point，因此 ATR 与最大波幅都稳定等于该值，测试可直接断言。
        """
        tf = str(timeframe or "").strip().upper()
        step = _TIMEFRAME_SECONDS.get(tf)
        if step is None or count <= 0:
            return []
        mid = float(self.prices_map.get(symbol.upper(), 1.0))
        span = self.symbol_point(symbol) * 200
        now = time.time()
        bars = []
        for i in range(int(count), 0, -1):
            bars.append({
                "time": now - i * step,
                "open": mid,
                "high": mid + span / 2,
                "low": mid - span / 2,
                "close": mid,
            })
        return bars

    def close_symbol(self, symbol: str) -> dict:
        base = symbol.upper().replace("/", "")
        keep, closed = [], 0
        for p in self._positions:
            if p["symbol"].startswith(base) or base.startswith(p["symbol"]):
                closed += 1
            else:
                keep.append(p)
        self._positions = keep
        return {"success": True, "symbol": symbol.upper(), "action": "CLOSE", "closed": closed}

    def close_all(self) -> dict:
        closed = len(self._positions)
        symbol = self._positions[0]["symbol"] if closed == 1 else None
        self._positions = []
        return {"success": True, "symbol": symbol, "action": "CLOSE", "closed": closed}

    def close_positions(self, positions: list[dict]) -> dict:
        tickets = {int(p.get("ticket") or 0) for p in positions or []}
        before = len(self._positions)
        kept = []
        closed_sym = None
        for p in self._positions:
            if int(p.get("ticket") or 0) in tickets:
                closed_sym = p.get("symbol")
            else:
                kept.append(p)
        self._positions = kept
        closed = before - len(kept)
        return {
            "success": True,
            "symbol": closed_sym if closed == 1 else None,
            "action": "CLOSE",
            "closed": closed,
        }
