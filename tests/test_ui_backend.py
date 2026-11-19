"""Desktop app backend: timetable, engine link, re-attach, consent gate (no webview needed)."""
import codecs
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest
from lecturehub.core import edition

from lecturehub.ui import backend as backend_module
from lecturehub.ui.backend import Backend, find_running_session, load_courses, today_slots

TIMETABLE = "폴더,과목,요일,시작,종료\r\n구조역학,구조역학 및 실험,화,10:00,11:50\r\n토질역학,수자원설계,화,14:00,17:50\r\n" \
            "기초어학,기초어학 1,월,14:00,15:15\r\n"


@pytest.fixture
def semester(tmp_path, monkeypatch):
    root = tmp_path / "대학" / "26-2 (합성)"
    (root / "녹음본" / "구조역학").mkdir(parents=True)
    for name in ("26.9.1 구조역학.m4a", "26.9.8 구조역학.m4a", "메모.txt"):
        (root / "녹음본" / "구조역학" / name).write_bytes(b"x")
    (root / "시간표.csv").write_bytes(codecs.BOM_UTF8 + TIMETABLE.encode("utf-8"))
    (root / "_lecturehub").mkdir(exist_ok=True)
    (root / "_lecturehub" / "synthetic_root").write_text("합성 시험 루트", encoding="utf-8")  # fake engine may link here
    config = tmp_path / "recorder.toml"
    config.write_text("\n".join([
        "[recorder]", f"out_dir = '{root / '_lecturehub' / 'recordings'}'", 'device_name = "Fake"',
        f"timetable = '{root / '시간표.csv'}'"]), encoding="utf-8")
    monkeypatch.setenv("LECTUREHUB_RECORDER_CONFIG", str(config))
    return root


def tuesday():
    return datetime(2026, 9, 29, 10, 5).astimezone()


def wait_phase(app, *phases, timeout=30):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        state = app.state()
        if state["phase"] in phases:
            return state
        time.sleep(0.05)
    raise TimeoutError(f"{phases}: {app.state()}")


def test_courses_and_today_from_timetable(semester):
    courses = load_courses(semester / "시간표.csv", semester)
    assert [c.folder for c in courses] == ["구조역학", "토질역학", "기초어학"]
    assert courses[0].recordings == 2 and courses[1].recordings == 0  # counts audio only, reads only
    assert [s["folder"] for s in today_slots(courses, tuesday())] == ["구조역학", "토질역학"]
    info = Backend(clock=tuesday, fake_engine=True).init_info()
    assert info["course"] == "구조역학" and info["date"] == "9/29(화)"
    assert info["devices"]["mic"][0]["name"].startswith("FakeSource")


def test_record_bookmark_stop_summary(semester):
    app = Backend(clock=tuesday, fake_engine=True)
    assert app.start("구조역학")["ok"]
    assert not app.start("구조역학")["ok"]  # one recording at a time
    wait_phase(app, "recording")
    time.sleep(0.6)
    assert app.bookmark()["ok"]
    time.sleep(0.6)
    assert app.recording and app.stop()["ok"]
    state = wait_phase(app, "done", timeout=60)
    summary = state["summary"]
    assert summary["cause"] == "user_stop" and summary["dropped_samples"] == 0 and summary["bookmarks"] == 1
    assert Path(summary["master"]).exists() and len(state["bookmarks"]) == 1
    assert Path(summary["session_dir"]).parent == semester / "_lecturehub" / "recordings"


