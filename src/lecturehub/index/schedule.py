"""Evaluate regular class times and explicit, dated schedule exceptions."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from lecturehub.core import timetable
from lecturehub.core.events import EventStore
from lecturehub.core.ids import new_ulid

_TIME_RANGE = re.compile(
    r"(?<![0-9])(?P<sh>[0-2]?[0-9]):(?P<sm>[0-5][0-9])\s*[-–—~〜～]\s*"
    r"(?P<eh>[0-2]?[0-9]):(?P<em>[0-5][0-9])(?![0-9])"
)


@dataclass(frozen=True)
class ScheduledClass:
    course_id: str
    course: str
    date: str
    start_ms: int
    end_ms: int
    kind: str = "정규"
    note: str = ""


@dataclass(frozen=True)
class TodaySchedule:
    at: str
    current: list[ScheduledClass]
    next: ScheduledClass | None
    classes: list[ScheduledClass]
    warnings: list[str]


@dataclass(frozen=True)
class ExceptionRegistration:
    course_id: str | None
    kind: str
    note: str
    dates: list[str]
    created: list[str]
    existing: list[str]


def resolve_course(connection: sqlite3.Connection, course: str) -> str:
    """Resolve an exact ID, Korean folder name, code or abbreviation uniquely."""
    course = course.strip()
    direct = connection.execute("SELECT id FROM Course WHERE id = ?", (course,)).fetchone()
    if direct:
        return direct[0]
    found = connection.execute(
        "SELECT id FROM Course WHERE folder_name = ? OR code = ? COLLATE NOCASE OR abbr = ? COLLATE NOCASE",
        (course, course, course),
    ).fetchall()
    if len(found) != 1:
        reason = "과목을 찾을 수 없습니다" if not found else "동일한 이름의 과목이 여러 개입니다. 과목 ID를 사용하세요"
        raise ValueError(f"{reason}: {course}")
    return found[0][0]


def exception_dates(
    *, on_date: date | None = None, start: date | None = None, until: date | None = None,
    weekday: int | None = None,
) -> list[date]:
    """Expand one date or an explicitly bounded weekly range, inclusively.

    Semester end dates are never inferred from a Course.term label.
    """
    if on_date is not None:
        if any(value is not None for value in (start, until, weekday)):
            raise ValueError("--date와 --from/--until/--weekday를 함께 사용할 수 없습니다.")
        return [on_date]
    if start is None or until is None or weekday is None:
        raise ValueError("--date 또는 --from, --until, --weekday를 모두 지정하세요. 학기 종료일은 추측하지 않습니다.")
    if not 0 <= weekday <= 6:
        raise ValueError("요일은 월~일 중 하나여야 합니다.")
    if until < start:
        raise ValueError("--until 날짜는 --from 날짜보다 빠를 수 없습니다.")
    current = start + timedelta(days=(weekday - start.weekday()) % 7)
    dates = []
    while current <= until:
        dates.append(current)
        if (until - current).days < 7:
            break
        current += timedelta(days=7)
    if not dates:
        raise ValueError("지정한 기간에 해당 요일이 없습니다.")
    return dates


def add_exceptions(
    connection: sqlite3.Connection, dates: list[date], *, course_id: str | None, kind: str, note: str,
) -> ExceptionRegistration:
    """Register dated user exceptions with Evidence/Event, without committing.

    Identical scope/date/kind/note inputs reuse their IDs and evidence. An
    immediate transaction serializes first-time callers; a savepoint prevents
    partial date ranges from surviving a failed registration.
    """
    if kind not in {"휴강", "보강", "대체", "시간변경"}:
        raise ValueError("종류는 휴강, 보강, 대체, 시간변경 중 하나여야 합니다.")
    note = note.strip()
    if not note:
        raise ValueError("사용자 근거를 남길 --note가 필요합니다.")
    if not dates or any(not isinstance(day, date) or isinstance(day, datetime) for day in dates):
        raise ValueError("유효한 날짜를 하나 이상 지정하세요.")
    if course_id is not None and not connection.execute("SELECT 1 FROM Course WHERE id=?", (course_id,)).fetchone():
        raise ValueError("등록되지 않은 과목 ID입니다.")
    if kind in {"보강", "시간변경"} and (course_id is None or _clock_range(note) is None):
        raise ValueError("보강·시간변경에는 특정 과목과 --note의 HH:MM-HH:MM 시간이 필요합니다.")
    encoded_dates = sorted({day.isoformat() for day in dates})
    created, existing = [], []
    if not connection.in_transaction:
        connection.execute("BEGIN IMMEDIATE")
    connection.execute("SAVEPOINT register_schedule_exceptions")
    try:
        for day in encoded_dates:
            found = connection.execute(
                "SELECT id FROM ScheduleException WHERE course_id IS ? AND date=? AND kind=? AND note=? "
                "ORDER BY id LIMIT 1", (course_id, day, kind, note),
            ).fetchone()
            identity = found[0] if found else new_ulid()
            if found:
                existing.append(identity)
            else:
                connection.execute(
                    "INSERT INTO ScheduleException(id,course_id,date,kind,note) VALUES (?,?,?,?,?)",
                    (identity, course_id, day, kind, note),
                )
                created.append(identity)
            payload = {"course_id": course_id, "date": day, "kind": kind, "note": note, "decided_by": "user"}
            detail = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            evidence = connection.execute(
                "SELECT 1 FROM Evidence WHERE subject_type='ScheduleException' AND subject_id=? "
                "AND kind='user' AND source='user' AND detail=?", (identity, detail),
            ).fetchone()
            if not evidence:
                connection.execute(
                    "INSERT INTO Evidence(id,subject_type,subject_id,kind,detail,source,created_at) "
                    "VALUES (?,'ScheduleException',?,'user',?,'user',?)",
                    (new_ulid(), identity, detail, time.time_ns() // 1_000_000),
                )
                EventStore(connection).append(
                    "SCHEDULE_EXCEPTION_CONFIRMED" if found else "SCHEDULE_EXCEPTION_ADDED",
                    "ScheduleException", identity, payload,
                )
    except BaseException:
        connection.execute("ROLLBACK TO register_schedule_exceptions")
        connection.execute("RELEASE register_schedule_exceptions")
        raise
    connection.execute("RELEASE register_schedule_exceptions")
    return ExceptionRegistration(course_id, kind, note, encoded_dates, created, existing)


def _clock_range(note: str) -> tuple[int, int] | None:
    matches = list(_TIME_RANGE.finditer(note))
    if len(matches) != 1:
        return None
    match = matches[0]
    sh, sm, eh, em = (int(match[key]) for key in ("sh", "sm", "eh", "em"))
    if sh > 23 or eh > 23:
        return None
    start, end = (sh * 60 + sm) * 60_000, (eh * 60 + em) * 60_000
    return (start, end) if end > start else None


def _calendar_closures(connection: sqlite3.Connection, day: date) -> list[tuple[str | None, str | None]]:
    """(course_id, course name) of confirmed whole-day closures on the calendar; course_id None = every course."""
    try:
        rows = connection.execute(
            "SELECT e.course_id, c.folder_name FROM CalendarEvent e LEFT JOIN Course c ON c.id = e.course_id "
            "WHERE e.kind='closure' AND e.status='confirmed' AND e.date_precision='day' AND e.date=?",
            (day.isoformat(),)).fetchall()
    except sqlite3.OperationalError:  # a library from before the calendar (0006)
        return []
    return [(row[0], row[1]) for row in rows]


def _semester_root(connection: sqlite3.Connection) -> Path | None:
    """The semester folder of a library database (<root>/_lecturehub/lecturehub.sqlite); None in memory."""
    try:
        file = next((row[2] for row in connection.execute("PRAGMA database_list") if row[1] == "main"), "")
    except sqlite3.Error:
        return None
    path = Path(file) if file else None
    return path.parent.parent if path and path.parent.name == "_lecturehub" else None


def _day(connection: sqlite3.Connection, day: date) -> tuple[list[ScheduledClass], list[str]]:
    rows = connection.execute(
        "SELECT c.id, c.folder_name, s.start, s.end FROM Schedule s "
        "JOIN Course c ON c.id = s.course_id WHERE s.weekday = ? ORDER BY s.start, c.folder_name",
        (day.weekday(),),
    ).fetchall()
    root = _semester_root(connection)
    if root is not None and not timetable.in_term(root, day):
        rows = []  # TT-001: before 개강 / after 종강 there are no regular classes; dated make-ups below still count
    classes = [ScheduledClass(row[0], row[1], day.isoformat(), row[2], row[3]) for row in rows]
    from .confirmation import INFERRED, schedule_exceptions

    listed = schedule_exceptions(connection, day.isoformat())
    exceptions = [(e["course_id"], e["course"] if e["course_id"] else None, e["kind"], e["note"]) for e in listed]
    # D-052: an exception the user has not confirmed is applied but shown as an estimate.
    warnings = [f"{day.isoformat()} {e['course']} {e['kind']}: 추정 — 사용자 확인 전"
                for e in listed if e["confirmation"] == INFERRED]
    warnings += [f"{day.isoformat()} {e['course']} {e['kind']}: 원격·동영상 강의로 대체될 수 있음(미정) — "
                 "열리면 과목을 골라 PC 소리로 녹음" for e in listed if e["maybe_online"]]
    warnings += [f"{day.isoformat()} {e['course']}: 온라인(영상) 강의로 대체 — 강의실 녹음 없음, "
                 "들을 때 PC 소리로 녹음할 수 있음" for e in listed if e["replacement"] == "online_lecture"]
    # A closure the user confirmed on the calendar (e.g. heard in class, user request 2026-10-30) also cancels
    # that day's regular class. Only a whole-day date counts: "10월 중순" (approx) never removes a class.
    closed = _calendar_closures(connection, day)
    warnings += [f"{day.isoformat()} {course or '전체 과목'} 휴강: 달력에서 확정" for _cid, course in closed]
    if any(cid is None for cid, _course in closed):
        return [], warnings
    classes = [item for item in classes if item.course_id not in {cid for cid, _course in closed}]
    # An explicitly registered all-course closure takes precedence over every
    # regular or make-up class on that day, regardless of insertion order.
    if any(course_id is None and kind == "휴강" for course_id, _course, kind, _note in exceptions):
        return [], warnings
    # Removing the regular occurrences first makes a cancellation plus an
    # explicit make-up session deterministic, independent of insertion order.
    for course_id, _course, kind, _note in exceptions:
        if kind in {"휴강", "대체", "시간변경"}:
            classes = [item for item in classes if course_id is not None and item.course_id != course_id]
    for course_id, course, kind, note in exceptions:
        if kind == "휴강":
            continue
        times = _clock_range(note)
        if times is None or course_id is None:
            if kind != "대체" or times is not None or course_id is None:
                warnings.append(
                    f"{day.isoformat()} {course or '전체 과목'} {kind}: 과목과 명시적 HH:MM-HH:MM 시간이 필요합니다."
                )
            # A dated replacement with no new time cancels its original
            # occurrence; a replacement slot is never guessed from prose.
            continue
        classes.append(ScheduledClass(course_id, course, day.isoformat(), *times, kind=kind, note=note))
    unique = {(item.course_id, item.start_ms, item.end_ms): item for item in classes}
    return sorted(unique.values(), key=lambda item: (item.start_ms, item.course, item.course_id)), warnings


def daily_schedule(connection: sqlite3.Connection, day: date) -> list[ScheduledClass]:
    """Return that date's class occurrences after all registered exceptions.

    Weekdays use Python's Monday=0 convention, and clock times are milliseconds
    after midnight. Explicit HH:MM-HH:MM ranges in exception notes supply
    make-up/replacement times. Recurrence is never inferred from free prose.
    """
    return _day(connection, day)[0]


def today(connection: sqlite3.Connection, now: datetime, *, horizon_days: int = 7) -> TodaySchedule:
    """Return current and next classes in the supplied local wall-clock time.

    Before a class starts it appears as ``next`` (including TODAY-001 at 10:00
    for a 10:05 class). At an exact end time it is no longer current.
    """
    if horizon_days < 0:
        raise ValueError("horizon_days must not be negative")
    classes, warnings = _day(connection, now.date())
    now_ms = ((now.hour * 60 + now.minute) * 60 + now.second) * 1000 + now.microsecond // 1000
    current = [item for item in classes if item.start_ms <= now_ms < item.end_ms]
    upcoming = [item for item in classes if item.start_ms > now_ms]
    for offset in range(1, horizon_days + 1):
        future, notes = _day(connection, now.date() + timedelta(days=offset))
        warnings.extend(notes)
        upcoming.extend(future)
        if upcoming:
            break
    return TodaySchedule(now.isoformat(), current, upcoming[0] if upcoming else None, classes, warnings)
