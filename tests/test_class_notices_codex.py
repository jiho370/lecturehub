"""Class notices through ChatGPT (Codex), D-073 (2026-11-24).

Synthetic data only. The Codex CLI is never run: the scan gets a fake client, or ``CodexText`` gets a fake engine
whose ``run_json`` records what it was given. No network.
"""
import json
import shutil
import subprocess
import threading
from contextlib import closing
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from lecturehub.core import db, settings
from lecturehub.index import calendar_events as cal
from lecturehub.index import class_notices as cn
from lecturehub.transcription.consent import ConsentStore

TODAY = date(2026, 10, 20)
COURSE_A, COURSE_B = "합성 과목 가", "합성 과목 나"
SAID = "그리고 그 주 금요일도 제가 학회가 있어서 11월에 보게 될 거예요(합성)"


def closure(text="그 주 금요일도 제가 학회가 있어서", line="E1#0"):
    return {"notices": [{
        "kind": "closure", "date": "2026-10-30", "date_precision": "day", "date_text": "그 주 금요일",
        "date_reasoning": "합성", "summary": "10/30(금) 휴강(합성)", "speaker": "professor", "certainty": "medium",
        "stt_note": "", "already_known": False, "quotes": [{"line": line, "text": text}]}]}


class Grants:
    """Consent per (provider, course)."""

    def __init__(self, *pairs):
        self.pairs = set(pairs)

    def granted(self, provider, course):
        return (provider, course) in self.pairs


def _make(connection):
    db.migrate(connection)
    for cid, name in (("ca", COURSE_A), ("cb", COURSE_B)):
        connection.execute("INSERT INTO Course VALUES (?,?,?,?,'26-2')", (cid, cid.upper(), name, cid))
        connection.execute("INSERT INTO Schedule (course_id, weekday, start, end) VALUES (?, 4, 37800000, 42300000)",
                           (cid,))
    for lid, cid, tid in (("la", "ca", "ta"), ("lb", "cb", "tb")):
        connection.execute("INSERT INTO Lecture VALUES (?,?,'2026-10-16',1,NULL,NULL)", (lid, cid))
        connection.execute("INSERT INTO Transcript (id,lecture_id,media_id,engine,role,role_decided_by,status,"
                           "source_file,sha256) VALUES (?, ?, NULL, 'mai', 'timeline', 'policy', 'done', ?, ?)",
                           (tid, lid, tid, tid))
        connection.execute("INSERT INTO TranscriptSegment (transcript_id,seq,start_ms,text,chunk_index) "
                           "VALUES (?,0,0,?,0)", (tid, SAID))
    # calendar: one entry of course A, one of course B, one with no course; a timetable change with no course
    cal.add(connection, kind="exam", title="가 과목 중간고사(합성)", day="2026-10-22", course_id="ca")
    cal.add(connection, kind="exam", title="나 과목 중간고사(합성)", day="2026-10-23", course_id="cb")
    cal.add(connection, kind="other", title="개인 일정 과목없음(합성)", day="2026-10-24", course_id=None)
    connection.execute("INSERT INTO ScheduleException (course_id, date, kind, note) VALUES (NULL, '2026-10-27', '휴강', '학교 행사 과목없음(합성)')")
    connection.execute("INSERT INTO ScheduleException (course_id, date, kind, note) VALUES ('ca', '2026-10-28', '보강', '가 과목 보강(합성)')")
    connection.commit()


@pytest.fixture
def library(tmp_path):
    with closing(db.connect(tmp_path / "lecturehub.sqlite")) as connection:
        _make(connection)
        yield connection


def _prompt_of(ai, course):
    return next(p for p in ai.prompts if f"과목: {course}" in p)