def test_reopened_app_reattaches_and_stops(semester):
    out = semester / "_lecturehub" / "recordings"
    engine = subprocess.Popen([sys.executable, "-m", "lecturehub.recorder.service", "--fake", "--course", "구조역학",
                               "--out", str(out)], stdout=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    try:
        end = time.monotonic() + 20
        while not find_running_session(out) and time.monotonic() < end:
            time.sleep(0.1)
        app = Backend(clock=tuesday, fake_engine=True)  # a new app instance
        end = time.monotonic() + 20  # the engine writes live.json (course, time) a moment after taking the lock
        while not (app.state()["status"] or {}).get("course") and time.monotonic() < end:
            time.sleep(0.1)
        state = app.state()
        assert state["attached"] and state["phase"] == "recording" and state["status"]["course"] == "구조역학"
        end = time.monotonic() + 20  # live.json is refreshed once a second; slow under a busy test run
        while (app.state()["status"] or {}).get("elapsed_ms", 0) <= 0 and time.monotonic() < end:
            time.sleep(0.1)
        assert app.state()["status"]["elapsed_ms"] > 0
        assert app.stop()["ok"]
        state = wait_phase(app, "done", timeout=60)
        assert engine.wait(30) == 0
        stop = json.loads((Path(state["summary"]["session_dir"]) / "stop.json").read_text(encoding="utf-8"))
        assert stop["trigger"] == "app_stop_file"
    finally:
        if engine.poll() is None:
            engine.kill()


def test_CONSENT_001_transcription_needs_consent_before_any_upload(semester, monkeypatch):
    """CONSENT-001 (app path): declining uploads nothing and records no consent."""
    spawned = []
    monkeypatch.setattr(backend_module, "spawn", lambda args, **kw: spawned.append(args) or subprocess.Popen(
        [sys.executable, "-c", "pass"]))
    app = Backend(clock=tuesday, fake_engine=True)
    master = semester / "_lecturehub" / "recordings" / "s" / "master.m4a"
    master.parent.mkdir(parents=True)
    master.write_bytes(b"x")
    app._state.update(phase="done", summary={"master": str(master)}, status={"course": "구조역학"})
    check = app.transcription_check()
    assert check["needs_consent"] and "Google" in check["notice"]
    assert not app.transcription_start(consent=False)["ok"] and spawned == []
    assert not (semester / "_lecturehub" / "consent.json").exists()
    assert app.transcription_start(consent=True)["ok"]
    assert spawned and spawned[0][2:5] == ["lecturehub", "transcribe", "run"] and "--course" in spawned[0]
    consent = json.loads((semester / "_lecturehub" / "consent.json").read_text(encoding="utf-8"))
    assert consent["gemini_transcribe"]["구조역학"]["granted_by"] == "user_app_dialog"
    assert not app.transcription_check()["needs_consent"]


def test_open_folder_only_inside_lecturehub(semester):
    app = Backend(clock=tuesday, fake_engine=True)
    assert app.open_folder(str(semester / "녹음본"))["ok"] is False


def test_closing_the_window_while_recording_asks_first():
    from lecturehub.ui.app import Api

    class FakeBackend:
        recording = True
        helpers_stopped = 0

        def stop_live_helpers(self):  # P-053: the Q&A helper and captions stop with the app
            FakeBackend.helpers_stopped += 1
            return []

    class FakeWindow:
        def __init__(self, answer):
            self.answer, self.asked = answer, []

        def create_confirmation_dialog(self, title, message):
            self.asked.append(message)
            return self.answer

    class FakeMini:
        shown = 0

        def show(self):
            FakeMini.shown += 1

    api = Api(FakeBackend())
    api._main, api._mini = FakeWindow(True), FakeMini()
    api._main.hide = lambda: None
    assert api._on_main_closing() is False and FakeMini.shown == 1  # user 2026-09-30: to the mini window, not away
    assert api._main.asked == []
    api._mini = None  # no mini window (older setups): ask as before
    api._main = FakeWindow(False)
    assert api._on_main_closing() is False and "녹음은 계속" in api._main.asked[0]
    assert FakeBackend.helpers_stopped == 0  # stayed open: helpers keep going
    api._main = FakeWindow(True)
    assert api._on_main_closing() is True and FakeBackend.helpers_stopped == 1
    FakeBackend.recording = False
    api._main = FakeWindow(False)
    assert api._on_main_closing() is True and api._main.asked == [] and FakeBackend.helpers_stopped == 2


def test_blank_course_transcription_asks_each_time_and_passes_consent_once(semester, monkeypatch):
    spawned = []
    monkeypatch.setattr(backend_module, "spawn", lambda args, **kw: spawned.append(args) or subprocess.Popen(
        [sys.executable, "-c", "pass"]))
    app = Backend(clock=tuesday, fake_engine=True)
    master = semester / "_lecturehub" / "recordings" / "s" / "master.m4a"
    master.parent.mkdir(parents=True)
    master.write_bytes(b"x")
    app._state.update(phase="done", summary={"master": str(master)}, status={"course": ""})
    assert app.transcription_check()["needs_consent"] and app.transcription_check()["once_only"]
    assert app.transcription_start(consent=True)["ok"]
    assert "--consent-once" in spawned[0]
    assert not (semester / "_lecturehub" / "consent.json").exists()
    assert app.transcription_check()["needs_consent"]  # still asked next time


def test_S2_auto_transcription_only_for_a_real_course_with_user_consent(semester, monkeypatch):
    app = Backend(clock=tuesday, fake_engine=True)
    started = []
    monkeypatch.setattr(app, "transcription_start", lambda consent, master=None, course=None: started.append(
        (master, course)) or {"ok": True})
    consent = semester / "_lecturehub" / "consent.json"
    consent.parent.mkdir(parents=True, exist_ok=True)
    consent.write_text(json.dumps({"gemini_transcribe": {
        "구조역학": {"granted_by": "user_app_dialog", "authority": "confirmed_by_user"},
        "합성 드라이런": {"granted_by": "user", "authority": "synthetic_test"},
        "": {"granted_by": "user_app_dialog", "authority": "invalid"}}}, ensure_ascii=False), encoding="utf-8")
    app._maybe_auto_transcribe({"state": "linked", "master": "a.m4a", "course": "구조역학"})
    app._maybe_auto_transcribe({"state": "linked", "master": "b.m4a", "course": "토질역학"})      # no consent
    app._maybe_auto_transcribe({"state": "linked", "master": "c.m4a", "course": "합성 드라이런"})  # synthetic
    app._maybe_auto_transcribe({"state": "linked", "master": "d.m4a", "course": ""})         # blank
    app._maybe_auto_transcribe({"state": "pending", "master": "e.m4a", "course": "구조역학"})   # not linked
    assert started == [("a.m4a", "구조역학")]


def test_D061_whisper_after_class_only_with_groq_consent_and_key(semester, monkeypatch):
    """Whisper (Groq) is a separate engine: its own consent, never the Google consent; no key → off."""
    from lecturehub.transcription import groq
    from lecturehub.ui import backend as backend_module

    app = Backend(clock=tuesday, fake_engine=True)
    monkeypatch.setattr(app, "transcription_start", lambda *a, **k: {"ok": True})
    spawned = []
    monkeypatch.setattr(backend_module, "spawn", lambda args, **kw: spawned.append(args) or _Done())
    consent = semester / "_lecturehub" / "consent.json"
    consent.parent.mkdir(parents=True, exist_ok=True)
    consent.write_text(json.dumps({
        "gemini_transcribe": {"토질역학": {"granted_by": "user_app_dialog", "authority": "confirmed_by_user"}},
        "whisper_groq": {"도시계": {"granted_by": "user", "authority": "confirmed_by_user"}}},
        ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(groq, "load_api_key", lambda: "gsk_SYNTHETIC")
    app._maybe_auto_transcribe({"state": "linked", "master": "a.m4a", "course": "도시계"})
    app._maybe_auto_transcribe({"state": "linked", "master": "b.m4a", "course": "토질역학"})   # Google consent only
    app._maybe_auto_transcribe({"state": "pending", "master": "c.m4a", "course": "도시계"})  # not linked
    assert [(a[a.index("run") + 1], a[a.index("--provider") + 1]) for a in spawned] == [("a.m4a", "whisper")]

    def no_key():
        raise groq.PermanentError("no key")

    monkeypatch.setattr(groq, "load_api_key", no_key)
    app._maybe_auto_transcribe({"state": "linked", "master": "d.m4a", "course": "도시계"})
    assert len(spawned) == 1 and app.engine_status()["whisper"] == "off"


class _Done:
    def wait(self):
        return 0


def test_S2_link_failure_never_touches_the_recording_state(semester, monkeypatch):
    app = Backend(clock=tuesday, fake_engine=True)
    app._state.update(phase="done", summary={"master": "x.m4a", "session_dir": "s"})

    def broken(*args, **kwargs):
        raise RuntimeError("DB가 이 앱보다 새 버전입니다")

    monkeypatch.setattr(app, "_cli", broken)
    app._after_recording(semester / "s")
    state = app.state()
    assert state["phase"] == "done" and state["summary"]["master"] == "x.m4a"
    assert state["link"]["state"] == "error" and "새 버전" in state["link"]["reason"]


def test_S2_restart_resumes_waiting_jobs_and_registers_finished_ones(semester, monkeypatch):
    app = Backend(clock=tuesday, fake_engine=True)
    jobs = semester / "_lecturehub" / "jobs"
    for job_id, state, eligible in (("waiting", "waiting_for_quota", 1), ("later", "waiting_for_quota", 10**15),
                                    ("done", "done", None)):
        (jobs / job_id).mkdir(parents=True)
        (jobs / job_id / "state.json").write_text(json.dumps({"job_id": job_id, "state": state,
                                                              "next_eligible_at": eligible}), encoding="utf-8")
    calls, spawned = [], []
    monkeypatch.setattr(app, "_cli", lambda *args, **kw: calls.append(args) or [])
    monkeypatch.setattr(backend_module, "spawn", lambda args, **kw: spawned.append(args) or subprocess.Popen(
        [sys.executable, "-c", "pass"]))
    assert app.resume_jobs() == ["waiting"]
    assert calls[0] == ("lecture", "sync-jobs") and spawned[0][3:6] == ["transcribe", "resume", "waiting"]


def test_audit1_restart_during_recording_holds_google_and_archive_copy(semester, monkeypatch):
    """Audit #1/#8 (11/5): app reopened while the engine keeps recording → no non-live Google resume, no archive copy;
    resumed jobs always run below normal."""
    from lecturehub.recorder.heartbeat import SessionLock

    app = Backend(clock=tuesday, fake_engine=True)
    jobs = semester / "_lecturehub" / "jobs"
    for job_id, live in (("google", False), ("livejob", True)):
        (jobs / job_id).mkdir(parents=True)
        (jobs / job_id / "state.json").write_text(json.dumps({"job_id": job_id, "state": "waiting_for_quota",
                                                              "next_eligible_at": 1, "live": live}), encoding="utf-8")
    calls, spawned = [], []
    monkeypatch.setattr(app, "_cli", lambda *args, **kw: calls.append(args) or [])
    monkeypatch.setattr(app, "_resume_job", lambda job_id, below_normal=True: spawned.append((job_id, below_normal)))
    session = semester / "_lecturehub" / "recordings" / "20261105_100500_mic"
    session.mkdir(parents=True)
    with SessionLock(session):  # started outside this app object (terminal or an earlier app run)
        assert app.resume_jobs() == ["livejob"]
    assert spawned == [("livejob", True)]
    assert ("lecture", "archive-copy") not in calls and ("lecture", "wav-cleanup") not in calls
    calls.clear(), spawned.clear()
    assert app.resume_jobs() == ["google", "livejob"]  # recording over: both resume, gently
    assert spawned == [("google", True), ("livejob", True)] and ("lecture", "archive-copy") in calls


def test_audit6_busy_sees_a_recording_started_outside_the_app(semester, monkeypatch):
    """Audit #6 (11/5): `lecturehub record` started after the app opened must still block heavy work."""
    from lecturehub.recorder.heartbeat import SessionLock
    from lecturehub.ui import app as app_module

    api = app_module.Api(Backend(clock=tuesday, fake_engine=True), None, None)
    assert api._busy() is False
    session = semester / "_lecturehub" / "recordings" / "20261105_100500_mic"
    session.mkdir(parents=True)
    with SessionLock(session):
        assert api._busy() is False  # cached for a few seconds (cheap on every call)
        api._backend._outside_cache = None
        assert api._busy() is True
    api._backend._outside_cache = None
    assert api._busy() is False


def test_S2_pending_confirmation_links_then_transcribes_when_consented(semester, monkeypatch):
    app = Backend(clock=tuesday, fake_engine=True)
    monkeypatch.setattr(app, "_cli", lambda *args, **kw: {"state": "linked", "lecture_id": "L", "media_id": "M",
                                                         "course": args[4], "master": "m.m4a"})
    started = []
    monkeypatch.setattr(app, "auto_transcription_allowed", lambda course: course == "구조역학")
    monkeypatch.setattr(app, "transcription_start", lambda *a: started.append(a) or {"ok": True})
    assert app.confirm_pending("ingest", "구조역학")["ok"] and started == [(False, "m.m4a", "구조역학")]
    assert app.state()["link"]["lecture_id"] == "L" and app.state()["library_version"] == 1


def test_S1_2_window_opens_even_if_the_library_layer_cannot_load(semester, monkeypatch):
    """Review S1-2: an import failure in the DB layer leaves the recorder usable."""
    import builtins

    from lecturehub.ui import app as app_module

    real_import = builtins.__import__

    def failing(name, *args, **kwargs):
        if name.endswith("library") and args and args[2] and "Library" in args[2]:
            raise ImportError("synthetic index failure")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", failing)
    library = app_module.open_library(semester)
    api = app_module.Api(Backend(clock=tuesday, fake_engine=True), library, None)
    result = api.sidebar()
    assert result["ok"] is False and "synthetic index failure" in result["error"]
    assert api.start("구조역학")["ok"]  # recording still starts
    wait_phase(api._backend, "recording")
    assert api.stop()["ok"]
    wait_phase(api._backend, "done", timeout=60)


def test_review_A2_course_is_taken_when_recording_starts_not_when_the_app_opened(semester, monkeypatch):
    """App opened before the class window: the pre-fill must follow the timetable at the start of recording."""
    spawned = []
    monkeypatch.setattr(backend_module.EngineLink, "spawn", lambda self, args, cwd=None: spawned.append(args))
    now = {"at": datetime(2026, 9, 29, 8, 0).astimezone()}
    app = Backend(clock=lambda: now["at"], fake_engine=True)
    assert app.init_info()["course"] == ""  # 08:00: no class yet
    now["at"] = tuesday()  # 10:05, window left open
    assert app.suggest_course() == "구조역학"

    def started(course, source):
        app._state["phase"] = "idle"
        assert app.start(course, "mic", None, source)["ok"]
        args = spawned[-1]
        return args[args.index("--course") + 1], args[args.index("--course-source") + 1]

    assert started("", "unknown") == ("구조역학", "timetable_auto")
    assert started("", "timetable_auto") == ("구조역학", "timetable_auto")
    assert started("토질역학", "user_pick") == ("토질역학", "user_pick")  # the user's pick is kept
    assert started("", "user_pick") == ("", "unknown")  # "no course" is not a course choice


def test_review_duplicate_jobs_are_never_started_or_resumed(semester, monkeypatch):
    """9/29: a CLI job and an app job sent the same recording; a restart then resumed the stopped duplicate."""
    jobs = semester / "_lecturehub" / "jobs"
    audio = str(semester / "_lecturehub" / "recordings" / "s" / "master.mp3")
    for job_id, state in (("A", "qa_warning"), ("B", "running")):
        (jobs / job_id).mkdir(parents=True)
        (jobs / job_id / "state.json").write_text(json.dumps({"job_id": job_id, "state": state, "audio": audio,
                                                              "created_at": 1}), encoding="utf-8")
    old = time.time() - 3600
    os.utime(jobs / "B" / "state.json", (old, old))  # looks stalled
    spawned = []
    monkeypatch.setattr(backend_module, "spawn", lambda args, **kw: spawned.append(args))
    app = Backend(clock=tuesday, fake_engine=True)
    monkeypatch.setattr(app, "_register_finished_jobs", lambda: None)
    monkeypatch.setattr(app, "_cli", lambda *a, **k: None)
    assert app.resume_jobs() == [] and spawned == []  # B is a duplicate of finished A
    monkeypatch.setattr(app, "transcription_check", lambda master, course: {"master": audio, "course": "구조역학"})
    result = app.transcription_start(True, audio, "구조역학")
    assert not result["ok"] and result["job"] in ("A", "B") and spawned == []


def test_class_seems_over_notice_never_stops_by_itself(semester):
    """Speakly §3: timetable end + 5 min (or 5 min of near silence) → a notice; the recording keeps going."""
    from datetime import timedelta

    now = {"at": datetime(2026, 9, 29, 10, 5).astimezone()}
    app = Backend(clock=lambda: now["at"], fake_engine=True)

    def status(**extra):
        app._on_event({"type": "status", "elapsed_ms": 1, "course": "구조역학", "quiet": False, **extra})

    app._on_event({"type": "started", "device": "Fake", "course": "구조역학", "mode": "mic", "session_dir": "s",
                   "keep_awake": True})
    assert app.state()["keep_awake"] is True
    now["at"] = now["at"].replace(hour=11, minute=54)
    status()
    assert app.state()["class_over"] is None  # 4 min after 11:50
    now["at"] = now["at"].replace(minute=56)
    status()
    over = app.state()["class_over"]
    assert over and "11:50" in over["reason"] and app.state()["phase"] == "recording"  # still recording
    app.class_over_answer("snooze")
    status()
    assert app.state()["class_over"] is None
    now["at"] += timedelta(minutes=11)
    status()
    assert app.state()["class_over"]  # asked again after the snooze
    app.class_over_answer("mute")
    now["at"] += timedelta(hours=1)
    status()
    assert app.state()["class_over"] is None  # not again for this recording


def test_class_seems_over_after_long_silence(semester):
    from datetime import timedelta

    now = {"at": datetime(2026, 9, 29, 10, 5).astimezone()}
    app = Backend(clock=lambda: now["at"], fake_engine=True)
    app._on_event({"type": "started", "device": "Fake", "course": "구조역학", "mode": "mic", "session_dir": "s"})
    for minutes in range(7):
        now["at"] = datetime(2026, 9, 29, 10, 20).astimezone() + timedelta(minutes=minutes)
        app._on_event({"type": "status", "elapsed_ms": 1, "course": "구조역학", "quiet": True, "paused": minutes == 6})
        if minutes == 5:
            assert "소리" in app.state()["class_over"]["reason"]


def test_mic_and_pc_sound_are_recorded_as_two_files_with_one_transcription(semester, monkeypatch, tmp_path):
    """User request 9/29: [PC 소리] adds a loopback file next to the mic; pause/stop reach both; one transcription."""
    spawned = []

    def fake_spawn(self, args, cwd=None):
        spawned.append(args)

    monkeypatch.setattr(backend_module.EngineLink, "spawn", fake_spawn)
    monkeypatch.setattr(backend_module.time, "sleep", lambda s: None)
    app = Backend(clock=tuesday, fake_engine=True)
    assert app.start("구조역학", "mic", "Fake", "user_pick", with_pc=True)["ok"]
    assert [a[a.index("--mode") + 1] for a in spawned] == ["mic", "loopback"]
    assert "--device-name" not in spawned[1] and spawned[1][spawned[1].index("--course") + 1] == "구조역학"
    mic_dir, pc_dir = tmp_path / "mic", tmp_path / "pc"
    mic_dir.mkdir(), pc_dir.mkdir()
    app.engine.session_dir, app.pc_engine.session_dir = mic_dir, pc_dir
    app._on_event({"type": "started", "device": "Fake", "course": "구조역학", "mode": "mic", "session_dir": str(mic_dir)})
    app._on_pc_event({"type": "started", "session_dir": str(pc_dir)})
    assert json.loads((mic_dir / "pair.json").read_text(encoding="utf-8")) == {"mic": str(mic_dir), "pc": str(pc_dir)}
    assert app.pause(True)["ok"] and (mic_dir / "control.pause").exists() and (pc_dir / "control.pause").exists()
    assert app.stop()["ok"] and (mic_dir / "control.stop").exists() and (pc_dir / "control.stop").exists()

    transcribed = []
    monkeypatch.setattr(app, "_maybe_auto_transcribe", lambda link: transcribed.append(link["master"]))
    monkeypatch.setattr(app, "_cli", lambda *a, **k: {"state": "linked", "master": a[2], "course": "구조역학"})
    app._after_recording(mic_dir, "main")
    assert transcribed == []  # waits: the PC sound is the one that gets transcribed
    app._after_recording(pc_dir, "pc")
    assert transcribed == [str(pc_dir)]


def test_pc_sound_failure_falls_back_to_transcribing_the_mic(semester, monkeypatch, tmp_path):
    monkeypatch.setattr(backend_module.EngineLink, "spawn", lambda self, args, cwd=None: None)
    monkeypatch.setattr(backend_module.time, "sleep", lambda s: None)
    app = Backend(clock=tuesday, fake_engine=True)
    app.start("구조역학", "mic", "Fake", "user_pick", with_pc=True)
    app._on_event({"type": "started", "device": "Fake", "course": "구조역학", "mode": "mic", "session_dir": "m"})
    app._on_pc_event({"type": "started", "session_dir": "p"})
    transcribed = []
    monkeypatch.setattr(app, "_maybe_auto_transcribe", lambda link: transcribed.append(link["master"]))
    monkeypatch.setattr(app, "_cli", lambda *a, **k: {"state": "linked", "master": a[2], "course": "구조역학"})
    app._after_recording(Path("m"), "main")
    app._on_pc_event({"type": "finalize_error", "message": "합성 실패"})
    deadline = time.monotonic() + 2
    while not transcribed and time.monotonic() < deadline:
        time.sleep(0.01)
    assert transcribed == ["m"]


def test_pc_sound_copy_is_named_apart():
    from datetime import date as _date

    from lecturehub.index.archive_copy import archive_name

    assert archive_name(_date(2026, 10, 2), "경제학원론", 1, ".mp3", " (PC 소리)") == "26.10.2 경제학원론 (PC 소리).mp3"
    assert archive_name(_date(2026, 10, 2), "경제학원론", 2, ".mp3") == "26.10.2 경제학원론 2.mp3"


def test_recordings_under_a_minute_are_never_auto_sent(semester, monkeypatch, tmp_path):
    """9/29 13:59: a 2-second mis-press was sent to Google and copied to 녹음본."""
    short, long_ = tmp_path / "short", tmp_path / "long"
    for folder, total in ((short, 2000), (long_, 6_000_000)):
        folder.mkdir()
        (folder / "stop.json").write_text(json.dumps({"total_ms": total}), encoding="utf-8")
    app = Backend(clock=tuesday, fake_engine=True)
    started = []
    monkeypatch.setattr(app, "auto_transcription_allowed", lambda course: True)
    monkeypatch.setattr(app, "transcription_start", lambda consent, master, course: started.append(master) or {"ok": True})
    app._maybe_auto_transcribe({"state": "linked", "master": str(short / "m.mp3"), "course": "구조역학"})
    app._maybe_auto_transcribe({"state": "linked", "master": str(long_ / "m.mp3"), "course": "구조역학"})
    assert started == [str(long_ / "m.mp3")]


def test_live_transcription_starts_only_for_consented_courses_at_low_priority(semester, monkeypatch, tmp_path):
    """D-059: remote transcription during recording, consented courses only; the app never doubles it later."""
    spawned = []

    class Proc:
        def poll(self):
            return None

        def wait(self):
            return 0

    def fake_spawn(args, **kw):
        spawned.append((args, kw))
        return Proc()

    monkeypatch.setattr(backend_module, "spawn", fake_spawn)
    app = Backend(clock=tuesday, fake_engine=True)
    monkeypatch.setattr(app, "_register_finished_jobs", lambda: None)
    monkeypatch.setattr(app, "auto_transcription_allowed", lambda course: course == "구조역학")
    app._start_live(str(tmp_path / "s1"), "토질역학")
    assert spawned == []  # no consent
    session = tmp_path / "s2"
    session.mkdir()
    app._start_live(str(session), "구조역학")
    args, kw = spawned[0]
    assert args[3:6] == ["transcribe", "live", str(session)] and kw.get("below_normal") is True
    jobs = semester / "_lecturehub" / "jobs" / "L"
    jobs.mkdir(parents=True)
    (jobs / "state.json").write_text(json.dumps({"job_id": "L", "state": "running", "audio": None,
                                                 "live": {"session_dir": str(session)}}), encoding="utf-8")
    master = str(session / "master.mp3")
    assert [s["job_id"] for s in app._jobs_for(master)] == ["L"]  # the live job counts: no second job
    monkeypatch.setattr(app, "transcription_check", lambda m, c: {"master": master, "course": "구조역학"})
    assert app.transcription_start(True, master, "구조역학")["ok"] is False  # alive: nothing new is started
    app._live["process"].poll = lambda: 0  # the live process died (e.g. the app was killed)
    result = app.transcription_start(True, master, "구조역학")
    assert result == {"ok": True, "resumed": "L"}  # review L2: the live job is resumed, never replaced
    assert spawned[-1][0][3:6] == ["transcribe", "resume", "L"]


def test_discard_while_recording_recycles_the_session_and_skips_linking(semester, monkeypatch, tmp_path):
    monkeypatch.setattr(backend_module.EngineLink, "spawn", lambda self, args, cwd=None: None)
    app = Backend(clock=tuesday, fake_engine=True)
    recycled, linked = [], []
    app.discard_hook = lambda name: recycled.append(name) or {"ok": True}
    monkeypatch.setattr(app, "_after_recording", lambda *a, **k: linked.append(a))
    session = tmp_path / "20260929_174645_loopback"
    session.mkdir()
    app.start("구조역학", "mic", "Fake", "user_pick")
    app.engine.session_dir = session
    app._on_event({"type": "started", "device": "Fake", "course": "구조역학", "mode": "mic", "session_dir": str(session)})
    assert app.discard()["ok"] and (session / "control.discard").exists()
    app._on_event({"type": "discarded", "session_dir": str(session), "report": {"total_ms": 1000}})
    deadline = time.monotonic() + 2
    while "recycled" not in (app.state()["summary"] or {}) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert recycled == ["20260929_174645_loopback"] and linked == []
    state = app.state()
    assert state["phase"] == "done" and state["summary"]["discarded"] and state["summary"]["recycled"]


def test_second_source_starts_and_stops_on_its_own(semester, monkeypatch, tmp_path):
    """User 2026-09-29: one record button; while it records a small button starts the other source on its own —
    own file, own stop, own transcription; the main button no longer stops it."""
    spawned = []
    monkeypatch.setattr(backend_module.EngineLink, "spawn", lambda self, args, cwd=None: spawned.append(args))
    app = Backend(clock=tuesday, fake_engine=True)
    assert app.start_companion()["ok"] is False  # nothing recording yet
    assert app.start("AI 외부 강의", "loopback", None, "user_pick")["ok"]  # PC sound only
    pc_dir, mic_dir = tmp_path / "pc", tmp_path / "mic"
    pc_dir.mkdir(), mic_dir.mkdir()
    app.engine.session_dir = pc_dir
    app._on_event({"type": "started", "device": "Spk", "course": "AI 외부 강의", "mode": "loopback",
                   "session_dir": str(pc_dir)})
    started = app.start_companion()
    assert started == {"ok": True, "mode": "mic"} and app.start_companion()["ok"] is False
    assert spawned[-1][spawned[-1].index("--mode") + 1] == "mic"
    assert spawned[-1][spawned[-1].index("--course") + 1] == "AI 외부 강의"
    app.pc_engine.session_dir = mic_dir
    app._on_pc_event({"type": "started", "session_dir": str(mic_dir)})
    pair = json.loads((mic_dir / "pair.json").read_text(encoding="utf-8"))
    assert pair == {"mic": str(mic_dir), "pc": str(pc_dir), "independent": True}
    assert app.state()["pc"]["mode"] == "mic" and app.recording
    assert app.stop_companion()["ok"] and (mic_dir / "control.stop").exists() and not (pc_dir / "control.stop").exists()
    assert app.start("x", "mic", None, "user_pick")["ok"] is False  # still recording the first one
    assert app.stop()["ok"] and (pc_dir / "control.stop").exists()

    transcribed = []
    monkeypatch.setattr(app, "_maybe_auto_transcribe", lambda link: transcribed.append(link["master"]))
    monkeypatch.setattr(app, "_cli", lambda *a, **k: {"state": "linked", "master": a[2], "course": "AI 외부 강의"})
    app._after_recording(pc_dir, "main")
    app._after_recording(mic_dir, "pc")
    assert transcribed == [str(pc_dir), str(mic_dir)]  # each covers its own span: both are transcribed


def test_main_stop_leaves_an_independent_second_source_running(semester, monkeypatch, tmp_path):
    monkeypatch.setattr(backend_module.EngineLink, "spawn", lambda self, args, cwd=None: None)
    app = Backend(clock=tuesday, fake_engine=True)
    app.start("구조역학", "mic", "Fake", "user_pick")
    mic_dir, pc_dir = tmp_path / "m", tmp_path / "p"
    mic_dir.mkdir(), pc_dir.mkdir()
    app.engine.session_dir = mic_dir
    app._on_event({"type": "started", "device": "Fake", "course": "구조역학", "mode": "mic", "session_dir": str(mic_dir)})
    app.start_companion()
    app.pc_engine.session_dir = pc_dir
    app._on_pc_event({"type": "started", "session_dir": str(pc_dir)})
    assert app.stop()["ok"] and (mic_dir / "control.stop").exists() and not (pc_dir / "control.stop").exists()
    app._on_event({"type": "stopped", "report": {"total_ms": 1}, "session_dir": str(mic_dir)})
    app._on_event({"type": "finalized", "master": "m.mp3", "session_dir": str(mic_dir)})
    assert app.state()["phase"] == "done" and app.state()["pc"]["state"] == "recording" and app.recording
    assert app.start("구조역학", "mic", "Fake", "user_pick")["ok"] is False  # the second one is still running


def test_fake_engine_never_touches_a_real_library(tmp_path, monkeypatch):
    """2026-09-29: a UI check with the synthetic source linked pending items into the real library and sent the
    sine recording through live transcription. Without the synthetic marker nothing is linked or sent."""
    root = tmp_path / "실제처럼 (합성)"
    root.mkdir()
    (root / "시간표.csv").write_bytes(codecs.BOM_UTF8 + TIMETABLE.encode("utf-8"))
    config = tmp_path / "recorder.toml"
    config.write_text("\n".join(["[recorder]", f"out_dir = '{tmp_path / 'rec'}'", 'device_name = "Fake"',
                                 f"timetable = '{root / '시간표.csv'}'"]), encoding="utf-8")
    monkeypatch.setenv("LECTUREHUB_RECORDER_CONFIG", str(config))
    app = Backend(clock=tuesday, fake_engine=True)
    called = []
    monkeypatch.setattr(app, "_cli", lambda *a, **k: called.append(a) or {})
    monkeypatch.setattr(backend_module, "spawn", lambda *a, **k: called.append(a))
    app._after_recording(tmp_path / "rec" / "s")
    app._start_live(str(tmp_path / "rec" / "s"), "구조역학")
    assert called == [] and app.state()["link"]["state"] == "skipped"


def test_a_synthetic_source_never_records_into_a_real_library(semester, tmp_path, monkeypatch):
    (semester / "_lecturehub" / "synthetic_root").unlink()  # now it looks like the user's real library
    app = Backend(clock=tuesday, fake_engine=True)
    out = app.start("구조역학", course_source="user_pick")
    assert not out["ok"] and "실제 라이브러리" in out["reason"]
    assert app.state()["phase"] == "idle" and not (semester / "_lecturehub" / "recordings").exists()
    app.out_dir = tmp_path / "scratch_rec"  # outside any library: allowed
    assert app._synthetic_out_refused() is None
    app.out_dir, app.fake_engine = semester / "_lecturehub" / "recordings", False  # the real source: allowed
    assert app._synthetic_out_refused() is None


def _recording(app, folder, monkeypatch, course="구조역학", mode="mic"):
    monkeypatch.setattr(backend_module.EngineLink, "spawn", lambda self, args, cwd=None: None)
    assert app.start(course, mode, "Fake", "user_pick")["ok"]
    session = folder / f"s_{mode}"
    session.mkdir(parents=True, exist_ok=True)
    app.engine.session_dir = session
    app._on_event({"type": "started", "device": "Fake", "course": course, "mode": mode, "session_dir": str(session)})
    return session


def test_auto_stop_after_fifteen_quiet_minutes_saves_and_marks_the_point(semester, monkeypatch, tmp_path):
    """User 2026-09-30 ("조용할 때만 멈춤"): no sound for 15 minutes → stop and save by itself; sound coming back
    or [계속] restarts the count; [이번 수업은 묻지 않기] turns it off."""
    from datetime import timedelta

    now = {"at": datetime(2026, 9, 29, 10, 20).astimezone()}
    app = Backend(clock=lambda: now["at"], fake_engine=True)
    session = _recording(app, tmp_path / "a", monkeypatch)

    def minute(m, quiet=True):
        now["at"] = datetime(2026, 9, 29, 10, 20).astimezone() + timedelta(minutes=m)
        app._on_event({"type": "status", "elapsed_ms": 900_000 + m * 60_000, "course": "구조역학", "quiet": quiet})

    minute(0)
    minute(5)
    notice = app.state()["class_over"]
    assert notice and notice["auto_stop_at_ms"]  # the card says when it will stop
    minute(6, quiet=False)  # someone speaks: no automatic stop is pending any more
    assert not app.state()["class_over"].get("auto_stop_at_ms") and not (session / "control.stop").exists()
    app.class_over_answer("snooze")
    for m in range(7, 22):
        minute(m)
    assert not (session / "control.stop").exists()  # 14.5 quiet minutes since minute 7
    minute(23)
    time.sleep(0.3)
    assert (session / "control.stop").exists()
    auto = json.loads((session / "auto_stop.json").read_text(encoding="utf-8"))
    assert auto["quiet_since_ms"] == 900_000 + 7 * 60_000 - 30_000 and "15분" in auto["reason"]
    app._on_event({"type": "stopped", "report": {"total_ms": 1, "cause": "user_stop"}, "session_dir": str(session)})
    assert "자동" in app.state()["summary"]["auto_stop"]["reason"]

    muted = Backend(clock=lambda: now["at"], fake_engine=True)
    other = _recording(muted, tmp_path / "b", monkeypatch)
    muted.class_over_answer("mute")
    for m in range(40):
        now["at"] = datetime(2026, 9, 29, 13, 0).astimezone() + timedelta(minutes=m)
        muted._on_event({"type": "status", "elapsed_ms": m * 60_000, "course": "구조역학", "quiet": True})
    time.sleep(0.2)
    assert not (other / "control.stop").exists()


def test_no_auto_stop_while_the_pc_sound_partner_still_plays(semester, monkeypatch, tmp_path):
    from datetime import timedelta

    now = {"at": datetime(2026, 9, 29, 20, 0).astimezone()}
    app = Backend(clock=lambda: now["at"], fake_engine=True)
    session = _recording(app, tmp_path, monkeypatch, course="AI 외부 강의")
    app.start_companion()
    app._on_pc_event({"type": "started", "session_dir": str(tmp_path / "pc")})
    for m in range(30):
        now["at"] += timedelta(minutes=1)
        app._on_pc_event({"type": "status", "elapsed_ms": m * 60_000, "quiet": False})
        app._on_event({"type": "status", "elapsed_ms": m * 60_000, "course": "AI 외부 강의", "quiet": True})
    time.sleep(0.2)
    assert not (session / "control.stop").exists() and app.state()["class_over"] is None


def test_a_long_pause_is_reminded_but_never_stopped(semester, monkeypatch, tmp_path):
    from datetime import timedelta

    now = {"at": datetime(2026, 9, 29, 10, 20).astimezone()}
    app = Backend(clock=lambda: now["at"], fake_engine=True)
    session = _recording(app, tmp_path, monkeypatch)
    for m in range(40):
        now["at"] += timedelta(minutes=1)
        app._on_event({"type": "status", "elapsed_ms": 1, "course": "구조역학", "quiet": True, "paused": True})
        if m == 8:
            assert app.state()["class_over"] is None
    time.sleep(0.2)
    notice = app.state()["class_over"]
    assert notice["kind"] == "paused" and "일시정지" in notice["reason"] and not (session / "control.stop").exists()


def test_next_class_is_offered_and_switch_saves_then_records_it(semester, monkeypatch, tmp_path):
    """User 2026-09-30: ten minutes before the next class (another course) ask; [바꾸기] saves this recording and
    starts the next class with the same source."""
    now = {"at": datetime(2026, 9, 29, 13, 45).astimezone()}
    app = Backend(clock=lambda: now["at"], fake_engine=True)
    spawned = []
    session = _recording(app, tmp_path, monkeypatch)
    monkeypatch.setattr(backend_module.EngineLink, "spawn", lambda self, args, cwd=None: spawned.append(args))
    monkeypatch.setattr(app, "_after_recording", lambda *a, **k: None)
    app._on_event({"type": "status", "elapsed_ms": 1, "course": "구조역학", "quiet": False})
    assert app.state().get("next_class") is None  # 15 minutes before 14:00
    now["at"] = datetime(2026, 9, 29, 13, 51).astimezone()
    app._on_event({"type": "status", "elapsed_ms": 1, "course": "구조역학", "quiet": False})
    offer = app.state()["next_class"]
    assert offer["course"] == "토질역학" and offer["start"] == "14:00"
    result = app.next_class_answer("switch")
    assert result == {"ok": True, "course": "토질역학"} and (session / "control.stop").exists()
    app._on_event({"type": "stopped", "report": {"total_ms": 1}, "session_dir": str(session)})
    app._on_event({"type": "finalized", "master": "m.mp3", "session_dir": str(session)})
    for _ in range(40):
        if spawned:
            break
        time.sleep(0.1)
    assert spawned and spawned[-1][spawned[-1].index("--course") + 1] == "토질역학"
    assert spawned[-1][spawned[-1].index("--mode") + 1] == "mic"
    app._on_event({"type": "started", "device": "Fake", "course": "토질역학", "mode": "mic", "session_dir": str(session)})
    app._on_event({"type": "status", "elapsed_ms": 1, "course": "토질역학", "quiet": False})
    assert app.state().get("next_class") is None


def test_discard_while_recording_also_throws_away_a_source_started_on_its_own(semester, monkeypatch, tmp_path):
    """User 2026-09-30: "1번째껀 버릴 수 있는데 2번째껀 못버림" — the trash took only the first source."""
    app = Backend(clock=tuesday, fake_engine=True)
    main = _recording(app, tmp_path, monkeypatch)
    app.start_companion()
    second = tmp_path / "second"
    second.mkdir()
    app.pc_engine.session_dir = second
    app._on_pc_event({"type": "started", "session_dir": str(second)})
    assert app.discard()["ok"]
    assert (main / "control.discard").exists() and (second / "control.discard").exists()


def test_reattached_recording_gets_the_notice_and_the_auto_stop(semester, monkeypatch, tmp_path):
    """Audit 2026-09-30: after an app restart the found recording never showed "수업이 끝난 것 같아요"."""
    from datetime import timedelta

    monkeypatch.setattr(backend_module.EngineLink, "attach",
                        lambda self, session_dir: setattr(self, "session_dir", session_dir))
    now = {"at": datetime(2026, 9, 29, 12, 30).astimezone()}
    app = Backend(clock=lambda: now["at"], fake_engine=True)
    session = tmp_path / "found"
    session.mkdir()
    started = int(datetime(2026, 9, 29, 10, 2).astimezone().timestamp() * 1000)
    (session / "session.json").write_text(json.dumps({"started_at": started, "course": "구조역학", "mode": "mic",
                                                      "device_name": "Fake"}), encoding="utf-8")
    app._attach(session)
    app._on_event({"type": "status", "elapsed_ms": 1, "course": "구조역학", "quiet": False})
    assert "11:50" in app.state()["class_over"]["reason"]
    for m in range(1, 17):
        now["at"] += timedelta(minutes=1)
        app._on_event({"type": "status", "elapsed_ms": m, "course": "구조역학", "quiet": True})
    deadline = time.monotonic() + 5  # the stop is written by a background thread; slow runners need longer than 0.3 s
    while not (session / "control.stop").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert (session / "control.stop").exists()

def _job(root, folder, provider, audio, state, **extra):
    path = root / "_lecturehub" / folder / extra.pop("job_id", state) / "state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"job_id": path.parent.name, "provider": provider, "audio": audio, "state": state,
                                "created_at": 1, **extra}, ensure_ascii=False), encoding="utf-8")


