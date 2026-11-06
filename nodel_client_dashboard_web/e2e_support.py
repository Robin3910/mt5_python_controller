"""端到端脚本共用的假会话。

导入相关接口（check-dir / pick-dir / versions / prepare / start）都要求
已登录，所以 e2e 脚本需要一个「已登录」的会话替身，
免去真的连后端做认证。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

FAKE_SNAPSHOT: dict[str, Any] = {
    "type": "snapshot",
    "seq": 1,
    "authenticated": True,
    "is_admin": True,
    "username": "admin",
    "hero": {
        "total": 0, "online": 0, "starting": 0, "stopped": 0, "pending": 0,
        "today_signals": 0, "avg_latency_ms": None, "alerts": 0,
    },
    "nodes": [],
    "busy": {},
    "version": {},
}


class FakeApi:
    """版本/审计接口要用的后端身份替身。"""

    base = "http://127.0.0.1:9/e2e"

    def stats(self, token: str) -> dict[str, dict]:
        """后端汇总统计替身：按 node_id 索引，没有数据就是空。"""
        return {}


class FakeSession:
    """已登录会话的最小替身：只实现 e2e 会碰到的成员。"""

    def __init__(self) -> None:
        self.principal = {
            "username": "admin",
            "user_id": 1,
            "is_admin": True,
            "menus": ["nodes"],
        }
        self.nodes: dict[str, dict] = {}
        self.api = FakeApi()
        self.token = "e2e-token"
        self.generation = 1
        self._locked = False

    @property
    def active(self) -> bool:
        return True

    @property
    def is_admin(self) -> bool:
        return True

    def check(self, generation: int | None = None) -> None:
        return None

    def grant_for_recovery(self, mp) -> None:
        return None

    def prepare_start(self, mp) -> None:
        return None

    def establish(self, api, token, **kwargs) -> None:
        return None

    def review(self, *, recover: bool = False) -> list[dict]:
        return []

    # ── 导入链路用到的成员 ──
    # perform 原样返回被调函数的返回值——这正是要验证的点：
    # 写 .env 的被调函数返回 None 时，编排层必须能兜住。
    def perform(self, action, cfg, fn, **kwargs):
        return fn()

    def enrolled(self, mt5_login: int, name: str, server: str = "") -> dict:
        return {
            "node_id": "e2e-node",
            "mt5_login": mt5_login,
            "owner_user_id": 1,
            "requested_by_user_id": 1,
            "approval_status": "approved",
            "enabled": True,
            "credential_generation": 1,
            "legacy_allowed": False,
            "has_credential": True,
        }

    def apply_node(self, cfg, node: dict) -> None:
        cfg.node_id = str(node["node_id"])
        cfg.mt5_login = int(node.get("mt5_login") or 0)
        cfg.approval_status = str(node.get("approval_status") or "approved")
        cfg.enabled = bool(node.get("enabled"))
        cfg.backend_base = self.api.base

    def owns(self, cfg) -> bool:
        """管理员可见全部本机实例；普通用户只看自己名下且已绑定的。"""
        if self.is_admin:
            return True
        owner = getattr(cfg, "owner_user_id", None)
        return owner is not None and int(owner) == int(self.principal["user_id"])

    def sync_credential(self, cfg) -> bool:
        """假会话不校验专属凭证，一律视为已同步。"""
        return True

    def ensure_local_credential(self, cfg, *, rotate_allowed: bool = True):
        """假会话没有真凭证，一律视为已就绪。"""
        from dashboard_session import CredentialRepair

        return CredentialRepair(ok=True, token="synthetic-e2e-token")

    def rotation_allowed(self, cfg, mp=None) -> bool:
        return True


def install_fake_session(bridge: Any) -> FakeSession:
    """把桥接层的会话换成假会话，并冻结快照。"""
    from dashboard_bridge import DashboardBridge

    session = FakeSession()
    bridge.session = session

    original_snapshot = DashboardBridge.snapshot
    DashboardBridge.snapshot = lambda self: dict(FAKE_SNAPSHOT)
    bridge._restore_snapshot = lambda: setattr(
        DashboardBridge, "snapshot", original_snapshot
    )
    return session
