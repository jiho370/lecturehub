"""TODAY-001 schedule exceptions and B-2 library status on synthetic records."""

from contextlib import closing
from datetime import date, datetime, timedelta, timezone

import pytest

from lecturehub.core.db import connect, migrate
from lecturehub.core.ids import new_ulid
from lecturehub.index.schedule import daily_schedule, today
from lecturehub.index.status import library_status, render_status_html, render_status_text

KST = timezone(timedelta(hours=9))


def insert(connection, table, **values):
    fields = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    connection.execute(f"INSERT INTO {table} ({fields}) VALUES ({placeholders})", tuple(values.values()))


@pytest.fixture
def connection(tmp_path):
    with closing(connect(tmp_path / "상태 합성 (26-2).sqlite")) as connection:
        migrate(connection)
        for course_id, name, abbr in [("su", "구조역학", "su"), ("gj", "도시계", "gj")]:
            insert(connection, "Course", id=course_id, code=course_id, folder_name=name, abbr=abbr, term="26-2")
        insert(connection, "Schedule", id=new_ulid(), course_id="su", weekday=1,
               start=(10 * 60 + 5) * 60_000, end=(11 * 60 + 10) * 60_000)
        insert(connection, "Schedule", id=new_ulid(), course_id="gj", weekday=2,
               start=16 * 3_600_000, end=17 * 3_600_000)
        connection.commit()
        yield connection


def test_today_001_upcoming_tuesday_and_wednesday_replacement(connection):
    """TODAY-001: Tuesday 10:00 shows hydraulics; replaced Wednesday has no card."""
    morning = today(connection, datetime(2026, 9, 29, 10, 0, tzinfo=KST))
    assert morning.current == []
    assert morning.next.course == "구조역학"
    assert morning.next.start_ms == (10 * 60 + 5) * 60_000
    insert(connection, "ScheduleException", id=new_ulid(), course_id="gj", date="2026-09-30",
           kind="대체", note="수요일 수업은 대체 일정으로 진행")
    afternoon = today(connection, datetime(2026, 9, 30, 16, 0, tzinfo=KST))
    assert afternoon.current == []
    assert all(item.course_id != "gj" for item in afternoon.classes)


def test_today_exact_start_and_end_are_half_open(connection):
    assert len(today(connection, datetime(2026, 9, 29, 10, 5, tzinfo=KST)).current) == 1
    assert today(connection, datetime(2026, 9, 29, 11, 10, tzinfo=KST)).current == []


def test_cancel_makeup_time_change_and_global_cancellation(connection):
    insert(connection, "ScheduleException", id=new_ulid(), course_id="su", date="2026-09-29",
           kind="시간변경", note="11:20-12:20 실습실 변경")
    changed = daily_schedule(connection, date(2026, 9, 29))
    assert len(changed) == 1 and changed[0].start_ms == (11 * 60 + 20) * 60_000
    insert(connection, "ScheduleException", id=new_ulid(), course_id="su", date="2026-10-01",
           kind="보강", note="보강 13:00~14:00")
    assert daily_schedule(connection, date(2026, 10, 1))[0].kind == "보강"
    insert(connection, "ScheduleException", id=new_ulid(), course_id=None, date="2026-09-30",
           kind="휴강", note="합성 전체 휴강")
    assert daily_schedule(connection, date(2026, 9, 30)) == []


def test_ambiguous_exception_does_not_invent_new_time(connection):
    insert(connection, "ScheduleException", id=new_ulid(), course_id="su", date="2026-09-29",
           kind="시간변경", note="오후로 변경; 정확한 시각 미정")
    result = today(connection, datetime(2026, 9, 29, 10, 0, tzinfo=KST), horizon_days=0)
    assert result.classes == []
    assert result.next is None
    assert [w for w in result.warnings if "HH:MM-HH:MM" in w] and len(result.warnings) == 2
    assert any("추정 — 사용자 확인 전" in w for w in result.warnings)  # D-052: not confirmed by the user