def test_engines_overview_counts_whisper_audio_and_mai_chunks(semester, monkeypatch):
    """The engine chip gets all three engines: Google quota, Groq audio seconds sent, MAI chunks today."""
    from lecturehub.transcription import groq, usage

    app = Backend(clock=tuesday, fake_engine=True)
    now = usage.now_ms()
    _job(semester, "jobs_whisper", "whisper_groq", "a.m4a", "done",
         chunks=[{"start_ms": 0, "end_ms": 1_800_000, "sent_at": [now - 600_000]},
                 {"start_ms": 1_800_000, "end_ms": 2_400_000, "sent_at": [now - 7_200_000]}])
    _job(semester, "jobs_mai", "mai_playground", "a.m4a", "done",
         chunks=[{"results": {"1": {"imported_at": now}}}], events=[{"type": "provider_rejection", "at": now}])
    monkeypatch.setattr(groq, "load_api_key", lambda: "gsk_SYNTHETIC")
    view = app.engines_overview()
    assert view["whisper"]["state"] == "ok"
    assert view["whisper"]["usage"]["hour_used_s"] == 1800 and view["whisper"]["usage"]["day_used_s"] == 2400
    assert view["whisper"]["usage"]["day_left_s"] == 28_800 - 2400
    assert view["mai"]["usage"]["today_chunks"] == 1 and view["mai"]["usage"]["today_rejections"] == 1
    assert "quota" in view["google"]


