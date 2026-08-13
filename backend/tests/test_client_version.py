"""客户端版本管理：纯规则函数 + /api/client-versions 接口。"""
import io
import pathlib
import zipfile

import fakeredis
import pytest
from fastapi.testclient import TestClient

from app import client_version as cv
from app.redis_store import RedisStore
from tests.test_helpers import drop_test_db, reset_test_db

_TEST_DB = pathlib.Path(__file__).resolve().parent / "_test_api.db"


@pytest.fixture
def client(monkeypatch, tmp_path):
    def fake_from_url(cls, url=None):
        return RedisStore(fakeredis.FakeAsyncRedis(decode_responses=True))

    reset_test_db(_TEST_DB)
    monkeypatch.setattr(RedisStore, "from_url", classmethod(fake_from_url))
    from app.main import app
    from app.settings import settings

    # 安装包落到用例私有目录，避免污染仓库与其它用例
    monkeypatch.setattr(settings, "client_package_dir", str(tmp_path / "packages"))

    with TestClient(app) as c:
        yield c
    drop_test_db(_TEST_DB)


def _auth(client) -> dict:
    r = client.post("/api/login", json={"username": "admin", "password": "admin123"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _node_token(client, headers) -> str:
    r = client.get("/api/config/node-token", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _pkg(version: str = "1.1.0", *, with_version_txt: bool = True, extra: dict | None = None) -> bytes:
    """构造一个最小可用的安装包 zip。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("node_client.exe", b"MZ fake binary")
        if with_version_txt:
            zf.writestr("version.txt", f"{version}\n")
        for name, content in (extra or {}).items():
            zf.writestr(name, content)
    return buf.getvalue()


def _upload(client, headers, version: str = "1.1.0", **kw):
    data = {"version": kw.pop("form_version", ""), "notes": kw.pop("notes", "")}
    blob = kw.pop("blob", None)
    if blob is None:
        blob = _pkg(version, **kw)
    return client.post(
        "/api/client-versions",
        files={"file": ("node_client.zip", blob, "application/zip")},
        data=data,
        headers=headers,
    )


# ----------------------------- 纯函数 -----------------------------

def test_normalize_version_accepts_semver_and_strips_v_prefix():
    assert cv.normalize_version("1.2.3") == "1.2.3"
    assert cv.normalize_version(" v1.2.3 ") == "1.2.3"
    assert cv.normalize_version("2026.08.12-rc1") == "2026.08.12-rc1"


def test_normalize_version_rejects_path_traversal():
    # 版本号会拼进磁盘文件名，必须挡掉任何路径成分
    for bad in ("../etc/passwd", "a/b", "a\\b", "", "  ", "C:1.0"):
        assert cv.normalize_version(bad) == "", bad


def test_compare_versions_pads_missing_segments():
    assert cv.compare_versions("1.1", "1.1.0") == 0
    assert cv.compare_versions("1.2.0", "1.10.0") < 0
    assert cv.compare_versions("2.0.0", "1.9.9") > 0


def test_accepts_build_stamped_version_from_node_client():
    # node_client 打包产出 `${数字版本号}-${年月日时分秒}`（见 node_client/version.py）
    early = "1.1.0-20260813090000"
    late = "1.1.0-20260813090001"
    assert cv.normalize_version(early) == early
    assert len(late) <= 32
    assert cv.compare_versions(early, late) < 0
    # 同一个数字版本号下，时间戳决定新旧；源码直跑上报的裸版本号排在构建产物之前
    assert cv.classify_update(early, late) == cv.UPGRADE
    assert cv.classify_update("1.1.0", early) == cv.UPGRADE
    assert cv.classify_update(late, "1.2.0-20200101000000") == cv.UPGRADE


def test_compare_versions_falls_back_to_string_when_no_digits():
    assert cv.compare_versions("alpha", "beta") < 0
    assert cv.compare_versions("alpha", "alpha") == 0


def test_classify_update_marks_downgrade_and_unknown():
    assert cv.classify_update("1.0.0", "1.1.0") == cv.UPGRADE
    assert cv.classify_update("1.1.0", "1.0.0") == cv.DOWNGRADE
    assert cv.classify_update("1.1.0", "1.1.0") == cv.SAME
    # 旧客户端不上报版本，判定必须落到 unknown 而不是误判成升级
    assert cv.classify_update("", "1.1.0") == cv.UNKNOWN
    assert cv.classify_update("1.1.0", "") == cv.UNKNOWN


def test_version_from_txt_takes_first_line_without_bom():
    assert cv.version_from_txt("\ufeff1.1.0\n附注\n") == "1.1.0"
    assert cv.version_from_txt("   ") == ""


def test_validate_package_requires_entry_and_rejects_zip_slip():
    ok, _ = cv.validate_package(["node_client.exe", "version.txt"])
    assert ok

    ok, reason = cv.validate_package(["readme.txt"])
    assert not ok and "node_client.exe" in reason

    for bad in ("../evil.exe", "/abs/evil.exe", "C:/evil.exe", "a/../../evil.exe"):
        ok, reason = cv.validate_package([bad, "node_client.exe"])
        assert not ok, bad
        assert "非法路径" in reason


def test_is_protected_covers_env_only():
    assert cv.is_protected(".env")
    assert cv.is_protected("sub/.ENV")
    assert not cv.is_protected("node_client.exe")


def test_build_release_records_previous_for_rollback():
    first = cv.build_release("1.0.0", None)
    assert first["version"] == "1.0.0"
    assert first["previous"] == ""

    second = cv.build_release("1.1.0", first)
    assert second["previous"] == "1.0.0"

    # 重复发布同一版本不能把 previous 冲掉，否则回滚会退无可退
    again = cv.build_release("1.1.0", second)
    assert again["previous"] == "1.0.0"


def test_rollback_release_swaps_pointer_and_stops_at_origin():
    release = {"version": "1.1.0", "previous": "1.0.0"}
    back = cv.rollback_release(release)
    assert back["version"] == "1.0.0"
    assert back["previous"] == "1.1.0"
    assert cv.rollback_release({"version": "1.0.0", "previous": ""}) is None
    assert cv.rollback_release(None) is None


# ----------------------------- 接口 -----------------------------

def test_client_versions_require_auth(client):
    assert client.get("/api/client-versions").status_code == 401
    assert client.post("/api/client-versions/1.0.0/release").status_code == 401
    assert client.delete("/api/client-versions/1.0.0").status_code == 401
    assert client.post("/api/client-versions/rollback").status_code == 401


def test_upload_reads_version_from_package(client):
    h = _auth(client)
    r = _upload(client, h, "1.1.0")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["version"] == "1.1.0"
    assert body["size"] > 0
    assert len(body["sha256"]) == 64

    listed = client.get("/api/client-versions", headers=h).json()
    assert [i["version"] for i in listed["items"]] == ["1.1.0"]
    # 上传不等于发布
    assert listed["release"]["version"] == ""
    assert listed["items"][0]["is_current"] is False


def test_upload_without_version_txt_requires_explicit_version(client):
    h = _auth(client)
    r = _upload(client, h, with_version_txt=False)
    assert r.status_code == 400
    assert "version.txt" in r.json()["detail"]

    r = _upload(client, h, with_version_txt=False, form_version="9.9.9")
    assert r.status_code == 201, r.text
    assert r.json()["version"] == "9.9.9"


def test_upload_rejects_bad_package(client):
    h = _auth(client)
    assert _upload(client, h, blob=b"not a zip at all").status_code == 400

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("readme.txt", "no exe here")
    r = _upload(client, h, blob=buf.getvalue(), form_version="1.0.0")
    assert r.status_code == 400
    assert "node_client.exe" in r.json()["detail"]


def test_upload_duplicate_version_returns_409(client):
    h = _auth(client)
    assert _upload(client, h, "1.1.0").status_code == 201
    r = _upload(client, h, "1.1.0")
    assert r.status_code == 409
    assert "已存在" in r.json()["detail"]


def test_release_then_downgrade_requires_confirmation(client):
    h = _auth(client)
    _upload(client, h, "1.0.0")
    _upload(client, h, "1.1.0")

    assert client.post("/api/client-versions/1.1.0/release", headers=h).status_code == 200

    # 降级必须显式确认
    r = client.post("/api/client-versions/1.0.0/release", headers=h)
    assert r.status_code == 409
    assert "降级" in r.json()["detail"]

    r = client.post(
        "/api/client-versions/1.0.0/release",
        json={"confirm_downgrade": True},
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert r.json()["version"] == "1.0.0"
    assert r.json()["previous"] == "1.1.0"


def test_release_unknown_version_returns_404(client):
    h = _auth(client)
    assert client.post("/api/client-versions/3.0.0/release", headers=h).status_code == 404


def test_rollback_returns_to_previous_release(client):
    h = _auth(client)
    _upload(client, h, "1.0.0")
    _upload(client, h, "1.1.0")

    # 从未发布过时无处可退
    assert client.post("/api/client-versions/rollback", headers=h).status_code == 409

    client.post("/api/client-versions/1.0.0/release", headers=h)
    client.post("/api/client-versions/1.1.0/release", headers=h)

    r = client.post("/api/client-versions/rollback", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["version"] == "1.0.0"

    listed = client.get("/api/client-versions", headers=h).json()
    assert listed["release"]["version"] == "1.0.0"


def test_delete_blocks_current_release_and_clears_rollback_target(client):
    h = _auth(client)
    _upload(client, h, "1.0.0")
    _upload(client, h, "1.1.0")
    client.post("/api/client-versions/1.0.0/release", headers=h)
    client.post("/api/client-versions/1.1.0/release", headers=h)

    r = client.delete("/api/client-versions/1.1.0", headers=h)
    assert r.status_code == 409
    assert "当前发布版本" in r.json()["detail"]

    # 删掉回滚落点后，指针里的 previous 必须一并清掉，否则回滚必然失败
    assert client.delete("/api/client-versions/1.0.0", headers=h).status_code == 200
    listed = client.get("/api/client-versions", headers=h).json()
    assert listed["release"]["previous"] == ""
    assert client.post("/api/client-versions/rollback", headers=h).status_code == 409


def test_node_token_can_read_current_and_download(client):
    h = _auth(client)
    blob = _pkg("1.1.0")
    assert _upload(client, h, blob=blob).status_code == 201
    client.post("/api/client-versions/1.1.0/release", headers=h)

    nh = {"X-Node-Token": _node_token(client, h)}
    r = client.get("/api/client-versions/current", headers=nh)
    assert r.status_code == 200, r.text
    assert r.json()["version"] == "1.1.0"
    assert r.json()["download_url"] == "/api/client-versions/1.1.0/download"

    r = client.get("/api/client-versions/1.1.0/download", headers=nh)
    assert r.status_code == 200
    assert r.content == blob


def test_available_lists_versions_for_node_token(client):
    """面板「导入节点」「更新到指定版本」靠这个清单选版本。"""
    h = _auth(client)
    _upload(client, h, "1.0.0")
    _upload(client, h, "1.10.0")
    _upload(client, h, "1.2.0")
    client.post("/api/client-versions/1.2.0/release", headers=h)

    nh = {"X-Node-Token": _node_token(client, h)}
    r = client.get("/api/client-versions/available", headers=nh)
    assert r.status_code == 200, r.text
    body = r.json()
    # 语义版本序，新版在前（1.10.0 > 1.2.0，不是字符串序）
    assert [i["version"] for i in body["items"]] == ["1.10.0", "1.2.0", "1.0.0"]
    assert body["current"] == "1.2.0"
    assert [i["is_current"] for i in body["items"]] == [False, True, False]
    # 不暴露后台操作痕迹
    assert "uploaded_by" not in body["items"][0]


def test_available_requires_node_token(client):
    h = _auth(client)
    _upload(client, h, "1.1.0")
    assert client.get("/api/client-versions/available").status_code == 401
    assert (
        client.get(
            "/api/client-versions/available", headers={"X-Node-Token": "wrong"}
        ).status_code
        == 401
    )


def test_available_empty_before_any_upload(client):
    h = _auth(client)
    nh = {"X-Node-Token": _node_token(client, h)}
    r = client.get("/api/client-versions/available", headers=nh)
    assert r.status_code == 200
    assert r.json() == {"items": [], "current": ""}


def test_current_returns_204_before_any_release(client):
    h = _auth(client)
    _upload(client, h, "1.1.0")
    nh = {"X-Node-Token": _node_token(client, h)}
    assert client.get("/api/client-versions/current", headers=nh).status_code == 204


def test_node_token_endpoints_reject_bad_token(client):
    h = _auth(client)
    _upload(client, h, "1.1.0")
    for headers in ({}, {"X-Node-Token": "wrong-token"}):
        assert client.get("/api/client-versions/current", headers=headers).status_code == 401
        assert (
            client.get("/api/client-versions/1.1.0/download", headers=headers).status_code == 401
        )


def test_node_token_cannot_perform_admin_writes(client):
    """NODE_TOKEN 全局共享且明文分发，绝不能用它改发布版本或删包。"""
    h = _auth(client)
    _upload(client, h, "1.1.0")
    nh = {"X-Node-Token": _node_token(client, h)}
    assert client.post("/api/client-versions/1.1.0/release", headers=nh).status_code == 401
    assert client.delete("/api/client-versions/1.1.0", headers=nh).status_code == 401
    assert client.post("/api/client-versions/rollback", headers=nh).status_code == 401
    assert client.get("/api/client-versions", headers=nh).status_code == 401


def test_download_rejects_traversal_version(client):
    h = _auth(client)
    _upload(client, h, "1.1.0")
    nh = {"X-Node-Token": _node_token(client, h)}
    r = client.get("/api/client-versions/..%2F..%2Fetc%2Fpasswd/download", headers=nh)
    assert r.status_code in (400, 404)


def test_version_list_reports_node_distribution(client):
    h = _auth(client)
    _upload(client, h, "1.1.0")
    r = client.post("/api/nodes", json={"name": "n1", "mt5_login": 77001}, headers=h)
    assert r.status_code == 201, r.text

    listed = client.get("/api/client-versions", headers=h).json()
    # 节点尚未上线上报版本，应计入 unknown 而不是挂到某个版本上
    assert listed["unknown_node_count"] == 1
    assert listed["items"][0]["node_count"] == 0
