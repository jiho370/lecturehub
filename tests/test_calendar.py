"""Calendar: user entries, eTL deadlines (import only), dates spoken in class stay candidates (D-052)."""
import json
import sqlite3
from contextlib import closing
from datetime import date

import pytest

from lecturehub.core import db
from lecturehub.index import calendar_events as cal


@pytest.fixture
def library(tmp_path):
    with closing(db.connect(tmp_path / "lecturehub.sqlite")) as connection:
        db.migrate(connection)
        connection.execute("INSERT INTO Course VALUES ('gj','GJ','도시계','gj','26-2')")
        connection.execute("INSERT INTO Course VALUES ('ac','AC','경제학원론','ac','26-2')")
        connection.commit()
        yield connection


def test_user_event_is_edited_and_cancelled_never_deleted(library):
    event_id = cal.add(library, kind="exam", title="중간고사", day="2026-10-20", course_id="gj", start="10:00",
                       note="범위: 1~7장(합성)")
    cal.update(library, event_id, day="2026-10-21", note="범위: 1~6장(합성)")
    cal.set_status(library, event_id, "cancelled", "합성")
    library.commit()
    row = library.execute("SELECT date, note, status, start_ms FROM CalendarEvent").fetchone()
    assert tuple(row) == ("2026-10-21", "범위: 1~6장(합성)", "cancelled", 36_000_000)
    with pytest.raises(sqlite3.DatabaseError, match="never deleted"):
        library.execute("DELETE FROM CalendarEvent")
    kinds = [r[0] for r in library.execute("SELECT type FROM Event WHERE subject_type='CalendarEvent' ORDER BY at, id")]
    assert kinds == ["CALENDAR_ADDED", "CALENDAR_CHANGED", "CALENDAR_STATUS"]
    with pytest.raises(ValueError):
        cal.add(library, kind="party", title="x", day="2026-10-01")


def test_spoken_date_cannot_be_inserted_as_confirmed(library):
    with pytest.raises(sqlite3.DatabaseError, match="D-052"):
        cal.add(library, kind="exam", title="x", day="2026-10-20", source="transcript", source_ref="r", status="confirmed")


@pytest.mark.parametrize(("text", "expected"), [
    ("중간고사는 10월 20일에 봅니다", [("exam", "2026-10-20", "day")]),
    ("10월 중순에 학회가 있어서 일주일 동안 수업을 못하는 주가 있어요", [("closure", "2026-10-15", "approx")]),
    ("과제는 다음 주 화요일까지 제출하세요", [("assignment", "2026-09-29", "day")]),
    ("오늘 10월 20일 날씨가 좋네요", []),  # a date without an exam/assignment/closure word
])
def test_spoken_dates(text, expected):
    found = cal.spoken_dates(text, date(2026, 9, 22))
    assert [(s.kind, s.day.isoformat(), s.precision) for s in found] == expected


def test_scan_makes_candidates_with_quote_and_exam_cue(library):
    library.execute("INSERT INTO Lecture VALUES ('l1','gj','2026-09-07',1,NULL,NULL)")
    for tid, engine, text in (("t1", "clova", "10월 중순에 내가 학회가 있어서 일주일 동안 수업을 못하는 주가 있어요(합성)"),
                              ("t2", "daglo", "10월 중순 학회로 수업 못 해요(합성)")):
        library.execute("INSERT INTO Transcript (id,lecture_id,media_id,engine,role,role_decided_by,status,source_file,"
                        "sha256) VALUES (?, 'l1', NULL, ?, 'timeline', 'policy', 'done', ?, ?)", (tid, engine, tid, tid))
        library.execute("INSERT INTO TranscriptSegment (transcript_id,seq,start_ms,text,chunk_index) VALUES (?,0,806000,?,0)",
                        (tid, text))
    library.execute("INSERT INTO Lecture VALUES ('l2','ac','2026-09-11',1,NULL,NULL)")
    library.execute("INSERT INTO Transcript (id,lecture_id,media_id,engine,role,role_decided_by,status,source_file,"
                    "sha256) VALUES ('t3','l2',NULL,'gemini_transcribe','timeline','policy','done','t3','t3')")
    library.execute("INSERT INTO TranscriptSegment (transcript_id,seq,start_ms,text,chunk_index) "
                    "VALUES ('t3',0,0,'중간고사는 10월 20일입니다(합성)',0)")
    first = cal.scan_transcripts(library)
    again = cal.scan_transcripts(library)
    library.commit()
    assert first["candidates_added"] >= 1 and again["candidates_added"] == 0  # idempotent
    items = cal.candidates(library)
    assert all(c["status"] == "candidate" and c["source"] == "transcript" for c in items)
    assert {c["kind"] for c in items} == {"closure", "exam"}
    assert [tuple(r) for r in library.execute("SELECT kind, decided_by FROM ExamCue")] == [("scope_notice", "rule")]
    closure = next(c for c in items if c["kind"] == "closure")
    assert closure["evidence"]["engines_agree"] == 2
    assert closure["date_precision"] == "approx" and closure["date_text"] == "10월 중순"
    assert closure["evidence"]["lecture_id"] == "l1" and closure["evidence"]["at_ms"] == 806000
    cal.set_status(library, closure["id"], "confirmed", "합성")
    assert cal.events(library, "2026-10-01", "2026-10-31")[0]["kind"] == "closure"