def test_recording_engines_say_why_a_button_is_off(semester, monkeypatch):
    from lecturehub.transcription import groq
    from lecturehub.ui import backend as backend_module

    app = Backend(clock=tuesday, fake_engine=True)
    master = semester / "녹음본" / "구조역학" / "26.9.8 구조역학.m4a"
    _job(semester, "jobs_whisper", "whisper_groq", str(master), "waiting_for_quota", next_eligible_at=123,
         chunks=[{"state": "done"}, {"state": "queued"}])
    monkeypatch.setattr(groq, "load_api_key", lambda: "gsk_SYNTHETIC")
    monkeypatch.setattr(backend_module, "_claude_exe", lambda: None)
    view = app.recording_engines(str(master))
    assert view["course"] == "구조역학"  # inferred from the 녹음본 folder
    w, m = view["engines"]["whisper"], view["engines"]["mai"]
    assert not w["can_start"] and "한도 대기" in w["why"] and w["job"]["next_eligible_at"] == 123
    assert w["job"]["chunks_done"] == 1 and w["job"]["chunks_total"] == 2
    assert not m["can_start"] and "Claude Code" in m["why"]
    assert app.engine_start("whisper", str(master))["ok"] is False


def test_engine_start_whisper_blank_course_needs_the_users_yes(semester, monkeypatch):
    from lecturehub.transcription import groq
    from lecturehub.ui import backend as backend_module

    app = Backend(clock=tuesday, fake_engine=True)
    master = semester / "_lecturehub" / "recordings" / "s" / "master.m4a"
    master.parent.mkdir(parents=True)
    master.write_bytes(b"x")
    monkeypatch.setattr(groq, "load_api_key", lambda: "gsk_SYNTHETIC")
    spawned = []
    monkeypatch.setattr(backend_module, "spawn", lambda args, **kw: spawned.append(args) or _Done())
    first = app.engine_start("whisper", str(master), "")
    assert first["needs_consent"] and "Groq" in first["notice"] and spawned == []
    assert app.engine_start("whisper", str(master), "", consent=True)["ok"]
    assert "--consent-once" in spawned[0] and "whisper" in spawned[0]


