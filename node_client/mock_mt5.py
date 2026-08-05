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
        self._realized_by_magic: dict[int, float] = {}  # 按魔术号累计已实现盈亏
        self._exit_deals: list[dict] = []         # 出场成交（供收口原因汇总）
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

    def _record_exit(self, pos: dict, *, reason: int, profit: float) -> None:
        """记下出场成交；reason 对齐 MT5 DEAL_REASON（4=SL / 5=TP / 3=EXPERT…）。"""
        self._exit_deals.append({
            "ticket": int(pos.get("ticket") or 0),
            "magic": int(pos.get("magic") or self.magic),
            "entry": 1,  # DEAL_ENTRY_OUT
            "reason": int(reason),
            "volume": float(pos.get("volume") or 0),
            "price": float(pos.get("price_current") or pos.get("price_open") or 0),
            "profit": float(profit),
            "time": time.time(),
        })

    def close_ticket(self, ticket: int) -> dict:
        pos = next((p for p in self._positions if p["ticket"] == ticket), None)
        if not pos:
            return {"success": False, "action": "CLOSE", "ticket": ticket, "error": f"position not found: {ticket}"}
        symbol = pos["symbol"]
        volume = float(pos["volume"])
        profit = float(pos.get("profit") or 0.0)
        magic = int(pos.get("magic") or self.magic)
        self._record_exit(pos, reason=3, profit=profit)  # EXPERT / 程序平仓
        self._positions = [p for p in self._positions if p["ticket"] != ticket]
        self._realized_by_magic[magic] = self._realized_by_magic.get(magic, 0.0) + profit
        self.balance += profit
        return {
            "success": True,
            "ticket": ticket,
            "symbol": symbol,
            "action": "CLOSE",
            "volume": volume,
            "position_type": pos["type"],
            "closed": 1,
            "profit": profit,
        }

    def positions_by_magic(self, magic: int) -> list[dict]:
        target = int(magic)
        return [dict(p) for p in self._positions if int(p.get("magic") or 0) == target]

    def close_by_magic(self, magic: int) -> dict:
        target = int(magic)
        matched = [p for p in self._positions if int(p.get("magic") or 0) == target]
        profit = sum(float(p.get("profit") or 0.0) for p in matched)
        for pos in matched:
            self._record_exit(pos, reason=3, profit=float(pos.get("profit") or 0.0))
        self._positions = [
            p for p in self._positions if int(p.get("magic") or 0) != target
        ]
        self._realized_by_magic[target] = self._realized_by_magic.get(target, 0.0) + profit
        self.balance += profit
        return {
            "success": True,
            "action": "CLOSE",
            "magic": target,
            "closed": len(matched),
            "profit": round(profit, 2),
        }

    def clear_by_magic_with_reason(self, magic: int, reason: int = 4) -> int:
        """测试用：按 DEAL_REASON 清空持仓（默认 4=止损），模拟终端自动打掉。"""
        target = int(magic)
        matched = [p for p in self._positions if int(p.get("magic") or 0) == target]
        profit = 0.0
        for pos in matched:
            pl = float(pos.get("profit") or 0.0)
            profit += pl
            self._record_exit(pos, reason=reason, profit=pl)
        self._positions = [
            p for p in self._positions if int(p.get("magic") or 0) != target
        ]
        self._realized_by_magic[target] = self._realized_by_magic.get(target, 0.0) + profit
        self.balance += profit
        return len(matched)

    def realized_profit_by_magic(self, magic: int, since_ts: float | None = None) -> float:
        """与真实 MT5Client 同口径：返回该魔术号累计已实现盈亏。"""
        return round(float(self._realized_by_magic.get(int(magic), 0.0)), 2)

    def exit_deals_by_magic(self, magic: int, since_ts: float | None = None) -> list[dict]:
        """与真实 MT5Client 同口径：返回该魔术号出场成交。"""
        del since_ts  # mock 不按时间窗裁剪
        target = int(magic)
        return [dict(d) for d in self._exit_deals if int(d.get("magic") or 0) == target]

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
        keep, closed, profit = [], 0, 0.0
        for p in self._positions:
            if p["symbol"].startswith(base) or base.startswith(p["symbol"]):
                closed += 1
                mag = int(p.get("magic") or self.magic)
                pl = float(p.get("profit") or 0.0)
                profit += pl
                self._realized_by_magic[mag] = self._realized_by_magic.get(mag, 0.0) + pl
            else:
                keep.append(p)
        self._positions = keep
        self.balance += profit
        return {"success": True, "symbol": symbol.upper(), "action": "CLOSE", "closed": closed}

    def close_all(self) -> dict:
        closed = len(self._positions)
        symbol = self._positions[0]["symbol"] if closed == 1 else None
        profit = 0.0
        for p in self._positions:
            mag = int(p.get("magic") or self.magic)
            pl = float(p.get("profit") or 0.0)
            profit += pl
            self._realized_by_magic[mag] = self._realized_by_magic.get(mag, 0.0) + pl
        self.balance += profit
        self._positions = []
        return {"success": True, "symbol": symbol, "action": "CLOSE", "closed": closed}

    def close_positions(self, positions: list[dict]) -> dict:
        tickets = {int(p.get("ticket") or 0) for p in positions or []}
        before = len(self._positions)
        kept = []
        closed_sym = None
        profit = 0.0
        for p in self._positions:
            if int(p.get("ticket") or 0) in tickets:
                closed_sym = p.get("symbol")
                mag = int(p.get("magic") or self.magic)
                pl = float(p.get("profit") or 0.0)
                profit += pl
                self._realized_by_magic[mag] = self._realized_by_magic.get(mag, 0.0) + pl
            else:
                kept.append(p)
        self._positions = kept
        self.balance += profit
        closed = before - len(kept)
        return {
            "success": True,
            "symbol": closed_sym if closed == 1 else None,
            "action": "CLOSE",
            "closed": closed,
        }
