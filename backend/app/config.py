"""交易常量——与参考仓库 `mt5_python_connector` 保持同源。

本模块刻意不依赖任何第三方库（仅用标准库），这样信号解析器等纯逻辑模块就能在
不引入 Web / 配置栈的情况下被单独导入和测试。
"""
import os


class Config:
    # —— 交易默认值（对齐参考仓库 config.py）——
    DEFAULT_LOT = float(os.getenv("DEFAULT_LOT", "0.1"))            # 默认手数
    DEFAULT_SLIPPAGE = int(os.getenv("DEFAULT_SLIPPAGE", "10"))      # 默认滑点（点）
    DEFAULT_MAGIC_NUMBER = int(os.getenv("DEFAULT_MAGIC_NUMBER", "20240615"))  # 订单魔术号

    # —— 风控 ——
    MAX_LOT_SIZE = float(os.getenv("MAX_LOT_SIZE", "1.0"))          # 单笔最大手数
    MAX_POSITIONS = int(os.getenv("MAX_POSITIONS", "5"))            # 最大持仓数（参考仓库未强制，保留以备扩展）

    # —— 品种映射（TradingView -> MT5），对齐参考仓库 ——
    SYMBOLS = {
        # 外汇
        "EURUSD": "EURUSD",
        "GBPUSD": "GBPUSD",
        "USDJPY": "USDJPY",
        "USDCHF": "USDCHF",
        "AUDUSD": "AUDUSD",
        "USDCAD": "USDCAD",
        "NZDUSD": "NZDUSD",
        "EURGBP": "EURGBP",
        "EURJPY": "EURJPY",
        "GBPJPY": "GBPJPY",
        # 商品
        "XAUUSD": "XAUUSD",
        "XAGUSD": "XAGUSD",
        "USOIL": "USOIL",
        "UKOIL": "UKOIL",
        # 指数
        "US100": "US100",
        "US30": "US30",
        "GER40": "GER40",
    }

    # —— 动作关键字映射（对齐参考仓库）——
    BUY_KEYWORDS = ["buy", "long", "做多", "买入", "多"]
    SELL_KEYWORDS = ["sell", "short", "做空", "卖出", "空"]
    CLOSE_KEYWORDS = ["close", "exit", "平仓", "平", "close_all"]