@pytest.mark.skipif(not edition.has("mai"), reason="MAI automation is excluded from the public edition")
def test_MAI_ASIDE_006_mai_button_uploads_through_aside_without_claude(semester, monkeypatch):
    """[MAI 실행] with the Aside CLI: prepare, then `mai run` in the background — no Claude window (2026-11-06)."""
    app = Backend(clock=tuesday, fake_engine=True)
    monkeypatch.setenv("LECTUREHUB_ASIDE_EXE", "aside.exe")
    monkeypatch.delenv("LECTUREHUB_MAI_BROWSER", raising=False)
    assert app.engine_status()["mai"] == "aside"
    prepared = subprocess.CompletedProcess([], 0, "MAI 작업 01SYNTHETICJOB: 조각 3개 × 1회\nmanifest: x\n", "")
    monkeypatch.setattr(backend_module.subprocess, "run", lambda *a, **k: prepared)
    monkeypatch.setattr(app, "_open_claude", lambda *a, **k: pytest.fail("Aside path must not open Claude"))
    spawned = []
    monkeypatch.setattr(backend_module, "spawn", lambda args, **kw: spawned.append((args, kw)) or _Done())
    result = app._mai_start("x.m4a", "구조역학", [])
    assert result["ok"] and result["via"] == "aside" and result["job_id"] == "01SYNTHETICJOB"
    args, kw = spawned[0]
    assert args[args.index("mai"):args.index("mai") + 3] == ["mai", "run", "01SYNTHETICJOB"] and kw["below_normal"]
    monkeypatch.setenv("LECTUREHUB_MAI_BROWSER", "chrome")  # the old Claude-in-Chrome path stays available
    assert app.engine_status()["mai"] in ("agent", "manual")


