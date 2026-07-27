"""MT5 登录号一致性：节点绑定账号 vs 终端实际上报。

两边都能解析为正整数且不相等时视为漂移；任一侧缺失/无效则不做拦截
（避免快照暂空误杀）。
"""
from __future__ import annotations

from typing import Optional


def parse_login(value) -> Optional[int]:
    """解析为正整数登录号；无效则返回 None。"""
    if value is None or value == "":
        return None
    try:
        login = int(value)
    except (TypeError, ValueError):
        return None
    return login if login > 0 else None


def is_mt5_login_mismatch(expected, reported) -> bool:
    """配置账号与上报账号是否冲突。"""
    exp = parse_login(expected)
    got = parse_login(reported)
    if exp is None or got is None:
        return False
    return exp != got


def login_mismatch_reason(expected, reported) -> Optional[str]:
    """冲突时返回中文说明，否则 None。"""
    if not is_mt5_login_mismatch(expected, reported):
        return None
    exp = parse_login(expected)
    got = parse_login(reported)
    return f"登录号校验：终端当前账号{got}与节点绑定账号{exp}不符，已断开"


def extract_reported_login(data: dict) -> object:
    """从 hello / account 上报载荷中取出 login 字段。"""
    if not isinstance(data, dict):
        return None
    acct = data.get("account")
    if isinstance(acct, dict) and acct.get("login") not in (None, ""):
        return acct.get("login")
    return data.get("login")
