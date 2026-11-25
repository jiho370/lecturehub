"""D-083 (MCP-MORE): more things an AI app can operate — the timetable, schedule changes, reminder settings and a
request to read a lecture's class notices again. Each is a D-080 kind (asked first unless automatic), behind the
write switch, the course gate, the caps, the change log and undo. Synthetic semester only (test_mcp_access); the
native dialog is replaced in-process (conftest / test_mcp_approve.answer), never shown.
"""
from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path

import pytest
from test_mcp_access import ALLOWED, HIDDEN, build_library
from test_mcp_approve import answer
from test_mcp_write import call, rows

from lecturehub.core import db, settings, timetable
from lecturehub.mcp import access, approve, changes, more_writes, writes

TABLE = ("폴더,과목,요일,시작,종료\n"
         f"{ALLOWED},{ALLOWED},월,09:00,10:15\n"
         f"{ALLOWED},{ALLOWED},수,09:00,10:15\n"
         f"{HIDDEN},{HIDDEN},화,13:00,14:15\n")
NEW = "합성과목 다"


@pytest.fixture
def library(tmp_path, monkeypatch):
    root = tmp_path / "학기 (합성) 26-2"
    build_library(root)
    (root / "시간표.csv").write_text(TABLE, encoding="utf-8-sig")
    monkeypatch.setenv("LECTUREHUB_ROOT", str(root))
    monkeypatch.setenv("LECTUREHUB_EDITION", "full")
    settings.set_ai_link(True)
    settings.set_ai_link_write(True)
    return root


def table(root) -> dict:
    return {c["folder"]: [f"{s['day']} {s['start']}~{s['end']}" for s in c["slots"]] for c in timetable.read(root)}


def schedule(root, course_id: str) -> list:
    return rows(root, "SELECT weekday, start, end FROM Schedule WHERE course_id = ? ORDER BY weekday, start", (course_id,))


MERGE = {"mode": "merge", "classes": [{"course": ALLOWED, "days": ["월"], "start": "10:00", "end": "11:15"},
                                      {"course": NEW, "days": ["금"], "start": "10:00", "end": "11:15", "room": "합성관 101"}]}


# ------------------------------------------------------------------ set_timetable
def test_MCP_MORE_001_set_timetable_merge_asks_shows_the_diff_and_writes_through_core_timetable(library, monkeypatch):
    fake = answer(monkeypatch, approve.ONCE)
    before = (library / "시간표.csv").read_bytes()
    payload, is_error, _ = call("set_timetable", MERGE)
    assert not is_error, payload
    body = fake.shown[0]["body"]
    assert fake.shown[0]["instruction"] == "시간표 바꾸기 — 허용할까요?"
    assert f"시간 바꿈: {ALLOWED}: 월 09:00~10:15, 수 09:00~10:15 → 월 10:00~11:15" in body.splitlines()
    assert f"넣음: {NEW}: 금 10:00~11:15 — 새 과목(폴더를 새로 만듦)" in body.splitlines()
    assert "강의실: 시간표 파일에 칸이 없어 저장하지 않음" in body and HIDDEN not in body
    assert table(library) == {ALLOWED: ["월 10:00~11:15"], HIDDEN: ["화 13:00~14:15"], NEW: ["금 10:00~11:15"]}
    assert (library / NEW).is_dir()  # timetable.write adds the folder; nothing is renamed or removed
    backups = list((library / "_lecturehub" / "backups").glob("시간표_*.csv"))
    assert backups and backups[0].read_bytes() == before
    assert schedule(library, "ca") == [(0, 36_000_000, 40_500_000)]  # the library's weekly slots follow (sync_schedule)
    assert payload["new_courses"] == [NEW] and "보여 줄 과목" in payload["message"]
    assert more_writes.ROOM_NOTE in payload["message"]
    # a brand-new course is NOT allowed for the AI app: consent stays the user's
    assert not access.course_allowed(library, NEW)
    assert call("list_courses")[0]["count"] == 1
    assert changes.recent(library)[0]["tool"] == "set_timetable" and changes.recent(library)[0]["undoable"]
    assert changes.signature(library) is not None  # the running app is told (marker)


def test_MCP_MORE_002_replace_keeps_courses_the_ai_app_cannot_see_and_a_new_course_stays_out_of_reach(library, monkeypatch):
    settings.set_ai_link_auto("set_timetable", True)
    fake = answer(monkeypatch, approve.ONCE, approve.ONCE)  # replace is asked about even when the kind is automatic (review H1)
    payload, is_error, _ = call("set_timetable", {"mode": "replace", "classes": [
        {"course": ALLOWED, "days": ["화", "목"], "start": "15:30", "end": "16:45"},
        {"course": NEW, "days": ["금"], "start": "09:00", "end": "10:15"}]})
    assert not is_error, payload
    assert table(library) == {ALLOWED: ["화 15:30~16:45", "목 15:30~16:45"], HIDDEN: ["화 13:00~14:15"],
                              NEW: ["금 09:00~10:15"]}
    # the new course is not allowed: a later replace leaves it alone, naming it answers like a hidden course
    payload, is_error, _ = call("set_timetable", {"mode": "replace", "classes": [
        {"course": ALLOWED, "days": ["월"], "start": "09:00", "end": "10:15"}]})
    assert not is_error and NEW in table(library) and len(fake.shown) == 2, payload
    for name in (NEW, HIDDEN, "미분류"):
        refused, is_error, _ = call("set_timetable", {"mode": "merge", "classes": [
            {"course": name, "days": ["토"], "start": "09:00", "end": "10:00"}]})
        assert is_error and refused == {"error": more_writes.HIDDEN}, (name, refused)
    gone, is_error, _ = call("set_timetable", {"mode": "merge", "classes": [], "remove_courses": [HIDDEN]})
    assert is_error and gone == {"error": access.NOT_FOUND}
    missing, is_error, _ = call("set_timetable", {"mode": "merge", "classes": [], "remove_courses": ["없는 과목"]})
    assert is_error and missing == gone  # not allowed answers exactly like not there


def test_MCP_MORE_003_overlap_with_a_hidden_course_does_not_name_it(library, monkeypatch):
    settings.set_ai_link_auto("set_timetable", True)
    refused, is_error, _ = call("set_timetable", {"mode": "merge", "classes": [
        {"course": ALLOWED, "days": ["화"], "start": "13:30", "end": "14:00"}]})
    assert is_error and refused == {"error": more_writes.HIDDEN} and HIDDEN not in json.dumps(refused, ensure_ascii=False)
    bad, is_error, _ = call("set_timetable", {"mode": "merge", "classes": [
        {"course": ALLOWED, "days": ["월"], "start": "11:00", "end": "10:00"}]})
    assert is_error and "start가 end보다 앞" in bad["error"]
    bad, is_error, _ = call("set_timetable", {"mode": "merge", "classes": [{"course": ALLOWED, "days": ["X"],
                                                                             "start": "09:00", "end": "10:00"}]})
    assert is_error and "월·화" in bad["error"]
    assert table(library)[ALLOWED] == ["월 09:00~10:15", "수 09:00~10:15"]