@pytest.mark.skipif(not edition.has("mai"), reason="MAI automation is excluded from the public edition")
def test_MAI_ASIDE_006_again_after_a_stopped_aside_run_resumes_the_same_job(semester, monkeypatch):
    app = Backend(clock=tuesday, fake_engine=True)
    master = semester / "_lecturehub" / "recordings" / "s" / "master.m4a"
    master.parent.mkdir(parents=True)
    master.write_bytes(b"x")
    monkeypatch.setenv("LECTUREHUB_ASIDE_EXE", "aside.exe")
    job = {"job_id": "01STOPPEDJOB", "state": "failed", "via": "aside", "label": "실패"}
    monkeypatch.setattr(app, "recording_engines", lambda *a, **k: {"engines": {"mai": {
        "job": job, "can_start": True, "why": "", "again": True}}})
    monkeypatch.setattr(app, "_mai_start", lambda *a, **k: pytest.fail("must not make new chunks"))
    spawned = []
    monkeypatch.setattr(backend_module, "spawn", lambda args, **kw: spawned.append(args) or _Done())
    result = app.engine_start("mai", str(master), "구조역학")
    assert result["ok"] and result["job_id"] == "01STOPPEDJOB" and "01STOPPEDJOB" in spawned[0]


def test_CAP_007_captions_button_needs_consent_then_starts_in_the_background(semester, monkeypatch):
    app = Backend(clock=tuesday, fake_engine=True)
    spawned = []
    monkeypatch.setattr(backend_module, "spawn", lambda args, **kw: spawned.append(args) or _Done())
    first = app.captions_start("구조역학", "mic")
    assert first["needs_consent"] and not first["once_only"] and spawned == []
    assert app.captions_start("구조역학", "mic", consent=True)["needs_consent"]  # a named course needs the native dialog
    ok = app.captions_start("구조역학", "pc", store_consent=True)
    assert ok["ok"] and spawned[0][spawned[0].index("captions"):spawned[0].index("captions") + 2] == ["captions", "start"]
    assert spawned[0][spawned[0].index("--source") + 1] == "pc" and "--consent-once" not in spawned[0]
    consent = json.loads((semester / "_lecturehub" / "consent.json").read_text(encoding="utf-8"))
    assert consent["google_live"]["구조역학"]["granted_by"] == "user_app_dialog"
    assert "--confirmed" not in spawned[0]  # Gemini Live alone unless Chirp is ticked
    blank = app.captions_start("", "mic", consent=True, chirp=True)
    assert blank["ok"] and "--consent-once" in spawned[1] and "--confirmed" in spawned[1]
    assert "google_live" in json.loads((semester / "_lecturehub" / "consent.json").read_text(encoding="utf-8"))
    assert "" not in consent["google_live"]


def test_CAP_007_api_stores_course_consent_only_with_the_native_token(semester, monkeypatch):
    from lecturehub.ui.app import Api

    app = Backend(clock=tuesday, fake_engine=True)
    api = Api(app)
    seen = []
    monkeypatch.setattr(app, "captions_start",
                        lambda course, source, consent, store, chirp, quick: seen.append((store, chirp)) or {"ok": True})
    assert api.captions_start("구조역학", "mic", True, "forged-token")["needs_confirm"]
    api.captions_start("구조역학", "mic", True, None)
    api.captions_start("", "mic", True, None, True)
    assert seen == [(False, False), (False, True)]


@pytest.mark.skipif(not edition.has("qa"), reason="Q&A is excluded from the public edition")
def test_QA_006_helper_needs_consent_then_starts_captions_and_the_helper(semester, monkeypatch):
    """User 2026-11-07: presentation Q&A helper. Captions off: they start (Gemini Live) next to the helper."""
    app = Backend(clock=tuesday, fake_engine=True)
    spawned = []
    monkeypatch.setattr(backend_module, "spawn", lambda args, **kw: spawned.append(args) or _Done())
    folder = semester / "구조역학" / "과제" / "합성 발표"
    folder.mkdir(parents=True)
    (folder / "합성덱.pptx").write_bytes(b"x")
    assert not app.qa_start("", str(folder))["ok"]  # consent is per course
    first = app.qa_start("구조역학", str(folder))
    assert first["needs_consent"] and set(first["needs"]) == {"claude_qa", "google_live"} and spawned == []
    ok = app.qa_start("구조역학", str(folder), store_qa=True, store_live=True)
    assert ok["ok"] and len(spawned) == 2
    cap, helper = spawned
    assert cap[cap.index("captions"):cap.index("captions") + 2] == ["captions", "start"] and "--no-quick" not in cap
    assert helper[helper.index("qa"):helper.index("qa") + 2] == ["qa", "start"]
    assert helper[helper.index("--folder") + 1] == str(folder)
    consent = json.loads((semester / "_lecturehub" / "consent.json").read_text(encoding="utf-8"))
    assert consent["claude_qa"]["구조역학"]["granted_by"] == "user_app_dialog"


