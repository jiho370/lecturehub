"""D-080 (MCP-APPROVE): Lecture Hub asks the user on screen before an AI app's change — the first time per kind —
then the kinds the user set to automatic apply at once. The native dialog is never shown here: ``approve.dialog`` is
replaced in-process (conftest answers "could not be shown" unless a test says otherwise). Synthetic semester only.
"""
from __future__ import annotations

import inspect
import json
import sqlite3
import sys
import threading
from contextlib import closing

import pytest
from test_mcp_access import ALLOWED, build_library
from test_mcp_write import ADD, call, dump, event_id, rows

from lecturehub.core import db, settings
from lecturehub.mcp import access, approve, changes, server, writes

REAL_DIALOG = approve.dialog  # taken at import, before conftest replaces it for each test


@pytest.fixture
def library(tmp_path, monkeypatch):
    root = tmp_path / "학기 (합성) 26-2"
    build_library(root)
    monkeypatch.setenv("LECTUREHUB_ROOT", str(root))
    monkeypatch.setenv("LECTUREHUB_EDITION", "full")
    settings.set_ai_link(True)
    settings.set_ai_link_write(True)
    return root


class Dialog:
    """Answers in turn and remembers what each dialog said."""

    def __init__(self, *answers, during=None):
        self.answers, self.during, self.shown = list(answers), during, []

    def __call__(self, title, instruction, body, timeout_s):
        self.shown.append({"title": title, "instruction": instruction, "body": body, "timeout": timeout_s})
        if self.during:
            self.during()
        return self.answers.pop(0) if self.answers else approve.FAILED


def answer(monkeypatch, *answers, during=None) -> Dialog:
    fake = Dialog(*answers, during=during)
    monkeypatch.setattr(approve, "dialog", fake)
    return fake


def ai_event(library, monkeypatch, title: str = ADD["title"]) -> dict:
    """An event the AI app added (made with the kind set to automatic, then back to asking)."""
    settings.set_ai_link_auto("add_calendar_event", True)
    added, is_error, _ = call("add_calendar_event", {**ADD, "title": title})
    assert not is_error and not added.get("already"), added
    settings.set_ai_link_auto("add_calendar_event", False)
    return added


# ------------------------------------------------------------------ the five paths of one dialog
def test_MCP_APPROVE_001_allow_once_applies_and_asks_again_next_time(library, monkeypatch):
    fake = answer(monkeypatch, approve.ONCE, approve.ONCE)
    added, is_error, _ = call("add_calendar_event", ADD)
    assert not is_error and added["status"] == "confirmed" and len(fake.shown) == 1
    assert settings.ai_link_auto()["add_calendar_event"] is False and not access.auto("add_calendar_event")
    second, is_error, _ = call("add_calendar_event", {**ADD, "title": "두 번째 발표"})
    assert not is_error and len(fake.shown) == 2  # still asked
    assert fake.shown[0]["timeout"] == approve.TIMEOUT_S == 50
    assert [r["id"] for r in changes.recent(library)] == [second["change_id"], added["change_id"]]


def test_MCP_APPROVE_002_auto_applies_saves_the_kind_and_the_next_call_is_not_asked(library, monkeypatch):
    fake = answer(monkeypatch, approve.AUTO)
    added, is_error, _ = call("add_calendar_event", ADD)
    assert not is_error and added["status"] == "confirmed" and "앞으로 묻지 않고" in added["message"]
    assert settings.ai_link_auto() == {**{k: False for k in settings.AI_AUTO_KINDS}, "add_calendar_event": True}
    assert settings.ai_link() == {"enabled": True, "write": True}  # nothing else of the switch changed
    assert settings.settings_path().with_name("settings.json.bak").is_file()
    again, is_error, _ = call("add_calendar_event", {**ADD, "title": "다음 발표"})
    assert not is_error and len(fake.shown) == 1  # automatic now
    # other kinds still ask
    _, is_error, _ = call("update_calendar_event", {"event_id": again["event_id"], "title": "바뀐 발표"})
    assert len(fake.shown) == 2 and is_error  # the fake has no answer left: could not be shown


