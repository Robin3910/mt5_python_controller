"""版本更新的端到端演练：起一个假后端，真跑一遍「下载 → 校验 → 解压 → 覆盖」。

假后端提供两个接口：
    GET /api/client-versions/available —— 版本清单
    GET /api/client-versions/{version}/download —— 安装包字节流

安装包是用真客户端源码打的 zip（含 node_client.exe 与 version.txt），
覆盖走的是真 `client_deploy.apply_package`，所以这条链路验的是真行为。

跑法：
    python e2e_version_update.py
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dashboard_bridge import DashboardBridge  # noqa: E402
from e2e_support import FakeApi, FakeSession  # noqa: E402
from models import InstanceConfig  # noqa: E402

VERSION = "2.0.0-20260102030405"
REPO = Path(__file__).resolve().parent.parent
CLIENT_SRC = REPO / "node_client" / "node_client.py"


def build_package(dest: Path) -> tuple[Path, str]:
    """打一个假的客户端安装包，返回 (zip 路径, sha256)。

    包里必须带 `client_capabilities.json`：真客户端的账户绑定能力清单。
    `client_deploy` / `client_binding` 会校验 exe 的 sha256 与清单一致 ——
    少了清单会被判成「旧客户端不支持安全账户绑定」而拒绝更新，这正是
    我们想要的把关行为。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    exe_bytes = b"fake-node-client-binary-v2"
    manifest = json.dumps(
        {
            "account_binding": True,
            "executable_sha256": hashlib.sha256(exe_bytes).hexdigest(),
        },
        ensure_ascii=False,
    )
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("node_client.exe", exe_bytes)
        zf.writestr("client_capabilities.json", manifest)
        zf.writestr("version.txt", f"{VERSION}\n")
        zf.writestr(".env.example", "MANAGER_WS_URL=\nNODE_TOKEN=\n")
    digest = hashlib.sha256()
    with dest.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            digest.update(chunk)
    return dest, digest.hexdigest()