# ---------------------------------------------------------------- engine choice and defaults
def test_NOTICE_CODEX_001_default_engine_is_codex_in_both_editions(monkeypatch):
    """D-075 (owner 2026-11-25, "GPT를 기본으로, 전부"): ChatGPT (Codex) is the default in the public AND the full
    edition; a saved choice of google is still honoured in both."""
    for name in ("public", "full"):
        monkeypatch.setenv("LECTUREHUB_EDITION", name)
        assert cn.default_engine() == "codex" and cn.engine() == "codex", name
    assert cn.set_engine("codex") == "codex" and settings.get("class_notices_engine") == "codex"
    for name in ("public", "full"):
        monkeypatch.setenv("LECTUREHUB_EDITION", name)
        assert cn.set_engine("google") == "google" and cn.engine() == "google", name  # the stored choice wins in both
    with pytest.raises(ValueError):
        cn.set_engine("claude")
    settings.save({"class_notices_engine": "something else"})
    assert cn.engine() == "codex"  # an unknown value counts as not set
    assert set(cn.ENGINES) == {"codex", "google"} and cn.PROVIDERS == {"google": "gemini_transcribe",
                                                                      "codex": "codex_notices"}


def test_NOTICE_CODEX_002_the_settings_answer_carries_the_engine_and_the_switch_stays(tmp_path, monkeypatch):
    from lecturehub.ui.library import Library

    monkeypatch.setenv("LECTUREHUB_EDITION", "public")
    monkeypatch.setattr("lecturehub.ai.codex.CodexEngine.executable", lambda self: None)
    setting = Library(tmp_path).notices_setting(TODAY)
    assert setting["ok"] and setting["engine"] == "codex" and setting["default_engine"] == "codex"
    assert setting["codex_found"] is False and set(setting["engines"]) == {"codex", "google"}
    changed = Library(tmp_path).notices_setting(TODAY, engine="google")
    assert changed["ok"] and changed["engine"] == "google" and changed["needs_consent"] == []
    assert Library(tmp_path).notices_setting(TODAY, engine="openai")["ok"] is False  # refused, nothing stored
    assert cn.engine() == "google"


# ---------------------------------------------------------------- consent per course, per provider
def test_NOTICE_CODEX_003_codex_sends_only_courses_with_codex_notices_consent(library, tmp_path):
    ai = cn.FakeText(closure())
    consent = Grants(("codex_notices", COURSE_A), ("gemini_transcribe", COURSE_B))
    result = cn.scan(library, tmp_path, TODAY, client=ai, consent=consent, engine_name="codex")
    assert len(ai.prompts) == 1 and f"과목: {COURSE_A}" in ai.prompts[0]  # B has Google consent only: not sent
    assert result["engine"] == "codex" and result["needs_consent"] == [COURSE_B]
    assert [s["course"] for s in result["skipped"]] == [COURSE_B] and "ChatGPT(Codex)" in result["skipped"][0]["reason"]
    state = json.loads((tmp_path / "_lecturehub" / "notices" / "state.json").read_text(encoding="utf-8"))
    assert "la" in state and "lb" not in state  # B is left alone, read once it is allowed
    assert cn.usage(tmp_path, TODAY)["by_engine"] == {"codex": 1}


def test_NOTICE_CODEX_004_google_does_not_take_codex_consent_and_vice_versa(library, tmp_path):
    ai = cn.FakeText({"notices": []})
    consent = Grants(("codex_notices", COURSE_A), ("gemini_transcribe", COURSE_B))
    result = cn.scan(library, tmp_path, TODAY, client=ai, consent=consent, engine_name="google")
    assert len(ai.prompts) == 1 and f"과목: {COURSE_B}" in ai.prompts[0]
    assert "needs_consent" not in result and cn.usage(tmp_path, TODAY)["by_engine"] == {"google": 1}


def test_NOTICE_CODEX_005_codex_note_consent_is_not_codex_notices_consent(library, tmp_path):
    """The store as the app writes it: a course allowed for notes (codex_note) is not sent for notices."""
    store = ConsentStore(tmp_path / "_lecturehub" / "consent.json")
    store.grant("codex_note", COURSE_A, 1, "합성", by="user_app_dialog")
    store.grant("ai_app_link", COURSE_A, 1, "합성", by="user_app_dialog")
    store.grant("codex_notices", COURSE_B, 1, "합성", by="user_app_dialog")
    ai = cn.FakeText({"notices": []})
    result = cn.scan(library, tmp_path, TODAY, client=ai, consent=store, engine_name="codex")
    assert len(ai.prompts) == 1 and f"과목: {COURSE_B}" in ai.prompts[0] and result["needs_consent"] == [COURSE_A]