@pytest.mark.parametrize("given,message", [(approve.REJECT, approve.REJECTED), (approve.TIMEOUT, approve.NO_ANSWER),
                                           (approve.FAILED, approve.NO_DIALOG), ("something else", approve.NO_DIALOG)],
                         ids=["reject", "timeout", "failed", "unknown-answer"])
def test_MCP_APPROVE_003_reject_timeout_and_a_dialog_that_cannot_be_shown_change_nothing(library, monkeypatch, given,
                                                                                          message):
    before = dump(library) + rows(library, "SELECT count(*) FROM Event")
    answer(monkeypatch, given)
    payload, is_error, _ = call("add_calendar_event", ADD)
    assert is_error and payload == {"error": message}
    assert dump(library) + rows(library, "SELECT count(*) FROM Event") == before  # the trial was rolled back
    assert not changes.log_path(library).exists() and settings.ai_link_auto()["add_calendar_event"] is False
    files = set((library / ALLOWED / "정리본").iterdir())
    payload, is_error, _ = call("save_note", {"lecture_id": "a1", "content": "# 정리"})
    assert is_error and set((library / ALLOWED / "정리본").iterdir()) == files


def test_MCP_APPROVE_004_a_dialog_that_raises_or_the_default_test_dialog_refuses(library, monkeypatch):
    payload, is_error, _ = call("add_calendar_event", ADD)  # conftest: the dialog could not be shown
    assert is_error and payload == {"error": approve.NO_DIALOG}

    def broken(*args):
        raise OSError("no desktop")

    monkeypatch.setattr(approve, "dialog", broken)
    payload, is_error, _ = call("add_calendar_event", ADD)
    assert is_error and payload == {"error": approve.NO_DIALOG} and not changes.log_path(library).exists()


def test_MCP_APPROVE_005_automatic_kinds_skip_the_dialog(library, monkeypatch):
    monkeypatch.setattr(approve, "dialog", lambda *a: pytest.fail("the dialog was shown for an automatic kind"))
    for kind in settings.AI_AUTO_KINDS:
        settings.set_ai_link_auto(kind, True)
    added, is_error, _ = call("add_calendar_event", ADD)
    assert not is_error
    assert not call("update_calendar_event", {"event_id": added["event_id"], "title": "고침"})[1]
    assert not call("delete_calendar_event", {"event_id": added["event_id"]})[1]
    assert not call("resolve_candidate", {"event_id": event_id(library, "퀴즈 — 수업 중 언급"), "action": "dismiss"})[1]
    assert not call("save_note", {"lecture_id": "a1", "content": "# 정리"})[1]
    assert len(changes.recent(library)) == 5


def test_MCP_APPROVE_006_nothing_to_change_needs_no_dialog(library, monkeypatch):
    added = ai_event(library, monkeypatch)
    monkeypatch.setattr(approve, "dialog", lambda *a: pytest.fail("asked about a change that changes nothing"))
    same, is_error, _ = call("add_calendar_event", ADD)  # already there
    assert not is_error and same["already"] and same["event_id"] == added["event_id"]
    unchanged, is_error, _ = call("update_calendar_event", {"event_id": added["event_id"], "title": ADD["title"]})
    assert not is_error and unchanged["message"] == "바뀐 것이 없습니다."
    refused, is_error, _ = call("delete_calendar_event", {"event_id": "없는일정"})  # refusals come before any dialog
    assert is_error and refused == {"error": access.NOT_FOUND}
    bad, is_error, _ = call("add_calendar_event", {**ADD, "date": "2026-02-30"})
    assert is_error and "있는 날짜" in bad["error"]


