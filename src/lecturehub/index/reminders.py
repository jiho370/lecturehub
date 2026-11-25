"""D-079: light, local reminders over stored lists; no recorder, network or AI imports.

The caller supplies the clock and toast. Only reminders_sent.json is written;
the library connection is read-only and never initializes or indexes a semester.
"""
from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import asdict
from datetime import datetime, time, timedelta
from pathlib import Path

from lecturehub.core.fileio import atomic_write_json
from lecturehub.core.reminder_settings import (  # noqa: F401 - re-exported
    SETTING,
    audible,
    preferences,
    set_preferences,
)

from . import calendar_events, etl_submissions, schedule

_LOCK = threading.Lock()
DEADLINES = {"assignment", "report", "presentation"}
EXAMS = {"exam", "quiz"}
BRIEFING_UNTIL = 14  # the morning briefing toast is shown from 07:00 until 14:00, once a day


def _when(day: str, ms: int | None, now: datetime) -> datetime:
    # A date-only deadline stays ahead through 23:59; a date-only exam's
    # morning slot is explicitly 08:00, independently of this end-of-day bound.
    midnight = datetime.combine(datetime.fromisoformat(day).date(), time(), tzinfo=now.tzinfo)
    return midnight + timedelta(milliseconds=ms if ms is not None else 86_340_000)


def items(connection, root: Path, now: datetime) -> list[dict]:
    out = []
    for row in etl_submissions.items(root):
        if etl_submissions.state(row.get("submission")) in etl_submissions.DONE:
            continue
        try:
            stamp = row.get("due") or ""
            timed = "T" in stamp or " " in stamp
            due = datetime.fromisoformat(stamp)
            due = due.replace(tzinfo=etl_submissions.KST) if due.tzinfo is None else due
            due = due.astimezone(now.tzinfo)
            if not timed:
                due = _when(stamp, None, now)
        except (ValueError, TypeError):
            continue
        out.append({"id": f"etl:{row.get('course')}:{row['id']}", "course": row.get("course"),
                    "title": row.get("title") or "과제", "kind": "assignment", "at": due,
                    "timed": timed})
    for row in calendar_events.events(connection, now.date().isoformat(), (now.date() + timedelta(days=8)).isoformat()):
        # eTL's stored list is authoritative, including submission and changed due.
        imported_assignment = row["source"] == "etl" and row["kind"] == "assignment" \
            and not str(row["source_ref"] or "").startswith(("ain:", "lxb:"))
        if imported_assignment or row["kind"] not in DEADLINES | EXAMS or row["date_precision"] != "day":
            continue
        try:
            due = _when(row["date"], row["start_ms"], now)
        except (ValueError, TypeError, OverflowError):
            continue
        out.append({"id": f"calendar:{row['id']}", "course": row["course"], "title": row["title"],
                    "kind": row["kind"], "at": due, "timed": row["start_ms"] is not None})
    return sorted(out, key=lambda r: (r["at"], r["id"]))


def _view(row: dict) -> dict:
    return {**row, "at": row["at"].isoformat()}


def today(connection, root: Path, now: datetime, *, listed=None) -> dict:
    rows = items(connection, root, now) if listed is None else listed
    return {"date": now.date().isoformat(),
            "classes": [asdict(c) for c in schedule.daily_schedule(connection, now.date())],
            "dues": [_view(r) for r in rows if r["kind"] in DEADLINES and now < r["at"] <= now + timedelta(hours=24)],
            "exams": [_view(r) for r in rows if r["kind"] in EXAMS and now < r["at"] <= now + timedelta(days=7)]}


def _slots(row: dict):
    due = row["at"]
    if row["kind"] in DEADLINES:
        return [("24h", due - timedelta(hours=24)), ("3h", due - timedelta(hours=3))]
    morning = due.replace(hour=8, minute=0, second=0, microsecond=0)
    return [("eve21", morning.replace(hour=21) - timedelta(days=1)),
            ("1h" if row["timed"] else "8am", due - timedelta(hours=1) if row["timed"] else morning)]


