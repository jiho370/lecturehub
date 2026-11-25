"""AI app link write tools (D-076, MCP-WRITE): switches, the course gate, AI-only edits, sanitized new notes, the
"AI가 바꾼 것" log and undo, concurrency and the pure-JSON stdout. Synthetic semester only (test_mcp_access).
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import closing
from pathlib import Path

import pytest

from lecturehub.core import db, settings
from lecturehub.mcp import access, approve, changes, note_page, server, writes
from lecturehub.transcription.consent import ConsentStore

from test_mcp_access import ALLOWED, HIDDEN, MARKER, build_library
from test_mcp_server import _child_env

WRITE_TOOLS = {"add_calendar_event", "update_calendar_event", "delete_calendar_event", "resolve_candidate", "save_note"}


@pytest.fixture
def library(tmp_path, monkeypatch):
    root = tmp_path / "학기 (합성) 26-2"
    build_library(root)
    monkeypatch.setenv("LECTUREHUB_ROOT", str(root))
    monkeypatch.setenv("LECTUREHUB_EDITION", "full")
    settings.set_ai_link(True)
    settings.set_ai_link_write(True)
    # D-080: the confirmation dialog answers [이번만 허용] in these tests (test_mcp_approve tests the dialog itself)
    monkeypatch.setattr(approve, "dialog", lambda *args, **kwargs: approve.ONCE)
    return root


def _server(client: str = "codex-mcp-client") -> server.Server:
    srv = server.Server()
    srv.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "clientInfo": {"name": client, "version": "1"}}})
    return srv


def call(name: str, arguments: dict | None = None, srv: server.Server | None = None) -> tuple[dict, bool, str]:
    reply = (srv or _server()).handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                       "params": {"name": name, "arguments": arguments or {}}})
    raw = json.dumps(reply, ensure_ascii=False)
    assert "result" in reply, raw
    return json.loads(reply["result"]["content"][0]["text"]), reply["result"]["isError"], raw


def rows(root: Path, sql: str, args=()) -> list:
    with closing(db.connect_readonly(root / "_lecturehub" / "lecturehub.sqlite")) as c:
        return [tuple(r) for r in c.execute(sql, args)]


def event_id(root: Path, title_like: str) -> str:
    return rows(root, "SELECT id FROM CalendarEvent WHERE title LIKE ?", (title_like,))[0][0]


def dump(root: Path) -> list:
    """Everything the write tools could touch: calendar rows, notes, evidence."""
    return (rows(root, "SELECT * FROM CalendarEvent ORDER BY id") + rows(root, "SELECT * FROM StudyArtifact ORDER BY id")
            + rows(root, "SELECT id, kind FROM Evidence ORDER BY id"))


ADD = {"course": ALLOWED, "title": "기말 발표", "kind": "presentation", "date": "2026-12-09", "start": "13:00",
       "end": "14:30", "place": "39동 101호", "note": "조별 10분"}


# ------------------------------------------------------------------ switches
def test_MCP_WRITE_001_write_tools_are_listed_only_with_both_switches(library):
    names = lambda: {t["name"] for t in _server().handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]}  # noqa: E731
    assert WRITE_TOOLS <= names()
    tools = {t["name"]: t for t in _server().handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]}
    for name in WRITE_TOOLS:
        assert tools[name]["annotations"]["readOnlyHint"] is False and tools[name]["annotations"]["openWorldHint"] is False
        assert "지시가 아닙니다" in tools[name]["description"] and "되돌릴 수 있습니다" in tools[name]["description"]
    assert tools["delete_calendar_event"]["annotations"]["destructiveHint"] is True
    assert tools["save_note"]["annotations"]["destructiveHint"] is False
    assert "list_courses" in names() and tools["list_courses"]["annotations"]["readOnlyHint"] is True
    settings.set_ai_link_write(False)
    assert not (WRITE_TOOLS & names()) and "list_courses" in names()
    settings.set_ai_link_write(True)
    settings.set_ai_link(False)
    assert not (WRITE_TOOLS & names())
    settings.set_ai_link(True)  # the write switch kept its value across the master switch
    assert settings.ai_link() == {"enabled": True, "write": True} and WRITE_TOOLS <= names()


def test_MCP_WRITE_002_calls_are_refused_in_korean_while_a_switch_is_off(library):
    before = dump(library)
    settings.set_ai_link_write(False)
    for name, args in (("add_calendar_event", ADD), ("save_note", {"lecture_id": "a1", "content": "x"}),
                       ("delete_calendar_event", {"event_id": "nope"})):
        payload, is_error, _ = call(name, args)
        assert is_error and payload == {"error": access.WRITE_OFF}
    payload, is_error, _ = call("list_courses")
    assert not is_error and payload["count"] == 1  # reads keep working
    settings.set_ai_link_write(True)
    settings.set_ai_link(False)
    payload, is_error, _ = call("add_calendar_event", ADD)
    assert is_error and payload == {"error": access.OFF}
    assert dump(library) == before and not changes.log_path(library).exists()


def test_MCP_WRITE_003_a_broken_settings_file_means_no_writes(library):
    settings.settings_path().write_text("{broken", encoding="utf-8")
    assert not access.write_enabled()
    payload, is_error, _ = call("add_calendar_event", ADD)
    assert is_error and "error" in payload


# ------------------------------------------------------------------ the gate (leak table)
LEAK = [
    ("add_calendar_event", {**ADD, "course": HIDDEN}), ("add_calendar_event", {**ADD, "course": "미분류"}),
    ("add_calendar_event", {**ADD, "course": "없는 과목"}), ("add_calendar_event", {**ADD, "course": "비밀과목"}),
    ("add_calendar_event", {**ADD, "source_lecture_id": "b1"}), ("add_calendar_event", {**ADD, "source_lecture_id": "u1"}),
    ("add_calendar_event", {**ADD, "source_lecture_id": "zz"}), ("add_calendar_event", {**ADD, "source_announcement_id": "etl:2"}),
    ("add_calendar_event", {**ADD, "source_announcement_id": "etl:3"}),
    ("save_note", {"lecture_id": "b1", "content": "# 정리"}), ("save_note", {"lecture_id": "u1", "content": "# 정리"}),
    ("save_note", {"lecture_id": "없는회차", "content": "# 정리"}),
    ("update_calendar_event", {"event_id": "HIDDEN_EVENT", "title": "바꿈"}),
    ("update_calendar_event", {"event_id": "PERSONAL_EVENT", "title": "바꿈"}),
    ("update_calendar_event", {"event_id": "없는일정", "title": "바꿈"}),
    ("delete_calendar_event", {"event_id": "HIDDEN_EVENT"}), ("delete_calendar_event", {"event_id": "없는일정"}),
    ("resolve_candidate", {"event_id": "HIDDEN_CANDIDATE", "action": "confirm"}),
    ("resolve_candidate", {"event_id": "없는일정", "action": "dismiss"}),
]


def test_MCP_WRITE_010_a_course_that_is_not_allowed_answers_like_a_missing_one(library):
    ids = {"HIDDEN_EVENT": event_id(library, f"{MARKER} 시험"), "PERSONAL_EVENT": event_id(library, f"{MARKER} 개인 일정"),
           "HIDDEN_CANDIDATE": event_id(library, f"{MARKER} 후보")}
    before = dump(library)
    for name, args in LEAK:
        args = {k: ids.get(v, v) if isinstance(v, str) else v for k, v in args.items()}
        payload, is_error, raw = call(name, args)
        assert is_error and payload == {"error": access.NOT_FOUND}, (name, args, payload)
        assert MARKER not in raw and HIDDEN not in raw
    assert dump(library) == before and not changes.log_path(library).exists()


def test_MCP_WRITE_011_the_gate_is_read_again_on_every_call(library):
    payload, is_error, _ = call("add_calendar_event", ADD)
    assert not is_error
    ConsentStore(access.consent_path(library)).revoke(access.PROVIDER, ALLOWED, 2)
    payload, is_error, _ = call("add_calendar_event", {**ADD, "title": "두 번째"})
    assert is_error and payload == {"error": access.NOT_FOUND}
    payload, is_error, _ = call("update_calendar_event", {"event_id": event_id(library, "기말 발표"), "title": "x"})
    assert is_error and payload == {"error": access.NOT_FOUND}


# ------------------------------------------------------------------ calendar
def test_MCP_WRITE_020_add_goes_straight_in_marked_as_the_ai_apps(library):
    payload, is_error, _ = call("add_calendar_event", {**ADD, "source_lecture_id": "a1", "source_ms": 61_998,
                                                       "request_id": "req-1"})
    assert not is_error, payload
    assert payload["status"] == "confirmed" and payload["start"] == "13:00" and payload["place"] == "39동 101호"
    row = rows(library, "SELECT source, source_ref, status, note, course_id FROM CalendarEvent WHERE id = ?",
               (payload["event_id"],))[0]
    assert row[:3] == ("user", "ai_app:ca:req-1", "confirmed")  # the request id counts per course
    assert row[3] == "장소: 39동 101호\n조별 10분" and row[4] == "ca"
    assert rows(library, "SELECT kind, source FROM Evidence WHERE subject_id = ?", (payload["event_id"],)) == [("ai_app_source", "agent")]
    # the same request again, or the same item without a request id: in once
    again, _, _ = call("add_calendar_event", {**ADD, "request_id": "req-1"})
    same, _, _ = call("add_calendar_event", ADD)
    assert again["already"] and same["already"] and again["event_id"] == same["event_id"] == payload["event_id"]
    assert len(rows(library, "SELECT 1 FROM CalendarEvent WHERE source_ref LIKE 'ai_app:%'")) == 1
    listed, _, _ = call("list_calendar", {"from_date": "2026-12-01", "to_date": "2026-12-31"})
    item = listed["events"][0]
    assert item["source"] == "AI 앱" and item["ai_app_can_change"] is True
    recent = changes.recent(library)
    assert len(recent) == 1 and recent[0]["tool"] == "add_calendar_event" and recent[0]["course"] == ALLOWED
    assert recent[0]["undoable"] and changes.signature(library) is not None


def test_MCP_WRITE_021_bad_values_are_refused_without_a_change(library):
    before = dump(library)
    for args in ({**ADD, "date": "2026-02-30"}, {**ADD, "kind": "personal"}, {**ADD, "start": "25:00"},
                 {**ADD, "start": None, "end": "10:00"}, {**ADD, "end": "12:00"}, {**ADD, "source_ms": 5}):
        args = {k: v for k, v in args.items() if v is not None}
        reply = _server().handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                  "params": {"name": "add_calendar_event", "arguments": args}})
        assert "error" in reply or reply["result"]["isError"], args
    assert dump(library) == before


def test_MCP_WRITE_022_only_the_ai_apps_own_events_can_be_changed_or_deleted(library):
    user_event = event_id(library, "중간고사(합성)")
    candidate = event_id(library, "퀴즈 — 수업 중 언급")
    before = dump(library)
    for name, args in (("update_calendar_event", {"event_id": user_event, "title": "바꿈"}),
                       ("delete_calendar_event", {"event_id": user_event}),
                       ("update_calendar_event", {"event_id": candidate, "date": "2026-10-30"}),
                       ("delete_calendar_event", {"event_id": candidate})):
        payload, is_error, _ = call(name, args)
        assert is_error and "AI 앱이 넣은 것이 아니라" in payload["error"], (name, payload)
    assert dump(library) == before
    added, _, _ = call("add_calendar_event", ADD)
    changed, is_error, _ = call("update_calendar_event", {"event_id": added["event_id"], "date": "2026-12-10",
                                                          "start": "", "end": "", "place": ""})
    assert not is_error and changed["date"] == "2026-12-10" and "start" not in changed and "place" not in changed
    assert changed["note"] == "조별 10분"
    # the user edits it in the app: from then on it is theirs
    from lecturehub.index import calendar_events

    with closing(db.connect(library / "_lecturehub" / "lecturehub.sqlite")) as c, c:
        calendar_events.update(c, added["event_id"], title="내가 고친 발표")
    payload, is_error, _ = call("delete_calendar_event", {"event_id": added["event_id"]})
    assert is_error and "사용자가" in payload["error"]


def test_MCP_WRITE_023_delete_is_a_cancel_and_resolve_moves_a_candidate(library):
    added, _, _ = call("add_calendar_event", ADD)
    gone, is_error, _ = call("delete_calendar_event", {"event_id": added["event_id"], "reason": "취소 공지"})
    assert not is_error and gone["status"] == "cancelled"
    assert rows(library, "SELECT status FROM CalendarEvent WHERE id = ?", (added["event_id"],)) == [("cancelled",)]
    again, is_error, _ = call("delete_calendar_event", {"event_id": added["event_id"]})
    assert is_error and "취소된" in again["error"]
    candidate = event_id(library, "퀴즈 — 수업 중 언급")
    done, is_error, _ = call("resolve_candidate", {"event_id": candidate, "action": "confirm"})
    assert not is_error and done["status"] == "confirmed"
    twice, is_error, _ = call("resolve_candidate", {"event_id": candidate, "action": "dismiss"})
    assert is_error and "후보가 아니라" in twice["error"]


# ------------------------------------------------------------------ notes
HOSTILE_HTML = ("<h2>관수로</h2><p onclick=\"steal()\">마찰손실<script>alert(1)</script></p>"
                "<img src=\"https://evil.invalid/x.png\" onerror=\"x()\"><iframe src=\"https://evil.invalid\"></iframe>"
                "<style>body{background:url(https://evil.invalid/a)}</style><a href=\"https://evil.invalid\">링크</a>"
                "<a href=\"javascript:alert(1)\">j</a><a href=\"#fn1\">1</a><svg><script>x</script></svg>"
                "<table><tr><td colspan=\"2\" style=\"color:red\">칸</td></tr></table><form><input value=\"x\"></form>")


def test_MCP_WRITE_030_save_note_writes_a_new_sanitized_page_and_never_overwrites(library):
    folder = library / ALLOWED / "정리본"
    existing = {p: p.read_bytes() for p in folder.iterdir()}
    first, is_error, _ = call("save_note", {"lecture_id": "a1", "format": "html", "content": HOSTILE_HTML})
    assert not is_error, first
    page = library / first["path"]
    text = page.read_text(encoding="utf-8")
    for bad in ("<script", "onclick", "onerror", "<img", "<iframe", "evil.invalid/a", "javascript:", "<svg", "<form",
                "<input", "style=\"color", "steal()"):
        assert bad not in text, bad
    assert "default-src 'none'" in text and "ChatGPT가 씀" in text and 'href="#fn1"' in text and 'colspan="2"' in text
    assert "<h2>관수로</h2>" in text and "링크" in text
    second, _, _ = call("save_note", {"lecture_id": "a1", "content": "# 둘째\n\n- 항목 **굵게**\n\n<script>x()</script>"})
    assert second["path"] != first["path"] and page.read_text(encoding="utf-8") == text  # the first stays as it was
    text2 = (library / second["path"]).read_text(encoding="utf-8")
    # raw HTML inside Markdown is shown as text, never run
    assert "<h1>둘째</h1>" in text2 and "<strong>굵게</strong>" in text2 and "<script" not in text2
    assert "&lt;script&gt;x()&lt;/script&gt;" in text2
    assert all(p.read_bytes() == data for p, data in existing.items())  # the notes that were there are untouched
    row = rows(library, "SELECT created_by, kind, lecture_ids, inputs FROM StudyArtifact WHERE id = ?", (second["note_id"],))[0]
    assert row[:3] == ("ai_app", "note", '["a1"]') and "generated_sha256" not in json.loads(row[3])
    note, _, _ = call("get_note", {"lecture_id": "a1", "note_id": second["note_id"]})
    assert "둘째" in note["text"] and "ChatGPT가 씀" in note["text"]
    lectures, _, _ = call("list_lectures", {"course": ALLOWED})
    assert next(x for x in lectures["lectures"] if x["lecture_id"] == "a1")["has_note"]


def test_MCP_WRITE_031_a_file_already_at_the_target_name_is_never_replaced(library, monkeypatch):
    target = library / ALLOWED / "정리본" / "taken_ChatGPT.html"
    target.write_text("사용자 파일", encoding="utf-8")
    monkeypatch.setattr(writes, "_note_target", lambda c, g, lid, by=None: (target, {}))
    saved, is_error, _ = call("save_note", {"lecture_id": "a1", "content": "본문", "request_id": "n1"})
    assert not is_error and saved["path"].endswith("taken_ChatGPT_2.html")
    assert target.read_text(encoding="utf-8") == "사용자 파일"
    again, _, _ = call("save_note", {"lecture_id": "a1", "content": "본문", "request_id": "n1"})
    assert again["already"] and again["note_id"] == saved["note_id"]
    assert not (library / ALLOWED / "정리본" / "taken_ChatGPT_3.html").exists()


def test_MCP_WRITE_032_empty_huge_and_claude_labelled_notes(library):
    payload, is_error, _ = call("save_note", {"lecture_id": "a1", "format": "html", "content": "<script>only()</script>"})
    assert is_error and "비어" in payload["error"]
    reply = _server().handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "save_note", "arguments": {"lecture_id": "a1", "content": "가" * (note_page.MAX_INPUT + 1)}}})
    assert reply["error"]["code"] == server.INVALID_PARAMS
    saved, _, _ = call("save_note", {"lecture_id": "a1", "content": "클로드"}, _server("claude-ai"))
    assert "Claude가 씀" in (library / saved["path"]).read_text(encoding="utf-8")


def test_MCP_WRITE_033_markdown_is_escaped_and_links_never_leave_the_page():
    out = note_page.clean_html(note_page.markdown_html(
        "# 제목 <b>x</b>\n\n[밖](https://evil.invalid) [안](#sec1) ![그림](https://evil.invalid/i.png)\n\n"
        "| a | b |\n|---|---|\n| 1 | <i>2</i> |\n\n```\n<script>\n```\n> 인용"))
    assert "<a href=\"#sec1\">안</a>" in out and "href=\"https" not in out and "<img" not in out
    assert "&lt;b&gt;x&lt;/b&gt;" in out and "&lt;script&gt;" in out and "<table>" in out and "<blockquote>" in out


def test_MCP_WRITE_034_a_text_that_opens_with_the_title_does_not_show_it_twice():
    made = note_page.page("# 1강 정리\n\n- 핵심", "markdown", title="1강 정리", subtitle="s", written_by="ChatGPT가 씀", change_id="c")
    assert made.count("<h1>1강 정리</h1>") == 1 and "<li>핵심</li>" in made
    with pytest.raises(ValueError, match="저장할 내용이 없습니다"):
        note_page.page("# 1강 정리", "markdown", title="1강 정리", subtitle="s", written_by="w", change_id="c")
    other = note_page.page("# 다른 제목\n\n본문", "markdown", title="1강 정리", subtitle="s", written_by="w", change_id="c")
    assert "<h1>다른 제목</h1>" in other and "<h1>1강 정리</h1>" in other


# ------------------------------------------------------------------ the app: switch, change list, live refresh
def _fake_backend(root: Path):
    import threading as _threading
    from types import SimpleNamespace

    from lecturehub.ui.backend import Backend

    fake = SimpleNamespace(root=root, needs_setup=False, courses=[SimpleNamespace(folder=ALLOWED)], extras=lambda: [],
                           _lock=_threading.Lock(), _state={"library_version": 0}, timetable_path=root / "시간표.csv")
    for name in ("_ai_link_courses", "ai_link_status", "ai_link_enable", "ai_link_write", "ai_change_undo",
                 "ai_change_undo_since", "_ai_changes_check", "_timetable_check", "_bump_library"):
        setattr(fake, name, getattr(Backend, name).__get__(fake))
    return fake


def test_MCP_WRITE_045_the_app_switch_the_change_list_undo_and_live_refresh(library):
    app = _fake_backend(library)
    status = app.ai_link_status()
    assert status["write"] is True and status["changes"] == [] and [a["app"] for a in status["apps"]] == ["codex", "claude_desktop"]
    app._ai_changes_check()  # the first look only remembers
    added, _, _ = call("add_calendar_event", ADD)
    app._ai_seen_at = 0.0
    app._ai_changes_check()
    assert app._state["library_version"] == 1  # an AI app's write shows in the running app without a restart
    app._ai_seen_at = 0.0
    app._ai_changes_check()
    assert app._state["library_version"] == 1  # nothing new: no redraw
    listed = app.ai_link_status()["changes"]
    assert [(c["id"], c["undoable"]) for c in listed] == [(added["change_id"], True)] and "기말 발표" in listed[0]["summary"]
    undone = app.ai_change_undo(added["change_id"])
    assert undone["ok"] and app._state["library_version"] == 2 and undone["changes"][0]["undone"] is True
    assert app.ai_change_undo(added["change_id"])["ok"] is False  # once
    assert app.ai_change_undo("없는 것")["ok"] is False
    # the second switch: a plain switch, only with the link on
    assert app.ai_link_write(False)["write"] is False
    app.ai_link_enable(False)
    assert app.ai_link_write(True)["ok"] is False and settings.ai_link() == {"enabled": False, "write": False}


# ------------------------------------------------------------------ log and undo
def test_MCP_WRITE_040_undo_puts_each_change_back(library):
    added, _, _ = call("add_calendar_event", ADD)
    changed, _, _ = call("update_calendar_event", {"event_id": added["event_id"], "title": "바뀐 발표"})
    candidate = event_id(library, "퀴즈 — 수업 중 언급")
    resolved, _, _ = call("resolve_candidate", {"event_id": candidate, "action": "confirm"})
    status = lambda eid: rows(library, "SELECT status, title FROM CalendarEvent WHERE id = ?", (eid,))[0]  # noqa: E731
    changes.undo(library, resolved["change_id"])
    assert status(candidate)[0] == "candidate"
    changes.undo(library, changed["change_id"])
    assert status(added["event_id"]) == ("confirmed", "기말 발표")
    with pytest.raises(changes.WriteRefused, match="이미 되돌린"):
        changes.undo(library, changed["change_id"])
    changes.undo(library, added["change_id"])
    assert status(added["event_id"])[0] == "cancelled"  # never deleted
    recent = changes.recent(library)
    assert [r["undone"] for r in recent] == [True, True, True] and not any(r["undoable"] for r in recent)
    # the AI app may not touch it again after the user's undo
    payload, is_error, _ = call("update_calendar_event", {"event_id": added["event_id"], "title": "다시"})
    assert is_error


def test_MCP_WRITE_041_undo_refuses_what_changed_since(library):
    from lecturehub.index import calendar_events

    added, _, _ = call("add_calendar_event", ADD)
    with closing(db.connect(library / "_lecturehub" / "lecturehub.sqlite")) as c, c:
        calendar_events.update(c, added["event_id"], note="내 메모")
    with pytest.raises(changes.WriteRefused, match="그 뒤에 바뀐"):
        changes.undo(library, added["change_id"])
    assert rows(library, "SELECT status, note FROM CalendarEvent WHERE id = ?", (added["event_id"],)) == [("confirmed", "내 메모")]


def test_MCP_WRITE_042_undoing_a_note_unlinks_it_and_keeps_the_file_in_the_app_folder(library):
    saved, _, _ = call("save_note", {"lecture_id": "a1", "content": "# 정리"})
    page = library / saved["path"]
    others = {p: p.read_bytes() for p in (library / ALLOWED / "정리본").iterdir() if p != page}
    result = changes.undo(library, saved["change_id"])
    assert result["ok"] and not page.exists()
    kept = changes.folder(library) / changes.UNDONE_NOTES / page.name
    assert kept.is_file()
    assert rows(library, "SELECT lecture_ids, path FROM StudyArtifact WHERE id = ?", (saved["note_id"],)) == [("[]", str(kept))]
    assert all(p.read_bytes() == data for p, data in others.items())
    note, _, _ = call("get_note", {"lecture_id": "a1", "note_id": saved["note_id"]})
    assert note == {"error": access.NOT_FOUND} or note.get("note_id") != saved["note_id"]


def test_MCP_WRITE_043_a_failed_transaction_leaves_no_change_no_file_and_a_voided_line(library, monkeypatch):
    def broken(connection, gate, arguments, context):
        writes.save_note(connection, gate, arguments, context)
        context.log.append(changes.entry("save_note", ALLOWED, "x", {"kind": "note"}))
        raise RuntimeError("boom")

    monkeypatch.setitem(writes.HANDLERS, "save_note", broken)
    before = dump(library)
    files = set((library / ALLOWED / "정리본").iterdir())
    payload, is_error, _ = call("save_note", {"lecture_id": "a1", "content": "# 정리"})
    assert is_error and "아무것도 바꾸지 않았습니다" in payload["error"]
    assert dump(library) == before and set((library / ALLOWED / "정리본").iterdir()) == files
    assert changes.recent(library) == []  # logged lines were voided


# ------------------------------------------------------------------ database
def test_MCP_WRITE_050_a_reader_holding_a_snapshot_does_not_block_a_write(library):
    reader = sqlite3.connect(library / "_lecturehub" / "lecturehub.sqlite")
    reader.execute("BEGIN")
    reader.execute("SELECT count(*) FROM CalendarEvent").fetchone()
    try:
        payload, is_error, _ = call("add_calendar_event", ADD)
        assert not is_error, payload
        payload, is_error, _ = call("list_calendar", {"from_date": "2026-12-01", "to_date": "2026-12-31"})
        assert payload["count"] == 1
    finally:
        reader.close()


def test_MCP_WRITE_051_a_busy_writer_makes_the_call_give_up_quickly(library):
    other = sqlite3.connect(library / "_lecturehub" / "lecturehub.sqlite", isolation_level=None)
    other.execute("BEGIN IMMEDIATE")
    try:
        started = time.monotonic()
        payload, is_error, _ = call("add_calendar_event", ADD)
        assert is_error and payload["error"] == changes.BUSY and time.monotonic() - started < 10
    finally:
        other.execute("ROLLBACK")
        other.close()


def test_MCP_WRITE_052_writes_wait_for_a_database_of_another_version(library):
    database = library / "_lecturehub" / "lecturehub.sqlite"
    with closing(sqlite3.connect(database)) as c, c:
        c.create_function("new_ulid", 0, lambda: "x")  # used by the schema's own triggers
        c.execute("INSERT INTO schema_version (version) VALUES (9999)")
    payload, is_error, _ = call("add_calendar_event", ADD)
    assert is_error and payload["error"] == changes.SCHEMA_MISMATCH
    with closing(sqlite3.connect(database)) as c, c:
        c.create_function("new_ulid", 0, lambda: "x")
        c.execute("DELETE FROM schema_version WHERE version >= ?", (db.known_schema_version(),))
    payload, is_error, _ = call("add_calendar_event", ADD)
    assert is_error and payload["error"] == changes.SCHEMA_MISMATCH


def test_MCP_WRITE_053_concurrent_reads_and_writes_from_two_threads(library):
    errors = []

    def reads():
        for _ in range(15):
            payload, is_error, _ = call("list_calendar", {"from_date": "2026-01-01", "to_date": "2026-12-31"})
            if is_error:
                errors.append(payload)

    thread = threading.Thread(target=reads)
    thread.start()
    for n in range(8):
        payload, is_error, _ = call("add_calendar_event", {**ADD, "title": f"발표 {n}"})
        if is_error:
            errors.append(payload)
    thread.join()
    assert not errors
    assert len(rows(library, "SELECT 1 FROM CalendarEvent WHERE source_ref LIKE 'ai_app:%'")) == 8


# ------------------------------------------------------------------ the real process
def test_MCP_WRITE_060_as_a_child_process_stdout_stays_json_and_nothing_is_left_open(tmp_path):
    root = tmp_path / "학기 (합성) 26-2"
    build_library(root)
    settings.set_ai_link(True)
    settings.set_ai_link_write(True)
    for kind in settings.AI_AUTO_KINDS:  # D-080: a child process would show the real dialog; every kind automatic
        settings.set_ai_link_auto(kind, True)
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}
    messages = [init, {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "add_calendar_event", "arguments": ADD}},
                {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                 "params": {"name": "save_note", "arguments": {"lecture_id": "a1", "content": "# 한글 정리\n\n본문"}}},
                {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                 "params": {"name": "save_note", "arguments": {"lecture_id": "b1", "content": "x"}}}]
    data = "".join(json.dumps(m, ensure_ascii=False) + "\n" for m in messages).encode("utf-8")
    done = subprocess.run([sys.executable, "-m", "lecturehub.mcp"], input=data, capture_output=True, timeout=120,
                          env=_child_env(root), check=False)
    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")[-800:]
    replies = [json.loads(line) for line in done.stdout.decode("utf-8").splitlines()]
    assert [r["id"] for r in replies] == [1, 2, 3, 4, 5]
    assert WRITE_TOOLS <= {t["name"] for t in replies[1]["result"]["tools"]}
    assert not replies[2]["result"]["isError"] and not replies[3]["result"]["isError"]
    assert replies[4]["result"]["isError"] and MARKER not in done.stdout.decode("utf-8")
    note = json.loads(replies[3]["result"]["content"][0]["text"])
    assert "한글 정리" in (root / note["path"]).read_text(encoding="utf-8")
    assert not any(p.name.endswith(("-wal", "-shm")) for p in (root / "_lecturehub").iterdir())


# ------------------------------------------------------------------ integration review (one test per finding)
@pytest.mark.parametrize("content,fmt", [("<a " * 4000, "html"), ("<" * 90_000, "html"), ("<!--" * 20_000, "html"),
                                         ("<svg>" * 15_000, "html"), ("[" * 20_000 + "](", "markdown"),
                                         (" *a" * 60_000, "markdown"), ("`" * 150_000, "markdown"),
                                         ("> " * 90_000, "markdown")],
                         ids=["open-tags", "lt", "comments", "svg", "brackets", "stars", "backticks", "quotes"])
def test_MCP_WRITE_070_pathological_notes_convert_in_bounded_time(content, fmt):
    """Finding 1: the sanitizer was super-linear (html.parser on "<a " repeated, nested regexes on "[" and " *a")."""
    started = time.monotonic()
    try:
        note_page.page(content, fmt, title="t", subtitle="s", written_by="w", change_id="c")
    except ValueError:
        pass  # empty after cleaning, or the time bound: refused, never hanging
    assert time.monotonic() - started < 3


def test_MCP_WRITE_071_the_note_is_prepared_before_the_write_lock_is_taken(library, monkeypatch):
    """Finding 1: the slow part (note_pack + conversion) ran inside BEGIN IMMEDIATE and held the app's writes."""
    from lecturehub.index import note_pack

    seen = []

    def free(real):
        def probe(*args, **kwargs):
            other = sqlite3.connect(library / "_lecturehub" / "lecturehub.sqlite", timeout=0, isolation_level=None)
            try:
                other.execute("BEGIN IMMEDIATE")  # "database is locked" if this call already held the writer
                other.execute("ROLLBACK")
                seen.append(real.__name__)
            finally:
                other.close()
            return real(*args, **kwargs)
        return probe

    monkeypatch.setattr(note_page, "page", free(note_page.page))
    monkeypatch.setattr(note_pack, "pack", free(note_pack.pack))
    saved, is_error, _ = call("save_note", {"lecture_id": "a1", "content": "# 정리\n\n본문"})
    assert not is_error, saved
    assert sorted(seen) == ["pack", "page"]