@pytest.mark.parametrize("given,message", [(approve.REJECT, approve.REJECTED), (approve.TIMEOUT, approve.NO_ANSWER),
                                           (approve.FAILED, approve.NO_DIALOG)], ids=["reject", "timeout", "failed"])
def test_MCP_MORE_004_reject_or_timeout_changes_nothing(library, monkeypatch, given, message):
    answer(monkeypatch, given)
    before = (library / "시간표.csv").read_bytes()
    payload, is_error, _ = call("set_timetable", MERGE)
    assert is_error and payload == {"error": message}
    assert (library / "시간표.csv").read_bytes() == before and not (library / NEW).exists()
    assert not (library / "_lecturehub" / "backups").exists() and changes.recent(library) == []
    assert schedule(library, "ca") == []


def test_MCP_MORE_005_the_timetable_changed_while_the_dialog_was_open(library, monkeypatch):
    def user_edits():
        (library / "시간표.csv").write_text(TABLE + f"{ALLOWED},{ALLOWED},금,15:00,16:00\n", encoding="utf-8-sig")

    answer(monkeypatch, approve.ONCE, during=user_edits)
    payload, is_error, _ = call("set_timetable", MERGE)
    assert is_error and payload == {"error": approve.CHANGED}
    assert "금 15:00~16:00" in table(library)[ALLOWED] and not (library / NEW).exists()


def test_MCP_MORE_006_nothing_to_change_needs_no_dialog(library, monkeypatch):
    monkeypatch.setattr(approve, "dialog", lambda *a: pytest.fail("asked about a change that changes nothing"))
    same, is_error, _ = call("set_timetable", {"mode": "merge", "classes": [
        {"course": ALLOWED, "days": ["월", "수"], "start": "09:00", "end": "10:15"}]})
    assert not is_error and "그대로" in same["message"] and changes.recent(library) == []


def test_MCP_MORE_007_refused_while_recording_and_the_recording_is_not_touched(library, monkeypatch):
    from lecturehub.recorder.heartbeat import SessionLock

    session = library / "_lecturehub" / "recordings" / "20261125_090000"
    session.mkdir(parents=True)
    monkeypatch.setattr(approve, "dialog", lambda *a: pytest.fail("asked while recording"))
    with SessionLock(session):
        payload, is_error, _ = call("set_timetable", MERGE)
    assert is_error and payload == {"error": more_writes.RECORDING}
    assert table(library)[ALLOWED] == ["월 09:00~10:15", "수 09:00~10:15"]

    def recording_starts():  # a recording that starts while the dialog is open: checked again under the lock
        monkeypatch.setattr(more_writes, "recording_now", lambda root: True)

    answer(monkeypatch, approve.ONCE, during=recording_starts)
    payload, is_error, _ = call("set_timetable", MERGE)
    assert is_error and payload == {"error": more_writes.RECORDING} and not (library / NEW).exists()


def test_MCP_MORE_008_undo_restores_the_previous_timetable_and_the_weekly_slots(library, monkeypatch):
    settings.set_ai_link_auto("set_timetable", True)
    before = (library / "시간표.csv").read_bytes()
    payload, is_error, _ = call("set_timetable", {**MERGE, "term_start": "2026-09-01", "term_end": "2026-12-20"})
    assert not is_error and timetable.load_term(library) == {"start": "2026-09-01", "end": "2026-12-20"}
    result = changes.undo(library, payload["change_id"])
    assert result["ok"] and "되돌렸습니다" in result["message"]
    assert (library / "시간표.csv").read_bytes() == before and timetable.load_term(library) == {}
    assert schedule(library, "ca") == [(0, 32_400_000, 36_900_000), (2, 32_400_000, 36_900_000)]
    assert (library / NEW).is_dir()  # a folder is never removed
    # changed since by the user: left alone
    # (the new course has a library row now and is not allowed: only the allowed one can be changed again)
    again, is_error, _ = call("set_timetable", {"mode": "merge", "classes": MERGE["classes"][:1]})
    assert not is_error, again
    (library / "시간표.csv").write_text(TABLE, encoding="utf-8-sig")
    with pytest.raises(changes.WriteRefused, match="그 뒤에 바뀐"):
        changes.undo(library, again["change_id"])


def test_MCP_MORE_009_caps_and_the_write_switch(library, monkeypatch):
    settings.set_ai_link_auto("set_timetable", True)
    monkeypatch.setattr(writes, "DAILY", 0)
    payload, is_error, _ = call("set_timetable", MERGE)
    assert is_error and payload == {"error": writes.TOO_MANY_TODAY}
    monkeypatch.setattr(writes, "DAILY", 200)
    settings.set_ai_link_write(False)
    payload, is_error, _ = call("set_timetable", MERGE)
    assert is_error and payload == {"error": access.WRITE_OFF}
    assert table(library)[ALLOWED] == ["월 09:00~10:15", "수 09:00~10:15"]


def test_MCP_MORE_010_a_failed_library_sync_puts_the_file_back(library, monkeypatch):
    from lecturehub.index import ingest

    settings.set_ai_link_auto("set_timetable", True)
    before = (library / "시간표.csv").read_bytes()

    def broken(root, connection):
        raise RuntimeError("합성 실패")

    monkeypatch.setattr(ingest, "sync_schedule", broken)
    payload, is_error, _ = call("set_timetable", MERGE)
    assert is_error and "아무것도 바꾸지 않았습니다" in payload["error"]
    assert (library / "시간표.csv").read_bytes() == before and not (library / NEW).exists()
    assert changes.recent(library) == []


def test_MCP_MORE_011_the_running_app_reloads_its_courses_after_an_ai_change(library, monkeypatch):
    from types import SimpleNamespace

    from lecturehub.ui.backend import Backend

    fake = SimpleNamespace(root=library, needs_setup=False, timetable_path=library / "시간표.csv", courses=[],
                           _lock=__import__("threading").Lock(), _state={"library_version": 0})
    for name in ("_ai_changes_check", "_timetable_check", "_bump_library"):
        setattr(fake, name, getattr(Backend, name).__get__(fake))
    fake._ai_changes_check()  # first look: remembers the marker
    settings.set_ai_link_auto("set_timetable", True)
    assert not call("set_timetable", MERGE)[1]
    fake._ai_seen_at = 0.0
    fake._ai_changes_check()
    assert fake._state["library_version"] == 1 and fake._state["timetable_version"] == 1
    assert NEW in [c.folder for c in fake.courses]


# ------------------------------------------------------------------ [사진으로 시간표 넣기 (ChatGPT·Codex)]
PNG = b"\x89PNG\r\n\x1a\n synthetic"
class _PhotoWindow:
    def __init__(self, picked, answer=True):
        self.picked, self.answer, self.asked, self.pick_calls = picked, answer, [], 0

    def create_file_dialog(self, *args, **kwargs):
        self.pick_calls += 1
        return [str(self.picked)] if self.picked else None

    def create_confirmation_dialog(self, title, message):
        self.asked.append((title, message))
        return self.answer