def briefing(view: dict, muted: set[str]) -> str:
    classes = [r for r in view["classes"] if r["course"] not in muted]
    dues = [r for r in view["dues"] if r.get("course") not in muted and r["at"][:10] == view["date"]]
    exams = [r for r in view["exams"] if r.get("course") not in muted and r["at"][:10] == view["date"]]
    first = classes[0] if classes else None
    clock = first["start_ms"] // 60_000 if first else 0
    early = f"(첫 수업 {clock // 60:02}:{clock % 60:02} {first['course']})" if first else ""
    exam = " · ".join(f"{r.get('course') or ''} {r['title']}".strip() for r in exams)
    return f"오늘: 수업 {len(classes)}개{early} · 마감 {len(dues)}개 · 시험 {len(exams)}개" + (f"({exam})" if exam else "")


def tick(connection, root: Path, *, clock, toast) -> int:
    """One background tick, at most three toasts including the overflow summary.

    An absent slot is caught up only for <2h, with its due still ahead. State is
    persisted before emission: a crash may lose a toast, but cannot flood a restart.
    Disabled/muted slots are also consumed so re-enabling does not replay them.
    Errors belong to this optional feature and never escape to recording.
    """
    if not _LOCK.acquire(blocking=False):
        return 0
    try:
        return _tick(connection, Path(root), clock(), toast)
    except Exception:  # noqa: BLE001 - next tick retries; recording never waits for this feature
        from lecturehub.core import applog

        applog.swallowed("마감·시험 알림")
        return 0
    finally:
        _LOCK.release()


def _tick(connection, root: Path, now: datetime, toast) -> int:
    path = root / "_lecturehub" / "reminders_sent.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        raw = {}
    raw = raw if isinstance(raw, dict) else {}
    sent = raw.get("sent")
    sent = {k: v for k, v in sent.items() if isinstance(v, int) and v > int(now.timestamp() * 1000) - 14 * 86400_000} \
        if isinstance(sent, dict) else {}
    prefs, rows = preferences(), items(connection, root, now)
    muted = set(prefs["muted_courses"])
    pending, changed = [], False
    for row in rows:
        due = row["at"]
        if due <= now:
            continue
        for slot, at in _slots(row):
            key = hashlib.sha256(f"{row['id']}|{slot}|{due.isoformat()}".encode()).hexdigest()
            if key in sent or at > now:
                continue
            sent[key], changed = int(now.timestamp() * 1000), True
            if now - at >= timedelta(hours=2) or not prefs["deadlines"] or row.get("course") in muted:
                continue
            label = calendar_events.KIND_LABEL[row["kind"]]
            when = due.strftime("%m/%d %H:%M") if row["timed"] else due.strftime("%m/%d (시각 미정)")
            pending.append((f"{label} 알림", f"{row.get('course') or ''} · {row['title']} · {when}".lstrip(" ·")))
    day = now.date().isoformat()
    brief_day = raw.get("briefing_day")
    if now.hour >= 7 and brief_day != day:
        brief_day, changed = day, True
        if prefs["briefing"] and now.hour < BRIEFING_UNTIL:  # a first start late in the day is no "morning" briefing
            pending.append(("오늘 일정", briefing(today(connection, root, now, listed=rows), muted)))
    if changed:
        atomic_write_json(path, {"sent": sent, "briefing_day": brief_day})
    batches = pending if len(pending) <= 3 else pending[:2] + [("일정 알림 모음", f"그 밖에 {len(pending) - 2}건 · " +
               " / ".join(message for _title, message in pending[2:]))]
    for title, message in batches:
        try:
            toast(title, message)
        except Exception:  # noqa: BLE001 - one failed Windows call cannot interrupt the others
            pass
    return len(batches)
