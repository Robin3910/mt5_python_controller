"""本机空闲 TCP 端口分配。"""
from __future__ import annotations

import socket


def find_free_port(host: str = "127.0.0.1", preferred: int = 0) -> int:
    """返回可用端口。preferred>0 且可绑定时优先用它。"""
    if preferred > 0 and _can_bind(host, preferred):
        return preferred
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return int(s.getsockname()[1])


def _can_bind(host: str, port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, port))
            return True
    except OSError:
        return False


def allocate_ports(used: set[int], count: int = 1, start: int = 18765) -> list[int]:
    """从 start 起跳过已占用，分配 count 个端口。"""
    out: list[int] = []
    port = start
    while len(out) < count and port < 65000:
        if port not in used and _can_bind("127.0.0.1", port):
            out.append(port)
            used.add(port)
        port += 1
    if len(out) < count:
        raise RuntimeError("无法分配足够的本机状态端口")
    return out


def port_has_listener(host: str = "127.0.0.1", port: int = 0, timeout: float = 0.2) -> bool:
    """该地址上是否已有进程在 accept。

    只有连接成功才算有监听。这台 Windows 上，空端口和只 bind 未 listen 的套接字
    都会让 connect 超时而不是立刻拒绝，不能把超时当成占用。
    """
    if int(port) <= 0:
        return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        try:
            sock.connect((host, int(port)))
        except OSError:
            return False
    return True
