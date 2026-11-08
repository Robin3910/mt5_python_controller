"""节点客户端配置（从 .env 加载）。"""
import sys
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _env_file() -> str:
    """开发时用当前目录 .env；PyInstaller 打包后用 exe 同目录 .env。"""
    if getattr(sys, "frozen", False):
        return str(Path(sys.executable).parent / ".env")
    return ".env"


class NodeSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_env_file(), env_file_encoding="utf-8-sig", extra="ignore"
    )

    # 后端网关地址，生产经 nginx/TLS 走 wss://hub.example.com/ws/node
    manager_ws_url: str = "ws://localhost:8000/ws/node"
    node_token: str = ""  # 节点接入令牌；面板节点由后台签发每节点专属凭证
    # 面板通过进程环境注入，存在时只能复用此账号的已登录终端，禁止交互换号。
    dashboard_expected_mt5_login: int | None = Field(default=None, gt=0)

    mt5_mock: bool = False  # true 时用内存模拟，免装 MT5

    # 各类时间间隔（秒）
    heartbeat_interval: int = 15        # 心跳间隔
    account_report_interval: float = 5.0  # 账户上报间隔（支持小数，下限见 clamp）
    reconnect_min: int = 2              # 重连退避下限
    reconnect_max: int = 30             # 重连退避上限
    auth_timeout: int = 10             # 等待 auth_ok 的超时

    # 交易相关
    watch_symbols: str = "XAUUSD,EURUSD,GBPUSD,USDJPY,US30,US100"  # 观察列表（上报报价）
    default_slippage: int = 20          # 默认滑点
    default_magic: int = 20240615       # 默认魔术号

    # 策略监控事件总线（MT5 无推送接口，事件由统一采样生成，见 market_hub.py）
    strategy_sample_interval: float = 0.5   # 采样间隔，决定事件延迟上限
    strategy_idle_interval: float = 5.0     # 无变化时的保活事件间隔
    strategy_empty_confirm: int = 2         # 判定持仓已全平所需的连续确认轮数

    # 首单失败后的「核实窗口」：下单失败不等于没成交。
    #
    # 券商回执只是**回执**：超时（10012）、order_send 返回 None 这类失败里，
    # 订单其实可能已经成交。节点若据此立刻收口，任务就停了而仓位还在，
    # 变成没人监控的孤儿单。所以失败后要留一段窗口反复读该魔术号的
    # 持仓 / 挂单，确认真的没成交再收口。
    #
    # 窗口内**不重试下单**（重试只会放大重复成交），只观察。
    strategy_open_verify_sec: float = 15.0      # 核实窗口总时长（秒）
    strategy_open_verify_interval: float = 1.0  # 窗口内的轮询间隔（秒）

    log_level: str = "INFO"

    # 历史成交采集（订单历史 / 带单绩效 / 概览盈亏统计的数据源）
    deals_sync_enabled: bool = True     # 关闭后节点不再上报成交
    deals_sync_interval: float = 60.0   # 采集间隔（秒）
    deals_batch_size: int = 500         # 单次上报的成交条数上限
    deals_lookback_days: int = 7        # 首次运行回看天数（此前的成交一并补采）

    # 本机运维面板状态口（仅 loopback；PORT=0 关闭）
    local_status_host: str = "127.0.0.1"
    local_status_port: int = 0

    @property
    def watchlist(self) -> list[str]:
        """逗号分隔 -> 列表。"""
        return [s.strip() for s in self.watch_symbols.split(",") if s.strip()]


ACCOUNT_REPORT_INTERVAL_MIN = 0.05
ACCOUNT_REPORT_INTERVAL_DEFAULT = 5.0

# 首单核实窗口的参数边界（秒）
OPEN_VERIFY_SEC_MIN = 0.0
OPEN_VERIFY_SEC_MAX = 120.0
OPEN_VERIFY_INTERVAL_MIN = 0.1
OPEN_VERIFY_INTERVAL_MAX = 10.0


def clamp_account_report_interval(value) -> float:
    """账户上报间隔（秒）：支持小数；空/非正回退默认 5，下限 0.05。"""
    try:
        n = float(value)
    except (TypeError, ValueError):
        n = ACCOUNT_REPORT_INTERVAL_DEFAULT
    if n <= 0:
        n = ACCOUNT_REPORT_INTERVAL_DEFAULT
    return max(ACCOUNT_REPORT_INTERVAL_MIN, n)


def clamp_open_verify_sec(value) -> float:
    """首单核实窗口时长（秒）：负数视为「不启用核实」，上限 120 秒。"""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return 0.0
    if n < 0:
        return 0.0
    return min(n, OPEN_VERIFY_SEC_MAX)


def clamp_open_verify_interval(value) -> float:
    """核实窗口的轮询间隔（秒）：下限 0.1 秒，上限 10 秒。"""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return 1.0
    return max(OPEN_VERIFY_INTERVAL_MIN, min(n, OPEN_VERIFY_INTERVAL_MAX))


@lru_cache
def get_settings() -> NodeSettings:
    return NodeSettings()
