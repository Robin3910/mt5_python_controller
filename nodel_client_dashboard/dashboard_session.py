"""面板会话、动作审计与内存守护授权。独立于 Tk，便于验证失败边界。"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from uuid import uuid4

from auth_service import DashboardAPI, DashboardAuthError
from client_binding import require_binding
from env_policy import env_values
from store import load_audit_queue, save_audit_queue, load_daemon_events, save_daemon_events

_DAEMON_QUEUE_LOCK = threading.RLock()

# 后端那枚专属令牌正被别的机器使用时，不能轮换：轮换会把对方的节点踢下线。
ROTATION_BLOCKED_REASON = "节点正在被其他终端使用，请稍后重试"


@dataclass
class CredentialRepair:
    """本机专属令牌的自愈结果。

    `ok`      —— 本机 .env 里的令牌可用（或已被自动修好）
    `token`   —— 当前生效的令牌（供启动流程复用）
    `reissued` —— 本次发生了重签：后端旧令牌已作废，运行中的客户端需要重启才用得上
    `reason`  —— 修不好的原因，直接给用户看
    """

    ok: bool
    token: str = ""
    reissued: bool = False
    reason: str = ""


class DashboardSession:
    def __init__(self, on_lock=None) -> None:
        self.api: DashboardAPI | None = None
        self.token = ""
        self.principal: dict = {}
        self.nodes: dict[str, dict] = {}
        self.generation = 0
        self._locked = True
        self.on_lock = on_lock or (lambda _reason: None)
        self._mutex = threading.RLock()
        self._local = threading.local()
        self.pending_results = load_audit_queue()

    @property
    def active(self) -> bool:
        return bool(not self._locked and self.token and self.principal and "nodes" in self.principal.get("menus", []))

    @property
    def is_admin(self) -> bool:
        return bool(self.principal.get("is_admin"))

    def establish(self, api: DashboardAPI, token: str, *, expected_generation: int | None = None) -> None:
        principal = api.me(token)
        if "nodes" not in principal.get("menus", []):
            raise DashboardAuthError("当前账号没有节点管理菜单权限", status=403)
        nodes = api.nodes(token)
        self.accept_login(api, token, principal, nodes, expected_generation=expected_generation)

    def accept_login(self, api, token, principal, nodes, *, expected_generation=None):
        if "nodes" not in principal.get("menus", []):
            raise DashboardAuthError("当前账号没有节点管理菜单权限", status=403)
        with self._mutex:
            if expected_generation is not None and expected_generation != self.generation:
                raise DashboardAuthError("登录请求已过期，请重新登录", status=401)
            self.api, self.token, self.principal = api, token, principal
            self.nodes = {str(n["node_id"]): n for n in nodes}
            self.generation += 1
            self._locked = False
        try:
            self.flush_results()
            self.flush_daemon_events()
        except DashboardAuthError:
            self.lock("审计尚未同步，请原操作账号或管理员重新登录")
            raise

    def lock(self, reason: str = "请重新登录", *, clear_token: bool = False) -> None:
        with self._mutex:
            self.generation += 1
            self._locked = True
            if clear_token:
                self.token = ""
                self.principal = {}
                self.nodes = {}
        self.on_lock(reason)

    def check(self, generation: int | None = None) -> None:
        if generation is None:
            generation = getattr(self._local, "generation", None)
        if not self.active or (generation is not None and generation != self.generation):
            raise DashboardAuthError("会话已变化，请重新登录后操作", status=401)
        if self.pending_results:
            raise DashboardAuthError("操作结果尚未同步，请先重新登录完成审计同步")

    def review(self, *, recover: bool = False) -> list[dict]:
        if not recover:
            self.check()
        elif not self.token:
            raise DashboardAuthError("请重新登录", status=401)
        generation = self.generation
        try:
            principal = self.api.me(self.token)
            nodes = self.api.nodes(self.token)
            if "nodes" not in principal.get("menus", []):
                raise DashboardAuthError("节点管理权限已撤回", status=403)
            if generation != self.generation:
                raise DashboardAuthError("会话已变化", status=401)
            self.principal = principal
            self.nodes = {str(n["node_id"]): n for n in nodes}
            if recover:
                self.flush_results()
                self._locked = False
            self.flush_daemon_events()
            return nodes
        except DashboardAuthError as exc:
            if generation == self.generation:
                self.lock(str(exc), clear_token=exc.status in (401, 403))
            raise

    def owns(self, cfg) -> bool:
        if not self.active:
            return False
        if cfg.backend_base and cfg.backend_base.rstrip("/") != self.api.base:
            return self.is_admin
        if self.is_admin:
            return True
        node = self.nodes.get(str(cfg.node_id or ""))
        return bool(node and (node.get("owner_user_id") == self.principal.get("user_id") or (node.get("approval_status") == "pending" and node.get("requested_by_user_id") == self.principal.get("user_id"))))

    def apply_node(self, cfg, node: dict) -> None:
        cfg.node_id = str(node["node_id"])
        cfg.mt5_login = int(node.get("mt5_login") or 0)
        # 后端记着的券商服务器名（节点鉴权首包上报，落库后一直可查）。
        # 后端没给时不覆盖：可能只是这一版后端还没这个字段，本机终端探测
        # 读到的实时服务器名更准，不能被空值冲掉。
        server = str(node.get("mt5_server") or "").strip()
        if server:
            cfg.mt5_server = server
        cfg.owner_user_id = node.get("owner_user_id")
        cfg.requested_by_user_id = node.get("requested_by_user_id")
        cfg.approval_status = str(node.get("approval_status") or ("approved" if node.get("enabled") else "pending"))
        cfg.enabled = bool(node.get("enabled"))
        cfg.credential_generation = int(node.get("credential_generation") or 0)
        cfg.legacy_allowed = bool(node.get("legacy_allowed", False))
        cfg.has_credential = bool(node.get("has_credential", False))
        cfg.backend_base = self.api.base

    def enrolled(self, mt5_login: int, name: str, server: str = "") -> dict:
        self.check()
        node = self.api.enroll(self.token, mt5_login, name, server)
        self.nodes[str(node["node_id"])] = node
        return node

    def perform(self, action: str, cfg, fn, *, params: dict | None = None, generation: int | None = None):
        """先提交严格意图审计，再执行本机操作；结果失败落本地队列并锁新操作。"""
        with self._mutex:
            self.check(generation)
            if cfg is not None and not self.owns(cfg):
                raise DashboardAuthError("无权操作该实例", status=403)
            foreign_backend = bool(cfg is not None and cfg.backend_base and cfg.backend_base.rstrip("/") != self.api.base)
            if foreign_backend and action not in {"bind_instance", "view_status"}:
                raise DashboardAuthError("实例属于其他后端，需管理员先明确重新绑定", status=403)
            ticket_generation = self.generation
            # 同线程嵌套的同一动作共用已取得意图；独立异步线程必须重新准入。
            if action in {"start", "restart"} or (action == "set_daemon" and (params or {}).get("enabled")):
                if cfg is None or not cfg.node_id or cfg.approval_status != "approved" or not cfg.enabled:
                    raise DashboardAuthError("节点需管理员审核开通后才能启动或开启守护", status=403)
                require_binding(cfg.exe_path)
            if getattr(self._local, "operation", None) == (action, cfg.id if cfg else None):
                return fn()
            payload = {"request_id": str(uuid4()), "action": action, "params": params or {}}
            if cfg is not None:
                payload["instance_id"] = cfg.id
                if action == "bind_instance" and (params or {}).get("node_id"):
                    payload["node_id"] = str(params["node_id"])
                elif cfg.node_id and not foreign_backend:
                    payload["node_id"] = str(cfg.node_id)
            try:
                ticket = self.api.authorize(self.token, payload)
            except DashboardAuthError as exc:
                if exc.status in (0, 401) or exc.status >= 500:
                    self.lock(str(exc), clear_token=exc.status == 401)
                raise
            self.check(ticket_generation)
            operation_id = int(ticket["operation_id"])
            actor = int(self.principal["user_id"])
            api, token = self.api, self.token
        result = "fail"
        previous_operation = getattr(self._local, "operation", None)
        previous_generation = getattr(self._local, "generation", None)
        self._local.generation = ticket_generation
        self._local.operation = (action, cfg.id if cfg else None)
        try:
            self.check(ticket_generation)
            value = fn()
            result = "fail" if value is False or getattr(value, "ok", True) is False else "ok"
            return value
        finally:
            self._local.operation = previous_operation
            self._local.generation = previous_generation
            record = {"backend_base": api.base, "operation_id": operation_id, "actor_user_id": actor, "result": result, "params": {"error_code": "local_operation_failed"} if result == "fail" else {}}
            try:
                api.result(token, operation_id, {"result": result, "params": record["params"]})
            except DashboardAuthError as exc:
                with self._mutex:
                    self.pending_results.append(record)
                    save_audit_queue(self.pending_results)
                self.lock("操作已执行，但结果审计尚未同步，请重新登录", clear_token=exc.status == 401)

    def flush_results(self) -> None:
        for record in list(self.pending_results):
            if not record.get("backend_base"):
                raise DashboardAuthError("旧审计队列缺少后端身份，请管理员核对来源后恢复")
            if record["backend_base"].rstrip("/") != self.api.base:
                raise DashboardAuthError("存在其他后端尚未同步的审计，请返回原后端登录完成同步")
            if record.get("actor_user_id") != self.principal.get("user_id") and not self.is_admin:
                self.lock("存在其他账号尚未同步的审计，请该账号或管理员登录")
                raise DashboardAuthError("请原操作账号或管理员同步未完成审计")
            self.api.result(self.token, int(record["operation_id"]), {"result": record["result"], "params": record.get("params", {})})
            self.pending_results.remove(record)
            save_audit_queue(self.pending_results)

    def flush_daemon_events(self) -> None:
        with _DAEMON_QUEUE_LOCK:
            events = load_daemon_events()
            for record in list(events):
                if record.get("backend_base", "").rstrip("/") != self.api.base:
                    continue
                if record["actor_user_id"] != self.principal.get("user_id") and not self.is_admin:
                    continue
                payload = {k: v for k, v in record.items() if k not in {"actor_user_id", "backend_base"}}
                self.api.replay_daemon_event(self.token, payload)
                events.remove(record)
                save_daemon_events(events)

    def credential(self, cfg, kind: str, token: str = "") -> dict:
        self.check()
        generation = self.generation
        if not self.owns(cfg):
            raise DashboardAuthError("无权操作该节点", status=403)
        payload = {"node_id": str(cfg.node_id)}
        if kind == "verify":
            payload["token"] = token
        if kind == "rotate":
            payload["expected_generation"] = cfg.credential_generation
        api, jwt = self.api, self.token
        result = api.credential(jwt, kind, payload)
        self.check(generation)
        return result

    def _write_local_token(self, cfg, text: str, token: str) -> None:
        """把专属令牌写回实例目录的 .env（面板内部动作，不惊动用户）。"""
        import env_file as ef

        updates = {
            "NODE_TOKEN": token,
            "MANAGER_WS_URL": ef.ws_url_from_backend_base(self.api.base),
            "DASHBOARD_EXPECTED_MT5_LOGIN": str(cfg.mt5_login),
        }
        self.perform(
            "edit_env",
            cfg,
            lambda: ef.write_env(cfg.cwd, ef.update_env_text(text, updates)),
        )

    def _rotate_local_credential(self, cfg, text: str) -> CredentialRepair:
        """轮换一枚专属令牌。

        rotate 要带 `expected_generation`，后端据此做乐观锁：期间若有人
        （另一个面板 / 管理员）动过凭证，就会 409。这里先用后端最新的
        generation 重试一次，把「别人先动了」这种竞态也自动消化掉。
        """
        for attempt in (0, 1):
            try:
                issued = self.credential(cfg, "rotate")
            except DashboardAuthError as exc:
                if exc.status != 409 or attempt:
                    return CredentialRepair(
                        ok=False,
                        reason="节点凭证状态已变化，请刷新后重试",
                    )
                node = self.nodes.get(str(cfg.node_id or ""))
                if node is None:
                    return CredentialRepair(ok=False, reason="节点已不可访问")
                latest = int(node.get("credential_generation") or 0)
                if latest == cfg.credential_generation:
                    return CredentialRepair(
                        ok=False, reason="节点凭证状态已变化，请刷新后重试"
                    )
                cfg.credential_generation = latest
                continue
            token = str(issued.get("token") or "")
            if not token:
                return CredentialRepair(ok=False, reason="后端未返回专属令牌")
            try:
                self._write_local_token(cfg, text, token)
            except (DashboardAuthError, OSError) as exc:
                return CredentialRepair(ok=False, reason=f"写入节点配置失败：{exc}")
            cfg.credential_generation = int(issued.get("generation") or cfg.credential_generation)
            cfg.has_credential = True
            cfg.legacy_allowed = False
            node = self.nodes.get(str(cfg.node_id or ""))
            if node is not None:
                node.update({
                    "credential_generation": cfg.credential_generation,
                    "has_credential": True,
                    "legacy_allowed": False,
                })
            return CredentialRepair(ok=True, token=token, reissued=True)
        return CredentialRepair(ok=False, reason="节点凭证状态已变化，请刷新后重试")

    def _issue_local_credential(self, cfg, text: str, *, reissue: bool) -> CredentialRepair:
        """后端签发 / 轮换一枚专属令牌，并写回本机 .env。

        `reissue=True` 表示后端已经签过一枚、只是本机丢了 —— 必须走 rotate
        显式作废旧令牌（issue 会被后端以 409 挡住）。轮换是不可逆的
        （旧令牌即刻失效），但**本机令牌本就已经不可用**，不轮换就永远起不来，
        因此这条路径不再需要用户确认。
        """
        if reissue:
            return self._rotate_local_credential(cfg, text)
        try:
            issued = self.credential(cfg, "issue")
        except DashboardAuthError as exc:
            # issue 撞上「已签发」= 本机令牌丢失：改走 rotate。
            if exc.status == 409:
                return self._rotate_local_credential(cfg, text)
            reason = {
                403: "节点归属校验未通过，请联系管理员确认该节点归属",
                404: "节点已不存在，请重新导入",
            }.get(exc.status, str(exc))
            return CredentialRepair(ok=False, reason=reason)
        token = str(issued.get("token") or "")
        if not token:
            return CredentialRepair(ok=False, reason="后端未返回专属令牌")
        try:
            self._write_local_token(cfg, text, token)
        except (DashboardAuthError, OSError) as exc:
            return CredentialRepair(ok=False, reason=f"写入节点配置失败：{exc}")
        cfg.credential_generation = int(issued.get("generation") or cfg.credential_generation)
        cfg.has_credential = True
        cfg.legacy_allowed = False
        node = self.nodes.get(str(cfg.node_id or ""))
        if node is not None:
            node.update({
                "credential_generation": cfg.credential_generation,
                "has_credential": True,
                "legacy_allowed": False,
            })
        return CredentialRepair(ok=True, token=token)

    def node_is_online_elsewhere(self, cfg, mp=None) -> bool:
        """该节点当前是否有一台**别的**机器在跑。

        后端那枚专属令牌正被别处使用时，轮换会作废它 —— 那台机器下一次
        重连就会失败，等于把别人的节点踢下线。这种情况不能自动轮换。

        `mp` 是本机的实例：若状态口显示正是本机这台在连（ws 已鉴权、
        节点编号一致），那就是「我们自己」，轮换后重启自己即可。
        """
        node = self.nodes.get(str(cfg.node_id or ""))
        if not node or node.get("status") != "online":
            return False
        if mp is not None:
            runtime = getattr(mp, "runtime", None)
            if runtime is not None and runtime.ws_state == "authenticated" \
                    and str(runtime.node_id or "") == str(cfg.node_id or ""):
                return False
        return True

    def rotation_allowed(self, cfg, mp=None) -> bool:
        """能不能安全地轮换这枚专属令牌。"""
        return not self.node_is_online_elsewhere(cfg, mp)

    def ensure_local_credential(self, cfg, *, rotate_allowed: bool = True) -> CredentialRepair:
        """确保本机 .env 里有一枚可用的专属令牌；失效就自动重签。

        这是「客户只管导入 + 启动」的落点：专属令牌是面板与后端之间的
        内部凭据，本机丢了、过期了、被旧全局令牌覆盖了，都由面板自己修，
        不把令牌管理甩给用户。

        轮换会作废后端那一枚旧令牌 —— 只有在它已经无法被本机使用时才做，
        否则没有副作用可言。`rotate_allowed=False` 用于「后端那边这枚令牌
        正被另一台机器用着」的情况：这时轮换会把别人的节点踢下线，
        必须拒绝。
        """
        import env_file as ef

        self.check()
        node = self.nodes.get(str(cfg.node_id or ""))
        if not node or cfg.approval_status != "approved" or not cfg.enabled:
            return CredentialRepair(ok=False, reason="节点尚未审核开通")
        text, _ = ef.read_env(cfg.cwd)
        values = env_values(text)
        local_token = str(values.get("NODE_TOKEN") or "")
        if cfg.legacy_allowed and not local_token.startswith("ndv1."):
            # 旧节点沿用全局令牌：不签专属令牌，也不改它的令牌。
            if not local_token:
                return CredentialRepair(ok=False, reason="节点接入令牌缺失")
            return CredentialRepair(ok=True, token=local_token)
        if local_token.startswith("ndv1."):
            try:
                verified = self.credential(cfg, "verify", local_token)
            except DashboardAuthError as exc:
                if exc.status not in (401, 403, 404, 409):
                    self.lock(str(exc))
                    raise
                verified = {}
            if (verified.get("valid") is True
                    and int(verified.get("mt5_login") or 0) == cfg.mt5_login):
                cfg.credential_generation = int(verified.get("generation") or cfg.credential_generation)
                return CredentialRepair(ok=True, token=local_token)
            # 本机这枚专属令牌已被后端作废或绑错了账号 → 自动轮换一枚新的。
            if not rotate_allowed:
                return CredentialRepair(ok=False, reason=ROTATION_BLOCKED_REASON)
            return self._issue_local_credential(cfg, text, reissue=True)
        # 本机没有专属令牌（还是旧全局令牌，或压根是空的）：
        # 后端没签过就签一枚；签过（= 本机那枚丢了）就轮换一枚。
        if cfg.has_credential and not rotate_allowed:
            return CredentialRepair(ok=False, reason=ROTATION_BLOCKED_REASON)
        return self._issue_local_credential(cfg, text, reissue=bool(cfg.has_credential))

    def sync_credential(self, cfg) -> bool:
        """登录 / 定时复核时同步开通状态并自愈本机专属令牌。

        凭证无效只阻止该实例，保留用户会话；令牌本身由面板自动修好，
        用户不需要参与令牌管理。
        """
        try:
            return self.ensure_local_credential(cfg).ok
        except DashboardAuthError as exc:
            if exc.status in (401, 403, 404, 409):
                return False
            self.lock(str(exc))
            raise

    def prepare_start(self, mp) -> None:
        """启动前把专属令牌准备好；本机令牌失效由面板自动重签。

        旧节点接入令牌缺失是人工配置问题，只能报错；专属令牌则是面板的
        内部凭据，丢了就自动轮换，用户不需要参与。
        """
        import env_file as ef
        self.check()
        generation = self.generation
        cfg = mp.cfg
        node = self.nodes.get(str(cfg.node_id or ""))
        if not node:
            raise DashboardAuthError("节点已不可访问", status=403)
        self.apply_node(cfg, node)
        if cfg.approval_status != "approved" or not cfg.enabled or cfg.mt5_login <= 0:
            raise DashboardAuthError("节点尚未审核开通", status=403)
        text, values = ef.read_env(cfg.cwd)
        values = env_values(text)
        token = str(values.get("NODE_TOKEN") or "")
        if cfg.legacy_allowed and not token.startswith("ndv1."):
            if not token:
                raise DashboardAuthError("旧节点接入令牌缺失，请管理员恢复配置", status=409)
        else:
            repair = self.ensure_local_credential(
                cfg, rotate_allowed=self.rotation_allowed(cfg, mp)
            )
            if not repair.ok:
                raise DashboardAuthError(repair.reason or "节点专属令牌不可用", status=409)
            token = repair.token
            if repair.reissued:
                # 自愈时已经把新令牌写进 .env 了；重读一次，避免用旧文本覆盖回去。
                text, _ = ef.read_env(cfg.cwd)
        self.check(generation)
        data = self.api.daemon_grant(self.token, str(cfg.node_id))
        self.check(generation)
        ef.write_env(cfg.cwd, ef.update_env_text(text, {"NODE_TOKEN": token, "MANAGER_WS_URL": ef.ws_url_from_backend_base(self.api.base), "DASHBOARD_EXPECTED_MT5_LOGIN": str(cfg.mt5_login)}))
        mp.daemon_grant = DaemonGrant(self.api, data)
        mp.grant_configuration = (cfg.exe_path, cfg.cwd, str(cfg.node_id), cfg.mt5_login)

    def grant_for_recovery(self, mp) -> None:
        if mp.cfg.approval_status == "approved" and mp.cfg.enabled and mp.cfg.node_id:
            generation = self.generation
            self.check(generation)
            data = self.api.daemon_grant(self.token, str(mp.cfg.node_id))
            self.check(generation)
            mp.daemon_grant = DaemonGrant(self.api, data)
            mp.grant_configuration = (mp.cfg.exe_path, mp.cfg.cwd, str(mp.cfg.node_id), mp.cfg.mt5_login)


class DaemonGrant:
    """只存于内存的守护授权，JWT 失效后仍可复核与上报自动重启。"""
    def __init__(self, api: DashboardAPI, data: dict) -> None:
        self.api = api
        self.token = str(data["grant_token"])
        self.node_id = str(data["node_id"])
        self.mt5_login = int(data["mt5_login"])
        self.generation = int(data["generation"])
        self.grant_operation_id = int(data.get("grant_operation_id") or 0)
        self.actor_user_id = int(data.get("actor_user_id") or 0)
        self.allowed = True
        self.events: list[dict] = []

    def check(self) -> bool:
        if not self.allowed:
            return False
        try:
            data = self.api.check_grant(self.token)
            self.allowed = bool(data.get("ok") and str(data.get("node_id")) == self.node_id and int(data.get("mt5_login") or 0) == self.mt5_login and int(data.get("generation") or 0) == self.generation)
            if self.allowed:
                self.flush()
        except DashboardAuthError as exc:
            if exc.status in (401, 403, 404, 409):
                self.allowed = False
        return self.allowed

    def restart(self, fn) -> bool:
        if not self.check():
            return False
        request_id = str(uuid4())
        intent = {"request_id": request_id, "phase": "intent", "params": {"action": "daemon_restart"}}
        try:
            self.api.daemon_event(self.token, intent)
        except DashboardAuthError as exc:
            if exc.status in (401, 403, 404, 409):
                self.allowed = False
                return False
            self._append(intent)
        result = "fail"
        try:
            fn()
            result = "ok"
            return True
        finally:
            self._append({"request_id": request_id, "phase": "result", "result": result, "params": {}})
            self.flush()

    def _append(self, event: dict) -> None:
        record = {"backend_base": self.api.base, "grant_operation_id": self.grant_operation_id, "actor_user_id": self.actor_user_id, **event}
        with _DAEMON_QUEUE_LOCK:
            records = load_daemon_events()
            if record not in records:
                records.append(record)
                save_daemon_events(records)
        self.events.append(event)

    def flush(self) -> None:
        while self.events and self.allowed:
            try:
                self.api.daemon_event(self.token, self.events[0])
            except DashboardAuthError as exc:
                if exc.status in (401, 403, 404, 409):
                    self.allowed = False
                return
            event = self.events.pop(0)
            with _DAEMON_QUEUE_LOCK:
                records = load_daemon_events()
                records = [r for r in records if not (r.get("backend_base") == self.api.base and r.get("grant_operation_id") == self.grant_operation_id and r.get("request_id") == event["request_id"] and r.get("phase") == event["phase"])]
                save_daemon_events(records)
