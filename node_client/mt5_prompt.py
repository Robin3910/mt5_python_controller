"""启动时解析 MT5 会话：已登录则复用；否则交互输入账号/密码/服务器。"""

from config import get_settings
from mt5_discover import discover_mt5_terminal


def prompt_mt5_credentials() -> dict[str, str | int | bool]:
    """采集 MT5 连接参数。

    - MT5_MOCK：返回模拟默认值
    - 真实模式：先在程序目录定位终端；若终端已登录则免输账号/密码/服务器；
      未登录再交互输入三者并在后续 connect 时执行 mt5.login。
    """
    settings = get_settings()
    if settings.mt5_mock:
        print("MT5_MOCK=true，跳过登录输入，使用模拟账户")
        return {
            "mt5_login": 90000001,
            "mt5_password": "mock",
            "mt5_server": "MockServer",
            "mt5_path": "",
            "reuse_terminal_session": False,
        }

    mt5_path = str(discover_mt5_terminal())
    print(f"已定位 MT5 终端：{mt5_path}")

    from mt5_client import MT5Error, peek_logged_in_account

    try:
        session = peek_logged_in_account(mt5_path)
    except MT5Error as e:
        raise MT5Error(f"无法连接 MT5 终端：{e}") from e

    if session:
        login = session["login"]
        server = session["server"] or ""
        print(f"检测到终端已登录：账号 {login}" + (f" @ {server}" if server else ""))
        print("将复用当前会话，无需再输入账号/密码/服务器")
        return {
            "mt5_login": login,
            "mt5_password": "",
            "mt5_server": server,
            "mt5_path": mt5_path,
            "reuse_terminal_session": True,
        }

    print("终端当前未登录，请输入账号信息：")
    print("=== MT5 登录信息 ===")
    login_str = input("MT5 账号 (MT5_LOGIN): ").strip()
    while not login_str.isdigit() or int(login_str) <= 0:
        login_str = input("请输入有效的正整数账号: ").strip()
    password = input("MT5 密码 (MT5_PASSWORD): ").strip()
    server = input("MT5 服务器 (MT5_SERVER): ").strip()
    while not server:
        server = input("服务器不能为空，请重新输入: ").strip()
    return {
        "mt5_login": int(login_str),
        "mt5_password": password,
        "mt5_server": server,
        "mt5_path": mt5_path,
        "reuse_terminal_session": False,
    }
