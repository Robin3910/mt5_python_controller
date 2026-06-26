"""节点客户端配置（从 .env 加载）。"""
import sys
from functools import lru_cache
from pathlib import Path

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
    node_token: str = ""  # 后端 POST /api/nodes 返回的一次性令牌

    # MT5 登录信息（运行本客户端的 Windows 主机上）
    mt5_login: int = 0
    mt5_password: str = ""
    mt5_server: str = ""
    mt5_path: str = ""      # 可选：terminal64.exe 路径
    mt5_mock: bool = False  # true 时用内存模拟，免装 MT5

    # 各类时间间隔（秒）
    heartbeat_interval: int = 15        # 心跳间隔
    account_report_interval: int = 5    # 账户上报间隔
    reconnect_min: int = 2              # 重连退避下限
    reconnect_max: int = 30             # 重连退避上限
    auth_timeout: int = 10             # 等待 auth_ok 的超时

    # 交易相关
    watch_symbols: str = "XAUUSD,EURUSD,GBPUSD,USDJPY,US30,US100"  # 观察列表（上报报价）
    default_slippage: int = 20          # 默认滑点
    default_magic: int = 20240615       # 默认魔术号

    log_level: str = "INFO"

    @property
    def watchlist(self) -> list[str]:
        """逗号分隔 -> 列表。"""
        return [s.strip() for s in self.watch_symbols.split(",") if s.strip()]


@lru_cache
def get_settings() -> NodeSettings:
    return NodeSettings()
