"""D-079 / REMIND: synthetic semester only, injected clocks and toast collectors."""
from __future__ import annotations

import json
from contextlib import closing
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from lecturehub.core import db, settings
from lecturehub.core.paths import database_path
from lecturehub.index import calendar_events as cal
from lecturehub.index import etl_submissions, reminders, schedule
from lecturehub.ui.app import Api
from lecturehub.ui.library import Library

COURSE_A, COURSE_B = "합성과목 가", "합성과목 나"
NOW = datetime(2026, 11, 25, 10, tzinfo=etl_submissions.KST)


@pytest.fixture(autouse=True)
def recorder_config(tmp_path, monkeypatch):
    monkeypatch.setenv("LECTUREHUB_RECORDER_CONFIG", str(tmp_path / "recorder.json"))


@pytest.fixture
def semester(tmp_path, monkeypatch):
    root = tmp_path / "합성 학기 (시험)"
    (root / "_lecturehub").mkdir(parents=True)
    monkeypatch.setenv("LECTUREHUB_RECORDER_CONFIG", str(tmp_path / "recorder.json"))
    with closing(db.connect(database_path(root))) as connection:
        db.migrate(connection)
        for cid, name in (("a", COURSE_A), ("b", COURSE_B)):
            connection.execute("INSERT INTO Course VALUES (?,?,?,?,'26-2')", (cid, cid.upper(), name, cid))
        connection.commit()
        yield root, connection


def assignment(root, due, *, ident="1", course=COURSE_A, submission=None):
    path = root / "_lecturehub" / "etl" / "assignments.json"
    path.parent.mkdir(exist_ok=True)
    rows = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else []
    rows = [r for r in rows if r["id"] != ident]
    rows.append({"id": ident, "course": course, "title": f"합성 과제 {ident}", "due": due.isoformat(),
                 "submission": submission})
    path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8-sig")


def event(connection, when, *, kind="exam", course="a", start="11:00", **kw):
    return cal.add(connection, kind=kind, title="합성 일정", day=when.date().isoformat(), course_id=course, start=start, **kw)


def run(semester, now=NOW):
    root, connection = semester
    said = []
    count = reminders.tick(connection, root, clock=lambda: now, toast=lambda *a: said.append(a))
    assert count == len(said)
    return said


def no_briefing():
    reminders.set_preferences({"briefing": False})


def test_WEEK_001_week_picture_uses_daily_exceptions_and_exact_ms(semester):
    """D-081 / WEEK-001: original cancelled slots are display-only, not recording classes."""
    root, connection = semester
    connection.execute("INSERT INTO Schedule(course_id,weekday,start,end) VALUES ('a',2,33300000,38700000)")
    connection.execute("INSERT INTO Schedule(course_id,weekday,start,end) VALUES ('b',4,63900000,67500001)")
    schedule.add_exceptions(connection, [NOW.date()], course_id="a", kind="휴강", note="합성 휴강")
    schedule.add_exceptions(connection, [NOW.date()], course_id="a", kind="보강", note="합성 보강 14:00-15:30; 강의실: 합성 101호")
    schedule.add_exceptions(connection, [NOW.date() + timedelta(days=1)], course_id="b", kind="시간변경", note="13:00-14:00")
    connection.commit()
    view = Library(root).week_view(NOW)
    assert view["ok"] and view["start"] == "2026-11-23" and view["end"] == "2026-11-29"
    assert len(view["days"]) == 5 and view["now_ms"] == 36_000_000
    assert (view["start_ms"], view["end_ms"]) == (9 * 3_600_000, 19 * 3_600_000)
    classes = view["days"][2]["classes"]
    assert [(c["kind"], c["start_ms"], c["end_ms"]) for c in classes] == [
        ("휴강", 33_300_000, 38_700_000), ("보강", 50_400_000, 55_800_000)]
    assert view["days"][3]["classes"][0]["kind"] == "시간변경"
    assert classes[1]["room"] == "합성 101호" and "room" not in classes[0]
    assert [c.kind for c in schedule.daily_schedule(connection, NOW.date())] == ["보강"]