def test_MCP_WRITE_072_a_request_id_counts_per_course_and_never_answers_for_another(library):
    """Finding 2: one global request key answered course B's call with course A's event (and failed once A was
    no longer allowed)."""
    first, is_error, _ = call("add_calendar_event", {**ADD, "request_id": "same"})
    assert not is_error
    store = ConsentStore(access.consent_path(library))
    store.grant(access.PROVIDER, HIDDEN, 1, "합성", by="user_app_dialog")
    other, is_error, _ = call("add_calendar_event", {**ADD, "course": HIDDEN, "request_id": "same"})
    assert not is_error and not other.get("already") and other["event_id"] != first["event_id"]
    assert other["course"] == HIDDEN
    store.revoke(access.PROVIDER, ALLOWED, 2)
    again, is_error, raw = call("add_calendar_event", {**ADD, "course": HIDDEN, "request_id": "same"})
    assert not is_error and again["already"] and again["event_id"] == other["event_id"]
    assert ALLOWED not in raw and first["event_id"] not in raw


def test_MCP_WRITE_073_a_closure_is_only_a_candidate_and_the_ai_app_cannot_confirm_it(library):
    """Finding 3: a closure takes the class off the timetable, so one an AI app adds waits for the user's yes."""
    from datetime import date

    from lecturehub.index import schedule

    closure = {"course": ALLOWED, "title": "휴강(합성)", "kind": "closure", "date": "2026-12-07"}
    added, is_error, _ = call("add_calendar_event", closure)
    assert not is_error and added["status"] == "candidate" and added["message"].startswith(writes.CLOSURE_CANDIDATE)
    assert rows(library, "SELECT status, kind FROM CalendarEvent WHERE id = ?", (added["event_id"],)) == [("candidate", "closure")]
    with closing(db.connect_readonly(library / "_lecturehub" / "lecturehub.sqlite")) as c:
        assert schedule._calendar_closures(c, date(2026, 12, 7)) == []  # the timetable is unchanged
    again, _, _ = call("add_calendar_event", closure)
    assert again["already"] and again["event_id"] == added["event_id"]
    confirm, is_error, _ = call("resolve_candidate", {"event_id": added["event_id"], "action": "confirm"})
    assert is_error and "AI 앱이 확정하지 않습니다" in confirm["error"]
    assert rows(library, "SELECT status FROM CalendarEvent WHERE id = ?", (added["event_id"],)) == [("candidate",)]
    exam, _, _ = call("add_calendar_event", ADD)
    turned, is_error, _ = call("update_calendar_event", {"event_id": exam["event_id"], "kind": "closure"})
    assert is_error and "휴강으로 바꾸지" in turned["error"]
    assert rows(library, "SELECT kind FROM CalendarEvent WHERE id = ?", (exam["event_id"],)) == [("presentation",)]
    dismissed, is_error, _ = call("resolve_candidate", {"event_id": added["event_id"], "action": "dismiss"})
    assert not is_error and dismissed["status"] == "cancelled"  # taking its own candidate back is fine