def test_NOTICE_CODEX_006_public_edition_has_no_all_courses_consent_for_codex_notices(tmp_path, monkeypatch):
    monkeypatch.setenv("LECTUREHUB_EDITION", "public")
    store = ConsentStore(tmp_path / "consent.json")
    with pytest.raises(ValueError):
        store.grant_all("codex_notices", 1, "user_app_dialog", "합성")
    path = tmp_path / "consent.json"
    path.write_text(json.dumps({"codex_notices": {"*": {"authority": "confirmed_by_user"}}}), encoding="utf-8")
    assert not store.granted("codex_notices", COURSE_A)  # an entry written some other way does not count


# ---------------------------------------------------------------- what one request carries
def test_NOTICE_CODEX_007_entries_without_a_course_are_not_in_the_codex_prompt(library, tmp_path):
    both = Grants(("codex_notices", COURSE_A), ("gemini_transcribe", COURSE_A))
    codex = cn.FakeText({"notices": []})
    cn.scan(library, tmp_path, TODAY, client=codex, consent=both, engine_name="codex", force=True)
    google = cn.FakeText({"notices": []})
    cn.scan(library, tmp_path, TODAY, client=google, consent=both, engine_name="google", force=True)
    sent, before = _prompt_of(codex, COURSE_A), _prompt_of(google, COURSE_A)
    for own in ("가 과목 중간고사(합성)", "가 과목 보강(합성)", "금 10:30–11:45", "2026-10-16(금)", SAID[:12]):
        assert own in sent, own
    for other in ("과목없음", "나 과목 중간고사"):
        assert other not in sent, other  # no course, or another course
    # the Google path is unchanged: entries without a course still go along
    assert "개인 일정 과목없음(합성)" in before and "학교 행사 과목없음(합성)" in before
    assert "나 과목 중간고사" not in before


def test_NOTICE_CODEX_008_a_notice_whose_quote_is_not_in_the_transcript_is_dropped(library, tmp_path):
    ai = cn.FakeText(closure(text="이 문장은 전사에 없습니다 합성"))
    result = cn.scan(library, tmp_path, TODAY, client=ai, consent=Grants(("codex_notices", COURSE_A)),
                     engine_name="codex")
    assert result["read"] == 1 and result["added"] == [] and cal.candidates(library) == []
    state = json.loads((tmp_path / "_lecturehub" / "notices" / "state.json").read_text(encoding="utf-8"))
    assert state["la"]["dropped_without_quote"] == 1 and state["la"]["model"] == "fake"


def test_NOTICE_CODEX_009_a_kept_notice_is_a_candidate_never_confirmed(library, tmp_path):
    ai = cn.FakeText(closure())
    ai.model = "codex_cli"
    result = cn.scan(library, tmp_path, TODAY, client=ai, consent=Grants(("codex_notices", COURSE_A)),
                     engine_name="codex")
    assert len(result["added"]) == 1 and result["added"][0]["model"] == "codex_cli"
    candidate = cal.candidates(library)[0]
    assert candidate["status"] == "candidate" and candidate["evidence"]["quote"] == "그 주 금요일도 제가 학회가 있어서"


# ---------------------------------------------------------------- recording, missing Codex, cancellation
def test_NOTICE_CODEX_010_nothing_is_started_while_recording(library, tmp_path):
    ai = cn.FakeText(closure())
    consent = Grants(("codex_notices", COURSE_A))
    held = cn.scan(library, tmp_path, TODAY, client=ai, consent=consent, engine_name="codex", busy=lambda: True)
    assert held["recording"] is True and held["read"] == 0 and ai.prompts == []
    assert cn.usage(tmp_path, TODAY)["readings"] == 0
    assert not (tmp_path / "_lecturehub" / "notices" / "state.json").exists()
    assert cn.status(tmp_path)["recording"] is True
    later = cn.scan(library, tmp_path, TODAY, client=ai, consent=consent, engine_name="codex", busy=lambda: False)
    assert later["read"] == 1 and len(ai.prompts) == 1 and cn.status(tmp_path)["recording"] is False


