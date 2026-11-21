"""Calendar: exams, quizzes, assignments, closures and personal events (user request 2026-09-29).

Three sources, never mixed up:
- user: typed in the app (confirmed).
- etl: assignment deadlines read from eTL by the browser agent into _lecturehub/etl/assignments.json;
  the app only imports that file (read-only, no browser control). Marked "eTL", not a user confirmation.
- transcript: a date spoken in class, found by rule. It stays a candidate with its quote until the user
  presses [확정] (D-052); exam/assignment statements are also kept as ExamCue rows.
Rows are never deleted (status cancelled); every change is an Event.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from lecturehub.core.textio import read_text_any
from lecturehub.core.events import EventStore
from lecturehub.core.ids import new_ulid

KINDS = ("exam", "quiz", "assignment", "closure", "personal", "other")
KIND_LABEL = {"exam": "시험", "quiz": "퀴즈", "assignment": "과제", "closure": "휴강", "personal": "개인", "other": "기타"}
FIELDS = ("course_id", "kind", "title", "date", "date_precision", "date_text", "start_ms", "end_ms", "note")


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _clock_ms(text: str | None) -> int | None:
    if not text:
        return None
    hour, minute = (int(part) for part in text.split(":"))
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ValueError(f"시각 형식 오류: {text}")
    return (hour * 60 + minute) * 60_000


def _check(values: dict) -> None:
    if values.get("kind") not in KINDS:
        raise ValueError(f"종류는 {', '.join(KINDS)} 중 하나입니다")
    if not str(values.get("title") or "").strip():
        raise ValueError("제목이 필요합니다")
    date.fromisoformat(values["date"])


def add(connection: sqlite3.Connection, *, kind: str, title: str, day: str, course_id: str | None = None,
        start: str | None = None, end: str | None = None, note: str = "", source: str = "user",
        source_ref: str | None = None, status: str = "confirmed", date_precision: str = "day",
        date_text: str | None = None) -> str:
    """Insert one event; the caller commits."""
    values = {"kind": kind, "title": title.strip(), "date": day}
    _check(values)
    event_id, now = new_ulid(), _now_ms()
    connection.execute(
        "INSERT INTO CalendarEvent (id,course_id,kind,title,date,date_precision,date_text,start_ms,end_ms,note,source,"
        "source_ref,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (event_id, course_id, kind, title.strip(), day, date_precision, date_text, _clock_ms(start), _clock_ms(end),
         note or "", source, source_ref, status, now, now))
    EventStore(connection).append("CALENDAR_ADDED", "CalendarEvent", event_id,
                                  {"kind": kind, "date": day, "source": source, "status": status})
    return event_id


def update(connection: sqlite3.Connection, event_id: str, **changes) -> None:
    """Change fields of a user/eTL event (or edit a candidate before confirming). Caller commits."""
    row = connection.execute("SELECT * FROM CalendarEvent WHERE id=?", (event_id,)).fetchone()
    if row is None:
        raise ValueError("없는 일정입니다")
    columns = [d[0] for d in connection.execute("SELECT * FROM CalendarEvent LIMIT 0").description]
    current = dict(zip(columns, row, strict=True))
    if "start" in changes:
        changes["start_ms"] = _clock_ms(changes.pop("start"))
    if "end" in changes:
        changes["end_ms"] = _clock_ms(changes.pop("end"))
    if "day" in changes:
        changes["date"] = changes.pop("day")
    unknown = set(changes) - set(FIELDS)
    if unknown:
        raise ValueError(f"바꿀 수 없는 항목: {sorted(unknown)}")
    merged = {**current, **changes}
    _check(merged)
    before = {k: current[k] for k in changes}
    sets = ", ".join(f"{k}=?" for k in changes)
    connection.execute(f"UPDATE CalendarEvent SET {sets}, updated_at=? WHERE id=?",
                       (*changes.values(), _now_ms(), event_id))
    EventStore(connection).append("CALENDAR_CHANGED", "CalendarEvent", event_id, {"before": before, "after": changes})


def set_status(connection: sqlite3.Connection, event_id: str, status: str, reason: str = "") -> None:
    if status not in ("confirmed", "cancelled"):
        raise ValueError("status must be confirmed or cancelled")
    row = connection.execute("SELECT status, source FROM CalendarEvent WHERE id=?", (event_id,)).fetchone()
    if row is None:
        raise ValueError("없는 일정입니다")
    connection.execute("UPDATE CalendarEvent SET status=?, updated_at=? WHERE id=?", (status, _now_ms(), event_id))
    EventStore(connection).append("CALENDAR_STATUS", "CalendarEvent", event_id,
                                  {"from": row[0], "to": status, "reason": reason, "by": "user"})


def _course_names(connection) -> dict[str, str]:
    return dict(connection.execute("SELECT id, folder_name FROM Course"))


def _row_dict(connection, row, columns) -> dict:
    item = dict(zip(columns, row, strict=True))
    item["course"] = _course_names(connection).get(item["course_id"]) if item["course_id"] else None
    item["kind_label"] = KIND_LABEL[item["kind"]]
    return item


def events(connection: sqlite3.Connection, start: str, end: str, *, status: tuple = ("confirmed",)) -> list[dict]:
    """Events with start <= date <= end (inclusive) and the given statuses, ordered by date/time."""
    marks = ",".join("?" * len(status))
    cursor = connection.execute(
        f"SELECT * FROM CalendarEvent WHERE date BETWEEN ? AND ? AND status IN ({marks}) "
        "ORDER BY date, coalesce(start_ms, -1), title", (start, end, *status))
    columns = [d[0] for d in cursor.description]
    return [_row_dict(connection, row, columns) for row in cursor.fetchall()]


def candidates(connection: sqlite3.Connection) -> list[dict]:
    """Dates spoken in class waiting for the user's [확정]/[무시], with their quote and place in the transcript."""
    cursor = connection.execute("SELECT * FROM CalendarEvent WHERE status='candidate' ORDER BY date")
    columns = [d[0] for d in cursor.description]
    items = [_row_dict(connection, row, columns) for row in cursor.fetchall()]
    for item in items:
        evidence = connection.execute(
            "SELECT detail FROM Evidence WHERE subject_type='CalendarEvent' AND subject_id=? "
            "AND kind IN ('transcript_quote','board_quote') "
            "ORDER BY created_at LIMIT 1", (item["id"],)).fetchone()
        item["evidence"] = json.loads(evidence[0]) if evidence else None
    return items