# ------------------------------------------------------------------ between the dialog and the write
def test_MCP_APPROVE_010_a_target_changed_while_the_dialog_was_open_is_refused(library, monkeypatch):
    from lecturehub.index import calendar_events

    added = ai_event(library, monkeypatch)
    database = library / "_lecturehub" / "lecturehub.sqlite"

    def user_edits():  # the user changes the same event in the app while the dialog waits
        with closing(db.connect(database)) as c, c:
            calendar_events.update(c, added["event_id"], note="내가 고친 메모")

    answer(monkeypatch, approve.ONCE, during=user_edits)
    payload, is_error, _ = call("update_calendar_event", {"event_id": added["event_id"], "title": "AI가 바꿈"})
    assert is_error and payload == {"error": approve.CHANGED}
    assert rows(library, "SELECT title, note FROM CalendarEvent WHERE id = ?", (added["event_id"],)) == [
        (ADD["title"], "내가 고친 메모")]
    # an event changed by an AI change in between (same values afterwards) is refused too: its history moved
    other = ai_event(library, monkeypatch, "둘째 발표")

    def ai_in_between():
        settings.set_ai_link_auto("update_calendar_event", True)
        try:
            assert not call("update_calendar_event", {"event_id": other["event_id"], "place": "다른 곳"})[1]
            assert not call("update_calendar_event", {"event_id": other["event_id"], "place": ADD["place"]})[1]
        finally:
            settings.set_ai_link_auto("update_calendar_event", False)

    answer(monkeypatch, approve.ONCE, during=ai_in_between)
    payload, is_error, _ = call("delete_calendar_event", {"event_id": other["event_id"]})
    assert is_error and payload == {"error": approve.CHANGED}


def test_MCP_APPROVE_011_a_course_taken_back_while_the_dialog_was_open_answers_not_found(library, monkeypatch):
    from lecturehub.transcription.consent import ConsentStore

    answer(monkeypatch, approve.ONCE,
           during=lambda: ConsentStore(access.consent_path(library)).revoke(access.PROVIDER, ALLOWED, 2))
    payload, is_error, _ = call("save_note", {"lecture_id": "a1", "content": "# 정리"})
    assert is_error and payload == {"error": access.NOT_FOUND}
    assert not any(p.name.endswith("_ChatGPT.html") for p in (library / ALLOWED / "정리본").iterdir())


def test_MCP_APPROVE_012_the_write_switch_turned_off_while_the_dialog_was_open(library, monkeypatch):
    answer(monkeypatch, approve.AUTO, during=lambda: settings.set_ai_link_write(False))
    payload, is_error, _ = call("add_calendar_event", ADD)
    assert is_error and payload == {"error": access.WRITE_OFF}
    assert settings.ai_link_auto()["add_calendar_event"] is False  # not saved with the switch off
    assert not changes.log_path(library).exists()


def test_MCP_APPROVE_013_a_change_nobody_was_asked_about_is_rolled_back(library, monkeypatch):
    """The trial found nothing to do (no dialog), then the data moved: the write must not land unasked."""
    monkeypatch.setattr(writes, "_preview", lambda *a, **k: None)
    before = dump(library)
    payload, is_error, _ = call("add_calendar_event", ADD)
    assert is_error and payload == {"error": approve.CHANGED} and dump(library) == before
    assert changes.recent(library) == []


def test_MCP_APPROVE_014_nothing_of_the_database_is_held_while_the_dialog_waits(library, monkeypatch):
    database = library / "_lecturehub" / "lecturehub.sqlite"
    seen = []

    def probe():
        other = sqlite3.connect(database, timeout=0, isolation_level=None)
        try:
            other.execute("BEGIN IMMEDIATE")  # "database is locked" if this call held the writer
            other.execute("ROLLBACK")
            busy, _, _ = other.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()  # 1 if a reader was still open
            seen.append(busy)
        finally:
            other.close()

    added = ai_event(library, monkeypatch)
    answer(monkeypatch, approve.ONCE, approve.ONCE, approve.ONCE, during=probe)
    assert not call("save_note", {"lecture_id": "a1", "content": "# 정리\n\n본문"})[1]
    assert not call("update_calendar_event", {"event_id": added["event_id"], "title": "고침"})[1]
    assert not call("resolve_candidate", {"event_id": event_id(library, "퀴즈 — 수업 중 언급"), "action": "confirm"})[1]
    assert seen == [0, 0, 0]