def test_MCP_WRITE_074_writes_are_capped_per_process_and_per_day(library, monkeypatch):
    """Finding 3: Codex runs the tools without asking; a planted instruction must not rewrite a semester."""
    srv = _server()
    srv.burst = writes.Burst(limit=4)
    _, is_error, _ = call("delete_calendar_event", {"event_id": "없는일정"}, srv)
    assert is_error and len(srv.burst.times) == 1  # a refused call counts too (D-083 review M1: no silent probing)
    for n in range(3):
        payload, is_error, _ = call("add_calendar_event", {**ADD, "title": f"발표 {n}"}, srv)
        assert not is_error, payload
    before = dump(library)
    payload, is_error, _ = call("add_calendar_event", {**ADD, "title": "넷째"}, srv)
    assert is_error and payload == {"error": writes.TOO_MANY} and dump(library) == before
    _, is_error, _ = call("list_courses", None, srv)
    assert not is_error  # reads go on
    clock = [0.0]
    burst = writes.Burst(limit=2, window_s=600, clock=lambda: clock[0])
    burst.note()
    burst.note()
    assert burst.full()
    clock[0] = 601.0
    assert not burst.full()  # the window moves on
    # the daily cap counts the log, so a new server process does not start from zero
    assert changes.count_since(library, writes._midnight_ms()) == 3
    monkeypatch.setattr(writes, "DAILY", 3)
    payload, is_error, _ = call("add_calendar_event", {**ADD, "title": "다섯째"})
    assert is_error and payload == {"error": writes.TOO_MANY_TODAY} and dump(library) == before