def test_NOTICE_CODEX_011_the_google_path_still_reads_while_recording(library, tmp_path):
    """P-035 ②: the Gemini reading is network only and was allowed while recording; that is unchanged."""
    ai = cn.FakeText({"notices": []})
    result = cn.scan(library, tmp_path, TODAY, client=ai, consent=Grants(("gemini_transcribe", COURSE_A)),
                     engine_name="google", busy=lambda: True)
    assert result["read"] == 1 and "recording" not in result


def test_NOTICE_CODEX_012_codex_missing_sends_nothing_and_says_so(library, tmp_path, monkeypatch):
    monkeypatch.setattr("lecturehub.ai.codex.CodexEngine.executable", lambda self: None)
    monkeypatch.setattr(cn, "GeminiText", lambda *a, **k: pytest.fail("no fallback to Google"))
    monkeypatch.setattr(cn, "CodexText", lambda *a, **k: pytest.fail("nothing to run"))
    result = cn.scan(library, tmp_path, TODAY, consent=Grants(("codex_notices", COURSE_A),
                                                              ("gemini_transcribe", COURSE_A)), engine_name="codex")
    assert result["read"] == 0 and "Codex(ChatGPT)가 이 PC에 없습니다" in result["unavailable"]
    assert cn.usage(tmp_path, TODAY)["readings"] == 0
    assert "codex login" in cn.status(tmp_path)["unavailable"]
    from lecturehub.ui.library import Library

    monkeypatch.setattr(cn, "default_engine", lambda: "codex")
    setting = Library(tmp_path).notices_setting(TODAY)
    assert setting["unavailable"] == cn.status(tmp_path)["unavailable"] and setting["codex_found"] is False


def test_NOTICE_CODEX_013_codex_logged_out_sends_nothing(library, tmp_path, monkeypatch):
    from lecturehub.core import tools

    monkeypatch.setattr("lecturehub.ai.codex.CodexEngine.executable", lambda self: "codex.cmd")
    monkeypatch.setattr(tools, "codex_signed_in", lambda exe, timeout=8.0: False)
    monkeypatch.setattr(cn, "CodexText", lambda *a, **k: pytest.fail("nothing to run"))
    result = cn.scan(library, tmp_path, TODAY, consent=Grants(("codex_notices", COURSE_A)), engine_name="codex")
    assert result["read"] == 0 and "로그인되어 있지 않습니다" in result["unavailable"]
    assert cn.usage(tmp_path, TODAY)["readings"] == 0


def test_NOTICE_CODEX_014_without_consent_the_login_is_not_even_asked(library, tmp_path, monkeypatch):
    from lecturehub.core import tools

    monkeypatch.setattr("lecturehub.ai.codex.CodexEngine.executable", lambda self: "codex.cmd")
    monkeypatch.setattr(tools, "codex_signed_in", lambda exe, timeout=8.0: pytest.fail("no process for nothing"))
    result = cn.scan(library, tmp_path, TODAY, consent=Grants(), engine_name="codex")
    assert result["needs_consent"] == [COURSE_A, COURSE_B] and "unavailable" not in result
    assert cn.status(tmp_path)["needs_consent"] == [COURSE_A, COURSE_B]


class FakeEngine:
    def __init__(self, behaviour):
        self.behaviour, self.calls = behaviour, []

    def run_json(self, prompt, schema, *, timeout=900, stop=None):
        self.calls.append({"prompt": prompt, "schema": schema, "timeout": timeout, "stop": stop})
        return self.behaviour(stop) if callable(self.behaviour) else self.behaviour