def test_MCP_APPROVE_015_a_real_server_loop_waits_for_the_dialog_with_nothing_open(library, monkeypatch):
    """The stdio loop is single-threaded: a reader in another thread (the app) is not blocked while it waits."""
    released = threading.Event()
    reads = []

    def during():
        def read():
            with closing(db.connect_readonly(library / "_lecturehub" / "lecturehub.sqlite")) as c:
                reads.append(c.execute("SELECT count(*) FROM CalendarEvent").fetchone()[0])
            released.set()

        threading.Thread(target=read).start()
        assert released.wait(10)

    answer(monkeypatch, approve.ONCE, during=during)
    assert not call("add_calendar_event", ADD)[1] and len(reads) == 1


# ------------------------------------------------------------------ what the dialog says
def test_MCP_APPROVE_020_the_dialog_shows_the_concrete_change(library, monkeypatch):
    fake = answer(monkeypatch, approve.ONCE, approve.ONCE, approve.ONCE, approve.ONCE, approve.ONCE)
    added, _, _ = call("add_calendar_event", ADD, server_for("claude-ai"))
    add = fake.shown[0]
    assert add["title"] == "Claude가 Lecture Hub를 바꾸려 합니다" and add["instruction"] == "일정 넣기 — 허용할까요?"
    for line in (f"과목: {ALLOWED}", "종류: 발표", "날짜: 2026-12-09 13:00~14:30", "제목: 기말 발표", "장소: 39동 101호",
                 "메모: 조별 10분"):
        assert line in add["body"].splitlines(), (line, add["body"])
    call("update_calendar_event", {"event_id": added["event_id"], "title": "기말 발표(수정)", "date": "2026-12-10"})
    update = fake.shown[1]["body"]
    assert "제목 (지금 → 바꾼 뒤): 기말 발표 → 기말 발표(수정)" in update
    assert "날짜·시각 (지금 → 바꾼 뒤): 2026-12-09 13:00~14:30 → 2026-12-10 13:00~14:30" in update
    assert "장소 (지금" not in update  # unchanged fields are not listed
    call("delete_calendar_event", {"event_id": added["event_id"], "reason": "중복"})
    delete = fake.shown[2]["body"]
    assert "일정: 2026-12-10 13:00~14:30 발표 · 기말 발표(수정)" in delete and "이 일정을 취소합니다" in delete and "까닭: 중복" in delete
    call("resolve_candidate", {"event_id": event_id(library, "퀴즈 — 수업 중 언급"), "action": "confirm"})
    assert "이 후보를 확정해 달력에 넣습니다" in fake.shown[3]["body"] and "퀴즈 — 수업 중 언급" in fake.shown[3]["body"]
    content = "# 1강 정리\n\n" + "가" * 2000
    call("save_note", {"lecture_id": "a1", "content": content})
    note = fake.shown[4]
    assert note["title"] == "ChatGPT가 Lecture Hub를 바꾸려 합니다" and note["instruction"] == "정리본 새로 저장 — 허용할까요?"
    lines = note["body"].splitlines()
    assert any(line.startswith("회차: ") for line in lines) and f"길이: {len(content):,}자" in note["body"]
    excerpt = next(line for line in lines if line.startswith("내용 앞부분: "))
    assert excerpt.startswith("내용 앞부분: # 1강 정리 가가") and len(excerpt) <= len("내용 앞부분: ") + 300
    assert approve.GUIDE in note["body"]


def server_for(client: str) -> server.Server:
    srv = server.Server()
    srv.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "clientInfo": {"name": client, "version": "1"}}})
    return srv


