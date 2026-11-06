"""本机 HTTP + SSE 桥：把 Python 侧的运维能力暴露给 WebView 前端。

为什么用 HTTP + SSE 而不是 pywebview 的 `js_api`：
  - `js_api` 是同步阻塞调用，长耗时操作（导入节点、批量更新）会把 UI 线程卡死；
  - `js_api` 需要前端在页面里显式注册 Python 对象，Vue 里生命周期难对齐；
  - HTTP + SSE 让前端可以像调后端 API 一样用 `fetch`，且后端推送（运行态变化）
    是单向的，SSE 正好合适。

安全模型：
  - 只监听 127.0.0.1，不对外暴露；
  - 启动时生成一次性 session token，所有 /api/* 与 /api/events 必须带 token；
  - token 通过 URL query 传给前端（EventSource 不能自定义 header），
    但它是随机会话级凭证，且只在本机环回地址上有效。

注意：前端必须通过 `http://127.0.0.1:<port>` 加载，不能用 `file://`，
否则 EventSource 会被 CORS 拦掉，SSE 直接不可用。
"""
from __future__ import annotations

import json
import mimetypes
import os
import secrets
import socket
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from auth_service import DashboardAPI, DashboardAuthError
from dashboard_session import DashboardSession
from models import InstanceConfig
from paths import frontend_dist_dir
from process_manager import ProcessManager, _http_json
from store import dedupe_instances, load_instances, save_instances
from viewmodels import build_detail, build_hero, build_list

# 轮询间隔：运行态快照的推送周期（秒）
SNAPSHOT_INTERVAL_S = 1.0

# 单次 SSE 连接的推送间隔
SSE_PUSH_INTERVAL_S = 0.8

# 后端统计（今日信号 / 平均延迟）的本地缓存时长（秒）。
# 快照每 1 秒推一次，不做缓存会把手势级别的请求放大成每秒一次后端查询。
_STATS_TTL_S = 10.0

# 版本徽标的刷新间隔（秒）。徽标要走一次后端 HTTP，放在后台线程里算。
BADGE_REFRESH_INTERVAL_S = 60.0


