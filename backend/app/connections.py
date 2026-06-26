"""进程内 WebSocket 连接注册表（单实例设计）。

若要做多实例高可用，应通过 Redis Pub/Sub 路由命令（见文档第 5 章），届时本
管理器只持有“当前实例自己”的连接。
"""
import logging
import time

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ConnectionManager:
    def __init__(self) -> None:
        self.nodes: dict[str, WebSocket] = {}   # node_id -> 连接
        self.admins: set[WebSocket] = set()      # 后台管理端连接集合

    # ---- 节点连接 ----
    async def register_node(self, node_id: str, ws: WebSocket) -> None:
        """登记节点连接；若同一节点已有旧连接则先踢掉（避免重复连接）。"""
        old = self.nodes.get(node_id)
        if old is not None and old is not ws:
            try:
                await old.close(code=4409)  # 4409：重复连接
            except Exception:
                pass
        self.nodes[node_id] = ws
        logger.info("node connected: %s (online=%d)", node_id, len(self.nodes))

    def unregister_node(self, node_id: str, ws: WebSocket) -> None:
        """注销连接（仅当当前登记的就是该连接时才移除，防止误删新连接）。"""
        if self.nodes.get(node_id) is ws:
            self.nodes.pop(node_id, None)
            logger.info("node disconnected: %s (online=%d)", node_id, len(self.nodes))

    def is_node_online(self, node_id: str) -> bool:
        return node_id in self.nodes

    def online_node_ids(self) -> list[str]:
        return list(self.nodes.keys())

    async def is_connection_alive(self, node_id: str) -> bool:
        """探测节点现有连接是否仍存活：发不出去即视为已死并顺手注销。

        用于“同一节点只允许一个在线”：旧连接若已断开（干净关闭或被 uvicorn
        ws-ping 判死），探测会失败，从而放行新连接接管，避免被永久锁死。
        """
        ws = self.nodes.get(node_id)
        if ws is None:
            return False
        try:
            await ws.send_json({"type": "ping", "data": {"ts": time.time()}})
            return True
        except Exception:  # noqa: BLE001
            self.nodes.pop(node_id, None)
            return False

    async def send_to_node(self, node_id: str, message: dict) -> bool:
        """向指定节点下发 JSON；连接不存在或发送失败返回 False。"""
        ws = self.nodes.get(node_id)
        if ws is None:
            return False
        try:
            await ws.send_json(message)
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("send_to_node failed %s: %s", node_id, e)
            return False

    # ---- 后台管理连接 ----
    def add_admin(self, ws: WebSocket) -> None:
        self.admins.add(ws)

    def remove_admin(self, ws: WebSocket) -> None:
        self.admins.discard(ws)

    async def broadcast_admin(self, message: dict) -> None:
        """向所有后台连接广播；顺手清理已断开的连接。"""
        dead = []
        for ws in list(self.admins):
            try:
                await ws.send_json(message)
            except Exception:  # noqa: BLE001
                dead.append(ws)
        for ws in dead:
            self.admins.discard(ws)


# 全局单例（单实例部署下即为连接的唯一真相来源）
manager = ConnectionManager()