def add_lecture(connection, lecture_id, lecture_date, *, recording=True, engine=None):
    insert(connection, "Lecture", id=lecture_id, course_id="su", date=lecture_date, session_index=1,
           title="<script>alert('합성')</script>")
    if not recording:
        return
    media_id = "media-" + lecture_id
    insert(connection, "MediaAsset", id=media_id, lecture_id=lecture_id, path=lecture_id + ".wav",
           sha256="sha-" + media_id, duration_ms=5000, codec="pcm", sample_rate=16000, channels=1, bytes=160000,
           origin="win_recorder", device_type="laptop", is_master=1, recorded_start=1000, partial=0,
           loudness_profile="[]", decided_by="user", confidence_class="exact")
    if engine:
        insert(connection, "Transcript", id="text-" + lecture_id, lecture_id=lecture_id, media_id=media_id,
               engine=engine, role="timeline", role_decided_by="user", status="done",
               source_file=lecture_id + ".txt", sha256="sha-text-" + lecture_id)


def test_status_counts_notes_and_excludes_existing_google_transcripts_from_backlog(connection):
    add_lecture(connection, "google", "2026-09-22", engine="gemini_transcribe")
    add_lecture(connection, "daglo", "2026-09-29", engine="daglo")
    add_lecture(connection, "missing", "2026-09-28", recording=False)
    insert(connection, "Material", id="material", course_id="su", path="합성.pdf", sha256="pdf-hash",
           kind="pdf", title="합성", page_count=3)
    insert(connection, "MaterialMatch", id=new_ulid(), lecture_id="daglo", material_id="material",
           page_from=1, page_to=3, confidence_class="exact", decided_by="user")
    insert(connection, "StudyArtifact", id="note", lecture_ids='["daglo"]', path="합성.html", kind="note",
           version=1, mode="new", inputs='{"transcript_ids":["text-daglo"]}', created_by="user", qa_status="unknown")
    report = library_status(connection, as_of=date(2026, 9, 29))
    assert [item.lecture_id for item in report.google_backlog] == ["daglo"]
    item = next(item for item in report.lectures if item.lecture_id == "daglo")
    assert (item.recordings, item.transcripts, item.materials, item.notes) == (1, {"daglo": 1}, 1, 1)
    assert any(item.lecture_id == "missing" for item in report.missing_recordings)
    assert "Google 전사 백로그: 1강의" in render_status_text(report)


def test_status_timetable_gaps_respect_cancellations_and_never_claim_certainty(connection):
    add_lecture(connection, "early", "2026-09-22", engine="gemini_transcribe")
    insert(connection, "ScheduleException", id=new_ulid(), course_id="gj", date="2026-09-23",
           kind="휴강", note="합성 휴강")
    report = library_status(connection, as_of=date(2026, 9, 30))
    gaps = {(item.course_id, item.date) for item in report.missing_recordings}
    assert ("gj", "2026-09-23") not in gaps
    assert ("su", "2026-09-29") in gaps
    assert ("gj", "2026-09-30") in gaps
    assert all("확인 필요" in item.reason for item in report.missing_recordings)
    assert report.warnings


def test_status_html_is_standalone_responsive_and_escapes_source_metadata(connection):
    add_lecture(connection, "one", "2026-09-29")
    document = render_status_html(library_status(connection, as_of=date(2026, 9, 29)))
    assert '<html lang="ko">' in document
    assert '<meta name="viewport"' in document
    assert "--paper:#ffffff" in document and "--green:#3c6e47" in document
    assert "@media(max-width:600px)" in document
    assert "<script>" not in document
    assert "&lt;script&gt;" in document
    assert "Google 전사 백로그" in document


def test_empty_status_has_no_invented_semester_start(connection):
    report = library_status(connection, as_of=date(2026, 9, 29))
    assert report.lectures == report.missing_recordings == report.google_backlog == []


def test_status_as_of_excludes_future_lecture_and_backlog(connection):
    add_lecture(connection, "future", "2026-10-06")
    report = library_status(connection, as_of=date(2026, 9, 29))
    assert report.lectures == report.google_backlog == []


