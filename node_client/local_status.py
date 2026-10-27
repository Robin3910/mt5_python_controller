"""本机 loopback HTTP 状态口：供运维面板做健康检查 / 任务快照 / 优雅停止。

仅绑定 LOCAL_STATUS_HOST（默认 127.0.0.1）；LOCAL_STATUS_PORT=0 表示不启动。
无鉴权——切勿改绑到公网地址。

监听套接字必须在连接 MT5 之前创建。端口已被占用时直接失败，避免后启动的进程
先 initialize 终端、再在绑定状态口时崩溃，把仍在运行的进程挤下 IPC。
"""
from __future__ import annotations

import asyncio
import errno
import inspect
import json
import logging
import socket
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("node.local_status")

StatusProvider = Callable[[], dict[str, Any]]
ShutdownFn = Callable[[], Any]


class StatusPortBusy(OSError):
    """状态口已经有进程在监听。此时还没有连接 MT5。"""

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = int(port)
        super().__init__(
            errno.EADDRINUSE,
            f"本机状态口 {self.host}:{self.port} 已被占用，退出且未连接 MT5。"
            "请先结束已在监听该端口的 node_client",
        )


def address_in_use(exc: BaseException) -> bool:
    if not isinstance(exc, OSError):
        return False
    if getattr(exc, "winerror", None) == 10048:
        return True
    return exc.errno in (errno.EADDRINUSE, 10048)


def bind_status_socket(host: str, port: int) -> socket.socket | None:
    """占用状态口。port<=0 时不监听，返回 None。

    不设置 SO_REUSEADDR：Windows 上该选项可能让探测绑定成功，而真正的监听仍然失败。
    """
    if int(port) <= 0:
        return None
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, int(port)))
        sock.listen(8)
    except OSError as e:
        sock.close()
        if address_in_use(e):
            raise StatusPortBusy(host, int(port)) from e
        raise
    return sock


class LocalStatusServer:
    """极简 asyncio HTTP：GET /health、GET /status、POST /stop。"""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        status_provider: StatusProvider,
        on_stop: ShutdownFn,
    ) -> None:
        self.host = host
        self.port = int(port)
        self._status_provider = status_provider
        self._on_stop = on_stop
        self._server: asyncio.AbstractServer | None = None

    @property
    def enabled(self) -> bool:
        return self.port > 0

    async def start(self, sock: socket.socket | None = None) -> None:
        """sock 为 main() 提前绑定的监听套接字；成功后由本服务关闭。"""
        if sock is None and not self.enabled:
            return
        try:
            if sock is not None:
                self._server = await asyncio.start_server(self._handle_client, sock=sock)
            else:
                self._server = await asyncio.start_server(
                    self._handle_client, self.host, self.port
                )
        except Exception:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
            raise
        socks = self._server.sockets or []
        bound = socks[0].getsockname() if socks else (self.host, self.port)
        logger.info("local status listening on http://%s:%s", bound[0], bound[1])

    async def close(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5.0)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            return

        try:
            head = raw.decode("latin-1", errors="replace")
            request_line = head.split("\r\n", 1)[0]
            parts = request_line.split()
            method = parts[0].upper() if parts else ""
            path = parts[1] if len(parts) > 1 else "/"
            path = path.split("?", 1)[0]

            if method == "GET" and path == "/health":
                body = self._health_body()
                await self._respond(writer, 200, body)
            elif method == "GET" and path == "/status":
                body = self._status_provider()
                await self._respond(writer, 200, body)
            elif method == "POST" and path == "/stop":
                result = self._on_stop()
                if inspect.isawaitable(result):
                    await result
                await self._respond(writer, 200, {"ok": True})
            else:
                await self._respond(writer, 404, {"ok": False, "error": "not_found"})
        except Exception as e:  # noqa: BLE001
            logger.warning("local status request error: %s", e)
            try:
                await self._respond(writer, 500, {"ok": False, "error": "internal"})
            except Exception:  # noqa: BLE001
                pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    def _health_body(self) -> dict[str, Any]:
        full = self._status_provider()
        return {
            "ok": True,
            "process": "alive",
            "version": full.get("version"),
            "ws_state": full.get("ws_state"),
            "uptime_s": full.get("uptime_s"),
        }

    @staticmethod
    async def _respond(
        writer: asyncio.StreamWriter, status: int, body: dict[str, Any]
    ) -> None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        reason = {200: "OK", 404: "Not Found", 500: "Internal Server Error"}.get(
            status, "OK"
        )
        header = (
            f"HTTP/1.1 {status} {reason}\r\n"
            "Content-Type: application/json; charset=utf-8\r\n"
            f"Content-Length: {len(payload)}\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("latin-1")
        writer.write(header + payload)
        await writer.drain()