class BridgeError(RuntimeError):
    """桥接层向用户可见的错误。`status` 用于 HTTP 状态码。"""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class DashboardBridge:
    """Python 运维能力 <-> WebView 前端 的桥。

    生命周期：`start()` 起 HTTP 服务，之后前端通过 `/` 加载页面；
    `stop()` 关闭服务。所有耗时动作都应放在后台线程执行，
    通过 `submit()` 投递，避免阻塞 HTTP 响应线程。
    """

    def __init__(self, *, token: str | None = None) -> None:
        self.token = token or secrets.token_urlsafe(32)
        self.port = 0
        self._server: ThreadingHTTPServer | None = None
        self._lock = threading.RLock()
        self._sessions: set[threading.Event] = set()
        self._snapshot_seq = 0

        # —— 业务状态 ——
        self.session = DashboardSession(on_lock=self._on_session_locked)
        self.manager = ProcessManager(on_change=self._on_runtime_change, session=self.session)
        self._inventory: dict[str, InstanceConfig] = {}
        self._inventory_loaded = False
        self._login_pending_token = ""
        self._lock_reason = ""
        self._busy: dict[str, str] = {}
        self._pending_login: dict[str, Any] = {}
        self._version_info: dict[str, Any] = {}
        self._auto_login_user = ""
        self._auto_login_password = ""
        # 后端统计缓存（今日信号 / 平均延迟），见 `_node_stats`
        self._stats_cache: dict[str, dict] = {}
        self._stats_fetched_at = 0.0

        # —— 导入节点 ——
        # 延迟导入：import_dialog 依赖 models/version_service 等模块，
        # 而这些模块在桥接层初始化时可能还没就绪。
        from import_dialog import ImportController

        self._import = ImportController(self)

        # —— 版本更新 ——
        # 同样延迟导入：version_update 依赖 client_deploy / version_service。
        from version_update import VersionUpdateController

        self._version_update = VersionUpdateController(self)

        # —— 前端脚本错误 ——
        # 白屏最常见的成因是前端 JS 崩了（Vue 没挂上）。浏览器控制台在
        # 打包后的窗口里没人看得到，所以前端把错误回传给本机桥落盘。
        self._client_logs: list[dict[str, Any]] = []
        self._client_log_lock = threading.RLock()

    # ─────────────────────────────────────────────── 生命周期

    def start(self) -> int:
        """启动 HTTP 服务，返回端口。"""
        self.port = _free_port()
        handler = _make_handler(self)
        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), handler)
        self._server.daemon_threads = True
        threading.Thread(
            target=self._server.serve_forever, name="dashboard-bridge", daemon=True
        ).start()
        threading.Thread(target=self._snapshot_loop, name="dashboard-snapshot", daemon=True).start()
        threading.Thread(target=self._badge_loop, name="dashboard-badge", daemon=True).start()
        # 自动登录：后端在途（网络 I/O），放后台线程，别拖慢 HTTP 服务起来。
        self._load_auto_login()
        if self._auto_login_user and self._auto_login_password:
            threading.Thread(
                target=self._try_auto_login, name="dashboard-auto-login", daemon=True
            ).start()
        return self.port

    def stop(self) -> None:
        server = self._server
        self._server = None
        if server is not None:
            try:
                server.shutdown()
            except Exception:  # noqa: BLE001
                pass
            try:
                server.server_close()
            except Exception:  # noqa: BLE001
                pass
        with self._lock:
            for event in list(self._sessions):
                event.set()
            self._sessions.clear()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?token={self.token}"

    # ─────────────────────────────────────────────── 推送

    def _on_runtime_change(self, _instance_id: str) -> None:
        self._push_snapshot()

    def _on_session_locked(self, reason: str) -> None:
        self._lock_reason = reason
        # 会话没了，缓存的统计也没意义了（下次登录要重新取）。
        with self._lock:
            self._stats_cache = {}
            self._stats_fetched_at = 0.0
        self._push_snapshot()

    def _snapshot_loop(self) -> None:
        while True:
            time.sleep(SNAPSHOT_INTERVAL_S)
            try:
                self._push_snapshot()
            except Exception:  # noqa: BLE001
                pass

    def _badge_loop(self) -> None:
        """后台刷新版本徽标。

        徽标要「后端发布版本 + 本机待更新数」，前者必须走网络。
        放在独立线程里算好再写进缓存，快照只读缓存 ——
        否则每秒一次的快照会被一次后端 HTTP 阻塞住，SSE 推送随之卡顿。
        """
        while True:
            time.sleep(BADGE_REFRESH_INTERVAL_S)
            try:
                self._version_update.refresh_badge()
            except Exception:  # noqa: BLE001
                pass

    def _push_snapshot(self) -> None:
        """通知所有 SSE 订阅者「状态有变」，由各自的连接线程重新取快照。

        不在这里直接写 socket：持锁写网络会把阻塞传播给所有订阅者。
        """
        with self._lock:
            events = list(self._sessions)
        for event in events:
            try:
                event.set()
            except Exception:  # noqa: BLE001
                pass

    # ─────────────────────────────────────────────── 快照

    def snapshot(self) -> dict[str, Any] | None:
        """当前完整状态。未登录时返回 None。"""
        if not self.session.active:
            return {
                "type": "locked",
                "reason": self._lock_reason,
                "authenticated": False,
            }
        with self._lock:
            self._snapshot_seq += 1
            seq = self._snapshot_seq
        stats = self._node_stats()
        items = build_list(self.manager, None, stats)
        hero = build_hero(self.manager, stats)
        detail = None
        return {
            "type": "snapshot",
            "seq": seq,
            "authenticated": True,
            "is_admin": self.session.is_admin,
            "username": str(self.session.principal.get("username") or ""),
            "hero": {
                "total": hero.total,
                "online": hero.online,
                "starting": hero.starting,
                "stopped": hero.stopped,
                "pending": hero.pending,
                "today_signals": hero.today_signals,
                "avg_latency_ms": hero.avg_latency_ms,
                "alerts": hero.alerts,
            },
            "nodes": [
                {
                    "id": vm.id,
                    "title": vm.title,
                    "path_short": vm.path_short,
                    "status_line": vm.status_line,
                    "alive": vm.alive,
                    "health": vm.health,
                    "daemon": vm.daemon,
                    "mt5_login": vm.mt5_login,
                    "algo_trading_disabled": vm.algo_trading_disabled,
                    "node_id": vm.node_id,
                    "server_label": vm.server_label,
                    "account_label": vm.account_label,
                    "uptime_text": vm.uptime_text,
                    "mt5_connected": vm.mt5_connected,
                    "mt5_authenticated": vm.mt5_authenticated,
                    "algo_trading_ok": vm.algo_trading_ok,
                    "algo_trading_known": vm.algo_trading_known,
                    "badge_text": vm.badge_text,
                    "badge_tone": vm.badge_tone,
                    "pending_review": vm.pending_review,
                    "today_signals": vm.today_signals,
                    "avg_latency_ms": vm.avg_latency_ms,
                }
                for vm in items
            ],
            "busy": dict(self._busy),
            "version": dict(self._version_info),
            "update": self._update_badge(),
        }

    def _update_badge(self) -> dict[str, Any]:
        """版本更新徽标（发布版本 + 待更新实例数）。

        取不到后端时返回空值：版本检查失败不该影响控制台本身。
        """
        try:
            return self._version_update.badge()
        except Exception:  # noqa: BLE001
            return {"published": "", "pending": 0}

    def _node_stats(self) -> dict[str, dict]:
        """后端统计（今日信号 / 平均延迟 / 服务器名），按 node_id 索引。

        快照每 1 秒推一次，不能每次都打后端：这里按 TTL 缓存，
        默认 10 秒刷新一次。取不到（后端版本旧 / 网络抖动）时返回空 dict，
        前端据此把指标显示成「--」而不是 0——0 会被误读成「今天没信号」。
        """
        now = time.monotonic()
        with self._lock:
            if self._stats_cache and now - self._stats_fetched_at < _STATS_TTL_S:
                return self._stats_cache
        try:
            data = self.session.api.stats(self.session.token)
        except Exception:  # noqa: BLE001
            data = {}
        with self._lock:
            self._stats_cache = data or {}
            self._stats_fetched_at = now
        return self._stats_cache

    def detail_snapshot(self, instance_id: str) -> dict[str, Any]:
        mp = self.manager.get(instance_id)
        vm = build_detail(mp)
        # 注意：这里不再下发 tasks_text / log_tail。
        # 运行日志与策略任务日志不展示给用户（也不需要把几十 KB 日志
        # 走一遍本机 HTTP），省掉一次磁盘读取与一次传输。
        return {
            "empty": vm.empty,
            "title": vm.title,
            "subtitle": vm.subtitle,
            "daemon": vm.daemon,
            "summary": vm.summary,
            "alive": vm.alive,
            "health": vm.health,
            "busy": vm.busy,
            "chips": [
                {"key": c.key, "text": c.text, "tone": c.tone, "visible": c.visible}
                for c in vm.chips
            ],
        }

    # ─────────────────────────────────────────────── 动作

    def login(self, username: str, password: str, code: str = "") -> dict[str, Any]:
        """登录。返回 `{ok, requires_2fa, error}`。"""
        base = self._backend_base()
        try:
            api = DashboardAPI(base)
        except DashboardAuthError as exc:
            raise BridgeError(str(exc), status=400) from exc

        try:
            if self._login_pending_token:
                result = api.login_2fa(self._login_pending_token, code)
            else:
                result = api.login(username, password)
        except DashboardAuthError as exc:
            raise BridgeError(str(exc), status=exc.status or 401) from exc

        if result.get("requires_2fa"):
            self._login_pending_token = str(result.get("login_token") or "")
            return {"ok": False, "requires_2fa": True, "error": "请输入 2FA 验证码"}

        token = str(result.get("token") or "")
        if not token:
            raise BridgeError("后端未返回登录凭证", status=500)
        self._login_pending_token = ""
        self._establish(api, token)
        return {"ok": True, "requires_2fa": False, "error": ""}

    def _backend_base(self) -> str:
        from store import load_app_url

        configured = load_app_url().strip()
        if not configured:
            raise BridgeError(
                "未配置后端地址：请在程序目录的 .env 中设置 APP_URL", status=400
            )
        return configured

    def config_status(self) -> dict[str, Any]:
        """后端配置状态。登录页要靠它告诉用户「为什么连不上」。

        登录失败有多种原因（没配 APP_URL / 配错地址 / 账号密码不对），
        前端必须能把它们区分开，否则用户只看到一句「无法连接」。
        """
        from store import dashboard_env_path, load_app_url

        base = load_app_url().strip()
        env_path = dashboard_env_path()
        return {
            "ok": bool(base),
            "app_url": base,
            "env_path": str(env_path),
            "env_exists": env_path.is_file(),
            "auto_login": bool(self._auto_login_user and self._auto_login_password),
            "error": "" if base else (
                f"未配置后端地址：请在 {env_path} 中设置 APP_URL"
            ),
        }

    def _try_auto_login(self) -> None:
        """用 .env 里的 DASHBOARD_LOGIN_USER/PASSWORD 自动登录。

        两项都留空 = 不自动登录（保持显示登录页）。
        开了 2FA 的账号无法自动登录，仍需交互输入验证码。
        """
        user = self._auto_login_user
        password = self._auto_login_password
        if not user or not password:
            return
        try:
            result = self.login(user, password)
        except BridgeError as exc:
            self._lock_reason = f"自动登录失败：{exc}"
            return
        if result.get("requires_2fa"):
            self._lock_reason = "该账号启用了 2FA，无法自动登录，请手动输入验证码"
            self._login_pending_token = ""

    def _load_auto_login(self) -> None:
        """从面板 .env 读取自动登录账号。"""
        from store import dashboard_env_path
        from env_file import parse_env_text

        try:
            text = dashboard_env_path().read_text(encoding="utf-8-sig")
        except OSError:
            return
        values = parse_env_text(text)
        self._auto_login_user = values.get("DASHBOARD_LOGIN_USER", "").strip()
        self._auto_login_password = values.get("DASHBOARD_LOGIN_PASSWORD", "").strip()

    def _establish(self, api: DashboardAPI, token: str) -> None:
        try:
            self.session.establish(api, token)
        except DashboardAuthError as exc:
            raise BridgeError(str(exc), status=exc.status or 401) from exc
        self._lock_reason = ""
        self._load_permitted_instances()
        # 登录成功就先把徽标算出来：否则用户登录后还要等一个刷新周期才看到提示。
        threading.Thread(
            target=self._version_update.refresh_badge, name="badge-refresh", daemon=True
        ).start()

    def _load_permitted_instances(self) -> None:
        """按当前会话的权限装载可见实例（与 TK 版 `_load_permitted_instances` 同源）。"""
        stored = load_instances()
        stored, dropped = dedupe_instances(stored)
        for cfg in dropped:
            if cfg.status_port > 0:
                try:
                    _http_json("POST", f"http://127.0.0.1:{cfg.status_port}/stop", timeout=0.6)
                except Exception:  # noqa: BLE001
                    pass
        for cfg in stored:
            self._inventory.setdefault(cfg.id, cfg)
        self._inventory_loaded = True

        candidates = list(self._inventory.values())
        allowed: list[InstanceConfig] = []
        for cfg in candidates:
            mp = self.manager._items.get(cfg.id)
            if mp is not None:
                cfg = mp.cfg
            if cfg.backend_base and cfg.backend_base.rstrip("/") != self.session.api.base:
                if self.session.is_admin:
                    cfg.approval_status = "unbound"
                    allowed.append(cfg)
                continue
            actual = None
            if cfg.status_port > 0:
                try:
                    _, actual = _http_json(
                        "GET", f"http://127.0.0.1:{cfg.status_port}/status", timeout=0.3
                    )
                except Exception:  # noqa: BLE001
                    pass
            if actual:
                node_id = str(actual.get("node_id") or "")
                login = int(actual.get("mt5_login") or 0)
                node = self.session.nodes.get(node_id)
                if not node and not node_id and login:
                    matches = [
                        n for n in self.session.nodes.values()
                        if int(n.get("mt5_login") or 0) == login
                    ]
                    node = matches[0] if len(matches) == 1 else None
                if node and login == int(node.get("mt5_login") or 0):
                    self.session.apply_node(cfg, node)
                elif self.session.nodes:
                    # 后端确实列出了节点，但这个本机实例对不上 —— 是真的未绑定。
                    if not self.session.is_admin:
                        continue
                    cfg.node_id = None
                    cfg.approval_status = "unbound"
                # 后端节点列表为空（Redis 缓存被清、backend 刚重启还没预热）
                # 时**保留**既有绑定：抹掉 node_id 会让 remove_instance 这类
                # 动作因为缺 node_id 被后端 422 挡下，实例也就再也绑不回去了。
            elif cfg.node_id:
                node = self.session.nodes.get(str(cfg.node_id))
                if node:
                    self.session.apply_node(cfg, node)
            if self.session.owns(cfg):
                allowed.append(cfg)

        self.manager.set_visible({c.id for c in allowed})
        self.manager.load(allowed)
        for cfg in allowed:
            self._inventory[cfg.id] = cfg
            mp = self.manager._items.get(cfg.id)
            if cfg.approval_status == "approved" and cfg.enabled:
                self._reconcile_credential(cfg, mp)
        self._persist()

    def _reconcile_credential(self, cfg, mp) -> None:
        """同步本机专属令牌；本机令牌失效由面板自动重签并让客户端重启。

        用户只需要「导入 + 启动」：专属令牌是面板与后端之间的内部凭据，
        本机丢了就自己补，不把令牌管理甩给使用者。
        """
        try:
            repair = self.session.ensure_local_credential(
                cfg, rotate_allowed=self.session.rotation_allowed(cfg, mp)
            )
        except Exception:  # noqa: BLE001
            # 复核失败不该把整个面板拖垮；下次启动前还会再试一次。
            return
        if not repair.ok:
            if mp is not None:
                mp.runtime.last_error = repair.reason or "节点专属令牌不可用"
                if mp.daemon_grant is not None:
                    mp.daemon_grant.allowed = False
            return
        if mp is not None:
            mp.runtime.last_error = ""
        if not repair.reissued:
            return
        # 后端那一枚旧令牌已被作废：运行中的客户端还在用旧令牌，
        # 必须重启才能读到新的 .env；守护授权也随轮换失效，一并重取。
        if mp is None:
            return
        try:
            self.session.grant_for_recovery(mp)
        except Exception:  # noqa: BLE001
            pass
        if not mp.runtime.process_alive:
            return
        try:
            mp.stop(graceful=True)
        except Exception:  # noqa: BLE001
            pass
        try:
            mp.start()
        except Exception as exc:  # noqa: BLE001
            mp.runtime.last_error = f"专属令牌已自动更新，但节点重启失败：{exc}"

    def _persist(self) -> None:
        if not self._inventory_loaded:
            return
        for iid, mp in self.manager._items.items():
            self._inventory[iid] = mp.cfg
        save_instances(list(self._inventory.values()))

    def logout(self) -> dict[str, Any]:
        try:
            self.session.perform("logout", None, lambda: None)
        finally:
            self._persist()
            self.session.lock("已退出登录，节点与既有守护继续运行", clear_token=True)
        return {"ok": True}

    # ── 实例操作 ──

    def instance_action(self, action: str, instance_id: str, **kwargs: Any) -> dict[str, Any]:
        mp = self.manager.get(instance_id)
        if mp is None:
            raise BridgeError("实例不存在", status=404)
        self.session.check()
        try:
            if action == "start":
                self.manager.start(instance_id)
            elif action == "stop":
                self.manager.stop(instance_id)
            elif action == "restart":
                self.manager.stop(instance_id)
                self.manager.start(instance_id)
            elif action == "refresh_health":
                self.session.perform(
                    "refresh_health", mp.cfg, lambda: mp.refresh_health()
                )
            elif action == "set_daemon":
                enabled = bool(kwargs.get("enabled"))
                self.manager.set_daemon(instance_id, enabled)
            elif action == "remove":
                # `manager.remove` 内部走 session.perform("remove_instance", ...)，
                # 审计票据与「先停止再摘除」的语义都在里面，这里不重复做。
                self.manager.remove(instance_id)
                self._inventory.pop(instance_id, None)
                self._version_update.invalidate_badge()
            elif action == "open_cwd":
                import os as _os
                import subprocess

                self.session.perform(
                    "open_cwd",
                    mp.cfg,
                    lambda: subprocess.Popen(["explorer", mp.cfg.cwd]),
                )
            else:
                raise BridgeError(f"未知操作：{action}", status=400)
        except DashboardAuthError as exc:
            raise BridgeError(str(exc), status=exc.status or 403) from exc
        except BridgeError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise BridgeError(str(exc) or exc.__class__.__name__, status=500) from exc
        self._persist()
        return {"ok": True}

    def batch_action(self, action: str) -> dict[str, Any]:
        self.session.check()
        try:
            if action == "start_all":
                results = self.manager.start_all()
            elif action == "stop_all":
                results = self.manager.stop_all()
            elif action == "daemon_on":
                results = self.manager.set_daemon_all(True)
            elif action == "daemon_off":
                results = self.manager.set_daemon_all(False)
            else:
                raise BridgeError(f"未知批量操作：{action}", status=400)
        except DashboardAuthError as exc:
            raise BridgeError(str(exc), status=exc.status or 403) from exc
        self._persist()
        return {
            "ok": True,
            "results": [
                {"id": i, "name": n, "ok": o, "message": m} for i, n, o, m in results
            ],
        }

    # ─────────────────────────────────────────────── 对话框类操作

    def connection_info(self) -> dict[str, Any]:
        """当前登录后端的信息（对应 TK 版的「后端连接」对话框）。"""
        self.session.check()
        return {
            "ok": True,
            "base": self.session.api.base,
            "username": str(self.session.principal.get("username") or ""),
            "is_admin": self.session.is_admin,
            "node_count": len(self.session.nodes),
        }

    def test_connection(self) -> dict[str, Any]:
        """验证当前登录会话是否仍然有效。"""
        self.session.check()
        try:
            self.session.review()
        except DashboardAuthError as exc:
            raise BridgeError(str(exc), status=exc.status or 401) from exc
        return {"ok": True, "message": "当前连接正常"}

    def read_instance_env(self, instance_id: str) -> dict[str, Any]:
        """读取某实例的 node_client .env（对应 TK 版的「编辑节点环境」对话框）。"""
        self.session.check()
        mp = self.manager.get(instance_id)
        if mp is None:
            raise BridgeError("实例不存在", status=404)
        import env_file as ef

        try:
            text, values = ef.read_env(mp.cfg.cwd)
        except OSError as exc:
            raise BridgeError(f"读取 .env 失败：{exc}", status=400) from exc
        return {
            "ok": True,
            "cwd": mp.cfg.cwd,
            "text": text,
            "values": values,
            "env_path": str(ef.env_path(mp.cfg.cwd)),
        }

    def write_instance_env(self, instance_id: str, text: str) -> dict[str, Any]:
        """写回实例的 node_client .env。写入前做格式校验。"""
        self.session.check()
        mp = self.manager.get(instance_id)
        if mp is None:
            raise BridgeError("实例不存在", status=404)
        import env_file as ef

        try:
            values = ef.parse_env_text(text)
        except Exception as exc:  # noqa: BLE001
            raise BridgeError(f".env 解析失败：{exc}", status=400) from exc

        problems = ef.validate_env(values)
        if problems:
            raise BridgeError("；".join(problems), status=400)

        def write() -> None:
            ef.write_env(mp.cfg.cwd, text)

        try:
            self.session.perform("edit_env", mp.cfg, write)
        except DashboardAuthError as exc:
            raise BridgeError(str(exc), status=exc.status or 403) from exc
        except OSError as exc:
            raise BridgeError(f"写入 .env 失败：{exc}", status=400) from exc
        return {"ok": True}

    def version_target(self) -> Any:
        """版本查询/下载用的后端入口（与 TK 版 `session_ui.version_target` 同源）。

        带上 session，让版本服务内部的调用也走审计。
        """
        self.session.check()
        import version_service as vs

        return vs.BackendTarget(
            self.session.api.base, self.session.token, "用户登录", "Authorization", self.session
        )

    def version_info(self) -> dict[str, Any]:
        """后端当前版本与可用版本列表。"""
        self.session.check()
        import version_service as vs

        target = self.version_target()
        if not target.ready:
            raise BridgeError("当前会话没有版本查询权限", status=403)
        try:
            current = vs.fetch_current_version(target)
            items, published = vs.fetch_available_versions(target)
        except vs.VersionServiceError as exc:
            raise BridgeError(str(exc), status=400) from exc
        return {
            "ok": True,
            "current": current,
            "published": published,
            "available": items,
        }

    def list_local_versions(self, instance_id: str) -> dict[str, Any]:
        """某实例本机的版本与可回滚的备份列表。"""
        self.session.check()
        mp = self.manager.get(instance_id)
        if mp is None:
            raise BridgeError("实例不存在", status=404)
        import client_deploy as cd

        current = cd.read_version_near(mp.cfg.exe_path)
        backups = cd.list_local_backups(mp.cfg.exe_path)
        return {"ok": True, "current": current, "backups": backups}

    def instance_env_target(self, instance_id: str) -> dict[str, Any]:
        """某实例的后端目标（版本查询/下载用）。"""
        self.session.check()
        mp = self.manager.get(instance_id)
        if mp is None:
            raise BridgeError("实例不存在", status=404)
        import version_service as vs

        target = vs.target_from_env(mp.cfg.cwd)
        return {
            "ok": True,
            "base": target.base,
            "ready": target.ready,
            "reason": target.reason,
        }

    # ─────────────────────────────────────────────── 导入节点

    def _pick_directory(self) -> dict[str, Any]:
        """打开系统目录选择器。

        原生对话框必须在 GUI 线程弹（pywebview 的 winforms 后端不做
        Invoke 转发，后台线程直接调用会挂死），所以这里交给
        native_dialogs 做线程 marshal。
        """
        self.session.check()
        import native_dialogs

        try:
            picked = native_dialogs.pick_folder()
        except native_dialogs.NativeDialogError as exc:
            raise BridgeError(str(exc), status=400) from exc
        if not picked:
            return {"ok": True, "path": "", "cancelled": True}
        return {"ok": True, "path": str(picked), "cancelled": False}

    # ─────────────────────────────────────────────── 请求分派

    def record_client_log(self, body: dict[str, Any]) -> dict[str, Any]:
        """接收前端回传的脚本错误 / 警告，落盘。

        打包后的窗口里没有开发者工具，前端 JS 一崩就只剩白屏，
        用户完全无从判断。把 window.onerror / unhandledrejection 收集起来
        POST 到这里，日志就有据可查了。
        """
        entries = body.get("entries")
        if not isinstance(entries, list):
            entries = []
        entries = entries[:200]

        import diagnostics

        log = diagnostics.get_logger("dashboard.frontend")
        count = 0
        with self._client_log_lock:
            for item in entries:
                if not isinstance(item, dict):
                    continue
                level = str(item.get("level") or "error").lower()
                msg = str(item.get("message") or "")[:2000]
                source = str(item.get("source") or "")
                line = str(item.get("line") or "")
                col = str(item.get("column") or "")
                stack = str(item.get("stack") or "")[:4000]
                where = f" ({source}:{line}:{col})" if source else ""
                log.error(
                    "[前端] %s%s | %s", level.upper(), where, msg
                )
                if stack:
                    log.error("[前端] 堆栈：%s", stack)
                self._client_logs.append(item)
                count += 1
            # 只留最近若干条备查，避免内存无限增长。
            if len(self._client_logs) > 500:
                del self._client_logs[:-500]

        return {"ok": True, "recorded": count}

    def client_logs(self) -> dict[str, Any]:
        """前端脚本错误列表（给体检报告与设置面板用）。"""
        with self._client_log_lock:
            items = list(self._client_logs)
        return {"ok": True, "count": len(items), "entries": items}

    def handle_api(self, path: str, query: dict[str, list[str]], body: dict[str, Any]) -> dict[str, Any]:
        """处理一条 /api/* 请求，返回 JSON 可序列化结果。

        鉴权失败（会话失效、未登录）统一转成 401/403；
        其余未预期的异常转成 500，但绝不把栈暴露给前端。
        """
        try:
            return self._dispatch(path, query, body)
        except BridgeError:
            raise
        except DashboardAuthError as exc:
            raise BridgeError(str(exc), status=exc.status or 401) from exc
        except Exception as exc:  # noqa: BLE001
            raise BridgeError(str(exc) or exc.__class__.__name__, status=500) from exc

    def _dispatch(self, path: str, query: dict[str, list[str]], body: dict[str, Any]) -> dict[str, Any]:
        """实际的分派逻辑。异常语义由 handle_api 统一归一化。"""
        method = "POST" if body else "GET"

        if path == "/api/state":
            return self.snapshot() or {"type": "locked", "authenticated": False}

        if path == "/api/login":
            return self.login(
                str(body.get("username") or ""),
                str(body.get("password") or ""),
                str(body.get("code") or ""),
            )

        if path == "/api/config":
            return self.config_status()

        if path == "/api/client-logs":
            return self.client_logs()

        if path == "/api/diagnose":
            import diagnostics

            return diagnostics.environment_report()

        if path == "/api/client-log":
            return self.record_client_log(body)

        if path == "/api/logout":
            return self.logout()

        if path == "/api/detail":
            instance_id = str((query.get("id") or [""])[0])
            return self.detail_snapshot(instance_id)

        if path == "/api/action":
            action = str(body.get("action") or "")
            instance_id = str(body.get("id") or "")
            return self.instance_action(action, instance_id)

        if path == "/api/batch":
            return self.batch_action(str(body.get("action") or ""))

        if path == "/api/refresh":
            self._load_permitted_instances()
            return {"ok": True}

        # —— 对话框类 ——
        if path == "/api/connection":
            return self.connection_info()

        if path == "/api/connection/test":
            return self.test_connection()

        if path == "/api/env/read":
            return self.read_instance_env(str((query.get("id") or [""])[0]))

        if path == "/api/env/write":
            return self.write_instance_env(
                str(body.get("id") or ""), str(body.get("text") or "")
            )

        if path == "/api/version/info":
            return self.version_info()

        if path == "/api/version/local":
            return self.list_local_versions(str((query.get("id") or [""])[0]))

        if path == "/api/version/target":
            return self.instance_env_target(str((query.get("id") or [""])[0]))

        # —— 版本更新 ——
        if path == "/api/version-update/instances":
            return self._version_update.instances()

        if path == "/api/version-update/detect":
            return self._version_update.detect()

        if path == "/api/version-update/prepare":
            return self._version_update.prepare(body)

        if path == "/api/version-update/start":
            return self._version_update.start_update(body)

        if path == "/api/version-update/rollback":
            return self._version_update.start_rollback(body)

        if path == "/api/version-update/progress":
            task_id = str((query.get("task_id") or [""])[0])
            task = self._version_update.task(task_id)
            if task is None:
                raise BridgeError("版本更新任务不存在或已过期", status=404)
            return task.snapshot()

        # —— 导入节点 ——
        if path == "/api/import/check-dir":
            return {"ok": True, **self._import.check_dir(str((query.get("path") or [""])[0]))}

        if path == "/api/import/versions":
            return self._import.versions()

        if path == "/api/import/pick-dir":
            return self._pick_directory()

        if path == "/api/import/prepare":
            return self._import.prepare(body)

        if path == "/api/import/start":
            return self._import.start(body)

        if path == "/api/import/progress":
            task_id = str((query.get("task_id") or [""])[0])
            task = self._import.task(task_id)
            if task is None:
                raise BridgeError("导入任务不存在或已过期", status=404)
            return task.snapshot()

        raise BridgeError(f"未知接口：{path}", status=404)