def test_MCP_WRITE_075_undo_everything_from_a_time_on_newest_first_skipping_what_changed(library):
    """Finding 3: "이 시각 이후 AI가 바꾼 것 모두 되돌리기" — one click after a bad run."""
    from lecturehub.index import calendar_events

    early, _, _ = call("add_calendar_event", {**ADD, "title": "먼저 넣은 것"})
    time.sleep(0.02)  # a later millisecond than the starting point
    start, _, _ = call("add_calendar_event", {**ADD, "title": "기점"})
    call("update_calendar_event", {"event_id": start["event_id"], "title": "기점 고침"})
    kept, _, _ = call("add_calendar_event", {**ADD, "title": "사용자가 고칠 것"})
    note, _, _ = call("save_note", {"lecture_id": "a1", "content": "# 정리"})
    with closing(db.connect(library / "_lecturehub" / "lecturehub.sqlite")) as c, c:
        calendar_events.update(c, kept["event_id"], note="내 메모")
    app = _fake_backend(library)
    result = app.ai_change_undo_since(start["change_id"])
    assert result["ok"] and result["undone"] == 3 and [s["id"] for s in result["skipped"]] == [kept["change_id"]]
    assert "1건은" in result["message"] and app._state["library_version"] == 1
    status = lambda eid: rows(library, "SELECT status, title FROM CalendarEvent WHERE id = ?", (eid,))[0]
    assert status(start["event_id"]) == ("cancelled", "기점")  # the edit went back before the add it edited
    assert status(kept["event_id"]) == ("confirmed", "사용자가 고칠 것")  # changed since: left alone
    assert status(early["event_id"]) == ("confirmed", "먼저 넣은 것")  # before that time: untouched
    assert not (library / note["path"]).exists()
    assert app.ai_change_undo_since("없는 것")["ok"] is False


