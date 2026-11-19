"""Release review 2026-11-16: the app's own log and the problem report file (core/applog.py, core/diagnostics.py)."""
from __future__ import annotations

import json
import logging
import sys
import threading
import zipfile

import pytest

from lecturehub.core import applog, diagnostics


@pytest.fixture
def logs(tmp_path, monkeypatch):
    """A fresh log folder and a clean logger; hooks and handlers are put back afterwards."""
    monkeypatch.setenv("LECTUREHUB_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("LECTUREHUB_SETTINGS", str(tmp_path / "appdata" / "settings.json"))
    saved = (sys.excepthook, threading.excepthook, list(applog.LOG.handlers), dict(applog._state))
    applog._state.update(process=None, crash_file=None)
    applog.LOG.handlers.clear()
    yield tmp_path / "logs"
    import faulthandler

    faulthandler.disable()
    for handler in applog.LOG.handlers:
        handler.close()
    if applog._state.get("crash_file"):
        applog._state["crash_file"].close()
    sys.excepthook, threading.excepthook = saved[0], saved[1]
    applog.LOG.handlers[:] = saved[2]
    applog._state.clear()
    applog._state.update(saved[3])


def _text(folder, name="app.log"):
    for handler in applog.LOG.handlers:
        handler.flush()
    return (folder / name).read_text(encoding="utf-8")


def test_keys_and_tokens_never_reach_the_log(logs):
    applog.setup("app")
    applog.LOG.error("요청 실패: https://example.invalid/v1?key=AIzaSyA1234567890abcdefghijklmnop&x=1")
    applog.LOG.error("헤더 %s", {"Authorization": "Bearer abcdefghijklmnop123456"})
    applog.LOG.error("gsk_abcdefghijklmnopqrstuv / token=canvas1234567890secret")
    text = _text(logs)
    for secret in ("AIzaSyA1234567890abcdefghijklmnop", "abcdefghijklmnop123456", "gsk_abcdefghijklmnopqrstuv",
                   "canvas1234567890secret"):
        assert secret not in text
    assert text.count("[가림]") >= 4 and "요청 실패" in text


def test_a_secret_inside_a_traceback_is_hidden_too(logs):
    applog.setup("app")
    try:
        raise RuntimeError("실패 api_key=AIzaSyA1234567890abcdefghijklmnop")
    except RuntimeError:
        applog.swallowed("합성 작업")
    text = _text(logs)
    assert "넘어간 오류: 합성 작업" in text and "RuntimeError" in text and "AIzaSyA1234567890" not in text


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_uncaught_errors_of_threads_are_written(logs, monkeypatch):
    applog.setup("worker")
    monkeypatch.setattr(sys, "stderr", open(logs / "stderr.txt", "w", encoding="utf-8"))  # noqa: SIM115 - quiet the default hook

    def boom():
        raise ValueError("합성 실패")

    worker = threading.Thread(target=boom, name="합성-작업")
    worker.start()
    worker.join()
    text = _text(logs, "worker.log")
    assert "백그라운드 작업(합성-작업)" in text and "ValueError: 합성 실패" in text


def test_setup_twice_keeps_one_handler_and_a_big_log_is_rolled_at_start(logs):
    logs.mkdir(parents=True)
    (logs / "app.log").write_text("x" * (applog.MAX_BYTES + 1), encoding="utf-8")
    applog.setup("app")
    applog.setup("app")
    assert len([h for h in applog.LOG.handlers if isinstance(h, logging.FileHandler)]) == 1
    assert (logs / "app.1.log").stat().st_size > applog.MAX_BYTES and "시작: Lecture Hub" in _text(logs)


def test_logging_setup_never_raises_when_the_folder_cannot_be_used(tmp_path, monkeypatch, logs):
    blocked = tmp_path / "파일"
    blocked.write_text("폴더가 아님", encoding="utf-8")
    monkeypatch.setenv("LECTUREHUB_LOG_DIR", str(blocked / "logs"))
    assert applog.setup("app") is None
    applog.swallowed("기록 폴더가 없어도 조용히")  # must not raise


def _without_truststore(monkeypatch):
    import builtins

    from lecturehub.core import net

    real = builtins.__import__

    def without(name, *args, **kwargs):
        if name == "truststore":
            raise ImportError("합성: 없음")
        return real(name, *args, **kwargs)

    monkeypatch.setattr(net, "_done", False)
    monkeypatch.setattr(builtins, "__import__", without)


def test_a_failed_switch_to_the_windows_certificate_store_is_written_to_the_log_file(logs, monkeypatch):
    """Before 2026-11-16 the switch ran before the log file was attached: the line went to stderr, which the
    windowed app does not have, and app.log said nothing."""
    _without_truststore(monkeypatch)
    assert applog.setup("app") == logs / "app.log"
    text = _text(logs)
    assert "넘어간 오류: Windows 인증서 저장소 사용" in text and "ImportError: 합성: 없음" in text
    assert text.index("시작: Lecture Hub") < text.index("넘어간 오류: Windows 인증서 저장소 사용")


def test_the_certificate_switch_runs_once_the_log_file_is_attached_and_also_without_a_log(tmp_path, monkeypatch, logs):
    from lecturehub.core import net

    seen = []  # per call: was the log file attached? (pytest hangs handlers of its own on the logger too)
    monkeypatch.setattr(net, "use_system_certificates", lambda: seen.append(
        any(type(h) is logging.FileHandler for h in applog.LOG.handlers)) or True)
    applog.setup("app")
    assert seen == [True]  # the handler was there when the switch was made
    applog.setup("app")
    assert seen == [True]  # a second setup does nothing
    # an unusable log folder: no log, but HTTPS still follows the Windows store
    applog._state.update(process=None)
    for handler in [h for h in applog.LOG.handlers if type(h) is logging.FileHandler]:
        handler.close()
        applog.LOG.removeHandler(handler)
    blocked = tmp_path / "파일"
    blocked.write_text("폴더가 아님", encoding="utf-8")
    monkeypatch.setenv("LECTUREHUB_LOG_DIR", str(blocked / "logs"))
    assert applog.setup("app") is None
    assert seen == [True, False]


def test_a_certificate_step_that_breaks_in_an_unexpected_way_cannot_stop_the_start(logs, monkeypatch):
    from lecturehub.core import net

    def broken():
        raise RuntimeError("합성: 예상 밖의 실패")

    monkeypatch.setattr(net, "use_system_certificates", broken)
    assert applog.setup("app") == logs / "app.log"  # D-050: starting the log never raises
    assert "RuntimeError: 합성: 예상 밖의 실패" in _text(logs)


def test_the_report_tells_a_cloud_synced_folder_by_the_wizards_own_test(tmp_path, logs, monkeypatch):
    """core/diagnostics.py had its own, shorter list of sync-folder names (no Google Drive for desktop)."""
    from lecturehub.recorder import devices
    from lecturehub.ui import setup

    monkeypatch.setattr(devices, "list_devices", list)  # no real audio device in a test: an empty list
    for name in setup.CLOUD_ENV:
        monkeypatch.delenv(name, raising=False)
    plain, synced = tmp_path / "문서" / "26-2", tmp_path / "내 드라이브" / "26-2"
    for folder in (plain, synced):
        folder.mkdir(parents=True)
    assert diagnostics.info(plain)["semester_folder"]["looks_cloud_synced"] is False
    assert diagnostics.info(synced)["semester_folder"]["looks_cloud_synced"] is True
    asked = []
    monkeypatch.setattr(setup, "cloud_synced", lambda path: asked.append(path) or True)
    assert diagnostics.info(plain)["semester_folder"]["looks_cloud_synced"] is True and asked == [plain]


def test_report_masks_the_semester_folder_the_home_folder_and_keys(tmp_path, logs):
    root = tmp_path / "사용자이름" / "문서" / "26-2"
    (root / "합성 과목").mkdir(parents=True)
    applog.setup("app")
    applog.LOG.error("파일을 열 수 없음: %s", root / "합성 과목" / "26.11.16 합성 과목.m4a")
    applog.LOG.error("설정 파일: %s", tmp_path / "사용자이름" / "AppData" / "settings.json")
    for handler in applog.LOG.handlers:
        handler.flush()
    target = diagnostics.make(root, dest=tmp_path / "out")
    with zipfile.ZipFile(target) as archive:
        names = archive.namelist()
        log = archive.read("logs/app.log").decode("utf-8")
        info = json.loads(archive.read("info.json").decode("utf-8"))
    assert {"info.json", "logs/app.log", "README.txt"} <= set(names)
    assert "합성 과목" not in log and "26.11.16" not in log and "<학기폴더>" in log
    assert info["app"]["version"] and info["semester_folder"]["chosen"] is True
    assert "합성 과목" not in json.dumps(info, ensure_ascii=False)


def test_home_folder_is_masked_but_the_rest_of_the_path_stays(tmp_path):
    mask = diagnostics.masker(None, home=tmp_path / "Users" / "홍길동")
    line = f"로그 폴더 {tmp_path / 'Users' / '홍길동' / 'AppData' / 'Local' / 'LectureHub'} 사용"
    assert mask(line) == "로그 폴더 <사용자폴더>\\AppData\\Local\\LectureHub 사용"


def test_plan_lists_what_goes_in_and_what_never_does(tmp_path, logs):
    applog.setup("app")
    applog.LOG.info("합성 줄")
    for handler in applog.LOG.handlers:
        handler.flush()
    plan = diagnostics.plan(tmp_path)
    assert [f["name"] for f in plan["files"]][:2] == ["info.json", "logs/app.log"]
    assert any("녹음" in item for item in plan["excluded"]) and any("키" in item for item in plan["excluded"])


def test_report_survives_a_missing_library_and_missing_logs(tmp_path, logs):
    target = diagnostics.make(None, dest=tmp_path / "out")
    with zipfile.ZipFile(target) as archive:
        info = json.loads(archive.read("info.json").decode("utf-8"))
    assert info["library"] == {"exists": False} and info["semester_folder"] == {"chosen": False}
    assert logging.getLogger("lecturehub") is applog.LOG


def test_a_stored_consent_can_be_listed_and_taken_back(tmp_path):
    from lecturehub.transcription.consent import NOTICES, ConsentStore

    store = ConsentStore(tmp_path / "_lecturehub" / "consent.json")
    store.grant("gemini_transcribe", "합성 과목", 1_700_000_000_000, NOTICES["gemini_transcribe"], by="user_app_dialog")
    store.grant("whisper_groq", "합성 과목", 1_700_000_000_001, NOTICES["whisper_groq"], by="user_app_dialog")
    assert [(r["provider"], r["course"]) for r in store.listing()] == [("gemini_transcribe", "합성 과목"), ("whisper_groq", "합성 과목")]
    assert store.revoke("gemini_transcribe", "합성 과목", 1_700_000_100_000) is True
    assert not store.granted("gemini_transcribe", "합성 과목") and store.granted("whisper_groq", "합성 과목")
    assert store.revoke("gemini_transcribe", "합성 과목", 1) is False
    kept = json.loads((tmp_path / "_lecturehub" / "consent_revoked.json").read_text(encoding="utf-8"))
    assert kept == [{"provider": "gemini_transcribe", "course": "합성 과목", "revoked_at": 1_700_000_100_000}]


def test_a_broken_consent_file_means_no_consent_and_is_kept(tmp_path, logs):
    from lecturehub.transcription.consent import ConsentStore

    path = tmp_path / "_lecturehub" / "consent.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"gemini_transcribe": {"합성 과목": ', encoding="utf-8")
    store = ConsentStore(path)
    assert store.granted("gemini_transcribe", "합성 과목") is False and store.listing() == []
    assert len(list(path.parent.glob("consent.json.corrupt-*"))) == 1


def test_broken_settings_fall_back_to_the_last_good_copy_and_keep_the_broken_file(tmp_path, logs):
    from lecturehub.core import settings

    settings.save({"data_root": str(tmp_path / "학기"), "term": "26-2"})
    settings.save({"labs": {"clova": True}})  # the second save keeps the first as settings.json.bak
    path = settings.settings_path()
    path.write_text("{broken", encoding="utf-8")
    assert settings.load() == {"data_root": str(tmp_path / "학기"), "term": "26-2"}
    assert settings.data_root() == tmp_path / "학기"
    assert len(list(path.parent.glob("settings.json.corrupt-*"))) == 1
    settings.save({"term": "27-1"})  # saving again repairs the file from the good copy
    assert json.loads(path.read_text(encoding="utf-8"))["data_root"] == str(tmp_path / "학기")