def test_NOTICE_CODEX_015_codextext_passes_a_json_schema_and_counts_requests():
    from lecturehub.ai.codex import strict_schema

    engine = FakeEngine({"notices": [], "model": "codex_cli", "usage": {"input_tokens": 1}, "cost_usd": None})
    client = cn.CodexText(engine)
    assert client.generate_json("합성 프롬프트", cn.SCHEMA) == {"notices": []} and client.posts == 1
    sent = engine.calls[0]
    assert sent["timeout"] == cn.CODEX_TIMEOUT_S and sent["stop"] is client.halt
    schema = sent["schema"]
    assert schema["type"] == "object" and "OBJECT" not in json.dumps(schema) and "nullable" not in json.dumps(schema)
    date_field = schema["properties"]["notices"]["items"]["properties"]["date"]
    assert date_field["type"] == ["string", "null"]
    strict = strict_schema(schema)  # what CodexEngine.run_json writes for --output-schema
    assert strict["additionalProperties"] is False and strict["properties"]["notices"]["items"]["additionalProperties"] is False


def test_NOTICE_CODEX_016_a_missing_cli_counts_nothing_and_a_cancel_is_stopped():
    def missing(stop):
        raise FileNotFoundError("synthetic")

    client = cn.CodexText(FakeEngine(missing))
    with pytest.raises(cn.NoticeError) as error:
        client.generate_json("합성", cn.SCHEMA)
    assert client.posts == 0 and not isinstance(error.value, cn.Stopped)
    stop = threading.Event()

    def cancelled(halt):
        stop.set()  # the app closes while the CLI runs; ai.process.execute raises this
        raise RuntimeError("AI 작업을 취소했습니다.")

    client = cn.CodexText(FakeEngine(cancelled), halt=cn.Halt(stop))
    with pytest.raises(cn.Stopped):
        client.generate_json("합성", cn.SCHEMA)
    assert client.posts == 1  # the prompt was on the CLI's stdin: counted

    def failed(halt):
        raise RuntimeError("AI 명령이 실패했습니다. 로그인·사용 한도와 CLI 버전을 확인하세요.")

    client = cn.CodexText(FakeEngine(failed))
    with pytest.raises(cn.NoticeError) as error:
        client.generate_json("합성", cn.SCHEMA)
    assert not isinstance(error.value, cn.Stopped) and "로그인" in str(error.value)


def test_NOTICE_CODEX_017_a_cancelled_request_does_not_wait_and_is_read_next_round(library, tmp_path):
    stop = threading.Event()

    def cancelled(halt):
        stop.set()
        raise RuntimeError("AI 작업을 취소했습니다.")

    consent = Grants(("codex_notices", COURSE_A))
    first = cn.scan(library, tmp_path, TODAY, client=cn.CodexText(FakeEngine(cancelled), halt=cn.Halt(stop)),
                    consent=consent, engine_name="codex")
    assert first["stopped"] and first["read"] == 0 and first["errors"] == [] and first["waiting"] == []
    assert cn.usage(tmp_path, TODAY)["by_engine"] == {"codex": 1}  # its text may have gone out
    state = json.loads((tmp_path / "_lecturehub" / "notices" / "state.json").read_text(encoding="utf-8"))
    assert "la" not in state  # no failure on record: no wait before the next try
    engine = FakeEngine({"notices": []})
    second = cn.scan(library, tmp_path, TODAY, client=cn.CodexText(engine), consent=consent, engine_name="codex")
    assert second["read"] == 1 and len(engine.calls) == 1


def test_NOTICE_CODEX_018_a_failed_codex_request_waits_like_a_failed_google_one(library, tmp_path):
    def failed(halt):
        raise RuntimeError("AI 명령이 실패했습니다.")

    consent = Grants(("codex_notices", COURSE_A))
    clock = [1_792_000_000_000]
    result = cn.scan(library, tmp_path, TODAY, client=cn.CodexText(FakeEngine(failed)), consent=consent,
                     engine_name="codex", now_ms=lambda: clock[0])
    assert result["errors"] and cn.usage(tmp_path, TODAY)["readings"] == 1
    engine = FakeEngine({"notices": []})
    again = cn.scan(library, tmp_path, TODAY, client=cn.CodexText(engine), consent=consent, engine_name="codex",
                    now_ms=lambda: clock[0] + 60_000)
    assert again["waiting"] and engine.calls == []  # RETRY_AFTER_MS applies to Codex too


