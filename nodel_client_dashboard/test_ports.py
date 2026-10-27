"""ports / 日志解码单元测试。"""
import socket

from ports import allocate_ports, find_free_port, port_has_listener
from process_manager import decode_log_bytes


def test_find_free_port():
    p = find_free_port()
    assert isinstance(p, int) and p > 0


def test_allocate_ports_unique():
    used: set[int] = set()
    a = allocate_ports(used, 2, start=18765)
    assert len(a) == 2
    assert a[0] != a[1]
    assert a[0] in used and a[1] in used


def test_port_has_listener_follows_accept():
    held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    held.bind(("127.0.0.1", 0))
    held.listen(1)
    port = held.getsockname()[1]
    try:
        assert port_has_listener("127.0.0.1", port) is True
    finally:
        held.close()
    assert port_has_listener("127.0.0.1", port) is False


def test_decode_log_bytes_utf8():
    text = "已登录 MT5 账户 52936219"
    assert decode_log_bytes(text.encode("utf-8")) == text


def test_decode_log_bytes_gbk():
    text = "远程计算机拒绝网络连接。复用已登录的 MT5 账户"
    assert decode_log_bytes(text.encode("gbk")) == text
