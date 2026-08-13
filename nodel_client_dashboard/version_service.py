"""面板与后端之间的版本通道：后端地址发现、版本查询、安装包下载与校验。

鉴权用的是节点令牌 NODE_TOKEN（与 node_client 的 .env 同一份），它只被后端授权
访问「查当前发布版本」和「下载安装包」两个只读接口。

后端地址的解析顺序是「面板手工配置优先，其次从实例 .env 自动发现」：新装机器上
还没有任何实例时 .env 无从读起，而多个实例也可能连不同的后端。

只用标准库：面板要打成单文件 exe，不引入 requests / httpx 之类的新依赖。
"""
from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse, urlunparse

DEFAULT_TIMEOUT = 15
DOWNLOAD_TIMEOUT = 300
_CHUNK = 1 << 16


class VersionServiceError(Exception):
    """后端不可达、鉴权失败或返回异常时抛出，消息可直接展示给用户。"""


@dataclass
class BackendTarget:
    """一个可用的后端访问入口。"""
    base: str
    token: str
    source: str = ""       # 该配置来自哪里，用于界面提示

    @property
    def ready(self) -> bool:
        return bool(self.base and self.token)


def backend_base_from_ws_url(ws_url: str) -> str:
    """从节点的 MANAGER_WS_URL 推导后端 HTTP 基址。

    ws://host/ws/node -> http://host；wss 对应 https。路径整段丢弃，因为
    /ws/node 是 WebSocket 端点，REST 接口挂在站点根下。
    """
    raw = (ws_url or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    scheme = {"ws": "http", "wss": "https", "http": "http", "https": "https"}.get(
        parsed.scheme.lower(), ""
    )
    if not scheme or not parsed.netloc:
        return ""
    return urlunparse((scheme, parsed.netloc, "", "", "", "")).rstrip("/")


def parse_env_text(text: str) -> dict[str, str]:
    """解析 .env 文本；忽略注释与空行，去掉值两侧的引号。"""
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        s = line.strip().lstrip("\ufeff")
        if not s or s.startswith("#") or "=" not in s:
            continue
        key, _, value = s.partition("=")
        v = value.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[key.strip()] = v
    return out


def read_node_env(cwd: str | Path) -> dict[str, str]:
    """读取实例工作目录下的 .env；不存在或读不了返回空字典。"""
    if not cwd:
        return {}
    path = Path(cwd) / ".env"
    if not path.is_file():
        return {}
    try:
        return parse_env_text(path.read_text(encoding="utf-8-sig"))
    except OSError:
        return {}


def target_from_env(cwd: str | Path) -> BackendTarget:
    """从某个实例的 .env 推导后端入口。"""
    env = read_node_env(cwd)
    base = backend_base_from_ws_url(env.get("MANAGER_WS_URL", ""))
    token = (env.get("NODE_TOKEN") or "").strip()
    return BackendTarget(base=base, token=token, source="实例 .env")


def resolve_backend(panel_cfg: dict | None, cwds: list[str] | None = None) -> BackendTarget:
    """解析出可用的后端入口：面板手工配置优先，缺项时回落到实例 .env。

    地址与令牌各自独立回落 —— 常见场景是管理员只在面板里改了后端地址（比如
    换了域名），令牌仍然沿用节点 .env 里的那一份。
    """
    cfg = panel_cfg or {}
    base = str(cfg.get("backend_base") or "").strip().rstrip("/")
    token = str(cfg.get("node_token") or "").strip()
    if base and token:
        return BackendTarget(base=base, token=token, source="面板配置")

    for cwd in cwds or []:
        found = target_from_env(cwd)
        if not base and found.base:
            base = found.base
        if not token and found.token:
            token = found.token
        if base and token:
            break

    if not base or not token:
        return BackendTarget(base=base, token=token, source="")
    source = "面板配置" if (cfg.get("backend_base") and cfg.get("node_token")) else "面板配置 + 实例 .env"
    return BackendTarget(base=base, token=token, source=source)


def _request(target: BackendTarget, path: str, timeout: int):
    url = f"{target.base}{path}"
    req = urllib.request.Request(url, headers={"X-Node-Token": target.token})
    try:
        return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310 - 地址来自本机配置
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise VersionServiceError("节点令牌无效，请检查 NODE_TOKEN") from e
        if e.code == 404:
            raise VersionServiceError("后端没有该版本的安装包") from e
        raise VersionServiceError(f"后端返回 HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise VersionServiceError(f"无法连接后端 {target.base}：{e.reason}") from e
    except OSError as e:
        raise VersionServiceError(f"无法连接后端 {target.base}：{e}") from e


def fetch_current_version(target: BackendTarget, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """查询后端当前发布的客户端版本；尚未发布任何版本时返回空字典。"""
    if not target.ready:
        raise VersionServiceError("尚未配置后端地址或节点令牌")
    with _request(target, "/api/client-versions/current", timeout) as resp:
        if resp.status == 204:
            return {}
        body = resp.read().decode("utf-8", "replace")
    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        raise VersionServiceError("后端返回的版本信息无法解析") from e
    return data if isinstance(data, dict) else {}


def fetch_available_versions(
    target: BackendTarget, timeout: int = DEFAULT_TIMEOUT
) -> tuple[list[dict], str]:
    """列出后端所有可下载的版本（新版在前），返回 (清单, 当前发布版本号)。

    供「导入节点」与「更新到指定版本」选版本用；后端尚无任何安装包时返回空清单。
    """
    if not target.ready:
        raise VersionServiceError("尚未配置后端地址或节点令牌")
    with _request(target, "/api/client-versions/available", timeout) as resp:
        body = resp.read().decode("utf-8", "replace")
    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        raise VersionServiceError("后端返回的版本清单无法解析") from e
    if not isinstance(data, dict):
        return [], ""
    items = data.get("items")
    return (items if isinstance(items, list) else []), str(data.get("current") or "")


def download_package(
    target: BackendTarget,
    version: str,
    dest: str | Path,
    *,
    timeout: int = DOWNLOAD_TIMEOUT,
) -> Path:
    """下载指定版本的安装包到 dest；返回落地路径。"""
    if not target.ready:
        raise VersionServiceError("尚未配置后端地址或节点令牌")
    out = Path(dest)
    out.parent.mkdir(parents=True, exist_ok=True)
    with _request(target, f"/api/client-versions/{version}/download", timeout) as resp:
        with out.open("wb") as f:
            while True:
                chunk = resp.read(_CHUNK)
                if not chunk:
                    break
                f.write(chunk)
    return out


def sha256_of(path: str | Path) -> str:
    """计算文件的 sha256（分块读，避免整包进内存）。"""
    digest = hashlib.sha256()
    with Path(path).open("rb") as f:
        while True:
            chunk = f.read(_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def verify_sha256(path: str | Path, expected: str) -> bool:
    """校验安装包完整性；后端没给校验和时视为通过。"""
    want = (expected or "").strip().lower()
    return True if not want else sha256_of(path) == want