def test_WEEK_002_chips_share_reminder_filters_and_keep_past_dates(semester):
    """D-081 / WEEK-002: confirmed only, submitted never shown, including imported duplicates."""
    root, connection = semester
    monday = NOW - timedelta(days=2)
    assignment(root, monday, ident="past", submission={"missing": True})
    for ident, submission in (("done", {"workflow_state": "submitted"}), ("excused", {"excused": True}),
                              ("graded", {"workflow_state": "graded"}), ("open", None)):
        assignment(root, NOW, ident=ident, submission=submission)
    cal.import_etl(connection, root / "_lecturehub" / "etl" / "assignments.json")
    past = event(connection, monday, kind="quiz")
    exam = event(connection, NOW + timedelta(days=3), kind="exam", start=None)
    event(connection, NOW, status="candidate")
    event(connection, NOW, status="cancelled")
    event(connection, NOW, date_precision="approx")
    connection.commit()
    view = Library(root).week_view(NOW)
    assert view["ok"]
    assert [d["date"] for d in view["days"]][-1] == "2026-11-28"  # only Saturday has an item
    items = [e for d in view["days"] for e in d["items"]]
    assert {e["id"] for e in items} == {f"etl:{COURSE_A}:{ident}" for ident in ("past", "graded", "open")} | {
        f"calendar:{past}", f"calendar:{exam}"}
    assert next(e for e in items if e["id"] == f"calendar:{exam}")["timed"] is False
    assert all("submission" not in e for e in items)  # past dates carry no missing-warning state


@pytest.mark.parametrize("closure", ["calendar", "exception", "all-calendar", "all-exception"])
def test_WEEK_003_closures_keep_faded_regular_slot_only_in_picture(semester, closure):
    """D-081 / WEEK-003: per-course and whole-day closures preserve real slots for display."""
    root, connection = semester
    connection.execute("INSERT INTO Schedule(course_id,weekday,start,end) VALUES ('a',2,32400000,36000000)")
    cid = None if closure.startswith("all-") else "a"
    if closure.endswith("calendar"):
        event(connection, NOW, kind="closure", course=cid, start=None)
    else:
        schedule.add_exceptions(connection, [NOW.date()], course_id=cid, kind="휴강", note="합성 휴강")
    connection.commit()
    assert schedule.daily_schedule(connection, NOW.date()) == []
    assert Library(root).week_view(NOW)["days"][2]["classes"][0]["kind"] == "휴강"


def test_WEEK_004_browsing_month_boundary_weekends_and_axis_minimum(semester):
    """D-081 / WEEK-004: signed offsets, Sunday classes and a minimum span ending at midnight."""
    root, connection = semester
    event(connection, NOW + timedelta(days=5), kind="quiz")  # next Monday, November 30
    schedule.add_exceptions(connection, [NOW.date() + timedelta(days=11)], course_id="a", kind="대체", note="23:00-23:30")
    connection.commit()
    library = Library(root)
    next_week = library.week_view(NOW, 1)
    assert (next_week["start"], next_week["end"]) == ("2026-11-30", "2026-12-06")
    assert [d["date"] for d in next_week["days"]] == ["2026-11-30", "2026-12-01", "2026-12-02", "2026-12-03", "2026-12-04", "2026-12-06"]
    assert next_week["days"][0]["items"] and next_week["days"][-1]["classes"][0]["kind"] == "대체"
    assert (next_week["start_ms"], next_week["end_ms"]) == (20 * 3_600_000, 24 * 3_600_000)
    previous = library.week_view(NOW, -1)
    assert previous["start"] == "2026-11-16" and len(previous["days"]) == 5
    assert (previous["start_ms"], previous["end_ms"]) == (9 * 3_600_000, 17 * 3_600_000)


def test_WEEK_007_ordinary_week_five_columns_then_saturday_exam(semester):
    """D-081 / WEEK-007: a regular weekday semester has no empty weekend columns."""
    root, connection = semester
    connection.execute("INSERT INTO Schedule(course_id,weekday,start,end) VALUES ('a',2,32400000,37800000)")
    connection.commit()
    library = Library(root)
    ordinary = library.week_view(NOW)
    assert [d["date"] for d in ordinary["days"]] == [f"2026-11-{d}" for d in range(23, 28)]
    event(connection, NOW + timedelta(days=3), kind="exam")
    connection.commit()
    saturday = library.week_view(NOW)
    assert [d["date"] for d in saturday["days"]] == [f"2026-11-{d}" for d in range(23, 29)]
    assert saturday["days"][-1]["items"][0]["kind"] == "exam"
    assert ordinary["days"] == saturday["days"][:-1]