def _photo_api(root, monkeypatch, picked, answer=True):
    import threading
    from types import SimpleNamespace

    from lecturehub.ai.codex import CodexEngine
    from lecturehub.ui.app import Api

    monkeypatch.setattr(CodexEngine, "available", lambda self: True)
    monkeypatch.setattr(CodexEngine, "executable", lambda self: "C:/합성 도구/codex.cmd")
    fake = SimpleNamespace(root=root, needs_setup=False, courses=[], extras=list, _lock=threading.Lock(),
                           _state={"library_version": 0})
    api = Api(fake, None)
    api._main = _PhotoWindow(picked, answer)
    launched = []
    api._codex_launcher = lambda command, workspace: launched.append((command, workspace))
    return api, launched


def test_MCP_MORE_020_photo_button_needs_both_switches_and_the_native_dialogs_token(library, tmp_path, monkeypatch):
    from lecturehub.mcp import connect
    from lecturehub.ui.app import PHOTO_SWITCHES

    picture = tmp_path / "사진 (합성).png"
    picture.write_bytes(PNG)
    api, launched = _photo_api(library, monkeypatch, picture)
    settings.set_ai_link_write(False)
    assert api.timetable_photo_confirm() == {"ok": False, "error": PHOTO_SWITCHES} and api._main.pick_calls == 0
    settings.set_ai_link_write(True)
    assert api.timetable_photo_open("made-up")["needs_confirm"] and launched == []  # straight from page script
    q = api.timetable_photo_confirm()
    title, message = api._main.asked[-1]
    assert q["yes"] and q["name"] == picture.name and picture.name in message and "OpenAI" in message
    assert "Codex 로그인" in message
    done = api.timetable_photo_open(q["token"])
    assert done == {"ok": True} and len(launched) == 1
    command, workspace = launched[0]
    assert "--image=" in command and connect.PHOTO_PROMPT in command and "timetable_photo_" in command
    assert "-c" in command and "mcp_servers.lecturehub.command=" in command  # this run only, config.toml untouched
    copies = list(workspace.glob("timetable_photo_*.png"))
    assert len(copies) == 1 and copies[0].read_bytes() == picture.read_bytes()
    assert api.timetable_photo_open(q["token"])["needs_confirm"] and len(launched) == 1  # one time only
    # switched off between the dialog and the start: nothing opens
    q = api.timetable_photo_confirm()
    settings.set_ai_link(False)
    assert api.timetable_photo_open(q["token"]) == {"ok": False, "error": PHOTO_SWITCHES} and len(launched) == 1


def test_MCP_MORE_021_photo_button_refuses_other_files_big_files_and_a_no(library, tmp_path, monkeypatch):
    from lecturehub.mcp import connect

    text = tmp_path / "시간표.txt"
    text.write_text("월 9시", encoding="utf-8")
    api, launched = _photo_api(library, monkeypatch, text)
    assert "그림 파일" in api.timetable_photo_confirm()["error"] and api._main.asked == []
    big = tmp_path / "큰 사진.jpg"
    big.write_bytes(b"\xff\xd8\xff" + b"x" * 2048)
    monkeypatch.setattr(connect, "PHOTO_MAX_BYTES", 1024)
    api, launched = _photo_api(library, monkeypatch, big)
    assert "너무 큽니다" in api.timetable_photo_confirm()["error"]
    small = tmp_path / "사진.webp"
    small.write_bytes(b"RIFF\x10\x00\x00\x00WEBPVP8 synthetic")
    api, launched = _photo_api(library, monkeypatch, small, answer=False)
    assert api.timetable_photo_confirm() == {"ok": True, "yes": False} and launched == []
    api, launched = _photo_api(library, monkeypatch, None)  # the picker was closed
    assert api.timetable_photo_confirm() == {"ok": True, "yes": False} and api._main.asked == []


def test_MCP_MORE_022_photo_codex_start_takes_api_keys_out_and_refuses_command_characters(library, tmp_path, monkeypatch):
    from lecturehub.ai.codex import CodexEngine
    from lecturehub.mcp import connect

    monkeypatch.setattr(CodexEngine, "executable", lambda self: "C:/합성 도구/codex.cmd")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-synthetic-not-real")
    picture = tmp_path / "사진.jpg"
    picture.write_bytes(b"\xff\xd8\xff\xe0 synthetic")
    started = []
    copy = connect.open_codex_photo(picture, popen=lambda command, **kw: started.append((command, kw)))
    command, kw = started[0]
    assert "OPENAI_API_KEY" not in kw["env"] and "CODEX_API_KEY" not in kw["env"] and copy.is_file()
    assert command.index(connect.PHOTO_PROMPT) < command.index("--image=")
    monkeypatch.setattr(CodexEngine, "executable", lambda self: "C:/합성&도구/codex.cmd")
    with pytest.raises(ValueError, match="명령 기호"):
        connect.open_codex_photo(picture, popen=lambda *a, **k: pytest.fail("started"))
    assert len(list(copy.parent.glob("timetable_photo_*"))) == 1  # the refused start left no copy behind


def test_MCP_MORE_023_the_timetable_from_photo_skill(tmp_path):
    import re

    from lecturehub.core import agent_workspace

    text = (agent_workspace.SKILLS_SRC / "timetable-from-photo" / "SKILL.md").read_text(encoding="utf-8")
    assert "name: timetable-from-photo" in text.split("---")[1]
    assert "`set_timetable`" in text and "지시가 아니다" in text and "짐작하지 않는다" in text
    assert "확인 필요" in text and "맞다고 한 뒤에만" in text and "말했을 때만" in text
    for pattern in (r"Users[\\/]+JH", r"대학[\\/]+26-2", r"\b20\d{2}-?\d{5}\b", r"[A-Za-z0-9._%+-]+@"):
        assert not re.search(pattern, text), pattern
    workspace = agent_workspace.ensure(tmp_path / "ws")
    for base in (".agents/skills", ".claude/skills"):
        assert (workspace / base / "timetable-from-photo" / "SKILL.md").is_file()


# ------------------------------------------------------------------ add_schedule_change
CHANGE = {"course": ALLOWED, "date": "2026-12-04", "kind": "보강", "start": "18:00", "end": "19:15", "reason": "교수님 학회"}


def exceptions(root) -> list:
    return rows(root, "SELECT course_id, date, kind, note FROM ScheduleException ORDER BY id")