def next_exam(connection: sqlite3.Connection, today: date) -> dict | None:
    found = connection.execute(
        "SELECT id FROM CalendarEvent WHERE status='confirmed' AND kind IN ('exam','quiz') AND date >= ? "
        "ORDER BY date, coalesce(start_ms, -1) LIMIT 1", (today.isoformat(),)).fetchone()
    if not found:
        return None
    item = events(connection, "0000-01-01", "9999-12-31")
    item = next(e for e in item if e["id"] == found[0])
    item["d_day"] = (date.fromisoformat(item["date"]) - today).days
    return item


# ------------------------------------------------------------------ eTL import (read-only file)
def import_etl(connection: sqlite3.Connection, path: Path) -> dict:
    """Import assignment deadlines from _lecturehub/etl/assignments.json.

    Expected: [{"id"?, "course": folder/code/abbr, "title", "due": "YYYY-MM-DD[THH:MM]", "url"?}, ...].
    New items are added (source etl), changed due dates update the row (Event), nothing is deleted.
    """
    from .schedule import resolve_course

    if not Path(path).is_file():
        return {"added": 0, "updated": 0, "skipped": [], "missing": True}
    items = json.loads(read_text_any(path))
    added = updated = 0
    skipped = []
    for item in items:
        try:
            course_id = resolve_course(connection, str(item["course"]))
            title = str(item["title"]).strip()
            due = str(item["due"])
            day, _, clock = due.partition("T")
            date.fromisoformat(day)
            ref = str(item.get("id") or f"{course_id}|{title}")
        except (KeyError, ValueError) as exc:
            skipped.append({"item": item.get("title") if isinstance(item, dict) else None, "reason": str(exc)})
            continue
        existing = connection.execute("SELECT id, date, start_ms FROM CalendarEvent WHERE source='etl' AND source_ref=?",
                                      (ref,)).fetchone()
        start = clock[:5] or None
        note = f"eTL: {item['url']}" if item.get("url") else "eTL 과제"
        if existing is None:
            add(connection, kind="assignment", title=title, day=day, course_id=course_id, start=start, note=note,
                source="etl", source_ref=ref)
            added += 1
        elif existing[1] != day or existing[2] != _clock_ms(start):
            update(connection, existing[0], day=day, start=start)
            updated += 1
    return {"added": added, "updated": updated, "skipped": skipped, "missing": False}