def test_WEEK_008_day_panel_keeps_cancelled_classes_without_changing_reminders(semester, monkeypatch):
    """D-081 / WEEK-008 / D-050: UI-only cancellations, readonly, no recorder access."""
    root, connection = semester
    connection.execute("INSERT INTO Schedule(course_id,weekday,start,end) VALUES ('a',2,32400000,37800000)")
    schedule.add_exceptions(connection, [NOW.date()], course_id="a", kind="휴강", note="합성 휴강")
    schedule.add_exceptions(connection, [NOW.date()], course_id="a", kind="보강", note="14:00-15:00")
    connection.commit()
    library = Library(root)
    monkeypatch.setattr(library, "_ensure", lambda: pytest.fail("no setup"))
    before = connection.total_changes
    shown = library.today_briefing(NOW)
    assert shown["ok"] and shown["now"] == NOW.isoformat()
    assert [c["kind"] for c in shown["classes"]] == ["휴강", "보강"]
    assert [c["kind"] for c in reminders.today(connection, root, NOW)["classes"]] == ["보강"]
    assert connection.total_changes == before


def test_WEEK_005_one_readonly_connection_and_api_delegation(semester, monkeypatch):
    """D-081 / D-050 / WEEK-005: no initialization, short timeout, no writes or notifications."""
    import lecturehub.ui.app as app_module

    root, connection = semester
    connection.commit()
    library, calls = Library(root), []
    monkeypatch.setattr(library, "_ensure", lambda: pytest.fail("must never initialize"))
    real_connect = db.connect_readonly

    def readonly(path, *, timeout):
        calls.append((path, timeout))
        return real_connect(path, timeout=timeout)

    monkeypatch.setattr(db, "connect_readonly", readonly)
    monkeypatch.setattr(app_module, "_now", lambda: NOW)
    before = {p.name: p.read_bytes() for p in library.path.parent.iterdir() if p.is_file() and not p.name.endswith("-shm")}
    api = Api(SimpleNamespace(), library=library)
    assert api.week_view(1)["start"] == "2026-11-30"
    assert calls == [(library.path, 0.05)]
    after = {p.name: p.read_bytes() for p in library.path.parent.iterdir() if p.is_file() and not p.name.endswith("-shm")}
    assert after == before


