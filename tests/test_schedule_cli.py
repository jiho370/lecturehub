"""Schedule exception registration, provenance and finite date-range regressions."""

import json
import sqlite3
from contextlib import closing
from datetime import date

import pytest
import typer
from typer.testing import CliRunner

from lecturehub.core.db import connect, init_db
from lecturehub.core.paths import database_path
from lecturehub.index.schedule import add_exceptions, daily_schedule, exception_dates
from lecturehub.index.schedule_cli import register


@pytest.fixture
def root(tmp_path):
    root = tmp_path / "학기 자료 (합성 26-2)"
    root.mkdir()
    (root / "시간표.csv").write_text("합성 원본 시간표 — 수정하지 않음", encoding="utf-8")
    init_db(database_path(root))
    with closing(connect(database_path(root))) as connection, connection:
        connection.execute(
            "INSERT INTO Course(id,code,folder_name,abbr,term) VALUES ('course','GJ','도시계','gj','26-2')"
        )
        connection.execute(
            "INSERT INTO Schedule(course_id,weekday,start,end) VALUES ('course',2,57600000,61200000)"
        )
    return root


def invoke(root, *options):
    app = typer.Typer()
    register(app)
    return CliRunner().invoke(app, ["schedule", "except", "add", "--root", str(root), *options])


def test_single_all_course_holiday_is_idempotent_with_user_evidence_and_event(root):
    """SCHEDULE-001: identical user input retains one exception and provenance pair."""
    arguments = ["--date", "2026-09-30", "--all-courses", "--kind", "휴강", "--note", "합성 전과목 휴강", "--json"]
    first = invoke(root, *arguments)
    second = invoke(root, *arguments)
    assert first.exit_code == second.exit_code == 0, first.output + second.output
    initial, repeated = json.loads(first.stdout), json.loads(second.stdout)
    assert initial["created"] == repeated["existing"]
    assert len(initial["created"]) == 1 and repeated["created"] == []
    with closing(connect(database_path(root))) as connection:
        row = connection.execute("SELECT id,course_id,date,kind FROM ScheduleException").fetchone()
        assert row[1:] == (None, "2026-09-30", "휴강")
        evidence = connection.execute("SELECT subject_id,source,detail FROM Evidence").fetchall()
        assert len(evidence) == 1 and evidence[0][0] == row[0] and evidence[0][1] == "user"
        assert json.loads(evidence[0][2])["note"] == "합성 전과목 휴강"
        assert connection.execute("SELECT count(*) FROM Event").fetchone()[0] == 1
        assert daily_schedule(connection, date(2026, 9, 30)) == []
        assert connection.execute("SELECT count(*) FROM Schedule").fetchone()[0] == 1
    assert (root / "시간표.csv").read_text(encoding="utf-8") == "합성 원본 시간표 — 수정하지 않음"


def test_bounded_weekly_replacement_uses_korean_course_and_inclusive_end(root):
    """SCHEDULE-002/TODAY-001: expand only the requested Wednesdays, without inference."""
    result = invoke(root, "--from", "2026-09-30", "--until", "2026-12-21", "--weekday", "수",
                    "--course", "도시계", "--kind", "대체", "--note", "합성 과제 대체", "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert len(payload["created"]) == 12
    assert payload["dates"][0] == "2026-09-30" and payload["dates"][-1] == "2026-12-16"
    with closing(connect(database_path(root))) as connection:
        assert daily_schedule(connection, date(2026, 9, 30)) == []
        assert daily_schedule(connection, date(2026, 12, 16)) == []
        assert len(daily_schedule(connection, date(2026, 12, 23))) == 1
        assert connection.execute("SELECT count(*) FROM Evidence WHERE source='user'").fetchone()[0] == 12


@pytest.mark.parametrize("date_arguments", [
    ["--date", "2026-02-30"],
    ["--date", "20260930"],
    ["--from", "2026-09-30", "--weekday", "수"],
    ["--from", "2026-10-07", "--until", "2026-09-30", "--weekday", "수"],
    ["--date", "2026-09-30", "--weekday", "수"],
])
def test_invalid_or_unbounded_dates_are_rejected_without_rows(root, date_arguments):
    """SCHEDULE-003: a missing semester end or invalid date never generates rows."""
    result = invoke(root, *date_arguments, "--all-courses", "--kind", "휴강", "--note", "합성")
    assert result.exit_code == 2, result.output
    with closing(connect(database_path(root))) as connection:
        assert connection.execute("SELECT count(*) FROM ScheduleException").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM Event").fetchone()[0] == 0


@pytest.mark.parametrize("scope", [[], ["--all-courses", "--course", "도시계"]])
def test_scope_must_be_explicit_and_unambiguous(root, scope):
    result = invoke(root, "--date", "2026-09-30", "--kind", "휴강", "--note", "합성", *scope)
    assert result.exit_code == 2, result.output


def test_unknown_or_ambiguous_course_never_registers(root):
    for name in ["없는과목", "도시계"]:
        if name == "도시계":
            with closing(connect(database_path(root))) as connection, connection:
                connection.execute(
                    "INSERT INTO Course(id,code,folder_name,abbr,term) VALUES ('other','OTHER','도시계','other','27-1')"
                )
        result = invoke(root, "--date", "2026-09-30", "--course", name, "--kind", "휴강", "--note", "합성")
        assert result.exit_code == 1, result.output
    with closing(connect(database_path(root))) as connection:
        assert connection.execute("SELECT count(*) FROM ScheduleException").fetchone()[0] == 0


def test_multi_date_registration_is_atomic_and_does_not_commit_callers_work(root):
    """SCHEDULE-004: one failed date rolls back all rows and their events/evidence."""
    with closing(connect(database_path(root))) as connection:
        connection.execute(
            "CREATE TRIGGER fail_test_date BEFORE INSERT ON ScheduleException WHEN NEW.date='2026-10-07' "
            "BEGIN SELECT RAISE(ABORT, 'synthetic rejection'); END"
        )
        dates = exception_dates(start=date(2026, 9, 30), until=date(2026, 10, 7), weekday=2)
        with pytest.raises(sqlite3.IntegrityError, match="synthetic rejection"):
            add_exceptions(connection, dates, course_id="course", kind="대체", note="합성")
        assert connection.execute("SELECT count(*) FROM ScheduleException").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM Evidence").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM Event").fetchone()[0] == 0
        result = add_exceptions(connection, [date(2026, 9, 30)], course_id=None, kind="휴강", note="합성")
        assert len(result.created) == 1 and connection.in_transaction
        connection.rollback()
        assert connection.execute("SELECT count(*) FROM ScheduleException").fetchone()[0] == 0


def test_makeup_requires_explicit_clock_time(root):
    absent = invoke(root, "--date", "2026-10-01", "--course", "도시계", "--kind", "보강", "--note", "미정")
    assert absent.exit_code == 1
    valid = invoke(root, "--date", "2026-10-01", "--course", "도시계", "--kind", "보강",
                   "--note", "합성 보강 13:00-14:00")
    assert valid.exit_code == 0, valid.output
    with closing(connect(database_path(root))) as connection:
        assert daily_schedule(connection, date(2026, 10, 1))[0].start_ms == 13 * 3_600_000