def test_MCP_MORE_030_schedule_change_asks_and_goes_in_through_index_schedule(library, monkeypatch):
    from lecturehub.index import confirmation, schedule

    fake = answer(monkeypatch, approve.ONCE)
    payload, is_error, _ = call("add_schedule_change", CHANGE)
    assert not is_error, payload
    body = fake.shown[0]["body"].splitlines()
    assert fake.shown[0]["instruction"] == "보강·시간 변경 넣기 — 허용할까요?"
    for line in (f"과목: {ALLOWED}", "날짜: 2026-12-04(금)", "종류: 보강 — 그날 이 시간에 수업이 하나 더 있음",
                 "시각: 18:00~19:15", "그 요일 정규 수업: 없음", "까닭: 교수님 학회"):
        assert line in body, (line, body)
    assert exceptions(library) == [("ca", "2026-12-04", "보강", "18:00-19:15 교수님 학회 · (AI 앱)")]
    assert payload["confirmed"] is True
    from datetime import date

    with closing(db.connect_readonly(library / "_lecturehub" / "lecturehub.sqlite")) as c:
        made = schedule.daily_schedule(c, date(2026, 12, 4))
        assert [(m.course, m.start_ms, m.end_ms, m.kind) for m in made] == [(ALLOWED, 64_800_000, 69_300_000, "보강")]
        eid = rows(library, "SELECT id FROM ScheduleException")[0][0]
        assert confirmation.state_of(c, "ScheduleException", eid) == confirmation.CONFIRMED
    # the same again: nothing to do, no dialog
    monkeypatch.setattr(approve, "dialog", lambda *a: pytest.fail("asked about nothing"))
    again, is_error, _ = call("add_schedule_change", CHANGE)
    assert not is_error and again["already"] and len(exceptions(library)) == 1


def test_MCP_MORE_031_schedule_change_automatic_is_marked_unconfirmed_and_undo_takes_it_back(library, monkeypatch):
    from lecturehub.index import confirmation

    settings.set_ai_link_auto("add_schedule_change", True)
    monkeypatch.setattr(approve, "dialog", lambda *a: pytest.fail("asked about an automatic kind"))
    payload, is_error, _ = call("add_schedule_change", {**CHANGE, "kind": "시간변경", "reason": ""})
    assert not is_error and payload["confirmed"] is False and "사용자 확인 전" in payload["message"]
    eid = rows(library, "SELECT id FROM ScheduleException")[0][0]
    with closing(db.connect_readonly(library / "_lecturehub" / "lecturehub.sqlite")) as c:
        assert confirmation.state_of(c, "ScheduleException", eid) == confirmation.INFERRED
    events = rows(library, "SELECT count(*) FROM Event")[0][0]
    assert changes.undo(library, payload["change_id"])["ok"]
    assert exceptions(library) == [] and rows(library, "SELECT count(*) FROM Event")[0][0] == events + 1
    with pytest.raises(changes.WriteRefused):
        changes.undo(library, payload["change_id"])


def test_MCP_MORE_032_schedule_change_gate_arguments_reject_and_target_changed(library, monkeypatch):
    for course in (HIDDEN, "미분류", "없는 과목"):
        payload, is_error, _ = call("add_schedule_change", {**CHANGE, "course": course})
        assert is_error and payload == {"error": access.NOT_FOUND}
    from test_mcp_write import _server

    reply = _server().handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": "add_schedule_change", "arguments": {**CHANGE, "kind": "휴강"}}})
    assert "보강" in reply["error"]["message"]  # closure goes through add_calendar_event's candidate path only
    bad, is_error, _ = call("add_schedule_change", {**CHANGE, "reason": "10:00-11:00 대신"})
    assert is_error and "시각 범위" in bad["error"]
    bad, is_error, _ = call("add_schedule_change", {**CHANGE, "start": "19:00", "end": "18:00"})
    assert is_error and "앞이어야" in bad["error"]
    answer(monkeypatch, approve.REJECT)
    payload, is_error, _ = call("add_schedule_change", CHANGE)
    assert is_error and payload == {"error": approve.REJECTED} and exceptions(library) == []

    def user_adds():
        from lecturehub.index import schedule
        from datetime import date

        with closing(db.connect(library / "_lecturehub" / "lecturehub.sqlite")) as c, c:
            schedule.add_exceptions(c, [date(2026, 12, 4)], course_id="ca", kind="대체", note="09:00-10:00 사용자")

    answer(monkeypatch, approve.ONCE, during=user_adds)
    payload, is_error, _ = call("add_schedule_change", CHANGE)
    assert is_error and payload == {"error": approve.CHANGED}
    assert [e[2] for e in exceptions(library)] == ["대체"]


# ------------------------------------------------------------------ set_reminder_settings
def prefs() -> dict:
    from lecturehub.core import reminder_settings

    return reminder_settings.preferences()


def test_MCP_MORE_040_reminders_dialog_shows_before_and_after_and_only_allowed_courses(library, monkeypatch):
    from lecturehub.core import reminder_settings

    reminder_settings.set_preferences({"muted_courses": [HIDDEN]})  # the user muted a course the AI app cannot see
    fake = answer(monkeypatch, approve.ONCE)
    payload, is_error, _ = call("set_reminder_settings", {"briefing": False, "deadlines": True, "mute_courses": [ALLOWED]})
    assert not is_error, payload
    assert fake.shown[0]["instruction"] == "알림 설정 바꾸기 — 허용할까요?"
    body = fake.shown[0]["body"].splitlines()
    assert "오늘 일정 아침 알림 (지금 → 바꾼 뒤): 켬 → 끔" in body, body
    assert f"과목 알림 (지금 → 바꾼 뒤): {ALLOWED}: 켬 → 끔" in body
    assert not any("마감" in line for line in body)  # deadlines was already on: not shown as a change
    assert HIDDEN not in fake.shown[0]["body"] and HIDDEN not in json.dumps(payload, ensure_ascii=False)
    assert prefs() == {"deadlines": True, "briefing": False, "muted_courses": sorted([ALLOWED, HIDDEN])}
    assert payload["muted_courses"] == [ALLOWED]
    # the same again: nothing to do, no dialog
    monkeypatch.setattr(approve, "dialog", lambda *a: pytest.fail("asked about nothing"))
    again, is_error, _ = call("set_reminder_settings", {"briefing": False, "mute_courses": [ALLOWED]})
    assert not is_error and "이미" in again["message"] and len(changes.recent(library)) == 1


@pytest.mark.parametrize("given,message", [(approve.REJECT, approve.REJECTED), (approve.TIMEOUT, approve.NO_ANSWER)],
                         ids=["reject", "timeout"])
def test_MCP_MORE_041_reminders_reject_or_timeout_changes_nothing(library, monkeypatch, given, message):
    answer(monkeypatch, given)
    payload, is_error, _ = call("set_reminder_settings", {"deadlines": False})
    assert is_error and payload == {"error": message}
    assert prefs()["deadlines"] is True and changes.recent(library) == []


