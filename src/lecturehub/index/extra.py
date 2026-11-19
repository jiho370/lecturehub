"""DB side of '수업 외' topics: a Course row marked kind=extra (Evidence), made when first needed."""
from __future__ import annotations

import json
import sqlite3
import time

from lecturehub.core.events import append
from lecturehub.core.ids import new_ulid

KIND = "course_kind"


def extra_course_ids(connection: sqlite3.Connection) -> set[str]:
    return {r[0] for r in connection.execute(
        "SELECT subject_id FROM Evidence WHERE subject_type = 'Course' AND kind = ? AND detail LIKE '%\"extra\"%'",
        (KIND,))}


def ensure_course(connection: sqlite3.Connection, name: str) -> str:
    """The Course id for this topic, creating it (kind=extra) the first time. Caller commits."""
    row = connection.execute("SELECT id FROM Course WHERE folder_name = ?", (name,)).fetchone()
    if row:
        return row[0]
    term = connection.execute("SELECT term FROM Course ORDER BY rowid LIMIT 1").fetchone()
    course_id = new_ulid()
    connection.execute("INSERT INTO Course (id, code, folder_name, abbr, term) VALUES (?, 'EXTRA', ?, 'ex', ?)",
                       (course_id, name, term[0] if term else ""))
    connection.execute(
        "INSERT INTO Evidence (id, subject_type, subject_id, field, kind, detail, weight, source, created_at) "
        "VALUES (?, 'Course', ?, NULL, ?, ?, NULL, 'user', ?)",
        (new_ulid(), course_id, KIND, json.dumps({"kind": "extra", "decided_by": "user"}), time.time_ns() // 1_000_000))
    append(connection, "COURSE_CREATED", "Course", course_id, {"name": name, "kind": "extra"})
    return course_id


def new_lecture(connection: sqlite3.Connection, course_id: str, day: str) -> str:
    """Outside-class recordings each get their own lecture (several talks a day are normal)."""
    n = connection.execute("SELECT coalesce(max(session_index), 0) FROM Lecture WHERE course_id = ? AND date = ?",
                           (course_id, day)).fetchone()[0] + 1
    lecture_id = new_ulid()
    connection.execute("INSERT INTO Lecture (id, course_id, date, session_index, title, lecture_type) "
                       "VALUES (?, ?, ?, ?, NULL, NULL)", (lecture_id, course_id, day, n))
    append(connection, "LECTURE_CREATED", "Lecture", lecture_id, {"course_id": course_id, "date": day,
                                                                  "by": "app_recording", "kind": "extra"})
    return lecture_id