def _make_handler(bridge: DashboardBridge):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "DashboardBridge/1.0"

        # ── 工具 ──

        def log_message(self, *args) -> None:  # noqa: A003
            pass

        def handle_one_request(self) -> None:
            """吞掉客户端提前断开产生的噪声。

            WebView 关闭瞬间，SSE 连接会被 Windows 报 WinError 10053。
            这不是服务端错误，不该在控制台刷一屏 traceback。
            """
            try:
                super().handle_one_request()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                self.close_connection = True

        def _token_ok(self) -> bool:
            token = bridge.token
            if not token:
                return True
            supplied = (self.path_params.get("token") or [""])[0]
            return bool(supplied) and secrets.compare_digest(supplied, token)

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def _send_json(self, code: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._send(code, body, "application/json; charset=utf-8")

        def _read_body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            if not raw:
                return {}
            try:
                data = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeError):
                return {}
            return data if isinstance(data, dict) else {}

        # ── 路由 ──

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            self.path_params = parse_qs(parsed.query)
            path = parsed.path

            if path == "/api/events":
                self._serve_sse()
                return

            if path.startswith("/api/"):
                if not self._token_ok():
                    self._send_json(403, {"error": "会话令牌无效"})
                    return
                try:
                    payload = bridge.handle_api(path, self.path_params, {})
                except BridgeError as exc:
                    self._send_json(exc.status, {"error": str(exc)})
                    return
                except Exception as exc:  # noqa: BLE001
                    self._send_json(500, {"error": str(exc)})
                    return
                self._send_json(200, payload)
                return

            self._serve_static(path)

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            self.path_params = parse_qs(parsed.query)
            path = parsed.path

            if not path.startswith("/api/"):
                self._send_json(404, {"error": "not found"})
                return
            if not self._token_ok():
                self._send_json(403, {"error": "会话令牌无效"})
                return

            body = self._read_body()
            try:
                payload = bridge.handle_api(path, self.path_params, body)
            except BridgeError as exc:
                self._send_json(exc.status, {"error": str(exc)})
                return
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"error": str(exc)})
                return
            self._send_json(200, payload)

        # ── SSE ──

        def _serve_sse(self) -> None:
            if not self._token_ok():
                self._send_json(403, {"error": "会话令牌无效"})
                return

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

            event = threading.Event()
            with bridge._lock:
                bridge._sessions.add(event)
            try:
                # 先立刻推一份，避免前端等满一个周期才拿到首屏。
                snapshot = bridge.snapshot()
                if snapshot is not None:
                    self._write_event("snapshot", snapshot)

                while True:
                    if event.wait(timeout=SSE_PUSH_INTERVAL_S):
                        event.clear()
                    snapshot = bridge.snapshot()
                    if snapshot is None:
                        continue
                    self._write_event("snapshot", snapshot)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            except Exception:  # noqa: BLE001
                pass
            finally:
                with bridge._lock:
                    bridge._sessions.discard(event)

        def _write_event(self, name: str, payload: dict[str, Any]) -> None:
            data = json.dumps(payload, ensure_ascii=False)
            chunk = f"event: {name}\ndata: {data}\n\n".encode("utf-8")
            self.wfile.write(chunk)
            self.wfile.flush()

        # ── 静态资源 ──

        def _serve_static(self, path: str) -> None:
            root = frontend_dist_dir()
            if path in ("/", ""):
                path = "/index.html"
            # 防目录穿越
            relative = path.lstrip("/")
            target = (root / relative).resolve()
            try:
                target.relative_to(root.resolve())
            except ValueError:
                self._send(403, b"forbidden", "text/plain; charset=utf-8")
                return
            if not target.is_file():
                index = root / "index.html"
                if not index.is_file():
                    # 前端没构建时给一张可读的提示页，而不是裸 404。
                    self._send(200, _PLACEHOLDER_HTML, "text/html; charset=utf-8")
                    return
                # SPA 回退
                target = index
            data = target.read_bytes()
            ctype, _ = mimetypes.guess_type(str(target))
            if target.suffix == ".js":
                ctype = "text/javascript; charset=utf-8"
            elif target.suffix == ".css":
                ctype = "text/css; charset=utf-8"
            elif target.suffix == ".html":
                ctype = "text/html; charset=utf-8"
            self._send(200, data, ctype or "application/octet-stream")

    return Handler


_PLACEHOLDER_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>亿点点 · 交易控制台</title>
<style>
 body{background:#0a0e17;color:#e8ecf4;font-family:"Segoe UI",system-ui,sans-serif;
      display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
 .box{max-width:560px;padding:32px 36px;background:#101826;border:1px solid #1e2a3d;border-radius:10px}
 h1{margin:0 0 12px;font-size:18px;color:#d9b36c}
 p{margin:8px 0;color:#8b96ab;font-size:13px;line-height:1.7}
 code{background:#151f31;padding:2px 6px;border-radius:4px;color:#e8ecf4}
</style></head>
<body><div class="box">
<h1>前端尚未构建</h1>
<p>本机 HTTP/SSE 桥已经就绪，但找不到前端产物。</p>
<p>请在仓库根目录执行：</p>
<p><code>cd frontend-console</code><br><code>npm install</code><br><code>npm run build</code></p>
<p>构建产物应位于 <code>frontend-console/dist</code>。</p>
</div></body></html>
""".encode("utf-8")