def test_MCP_MORE_042_reminders_gate_arguments_and_the_setting_changed_during_the_dialog(library, monkeypatch):
    from lecturehub.core import reminder_settings

    for course in (HIDDEN, "없는 과목"):
        for key in ("mute_courses", "unmute_courses"):
            payload, is_error, _ = call("set_reminder_settings", {key: [course]})
            assert is_error and payload == {"error": access.NOT_FOUND}
    bad, is_error, _ = call("set_reminder_settings", {})
    assert is_error and "하나 이상" in bad["error"]
    bad, is_error, _ = call("set_reminder_settings", {"mute_courses": [ALLOWED], "unmute_courses": [ALLOWED]})
    assert is_error and "하나만" in bad["error"]
    answer(monkeypatch, approve.ONCE, during=lambda: reminder_settings.set_preferences({"deadlines": False}))
    payload, is_error, _ = call("set_reminder_settings", {"briefing": False})
    assert is_error and payload == {"error": approve.CHANGED}
    assert prefs() == {"deadlines": False, "briefing": True, "muted_courses": []}


def test_MCP_MORE_043_reminders_automatic_caps_switch_and_undo(library, monkeypatch):
    from lecturehub.core import reminder_settings

    settings.set_ai_link_auto("set_reminder_settings", True)
    monkeypatch.setattr(approve, "dialog", lambda *a: pytest.fail("asked about an automatic kind"))
    payload, is_error, _ = call("set_reminder_settings", {"deadlines": False, "mute_courses": [ALLOWED]})
    assert not is_error, payload
    assert prefs() == {"deadlines": False, "briefing": True, "muted_courses": [ALLOWED]}
    record = changes.records(library)[-1]
    assert record["tool"] == "set_reminder_settings" and record["undo"]["kind"] == "settings"
    result = changes.undo(library, payload["change_id"])
    assert result["ok"] and prefs() == {"deadlines": True, "briefing": True, "muted_courses": []}
    # a value the user changed afterwards is left alone; the others go back
    payload, _, _ = call("set_reminder_settings", {"deadlines": False, "briefing": False})
    reminder_settings.set_preferences({"briefing": True})
    result = changes.undo(library, payload["change_id"])
    assert "1개는" in result["message"] and prefs()["deadlines"] is True and prefs()["briefing"] is True
    payload, _, _ = call("set_reminder_settings", {"deadlines": False})
    reminder_settings.set_preferences({"deadlines": True})
    with pytest.raises(changes.WriteRefused, match="그 뒤에 바뀐"):
        changes.undo(library, payload["change_id"])
    monkeypatch.setattr(writes, "DAILY", 0)
    payload, is_error, _ = call("set_reminder_settings", {"deadlines": False})
    assert is_error and payload == {"error": writes.TOO_MANY_TODAY}
    monkeypatch.setattr(writes, "DAILY", 200)
    settings.set_ai_link_write(False)
    payload, is_error, _ = call("set_reminder_settings", {"deadlines": False})
    assert is_error and payload == {"error": access.WRITE_OFF} and prefs()["deadlines"] is True


def test_MCP_MORE_044_reminders_never_write_over_a_broken_settings_file(library, monkeypatch):
    settings.set_ai_link_auto("set_reminder_settings", True)
    monkeypatch.setattr(settings, "save_from_ai_strict", lambda key, value: False)
    payload, is_error, _ = call("set_reminder_settings", {"deadlines": False})
    assert is_error and payload == {"error": more_writes.SETTINGS_UNREADABLE} and changes.recent(library) == []


# ------------------------------------------------------------------ request_notice_rescan
def allow_codex(root, course=ALLOWED):
    from lecturehub.transcription.consent import ConsentStore

    ConsentStore(root / "_lecturehub" / "consent.json").grant("codex_notices", course, 5, "n")


def queued(root) -> list:
    from lecturehub.mcp import rescan_queue

    return [r["lecture_id"] for r in rescan_queue.pending(root)]


def test_MCP_MORE_050_rescan_asks_names_the_engine_and_leaves_one_request(library, monkeypatch):
    allow_codex(library)
    fake = answer(monkeypatch, approve.ONCE)
    payload, is_error, _ = call("request_notice_rescan", {"lecture_id": "a1"})
    assert not is_error, payload
    assert fake.shown[0]["instruction"] == "수업 중 공지 다시 찾기 — 허용할까요?"
    body = fake.shown[0]["body"]
    for part in (f"과목: {ALLOWED}", "회차: 2026-09-07", "보내는 곳: ChatGPT Edu (Codex)", "언제: Lecture Hub 앱이"):
        assert part in body, (part, body)
    assert payload["queued"] and "확인 전 후보" in payload["message"] and queued(library) == ["a1"]
    # the same again: already waiting, no dialog, still one request
    monkeypatch.setattr(approve, "dialog", lambda *a: pytest.fail("asked about a request already waiting"))
    again, is_error, _ = call("request_notice_rescan", {"lecture_id": "a1"})
    assert not is_error and again["already"] and queued(library) == ["a1"] and len(changes.recent(library)) == 1


def test_MCP_MORE_051_rescan_gate_consent_switch_empty_lecture_and_the_cap(library, monkeypatch):
    from lecturehub.index import class_notices
    from lecturehub.mcp import rescan_queue

    monkeypatch.setattr(approve, "dialog", lambda *a: pytest.fail("asked for a request that cannot be made"))
    for lecture in ("b1", "u1", "없는회차"):  # a course that is not allowed answers like one that does not exist
        payload, is_error, _ = call("request_notice_rescan", {"lecture_id": lecture})
        assert is_error and payload == {"error": access.NOT_FOUND}
    payload, is_error, _ = call("request_notice_rescan", {"lecture_id": "a1"})  # no codex_notices consent yet
    assert is_error and "설정 › AI › '수업 중 공지 찾기'" in payload["error"] and "허용" in payload["error"]
    class_notices.set_engine("google")
    payload, is_error, _ = call("request_notice_rescan", {"lecture_id": "a1"})
    assert is_error and "Google 전사 전송 동의" in payload["error"]
    class_notices.set_engine("codex")
    allow_codex(library)
    payload, is_error, _ = call("request_notice_rescan", {"lecture_id": "a2"})  # no transcript
    assert is_error and payload == {"error": more_writes.RESCAN_EMPTY}
    class_notices.set_enabled(False)
    payload, is_error, _ = call("request_notice_rescan", {"lecture_id": "a1"})
    assert is_error and payload == {"error": more_writes.RESCAN_OFF}
    class_notices.set_enabled(True)
    for n in range(rescan_queue.MAX_PENDING):
        rescan_queue.add(library, f"other{n}", f"c{n}", ALLOWED)
    payload, is_error, _ = call("request_notice_rescan", {"lecture_id": "a1"})
    assert is_error and "기다리고" in payload["error"] and "a1" not in queued(library)