def test_NOTICE_CODEX_019_halt_asks_stop_every_time_and_busy_at_most_every_few_seconds(monkeypatch):
    asked, now = [], [100.0]
    monkeypatch.setattr(cn.time, "monotonic", lambda: now[0])
    stop = threading.Event()
    halt = cn.Halt(stop, lambda: asked.append(1) or len(asked) > 1, every=5.0)
    assert halt.is_set() is False and halt.is_set() is False and len(asked) == 1
    now[0] += 5.0
    assert halt.is_set() is True and len(asked) == 2  # a recording started
    assert cn.Halt(stop).is_set() is False
    stop.set()
    assert cn.Halt(stop).is_set() is True
    broken = cn.Halt(None, lambda: (_ for _ in ()).throw(OSError("synthetic")))
    assert broken.is_set() is True  # the recorder's state cannot be read: stop to be safe


# ---------------------------------------------------------------- the app's library layer
def _library_root(tmp_path):
    from lecturehub.core.paths import database_path

    database_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    with closing(db.connect(database_path(tmp_path))) as connection:
        _make(connection)


def test_NOTICE_CODEX_020_a_recording_session_lock_holds_the_codex_reading(tmp_path, monkeypatch):
    from lecturehub.ui.library import Library

    _library_root(tmp_path)
    ai = cn.FakeText(closure())
    monkeypatch.setattr(cn, "default_client", lambda *a, **k: ai)
    monkeypatch.setattr(cn, "codex_missing", lambda **k: None)
    monkeypatch.setattr("lecturehub.transcription.consent.ConsentStore",
                        lambda path: Grants(("codex_notices", COURSE_A)))
    cn.set_engine("codex")
    session = tmp_path / "_lecturehub" / "recordings" / "합성 세션 (1)"
    session.mkdir(parents=True)
    (session / ".recording.lock").write_text("", encoding="utf-8")
    held = Library(tmp_path).notices_scan(TODAY)
    assert held["ok"] and held["recording"] is True and ai.prompts == []
    window = Library(tmp_path).notices_scan(TODAY, busy=lambda: True)  # the window says a recording runs
    assert window["recording"] is True and ai.prompts == []
    (session / "stop.json").write_text("{}", encoding="utf-8")
    done = Library(tmp_path).notices_scan(TODAY, busy=lambda: False)
    assert done["read"] == 1 and len(ai.prompts) == 1


def test_NOTICE_CODEX_021_calendar_refresh_uses_the_word_rules_when_codex_is_missing(tmp_path, monkeypatch):
    from lecturehub.index import etl_sync
    from lecturehub.ui.library import Library

    _library_root(tmp_path)
    monkeypatch.setattr(etl_sync, "sync", lambda root, **_: {"state": "not_connected"})
    monkeypatch.setattr("lecturehub.ai.codex.CodexEngine.executable", lambda self: None)
    cn.set_engine("codex")
    calls, real = [], cal.scan_transcripts
    monkeypatch.setattr(cal, "scan_transcripts", lambda connection, *a, **k: calls.append(1) or real(connection, *a, **k))
    result = Library(tmp_path).calendar_refresh()
    assert result["ok"] and result["notices"]["unavailable"] and calls == [1]  # local, sends nothing


def test_NOTICE_CODEX_022_notices_wait_returns_when_no_reading_runs():
    from lecturehub.ui import library

    assert library.Library.notices_wait(0.1) is True
    library._NOTICE_LOCK.acquire()
    try:
        assert library.Library.notices_wait(0.05) is False
    finally:
        library._NOTICE_LOCK.release()


