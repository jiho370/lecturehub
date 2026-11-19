"""FILEIO-001..007: atomic writes (review #2)."""
from __future__ import annotations

import json
import os

import pytest

from lecturehub.core import fileio


def test_FILEIO_001_writes_text_and_creates_parent_korean_path(tmp_path):
    target = tmp_path / "강의 (메모)" / "2026-11-05 기초어학.md"
    fileio.atomic_write_text(target, "안녕\n두 줄")
    assert target.read_text(encoding="utf-8") == "안녕\n두 줄"
    assert [p.name for p in target.parent.iterdir()] == [target.name]  # no temp litter


def test_FILEIO_002_json_roundtrip_and_overwrite(tmp_path):
    target = tmp_path / "prefs.json"
    fileio.atomic_write_json(target, {"a": "가"})
    fileio.atomic_write_json(target, {"b": 2})
    assert json.loads(target.read_text(encoding="utf-8")) == {"b": 2}
    assert "가" not in target.read_text(encoding="utf-8")


def test_FILEIO_003_failed_write_keeps_old_content_and_no_temp(tmp_path, monkeypatch):
    target = tmp_path / "memo.md"
    target.write_text("old", encoding="utf-8")

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(fileio.os, "replace", boom)
    with pytest.raises(OSError):
        fileio.atomic_write_text(target, "new")
    assert target.read_text(encoding="utf-8") == "old"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["memo.md"]


def test_FILEIO_004_unserialisable_json_touches_nothing(tmp_path):
    target = tmp_path / "x.json"
    target.write_text("{}", encoding="utf-8")
    with pytest.raises(TypeError):
        fileio.atomic_write_json(target, {"bad": object()})
    assert target.read_text(encoding="utf-8") == "{}"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["x.json"]


def test_FILEIO_005_retries_windows_permission_error(tmp_path, monkeypatch):
    target = tmp_path / "s.json"
    real = os.replace
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError("locked by antivirus")
        return real(src, dst)

    monkeypatch.setattr(fileio.os, "replace", flaky)
    fileio.atomic_write_text(target, "ok", backoff_s=0)
    assert calls["n"] == 3 and target.read_text(encoding="utf-8") == "ok"

    calls["n"] = -100  # always locked → gives up with the original error, no temp left
    with pytest.raises(PermissionError):
        fileio.atomic_write_text(target, "never", retries=2, backoff_s=0)
    assert target.read_text(encoding="utf-8") == "ok"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["s.json"]


def test_FILEIO_006_optional_backup(tmp_path):
    target = tmp_path / "n.md"
    fileio.atomic_write_text(target, "v1", backup=True)  # nothing to back up yet
    assert not (tmp_path / "n.md.bak").exists()
    fileio.atomic_write_text(target, "v2", backup=True)
    assert (tmp_path / "n.md.bak").read_text(encoding="utf-8") == "v1"
    assert target.read_text(encoding="utf-8") == "v2"


def test_FILEIO_007_mkdir_false_never_creates_folders(tmp_path):
    with pytest.raises(OSError):
        fileio.atomic_write_json(tmp_path / "gone session" / "pair.json", {"mic": "x"}, mkdir=False)
    assert list(tmp_path.iterdir()) == []