def test_MCP_WRITE_076_an_ai_note_is_neither_the_style_nor_the_note_revised_and_an_edited_one_stays(library):
    """Finding 6: [정리본 만들기] took the AI page as its style/target (…_ChatGPT_ChatGPT), and undo removed a page
    the user had edited since."""
    import os

    from lecturehub.index import note_pack

    database = library / "_lecturehub" / "lecturehub.sqlite"
    with closing(db.connect_readonly(database)) as c:
        before = note_pack.pack(c, library, "a1")
    saved, _, _ = call("save_note", {"lecture_id": "a1", "content": "# 정리"})
    page = library / saved["path"]
    later = time.time() + 60
    os.utime(page, (later, later))  # the newest page of the folder
    with closing(db.connect_readonly(database)) as c:
        after = note_pack.pack(c, library, "a1")
    assert after["style_from"] == before["style_from"] and after["target"] == before["target"]
    assert str(page) not in after["attached_notes"]
    second, _, _ = call("save_note", {"lecture_id": "a1", "content": "# 둘째"})
    assert second["path"].endswith("_ChatGPT_2.html") and "ChatGPT_ChatGPT" not in second["path"]
    page.write_text(page.read_text(encoding="utf-8") + "<!-- 내가 고침 -->", encoding="utf-8")
    with pytest.raises(changes.WriteRefused, match="그 뒤에 바뀐"):
        changes.undo(library, saved["change_id"])
    assert page.is_file() and "내가 고침" in page.read_text(encoding="utf-8")
    assert rows(library, "SELECT lecture_ids, path FROM StudyArtifact WHERE id = ?", (saved["note_id"],)) == [('["a1"]', str(page))]