# ---------------------------------------------------------------- the window: consent only through the dialog
class _Window:
    def __init__(self, answer):
        self.answer, self.asked = answer, []

    def create_confirmation_dialog(self, title, message):
        self.asked.append((title, message))
        return self.answer


class _Backend:
    def __init__(self):
        self.allowed = []

    def notices_allow(self, course):
        self.allowed.append(course)
        return {"ok": True}


def test_NOTICE_CODEX_SEC_allow_needs_the_native_dialogs_token(tmp_path):
    from lecturehub.ui.app import CONSENT_TITLES, Api

    backend = _Backend()
    api = Api(backend, SimpleNamespace(root=tmp_path))
    assert api.notices_allow(COURSE_A)["needs_confirm"] and backend.allowed == []  # straight from page JS
    assert api.notices_allow(COURSE_A, "made-up")["ok"] is False and backend.allowed == []
    api._main = _Window(True)
    for other in ("codex_note", "gemini_transcribe", "ai_app_link"):  # another provider's token
        token = api.consent_confirm(other, COURSE_A)["token"]
        assert api.notices_allow(COURSE_A, token)["ok"] is False and backend.allowed == []
    token = api.consent_confirm("codex_notices", COURSE_B)["token"]
    assert api.notices_allow(COURSE_A, token)["ok"] is False  # another course's token
    q = api.consent_confirm("codex_notices", COURSE_A)
    title, message = api._main.asked[-1]
    assert CONSENT_TITLES["codex_notices"] in title and "Codex(OpenAI)" in message
    assert "소리는 보내지 않습니다" in message and "과목 없는 일정" in message and "ChatGPT Edu" in message
    assert api.notices_allow(COURSE_A, q["token"]) == {"ok": True} and backend.allowed == [COURSE_A]
    assert api.notices_allow(COURSE_A, q["token"])["ok"] is False  # one time only
    api._main = _Window(False)
    assert api.consent_confirm("codex_notices", COURSE_A) == {"ok": True, "yes": False}


def test_NOTICE_CODEX_SEC_backend_lists_allows_and_revokes(tmp_path):
    from lecturehub.ui.backend import Backend

    fake = SimpleNamespace(root=tmp_path, needs_setup=False,
                           courses=[SimpleNamespace(folder=COURSE_A), SimpleNamespace(folder=COURSE_B)],
                           extras=lambda: ["미분류"])
    for name in ("_ai_link_courses", "notices_courses", "notices_allow", "consent_list", "consent_revoke"):
        setattr(fake, name, getattr(Backend, name).__get__(fake))
    fake.CONSENT_LABELS = Backend.CONSENT_LABELS
    from lecturehub.ui.backend import NOTICES_PROVIDER

    assert NOTICES_PROVIDER == cn.CODEX_PROVIDER  # the window layer keeps its own copy (no index import, D-050)
    assert fake.notices_courses() == [{"course": COURSE_A, "allowed": False}, {"course": COURSE_B, "allowed": False}]
    assert fake.notices_allow("미분류")["ok"] is False and fake.notices_allow("없는 과목")["ok"] is False
    assert fake.notices_allow(COURSE_B)["courses"][1] == {"course": COURSE_B, "allowed": True}
    store = ConsentStore(tmp_path / "_lecturehub" / "consent.json")
    assert store.granted("codex_notices", COURSE_B) and not store.granted("codex_note", COURSE_B)
    labels = {r["provider"]: r["label"] for r in fake.consent_list()["rows"]}
    assert labels["codex_notices"] == "OpenAI Codex · 수업 중 공지 찾기"
    assert fake.consent_revoke("codex_notices", COURSE_B)["ok"] and not fake.notices_courses()[1]["allowed"]