def make_backend(pkg: Path, sha: str):
    """起一个只实现版本相关接口的假后端。"""
    payload = pkg.read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def _json(self, code: int, body: dict) -> None:
            raw = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):  # noqa: N802
            if self.path.startswith("/api/client-versions/available"):
                self._json(200, {
                    "items": [{
                        "version": VERSION, "size": len(payload), "sha256": sha,
                        "notes": "e2e 假版本", "is_current": True,
                    }],
                    "current": VERSION,
                })
                return
            if self.path.startswith("/api/client-versions/current"):
                self._json(200, {
                    "version": VERSION, "size": len(payload), "sha256": sha,
                    "notes": "e2e 假版本",
                })
                return
            if self.path.endswith("/download"):
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            self._json(404, {"detail": "not found"})

        def log_message(self, *_args):
            return None

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def main() -> int:
    work = Path(__file__).resolve().parent / "_e2e_ver"
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    pkg, sha = build_package(work / f"node_client-{VERSION}.zip")
    server, port = make_backend(pkg, sha)
    base = f"http://127.0.0.1:{port}"
    print(f"[e2e] 假后端 {base}")

    # —— 造一个装了旧客户端的实例 ——
    instance_dir = work / "prod-01"
    instance_dir.mkdir(parents=True, exist_ok=True)
    v1_exe = b"fake-node-client-binary-v1"
    (instance_dir / "node_client.exe").write_bytes(v1_exe)
    (instance_dir / "client_capabilities.json").write_text(
        json.dumps(
            {
                "account_binding": True,
                "executable_sha256": hashlib.sha256(v1_exe).hexdigest(),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (instance_dir / "version.txt").write_text("1.0.0-20260101000000\n", encoding="utf-8")
    (instance_dir / ".env").write_text("MANAGER_WS_URL=ws://x/ws/node\nNODE_TOKEN=t\n", encoding="utf-8")

    bridge = DashboardBridge()
    session = FakeSession()
    session.api = FakeApi()
    session.api.base = base
    bridge.session = session
    bridge.manager.session = session

    cfg = InstanceConfig.create(
        name="mt5-prod-01",
        exe_path=str(instance_dir / "node_client.exe"),
        cwd=str(instance_dir),
    )
    cfg.node_id = "nd_9f21"
    cfg.mt5_login = 50231488
    cfg.approval_status = "approved"
    cfg.enabled = True
    bridge.manager.add(cfg)

    try:
        # —— ① 检测 ——
        detect = bridge._version_update.detect()
        print(f"[e2e] 检测: ok={detect['ok']} published={detect['published']} "
              f"versions={[v['version'] for v in detect['versions']]}")
        assert detect["ok"] and detect["published"] == VERSION, detect

        # —— ② 实例清单 ——
        inst = bridge._version_update.instances()
        item = inst["instances"][0]
        print(f"[e2e] 实例: {item['name']} v{item['version']} pending={inst['pending']}")
        assert item["version"] == "1.0.0-20260101000000", item

        # —— ③ 判定 ——
        prep = bridge._version_update.prepare({"version": VERSION, "ids": [cfg.id]})
        print(f"[e2e] 判定: kind={prep['rows'][0]['kind']} label={prep['rows'][0]['label']}")
        assert prep["rows"][0]["kind"] == "upgrade", prep

        # —— ④ 真跑一次更新 ——
        started = bridge._version_update.start_update({
            "version": VERSION, "sha256": sha, "ids": [cfg.id],
        })
        print(f"[e2e] 启动更新: {started}")
        assert started["ok"], started

        task_id = started["task_id"]
        for _ in range(200):
            snap = bridge._version_update.task(task_id).snapshot()
            if snap["done"]:
                break
            time.sleep(0.1)
        print(f"[e2e] 任务结束: error={snap['error']!r} result={snap['result']}")
        assert not snap["error"], snap["error"]

        # —— ⑤ 校验覆盖结果 ——
        new_version = (instance_dir / "version.txt").read_text(encoding="utf-8").strip()
        exe_bytes = (instance_dir / "node_client.exe").read_bytes()
        env_text = (instance_dir / ".env").read_text(encoding="utf-8")
        print(f"[e2e] 覆盖后版本: {new_version}")
        print(f"[e2e] 覆盖后 exe: {exe_bytes[:40]!r}")
        print(f"[e2e] .env 是否保留: {'NODE_TOKEN=t' in env_text}")
        print(f"[e2e] 备份目录: {[p.name for p in (instance_dir / '.backups').iterdir()]}")

        assert new_version == VERSION, f"版本没更新：{new_version}"
        assert exe_bytes == b"fake-node-client-binary-v2", exe_bytes
        assert "NODE_TOKEN=t" in env_text, ".env 被覆盖了！"
        assert (instance_dir / ".backups" / "1.0.0-20260101000000").is_dir(), "没有生成版本化备份"

        # —— ⑥ 回滚 ——
        rb = bridge._version_update.start_rollback({
            "version": "1.0.0-20260101000000", "ids": [cfg.id],
        })
        print(f"[e2e] 启动回滚: {rb}")
        assert rb["ok"], rb
        for _ in range(200):
            snap = bridge._version_update.task(rb["task_id"]).snapshot()
            if snap["done"]:
                break
            time.sleep(0.1)
        print(f"[e2e] 回滚结束: error={snap['error']!r}")
        assert not snap["error"], snap["error"]
        rolled = (instance_dir / "version.txt").read_text(encoding="utf-8").strip()
        print(f"[e2e] 回滚后版本: {rolled}")
        assert rolled == "1.0.0-20260101000000", rolled

        print("-" * 50)
        print("[ok] 版本更新 + 回滚全链路通过")
        return 0
    finally:
        bridge.stop()
        server.shutdown()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