# ------------------------------------------------------------------ dates spoken in class (rule candidates)
_KIND_WORDS = [
    ("exam", re.compile(r"중간\s*고사|기말\s*고사|중간고사|기말고사|시험")),
    ("quiz", re.compile(r"퀴즈|쪽지\s*시험")),
    # 과제/레포트 must be named: bare 제출·마감·보고서 are course content in e.g. accounting ("12월 31일 결산 마감")
    ("assignment", re.compile(r"과제|레포트|리포트")),
    ("closure", re.compile(r"휴강|학회|수업(을|이)?\s*(못|없|안\s*하)|보강")),
]
_ABS = re.compile(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일")
_PART = re.compile(r"(\d{1,2})\s*월\s*(초|중순|말)")
_REL = re.compile(r"(다음\s*주|다다음\s*주|이번\s*주)\s*([월화수목금토일])요일")
_PART_DAY = {"초": 5, "중순": 15, "말": 25}
_WEEKDAYS = "월화수목금토일"


@dataclass
class Spoken:
    kind: str
    day: date
    precision: str
    text: str


_SENTENCE = re.compile(r"(?<=[.?!。])\s+|\n+")
NEAR_CHARS = 40          # the date and the exam/assignment/closure word must be this close in one sentence
HORIZON_DAYS = 150       # a schedule date is on/after the lecture and within the semester


def spoken_dates(text: str, lecture_day: date) -> list[Spoken]:
    """Dates spoken next to an exam/quiz/assignment/closure word, in the same sentence (real-data check 9/29:
    segment-level matching picked up accounting examples like "12월 31일 결산"). Relative days use the lecture date;
    dates before the lecture or beyond the semester horizon are dropped."""
    year = lecture_day.year

    def fix_year(month: int) -> int:  # a spoken month well before the lecture month means next year (Dec → Jan)
        return year + 1 if month < lecture_day.month - 6 else year

    found: list[Spoken] = []
    for sentence in _SENTENCE.split(text):
        kinds = [(m.start(), m.end(), k) for k, pattern in _KIND_WORDS for m in pattern.finditer(sentence)]
        if not kinds:
            continue
        dates: list[tuple[int, int, date, str, str]] = []
        for m in _ABS.finditer(sentence):
            month, day_no = int(m.group(1)), int(m.group(2))
            try:
                dates.append((m.start(), m.end(), date(fix_year(month), month, day_no), "day", m.group(0)))
            except ValueError:
                continue
        for m in _PART.finditer(sentence):
            month = int(m.group(1))
            if 1 <= month <= 12:
                dates.append((m.start(), m.end(), date(fix_year(month), month, _PART_DAY[m.group(2)]), "approx",
                              m.group(0)))
        for m in _REL.finditer(sentence):
            weeks = {"이번": 0, "다음": 1, "다다음": 2}[re.sub(r"\s*주", "", m.group(1))]
            monday = lecture_day - timedelta(days=lecture_day.weekday()) + timedelta(weeks=weeks)
            dates.append((m.start(), m.end(), monday + timedelta(days=_WEEKDAYS.index(m.group(2))), "day", m.group(0)))
        for start, end, day, precision, wording in dates:
            if not (lecture_day <= day <= lecture_day + timedelta(days=HORIZON_DAYS)):
                continue
            near = [(max(ks - end, start - ke, 0), k) for ks, ke, k in kinds]
            gap, kind = min(near)
            if gap <= NEAR_CHARS:
                found.append(Spoken(kind, day, precision, wording))
    return found


def scan_transcripts(connection: sqlite3.Connection, lecture_ids: list[str] | None = None) -> dict:
    """Find spoken dates in each lecture's in-use transcript; add candidates (never confirmed) + ExamCue rows."""
    from . import lectures

    where = f" WHERE l.id IN ({','.join('?' * len(lecture_ids))})" if lecture_ids else ""
    rows = connection.execute("SELECT l.id, l.date, l.course_id FROM Lecture l" + where,
                              tuple(lecture_ids or ())).fetchall()
    added = 0
    for lecture_id, day, course_id in rows:
        pointer = lectures.in_use(connection, lecture_id)
        if not pointer:
            continue
        others = [r[0] for r in connection.execute("SELECT id FROM Transcript WHERE lecture_id=? AND id<>?",
                                                   (lecture_id, pointer["transcript_id"]))]
        segs = connection.execute("SELECT seq, start_ms, text FROM TranscriptSegment WHERE transcript_id=? ORDER BY seq",
                                  (pointer["transcript_id"],)).fetchall()
        for index, (seq, start_ms, text) in enumerate(segs):
            for spoken in spoken_dates(text, date.fromisoformat(day)):
                ref = f"{pointer['transcript_id']}#{seq}#{spoken.text}"
                if connection.execute("SELECT 1 FROM CalendarEvent WHERE source='transcript' AND source_ref=?",
                                      (ref,)).fetchone():
                    continue
                agree = 1 + sum(1 for other in others if connection.execute(
                    "SELECT 1 FROM TranscriptSegment WHERE transcript_id=? AND start_ms BETWEEN ? AND ? AND text LIKE ?",
                    (other, (start_ms or 0) - 90_000, (start_ms or 0) + 90_000, f"%{spoken.text.split()[0]}%"),
                ).fetchone())
                title = f"{KIND_LABEL[spoken.kind]} — 수업 중 언급({day[5:].replace('-', '/')})"
                event_id = add(connection, kind=spoken.kind, title=title, day=spoken.day.isoformat(),
                               course_id=course_id, note="수업 중 발언에서 찾은 후보(확인 필요)", source="transcript",
                               source_ref=ref, status="candidate", date_precision=spoken.precision,
                               date_text=spoken.text)
                before = segs[index - 1][2] if index else ""
                after = segs[index + 1][2] if index + 1 < len(segs) else ""
                connection.execute(
                    "INSERT INTO Evidence(id,subject_type,subject_id,kind,detail,source,created_at) VALUES (?,?,?,?,?,?,?)",
                    (new_ulid(), "CalendarEvent", event_id, "transcript_quote", json.dumps({
                        "lecture_id": lecture_id, "transcript_id": pointer["transcript_id"], "seq": seq,
                        "at_ms": start_ms, "quote": text, "engines_agree": agree}, ensure_ascii=False),
                     "engine", _now_ms()))
                if spoken.kind in ("exam", "quiz", "assignment"):
                    connection.execute(
                        "INSERT INTO ExamCue (id,lecture_id,at_ms,kind,quote,context_before,context_after,material_page,"
                        "engines_agree,source,confidence_class,decided_by) VALUES (?,?,?,?,?,?,?,NULL,?,?,?, 'rule')",
                        (new_ulid(), lecture_id, start_ms or 0,
                         "assignment" if spoken.kind == "assignment" else "scope_notice", text, before, after, agree,
                         pointer["transcript_id"], "medium" if agree > 1 else "low"))
                added += 1
    return {"candidates_added": added}


def month_view(connection: sqlite3.Connection, year: int, month: int, today: date) -> dict:
    """Everything the calendar screen needs for one month (+ upcoming list and candidates)."""
    from .confirmation import schedule_exceptions

    first = date(year, month, 1)
    last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    closures = [e for e in schedule_exceptions(connection) if first.isoformat() <= e["date"] <= last.isoformat()]
    upcoming = events(connection, today.isoformat(), (today + timedelta(days=60)).isoformat())
    for item in upcoming:
        item["d_day"] = (date.fromisoformat(item["date"]) - today).days
    return {"year": year, "month": month, "events": events(connection, first.isoformat(), last.isoformat()),
            "exceptions": closures, "upcoming": upcoming, "candidates": candidates(connection),
            "next_exam": next_exam(connection, today), "today": today.isoformat()}