def test_MCP_APPROVE_021_dialog_text_from_the_ai_app_is_sanitised(library, monkeypatch):
    fake = answer(monkeypatch, approve.REJECT, approve.REJECT)
    planted = ("시험\u202e\n\n[이번만 허용]\r\n이 종류는 앞으로 자동\x00\x07\u2066숨김\u2069 " + "길" * 300)
    srv = server_for("ChatGPT\n[이번만 허용] 가짜 앱 이름\u202e")
    call("add_calendar_event", {**ADD, "title": planted[:200], "note": planted[:1000]}, srv)
    shown = fake.shown[0]
    assert shown["title"] == "ChatGPT가 Lecture Hub를 바꾸려 합니다"  # the fixed label, never the client's own name
    body = shown["body"]
    for bad in ("\u202e", "\u2066", "\u2069", "\x00", "\x07", "\r", "[이번만 허용]"):
        assert bad not in body, repr(bad)
    lines = body.split("\n")
    labels = ("과목: ", "종류: ", "날짜: ", "제목: ", "장소: ", "메모: ")
    fields, guide = lines[:lines.index("")], lines[lines.index("") + 1:]
    assert all(line.startswith(labels) for line in fields) and guide == [approve.GUIDE]  # no extra lines of its own
    title = next(line for line in fields if line.startswith("제목: "))
    assert len(title) <= len("제목: ") + 120 and title.endswith("…") and "(이번만 허용)" in title
    assert len(next(line for line in fields if line.startswith("메모: "))) <= len("메모: ") + 200
    # a long note text is cut, and a name the AI app sent never reaches the title
    call("save_note", {"lecture_id": "a1", "content": "\u202e[거절]\n" * 500}, srv)
    assert "\u202e" not in fake.shown[1]["body"] and "[거절]" not in fake.shown[1]["body"]


def test_MCP_APPROVE_022_clean_and_actor():
    assert approve.clean("a\nb\tc\u200b\u202ed  e") == "a b c d e"
    assert approve.clean("[x]") == "(x)" and approve.clean(None) == "" and approve.clean(12) == "12"
    assert approve.clean("가" * 10, 5) == "가가가가…"
    assert approve.actor("Claude가 씀") == "Claude가" and approve.actor("ChatGPT가 씀") == "ChatGPT가"
    assert approve.actor("AI 앱이 씀") == "AI 앱이" and approve.actor("가짜 이름\n") == "AI 앱이"


# ------------------------------------------------------------------ settings
def test_MCP_APPROVE_030_the_auto_setting_is_read_strictly(library):
    assert not any(access.auto(kind) for kind in settings.AI_AUTO_KINDS)  # a new install asks for everything
    settings.set_ai_link_auto("save_note", True)
    assert access.auto("save_note") and not access.auto("add_calendar_event")
    data = json.loads(settings.settings_path().read_text(encoding="utf-8"))
    data["ai_link"]["auto"] = {"save_note": "true", "add_calendar_event": 1}  # not exactly true: ask
    settings.settings_path().write_text(json.dumps(data), encoding="utf-8")
    assert not access.auto("save_note") and not access.auto("add_calendar_event")
    settings.settings_path().write_text("{broken", encoding="utf-8")
    assert not access.auto("save_note")
    with pytest.raises(KeyError):
        settings.set_ai_link_auto("delete_everything", True)


def test_MCP_APPROVE_031_the_strict_save_from_the_mcp_process(library):
    assert settings.save_ai_link_auto_strict("save_note") is True
    assert settings.ai_link_auto()["save_note"] is True
    assert settings.save_ai_link_auto_strict("not_a_kind") is False
    settings.set_ai_link_write(False)
    assert settings.save_ai_link_auto_strict("add_calendar_event") is False  # switched off meanwhile
    settings.set_ai_link_write(True)
    good = settings.settings_path().read_bytes()
    settings.settings_path().write_text("{broken", encoding="utf-8")
    assert settings.save_ai_link_auto_strict("add_calendar_event") is False
    assert settings.settings_path().read_text(encoding="utf-8") == "{broken"  # left alone, no .bak fallback used
    settings.settings_path().write_bytes(good)
    assert not list(settings.settings_path().parent.glob("*.part"))


