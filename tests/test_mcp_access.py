"""AI app link (D-072): the one gate, no leaks, read-only. Synthetic semester only.

Course "합성과목 가" is allowed for the AI app; "비밀과목 나" is not (it only has another provider's consent and a
standing all-courses entry that must not count). A marker planted in every kind of item of the second course (and in
items without a course) must appear in no reply of any tool, whatever arguments are passed.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from lecturehub.core import db, settings
from lecturehub.mcp import access, server, tools
from lecturehub.transcription.consent import CONFIRMED, ConsentStore

ALLOWED, HIDDEN = "합성과목 가", "비밀과목 나"
MARKER = "ZQX숨김표식7731"
NOTE_HTML = "<html><head><style>.x{color:red}</style><script>alert(1)</script></head><body>" \
            "<h1>관수로 정리</h1><p>마찰손실은 <b>다르시</b> 식으로 구한다.</p><ul><li>층류 f=64/Re</li></ul></body></html>"


def build_library(root: Path) -> Path:
    """A small synthetic semester under ``root``: two courses, 미분류, transcripts, materials, notes, calendar, eTL."""
    from lecturehub.index import calendar_events

    app = root / "_lecturehub"
    app.mkdir(parents=True)
    for course in (ALLOWED, HIDDEN):
        (root / course / "강의자료").mkdir(parents=True)
        (root / course / "정리본").mkdir(parents=True)
    (root / ALLOWED / "강의자료" / "1주차 관수로.pdf").write_bytes(b"%PDF-synthetic")
    (root / HIDDEN / "정리본" / "비공개 정리.html").write_text(f"<p>{MARKER} 파일 정리본</p>", encoding="utf-8")
    (root / ALLOWED / "정리본" / "가 정리.html").write_text(NOTE_HTML, encoding="utf-8")
    database = app / "lecturehub.sqlite"
    with closing(db.connect(database)) as c:
        db.migrate(c)
        c.execute("INSERT INTO Course VALUES ('ca','CA',?,'ca','26-2')", (ALLOWED,))
        c.execute("INSERT INTO Course VALUES ('cb','CB',?,'cb','26-2')", (HIDDEN,))
        c.execute("INSERT INTO Course VALUES ('cu','CU','미분류','cu','26-2')")
        for lid, course, day in (("a1", "ca", "2026-09-07"), ("a2", "ca", "2026-09-14"), ("b1", "cb", "2026-09-08"),
                                 ("u1", "cu", "2026-09-09")):
            c.execute("INSERT INTO Lecture VALUES (?, ?, ?, 1, NULL, NULL)", (lid, course, day))
        lines = {"ta": ["오늘은 관수로 마찰손실을 봅니다.", "다르시 바이스바흐 식이 중요합니다.", "층류에서는 64 나누기 레이놀즈 수."],
                 "tb": [f"{MARKER} 시험 범위는 3장입니다.", f"비밀 내용 {MARKER} 둘째 줄"],
                 "tu": [f"미분류 녹음 {MARKER} 문장"]}
        for tid, lid in (("ta", "a1"), ("tb", "b1"), ("tu", "u1")):
            c.execute("INSERT INTO Transcript (id,lecture_id,media_id,engine,role,role_decided_by,status,source_file,sha256) "
                      "VALUES (?, ?, NULL, 'gemini_transcribe', 'timeline', 'policy', 'done', ?, ?)",
                      (tid, lid, str(app / "transcripts" / f"{tid}.txt"), tid))
            for seq, text in enumerate(lines[tid]):
                c.execute("INSERT INTO TranscriptSegment (transcript_id,seq,start_ms,end_ms,text,chunk_index) VALUES (?,?,?,?,?,0)",
                          (tid, seq, seq * 61_999 + 999, seq * 61_999 + 1999, text))
        for mid, course, path, pages in (
                ("ma", "ca", root / ALLOWED / "강의자료" / "1주차 관수로.pdf", ["관수로 마찰손실 슬라이드", "다르시 식 h_f = f L/D V²/2g"]),
                ("mb", "cb", root / HIDDEN / "강의자료" / "숨김.pdf", [f"{MARKER} 자료 첫 쪽"])):
            c.execute("INSERT INTO Material (id,course_id,path,sha256,kind,title,page_count) VALUES (?,?,?,?,?,?,?)",
                      (mid, course, str(path), mid, "pdf", f"{mid} 제목", len(pages)))
            for no, text in enumerate(pages, 1):
                c.execute("INSERT INTO MaterialPage (material_id,page_no,text,terms) VALUES (?,?,?,'[]')", (mid, no, text))
        c.execute("INSERT INTO MaterialMatch (lecture_id,material_id,page_from,page_to,confidence_class,decided_by) "
                  "VALUES ('a1','ma',1,2,'high','user')")
        c.execute("INSERT INTO MaterialMatch (lecture_id,material_id,page_from,page_to,confidence_class,decided_by) "
                  "VALUES ('b1','mb',1,1,'high','user')")
        for aid, lid, path, kind in (("na", "a1", "db:lecture_note/a1/v1", "lecture_note"),
                                     ("nb", "b1", "db:lecture_note/b1/v1", "lecture_note"),
                                     ("fa", "a2", str(root / ALLOWED / "정리본" / "가 정리.html"), "note"),
                                     ("fb", "b1", str(root / HIDDEN / "정리본" / "비공개 정리.html"), "note")):
            c.execute("INSERT INTO StudyArtifact (id,lecture_ids,path,kind,version,parent_version,build_script_path,mode,"
                      "inputs,created_by,qa_status) VALUES (?,?,?,?,1,NULL,NULL,'new','{}','test','passed')",
                      (aid, json.dumps([lid]), path, kind))
        for bid, aid, seq, kind, text in (("x1", "na", 0, "heading", "관수로 마찰"), ("x2", "na", 1, "paragraph", "손실수두는 다르시 식."),
                                          ("x3", "nb", 0, "paragraph", f"{MARKER} 정리본 문단")):
            c.execute("INSERT INTO NoteBlock (id,artifact_id,block_key,seq,kind,text,authored_by,created_at) "
                      "VALUES (?,?,?,?,?,?,'ai',0)", (bid, aid, bid, seq, kind, text))
        calendar_events.add(c, kind="exam", title="중간고사(합성)", day="2026-10-20", course_id="ca", start="09:00")
        calendar_events.add(c, kind="exam", title=f"{MARKER} 시험", day="2026-10-21", course_id="cb")
        calendar_events.add(c, kind="personal", title=f"{MARKER} 개인 일정", day="2026-10-22")
        for course, title in (("ca", "퀴즈 — 수업 중 언급"), ("cb", f"{MARKER} 후보")):
            event = calendar_events.add(c, kind="quiz", title=title, day="2026-10-23", course_id=course,
                                        source="transcript", status="candidate", source_ref=f"ref-{course}")
            quote = "다음 주 수요일 퀴즈" if course == "ca" else f"{MARKER} 인용"
            lecture = "a1" if course == "ca" else "b1"
            c.execute("INSERT INTO Evidence(id,subject_type,subject_id,kind,detail,source,created_at) VALUES (?,?,?,?,?,?,?)",
                      (f"ev-{course}", "CalendarEvent", event, "transcript_quote",
                       json.dumps({"lecture_id": lecture, "at_ms": 62_998, "quote": quote}, ensure_ascii=False), "engine", 1))
        c.commit()
    etl = app / "etl"
    etl.mkdir()
    (etl / "announcements.json").write_text(json.dumps([
        {"id": "etl:1", "course": ALLOWED, "title": "과제 안내(합성)", "posted": "2026-09-10T09:00", "text": "보고서는 금요일까지",
         "url": "https://example.invalid/a/1", "attachments": ["안내.pdf"]},
        {"id": "etl:2", "course": HIDDEN, "title": f"{MARKER} 공지", "posted": "2026-09-11T09:00", "text": MARKER},
        {"id": "etl:3", "course": "", "title": f"{MARKER} 과목 없음", "posted": "2026-09-12T09:00", "text": MARKER},
    ], ensure_ascii=False), encoding="utf-8")
    (etl / "files.json").write_text(json.dumps({"at": 1_789_000_000, "files": []}), encoding="utf-8")
    (etl / "assignments.json").write_text(json.dumps([  # D-077: as etl_sync writes it (no score or grade is ever kept)
        # score/grade fields as if a file carried them anyway: list_assignments passes only its whitelist (review 9)
        {"id": "etl:31", "course": ALLOWED, "title": "보고서 1(합성)", "due": "2026-10-02T23:59", "url": "https://example.invalid/as/31",
         "points_possible": 100, "score": 87.25, "grade": "GRADE-MARKER-Q",
         "submission": {"workflow_state": "submitted", "submitted_at": "2026-10-02T21:10", "missing": False, "late": False,
                        "excused": False, "score": 87.25, "grade": "GRADE-MARKER-Q", "entered_grade": "GRADE-MARKER-Q"}},
        {"id": "etl:32", "course": ALLOWED, "title": "보고서 2(합성)", "due": "2026-10-09T23:59",
         "submission": {"workflow_state": "unsubmitted", "missing": True, "late": False, "excused": False}},
        {"id": "etl:33", "course": HIDDEN, "title": f"{MARKER} 과제", "due": "2026-10-03T23:59", "url": f"https://x.invalid/{MARKER}"},
        {"id": "etl:34", "course": "", "title": f"{MARKER} 과목 없는 과제", "due": "2026-10-04T23:59"},
    ], ensure_ascii=False), encoding="utf-8")
    (etl / "sync.json").write_text(json.dumps({"ok_at": 1_789_000_000, "count": 4}), encoding="utf-8")
    consent = {"ai_app_link": {ALLOWED: {"granted_at": 1, "granted_by": "user_app_dialog", "notice": "n", "authority": CONFIRMED},
                               "*": {"granted_at": 1, "granted_by": "user", "notice": "n", "authority": CONFIRMED},
                               "미분류": {"granted_at": 1, "granted_by": "user", "notice": "n", "authority": CONFIRMED}},
               "gemini_transcribe": {HIDDEN: {"granted_at": 1, "granted_by": "user", "notice": "n", "authority": CONFIRMED}}}
    (app / "consent.json").write_text(json.dumps(consent, ensure_ascii=False), encoding="utf-8")
    return database


def seed_inspection_sources(root: Path) -> None:
    # D-082: hidden-course markers in every new inspection source, not just transcript/material text.
    app = root / "_lecturehub"
    database = app / "lecturehub.sqlite"
    with closing(db.connect(database)) as c:
        c.execute("INSERT INTO Schedule(course_id,weekday,start,end) VALUES ('ca',0,32400000,36000000)")
        c.execute("INSERT INTO Schedule(course_id,weekday,start,end) VALUES ('cb',1,32400000,36000000)")
        c.execute("INSERT INTO ScheduleException(course_id,date,kind,note) VALUES ('cb','2026-09-09','보강',?)",
                  (f"10:00-11:00 {MARKER}",))
        c.commit()
    notices = app / "notices"
    notices.mkdir()
    (notices / "status.json").write_text(json.dumps({"engine": "codex", "at": 123456,
        "unavailable": f"{MARKER} C:/private/account", "needs_consent": [HIDDEN], "recording": False}), encoding="utf-8")
    (notices / "state.json").write_text(json.dumps({"b1": {"read_at": 123456, "model": MARKER}}), encoding="utf-8")
    (notices / "inbox.json").write_text(json.dumps([{"key": MARKER, "course": HIDDEN, "course_id": "cb",
        "lecture_id": "b1", "summary": MARKER, "quote": MARKER}]), encoding="utf-8")
    ai_link = app / "ai_link"
    ai_link.mkdir()
    (ai_link / "changes.jsonl").write_text(json.dumps({"id": "hidden-change", "course": HIDDEN,
        "tool": "save_note", "summary": MARKER, "at_ms": 123456, "undo": {"path": f"C:/{MARKER}"}}) + "\n", encoding="utf-8")


@pytest.fixture
def library(tmp_path, monkeypatch):
    root = tmp_path / "학기 (합성) 26-2"
    build_library(root)
    seed_inspection_sources(root)
    monkeypatch.setenv("LECTUREHUB_ROOT", str(root))
    monkeypatch.setenv("LECTUREHUB_EDITION", "full")  # the standing "*" entry would count for other providers here
    settings.set_ai_link(True)
    settings.save({"reminders": {"muted_courses": [HIDDEN]}})
    return root


def call(name: str, arguments: dict | None = None) -> tuple[dict, bool, str]:
    """One tool call through the real server path; returns (payload, isError, raw reply text)."""
    srv = server.Server()
    srv.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
    reply = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": name, "arguments": arguments or {}}})
    raw = json.dumps(reply, ensure_ascii=False)
    assert "result" in reply, raw
    text = reply["result"]["content"][0]["text"]
    return json.loads(text), reply["result"]["isError"], raw


# every tool with many argument shapes, including the ids and the name of the course that is not allowed
MATRIX = [
    ("list_courses", {}),
    ("list_lectures", {}), ("list_lectures", {"course": ALLOWED}), ("list_lectures", {"course": HIDDEN}),
    ("list_lectures", {"course": "미분류"}), ("list_lectures", {"course": "비밀과목"}), ("list_lectures", {"offset": 1}),
    ("list_lectures", {"from_date": "2026-09-01", "to_date": "2026-09-30"}),
    ("search", {"query": MARKER}), ("search", {"query": "비밀"}), ("search", {"query": "시험"}), ("search", {"query": "관수로"}),
    ("search", {"query": "자료"}), ("search", {"query": "3장"}), ("search", {"query": "관수로", "course": HIDDEN}),
    ("search", {"query": MARKER, "course": "미분류"}), ("search", {"query": "문장"}), ("search", {"query": "정리본"}),
    ("search", {"query": "줄", "limit": 50}), ("search", {"query": "수", "offset": 1}),
    ("get_transcript", {"lecture_id": "a1"}), ("get_transcript", {"lecture_id": "b1"}),
    ("get_transcript", {"lecture_id": "u1"}), ("get_transcript", {"lecture_id": "b1", "engine": "Google"}),
    ("get_transcript", {"lecture_id": "nope"}), ("get_transcript", {"lecture_id": "tb"}),
    ("get_note", {"lecture_id": "a1"}), ("get_note", {"lecture_id": "a2"}), ("get_note", {"lecture_id": "b1"}),
    ("get_note", {"lecture_id": "a1", "note_id": "nb"}), ("get_note", {"lecture_id": "a1", "note_id": "fb"}),
    ("get_note", {"lecture_id": "u1"}),
    ("get_material", {"material_id": "ma"}), ("get_material", {"material_id": "mb"}),
    ("get_material", {"material_id": "mb", "page_from": 1, "page_to": 1}), ("get_material", {"material_id": "zz"}),
    ("list_calendar", {}), ("list_calendar", {"from_date": "2026-01-01", "to_date": "2027-12-31"}),
    ("list_calendar", {"course": HIDDEN, "from_date": "2026-01-01", "to_date": "2027-12-31"}),
    ("list_calendar", {"include_candidates": True, "from_date": "2026-10-01", "to_date": "2026-10-31"}),
    ("list_announcements", {}), ("list_announcements", {"course": HIDDEN}), ("list_announcements", {"limit": 50}),
    ("list_announcements", {"course": ""}),
    ("list_assignments", {}), ("list_assignments", {"course": HIDDEN}), ("list_assignments", {"course": ALLOWED}),
    ("list_assignments", {"course": ""}), ("list_assignments", {"include_submitted": False}),
    ("list_assignments", {"course": "미분류"}), ("list_assignments", {"offset": 1}),
    ("get_app_status", {}), ("get_app_status", {"offset": 1}), ("get_app_status", {"offset": 100}),
    ("get_timetable", {}), ("get_timetable", {"week_of": "2026-09-08"}),
    ("get_timetable", {"week_of": "2026-09-08", "course": HIDDEN}),
    ("get_timetable", {"course": ALLOWED, "offset": 1}), ("get_timetable", {"course": "미분류"}),
    ("get_lecture_status", {"lecture_id": "a1"}), ("get_lecture_status", {"lecture_id": "b1"}),
    ("get_lecture_status", {"lecture_id": "b1", "offset": 1}), ("get_lecture_status", {"lecture_id": "u1"}),
    ("get_lecture_status", {"lecture_id": "none"}),
    ("list_ai_changes", {}), ("list_ai_changes", {"course": HIDDEN}),
    ("list_ai_changes", {"course": ALLOWED, "offset": 1}), ("list_ai_changes", {"limit": 100}),
]
DRIVE = re.compile(r"(?<![A-Za-z])[A-Za-z]:[\\/]")


def test_MCP_ACCESS_001_nothing_of_a_course_that_is_not_allowed_leaks_through_any_tool(library):
    for name, arguments in MATRIX:
        _, _, raw = call(name, arguments)
        for secret in (MARKER, HIDDEN, "비밀과목", "숨김.pdf", "b1", "tb", "mb", "cb", "nb", "fb"):
            assert secret not in raw, (name, arguments, secret, raw[:400])


def test_MCP_ACCESS_002_not_allowed_answers_exactly_like_not_there(library):
    pairs = [("get_transcript", {"lecture_id": "b1"}, {"lecture_id": "does-not-exist"}),
             ("get_transcript", {"lecture_id": "u1"}, {"lecture_id": "does-not-exist"}),
             ("get_note", {"lecture_id": "b1"}, {"lecture_id": "does-not-exist"}),
             ("get_material", {"material_id": "mb"}, {"material_id": "does-not-exist"}),
             ("list_lectures", {"course": HIDDEN}, {"course": "없는 과목"}),
             ("list_calendar", {"course": HIDDEN}, {"course": "없는 과목"}),
             ("list_announcements", {"course": HIDDEN}, {"course": "없는 과목"}),
             ("list_assignments", {"course": HIDDEN}, {"course": "없는 과목"}),
             ("get_timetable", {"course": HIDDEN}, {"course": "없는 과목"}),
             ("get_lecture_status", {"lecture_id": "b1"}, {"lecture_id": "does-not-exist"}),
             ("list_ai_changes", {"course": HIDDEN}, {"course": "없는 과목"}),
             ("search", {"query": "관수로", "course": HIDDEN}, {"query": "관수로", "course": "없는 과목"}),
             ("get_note", {"lecture_id": "a1", "note_id": "nb"}, {"lecture_id": "a1", "note_id": "none"})]
    for name, hidden_args, missing_args in pairs:
        hidden, hidden_error, _ = call(name, hidden_args)
        missing, missing_error, _ = call(name, missing_args)
        assert hidden_error and missing_error and hidden == missing == {"error": access.NOT_FOUND}, name


def test_MCP_ACCESS_003_allowed_course_is_shown_with_its_sources(library):
    courses, error, _ = call("list_courses")
    assert not error and [c["course"] for c in courses["courses"]] == [ALLOWED] and courses["courses"][0]["lectures"] == 2
    lectures, _, _ = call("list_lectures", {"course": ALLOWED})
    assert [x["lecture_id"] for x in lectures["lectures"]] == ["a2", "a1"]
    first = lectures["lectures"][1]
    assert first["label"] == "1강" and first["transcript"]["engine"] == "Google" and first["has_note"]
    assert first["materials"][0]["material_id"] == "ma"
    assert lectures["course_materials"][0]["path"] == f"{ALLOWED}/강의자료/1주차 관수로.pdf"
    found, _, _ = call("search", {"query": "다르시"})
    kinds = {r["kind"] for r in found["results"]}
    assert kinds == {"transcript", "material"} and "notice" not in found  # said once, in the tool description
    assert "자동 음성 인식" in tools.BY_NAME["search"]["description"]
    hit = next(r for r in found["results"] if r["kind"] == "transcript")
    # integer ms, floored clock (62998 ms -> 01:02, never rounded up to 01:03)
    assert hit["lecture_id"] == "a1" and hit["start_ms"] == 62_998 and hit["clock"] == "01:02" and hit["engine"] == "Google"
    assert hit["before"].startswith("오늘은") and hit["after"].startswith("층류")
    page = next(r for r in found["results"] if r["kind"] == "material")
    assert page["page"] == 2 and page["material_id"] == "ma" and page["lectures"] == [{"lecture_id": "a1", "date": "2026-09-07"}]
    transcript, _, _ = call("get_transcript", {"lecture_id": "a1"})
    assert transcript["text"].splitlines() == ["[00:00] 오늘은 관수로 마찰손실을 봅니다.", "[01:02] 다르시 바이스바흐 식이 중요합니다.",
                                               "[02:04] 층류에서는 64 나누기 레이놀즈 수."]
    assert transcript["engine"] == "Google" and transcript["from_ms"] == 999 and transcript["count"] == 3
    assert transcript["label"] == "1강" and transcript["in_use"] is True and not transcript["truncated"]
    material, _, _ = call("get_material", {"material_id": "ma", "page_from": 2})
    assert [p["page"] for p in material["pages"]] == [2] and material["path"].startswith(ALLOWED + "/")


def test_MCP_ACCESS_004_items_without_a_course_are_never_returned(library):
    calendar, _, _ = call("list_calendar", {"from_date": "2026-01-01", "to_date": "2027-12-31"})
    assert {e["course"] for e in calendar["events"]} == {ALLOWED}
    assert {e["status"] for e in calendar["events"]} == {"confirmed", "candidate"}
    candidate = next(e for e in calendar["events"] if e["status"] == "candidate")
    assert candidate["evidence"] == {"quote": "다음 주 수요일 퀴즈", "lecture_id": "a1", "lecture_date": "2026-09-07",
                                     "start_ms": 62_998, "clock": "01:02"}
    confirmed = next(e for e in calendar["events"] if e["status"] == "confirmed")
    assert confirmed["start"] == "09:00" and confirmed["source"] == "사용자 입력"
    notices, _, _ = call("list_announcements")
    assert [a["announcement_id"] for a in notices["announcements"]] == ["etl:1"] and notices["synced_at"]
    lectures, _, _ = call("list_lectures")
    assert {x["course"] for x in lectures["lectures"]} == {ALLOWED}


def test_MCP_ASSIGN_001_list_assignments_shows_the_allowed_course_with_state_and_no_grade(library):
    """D-077: title, due, my submission state and the link; a course that is not allowed behaves as missing."""
    found, error, raw = call("list_assignments")
    assert not error and found["total"] == 2 and found["synced_at"]
    assert [(a["assignment_id"], a["state"]) for a in found["assignments"]] == [("etl:31", "submitted"), ("etl:32", "missing")]
    first = found["assignments"][0]
    assert first == {"assignment_id": "etl:31", "course": ALLOWED, "title": "보고서 1(합성)", "due": "2026-10-02T23:59",
                     "state": "submitted", "submitted_at": "2026-10-02T21:10", "url": "https://example.invalid/as/31"}
    for word in ("score", "grade", "points", "GRADE-MARKER", "87.25"):
        assert word not in raw
    still, _, _ = call("list_assignments", {"include_submitted": False})
    assert [a["assignment_id"] for a in still["assignments"]] == ["etl:32"]
    assert "list_assignments" in tools.BY_NAME and tools.BY_NAME["list_assignments"]["annotations"]["readOnlyHint"]
    (library / "_lecturehub" / "etl" / "assignments.json").unlink()
    empty, error, _ = call("list_assignments")
    assert not error and empty["total"] == 0 and "eTL" in empty["message"]


def test_MCP_ASSIGN_002_graded_without_a_hand_in_and_an_item_without_a_title(library):
    """Integration review (etl-submit 1 and 6): graded with no submission time said "submitted", and a stored item
    without a title or due date failed the whole call."""
    (library / "_lecturehub" / "etl" / "assignments.json").write_text(json.dumps([
        {"id": "etl:41", "course": ALLOWED, "due": "2026-10-20T23:59", "submission": {"workflow_state": "graded"}},
        {"id": "etl:42", "course": ALLOWED, "title": None,
         "submission": {"workflow_state": "graded", "submitted_at": "2026-10-01T10:00"}},
        {"id": "etl:43", "course": ALLOWED, "title": "누락(합성)", "due": "2026-10-21T23:59",
         "submission": {"workflow_state": "graded", "missing": True}},
    ], ensure_ascii=False), encoding="utf-8")
    found, error, _ = call("list_assignments")
    assert not error and found["total"] == 3
    assert {a["assignment_id"]: a["state"] for a in found["assignments"]} == {
        "etl:41": "graded_no_submission", "etl:42": "submitted", "etl:43": "missing"}
    still, _, _ = call("list_assignments", {"include_submitted": False})
    assert {a["assignment_id"] for a in still["assignments"]} == {"etl:41", "etl:43"}  # not folded away


def test_MCP_ACCESS_005_master_switch_off_refuses_every_tool_but_lists_them(library):
    settings.set_ai_link(False)
    for name, arguments in MATRIX:
        payload, error, _ = call(name, arguments)
        assert error and payload == {"error": access.OFF}, name
    srv = server.Server()
    srv.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
    assert len(srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]) == 13


def test_MCP_ACCESS_006_revoking_a_course_applies_to_the_very_next_call(library):
    assert call("get_transcript", {"lecture_id": "a1"})[1] is False
    assert ConsentStore(library / "_lecturehub" / "consent.json").revoke(access.PROVIDER, ALLOWED, 2)
    payload, error, _ = call("get_transcript", {"lecture_id": "a1"})
    assert error and payload == {"error": access.NOT_FOUND}
    assert call("list_courses")[0]["courses"] == []
    # D-082: all new tool argument shapes are re-gated after revocation, including aggregate counts.
    for name, arguments in MATRIX:
        if name not in ("get_app_status", "get_timetable", "get_lecture_status", "list_ai_changes"):
            continue
        _, _, raw = call(name, arguments)
        for secret in (ALLOWED, HIDDEN, MARKER, "a1", "b1"):
            assert secret not in raw, (name, arguments, raw)
    ConsentStore(library / "_lecturehub" / "consent.json").grant(access.PROVIDER, HIDDEN, 3, "n")
    assert [c["course"] for c in call("list_courses")[0]["courses"]] == [HIDDEN]


def test_MCP_ACCESS_007_no_standing_consent_for_this_provider_even_in_the_full_edition(library):
    store = ConsentStore(library / "_lecturehub" / "consent.json")
    assert store.granted(access.PROVIDER, ALLOWED) and not store.granted(access.PROVIDER, HIDDEN)
    assert not store.granted(access.PROVIDER, "*") and not store.granted(access.PROVIDER, "미분류")
    assert store.granted("gemini_transcribe", HIDDEN)
    store.grant_all("whisper_groq", 1, "user", "합성")  # other providers keep D-062 in the full edition
    assert store.granted("whisper_groq", "아무 과목")
    with pytest.raises(ValueError):
        store.grant_all(access.PROVIDER, 1, "user", "합성")
    assert not any(r["provider"] == access.PROVIDER and r["all_courses"] for r in store.listing())
    with pytest.raises(ValueError):
        store.grant(access.PROVIDER, "미분류", 1, "n")


def test_MCP_ACCESS_008_replies_hold_only_paths_relative_to_the_data_folder(library):
    for name, arguments in MATRIX:
        _, _, raw = call(name, arguments)
        assert not DRIVE.search(raw), (name, arguments, raw[:300])
        assert "Users" not in raw and str(library.parent) not in raw


def _tree(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def test_MCP_ACCESS_009_read_only_nothing_under_the_root_changes_and_no_wal_appears(library):
    before = _tree(library)
    assert not any(name.endswith(("-wal", "-shm")) for name in before)
    for name, arguments in MATRIX:
        call(name, arguments)
    assert _tree(library) == before  # same files, same bytes: no -wal/-shm, no .corrupt copies, no logs here


def test_MCP_ACCESS_010_with_the_app_holding_the_database_open_the_reader_sees_wal_rows_and_adds_no_file(library):
    database = library / "_lecturehub" / "lecturehub.sqlite"
    with closing(db.connect(database)) as app_connection:  # the app (or the recorder's library) has it open
        app_connection.execute("INSERT INTO Lecture VALUES ('a3', 'ca', '2026-09-21', 1, NULL, NULL)")
        app_connection.commit()  # only in the WAL so far
        names = sorted(p.name for p in database.parent.iterdir())
        assert database.name + "-wal" in names and database.name + "-shm" in names
        main_and_wal = (hashlib.sha256(database.read_bytes()).hexdigest(),
                        hashlib.sha256(Path(f"{database}-wal").read_bytes()).hexdigest())
        lectures, _, _ = call("list_lectures", {"course": ALLOWED})
        assert lectures["lectures"][0]["lecture_id"] == "a3"  # read through the WAL
        assert sorted(p.name for p in database.parent.iterdir()) == names
        assert (hashlib.sha256(database.read_bytes()).hexdigest(),
                hashlib.sha256(Path(f"{database}-wal").read_bytes()).hexdigest()) == main_and_wal
        app_connection.execute("INSERT INTO Lecture VALUES ('a4', 'ca', '2026-09-28', 1, NULL, NULL)")
        app_connection.commit()  # a reader never blocks the app's writes
    assert not Path(f"{database}-wal").exists()  # the app's own close still cleans up


def test_DB_READONLY_connect_readonly_refuses_writes_and_creates_nothing(tmp_path):
    database = tmp_path / "데이터 (합성)" / "lecturehub.sqlite"
    with pytest.raises(FileNotFoundError):
        db.connect_readonly(database)
    assert not database.parent.exists()
    db.init_db(database)
    assert sorted(p.name for p in database.parent.iterdir()) == ["lecturehub.sqlite"]
    with closing(db.connect_readonly(database)) as reader:
        assert reader.execute("SELECT count(*) FROM Course").fetchone()[0] == 0
        assert isinstance(reader.execute("SELECT 1 AS one").fetchone(), sqlite3.Row)
        with pytest.raises(sqlite3.OperationalError):
            reader.execute("INSERT INTO Course VALUES ('x','X','x','x','26-2')")
        reader.execute("PRAGMA journal_mode").fetchall()
        with pytest.raises(sqlite3.OperationalError):
            reader.execute("CREATE TABLE t(x)")
    assert sorted(p.name for p in database.parent.iterdir()) == ["lecturehub.sqlite"]


def test_DB_READONLY_schema_too_new_is_refused_before_reading(tmp_path):
    database = tmp_path / "lecturehub.sqlite"
    db.init_db(database)
    with closing(sqlite3.connect(database)) as c:
        c.create_function("new_ulid", 0, lambda: "01NEWERAPPWROTETHISROW0000")  # as a newer app would
        c.execute("INSERT INTO schema_version(version) VALUES (9999)")
        c.commit()
    with pytest.raises(db.SchemaTooNewError):
        db.connect_readonly(database)


def test_MCP_ACCESS_011_paging_says_where_to_read_on(library, monkeypatch):
    monkeypatch.setattr(tools, "TEXT_CAP", 20)
    first, _, _ = call("get_transcript", {"lecture_id": "a1"})
    assert first["truncated"] and first["next_seq"] == 1 and first["next_from_ms"] == 62_998 and first["count"] == 1
    rest, _, _ = call("get_transcript", {"lecture_id": "a1", "from_seq": first["next_seq"]})
    assert rest["from_ms"] == 62_998 and rest["text"].startswith("[01:02] ")
    by_time, _, _ = call("get_transcript", {"lecture_id": "a1", "from_ms": first["next_from_ms"]})
    assert by_time["text"] == rest["text"]
    monkeypatch.setattr(tools, "MIN_ROOM", 20)  # the whole reply is far over 20 characters: the text gets MIN_ROOM
    note, _, _ = call("get_note", {"lecture_id": "a2"})
    # 20 characters as written in JSON: the line break inside counts two ("\n"), so 19 characters of text fit
    assert note["truncated"] and note["next_offset"] == len(note["text"]) == 19 and note["kind"] == "정리본 파일"
    more, _, _ = call("get_note", {"lecture_id": "a2", "offset": note["next_offset"]})
    assert more["offset"] == 19 and more["text"]
    walked, args = "", {"lecture_id": "a2"}
    for _ in range(20):
        piece, _, _ = call("get_note", args)
        walked += piece["text"]
        if not piece["truncated"]:
            break
        args = {"lecture_id": "a2", "offset": piece["next_offset"]}
    assert walked == tools.html_text(NOTE_HTML)  # every character exactly once
    assert "alert" not in note["text"] + more["text"] and "color" not in note["text"] + more["text"]
    monkeypatch.setattr(tools, "MIN_ROOM", 1_000)  # room for one whole short page
    pages, _, _ = call("get_material", {"material_id": "ma"})
    assert pages["truncated"] and pages["next_page"] == 2 and "next_page_offset" not in pages


def test_MCP_ACCESS_012_note_text_without_tags_and_app_note_blocks(library):
    file_note, _, _ = call("get_note", {"lecture_id": "a2"})
    assert file_note["text"].splitlines()[:2] == ["관수로 정리", "마찰손실은 다르시 식으로 구한다."]
    assert file_note["path"] == f"{ALLOWED}/정리본/가 정리.html"
    app_note, _, _ = call("get_note", {"lecture_id": "a1"})
    assert app_note["kind"] == "앱 정리본" and app_note["text"] == "## 관수로 마찰\n\n손실수두는 다르시 식."


def test_MCP_ACCESS_013_a_note_file_outside_the_data_folder_is_never_read(library, tmp_path):
    outside = tmp_path / "밖" / "남의 파일.html"
    outside.parent.mkdir()
    outside.write_text(f"<p>{MARKER}</p>", encoding="utf-8")
    with closing(sqlite3.connect(library / "_lecturehub" / "lecturehub.sqlite")) as c:
        c.execute("UPDATE StudyArtifact SET path = ? WHERE id = 'fa'", (str(outside),))
        c.commit()
    payload, error, raw = call("get_note", {"lecture_id": "a2"})
    assert not error and payload["count"] == 0 and MARKER not in raw


def test_MCP_ACCESS_014_a_broken_consent_file_means_no_course_and_nothing_is_written(library):
    path = library / "_lecturehub" / "consent.json"
    path.write_text("{broken", encoding="utf-8")
    before = _tree(library)
    assert call("list_courses")[0]["courses"] == []
    assert _tree(library) == before  # no .corrupt copy beside it (transcription.store.read_json would leave one)


# ---------- the app window: consent only through the native dialog's token (security review pattern, SEC-005)
class _Window:
    def __init__(self, answer):
        self.answer, self.asked = answer, []

    def create_confirmation_dialog(self, title, message):
        self.asked.append((title, message))
        return self.answer


class _Backend:
    def __init__(self):
        self.allowed = []

    def ai_link_allow(self, course):
        self.allowed.append(course)
        return {"ok": True}


class _Library:
    def __init__(self, root):
        self.root = root


def test_SEC_009_ai_link_allow_needs_the_native_dialogs_token(tmp_path):
    from lecturehub.ui.app import CONSENT_TITLES, Api

    backend = _Backend()
    api = Api(backend, _Library(tmp_path))
    assert api.ai_link_allow(ALLOWED)["needs_confirm"] and backend.allowed == []  # straight from page JS
    assert api.ai_link_allow(ALLOWED, "made-up")["ok"] is False and backend.allowed == []
    api._main = _Window(True)
    other = api.consent_confirm("gemini_transcribe", ALLOWED)["token"]
    assert api.ai_link_allow(ALLOWED, other)["ok"] is False  # another provider's token
    q = api.consent_confirm(access.PROVIDER, HIDDEN)
    assert api.ai_link_allow(ALLOWED, q["token"])["ok"] is False  # another course's token
    q = api.consent_confirm(access.PROVIDER, ALLOWED)
    assert "녹음 파일(소리)은 건네지 않습니다" in api._main.asked[-1][1] and CONSENT_TITLES[access.PROVIDER] in api._main.asked[-1][0]
    assert api.ai_link_allow(ALLOWED, q["token"]) == {"ok": True} and backend.allowed == [ALLOWED]
    assert api.ai_link_allow(ALLOWED, q["token"])["ok"] is False  # one time only
    api._main = _Window(False)
    assert api.consent_confirm(access.PROVIDER, ALLOWED) == {"ok": True, "yes": False}


def test_SEC_009_backend_status_allow_and_revoke(library, monkeypatch):
    from types import SimpleNamespace

    from lecturehub.ui.backend import Backend

    fake = SimpleNamespace(root=library, needs_setup=False, courses=[SimpleNamespace(folder=ALLOWED),
                                                                      SimpleNamespace(folder=HIDDEN)],
                           extras=lambda: ["미분류"])
    for name in ("_ai_link_courses", "ai_link_status", "ai_link_enable", "ai_link_allow", "consent_list",
                 "consent_revoke"):
        setattr(fake, name, getattr(Backend, name).__get__(fake))
    fake.CONSENT_LABELS = Backend.CONSENT_LABELS
    status = fake.ai_link_status()
    assert status["enabled"] and status["courses"] == [{"course": ALLOWED, "allowed": True}, {"course": HIDDEN, "allowed": False}]
    assert status["args"] == ["-m", "lecturehub.mcp"] and '"mcpServers"' in status["claude_desktop"]
    assert status["codex"].startswith("[mcp_servers.lecturehub]") and status["claude_code"].startswith("claude mcp add")
    assert fake.ai_link_allow("미분류")["ok"] is False and fake.ai_link_allow("없는 과목")["ok"] is False
    assert fake.ai_link_allow(HIDDEN)["courses"][1] == {"course": HIDDEN, "allowed": True}
    labels = {r["provider"]: r["label"] for r in fake.consent_list()["rows"]}
    assert labels[access.PROVIDER] == Backend.CONSENT_LABELS[access.PROVIDER]
    assert fake.consent_revoke(access.PROVIDER, HIDDEN)["ok"]
    assert not fake.ai_link_status()["courses"][1]["allowed"]
    assert fake.ai_link_enable(False)["enabled"] is False and settings.ai_link()["enabled"] is False


def test_MCP_CONFIG_snippets_use_the_worker_command_and_parse():
    import tomllib

    from lecturehub.mcp import config

    found = config.snippets(r"C:\Program Files\Lecture Hub (합성)\LectureHubWorker.exe")
    desktop = json.loads(found["claude_desktop"])["mcpServers"]["lecturehub"]
    assert desktop == {"command": found["command"], "args": ["-m", "lecturehub.mcp"]}
    assert tomllib.loads(found["codex"])["mcp_servers"]["lecturehub"] == desktop
    assert found["claude_code"] == ('claude mcp add --scope user lecturehub -- '
                                    '"C:\\Program Files\\Lecture Hub (합성)\\LectureHubWorker.exe" -m lecturehub.mcp')