def test_etl_deadlines_are_imported_and_updated_not_duplicated(library, tmp_path):
    path = tmp_path / "assignments.json"
    path.write_text(json.dumps([{"id": "e1", "course": "경제학원론", "title": "과제 1(합성)", "due": "2026-10-05T23:59"},
                                {"course": "없는과목", "title": "x", "due": "2026-10-05"}], ensure_ascii=False),
                    encoding="utf-8")
    first = cal.import_etl(library, path)
    assert first["added"] == 1 and len(first["skipped"]) == 1
    path.write_text(json.dumps([{"id": "e1", "course": "ac", "title": "과제 1(합성)", "due": "2026-10-06T18:00"}]),
                    encoding="utf-8")
    second = cal.import_etl(library, path)
    library.commit()
    assert (second["added"], second["updated"]) == (0, 1)
    row = library.execute("SELECT date, start_ms, source, status FROM CalendarEvent").fetchone()
    assert tuple(row) == ("2026-10-06", 18 * 3_600_000, "etl", "confirmed")
    assert cal.import_etl(library, tmp_path / "none.json")["missing"]


def test_month_view_and_next_exam_d_day(library):
    cal.add(library, kind="exam", title="중간고사(합성)", day="2026-10-20", course_id="gj")
    cal.add(library, kind="personal", title="약속(합성)", day="2026-10-02")
    view = cal.month_view(library, 2026, 10, date(2026, 9, 29))
    assert [e["title"] for e in view["events"]] == ["약속(합성)", "중간고사(합성)"]
    assert view["next_exam"]["d_day"] == 21 and view["next_exam"]["course"] == "도시계"
    assert [e["d_day"] for e in view["upcoming"]] == [3, 21]


@pytest.mark.parametrize("text", [
    "이 회사의 결산일은 12월 31일이야. 12월 31일에 마감은 해야 되잖아.",  # accounting content, not a deadline
    "사업 보고서는 1월 1일부터 12월 31일까지를 보여줘요.",
    "3월 말 결산 과제 얘기는 지난 학기에 했죠.",  # a date before the lecture
    "과제 얘기를 좀 하고 넘어갈게요. 그리고 한참 딴 얘기를 하다가 보면 어느새 시간이 가고 결국 10월 20일이 되겠죠.",
])
def test_spoken_dates_ignore_course_content_and_far_or_past_dates(text):
    """Real-data check 2026-09-29: 22 candidates → 2 after same-sentence/near/horizon rules."""
    assert cal.spoken_dates(text, date(2026, 9, 22)) == []


def test_presentation_and_report_kinds_after_0009(tmp_path):
    """User 2026-11-24: 발표·보고서 get their own kinds; rows written before 0009 survive the rebuild."""
    path = tmp_path / "lecturehub.sqlite"
    with closing(db.connect(path)) as connection:
        db.migrate(connection)
        connection.execute("DELETE FROM schema_version WHERE version=9")
        connection.commit()
    # rebuild an 0008-shaped table holding one row, then let 0009 run on it
    with closing(db.connect(path)) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DROP TABLE CalendarEvent")
        script = (db.resources.files("lecturehub.core") / "migrations" / "0006_calendar.sql").read_text(encoding="utf-8")
        for statement in db._statements(script):
            connection.execute(statement)
        connection.execute("INSERT INTO Course VALUES ('gj','GJ','도시계','gj','26-2')")
        old = cal.add(connection, kind="other", title="예비발표(합성)", day="2026-12-02", course_id="gj", start="10:00")
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE CalendarEvent SET kind='presentation' WHERE id=?", (old,))
        connection.rollback()
    with closing(db.connect(path)) as connection:
        assert db.migrate(connection) == [9]
        assert tuple(connection.execute("SELECT kind, title, start_ms FROM CalendarEvent WHERE id=?", (old,)).fetchone()) \
            == ("other", "예비발표(합성)", 36_000_000)
        cal.update(connection, old, kind="presentation")
        cal.add(connection, kind="report", title="보고서(합성)", day="2026-12-15", course_id="gj", start="23:59")
        connection.commit()
        labels = sorted(e["kind_label"] for e in cal.events(connection, "2026-12-01", "2026-12-31"))
        assert labels == ["발표", "보고서"]
        with pytest.raises(sqlite3.DatabaseError, match="never deleted"):
            connection.execute("DELETE FROM CalendarEvent")