def test_MCP_APPROVE_032_auto_that_cannot_be_saved_still_applies_this_change_once(library, monkeypatch):
    monkeypatch.setattr(settings, "save_ai_link_auto_strict", lambda kind: False)
    fake = answer(monkeypatch, approve.AUTO, approve.ONCE)
    added, is_error, _ = call("add_calendar_event", ADD)
    assert not is_error and "저장하지 못해 다음에도 묻습니다" in added["message"]
    call("add_calendar_event", {**ADD, "title": "다음"})
    assert len(fake.shown) == 2


def test_MCP_APPROVE_033_no_bypass_by_environment_or_argument():
    """The AI app can edit its own MCP configuration and environment: nothing there may skip the dialog."""
    for module in (approve, writes):
        source = inspect.getsource(module)
        assert "os.environ" not in source and "getenv" not in source and "sys.argv" not in source, module.__name__
    assert set(inspect.signature(writes.run).parameters) == {"name", "arguments", "written_by"}
    assert list(approve.KINDS) == list(settings.AI_AUTO_KINDS) == list(writes.HANDLERS)


def test_MCP_APPROVE_034_tool_descriptions_and_instructions_say_it_asks_first(library):
    tools = {t["name"]: t for t in server_for("codex").handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
             ["result"]["tools"]}
    for name in writes.HANDLERS:
        assert "처음 쓸 때는 Lecture Hub가 화면에 확인 창" in tools[name]["description"]
        assert "자동으로 둔 종류는 묻지 않고" in tools[name]["description"]
        assert "따로 묻지 않음" not in tools[name]["description"]
    init = server.Server().handle({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
    assert "확인 창" in init["result"]["instructions"]


# ------------------------------------------------------------------ the settings screen
def _app(root):
    import threading as _threading
    from types import SimpleNamespace

    from lecturehub.ui.app import Api
    from lecturehub.ui.backend import Backend

    fake = SimpleNamespace(root=root, needs_setup=False, courses=[SimpleNamespace(folder=ALLOWED)], extras=list,
                           _lock=_threading.Lock(), _state={"library_version": 0})
    for name in ("_ai_link_courses", "ai_link_status", "ai_link_auto", "ai_link_auto_reset"):
        setattr(fake, name, getattr(Backend, name).__get__(fake))
    return Api(fake, None)


class _Window:
    def __init__(self, answer):
        self.answer, self.asked = answer, []

    def create_confirmation_dialog(self, title, message):
        self.asked.append((title, message))
        return self.answer


def test_MCP_APPROVE_040_settings_rows_need_the_native_dialog_to_go_automatic(library):
    from lecturehub.ui.app import AI_AUTO_WARNING

    api = _app(library)
    status = api._backend.ai_link_status()
    assert [(k["kind"], k["label"], k["auto"]) for k in status["auto"]] == [(k, v, False) for k, v in approve.KINDS.items()]
    refused = api.ai_link_auto("save_note", True)  # straight from page script
    assert refused["ok"] is False and refused["needs_confirm"] and not settings.ai_link_auto()["save_note"]
    assert api.ai_link_auto("save_note", True, "made-up")["ok"] is False
    api._main = _Window(False)
    assert api.ai_link_auto_confirm("save_note") == {"ok": True, "yes": False}
    api._main = _Window(True)
    q = api.ai_link_auto_confirm("save_note")
    title, message = api._main.asked[-1]
    assert q["yes"] and "정리본 새로 저장" in title and AI_AUTO_WARNING in message
    assert api.ai_link_auto("add_calendar_event", True, q["token"])["ok"] is False  # bound to its kind
    q = api.ai_link_auto_confirm("save_note")
    done = api.ai_link_auto("save_note", True, q["token"])
    assert done["ok"] and next(k for k in done["auto"] if k["kind"] == "save_note")["auto"] is True
    assert access.auto("save_note")
    assert api.ai_link_auto("update_calendar_event", True, q["token"])["ok"] is False  # one time only
    assert api.ai_link_auto_confirm("delete_everything")["ok"] is False
    back = api.ai_link_auto("save_note", False)  # back to asking: no dialog needed
    assert back["ok"] and not access.auto("save_note") and len(api._main.asked) == 2


def test_MCP_APPROVE_042_all_kinds_automatic_in_one_click_behind_one_dialog(library):
    """[모두 자동] (user 2026-11-25): one native dialog that names every kind; its token is for "*" only."""
    api = _app(library)
    assert api.ai_link_auto("*", True)["needs_confirm"] and not any(settings.ai_link_auto().values())  # page script
    api._main = _Window(True)
    single = api.ai_link_auto_confirm("save_note")["token"]
    assert api.ai_link_auto("*", True, single)["ok"] is False  # a one-kind token is not an all-kinds token
    q = api.ai_link_auto_confirm("*")
    title, message = api._main.asked[-1]
    assert q["yes"] and "모든 종류" in title and all(label in message for label in approve.KINDS.values())
    assert api.ai_link_auto("save_note", True, q["token"])["ok"] is False  # and the other way round
    q = api.ai_link_auto_confirm("*")
    done = api.ai_link_auto("*", True, q["token"])
    assert done["ok"] and all(k["auto"] for k in done["auto"]) and all(access.auto(k) for k in settings.AI_AUTO_KINDS)
    assert api.ai_link_auto("*", True, q["token"])["ok"] is False  # one time only
    assert settings.ai_link() == {"enabled": True, "write": True}  # nothing else changed
    api._main = _Window(False)
    assert api.ai_link_auto_confirm("*") == {"ok": True, "yes": False}


def test_MCP_APPROVE_041_reset_asks_again_for_every_kind(library):
    api = _app(library)
    for kind in settings.AI_AUTO_KINDS:
        settings.set_ai_link_auto(kind, True)
    result = api.ai_link_auto_reset()
    assert result["ok"] and not any(k["auto"] for k in result["auto"])
    assert not any(access.auto(kind) for kind in settings.AI_AUTO_KINDS)
    assert settings.ai_link() == {"enabled": True, "write": True}


# ------------------------------------------------------------------ the native dialog itself (never shown)
@pytest.mark.skipif(sys.platform != "win32", reason="Windows dialog")
def test_MCP_APPROVE_050_the_task_dialog_structures_work_without_showing_it():
    """TaskDialogIndirect through comctl32 v6 is called for real but closes itself as [거절] when it is created,
    before it is displayed."""
    assert approve._task_dialog("합성 제목", "합성 질문", "합성 내용", 5, close_at_once=True) == approve.REJECT


def test_MCP_APPROVE_051_the_simple_box_when_the_task_dialog_fails(monkeypatch):
    if sys.platform != "win32":
        assert REAL_DIALOG("t", "i", "b", 1) == approve.FAILED
        return

    def broken(*args, **kwargs):
        raise AttributeError("TaskDialogIndirect")

    seen = []
    monkeypatch.setattr(approve, "_task_dialog", broken)
    monkeypatch.setattr(approve, "_message_box", lambda title, text, timeout: seen.append((title, text)) or approve.ONCE)
    assert REAL_DIALOG("합성 제목", "질문", "내용", 50) == approve.ONCE
    assert "[예] 이번만 허용" in seen[0][1] and "설정 › AI › AI 앱 연결" in seen[0][1]
    monkeypatch.setattr(approve, "_message_box", lambda *a: (_ for _ in ()).throw(OSError("no desktop")))
    assert REAL_DIALOG("t", "i", "b", 50) == approve.FAILED
