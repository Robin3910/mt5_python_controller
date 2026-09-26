"""进程内 WebSocket 连接注册表（单实例设计）。

若要做多实例高可用，应通过 Redis Pub/Sub 路由命令（见文档第 5 章），届时本
管理器只持有“当前实例自己”的连接。
"""
import logging
import time
from typing import Awaitable, Callable, Optional

from fastapi import WebSocket

from . import permissions
from .permissions import Principal

logger = logging.getLogger(__name__)

# 后台 WS 关闭码：会话已吊销（禁用 / 改密 / 重置密码），前端据此退出登录、不再重连
WS_CLOSE_REVOKED = 4401
# 权限已变更（角色 / 菜单 / 节点归属），前端重新拉取 /api/me 与列表后自动重连
WS_CLOSE_PERM_CHANGED = 4001

# (资源类型, 资源 ID) -> 所有者 user_id；启动时由 main 注入（需要 Redis 访问层）
OwnerResolver = Callable[[tuple[str, str]], Awaitable[Optional[int]]]


class ConnectionManager:
    def __init__(self) -> None:
        self.nodes: dict[str, WebSocket] = {}   # node_id -> 连接
        self.admins: dict[WebSocket, Principal] = {}  # 后台连接 -> 登录身份（推送按归属过滤）
        self.owner_resolver: Optional[OwnerResolver] = None

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
    def add_admin(self, ws: WebSocket, principal: Principal) -> None:
        self.admins[ws] = principal

    def remove_admin(self, ws: WebSocket) -> None:
        self.admins.pop(ws, None)

    async def _event_owner(
        self, resource: Optional[tuple[str, str]],
    ) -> Optional[int]:
        if resource is None or self.owner_resolver is None:
            return None
        try:
            return await self.owner_resolver(resource)
        except Exception:  # noqa: BLE001
            logger.warning("resolve event owner failed: %s", resource)
            return None

    async def broadcast_admin(self, message: dict) -> None:
        """向后台连接广播：管理员全收，普通用户只收自己名下节点 / 分组的事件。

        只有存在普通用户连接时才解析事件归属，纯管理员场景不多查一次 Redis。
        顺手清理已断开的连接。
        """
        targets = list(self.admins.items())
        resource: Optional[tuple[str, str]] = None
        owner: Optional[int] = None
        if any(not p.is_admin for _, p in targets):
            resource = permissions.event_resource(message)
            owner = await self._event_owner(resource)
        dead = []
        for ws, principal in targets:
            if not permissions.event_visible(principal, resource, owner):
                continue
            try:
                await ws.send_json(message)
            except Exception:  # noqa: BLE001
                dead.append(ws)
        for ws in dead:
            self.admins.pop(ws, None)

    async def close_user(self, username: str, code: int) -> int:
        """断开某用户的全部后台连接（会话吊销 / 权限变更）；返回断开的连接数。"""
        targets = [ws for ws, p in list(self.admins.items()) if p.username == username]
        for ws in targets:
            self.admins.pop(ws, None)
            try:
                await ws.close(code=code)
            except Exception:  # noqa: BLE001
                pass
        return len(targets)


# 全局单例（单实例部署下即为连接的唯一真相来源）
manager = ConnectionManager()
