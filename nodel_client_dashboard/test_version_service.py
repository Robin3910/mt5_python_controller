"""面板版本通道：后端地址推导、.env 解析与入口解析优先级。"""
from __future__ import annotations

import version_service as vs


def test_backend_base_from_ws_url_maps_scheme_and_drops_path():
    assert vs.backend_base_from_ws_url("ws://159.75.33.185/ws/node") == "http://159.75.33.185"
    assert vs.backend_base_from_ws_url("wss://hub.example.com/ws/node") == "https://hub.example.com"
    # 带端口与查询串同样只保留 scheme + host
    assert vs.backend_base_from_ws_url("ws://127.0.0.1:8000/ws/node?x=1") == "http://127.0.0.1:8000"
    # 已经是 http(s) 的也接受，便于用户直接填后端地址
    assert vs.backend_base_from_ws_url("https://hub.example.com/") == "https://hub.example.com"


def test_backend_base_from_ws_url_rejects_garbage():
    for bad in ("", "   ", "not a url", "ftp://host/path", "/ws/node"):
        assert vs.backend_base_from_ws_url(bad) == "", bad


def test_parse_env_text_ignores_comments_and_strips_quotes():
    env = vs.parse_env_text(
        "\ufeff# 注释\n"
        "MANAGER_WS_URL=ws://1.2.3.4/ws/node\n"
        "\n"
        '  NODE_TOKEN = "abc123"  \n'
        "EMPTY=\n"
        "no_equals_line\n"
    )
    assert env["MANAGER_WS_URL"] == "ws://1.2.3.4/ws/node"
    assert env["NODE_TOKEN"] == "abc123"
    assert env["EMPTY"] == ""
    assert "no_equals_line" not in env


def test_read_node_env_returns_empty_when_missing(tmp_path):
    assert vs.read_node_env(tmp_path) == {}
    assert vs.read_node_env("") == {}


def _write_env(tmp_path, url: str = "ws://1.2.3.4/ws/node", token: str = "tok-from-env"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / ".env").write_text(
        f"MANAGER_WS_URL={url}\nNODE_TOKEN={token}\n", encoding="utf-8"
    )
    return str(tmp_path)


def test_target_from_env_reports_base_and_token(tmp_path):
    """「连接配置」对话框的自动发现列表直接读这个结果。"""
    cwd = _write_env(tmp_path)
    found = vs.target_from_env(cwd)
    assert found.base == "http://1.2.3.4"
    assert found.token == "tok-from-env"
    assert found.source == "实例 .env"
    assert found.ready


def test_target_from_env_not_ready_when_env_missing(tmp_path):
    found = vs.target_from_env(tmp_path / "no-such-dir")
    assert not found.ready
    assert found.base == ""
    assert found.token == ""


def test_resolve_backend_prefers_manual_panel_config(tmp_path):
    cwd = _write_env(tmp_path)
    target = vs.resolve_backend(
        {"backend_base": "https://manual.example.com", "node_token": "manual-token"}, [cwd]
    )
    assert target.base == "https://manual.example.com"
    assert target.token == "manual-token"
    assert target.source == "面板配置"
    assert target.ready


def test_resolve_backend_falls_back_to_instance_env(tmp_path):
    cwd = _write_env(tmp_path)
    target = vs.resolve_backend({}, [cwd])
    assert target.base == "http://1.2.3.4"
    assert target.token == "tok-from-env"
    assert target.ready


def test_resolve_backend_mixes_manual_base_with_env_token(tmp_path):
    """常见场景：后端换了域名，只在面板改地址，令牌仍沿用节点 .env。"""
    cwd = _write_env(tmp_path)
    target = vs.resolve_backend({"backend_base": "https://new.example.com"}, [cwd])
    assert target.base == "https://new.example.com"
    assert target.token == "tok-from-env"
    assert target.ready


def test_resolve_backend_skips_instances_without_env(tmp_path):
    """多实例时逐个找：前面的实例还没配 .env 不应挡住后面已配好的。"""
    empty = tmp_path / "no-env"
    empty.mkdir()
    configured = _write_env(tmp_path / "configured")

    target = vs.resolve_backend({}, [str(empty), configured])
    assert target.base == "http://1.2.3.4"
    assert target.token == "tok-from-env"
    assert target.ready


def test_resolve_backend_combines_partial_env_across_instances(tmp_path):
    """地址与令牌可以来自不同实例——只要凑齐就能用。"""
    only_url = tmp_path / "only-url"
    only_url.mkdir()
    (only_url / ".env").write_text("MANAGER_WS_URL=ws://5.6.7.8/ws/node\n", encoding="utf-8")

    only_token = tmp_path / "only-token"
    only_token.mkdir()
    (only_token / ".env").write_text("NODE_TOKEN=tok-2\n", encoding="utf-8")

    target = vs.resolve_backend({}, [str(only_url), str(only_token)])
    assert target.base == "http://5.6.7.8"
    assert target.token == "tok-2"
    assert target.ready


def test_resolve_backend_not_ready_without_any_source():
    target = vs.resolve_backend({}, [])
    assert not target.ready
    assert target.base == ""
    assert target.token == ""


def test_verify_sha256_matches_and_skips_when_absent(tmp_path):
    f = tmp_path / "pkg.zip"
    f.write_bytes(b"hello world")
    digest = vs.sha256_of(f)
    assert vs.verify_sha256(f, digest)
    assert vs.verify_sha256(f, digest.upper())
    assert not vs.verify_sha256(f, "0" * 64)
    # 后端没给校验和时不阻断更新
    assert vs.verify_sha256(f, "")