@pytest.mark.skipif(not edition.has("qa"), reason="Q&A is excluded from the public edition")
def test_QA_006_running_chirp_gets_gemini_live_for_the_qa_and_back_after(semester, monkeypatch):
    from lecturehub.transcription import captions

    app = Backend(clock=tuesday, fake_engine=True)
    spawned, switched = [], []
    monkeypatch.setattr(backend_module, "spawn", lambda args, **kw: spawned.append(args) or _Done())
    monkeypatch.setattr(app, "captions_status", lambda: {"running": True, "engines": {"confirmed": "chirp_3@us", "quick": None}})
    monkeypatch.setattr(captions, "request_quick", lambda root, on: switched.append(on))
    folder = semester / "합성 자료"
    folder.mkdir()
    ok = app.qa_start("구조역학", str(folder), store_qa=True)
    assert ok["ok"] and switched == [True] and len(spawned) == 1 and "qa" in spawned[0]
    app.qa_stop()
    assert switched == [True, False]  # Chirp alone again, as before the Q&A


@pytest.mark.skipif(not edition.has("qa"), reason="Q&A is excluded from the public edition")
def test_QA_016_ending_the_helper_stops_the_captions_it_started(semester, monkeypatch):
    """User 2026-11-17: "끝내도 계속 녹음이 되고" — captions the helper switched on end with [끝내기];
    captions the user had on before stay on."""
    from lecturehub.transcription import captions

    app = Backend(clock=tuesday, fake_engine=True)
    stops, switched = [], []
    monkeypatch.setattr(backend_module, "spawn", lambda args, **kw: _Done())
    monkeypatch.setattr(captions, "request_stop", lambda root: stops.append(root))
    monkeypatch.setattr(captions, "request_quick", lambda root, on: switched.append(on))
    folder = semester / "합성 자료"
    folder.mkdir()
    running = {"on": False}
    monkeypatch.setattr(app, "captions_status", lambda: {"running": running["on"], "engines": {"quick": "gemini"}})
    assert app.qa_start("구조역학", str(folder), store_qa=True, store_live=True)["ok"]  # captions were off: it starts them
    assert app.qa_stop() == {"ok": True, "captions_stopped": True} and len(stops) == 1
    running["on"] = True  # now the user's own captions are on
    assert app.qa_start("구조역학", str(folder), store_qa=True)["ok"]
    assert app.qa_stop() == {"ok": True} and len(stops) == 1 and switched == [True]


@pytest.mark.skipif(not edition.has("qa"), reason="Q&A is excluded from the public edition")
def test_QA_007_api_stores_qa_consent_only_with_the_native_token(semester, monkeypatch):
    from lecturehub.ui.app import Api

    app = Backend(clock=tuesday, fake_engine=True)
    api = Api(app)
    seen = []
    monkeypatch.setattr(app, "qa_start", lambda course, folder, store_qa, store_live, model, pack="": seen.append((store_qa, store_live)) or {"ok": True})
    assert api.qa_start("구조역학", "x", "forged", None)["needs_confirm"]
    assert api.qa_start("구조역학", "x", None, "forged")["needs_confirm"]
    api.qa_start("구조역학", "x", None, None)
    assert seen == [(False, False)]


@pytest.mark.skipif(not edition.has("qa"), reason="Q&A is excluded from the public edition")
def test_QA_008_helper_starts_from_a_presentation_pack_with_its_course(semester, monkeypatch):
    """P-046: the user picks a presentation, not a course; consent and captions use the pack's course."""
    from lecturehub.transcription import qa_packs
    from lecturehub.ui.app import Api

    app = Backend(clock=tuesday, fake_engine=True)
    spawned = []
    monkeypatch.setattr(backend_module, "spawn", lambda args, **kw: spawned.append(args) or _Done())
    folder = semester / "구조역학" / "과제" / "합성 발표"
    folder.mkdir(parents=True)
    (folder / "대본.md").write_text("합성 대본", encoding="utf-8")
    made = qa_packs.create(semester, "합성 발표", folder)  # packs are made in Claude Code (P-049)
    assert made["ok"] and made["course"] == "구조역학" and [p["id"] for p in app.qa_packs()["packs"]] == [made["id"]]
    first = app.qa_start("", "", pack=made["id"])
    assert first["needs_consent"] and first["course"] == "구조역학" and spawned == []
    assert app.qa_start("", "", store_qa=True, store_live=True, pack=made["id"])["ok"]
    helper = spawned[-1]
    assert helper[helper.index("--pack") + 1] == made["id"] and "--folder" not in helper and "--course" not in helper
    assert not app.qa_start("", "", pack="../밖")["ok"]
    seen = []
    api = Api(app)
    monkeypatch.setattr(app, "qa_start", lambda *a: seen.append(a) or {"ok": True})
    monkeypatch.setattr(api, "_take_consent_token", lambda token, provider, course: seen.append((provider, course)) or True)
    api.qa_start("토질역학", "", "tok", None, "sonnet", made["id"])  # the page's course is ignored for a pack
    assert seen[0] == ("claude_qa", "구조역학") and seen[1][0] == "구조역학" and seen[1][-1] == made["id"]
    assert qa_packs.load(semester, made["id"])["folder_ok"]
    for gone in ("qa_pack_create", "qa_pack_folder", "qa_pack_prepare", "qa_pack_open", "qa_pick_folder", "qa_suggest_folder"):
        assert not hasattr(api, gone) and not hasattr(app, gone), gone  # P-049: edited in Claude Code only


@pytest.mark.skipif(not edition.has("mai"), reason="MAI automation is excluded from the public edition")
def test_mai_button_never_consents_for_a_blank_course(semester, monkeypatch):
    app = Backend(clock=tuesday, fake_engine=True)
    master = semester / "_lecturehub" / "recordings" / "s" / "master.m4a"
    master.parent.mkdir(parents=True)
    master.write_bytes(b"x")
    monkeypatch.setattr(app, "_mai_start", lambda *a, **k: pytest.fail("must not prepare without consent"))
    app.mai_prepare(str(master))
    end = time.monotonic() + 5
    while (app.state().get("mai") or {}).get("state") == "running" and time.monotonic() < end:
        time.sleep(0.05)
    assert app.state()["mai"]["state"] == "failed" and "동의" in app.state()["mai"]["error"]


def test_source_is_remembered_per_course_from_real_starts(semester, monkeypatch):
    """User 2026-09-30 (22:15 on 9/29: started with the mic, restarted with PC sound 30 s later): the source each
    course was last recorded with comes back; the last 수업 외 item is remembered for the course question."""
    monkeypatch.setattr(backend_module.EngineLink, "spawn", lambda self, args, cwd=None: None)
    app = Backend(clock=tuesday, fake_engine=True)
    app.add_extra("AI 외부 강의")
    assert app.init_info()["prefs"] == {"source_by_course": {}, "last_extra": None}
    assert app.start("AI 외부 강의", "loopback", None, "user_pick")["ok"]
    app._state["phase"] = "idle"
    assert app.start("구조역학", "mic", "Fake", "user_pick", with_pc=True)["ok"]
    prefs = Backend(clock=tuesday, fake_engine=True).init_info()["prefs"]
    assert prefs == {"source_by_course": {"AI 외부 강의": "pc", "구조역학": "both"}, "last_extra": "AI 외부 강의"}
    assert json.loads((semester / "_lecturehub" / "ui_prefs.json").read_text(encoding="utf-8"))["last_extra"] == "AI 외부 강의"


def test_a_recording_under_a_minute_waits_for_keep_or_discard(semester, monkeypatch, tmp_path):
    """User 2026-09-30: a mis-press must not become a lecture by itself — [버리기] or [남기기]."""
    monkeypatch.setattr(backend_module.EngineLink, "spawn", lambda self, args, cwd=None: None)
    app = Backend(clock=tuesday, fake_engine=True)
    linked, recycled = [], []
    monkeypatch.setattr(app, "_after_recording", lambda *a, **k: linked.append(a))
    app.discard_hook = lambda name: recycled.append(name) or {"ok": True}

    def record(seconds, name):
        app._state["phase"] = "idle"
        assert app.start("구조역학", "mic", "Fake", "user_pick")["ok"]
        session = tmp_path / name
        session.mkdir()
        app._on_event({"type": "started", "device": "Fake", "course": "구조역학", "mode": "mic", "session_dir": str(session)})
        app._on_event({"type": "stopped", "report": {"total_ms": seconds * 1000}, "session_dir": str(session)})
        app._on_event({"type": "finalized", "master": str(session / "m.mp3"), "session_dir": str(session)})
        return session

    record(20, "short1")
    assert app.state()["link"]["state"] == "short" and linked == []
    assert app.short_answer("discard")["ok"]
    time.sleep(0.2)
    assert recycled == ["short1"] and app.state()["link"]["state"] == "discarded" and app.state()["short"] is None
    record(40, "short2")
    assert app.short_answer("keep")["ok"]
    time.sleep(0.2)
    assert [Path(a[0]).name for a in linked] == ["short2"]
    record(61, "long")
    time.sleep(0.2)
    assert [Path(a[0]).name for a in linked] == ["short2", "long"] and app.state()["short"] is None
    assert app.short_answer("keep")["ok"] is False  # nothing held
    app.discard_hook = lambda name: {"ok": False, "error": "휴지통을 쓸 수 없습니다"}
    record(10, "short3")
    assert app.short_answer("discard")["ok"]
    time.sleep(0.2)
    failed = app.state()["link"]
    assert failed == {"state": "discard_failed", "reason": "휴지통을 쓸 수 없습니다"}  # not "회차 연결 실패"
    assert [Path(a[0]).name for a in linked] == ["short2", "long"]  # a failed discard is not linked behind the user's back