def test_MCP_MORE_052_rescan_reject_automatic_caps_and_undo_while_waiting(library, monkeypatch):
    allow_codex(library)
    answer(monkeypatch, approve.REJECT)
    payload, is_error, _ = call("request_notice_rescan", {"lecture_id": "a1"})
    assert is_error and payload == {"error": approve.REJECTED} and queued(library) == []
    answer(monkeypatch, approve.ONCE, during=lambda: allow_codex(library, HIDDEN))  # not about this course: goes ahead
    settings.set_ai_link_auto("request_notice_rescan", True)
    monkeypatch.setattr(approve, "dialog", lambda *a: pytest.fail("asked about an automatic kind"))
    payload, is_error, _ = call("request_notice_rescan", {"lecture_id": "a1"})
    assert not is_error and queued(library) == ["a1"]
    assert changes.undo(library, payload["change_id"])["ok"] and queued(library) == []
    with pytest.raises(changes.WriteRefused):
        changes.undo(library, payload["change_id"])
    again, is_error, _ = call("request_notice_rescan", {"lecture_id": "a1"})  # once per lecture per day (review H2)
    assert is_error and again == {"error": more_writes.RESCAN_LECTURE_TODAY} and queued(library) == []
    monkeypatch.setattr(more_writes, "_rescans_today", lambda root: [])  # as if on another day
    payload, _, _ = call("request_notice_rescan", {"lecture_id": "a1"})
    from lecturehub.mcp import rescan_queue

    rescan_queue.remove(library, "a1")  # the app read it
    with pytest.raises(changes.WriteRefused, match="이미 읽었"):
        changes.undo(library, payload["change_id"])
    monkeypatch.setattr(writes, "DAILY", 0)
    payload, is_error, _ = call("request_notice_rescan", {"lecture_id": "a1"})
    assert is_error and payload == {"error": writes.TOO_MANY_TODAY}
    monkeypatch.setattr(writes, "DAILY", 200)
    settings.set_ai_link_write(False)
    payload, is_error, _ = call("request_notice_rescan", {"lecture_id": "a1"})
    assert is_error and payload == {"error": access.WRITE_OFF} and queued(library) == []


def test_MCP_MORE_053_rescan_consent_withdrawn_while_the_dialog_was_open(library, monkeypatch):
    from lecturehub.transcription.consent import ConsentStore

    allow_codex(library)
    answer(monkeypatch, approve.ONCE,
           during=lambda: ConsentStore(library / "_lecturehub" / "consent.json").revoke("codex_notices", ALLOWED, 6))
    payload, is_error, _ = call("request_notice_rescan", {"lecture_id": "a1"})
    assert is_error and queued(library) == []
    assert "ChatGPT Edu (Codex)(으)로 보내는 것이 허용되어 있지 않아" in payload["error"], payload
    assert ALLOWED in payload["error"] and "수업 중 공지 찾기" in payload["error"]


def test_MCP_MORE_054_the_app_reads_requests_with_its_own_engine_and_removes_them(library, monkeypatch):
    import threading
    from datetime import date
    from types import SimpleNamespace

    from lecturehub.mcp import rescan_queue
    from lecturehub.ui.app import Api
    from lecturehub.ui.library import Library

    import itertools
    import time

    ticks = itertools.count(int(time.time() * 1000))
    monkeypatch.setattr(rescan_queue, "_now_ms", lambda: next(ticks))  # oldest first: a1 was asked before a2
    rescan_queue.add(library, "a1", "c1", ALLOWED)
    rescan_queue.add(library, "a2", "c2", ALLOWED)
    calls, answers = [], [{"ok": True, "added": [{"course": ALLOWED, "summary": "합성 휴강"}]}, {"ok": True, "busy": True}]

    def scan(today, force=False, lecture_ids=None, *, busy=None, stop=None):
        calls.append((force, lecture_ids))
        return answers.pop(0)

    fake = SimpleNamespace(root=library, notices_scan=scan)
    result = Library.notices_requested(fake, date(2026, 11, 25), busy=lambda: False, stop=threading.Event())
    assert calls == [(True, ["a1"]), (True, ["a2"])]  # each through the app's reading, one lecture, switches checked
    assert result["read"] == 1 and result["added"][0]["summary"] == "합성 휴강"
    assert queued(library) == ["a2"]  # another reading was running: stays for the next minute
    assert rescan_queue.read_today(library) == ["a1"]  # counted toward the per-day limits (review H2)
    calls.clear()
    Library.notices_requested(fake, date(2026, 11, 25), busy=lambda: True, stop=threading.Event())
    assert calls == [] and queued(library) == ["a2"]  # nothing is read while recording
    # the window's minute loop: picks up a waiting request only when nothing records
    window = SimpleNamespace(_library=SimpleNamespace(root=library, notices_requested=None), _busy=lambda: False)
    assert Api._rescan_waiting(window) is True
    window._busy = lambda: True
    assert Api._rescan_waiting(window) is False
    rescan_queue.remove(library, "a2")
    window._busy = lambda: False
    assert Api._rescan_waiting(window) is False
    # a broken or very old file is dropped, never read
    (rescan_queue.folder(library) / "junk.json").write_text("{", encoding="utf-8")
    assert rescan_queue.pending(library) == []
    rescan_queue.drop_stale(library)
    assert list(rescan_queue.folder(library).glob("*.json")) == []


@pytest.mark.parametrize("off", ["notices", "link", "write", "course"])
def test_MCP_MORE_055_review_H2_a_request_is_dropped_when_a_switch_is_off_at_reading_time(library, monkeypatch, off):
    import threading
    from datetime import date
    from types import SimpleNamespace

    from lecturehub.index import class_notices
    from lecturehub.mcp import rescan_queue
    from lecturehub.transcription.consent import ConsentStore
    from lecturehub.ui.library import Library

    rescan_queue.add(library, "a1", "c1", ALLOWED)
    if off == "notices":
        class_notices.set_enabled(False)
    elif off == "link":
        settings.set_ai_link(False)
    elif off == "write":
        settings.set_ai_link_write(False)
    else:
        ConsentStore(library / "_lecturehub" / "consent.json").revoke(access.PROVIDER, ALLOWED, 9)
    fake = SimpleNamespace(root=library, notices_scan=lambda *a, **k: pytest.fail("read after the switch went off"))
    result = Library.notices_requested(fake, date(2026, 11, 25), busy=lambda: False, stop=threading.Event())
    assert result["dropped"] == ["a1"] and queued(library) == [] and rescan_queue.read_today(library) == []