@pytest.mark.parametrize("damage", ["missing", "garbage", "newer", "busy"])
def test_WEEK_006_bad_database_fails_softly_without_setup(tmp_path, monkeypatch, damage):
    """D-081 / D-050 / WEEK-006: optional picture errors do not initialize or change any file."""
    import sqlite3

    root = tmp_path / "합성 학기 (읽기 전용)"
    library = Library(root)
    if damage in {"garbage", "newer"}:
        library.path.parent.mkdir(parents=True)
        if damage == "garbage":
            library.path.write_bytes(b"synthetic broken database")
        else:
            db.init_db(library.path)
            with closing(db.connect(library.path)) as connection:
                connection.execute("INSERT INTO schema_version(version) VALUES (?)", (db.known_schema_version() + 1,))
                connection.commit()
    if damage == "busy":
        def busy(*args, **kw):
            assert kw["timeout"] == 0.05
            raise sqlite3.OperationalError("synthetic busy database")
        monkeypatch.setattr(db, "connect_readonly", busy)
    before = {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    monkeypatch.setattr(library, "_ensure", lambda: pytest.fail("must never initialize"))
    assert library.week_view(NOW)["ok"] is False
    assert {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


def test_REMIND_001_deadline_slots_restart_and_changed_due(semester):
    root, _ = semester
    no_briefing()
    due = NOW + timedelta(hours=24)
    assignment(root, due)
    assert len(run(semester)) == 1
    assert run(semester) == []  # same tuple after restart
    assignment(root, due + timedelta(minutes=30))
    assert run(semester) == []
    assert len(run(semester, NOW + timedelta(minutes=30))) == 1  # changed due re-arms
    assert len(run(semester, due - timedelta(hours=3) + timedelta(minutes=30))) == 1
    state = json.loads((root / "_lecturehub" / "reminders_sent.json").read_text(encoding="utf-8"))
    assert len(state["sent"]) == 3 and all(isinstance(v, int) for v in state["sent"].values())


@pytest.mark.parametrize(("late_minutes", "count"), [(0, 1), (119, 1), (120, 0), (121, 0)])
def test_REMIND_002_offline_catchup_is_strictly_less_than_two_hours(semester, late_minutes, count):
    root, _ = semester
    no_briefing()
    assignment(root, NOW + timedelta(hours=3))
    assert len(run(semester, NOW + timedelta(minutes=late_minutes))) == count


@pytest.mark.parametrize("sub", [None, {"workflow_state": "graded"}, {"missing": True, "workflow_state": "graded"},
                                 {"workflow_state": "submitted"}, {"excused": True},
                                 {"workflow_state": "graded", "submitted_at": "2026-11-24T12:00"}])
def test_REMIND_003_submission_DONE_matches_existing_policy(semester, sub):
    root, _ = semester
    no_briefing()
    assignment(root, NOW + timedelta(hours=3), submission=sub)
    expected = etl_submissions.state(sub) not in etl_submissions.DONE
    assert bool(run(semester)) is expected
    assert bool(reminders.today(semester[1], root, NOW)["dues"]) is expected


@pytest.mark.parametrize("kind", ["assignment", "report", "presentation"])
def test_REMIND_004_confirmed_calendar_deadlines(semester, kind):
    _, connection = semester
    no_briefing()
    event(connection, NOW, kind=kind, start="13:00")
    event(connection, NOW, kind=kind, start="13:01", status="candidate")
    event(connection, NOW, kind=kind, start="13:02", status="cancelled")
    assert len(run(semester)) == 1


@pytest.mark.parametrize("kind", ["exam", "quiz"])
def test_REMIND_005_exam_evening_and_start_slots(semester, kind):
    _, connection = semester
    no_briefing()
    event(connection, NOW, kind=kind, start="11:00")
    evening = (NOW - timedelta(days=1)).replace(hour=21)
    assert len(run(semester, evening)) == 1
    assert run(semester, evening + timedelta(minutes=1)) == []
    assert len(run(semester)) == 1
    assert run(semester, NOW + timedelta(hours=1)) == []  # due has arrived


def test_REMIND_006_date_only_exam_uses_eight_am_and_precision_is_respected(semester):
    root, connection = semester
    no_briefing()
    event(connection, NOW, start=None)
    event(connection, NOW, start=None, date_precision="approx")
    assert run(semester, NOW.replace(hour=7, minute=59)) == []
    assert len(run(semester, NOW.replace(hour=8))) == 1
    card = reminders.today(connection, root, NOW.replace(hour=8))
    assert len(card["exams"]) == 1 and card["exams"][0]["timed"] is False


def test_REMIND_007_date_only_deadline_and_midnight_start(semester):
    root, connection = semester
    no_briefing()
    event(connection, NOW, kind="report", start=None)
    event(connection, NOW + timedelta(days=1), kind="quiz", start="00:00")
    assert len(run(semester, NOW.replace(hour=20, minute=59))) == 1  # 23:59 deadline minus 3h
    assert len(run(semester, NOW.replace(hour=21))) == 1
    assert len(run(semester, NOW.replace(hour=23))) == 1  # midnight exam minus 1h


def test_REMIND_008_three_toasts_maximum_including_summary_and_no_replay(semester):
    root, _ = semester
    no_briefing()
    for i in range(7):
        assignment(root, NOW + timedelta(hours=3), ident=str(i))
    said = run(semester)
    assert len(said) == 3 and said[-1][0] == "일정 알림 모음" and "5건" in said[-1][1]
    assert run(semester) == []


def test_REMIND_009_briefing_once_after_seven_per_semester_and_day(semester, tmp_path):
    root, connection = semester
    assert reminders.preferences() == {"deadlines": True, "briefing": True, "muted_courses": []}
    assert run(semester, NOW.replace(hour=6, minute=59)) == []
    said = run(semester, NOW.replace(hour=7))
    assert len(said) == 1 and "오늘: 수업 0개 · 마감 0개 · 시험 0개" in said[0][1]
    assert run(semester, NOW.replace(hour=19)) == []
    assert len(run(semester, NOW + timedelta(days=1))) == 1
    assert reminders.tick(connection, tmp_path / "다른 학기", clock=lambda: NOW, toast=lambda *a: None) == 1


def test_REMIND_019_no_morning_briefing_on_a_first_start_late_in_the_day(semester):
    assert run(semester, NOW.replace(hour=reminders.BRIEFING_UNTIL)) == []  # first start at 14:00: no "오늘" toast
    assert run(semester, NOW.replace(hour=20)) == []  # and that day stays done
    assert len(run(semester, (NOW + timedelta(days=1)).replace(hour=8))) == 1  # the next morning it comes


def test_REMIND_010_home_and_briefing_use_closures_makeups_and_time_windows(semester):
    root, connection = semester
    connection.execute("INSERT INTO Schedule(course_id,weekday,start,end) VALUES ('a',2,32400000,36000000)")
    connection.execute("INSERT INTO Schedule(course_id,weekday,start,end) VALUES ('b',2,39600000,43200000)")
    schedule.add_exceptions(connection, [NOW.date()], course_id="a", kind="휴강", note="합성 휴강")
    schedule.add_exceptions(connection, [NOW.date()], course_id="a", kind="보강", note="합성 보강 14:00-15:00")
    assignment(root, NOW + timedelta(hours=5))
    assignment(root, NOW + timedelta(hours=25), ident="later")
    event(connection, NOW, start="11:00")
    event(connection, NOW + timedelta(days=6), kind="quiz")
    event(connection, NOW + timedelta(days=8), kind="exam")
    view = reminders.today(connection, root, NOW)
    assert [(r["course"], r["kind"]) for r in view["classes"]] == [(COURSE_B, "정규"), (COURSE_A, "보강")]
    assert len(view["dues"]) == 1 and len(view["exams"]) == 2
    text = reminders.briefing(view, set())
    assert f"수업 2개(첫 수업 11:00 {COURSE_B})" in text and "마감 1개 · 시험 1개" in text
    reminders.set_preferences({"muted_courses": [COURSE_A]})
    assert reminders.today(connection, root, NOW) == view  # mute never hides the home card
    assert COURSE_A not in reminders.briefing(view, {COURSE_A})


def test_REMIND_011_off_and_mute_consume_slots_without_later_flood(semester):
    root, _ = semester
    no_briefing()
    assignment(root, NOW + timedelta(hours=3))
    reminders.set_preferences({"muted_courses": [COURSE_A]})
    assert run(semester) == []
    reminders.set_preferences({"muted_courses": []})
    assert run(semester, NOW + timedelta(minutes=1)) == []
    assignment(root, NOW + timedelta(hours=3, minutes=1))
    reminders.set_preferences({"deadlines": False})
    assert run(semester, NOW + timedelta(minutes=1)) == []
    reminders.set_preferences({"deadlines": True})
    assert run(semester, NOW + timedelta(minutes=2)) == []


def test_REMIND_012_broken_state_invalid_due_and_no_double_etl_calendar(semester):
    root, connection = semester
    no_briefing()
    path = root / "_lecturehub" / "reminders_sent.json"
    path.write_text('{"sent": ["broken"]}', encoding="utf-8-sig")
    assignment(root, NOW + timedelta(hours=3))
    cal.import_etl(connection, root / "_lecturehub" / "etl" / "assignments.json")
    assert len(run(semester)) == 1
    assignment(root, NOW - timedelta(minutes=1), ident="past")
    data = root / "_lecturehub" / "etl" / "assignments.json"
    rows = json.loads(data.read_text(encoding="utf-8-sig")) + [{"id": "bad", "due": "?"}]
    data.write_text(json.dumps(rows), encoding="utf-8")
    assert run(semester) == []


def test_REMIND_013_errors_never_escape_or_initialize_the_library(semester, monkeypatch):
    root, connection = semester
    monkeypatch.setattr(reminders, "items", lambda *a: (_ for _ in ()).throw(RuntimeError("synthetic")))
    assert run(semester) == []
    library = Library(root)
    monkeypatch.setattr(library, "_ensure", lambda: pytest.fail("must never initialize or index"))
    api = Api(SimpleNamespace(), library=library)
    api._reminder_tick(clock=lambda: NOW, toast=lambda *a: pytest.fail("no real toast"))
    missing = Library(root / "없음")
    api = Api(SimpleNamespace(), library=missing)
    api._reminder_tick(clock=lambda: NOW, toast=lambda *a: pytest.fail("no real toast"))
    assert not missing.path.exists()


def test_REMIND_014_mute_filters_all_etl_toast_paths_and_keeps_inbox(semester, monkeypatch):
    from lecturehub.ui import notify

    reminders.set_preferences({"muted_courses": [COURSE_A]})
    said = []
    monkeypatch.setattr(notify, "toast", lambda *a, **kw: said.append(a))
    api = Api(SimpleNamespace())
    rows = [{"course": c, "kind": "assignment_new", "line": c, "title": c, "text": "합성 글"}
            for c in (COURSE_A, COURSE_B)]
    api._tell_etl(rows)
    api._tell_board(rows)
    api._announce({"added": [{**r, "kind_label": "과제", "summary": r["course"]} for r in rows]})
    assert len(said) == 3 and all(COURSE_A not in str(a) and COURSE_B in str(a) for a in said)
    assert len(rows) == 2


def test_REMIND_015_public_settings_roundtrip_and_home_readonly(semester, monkeypatch):
    root, connection = semester
    connection.commit()
    monkeypatch.setenv("LECTUREHUB_EDITION", "public")
    backend = SimpleNamespace(ordered_courses=lambda: [SimpleNamespace(folder=COURSE_A), SimpleNamespace(folder=COURSE_B)])
    library, said = Library(root), []
    api = Api(backend, library=library)
    assert api.reminders_get()["deadlines"] is True
    assert api.reminders_set({"briefing": False, "muted_courses": [COURSE_A]})["courses"][0]["muted"] is True
    assert settings.get("reminders")["briefing"] is False
    assert api.reminders_set({"muted_courses": "invalid"})["ok"] is False
    monkeypatch.setattr(library, "_ensure", lambda: pytest.fail("must remain read-only"))
    assert library.today_briefing(NOW)["ok"] is True
    api._reminder_tick(clock=lambda: NOW, toast=lambda *a: said.append(a))
    assert said == []


def test_REMIND_016_confirmed_etl_notice_events_are_reminded(semester):
    _, connection = semester
    no_briefing()
    event(connection, NOW, source="etl", source_ref="ain:synthetic:exam", start="11:00")
    event(connection, NOW, kind="assignment", source="etl", source_ref="ain:synthetic:assignment", start="13:00")
    event(connection, NOW, kind="assignment", source="etl", source_ref="lxb:synthetic#date", start="13:00")
    assert len(run(semester)) == 3


def test_REMIND_017_notice_scheduler_keeps_ticking_during_a_slow_scan(monkeypatch):
    import lecturehub.ui.app as app_module

    api = Api(SimpleNamespace())
    ticks, workers, waits = [], [], []
    api._reminder_tick = lambda: ticks.append("tick")

    class Worker:
        def __init__(self, *, target, daemon):
            assert target == api._notice_round and daemon
            workers.append(self)

        def start(self):
            pass  # simulate a scan that stays running, without AI or network

        def is_alive(self):
            return True

    def wait(seconds):
        waits.append(seconds)
        return len(waits) == 4

    api._notice_stop = SimpleNamespace(wait=wait)
    monkeypatch.setattr(app_module.threading, "Thread", Worker)
    api._notice_loop()
    assert waits == [30, 60, 60, 60] and len(ticks) == 3 and len(workers) == 1


def test_REMIND_018_atomic_write_failure_and_toast_failure_stay_local(semester, monkeypatch):
    root, connection = semester
    no_briefing()
    assignment(root, NOW + timedelta(hours=3))
    real_write = reminders.atomic_write_json
    monkeypatch.setattr(reminders, "atomic_write_json", lambda *a: (_ for _ in ()).throw(OSError("synthetic")))
    assert run(semester) == []
    assert not (root / "_lecturehub" / "reminders_sent.json").exists()
    monkeypatch.setattr(reminders, "atomic_write_json", real_write)
    assert reminders.tick(connection, root, clock=lambda: NOW,
                          toast=lambda *a: (_ for _ in ()).throw(OSError("synthetic"))) == 1
    assert run(semester) == []
