"""强制更新令：只升级落后实例，成功后不再重复执行。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import force_update  # noqa: E402


class _Cfg:
    def __init__(self, instance_id: str, exe: Path) -> None:
        self.id = instance_id
        self.exe_path = str(exe)


class _Result:
    def __init__(self, ok: bool) -> None:
        self.ok = ok


class _Manager:
    def __init__(self, configs) -> None:
        self._configs = configs
        self.calls: list[list[str]] = []
        self.results: list[_Result] = []

    def configs(self):
        return self._configs

    def batch_update(self, _package_dir, *, instance_ids, restart_if_was_running, on_progress=None):
        assert restart_if_was_running is True
        self.calls.append(list(instance_ids))
        return list(self.results)


class _Target:
    ready = True


def _layout(tmp_path: Path):
    old = tmp_path / "old"
    new = tmp_path / "new"
    old.mkdir()
    new.mkdir()
    (old / "node_client.exe").write_bytes(b"x")
    (new / "node_client.exe").write_bytes(b"x")
    (old / "version.txt").write_text("1.0.0\n", encoding="utf-8")
    (new / "version.txt").write_text("9.0.0\n", encoding="utf-8")
    manager = _Manager([
        _Cfg("old", old / "node_client.exe"),
        _Cfg("new", new / "node_client.exe"),
    ])
    return manager


def test_force_update_applies_outdated_once(tmp_path, monkeypatch):
    monkeypatch.setattr(force_update, "data_dir", lambda: tmp_path)

    def _fake_download(manager, target, version, sha, ids):
        return manager.batch_update(None, instance_ids=ids, restart_if_was_running=True)

    monkeypatch.setattr(force_update, "_download_and_apply", _fake_download)
    force_update._INFLIGHT = ""
    force_update._RETRY_AT = 0.0
    manager = _layout(tmp_path)
    manager.results = [_Result(True)]
    payload = {"version": "2.0.0", "sha256": "abc", "force": {"id": "f1", "version": "2.0.0"}}

    force_update.apply_if_needed(manager, _Target(), payload)
    assert manager.calls == [["old"]]
    force_update.apply_if_needed(manager, _Target(), payload)
    assert manager.calls == [["old"]]
    assert json.loads((tmp_path / "force_update.json").read_text(encoding="utf-8"))["id"] == "f1"

    manager.results = [_Result(False)]
    (tmp_path / "force_update.json").unlink()
    force_update._RETRY_AT = 0.0
    again = {"version": "2.0.0", "sha256": "abc", "force": {"id": "f2", "version": "2.0.0"}}
    force_update.apply_if_needed(manager, _Target(), again)
    assert len(manager.calls) == 2
    force_update.apply_if_needed(manager, _Target(), again)
    assert len(manager.calls) == 2
    force_update._RETRY_AT = 0.0
    force_update.apply_if_needed(manager, _Target(), again)
    assert len(manager.calls) == 3