def test_MCP_WRITE_077_a_marker_that_cannot_be_written_does_not_turn_a_saved_change_into_an_error(library, monkeypatch):
    """Finding 7: touch() raising OSError after the commit answered "nothing changed" for a change that landed."""
    def locked(root):
        raise PermissionError("marker locked")

    monkeypatch.setattr(changes, "touch", locked)
    payload, is_error, _ = call("add_calendar_event", ADD)
    assert not is_error and payload["change_id"] and payload["status"] == "confirmed"
    assert [r["id"] for r in changes.recent(library)] == [payload["change_id"]]


def test_MCP_WRITE_078_list_calendar_does_not_offer_what_the_user_edited(library):
    """Finding 8: ai_app_can_change was true for an AI event the user had edited since (the tools then refused)."""
    from lecturehub.index import calendar_events

    added, _, _ = call("add_calendar_event", ADD)
    item = lambda: call("list_calendar", {"from_date": "2026-12-01", "to_date": "2026-12-31"})[0]["events"][0]
    assert item()["ai_app_can_change"] is True
    with closing(db.connect(library / "_lecturehub" / "lecturehub.sqlite")) as c, c:
        calendar_events.update(c, added["event_id"], title="내가 고친 발표")
    assert not item().get("ai_app_can_change")


def test_MCP_WRITE_079_a_write_with_the_switch_off_is_answered_without_opening_anything(library, monkeypatch):
    """Finding 9: the refusal went through writes.run (settings, database path) instead of being answered directly."""
    from lecturehub.mcp import tools

    listed = {t["name"] for t in _server().handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]}
    assert len(tools.DEFINITIONS) == 13 and "list_assignments" in listed and WRITE_TOOLS <= listed
    settings.set_ai_link_write(False)
    monkeypatch.setattr(writes, "run", lambda *a, **k: pytest.fail("writes.run was called"))
    monkeypatch.setattr(db, "connect_readonly", lambda *a, **k: pytest.fail("the database was opened"))
    payload, is_error, _ = call("add_calendar_event", {"not": "checked either"})
    assert is_error and payload == {"error": access.WRITE_OFF}


