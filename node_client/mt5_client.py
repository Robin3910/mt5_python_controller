"""MT5 交易客户端——操作规则与参考仓库 `mt5_python_connector/mt5_client.py` 保持一致：

- 品种解析：自动尝试券商后缀并 symbol_select 选入行情；
- 填充模式：探测 FOK/IOC/RETURN，遇到 10030(不支持填充模式)自动切换；
- 市价单：TRADE_ACTION_DEAL，带 deviation(滑点)/magic(魔术号)/comment；
- 挂单：TRADE_ACTION_PENDING，价格由调用方给定，撤单走 TRADE_ACTION_REMOVE；
- 平仓：用反方向 deal + position(订单号) 在当前价平掉；
- 重试：对瞬时错误(requote/价格变动等)刷新价格后重试。

「平掉某魔术号 / 某品种 / 全部」这三个收口口径一律连挂单一起撤：只平持仓会把未成交
的挂单留在终端里，等策略任务收口、监控停掉之后它仍可能成交，变成没有止盈止损也没有
保本监控的孤儿仓。
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
RET_INVALID_PRICE = 10015   # 价格非法
RET_INVALID_STOPS = 10016   # 止损/挂单价距现价太近（低于 stops_level）
RET_INVALID_FILL = 10030    # 不支持的填充模式
TRANSIENT = {RET_REQUOTE, RET_PRICE_CHANGED, RET_PRICE_OFF, RET_TIMEOUT}  # 可重试的瞬时错误

# 挂单方向。limit = 等价格回到更有利处成交；stop = 等价格突破后成交。
PENDING_LIMIT = "limit"
PENDING_STOP = "stop"
PENDING_KINDS = (PENDING_LIMIT, PENDING_STOP)

# (方向, 挂单类型) -> MT5 订单类型。与 TIMEFRAMES 同样做导入守卫：非 Windows 上
# MetaTrader5 不可用时留空，下挂单的入口会因此直接返回失败而不是抛 AttributeError。
PENDING_ORDER_TYPES: dict[tuple[str, str], int] = (
    {
        ("BUY", PENDING_LIMIT): mt5.ORDER_TYPE_BUY_LIMIT,
        ("SELL", PENDING_LIMIT): mt5.ORDER_TYPE_SELL_LIMIT,
        ("BUY", PENDING_STOP): mt5.ORDER_TYPE_BUY_STOP,
        ("SELL", PENDING_STOP): mt5.ORDER_TYPE_SELL_STOP,
    }
    if mt5 is not None
    else {}
)

# MT5 订单类型 -> (方向, 挂单类型)，读 orders_get 时把数字还原成可读字段
_PENDING_TYPE_NAMES: dict[int, tuple[str, str]] = {
    v: k for k, v in PENDING_ORDER_TYPES.items()
}

# 策略档位可选的 K 线周期 -> MT5 常量。模块导入时 MetaTrader5 可能不可用（非 Windows），
# 那时留空字典，读 K 线的入口会因此直接返回空列表。
TIMEFRAMES: dict[str, int] = (
    {
        "M1": mt5.TIMEFRAME_M1,
        "M5": mt5.TIMEFRAME_M5,
        "M15": mt5.TIMEFRAME_M15,
        "M30": mt5.TIMEFRAME_M30,
        "H1": mt5.TIMEFRAME_H1,
        "H4": mt5.TIMEFRAME_H4,
        "D1": mt5.TIMEFRAME_D1,
        "W1": mt5.TIMEFRAME_W1,
        "MN": mt5.TIMEFRAME_MN1,
    }
    if mt5 is not None
    else {}
)


# 账户保证金模式。网格「一格一笔持仓」的模型只在对冲账户成立，净持仓账户会把
# 同品种仓位合并成一笔，格位与 comment 全部对不上。
ACCOUNT_MARGIN_MODES = {0: "netting", 1: "exchange", 2: "hedging"}


class MT5Error(RuntimeError):
    pass


def peek_logged_in_account(path: str) -> dict | None:
    """尝试附着终端并读取当前已登录账户；未登录返回 None。用完会 shutdown。"""
    if mt5 is None:
        raise MT5Error("MetaTrader5 package not available on this host")
    if not path:
        raise MT5Error("未指定 MT5 终端路径")
    if not mt5.initialize(path=path):
        raise MT5Error(f"无法初始化 MT5 终端：{mt5.last_error()}")
    try:
        ai = mt5.account_info()
        if ai is None or not getattr(ai, "login", None):
            return None
        return {
            "login": int(ai.login),
            "server": str(getattr(ai, "server", "") or ""),
        }
    finally:
        mt5.shutdown()


def _closed_count(results: list[dict]) -> int:
    """实际平掉的笔数：失败的不计入，避免把尝试笔数当成已平笔数上报。"""
    return sum(1 for r in results if r.get("success"))


def _cancelled_count(results: list[dict]) -> int:
    """实际撤掉的挂单笔数，口径同 _closed_count。"""
    return sum(1 for r in results if r.get("success"))


class MT5Client:
    def __init__(
        self,
        login: int,
        password: str,
        server: str,
        path: str = "",
        slippage: int = 20,
        magic: int = 20240615,
        *,
        reuse_terminal_session: bool = False,
    ) -> None:
        self.login = int(login)
        self.password = password
        self.server = server
        self.path = path            # terminal64.exe / terminal.exe 完整路径（真实模式必填）
        self.slippage = slippage    # 默认滑点（deviation）
        self.magic = magic          # 默认魔术号
        self.reuse_terminal_session = reuse_terminal_session
        self.connected = False

    # ----------------------- 连接 -----------------------
    def connect(self) -> bool:
        """初始化并登录 MT5 终端；任一步失败返回 False。"""
        if mt5 is None:
            raise MT5Error("MetaTrader5 package not available on this host")
        if not self.path:
            raise MT5Error(
                "未指定 MT5 终端路径；请将本程序放入 MetaTrader 5 安装目录后启动"
            )
        if not mt5.initialize(path=self.path):
            logger.error("mt5.initialize failed: %s", mt5.last_error())
            return False
        if self.reuse_terminal_session:
            ai = mt5.account_info()
            if ai is None or not getattr(ai, "login", None):
                logger.error("终端未登录，无法复用会话")
                mt5.shutdown()
                return False
            got = int(ai.login)
            if self.login and got != self.login:
                logger.error(
                    "终端登录号不符：当前=%s 期望=%s", got, self.login,
                )
                mt5.shutdown()
                return False
            self.login = got
            self.server = str(getattr(ai, "server", "") or self.server)
        elif self.login:
            if not mt5.login(self.login, password=self.password, server=self.server):
                logger.error(
                    "mt5.login failed for %s@%s: %s",
                    self.login, self.server, mt5.last_error(),
                )
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
            "margin_mode": ACCOUNT_MARGIN_MODES.get(
                getattr(ai, "margin_mode", None), ""
            ),
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

    def pending_orders(self) -> list[dict]:
        """返回当前所有未成交挂单（字段口径对齐 positions，方便上层同构处理）。

        `volume` 取 volume_current（剩余待成交量）而不是 volume_initial：部分成交
        后剩下的才是还挂在盘上的量。
        """
        self.ensure()
        out = []
        for o in mt5.orders_get() or []:
            otype = int(getattr(o, "type", -1))
            direction, kind = _PENDING_TYPE_NAMES.get(otype, ("", ""))
            if not direction:
                continue  # 市价单在 orders_get 里只是瞬时态，不纳入挂单口径
            out.append(
                {
                    "ticket": o.ticket,
                    "symbol": o.symbol,
                    "type": direction,
                    "pending_kind": kind,
                    "volume": float(
                        getattr(o, "volume_current", 0) or getattr(o, "volume_initial", 0) or 0
                    ),
                    "price_open": float(getattr(o, "price_open", 0) or 0),
                    "price_current": float(getattr(o, "price_current", 0) or 0),
                    "sl": float(getattr(o, "sl", 0) or 0),
                    "tp": float(getattr(o, "tp", 0) or 0),
                    "magic": int(getattr(o, "magic", 0) or 0),
                    "comment": getattr(o, "comment", "") or "",
                    "time": getattr(o, "time_setup", 0),
                }
            )
        return out

    def pending_orders_by_magic(self, magic: int) -> list[dict]:
        """按魔术号筛选挂单：策略任务据此判断自己名下还有没有在途的单。"""
        target = int(magic)
        return [o for o in self.pending_orders() if int(o.get("magic") or 0) == target]

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

    def filling_modes(self, symbol: str, *, pending: bool = False) -> list[int]:
        """返回该品种可尝试的填充模式顺序（优先用品种支持的，再补全兜底项）。

        挂单要单独排序：挂单在成交前一直留在盘上，语义上就是 RETURN（未成交部分
        保留），多数券商对 TRADE_ACTION_PENDING 只接受它，而 symbol_info.filling_mode
        这个位掩码描述的是市价成交能力，照搬过来会让首选项必然吃到 10030。
        """
        if pending:
            return [
                mt5.ORDER_FILLING_RETURN,
                mt5.ORDER_FILLING_IOC,
                mt5.ORDER_FILLING_FOK,
            ]
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

    def check_pending_price(self, symbol: str, action: str, price: float,
                            kind: str = PENDING_LIMIT) -> Optional[str]:
        """校验挂单价是否落在合法一侧且离现价足够远；合法返回 None，否则返回原因。

        单独抽出来是为了让上层能在真正下单前批量预检一整组阶梯价：挂单被券商拒掉
        （10015 价格非法 / 10016 距离太近）不像市价单那样重试就能好，只能改价，
        所以宁可在下第一笔之前就发现整组都不可行。
        """
        resolved = self.resolve_symbol(symbol)
        if not resolved:
            return f"symbol not found: {symbol}"
        if price <= 0:
            return "挂单价需大于 0"
        tick = mt5.symbol_info_tick(resolved)
        if tick is None:
            return "no tick"
        info = mt5.symbol_info(resolved)
        point = float(getattr(info, "point", 0.0) or 0.0) if info else 0.0
        digits = int(getattr(info, "digits", 5) or 5) if info else 5
        stops = int(getattr(info, "trade_stops_level", 0) or 0) if info else 0
        # 挂单成交侧：BUY 用 ask、SELL 用 bid，与该方向真正的成交价口径一致
        market = float(tick.ask if action == "BUY" else tick.bid)
        if market <= 0:
            return "no tick"
        below = price < market
        # BUY LIMIT 挂在现价下方等回落，SELL LIMIT 挂在上方等反弹；STOP 单方向相反
        want_below = (action == "BUY") == (kind == PENDING_LIMIT)
        if below != want_below:
            side = "低于" if want_below else "高于"
            return (
                f"{action} {kind} 挂单价 {round(price, digits)} 必须{side}现价 "
                f"{round(market, digits)}"
            )
        if stops > 0 and point > 0 and abs(price - market) < stops * point:
            return (
                f"挂单价距现价 {round(abs(price - market) / point)} 点，"
                f"低于券商要求的 {stops} 点"
            )
        return None

    def place_pending_order(self, symbol: str, action: str, volume: float, price: float,
                            sl: Optional[float] = None, tp: Optional[float] = None,
                            comment: str = "", magic: Optional[int] = None,
                            kind: str = PENDING_LIMIT, max_retry: int = 3) -> dict:
        """挂限价 / 止损单。成功返回 pending=True，此时只是挂上了，并未成交。

        与市价单的关键差别：价格由调用方给定，不刷新、不重取，因此瞬时错误重试也
        不会改价；请求里没有 deviation（挂单按挂单价成交，没有滑点容忍的概念）。
        """
        self.ensure()
        resolved = self.resolve_symbol(symbol)
        if not resolved:
            return {"success": False, "error": f"symbol not found: {symbol}"}
        kind = str(kind or PENDING_LIMIT).strip().lower()
        order_type = PENDING_ORDER_TYPES.get((action, kind))
        if order_type is None:
            return {"success": False, "symbol": symbol,
                    "error": f"unsupported pending order: {action} {kind}"}
        reject = self.check_pending_price(symbol, action, float(price), kind)
        if reject:
            return {"success": False, "symbol": symbol, "error": reject}

        fillings = self.filling_modes(resolved, pending=True)
        fill_idx = 0
        attempt = 0
        last = None

        while attempt <= max_retry and fill_idx < len(fillings):
            request = {
                "action": mt5.TRADE_ACTION_PENDING,
                "symbol": resolved,
                "volume": float(volume),
                "type": order_type,
                "price": float(price),
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
                logger.error("pending order_send returned None: %s", mt5.last_error())
                attempt += 1
                continue

            rc = result.retcode
            if rc in (RET_DONE, RET_DONE_PARTIAL):
                return {
                    "success": True,
                    "pending": True,
                    "symbol": symbol,
                    "action": action,
                    "pending_kind": kind,
                    "retcode": rc,
                    "order": result.order,
                    "volume": float(volume),
                    "price": float(price),
                }
            if rc == RET_INVALID_FILL:
                fill_idx += 1
                continue
            if rc in TRANSIENT:
                attempt += 1
                time.sleep(min(0.3 * attempt, 1.5))
                continue
            return {"success": False, "symbol": symbol, "retcode": rc, "error": result.comment}

        return {
            "success": False,
            "symbol": symbol,
            "retcode": getattr(last, "retcode", None),
            "error": getattr(last, "comment", "pending order failed after retries"),
        }

    def cancel_order(self, ticket: int, max_retry: int = 3) -> dict:
        """撤掉一张挂单（TRADE_ACTION_REMOVE）。

        撤单失败留下的是无人管的在途单，和平仓失败一样危险，所以同样对瞬时错误重试。
        """
        self.ensure()
        target = int(ticket)
        attempt = 0
        last = None
        while attempt <= max_retry:
            result = mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": target})
            last = result
            if result is None:
                logger.error("cancel order_send returned None: %s", mt5.last_error())
                attempt += 1
                continue
            rc = result.retcode
            if rc in (RET_DONE, RET_DONE_PARTIAL):
                return {"success": True, "action": "CANCEL", "ticket": target, "retcode": rc}
            if rc in TRANSIENT:
                attempt += 1
                time.sleep(min(0.3 * attempt, 1.5))
                continue
            return {"success": False, "action": "CANCEL", "ticket": target,
                    "retcode": rc, "error": result.comment}
        return {
            "success": False,
            "action": "CANCEL",
            "ticket": target,
            "retcode": getattr(last, "retcode", None),
            "error": getattr(last, "comment", "cancel failed after retries"),
        }

    def cancel_orders_by_magic(self, magic: int) -> dict:
        """撤掉某魔术号名下的全部挂单（只动本任务的单）。"""
        results = [
            self.cancel_order(int(o["ticket"])) for o in self.pending_orders_by_magic(magic)
        ]
        return {
            "success": all(r.get("success") for r in results) if results else True,
            "action": "CANCEL",
            "magic": int(magic),
            "cancelled": _cancelled_count(results),
            "results": results,
        }

    def close_position(self, pos: dict, max_retry: int = 3) -> dict:
        """平掉单个持仓：下反方向 deal，并通过 position=ticket 指定要平的仓位。

        与开仓同样处理填充模式切换与瞬时错误重试：平仓失败留下的是无人管的持仓，
        比开仓失败更危险，不能一遇到 requote 就放弃。
        """
        self.ensure()
        resolved = pos["symbol"]
        is_buy = pos["type"] == "BUY"
        close_type = mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY  # 反向平仓
        fillings = self.filling_modes(resolved)
        fill_idx = 0     # 当前尝试的填充模式下标
        attempt = 0      # 瞬时错误重试计数
        last = None

        def _fail(error: str, retcode: Optional[int] = None) -> dict:
            out = {
                "success": False,
                "ticket": pos["ticket"],
                "symbol": pos["symbol"],
                "action": "CLOSE",
                "volume": float(pos["volume"]),
                "position_type": pos["type"],
                "error": error,
            }
            if retcode is not None:
                out["retcode"] = retcode
            return out

        while attempt <= max_retry and fill_idx < len(fillings):
            # 每次都重新取价（重试时价格可能已变）
            tick = mt5.symbol_info_tick(resolved)
            if tick is None:
                return _fail("no tick")
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": resolved,
                "volume": float(pos["volume"]),
                "type": close_type,
                "position": pos["ticket"],
                "price": tick.bid if is_buy else tick.ask,
                "deviation": self.slippage,
                "magic": int(pos.get("magic") or self.magic),
                "comment": "close",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": fillings[fill_idx],
            }
            result = mt5.order_send(request)
            last = result
            if result is None:
                logger.error("close order_send returned None: %s", mt5.last_error())
                attempt += 1
                continue

            rc = result.retcode
            if rc in (RET_DONE, RET_DONE_PARTIAL):
                return {
                    "success": True,
                    "ticket": pos["ticket"],
                    "symbol": pos["symbol"],
                    "action": "CLOSE",
                    "volume": float(pos["volume"]),
                    "position_type": pos["type"],
                    "retcode": rc,
                }
            if rc == RET_INVALID_FILL:
                fill_idx += 1  # 换下一种填充模式（不消耗重试次数）
                continue
            if rc in TRANSIENT:
                attempt += 1
                time.sleep(min(0.3 * attempt, 1.5))
                continue
            return _fail(result.comment, rc)

        return _fail(
            getattr(last, "comment", "close failed after retries"),
            getattr(last, "retcode", None),
        )

    def close_ticket(self, ticket: int) -> dict:
        """按订单号平仓。"""
        for p in self.positions():
            if p["ticket"] == ticket:
                return self.close_position(p)
        return {"success": False, "action": "CLOSE", "ticket": ticket, "error": f"position not found: {ticket}"}

    def positions_by_magic(self, magic: int) -> list[dict]:
        """按魔术号筛选持仓：策略托管任务据此判断自己是否已全平。"""
        target = int(magic)
        return [p for p in self.positions() if int(p.get("magic") or 0) == target]

    def close_by_magic(self, magic: int) -> dict:
        """平掉某魔术号的全部持仓并撤掉其挂单（只动本任务的单）。

        先撤单再平仓：反过来的话，平仓到撤单之间价格若正好触及挂单价，会当场成交
        出一笔新持仓，收口完成时账上反而又有仓了。
        """
        cancels = [
            self.cancel_order(int(o["ticket"])) for o in self.pending_orders_by_magic(magic)
        ]
        results = [self.close_position(p) for p in self.positions_by_magic(magic)]
        ok = all(r.get("success") for r in results + cancels) if results or cancels else True
        return {
            "success": ok,
            "action": "CLOSE",
            "magic": int(magic),
            "closed": _closed_count(results),
            "cancelled": _cancelled_count(cancels),
            "results": results,
            "cancel_results": cancels,
        }

    def _history_deals(self, since_ts: float | None = None):
        """取时间窗内的成交历史；失败或空返回空元组。"""
        self.ensure()
        from datetime import datetime, timedelta

        end = datetime.now()
        if since_ts:
            start = datetime.fromtimestamp(float(since_ts)) - timedelta(minutes=1)
        else:
            start = end - timedelta(days=7)
        return mt5.history_deals_get(start, end) or ()

    def realized_profit_by_magic(self, magic: int, since_ts: float | None = None) -> float:
        """汇总某魔术号在时间窗内的已实现盈亏（成交 profit + swap + commission）。

        用于策略任务收口时上报 realized_profit；时间窗默认最近 7 天。
        """
        deals = self._history_deals(since_ts)
        target = int(magic)
        total = 0.0
        for d in deals:
            if int(getattr(d, "magic", 0) or 0) != target:
                continue
            total += float(getattr(d, "profit", 0) or 0)
            total += float(getattr(d, "swap", 0) or 0)
            total += float(getattr(d, "commission", 0) or 0)
        return round(total, 2)

    def exit_deals_by_magic(self, magic: int, since_ts: float | None = None) -> list[dict]:
        """某魔术号在时间窗内的出场成交，供收口时区分止损 / 止盈 / 人工等。

        每项含 entry / reason / profit / volume / price / ticket；只返回出场类 entry。
        """
        # DEAL_ENTRY_*：包不可用时回退到文档常量
        entry_out = getattr(mt5, "DEAL_ENTRY_OUT", 1) if mt5 else 1
        entry_inout = getattr(mt5, "DEAL_ENTRY_INOUT", 2) if mt5 else 2
        entry_out_by = getattr(mt5, "DEAL_ENTRY_OUT_BY", 3) if mt5 else 3
        out_entries = {int(entry_out), int(entry_inout), int(entry_out_by)}

        deals = self._history_deals(since_ts)
        target = int(magic)
        out: list[dict] = []
        for d in deals:
            if int(getattr(d, "magic", 0) or 0) != target:
                continue
            entry = int(getattr(d, "entry", -1) or -1)
            if entry not in out_entries:
                continue
            out.append({
                "ticket": int(getattr(d, "ticket", 0) or 0),
                "entry": entry,
                "reason": int(getattr(d, "reason", -1) if getattr(d, "reason", None) is not None else -1),
                "volume": float(getattr(d, "volume", 0) or 0),
                "price": float(getattr(d, "price", 0) or 0),
                "profit": float(getattr(d, "profit", 0) or 0),
            })
        return out

    def symbol_point(self, symbol: str) -> float:
        """品种最小价格变动单位；解析不到时返回 0（调用方据此跳过判定）。"""
        self.ensure()
        resolved = self.resolve_symbol(symbol)
        if not resolved:
            return 0.0
        info = mt5.symbol_info(resolved)
        return float(getattr(info, "point", 0.0) or 0.0) if info else 0.0

    def symbol_spec(self, symbol: str) -> dict:
        """品种的报价与手数规格，供以损定量反推手数。

        trade_tick_value 是「一手波动一个 tick 的账户货币价值」，配合 trade_tick_size
        就能把价格距离折算成金额，这是唯一能跨品种通用的换算口径（外汇、金属、指数、
        加密的合约规格与计价货币各不相同，用 contract_size 自己算需要汇率）。
        解析不到品种时返回空字典，调用方据此放弃计算。
        """
        self.ensure()
        resolved = self.resolve_symbol(symbol)
        if not resolved:
            return {}
        info = mt5.symbol_info(resolved)
        if info is None:
            return {}
        return {
            "symbol": resolved,
            "point": float(getattr(info, "point", 0.0) or 0.0),
            "digits": int(getattr(info, "digits", 5) or 5),
            "tick_size": float(getattr(info, "trade_tick_size", 0.0) or 0.0),
            "tick_value": float(getattr(info, "trade_tick_value", 0.0) or 0.0),
            "volume_min": float(getattr(info, "volume_min", 0.0) or 0.0),
            "volume_step": float(getattr(info, "volume_step", 0.0) or 0.0),
            "volume_max": float(getattr(info, "volume_max", 0.0) or 0.0),
            # 挂单价与止损价距现价的最小点数，0 = 券商不限制
            "stops_level": int(getattr(info, "trade_stops_level", 0) or 0),
        }

    def modify_position_sl(self, ticket: int, sl: float,
                           tp: Optional[float] = None) -> dict:
        """改单：只动止损止盈，不动手数（TRADE_ACTION_SLTP）。

        tp 传 None 表示沿用持仓上的现值；MT5 的 SLTP 请求会用请求里的值整体覆盖，
        所以必须把当前止盈一起带上，否则会把已设的止盈抹掉。
        """
        self.ensure()
        target = int(ticket)
        pos = next((p for p in self.positions() if int(p.get("ticket") or 0) == target), None)
        if pos is None:
            return {"success": False, "ticket": target, "error": f"position not found: {target}"}
        resolved = pos["symbol"]
        take_profit = float(tp) if tp is not None else float(pos.get("tp") or 0.0)
        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": resolved,
            "position": target,
            "sl": float(sl),
            "tp": take_profit,
            "magic": int(pos.get("magic") or self.magic),
        }
        result = mt5.order_send(request)
        if result is None:
            return {"success": False, "ticket": target, "error": str(mt5.last_error())}
        if result.retcode in (RET_DONE, RET_DONE_PARTIAL):
            return {
                "success": True,
                "ticket": target,
                "symbol": pos["symbol"],
                "sl": float(sl),
                "tp": take_profit,
                "retcode": result.retcode,
            }
        return {
            "success": False,
            "ticket": target,
            "symbol": pos["symbol"],
            "retcode": result.retcode,
            "error": result.comment,
        }

    def modify_sl_by_magic(self, magic: int, sl: float) -> dict:
        """把某魔术号下全部持仓的止损改到同一价位（保本触发用）。"""
        results = [
            self.modify_position_sl(int(p["ticket"]), sl)
            for p in self.positions_by_magic(magic)
        ]
        ok = all(r.get("success") for r in results) if results else False
        return {
            "success": ok,
            "magic": int(magic),
            "sl": float(sl),
            "modified": sum(1 for r in results if r.get("success")),
            "results": results,
        }

    def closed_bars(self, symbol: str, timeframe: str, count: int) -> list[dict]:
        """取最近 count 根**已收盘** K 线，按时间升序返回。

        从 index 1 起取，跳过 index 0 的当前未收盘 K 线——ATR / 波幅这类统计要的
        是稳定值，掺进走势未定的当前 K 线会让阈值在同一根 K 线内来回跳。
        取不到时返回空列表，由调用方决定跳过判定。
        """
        self.ensure()
        tf = TIMEFRAMES.get(str(timeframe or "").strip().upper())
        if tf is None or count <= 0:
            return []
        resolved = self.resolve_symbol(symbol)
        if not resolved:
            return []
        rates = mt5.copy_rates_from_pos(resolved, tf, 1, int(count))
        if rates is None or len(rates) == 0:
            logger.debug("no bars for %s %s: %s", resolved, timeframe, mt5.last_error())
            return []
        return [
            {
                "time": float(r["time"]),
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
            }
            for r in rates
        ]

    def close_symbol(self, symbol: str) -> dict:
        """平掉某品种的所有持仓并撤掉其挂单（兼容券商后缀）。"""
        base = symbol.upper().replace("/", "")

        def _match(name: str) -> bool:
            name = name.upper()
            return name.startswith(base) or base.startswith(name)

        cancels = [
            self.cancel_order(int(o["ticket"]))
            for o in self.pending_orders() if _match(str(o.get("symbol") or ""))
        ]
        results = [p for p in self.positions() if _match(str(p.get("symbol") or ""))]
        results = [self.close_position(p) for p in results]
        ok = all(r.get("success") for r in results + cancels) if results or cancels else True
        return {
            "success": ok,
            "symbol": symbol.upper(),
            "action": "CLOSE",
            "closed": _closed_count(results),
            "cancelled": _cancelled_count(cancels),
            "results": results,
            "cancel_results": cancels,
        }

    def close_positions(self, positions: list[dict]) -> dict:
        """平掉给定持仓列表。"""
        results = [self.close_position(p) for p in positions or []]
        ok = all(r.get("success") for r in results) if results else True
        symbol = None
        if results:
            syms = {str(r.get("symbol") or "") for r in results if r.get("symbol")}
            if len(syms) == 1:
                symbol = next(iter(syms))
        return {
            "success": ok,
            "symbol": symbol,
            "action": "CLOSE",
            "closed": _closed_count(results),
            "results": results,
        }

    def close_all(self) -> dict:
        """平掉账户全部持仓并撤掉全部挂单。

        这是账户级风控清仓的落点，留着挂单等于清完仓又埋了一颗重新开仓的雷。
        """
        cancels = [self.cancel_order(int(o["ticket"])) for o in self.pending_orders()]
        results = [self.close_position(p) for p in self.positions()]
        ok = all(r.get("success") for r in results + cancels) if results or cancels else True
        symbol = results[0]["symbol"] if len(results) == 1 else None
        return {
            "success": ok,
            "symbol": symbol,
            "action": "CLOSE",
            "closed": _closed_count(results),
            "cancelled": _cancelled_count(cancels),
            "results": results,
            "cancel_results": cancels,
        }
