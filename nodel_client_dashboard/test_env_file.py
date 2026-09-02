"""node_client .env 读写与字段校验测试。"""
from pathlib import Path

import env_file as ef

_SAMPLE = """\
# ============ 后端连接 ============
# 后端网关 WebSocket 地址
MANAGER_WS_URL=ws://1.2.3.4/ws/node
NODE_TOKEN=old-token

# timings (seconds)
HEARTBEAT_INTERVAL=15

MY_CUSTOM_KEY=keep-me
"""


def test_parse_env_text_ignores_comments_and_strips_quotes():
    env = ef.parse_env_text('\ufeffA=1\n# c\n\nB = "two"  \nC=\nno_equals\n')
    assert env == {"A": "1", "B": "two", "C": ""}


def test_update_env_text_preserves_comments_order_and_unknown_keys():
    out = ef.update_env_text(_SAMPLE, {"NODE_TOKEN": "new-token"})
    # 注释与自定义键必须原样留着 —— .env.example 的中文注释是主要说明来源
    assert "# ============ 后端连接 ============" in out
    assert "# 后端网关 WebSocket 地址" in out
    assert "MY_CUSTOM_KEY=keep-me" in out
    assert "NODE_TOKEN=new-token" in out
    assert "old-token" not in out
    # 改值不改位置
    lines = [ln for ln in out.splitlines() if ln and not ln.startswith("#")]
    assert lines[0].startswith("MANAGER_WS_URL=")
    assert lines[1].startswith("NODE_TOKEN=")


def test_update_env_text_appends_new_keys_at_end():
    out = ef.update_env_text(_SAMPLE, {"BRAND_NEW": "v"})
    assert out.splitlines()[-1] == "BRAND_NEW=v"
    assert ef.parse_env_text(out)["HEARTBEAT_INTERVAL"] == "15"


def test_update_env_text_from_empty_and_always_ends_with_newline():
    out = ef.update_env_text("", {"A": "1"})
    assert out == "A=1\n"


def test_update_env_text_skips_none_values():
    out = ef.update_env_text(_SAMPLE, {"NODE_TOKEN": None, "HEARTBEAT_INTERVAL": "30"})
    env = ef.parse_env_text(out)
    assert env["NODE_TOKEN"] == "old-token"
    assert env["HEARTBEAT_INTERVAL"] == "30"


def test_update_env_text_quotes_values_that_would_be_misread():
    out = ef.update_env_text("", {"A": " padded ", "B": "has#hash", "C": "plain"})
    assert 'A=" padded "' in out
    assert 'B="has#hash"' in out
    assert "C=plain" in out
    # 引号值回读后应还原原样
    env = ef.parse_env_text(out)
    assert env["A"] == " padded "
    assert env["B"] == "has#hash"


def test_read_write_env_round_trip(tmp_path: Path):
    assert ef.read_env(tmp_path) == ("", {})

    ef.write_env(tmp_path, _SAMPLE)
    text, env = ef.read_env(tmp_path)
    assert env["MANAGER_WS_URL"] == "ws://1.2.3.4/ws/node"

    ef.write_env(tmp_path, ef.update_env_text(text, {"NODE_TOKEN": "t2"}))
    _text2, env2 = ef.read_env(tmp_path)
    assert env2["NODE_TOKEN"] == "t2"
    assert env2["MY_CUSTOM_KEY"] == "keep-me"


def test_bool_helpers():
    for word in ("1", "true", "TRUE", "yes", "on"):
        assert ef.as_bool(word)
    for word in ("0", "false", "no", "", "off", None):
        assert not ef.as_bool(word)
    assert ef.bool_text(True) == "true"
    assert ef.bool_text(False) == "false"


# ----------------------------- 校验 -----------------------------

def _field(key: str) -> ef.EnvField:
    return next(f for f in ef.ENV_FIELDS if f.key == key)


def test_validate_required_and_types():
    assert ef.validate_field(_field("NODE_TOKEN"), "") != ""
    assert ef.validate_field(_field("NODE_TOKEN"), "tok") == ""
    assert ef.validate_field(_field("DEFAULT_SLIPPAGE"), "abc") != ""
    assert ef.validate_field(_field("DEFAULT_SLIPPAGE"), "20") == ""
    assert ef.validate_field(_field("STRATEGY_SAMPLE_INTERVAL"), "0.5") == ""
    assert ef.validate_field(_field("STRATEGY_SAMPLE_INTERVAL"), "x") != ""
    assert ef.validate_field(_field("ACCOUNT_REPORT_INTERVAL"), "0.5") == ""
    assert ef.validate_field(_field("ACCOUNT_REPORT_INTERVAL"), "1") == ""
    assert ef.validate_field(_field("ACCOUNT_REPORT_INTERVAL"), "x") != ""
    assert ef.validate_field(_field("LOG_LEVEL"), "info") == ""  # 大小写不敏感
    assert ef.validate_field(_field("LOG_LEVEL"), "VERBOSE") != ""
    # 非必填项留空不算错
    assert ef.validate_field(_field("DEFAULT_SLIPPAGE"), "") == ""