def test_MCP_WRITE_080_nul_unclosed_containers_and_a_void_embed():
    """Finding 10: a NUL in Markdown broke the code placeholders (IndexError); an unclosed <svg>/<head> ate the rest
    of the page; <embed> (a void element) was treated as a container."""
    assert "\x00" not in note_page.markdown_html("`a`\x00b `c`\n\n\x00 text \x000\x00")
    assert note_page.markdown_html("\x000\x00 x `y`") == "<p>0 x <code>y</code></p>"
    for text, kept in (("<p>앞</p><svg><p>본문은 남음</p>", "본문은 남음"), ("<head><p>머리 뒤 본문</p>", "머리 뒤 본문"),
                       ("<embed src=x><p>뒤 문단</p>", "뒤 문단"), ("<object><p>닫히지 않은 개체 뒤</p>", "닫히지 않은 개체 뒤")):
        out = note_page.clean_html(text)
        assert kept in out and "<svg" not in out and "<embed" not in out and "<object" not in out, text
    closed = note_page.clean_html("<svg><p>그림 안</p></svg><p>밖</p>")
    assert "그림 안" not in closed and "<p>밖</p>" in closed


def test_MCP_WRITE_081_the_author_comes_from_the_ai_apps_name(library):
    """Finding 12: every note said "ChatGPT가 씀" unless the client was Claude; the app showed the same label."""
    from lecturehub.index import lectures

    assert writes.author_for("claude-ai") == "Claude가 씀" and writes.author_for("Codex") == "ChatGPT가 씀"
    assert writes.author_for("some-other-client") == writes.author_for(None) == writes.DEFAULT_AUTHOR == "AI 앱이 씀"
    other, _, _ = call("save_note", {"lecture_id": "a1", "content": "# 다른 앱"}, _server("cursor-mcp"))
    text = (library / other["path"]).read_text(encoding="utf-8")
    assert other["path"].endswith("_AI.html") and other["written_by"] == "AI 앱이 씀"
    assert "AI 앱이 씀" in text and "ChatGPT가 씀" not in text and "Claude가 씀" not in text
    claude, _, _ = call("save_note", {"lecture_id": "a2", "content": "# 클로드"}, _server("claude-ai"))
    assert claude["path"].endswith("_Claude.html") and claude["written_by"] == "Claude가 씀"
    with closing(db.connect_readonly(library / "_lecturehub" / "lecturehub.sqlite")) as c:
        notes = lectures.lecture_detail(c, "a1")["notes"]
    assert [n["written_by"] for n in notes if n.get("by_ai_app")] == ["AI 앱이 씀"]
