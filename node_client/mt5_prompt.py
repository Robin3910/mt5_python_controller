"""启动时交互式采集 MT5 登录信息。"""

from config import get_settings


def prompt_mt5_credentials() -> dict[str, str | int]:
    """让用户在终端手动输入 MT5 连接参数；MT5_MOCK 时返回模拟默认值。"""
    settings = get_settings()
    if settings.mt5_mock:
        print("MT5_MOCK=true，跳过登录输入，使用模拟账户")
        return {
            "mt5_login": 90000001,
            "mt5_password": "mock",
            "mt5_server": "MockServer",
            "mt5_path": "",
        }

    print("=== MT5 登录信息 ===")
    login_str = input("MT5 账号 (MT5_LOGIN): ").strip()
    while not login_str.isdigit() or int(login_str) <= 0:
        login_str = input("请输入有效的正整数账号: ").strip()
    password = input("MT5 密码 (MT5_PASSWORD): ").strip()
    server = input("MT5 服务器 (MT5_SERVER): ").strip()
    while not server:
        server = input("服务器不能为空，请重新输入: ").strip()
    path = input("MT5 路径 (MT5_PATH，可选，回车跳过): ").strip()
    return {
        "mt5_login": int(login_str),
        "mt5_password": password,
        "mt5_server": server,
        "mt5_path": path,
    }