def test_validate_manager_ws_url_scheme():
    f = _field("MANAGER_WS_URL")
    assert ef.validate_field(f, "ws://1.2.3.4/ws/node") == ""
    assert ef.validate_field(f, "wss://hub.example.com/ws/node") == ""
    # http 是最容易犯的错：填了后端页面地址而不是网关地址
    assert "ws://" in ef.validate_field(f, "http://1.2.3.4/ws/node")
    assert ef.validate_field(f, "ws:///ws/node") != ""


def test_validate_env_collects_all_errors():
    errors = ef.validate_env({"MANAGER_WS_URL": "http://x/ws/node", "DEFAULT_MAGIC": "abc"})
    # 缺 NODE_TOKEN + 地址协议错 + 魔术号非整数
    assert len(errors) == 3


# ----------------------------- 导入辅助 -----------------------------

def test_ws_url_from_backend_base_is_inverse_of_discovery():
    import version_service as vs

    assert ef.ws_url_from_backend_base("http://1.2.3.4") == "ws://1.2.3.4/ws/node"
    assert ef.ws_url_from_backend_base("https://hub.example.com/") == "wss://hub.example.com/ws/node"
    assert ef.ws_url_from_backend_base("http://127.0.0.1:8000") == "ws://127.0.0.1:8000/ws/node"
    # 与自动发现互为逆运算
    base = "http://159.75.33.185"
    assert vs.backend_base_from_ws_url(ef.ws_url_from_backend_base(base)) == base


def test_ws_url_from_backend_base_rejects_garbage():
    for bad in ("", "   ", "ftp://host", "not a url"):
        assert ef.ws_url_from_backend_base(bad) == "", bad


def test_find_terminal_and_is_mt5_dir(tmp_path: Path):
    assert not ef.is_mt5_dir(tmp_path)
    assert ef.find_terminal(tmp_path) is None
    assert not ef.is_mt5_dir("")

    (tmp_path / "terminal64.exe").write_bytes(b"MZ")
    assert ef.is_mt5_dir(tmp_path)
    assert ef.find_terminal(tmp_path).name == "terminal64.exe"


def test_find_terminal_accepts_32bit_name(tmp_path: Path):
    (tmp_path / "terminal.exe").write_bytes(b"MZ")
    assert ef.find_terminal(tmp_path).name == "terminal.exe"


def test_build_initial_env_fills_address_and_token():
    out = ef.build_initial_env(
        _SAMPLE, ws_url="wss://hub.example.com/ws/node", node_token="tok"
    )
    env = ef.parse_env_text(out)
    assert env["MANAGER_WS_URL"] == "wss://hub.example.com/ws/node"
    assert env["NODE_TOKEN"] == "tok"
    # 模板里的注释与其它默认值要留着，新节点才有可读的配置说明
    assert "# ============ 后端连接 ============" in out
    assert env["HEARTBEAT_INTERVAL"] == "15"


def test_build_initial_env_without_template():
    out = ef.build_initial_env("", ws_url="ws://h/ws/node", node_token="t")
    assert ef.parse_env_text(out) == {"MANAGER_WS_URL": "ws://h/ws/node", "NODE_TOKEN": "t"}


def test_write_import_env_creates_when_missing(tmp_path: Path):
    overwritten = ef.write_import_env(
        tmp_path, _SAMPLE, ws_url="wss://hub.example.com/ws/node", node_token="tok"
    )
    assert overwritten is False
    _text, env = ef.read_env(tmp_path)
    assert env["MANAGER_WS_URL"] == "wss://hub.example.com/ws/node"
    assert env["NODE_TOKEN"] == "tok"
    assert env["HEARTBEAT_INTERVAL"] == "15"


def test_write_import_env_overwrites_existing(tmp_path: Path):
    (tmp_path / ".env").write_text(
        "MANAGER_WS_URL=ws://old/ws/node\nNODE_TOKEN=old-token\nOLD_ONLY_KEY=from-old-file\n",
        encoding="utf-8",
    )
    overwritten = ef.write_import_env(
        tmp_path, _SAMPLE, ws_url="wss://hub.example.com/ws/node", node_token="new-tok"
    )
    assert overwritten is True
    text, env = ef.read_env(tmp_path)
    assert env["MANAGER_WS_URL"] == "wss://hub.example.com/ws/node"
    assert env["NODE_TOKEN"] == "new-tok"
    assert env["HEARTBEAT_INTERVAL"] == "15"
    assert "OLD_ONLY_KEY" not in env
    assert "old-token" not in text
    assert "# ============ 后端连接 ============" in text