def test_MCP_MORE_056_review_H2_one_per_lecture_and_ten_per_day(library, monkeypatch):
    import threading
    from datetime import date
    from types import SimpleNamespace

    from lecturehub.mcp import rescan_queue
    from lecturehub.ui.library import Library

    allow_codex(library)
    settings.set_ai_link_auto("request_notice_rescan", True)
    # request time: ten lectures asked today already
    monkeypatch.setattr(more_writes, "_rescans_today", lambda root: [f"other{n}" for n in range(rescan_queue.PER_DAY)])
    payload, is_error, _ = call("request_notice_rescan", {"lecture_id": "a1"})
    assert is_error and payload == {"error": more_writes.RESCAN_DAY_FULL.format(n=10)} and queued(library) == []
    monkeypatch.setattr(more_writes, "_rescans_today", lambda root: [])
    for n in range(rescan_queue.PER_DAY):
        rescan_queue.mark_read(library, f"other{n}")
    payload, is_error, _ = call("request_notice_rescan", {"lecture_id": "a1"})  # queued, then ...
    assert not is_error
    read = []
    fake = SimpleNamespace(root=library, notices_scan=lambda *a, **k: read.append(1) or {"ok": True})
    result = Library.notices_requested(fake, date(2026, 11, 25), busy=lambda: False, stop=threading.Event())
    assert read == [] and result["dropped"] == ["a1"]  # ... the app enforces the day's limit again
    (rescan_queue.folder(library) / rescan_queue.DONE).unlink()
    rescan_queue.mark_read(library, "a1")
    payload, is_error, _ = call("request_notice_rescan", {"lecture_id": "a1"})
    assert is_error and payload == {"error": more_writes.RESCAN_LECTURE_TODAY}
    rescan_queue.add(library, "a1", "c9", ALLOWED)
    result = Library.notices_requested(fake, date(2026, 11, 25), busy=lambda: False, stop=threading.Event())
    assert read == [] and result["dropped"] == ["a1"]  # read once today already


# ------------------------------------------------------------------ review of 2026-11-25 (timetable)
def test_MCP_MORE_060_review_H1_removals_are_never_folded_and_always_asked(library, monkeypatch):
    many = [{"course": f"합성새과목 {n:02d}", "days": ["토"], "start": f"{8 + n // 2:02d}:{30 * (n % 2):02d}",
             "end": f"{8 + n // 2:02d}:{30 * (n % 2) + 20:02d}"} for n in range(15)]
    settings.set_ai_link_auto("set_timetable", True)
    fake = answer(monkeypatch, approve.REJECT, approve.REJECT)
    payload, is_error, _ = call("set_timetable", {"mode": "replace", "classes": many})
    assert is_error and payload == {"error": approve.REJECTED}  # automatic, yet asked: it removes a course
    lines = fake.shown[0]["body"].split("\n\n")[0].splitlines()
    assert lines[1] == f"시간표에서 뺌: {ALLOWED} (폴더·녹음·자료는 그대로)", lines  # first, never folded
    assert lines[-1] == "그 밖: 넣음 2건 더" and len([x for x in lines if x.startswith("넣음")]) == 13
    fake.shown.clear()
    payload, is_error, _ = call("set_timetable", {"mode": "merge", "classes": [], "remove_courses": [ALLOWED]})
    assert is_error and len(fake.shown) == 1  # remove_courses: asked as well
    monkeypatch.setattr(approve, "dialog", lambda *a: pytest.fail("a plain merge of an automatic kind was asked about"))
    assert not call("set_timetable", MERGE)[1]
    assert writes.ALWAYS_ASK["set_timetable"]({"mode": "merge", "classes": []}) is False
    assert "매번" in writes.BY_NAME["set_timetable"]["description"]


def test_MCP_MORE_061_review_M1_probing_counts_and_every_hidden_cause_reads_the_same(library, monkeypatch):
    from test_mcp_write import _server

    told = []
    monkeypatch.setattr(approve, "notice", lambda title, text: told.append(text))
    settings.set_ai_link_auto("set_timetable", True)
    texts = set()
    for classes in ([{"course": HIDDEN, "days": ["토"], "start": "09:00", "end": "10:00"}],  # a hidden name
                    [{"course": ALLOWED, "days": ["화"], "start": "13:30", "end": "14:00"}],  # overlap with it
                    [{"course": " 비밀과목  나 ", "days": ["토"], "start": "09:00", "end": "10:00"}]):
        refused, is_error, _ = call("set_timetable", {"mode": "merge", "classes": classes})
        assert is_error
        texts.add(json.dumps(refused, ensure_ascii=False))
    assert texts == {json.dumps({"error": more_writes.HIDDEN}, ensure_ascii=False)} and HIDDEN not in texts.pop()
    assert len(told) == 1 and "보이지 않게 한 과목" in told[0] and HIDDEN not in told[0]  # once per minute
    # every refused write counts toward the 10-minute cap
    server = _server()
    server.burst.limit = 3
    for _ in range(3):
        server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "set_timetable", "arguments": {"mode": "merge", "classes": [
                {"course": HIDDEN, "days": ["토"], "start": "09:00", "end": "10:00"}]}}})
    reply = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
        "name": "set_timetable", "arguments": MERGE}})
    assert writes.TOO_MANY in reply["result"]["content"][0]["text"]


@pytest.mark.parametrize("name", ["비밀과목 나".replace("비밀", "비밀"), "NUL", "con.txt", "COM1", "aux", "LPT1", "녹음본",
                                  "합성 폴더 있음"])
def test_MCP_MORE_062_review_M2_case_reserved_and_existing_folder_names(library, monkeypatch, name):
    (library / "합성 폴더 있음").mkdir()
    settings.set_ai_link_auto("set_timetable", True)
    refused, is_error, _ = call("set_timetable", {"mode": "merge", "classes": [
        {"course": name, "days": ["토"], "start": "09:00", "end": "10:00"}]})
    assert is_error, refused
    assert not any(s.startswith("토") for s in table(library).get(name, []))
    if name in ("비밀과목 나", "합성 폴더 있음"):
        assert refused == {"error": more_writes.HIDDEN}  # an existing folder reads like a hidden course


def test_MCP_MORE_063_review_M2_casefolded_names_and_no_folder_list_in_the_reply(library, monkeypatch):
    from lecturehub.core import timetable as tt

    (library / "Hydro Lab").mkdir()
    (library / "시간표.csv").write_text(TABLE + "Hydro Lab,Hydro Lab,목,09:00,10:00\n", encoding="utf-8-sig")
    settings.set_ai_link_auto("set_timetable", True)
    refused, is_error, _ = call("set_timetable", {"mode": "merge", "classes": [
        {"course": "hydro lab", "days": ["토"], "start": "09:00", "end": "10:00"}]})
    assert is_error and refused == {"error": more_writes.HIDDEN}
    payload, is_error, _ = call("set_timetable", MERGE)
    assert not is_error and "new_folders" not in payload and "schedules" not in payload
    for bad in ("NUL", "Com3.log", "녹음본", "전사본"):
        with pytest.raises(tt.TimetableError):
            tt.folder_name(bad)


@pytest.mark.parametrize("name", ["합성\x00과목", "합성\x1b과목", "합성\u202e과목", "=HYPERLINK(1)", "+합성", "-합성", "@합성",
                                  "합성[과목]", "합성\u200b과목"])
def test_MCP_MORE_064_review_M3_control_format_and_formula_characters_are_refused(library, monkeypatch, name):
    from lecturehub.core import timetable as tt

    settings.set_ai_link_auto("set_timetable", True)
    before = (library / "시간표.csv").read_bytes()
    refused, is_error, _ = call("set_timetable", {"mode": "merge", "classes": [
        {"course": name, "days": ["토"], "start": "09:00", "end": "10:00"}]})
    assert is_error and "쓸 수 없는 글자" in refused["error"], refused
    assert (library / "시간표.csv").read_bytes() == before
    if name[0] not in "=+-@[" and "[" not in name:
        with pytest.raises(tt.TimetableError):
            tt.folder_name(name)


