"""Inferred vs user-confirmed vs synthetic-test authority (D-052, §0.4).

A confirmation state is an append-only Evidence row (field='confirmation');
the latest one wins. Nothing here edits the subject row or older evidence.
Without any confirmation record a subject counts as ``inferred``: a value
someone typed on the user's behalf, or derived from a calendar, is not a
user confirmation until the user confirms it (in the app or with --by-user).
"""
from __future__ import annotations

import json
import sqlite3
import time

from lecturehub.core.events import EventStore
from lecturehub.core.ids import new_ulid

FIELD = "confirmation"
INFERRED, CONFIRMED, SYNTHETIC = "inferred", "confirmed_by_user", "synthetic_test"
STATES = (INFERRED, CONFIRMED, SYNTHETIC)
_SOURCE = {INFERRED: "agent", CONFIRMED: "user", SYNTHETIC: "agent"}


def record(connection: sqlite3.Connection, subject_type: str, subject_id: str, state: str, reason: str) -> str:
    """Append a confirmation state; the caller commits."""
    if state not in STATES:
        raise ValueError(f"state must be one of {STATES}")
    if not reason.strip():
        raise ValueError("a reason is required")
    evidence_id = new_ulid()
    connection.execute(
        "INSERT INTO Evidence(id,subject_type,subject_id,field,kind,detail,source,created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (evidence_id, subject_type, subject_id, FIELD, state, reason.strip(), _SOURCE[state],
         time.time_ns() // 1_000_000),
    )
    EventStore(connection).append("CONFIRMATION_RECORDED", subject_type, subject_id,
                                  {"state": state, "reason": reason.strip(), "evidence_id": evidence_id})
    return evidence_id


def state_of(connection: sqlite3.Connection, subject_type: str, subject_id: str) -> str:
    row = connection.execute(
        "SELECT kind FROM Evidence WHERE subject_type=? AND subject_id=? AND field=? "
        "ORDER BY created_at DESC, id DESC LIMIT 1", (subject_type, subject_id, FIELD)).fetchone()
    return row[0] if row else INFERRED


# How a closed classroom session is replaced. MAYBE_ONLINE: a remote/video lecture may replace it, undecided
# (user's word). ONLINE: a notice says it is an online (video) lecture — no classroom recording that day.
REPLACEMENT_FIELD, MAYBE_ONLINE, ONLINE = "replacement", "possible_online_undecided", "online_lecture"


def record_replacement(connection: sqlite3.Connection, exception_id: str, kind: str, reason: str,
                       source: str = "user") -> str:
    """Append how the session is replaced; the caller commits."""
    if kind not in (MAYBE_ONLINE, ONLINE):
        raise ValueError(f"kind must be {MAYBE_ONLINE} or {ONLINE}")
    if not reason.strip():
        raise ValueError("a reason is required")
    evidence_id = new_ulid()
    connection.execute(
        "INSERT INTO Evidence(id,subject_type,subject_id,field,kind,detail,source,created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (evidence_id, "ScheduleException", exception_id, REPLACEMENT_FIELD, kind, reason.strip(), source,
         time.time_ns() // 1_000_000),
    )
    return evidence_id


def record_maybe_online(connection: sqlite3.Connection, exception_id: str, reason: str) -> str:
    """Mark a closure as "may be replaced by a remote/video lecture — undecided" (user's word). Caller commits."""
    return record_replacement(connection, exception_id, MAYBE_ONLINE, reason)


def replacement_of(connection: sqlite3.Connection, exception_id: str) -> str | None:
    row = connection.execute(
        "SELECT kind FROM Evidence WHERE subject_type='ScheduleException' AND subject_id=? AND field=? "
        "ORDER BY created_at DESC, id DESC LIMIT 1", (exception_id, REPLACEMENT_FIELD)).fetchone()
    return row[0] if row else None


def maybe_online(connection: sqlite3.Connection, exception_id: str) -> bool:
    return replacement_of(connection, exception_id) == MAYBE_ONLINE


def record_statement(connection: sqlite3.Connection, subject_type: str, subject_id: str, claim: str,
                     refs: list[dict], certainty: str) -> str:
    """Transcript evidence for a schedule claim: transcript ids and segment numbers only, never the words
    (lecture speech, student names). ``claim`` is our own one-line summary. Caller commits."""
    evidence_id = new_ulid()
    detail = json.dumps({"claim": claim, "refs": refs, "certainty": certainty}, ensure_ascii=False, sort_keys=True)
    connection.execute(
        "INSERT INTO Evidence(id,subject_type,subject_id,kind,detail,source,created_at) VALUES (?,?,?,?,?,?,?)",
        (evidence_id, subject_type, subject_id, "transcript_statement", detail, "engine", time.time_ns() // 1_000_000))
    return evidence_id


def schedule_exceptions(connection: sqlite3.Connection, day: str | None = None) -> list[dict]:
    query = ("SELECT e.id, e.date, e.kind, e.note, e.course_id, c.folder_name FROM ScheduleException e "
             "LEFT JOIN Course c ON c.id = e.course_id")
    rows = connection.execute(query + (" WHERE e.date = ?" if day else "") + " ORDER BY e.date, e.id",
                              (day,) if day else ()).fetchall()
    return [{"id": r[0], "date": r[1], "kind": r[2], "note": r[3], "course_id": r[4],
             "course": r[5] or "전체 과목", "confirmation": state_of(connection, "ScheduleException", r[0]),
             "maybe_online": maybe_online(connection, r[0]), "replacement": replacement_of(connection, r[0])}
            for r in rows]