@pytest.mark.parametrize("global_first", [True, False])
def test_global_holiday_overrides_explicit_makeup_regardless_of_insert_order(connection, global_first):
    """SCHEDULE-005: an all-course closure suppresses explicit make-up classes too."""
    exceptions = [
        {"course_id": None, "date": "2026-10-01", "kind": "휴강", "note": "전과목 휴강"},
        {"course_id": "su", "date": "2026-10-01", "kind": "보강", "note": "13:00-14:00 합성 보강"},
    ]
    for values in exceptions if global_first else reversed(exceptions):
        insert(connection, "ScheduleException", id=new_ulid(), **values)
    assert daily_schedule(connection, date(2026, 10, 1)) == []


def test_status_same_as_of_comparison_applies_explicit_holiday(connection):
    """SCHEDULE-006: before/after gap counts use the same cutoff, not today's date."""
    add_lecture(connection, "early", "2026-09-22", engine="gemini_transcribe")
    cutoff = date(2026, 9, 30)
    before = library_status(connection, as_of=cutoff)
    insert(connection, "ScheduleException", id=new_ulid(), course_id=None, date="2026-09-23",
           kind="휴강", note="사용자 확인 합성 휴강")
    after = library_status(connection, as_of=cutoff)
    assert before.as_of == after.as_of == cutoff.isoformat()
    assert len(after.missing_recordings) == len(before.missing_recordings) - 1


@pytest.mark.parametrize("source,detail,is_confirmed", [
    ("user", '{"decided_by":"user","references":[]}', True),
    ("heuristic", '{"decided_by":"user"}', False),
    ("user", '{"decided_by":"heuristic"}', False),
    ("user", "old plain-text evidence", False),
])
def test_status_separates_only_explicit_user_confirmed_recording_absence(connection, source, detail, is_confirmed):
    """STATUS-ABSENCE: a missing media link alone never becomes confirmed absence."""
    add_lecture(connection, "known", "2026-09-29", recording=False)
    insert(connection, "Evidence", id=new_ulid(), subject_type="Lecture", subject_id="known",
           kind="recording_absence_confirmed", detail=detail, source=source, created_at=1000)
    report = library_status(connection, as_of=date(2026, 9, 29))
    assert bool(report.known_absences) is is_confirmed
    assert bool(report.missing_recordings) is not is_confirmed
    if is_confirmed:
        assert "원음 없음·사용자 확인 완료: 1건" in render_status_text(report)
        assert "원음 없음 · 사용자 확인 완료" in render_status_html(report)


def test_confirmed_absence_does_not_hide_an_audio_recording_registered_later(connection):
    add_lecture(connection, "restored", "2026-09-29", recording=True)
    insert(connection, "Evidence", id=new_ulid(), subject_type="Lecture", subject_id="restored",
           kind="recording_absence_confirmed", detail='{"decided_by":"user"}', source="user", created_at=1000)
    report = library_status(connection, as_of=date(2026, 9, 29))
    assert report.known_absences == []
    assert report.lectures[0].recordings == 1


def test_status_start_date_bounds_missing_and_confirmed_absences_but_keeps_all_lectures(connection):
    """STATUS-RANGE: newly found older notes must not change a fixed-range comparison."""
    for lecture_id, lecture_date in [
        ("old-unknown", "2026-09-02"), ("old-confirmed", "2026-09-02"),
        ("in-range-unknown", "2026-09-04"), ("in-range-confirmed", "2026-09-04"),
    ]:
        add_lecture(connection, lecture_id, lecture_date, recording=False)
        if lecture_id.endswith("confirmed"):
            insert(connection, "Evidence", id=new_ulid(), subject_type="Lecture", subject_id=lecture_id,
                   kind="recording_absence_confirmed", detail='{"decided_by":"user"}', source="user", created_at=1000)
    report = library_status(connection, as_of=date(2026, 9, 4), start_date=date(2026, 9, 4))
    assert {item.lecture_id for item in report.lectures} == {
        "old-unknown", "old-confirmed", "in-range-unknown", "in-range-confirmed",
    }
    assert [item.lecture_id for item in report.missing_recordings] == ["in-range-unknown"]
    assert [item.lecture_id for item in report.known_absences] == ["in-range-confirmed"]


def test_status_rejects_start_after_cutoff(connection):
    """STATUS-RANGE: reversed comparison ranges produce a clear error."""
    with pytest.raises(ValueError, match="기준 날짜"):
        library_status(connection, as_of=date(2026, 9, 4), start_date=date(2026, 9, 5))