def test_MCP_MORE_065_review_L2_fingerprint_follows_what_the_dialog_shows(library, monkeypatch):
    from lecturehub.transcription.consent import ConsentStore

    (library / "시간표.csv").write_text(TABLE + "합성 표만,합성 표만,목,09:00,10:00\n", encoding="utf-8-sig")
    ConsentStore(library / "_lecturehub" / "consent.json").grant(access.PROVIDER, "합성 표만", 5, "n")

    def user_withdraws():  # only in 시간표.csv: the replace would no longer remove it
        ConsentStore(library / "_lecturehub" / "consent.json").revoke(access.PROVIDER, "합성 표만", 6)

    answer(monkeypatch, approve.ONCE, during=user_withdraws)
    payload, is_error, _ = call("set_timetable", {"mode": "replace", "classes": [
        {"course": ALLOWED, "days": ["월"], "start": "09:00", "end": "10:15"}]})
    assert is_error and payload == {"error": approve.CHANGED} and "합성 표만" in table(library)


def test_MCP_MORE_066_review_L3_undo_waits_for_the_recording(library, monkeypatch):
    settings.set_ai_link_auto("set_timetable", True)
    payload, is_error, _ = call("set_timetable", MERGE)
    assert not is_error
    monkeypatch.setattr(more_writes, "recording_now", lambda root: True)
    with pytest.raises(changes.WriteRefused, match="녹음 중"):
        changes.undo(library, payload["change_id"])
    assert table(library)[ALLOWED] == ["월 10:00~11:15"]


def test_MCP_MORE_067_review_L6_a_rolled_back_write_leaves_no_backup(library, monkeypatch):
    from lecturehub.index import ingest

    settings.set_ai_link_auto("set_timetable", True)
    monkeypatch.setattr(ingest, "sync_schedule", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("합성 실패")))
    payload, is_error, _ = call("set_timetable", MERGE)
    assert is_error
    backups = library / "_lecturehub" / "backups"
    assert not list(backups.glob("시간표_*.csv")) if backups.exists() else True


# ------------------------------------------------------------------ review of 2026-11-25 (photo, app, wording)
def test_MCP_MORE_070_review_L4_the_launch_sends_the_confirmed_copy_only(library, tmp_path, monkeypatch):
    import os
    import time

    from lecturehub.mcp import connect

    picture = tmp_path / "사진.png"
    picture.write_bytes(PNG)
    api, launched = _photo_api(library, monkeypatch, picture)
    q = api.timetable_photo_confirm()
    picture.write_bytes(b"\x89PNG\r\n\x1a\n swapped!!")  # the same size, other bytes, inside the token's time
    assert api.timetable_photo_open(q["token"]) == {"ok": True}
    sent = list(launched[0][1].glob("timetable_photo_*.png"))
    assert len(sent) == 1 and sent[0].read_bytes() == PNG  # what was confirmed, not the swapped file
    # a copy changed after the confirmation is refused and removed
    q = api.timetable_photo_confirm()
    staged = api._consent_tokens[q["token"]][1].split("|", 1)[1]
    with open(staged, "ab") as handle:
        handle.write(b"x")
    assert "바뀌었습니다" in api.timetable_photo_open(q["token"])["error"] and not os.path.exists(staged)
    # not an image, whatever its name; copies older than a day are swept at the next use
    fake = tmp_path / "가짜.png"
    fake.write_bytes(b"MZ not a picture")
    assert connect.photo_problem(fake) == connect.NOT_AN_IMAGE
    old = sent[0]
    os.utime(old, (time.time() - 2 * 86_400, time.time() - 2 * 86_400))
    q = api.timetable_photo_confirm()
    assert not old.exists() and q["yes"]


def test_MCP_MORE_071_review_L4_a_link_named_png_is_refused(library, tmp_path, monkeypatch):
    from lecturehub.mcp import connect

    target = tmp_path / "진짜.png"
    target.write_bytes(PNG)
    link = tmp_path / "링크.png"
    try:
        link.symlink_to(target)
    except OSError:
        monkeypatch.setattr(connect, "_reparse_point", lambda path: Path(path) == link)  # no symlink right here
        link.write_bytes(PNG)
    assert "링크" in connect.photo_problem(link)
    assert connect.photo_problem(target) is None


def test_MCP_MORE_072_review_L5_an_unreadable_timetable_keeps_the_old_courses(library, monkeypatch):
    from types import SimpleNamespace

    from lecturehub.ui import backend
    from lecturehub.ui.backend import Backend

    fake = SimpleNamespace(root=library, timetable_path=library / "시간표.csv", courses=["예전"],
                           _lock=__import__("threading").Lock(), _state={})
    check = Backend._timetable_check.__get__(fake)
    check(reload=False)
    (library / "시간표.csv").write_bytes("폴더,과목\n".encode("cp949") + b"\xb0\xa1\xff\xfe,x\n")
    monkeypatch.setattr(backend, "load_courses", lambda *a: (_ for _ in ()).throw(ValueError("encoding suspect")))
    check()
    assert fake.courses == ["예전"] and "timetable_version" not in fake._state


def test_MCP_MORE_073_review_M4_texts_promise_a_dialog_only_when_not_automatic(library, tmp_path, monkeypatch):
    from lecturehub.core import agent_workspace

    picture = tmp_path / "사진.png"
    picture.write_bytes(PNG)
    api, _ = _photo_api(library, monkeypatch, picture)
    api.timetable_photo_confirm()
    message = api._main.asked[-1][1]
    assert "자동으로 두지 않았다면" in message and "하루 동안" in message
    skill = (agent_workspace.SKILLS_SRC / "timetable-from-photo" / "SKILL.md").read_text(encoding="utf-8")
    assert "자동으로 두지 않았다면" in skill and "자동이어도 늘 묻는다" in skill
    root = Path(__file__).resolve().parents[1]
    docs = root / "tools" / "public_docs" / "docs"
    if not docs.is_dir():  # the public export puts these documents at docs/
        docs = root / "docs"
    for name in ("PRIVACY.md", "INSTALL.md"):
        assert "자동으로 두지 않았다면" in (docs / name).read_text(encoding="utf-8"), name

@pytest.mark.parametrize("first", ["lecturehub.mcp.more_writes", "lecturehub.mcp.writes", "lecturehub.mcp.changes"])
def test_MCP_MORE_080_each_module_can_be_imported_first(first):
    """The built app's self-check imports every module by itself; more_writes first used to fail (circular import)."""
    import subprocess
    import sys

    code = (f"import {first}; from lecturehub.mcp import writes; "
            "assert {'set_timetable', 'add_schedule_change', 'set_reminder_settings', 'request_notice_rescan', "
            "'save_note'} <= set(writes.HANDLERS) and len(writes.DEFINITIONS) == len(writes.BY_NAME) == 9")
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-500:]