def test_NOTICE_CODEX_APP_closing_cancels_and_waits(tmp_path):
    from lecturehub.ui.app import Api

    waited = []
    lib = SimpleNamespace(root=tmp_path, notices_wait=lambda timeout: waited.append(timeout) or True)
    backend = SimpleNamespace(recording=False, stop_live_helpers=lambda: None)
    api = Api(backend, lib)
    assert not api._notice_stop.is_set()
    from lecturehub.core import startup

    startup.set_close_choice(False)  # D-084: the user chose that closing the window quits the app
    assert api._on_main_closing() is True and api._notice_stop.is_set()
    assert api._end_background_ai(3.0) is True and waited == [3.0]


def test_NOTICE_CODEX_APP_round_passes_the_recording_check_and_the_stop(tmp_path, monkeypatch):
    from lecturehub.ui.app import Api

    seen = {}

    class Lib:
        root = tmp_path

        def notices_scan(self, today, **kwargs):
            seen.update(kwargs)
            api._quitting = True
            return {"ok": True, "added": []}

        def etl_board_auto(self):
            return []

    api = Api(SimpleNamespace(state=lambda: {"phase": "idle"}), Lib())
    monkeypatch.setattr(api, "_material_text_kick", lambda: "off")
    api._notice_round()  # D-079: the minute scheduler runs this scan in its own worker
    assert seen["stop"] is api._notice_stop and seen["busy"] == api._busy


# ---------------------------------------------------------------- the settings card (node)
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
WEB = Path(__file__).resolve().parents[1] / "src" / "lecturehub" / "ui" / "web"


@needs_node
def test_NOTICE_CODEX_UI_card_shows_engine_courses_and_what_is_missing(tmp_path):
    src = (WEB / "app.js").read_text(encoding="utf-8")
    start = src.index("  function noticesSection(N) {")
    helpers = src[src.index("  const SETTINGS_GROUPS = ["):src.index("  /* Class notices (P-035")]  # fold, pickAll (2026-11-25)
    body = helpers + src[start:src.index("  /* AI app link (D-072)", start)]
    script = tmp_path / "card.js"
    script.write_text("""
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
const ic = (n) => `<svg data-i="${n}"></svg>`;
""" + body + """
const out = {
  codex: noticesSection({ ok: true, on: true, engine: "codex", readings: 2, characters: 3000, by_engine: { codex: 2 },
    codex_found: true, courses: [{ course: "합성 과목 가", allowed: true }, { course: "합성 과목 나", allowed: false }],
    needs_consent: ["합성 과목 나"] }),
  missing: noticesSection({ ok: true, on: true, engine: "codex", codex_found: false, courses: [] }),
  unavailable: noticesSection({ ok: true, on: true, engine: "codex", codex_found: true, unavailable: "합성: 로그인 필요", courses: [] }),
  held: noticesSection({ ok: true, on: true, engine: "codex", codex_found: true, recording_held: true, courses: [] }),
  google: noticesSection({ ok: true, on: false, engine: "google", readings: 1, characters: 1000 }),
};
console.log(JSON.stringify(out));
""", encoding="utf-8")
    done = subprocess.run(["node", str(script)], capture_output=True, text=True, encoding="utf-8", timeout=60, check=False)
    assert done.returncode == 0, done.stderr[-2000:]
    out = json.loads(done.stdout.strip().splitlines()[-1])
    codex = out["codex"]
    assert 'data-notices checked' in codex and '<option value="codex" selected>' in codex
    assert 'data-notices-course="합성 과목 가" checked' in codex and 'data-notices-course="합성 과목 나">' in codex
    assert "허용이 필요한 과목: 합성 과목 나" in codex and "ChatGPT 2" in codex
    assert "과목 없는 일정과 소리는 보내지 않고" in codex and "녹음 중에는 보내지 않습니다" in codex
    assert "다른 회사로 바꿔 보내지 않습니다" in codex
    assert "Codex(ChatGPT)가 이 PC에 없습니다" in out["missing"] and "합성: 로그인 필요" in out["unavailable"]
    assert "녹음 중이라 쉬고 있습니다" in out["held"]
    google = out["google"]
    assert '<option value="google" selected>' in google and "data-notices-course" not in google
    assert "과목 없는 것 포함" in google and "Gemini로 보냅니다" in google and "data-notices checked" not in google