def test_D052_exceptions_stay_inferred_until_the_user_confirms(connection):
    """D-052: calendar/agent-registered exceptions are estimates; confirmation is append-only."""
    from lecturehub.index import confirmation

    closure = new_ulid()
    insert(connection, "ScheduleException", id=closure, course_id=None, date="2026-10-05", kind="휴강",
           note="추정 휴강(합성)")
    insert(connection, "Evidence", id=new_ulid(), subject_type="ScheduleException", subject_id=closure,
           kind="user", detail="대리 기록(합성)", source="user", created_at=1)  # an old agent-typed 'user' row
    assert confirmation.state_of(connection, "ScheduleException", closure) == confirmation.INFERRED
    result = today(connection, datetime(2026, 10, 5, 9, 0, tzinfo=KST), horizon_days=0)
    assert result.classes == [] and any("추정" in w for w in result.warnings)
    before = connection.execute("SELECT * FROM ScheduleException WHERE id=?", (closure,)).fetchone()
    confirmation.record(connection, "ScheduleException", closure, confirmation.CONFIRMED, "사용자 확인(합성)")
    assert confirmation.state_of(connection, "ScheduleException", closure) == confirmation.CONFIRMED
    assert not today(connection, datetime(2026, 10, 5, 9, 0, tzinfo=KST), horizon_days=0).warnings
    assert connection.execute("SELECT * FROM ScheduleException WHERE id=?", (closure,)).fetchone() == before
    source = connection.execute("SELECT source FROM Evidence WHERE field='confirmation'").fetchone()[0]
    assert source == "user"


def test_confirmed_closure_that_may_become_online_keeps_a_warning(connection):
    """User (2026-09-29): all-course closures are right, but a remote/video lecture may replace them — undecided."""
    from lecturehub.index import confirmation

    closure = new_ulid()
    insert(connection, "ScheduleException", id=closure, course_id=None, date="2026-10-05", kind="휴강",
           note="휴강(합성)")
    confirmation.record(connection, "ScheduleException", closure, confirmation.CONFIRMED, "사용자 확인(합성)")
    confirmation.record_maybe_online(connection, closure, "원격·동영상 대체 가능, 미정(합성)")
    result = today(connection, datetime(2026, 10, 5, 9, 0, tzinfo=KST), horizon_days=0)
    assert result.classes == []  # no classroom class to record automatically
    assert [w for w in result.warnings if "미정" in w] and not [w for w in result.warnings if "추정" in w]
    listed = confirmation.schedule_exceptions(connection, "2026-10-05")[0]
    assert listed["confirmation"] == confirmation.CONFIRMED and listed["maybe_online"]


def test_online_replacement_by_notice_and_transcript_statement_evidence(connection):
    """eTL notice: an online (video) lecture replaces the classroom session; transcript evidence keeps ids only."""
    import json as _json

    from lecturehub.index import confirmation

    row = new_ulid()
    course = connection.execute("SELECT id FROM Course LIMIT 1").fetchone()[0]
    insert(connection, "ScheduleException", id=row, course_id=course, date="2026-10-02", kind="대체",
           note="온라인 영상강의(합성 공지)")
    confirmation.record_replacement(connection, row, confirmation.ONLINE, "eTL 공지(합성)", source="metadata")
    listed = confirmation.schedule_exceptions(connection, "2026-10-02")[0]
    assert listed["replacement"] == confirmation.ONLINE and not listed["maybe_online"]
    result = today(connection, datetime(2026, 10, 2, 8, 0, tzinfo=KST), horizon_days=0)
    assert any("온라인(영상) 강의로 대체" in w for w in result.warnings)
    assert all(c.course_id != course for c in result.classes)
    confirmation.record_statement(connection, "ScheduleException", row, "합성 요약", [{"transcript_id": "t", "seq": 3}],
                                  "medium")
    detail = _json.loads(connection.execute(
        "SELECT detail FROM Evidence WHERE kind='transcript_statement'").fetchone()[0])
    assert detail == {"claim": "합성 요약", "refs": [{"transcript_id": "t", "seq": 3}], "certainty": "medium"}
