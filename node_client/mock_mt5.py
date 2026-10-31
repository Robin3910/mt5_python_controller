"""MT5 客户端的内存模拟实现，便于无真实 MT5 终端时开发联调。

通过 MT5_MOCK=true 启用。接口与 MT5Client 完全一致（node_client 无需区分）。
开仓即在内存中新增一笔持仓，平仓即移除，账户净值随浮盈联动。

挂单同样是内存态：`place_pending_order` 只登记不成交，之后每次读持仓 / 读挂单时
按 prices_map 的现价判定是否触及挂单价，触及就转成持仓——真实终端也是在后台这样
撮合的，联调时改 prices_map 即可走完「挂上 → 成交 → 收口」的完整链路。
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
        self._pending: list[dict] = []           # 内存挂单（未成交）
        self._realized_by_magic: dict[int, float] = {}  # 按魔术号累计已实现盈亏
        self._exit_deals: list[dict] = []         # 出场成交（供收口原因汇总）
        self._deals: list[dict] = []              # 全部成交流水（开仓 + 出场），供增量采集
        self.prices_map = dict(_DEFAULT_PRICES)
        # 逐笔持仓的对冲账户；改成 netting 可模拟网格被拒的场景
        self.margin_mode = "hedging"
        # 现价触及挂单价时自动成交；置 False 可让测试完全手工驱动成交时点
        self.auto_fill_pending = True
        # 券商要求挂单价离现价的最小点数；0 表示不限制
        self.stops_level = 0
        # 模拟环境没有券商钟面偏移，与 time.time() 同一套 UTC
        self._server_tz_offset = 0

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
            "margin_mode": self.margin_mode,
        }

    def positions(self) -> list[dict]:
        self._settle_pending()
        return [dict(p) for p in self._positions]

    def server_time_offset_sec(self) -> int:
        """模拟终端与 UTC 无偏移。"""
        return int(self._server_tz_offset)

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
        self._record_deal(
            entry="IN",
            reason=0,  # DEAL_REASON_CLIENT
            position_id=tk,
            symbol=symbol.upper(),
            direction=str(action).upper(),
            volume=float(volume),
            price=price,
            profit=0.0,
            magic=int(magic or self.magic),
            comment=comment,
        )
        return {"success": True, "symbol": symbol, "retcode": 10009,
                "order": tk, "deal": tk, "volume": float(volume), "price": price}

    # ----------------------- 挂单 -----------------------
    def _market_price(self, symbol: str, action: str) -> float:
        """挂单校验用的现价：BUY 看卖价、SELL 看买价（与真实 MT5Client 一致）。

        挂单成交侧决定挂单价必须在哪一侧：BUY LIMIT 要挂在卖价下方，
        BUY STOP 要挂在卖价上方。
        """
        sym = str(symbol or "").upper()
        mid = float(self.prices_map.get(sym, 0.0))
        if mid <= 0:
            return 0.0
        quote = self.quotes([sym]).get(sym) or {}
        bid = float(quote.get("bid") or mid)
        ask = float(quote.get("ask") or mid)
        return ask if str(action or "").upper() == "BUY" else bid

    def _pending_price_status(self, symbol, action, price, kind="limit"):
        """与 MT5Client 同一组返回码：None 可挂，否则 side / stops / unavailable。"""
        market = self._market_price(symbol, action)
        if price <= 0:
            return "unavailable", "挂单价需大于 0"
        if market <= 0:
            return "unavailable", "no tick"
        point = self.symbol_point(symbol)
        stops = int(self.stops_level or 0)
        want_below = (action == "BUY") == (kind == "limit")
        if (price < market) != want_below:
            side = "低于" if want_below else "高于"
            return "side", f"{action} {kind} 挂单价 {price} 必须{side}现价 {market}"
        if stops > 0 and point > 0 and abs(price - market) < stops * point:
            return "stops", (
                f"挂单价距现价 {round(abs(price - market) / point)} 点，"
                f"低于券商要求的 {stops} 点"
            )
        return None, None

    def pending_price_block(self, symbol, action, price, kind="limit"):
        code, _message = self._pending_price_status(symbol, action, price, kind)
        return code

    def check_pending_price(self, symbol, action, price, kind="limit"):
        """方向必须对，且不能近于 stops_level。"""
        _code, message = self._pending_price_status(symbol, action, price, kind)
        return message

    def place_pending_order(self, symbol, action, volume, price, sl=None, tp=None,
                            comment="", magic=None, kind="limit", max_retry=3) -> dict:
        kind = str(kind or "limit").strip().lower()
        reject = self.check_pending_price(symbol, action, float(price), kind)
        if reject:
            return {"success": False, "symbol": symbol, "error": reject}
        tk = next(self._tickets)
        self._pending.append(
            {
                "ticket": tk,
                "symbol": symbol.upper(),
                "type": action,
                "pending_kind": kind,
                "volume": float(volume),
                "price_open": float(price),
                "price_current": float(self.prices_map.get(symbol.upper(), 1.0)),
                "sl": float(sl) if sl else 0.0,
                "tp": float(tp) if tp else 0.0,
                "magic": int(magic or self.magic),
                "comment": comment,
                "time": time.time(),
            }
        )
        logger.info(
            "MOCK pending %s %s %s %.2f @ %.5f -> ticket %s",
            action, kind, symbol, volume, price, tk,
        )
        return {"success": True, "pending": True, "symbol": symbol, "action": action,
                "pending_kind": kind, "retcode": 10009, "order": tk,
                "volume": float(volume), "price": float(price)}

    def modify_pending_order(self, ticket, price, sl=None, tp=None, max_retry=3) -> dict:
        """改挂单价和止损止盈。sl / tp 为 None 时保留原值。"""
        self._settle_pending()
        target = int(ticket)
        order = next((o for o in self._pending if int(o["ticket"]) == target), None)
        if order is None:
            return {"success": False, "ticket": target, "error": f"order not found: {target}"}
        reject = self.check_pending_price(
            order["symbol"], order["type"], float(price), order.get("pending_kind") or "limit",
        )
        if reject:
            return {"success": False, "ticket": target, "symbol": order["symbol"], "error": reject}
        order["price_open"] = float(price)
        if sl is not None:
            order["sl"] = float(sl or 0.0)
        if tp is not None:
            order["tp"] = float(tp or 0.0)
        return {
            "success": True, "ticket": target, "symbol": order["symbol"],
            "price": float(price), "sl": float(order.get("sl") or 0.0),
            "tp": float(order.get("tp") or 0.0), "retcode": 10009,
        }

    def _touched(self, order: dict) -> bool:
        """现价是否已触及挂单价。"""
        mid = float(self.prices_map.get(str(order.get("symbol") or "").upper(), 0.0))
        if mid <= 0:
            return False
        price = float(order.get("price_open") or 0.0)
        # BUY LIMIT / SELL STOP 等回落，SELL LIMIT / BUY STOP 等上涨
        want_below = (order.get("type") == "BUY") == (order.get("pending_kind") == "limit")
        return mid <= price if want_below else mid >= price

    def _settle_pending(self) -> int:
        """把已被现价触及的挂单转成持仓，返回成交笔数。"""
        if not self.auto_fill_pending or not self._pending:
            return 0
        filled = [o for o in self._pending if self._touched(o)]
        for order in filled:
            self._fill(order)
        return len(filled)

    def _fill(self, order: dict) -> None:
        """挂单成交：从挂单列表移除，按挂单价建仓（挂单没有滑点）。"""
        self._pending = [o for o in self._pending if o["ticket"] != order["ticket"]]
        price = float(order["price_open"])
        self._positions.append(
            {
                "ticket": order["ticket"],
                "symbol": order["symbol"],
                "type": order["type"],
                "volume": float(order["volume"]),
                "price_open": price,
                "price_current": price,
                "sl": float(order.get("sl") or 0.0),
                "tp": float(order.get("tp") or 0.0),
                "profit": 0.0,
                "magic": int(order.get("magic") or self.magic),
                "comment": order.get("comment", ""),
                "time": time.time(),
            }
        )
        logger.info("MOCK pending filled: ticket %s @ %.5f", order["ticket"], price)
        self._record_deal(
            entry="IN",
            reason=0,  # DEAL_REASON_CLIENT
            position_id=int(order["ticket"]),
            symbol=str(order.get("symbol") or ""),
            direction=str(order.get("type") or "BUY").upper(),
            volume=float(order.get("volume") or 0),
            price=price,
            profit=0.0,
            magic=int(order.get("magic") or self.magic),
            comment=str(order.get("comment") or ""),
        )

    def fill_pending_order(self, ticket: int) -> bool:
        """测试用：不看价格，强制成交某张挂单。"""
        order = next((o for o in self._pending if int(o["ticket"]) == int(ticket)), None)
        if order is None:
            return False
        self._fill(order)
        return True

    def pending_orders(self) -> list[dict]:
        self._settle_pending()
        return [dict(o) for o in self._pending]

    def pending_orders_by_magic(self, magic: int) -> list[dict]:
        target = int(magic)
        return [o for o in self.pending_orders() if int(o.get("magic") or 0) == target]

    def cancel_order(self, ticket: int, max_retry: int = 3) -> dict:
        target = int(ticket)
        before = len(self._pending)
        self._pending = [o for o in self._pending if int(o["ticket"]) != target]
        if len(self._pending) == before:
            return {"success": False, "action": "CANCEL", "ticket": target,
                    "error": f"order not found: {target}"}
        return {"success": True, "action": "CANCEL", "ticket": target, "retcode": 10009}

    def cancel_orders_by_magic(self, magic: int) -> dict:
        results = [
            self.cancel_order(int(o["ticket"])) for o in self.pending_orders_by_magic(magic)
        ]
        return {
            "success": all(r.get("success") for r in results) if results else True,
            "action": "CANCEL",
            "magic": int(magic),
            "cancelled": sum(1 for r in results if r.get("success")),
            "results": results,
        }

    def _record_deal(self, *, entry: str, reason: int, position_id: int,
                     symbol: str, direction: str, volume: float, price: float,
                     profit: float, magic: int, comment: str = "") -> None:
        """记一笔成交到成交流水（与真 MT5 的字段口径一致）。

        deal_id 用自增票号，模拟 MT5 单账户内成交号单调递增的语义 ——
        增量采集的游标正是靠「成交号决定哪笔算新」来去重的。
        """
        tk = next(self._tickets)
        self._deals.append({
            "deal_id": tk,
            "order_id": tk,
            "position_id": int(position_id or 0),
            "symbol": str(symbol or ""),
            "direction": direction,
            "entry_type": entry,
            "reason": int(reason),
            "volume": float(volume or 0),
            "price": float(price or 0),
            "profit": float(profit or 0),
            "swap": 0.0,
            "commission": 0.0,
            "net_profit": float(profit or 0),
            "magic": int(magic or 0),
            "comment": str(comment or "")[:128],
            "time": time.time(),
        })

    def history_deals_since(self, since_ts: float, after_deal_id: int = 0) -> list[dict]:
        """与真 MT5Client 同口径：取增量成交流水。

        mock 不按时间窗裁剪（成交都在本进程内产生），只按成交号做增量过滤。
        """
        floor = int(after_deal_id or 0)
        rows = [dict(d) for d in self._deals if int(d.get("deal_id") or 0) > floor]
        rows.sort(key=lambda r: (r["time"], r["deal_id"]))
        return rows

    def _record_exit(self, pos: dict, *, reason: int, profit: float) -> None:
        """记下出场成交；reason 对齐 MT5 DEAL_REASON（4=SL / 5=TP / 3=EXPERT…）。"""
        self._exit_deals.append({
            "ticket": int(pos.get("ticket") or 0),
            "position_id": int(pos.get("ticket") or 0),
            "magic": int(pos.get("magic") or self.magic),
            "entry": 1,  # DEAL_ENTRY_OUT
            "reason": int(reason),
            "volume": float(pos.get("volume") or 0),
            "price": float(pos.get("price_current") or pos.get("price_open") or 0),
            "profit": float(profit),
            "time": time.time(),
        })
        # 同时记进成交流水：出场方向与持仓方向相反（平多单是 SELL）
        self._record_deal(
            entry="OUT",
            reason=int(reason),
            position_id=int(pos.get("ticket") or 0),
            symbol=str(pos.get("symbol") or ""),
            direction=("SELL" if str(pos.get("type")) == "BUY" else "BUY"),
            volume=float(pos.get("volume") or 0),
            price=float(pos.get("price_current") or pos.get("price_open") or 0),
            profit=float(profit),
            magic=int(pos.get("magic") or self.magic),
            comment=str(pos.get("comment") or ""),
        )

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
        return [p for p in self.positions() if int(p.get("magic") or 0) == target]

    def close_by_magic(self, magic: int) -> dict:
        target = int(magic)
        cancelled = self.cancel_orders_by_magic(target).get("cancelled", 0)
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
            "cancelled": cancelled,
            "profit": round(profit, 2),
        }

    def close_ticket_with_reason(self, ticket: int, reason: int = 5) -> bool:
        """测试用：按 DEAL_REASON 平掉单笔持仓（默认 5=止盈），模拟终端自动兑现。"""
        target = int(ticket)
        pos = next((p for p in self._positions if int(p.get("ticket") or 0) == target), None)
        if pos is None:
            return False
        pl = float(pos.get("profit") or 0.0)
        magic = int(pos.get("magic") or self.magic)
        self._record_exit(pos, reason=reason, profit=pl)
        self._positions = [p for p in self._positions if int(p.get("ticket") or 0) != target]
        self._realized_by_magic[magic] = self._realized_by_magic.get(magic, 0.0) + pl
        self.balance += pl
        return True

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

    def realized_profit_by_position(
        self, magic: int, since_ts: float | None = None,
    ) -> dict[int, float]:
        """与真实 MT5Client 同口径：按持仓票号汇总已实现盈亏。"""
        del since_ts
        target = int(magic)
        out: dict[int, float] = {}
        for d in self._exit_deals:
            if int(d.get("magic") or 0) != target:
                continue
            pos_id = int(d.get("position_id") or 0)
            if pos_id <= 0:
                continue
            pl = (
                float(d.get("profit") or 0)
                + float(d.get("swap") or 0)
                + float(d.get("commission") or 0)
            )
            out[pos_id] = round(out.get(pos_id, 0.0) + pl, 2)
        return out

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
            "stops_level": int(self.stops_level or 0),
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

        振幅取 200 个 point，因此 ATR 与高低点总跨度都稳定等于该值，测试可直接断言。
        """
        return self._synth_bars(symbol, timeframe, count, live=False)

    def recent_bars(self, symbol: str, timeframe: str, count: int) -> list[dict]:
        """合成最近 count 根 K 线，**含**当前未收盘那根（最后一根）。

        与 closed_bars 只差起点：这里最后一根就是正在走的那根，
        供波幅自适应同时拿到「实时根」与「已收盘窗口」。
        """
        return self._synth_bars(symbol, timeframe, count, live=True)

    def current_bar(self, symbol: str, timeframe: str) -> dict:
        bars = self.recent_bars(symbol, timeframe, 1)
        return bars[-1] if bars else {}

    def _synth_bars(
        self, symbol: str, timeframe: str, count: int, *, live: bool,
    ) -> list[dict]:
        """合成 K 线序列（时间升序）；live=True 时最后一根是当前正在走的这根。

        与 MT5 的 copy_rates_from_pos 口径一致：最老在前，最新在后。
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
        if not live:
            return bars
        # 当前这根：时间落在最后（未收盘），振幅沿用同一段
        bars.append({
            "time": now,
            "open": mid,
            "high": mid + span / 2,
            "low": mid - span / 2,
            "close": mid,
        })
        return bars

    def close_symbol(self, symbol: str) -> dict:
        base = symbol.upper().replace("/", "")
        cancels = [
            self.cancel_order(int(o["ticket"]))
            for o in self.pending_orders()
            if str(o["symbol"]).startswith(base) or base.startswith(str(o["symbol"]))
        ]
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
        return {"success": True, "symbol": symbol.upper(), "action": "CLOSE",
                "closed": closed, "cancelled": sum(1 for r in cancels if r.get("success"))}

    def close_all(self) -> dict:
        cancelled = len(self._pending)
        self._pending = []
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
        return {"success": True, "symbol": symbol, "action": "CLOSE",
                "closed": closed, "cancelled": cancelled}

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