def test_IMPORT_011_an_imported_recording_is_sent_by_itself_only_as_the_lectures_first_source(semester, monkeypatch):
    """P-024: consent decides, a class that is already covered is not sent twice, nothing new next to a recording."""
    app = Backend(clock=tuesday, fake_engine=True)
    started = []
    monkeypatch.setattr(app, "transcription_start", lambda consent, master=None, course=None: started.append(
        (consent, master, course)) or {"ok": True})
    (semester / "_lecturehub" / "consent.json").write_text(json.dumps({"gemini_transcribe": {
        "구조역학": {"granted_by": "user_app_dialog", "authority": "confirmed_by_user"}}}, ensure_ascii=False), encoding="utf-8")
    link = {"state": "linked", "master": "a.m4a", "course": "구조역학", "duration_ms": 75 * 60_000,
            "other_recordings": 0, "transcripts": 0}
    before = app.state()["library_version"]
    assert app.after_import(link) == {"started": ["google"], "why": ""}
    assert started == [(False, "a.m4a", "구조역학")] and app.state()["library_version"] == before + 1
    assert app.after_import({**link, "course": "토질역학"}) == {"started": [], "why": "이 과목은 아직 전사 동의가 없습니다"}
    assert "이미 녹음이나 전사" in app.after_import({**link, "other_recordings": 1})["why"]
    assert "이미 녹음이나 전사" in app.after_import({**link, "transcripts": 1})["why"]
    assert "1분보다 짧은" in app.after_import({**link, "duration_ms": 20_000})["why"]
    app._state["phase"] = "recording"
    assert app.after_import(link) == {"started": [], "why": "녹음 중이라 Google 전사는 시작하지 않았습니다"}
    assert len(started) == 1


def test_IMPORT_012_a_file_that_needs_converting_waits_for_the_recording_to_end():
    """AGENTS §2.7: ffmpeg is not run next to a recording; a plain copy is."""
    from lecturehub.ui.app import Api, _NoLibrary
    from lecturehub.ui.library import Library

    assert Api(None, _NoLibrary(".", "합성 오류"))._import_suffixes(True) == []  # D-050: no library, no crash

    class Lib:
        def __init__(self):
            self.seen = []

        import_suffixes = staticmethod(Library.import_suffixes)

        def import_commit(self, item, today):
            self.seen.append(item["path"])
            return {"ok": True, "master": item["path"], "course": "구조역학"}

    class Back:
        phase = "recording"

        def state(self):
            return {"phase": self.phase}

        def after_import(self, link):
            return {"started": [], "why": ""}

    lib, back = Lib(), Back()
    api = Api(back, lib)
    assert "녹음 중에는 형식을 바꿔야" in api.import_commit({"path": "통화.amr"})["error"] and lib.seen == []
    assert api.import_commit({"path": "음성.m4a"})["transcription"] == {"started": [], "why": ""}
    back.phase = "idle"
    assert api.import_commit({"path": "통화.amr"})["ok"] and lib.seen == ["음성.m4a", "통화.amr"]
    dropped = []
    api._main = type("W", (), {"evaluate_js": lambda self, js: dropped.append(js)})()
    api._on_drop({"dataTransfer": {"files": [{"name": "음성.m4a", "pywebviewFullPath": "C:/받은 파일/음성.m4a"},
                                             {"name": "경로 없음.m4a"}]}})
    assert dropped == [("window.__lecturehubDropped && window.__lecturehubDropped("
                        f"{json.dumps(['C:/받은 파일/음성.m4a'])}, {json.dumps(['음성.m4a', '경로 없음.m4a'])})")]


def test_TLANG_006_engine_buttons_carry_the_speech_language(semester, monkeypatch):
    """User 2026-10-19: a German video recorded as PC sound came back as two lines — the recording's language is
    picked on the lecture screen; Google goes again only in another language (quota guard kept)."""
    from lecturehub.transcription import gemini, groq
    from lecturehub.ui import backend as backend_module

    class _Running:
        pid = 1

        def wait(self):
            return 0

        def poll(self):
            return 0

    app = Backend(clock=tuesday, fake_engine=True)
    master = semester / "_lecturehub" / "recordings" / "s" / "master.mp3"
    master.parent.mkdir(parents=True)
    master.write_bytes(b"x")
    (semester / "_lecturehub" / "transcribe_languages.json").write_text('{"구조역학": "de"}', encoding="utf-8")
    monkeypatch.setattr(gemini, "load_api_key", lambda: "SYNTHETIC")
    monkeypatch.setattr(groq, "load_api_key", lambda: "gsk_SYNTHETIC")
    spawned = []
    monkeypatch.setattr(backend_module, "spawn", lambda args, **kw: spawned.append(args) or _Running())
    view = app.recording_engines(str(master), "구조역학")
    assert view["language"] == "de" and "auto" in view["languages"]  # the course default is pre-selected
    _job(semester, "jobs", "gemini_transcribe", str(master), "done", preset={"language": "ko"})
    assert app.recording_engines(str(master), "구조역학")["engines"]["gemini_transcribe"]["job"]["language"] == "ko"
    same = app.engine_start("google", str(master), "구조역학", consent=True, language="ko")
    assert not same["ok"] and "같은 언어" in same["reason"] and spawned == []
    other = app.engine_start("google", str(master), "구조역학", consent=True, language="de")
    assert other["ok"], other
    args = spawned[-1]
    assert args[args.index("--language") + 1] == "de" and "--again" in args
    assert app.engine_start("whisper", str(master), "구조역학", consent=True, language="auto")["ok"]
    assert spawned[-1][spawned[-1].index("--language") + 1] == "auto"


def test_audit2_start_while_finalizing_says_so_and_a_stuck_merge_is_let_go(semester):
    app = Backend(clock=tuesday, fake_engine=True)
    app._state.update(phase="finalizing", summary={"session_dir": "s"})
    app._finalizing_since = time.monotonic()
    result = app.start("구조역학")
    assert not result["ok"] and "이전 녹음을 정리하는 중" in result["reason"] and "최대 10분" in result["reason"]
    assert "이미 녹음 중" not in result["reason"]
    stale = object()
    app.engine = stale
    app._finalizing_since = time.monotonic() - app.FINALIZE_LIMIT_S - 1
    assert app.start("구조역학")["ok"]  # past the limit the next class records anyway
    try:
        state = wait_phase(app, "recording")
        assert state["phase"] == "recording"
        app._dispatch_event(stale, {"type": "stopped", "session_dir": "old"})  # the old engine cannot touch it
        assert app.state()["phase"] == "recording"
    finally:
        app.stop()
        wait_phase(app, "done", timeout=60)


def test_an_earlier_engine_that_ends_late_does_not_break_the_next_recording(semester, monkeypatch, tmp_path):
    """An engine says ``finalized`` (the next recording may start), then tidies up and only then ends. Its process
    ending used to be applied to the recording that had started meanwhile: the screen went to "error" and the
    running recording could not be stopped from the app (review 2026-11-19)."""
    monkeypatch.setattr(backend_module.EngineLink, "spawn", lambda self, args, cwd=None: None)
    app = Backend(clock=tuesday, fake_engine=True)
    monkeypatch.setattr(app, "_after_recording", lambda *a, **k: None)
    first, second = tmp_path / "20260929_090000_mic", tmp_path / "20260929_103000_mic"
    first.mkdir()
    second.mkdir()

    assert app.start("구조역학", "mic", "Fake", "user_pick")["ok"]
    old = app.engine
    old.session_dir = first
    old.on_event({"type": "started", "device": "Fake", "course": "구조역학", "mode": "mic", "session_dir": str(first)})
    old.on_event({"type": "stopped", "session_dir": str(first), "bookmarks": 0,
                  "report": {"cause": "user_stop", "total_ms": 1000, "total_samples": 16000, "partial": False}})
    old.on_event({"type": "finalized", "session_dir": str(first), "master": str(first / "master.mp3"),
                  "total_ms": 1000, "duration_difference_ms": 0})
    assert app.state()["phase"] == "done"

    assert app.start("구조역학", "mic", "Fake", "user_pick")["ok"]  # the next class, while the first engine tidies up
    new = app.engine
    assert new is not old
    old.on_event({"type": "exited", "code": 0})  # ...and ends while the new one is still starting
    assert app.state()["phase"] == "starting" and not app.state()["error"]
    new.session_dir = second
    new.on_event({"type": "started", "device": "Fake", "course": "구조역학", "mode": "mic", "session_dir": str(second)})
    assert app.state()["phase"] == "recording"

    old.on_event({"type": "compacted", "session_dir": str(first), "result": {"state": "compacted"}})
    old.on_event({"type": "exited", "code": 0})  # or ends once the new one records
    state = app.state()
    assert state["phase"] == "recording" and not state["error"]
    assert app.stop()["ok"] and (second / "control.stop").exists()  # the running recording can still be stopped
    new.on_event({"type": "exited", "code": 1})  # the CURRENT engine dying is still noticed
    assert app.state()["phase"] == "error"
