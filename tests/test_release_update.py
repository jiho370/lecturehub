"""Release review 2026-11-16: the new-version notice (core/update.py) — daily, silent on failure, never installs."""
from __future__ import annotations

import io
import json
import sys

import pytest

import lecturehub
from lecturehub.core import settings, update, webview2


@pytest.fixture
def installed(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("LECTUREHUB_UPDATE_REPO", "synthetic-owner/lecture-hub")
    monkeypatch.setattr(lecturehub, "__version__", "1.2.0")


def _answer(tag: str, page: str | None = None, seen: list | None = None):
    def opener(request, timeout=0):
        if seen is not None:
            seen.append(request)
        body = {"tag_name": tag, "html_url": page or f"https://github.com/synthetic-owner/lecture-hub/releases/tag/{tag}"}
        return io.BytesIO(json.dumps(body).encode("utf-8"))

    return opener


def test_newer_release_is_reported_and_only_asked_once_a_day(installed):
    seen: list = []
    state = update.check(now=1_000_000, opener=_answer("v1.3.0", seen=seen))
    assert state["available"] and state["latest"] == "1.3.0" and state["url"].endswith("/releases/tag/v1.3.0")
    update.check(now=1_000_000 + 3600, opener=_answer("v9.9.9", seen=seen))
    assert len(seen) == 1 and update.status()["latest"] == "1.3.0"
    update.check(now=1_000_000 + update.EVERY_SECONDS + 1, opener=_answer("v1.4.0", seen=seen))
    assert len(seen) == 2 and update.status()["latest"] == "1.4.0"


def test_the_request_says_nothing_about_the_user(installed):
    seen: list = []
    update.check(now=1.0e6, opener=_answer("v1.3.0", seen=seen))
    request = seen[0]
    assert request.full_url == "https://api.github.com/repos/synthetic-owner/lecture-hub/releases/latest"
    assert request.get_header("User-agent") == "LectureHub/1.2.0" and request.data is None
    assert set(k.lower() for k in request.headers) == {"user-agent", "accept"}


def test_same_or_older_release_and_odd_answers_are_not_a_notice(installed):
    assert not update.check(now=1.0e6, opener=_answer("v1.2.0"))["available"]
    settings.save({"update_seen": None})
    assert not update.check(now=1.0e6, opener=_answer("nightly"))["available"]
    settings.save({"update_seen": None})
    elsewhere = _answer("v5.0.0", page="https://example.invalid/download")
    assert not update.check(now=1.0e6, opener=elsewhere)["available"]


def test_offline_is_silent_and_waits_a_day(installed):
    calls = []

    def offline(request, timeout=0):
        calls.append(1)
        raise OSError("no network")

    assert update.check(now=1.0e6, opener=offline) == update.status()
    update.check(now=1.0e6 + 60, opener=offline)
    assert len(calls) == 1 and not update.status()["available"]


def test_off_by_setting_from_source_or_without_a_repository(installed, monkeypatch):
    def never(request, timeout=0):
        raise AssertionError("must not ask the network")

    update.set_enabled(False)
    update.check(now=1.0e6, opener=never)
    update.set_enabled(True)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    update.check(now=1.0e6, opener=never)  # running from source
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("LECTUREHUB_UPDATE_REPO", "")
    assert update.repo() == "" and not update.enabled()
    update.check(now=1.0e6, opener=never)
    assert update.status()["possible"] is False


def test_webview2_version_is_found_in_any_of_the_three_places():
    places = {("HKCU", rf"Software\Microsoft\EdgeUpdate\Clients\{webview2.CLIENT}"): "157.0.1.2"}
    assert webview2.version(lambda hive, key: places.get((hive, key))) == "157.0.1.2"
    assert webview2.version(lambda hive, key: None) is None
    assert webview2.version(lambda hive, key: "0.0.0.0") is None


def test_launcher_tells_the_user_when_the_window_cannot_start(monkeypatch, tmp_path):
    from lecturehub.ui import launch

    said: list[str] = []
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(webview2, "version", lambda read=None: None)
    assert launch.run([], show=said.append) == 2 and "WebView2" in said[0]

    monkeypatch.setattr(webview2, "version", lambda read=None: "157.0")
    import lecturehub.ui.app as app

    def broken():
        raise RuntimeError("합성 시작 실패")

    monkeypatch.setattr(app, "main", broken)
    assert launch.run([], show=said.append) == 1
    assert "시작하지 못했습니다" in said[1] and "합성 시작 실패" not in said[1]  # the reason goes to the log, not the box
