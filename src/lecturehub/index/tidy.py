"""Empty lectures that the index once made from the name of a 녹음본 copy (user request 2026-10-09).

Before the index knew a 녹음본 file as the D-055 copy of an app recording, it made a lecture from
the file name first ("26.9.29 토질역학 2") and only then found the audio already registered under the
recording's own lecture. That left a lecture with nothing in it. ``empty_copy_lectures`` finds
exactly those: made from a file name, named like a recorded copy, and referred to by nothing else.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from lecturehub.core.events import EventStore

from .archive_copy import ROLE


def _copy_stems(connection: sqlite3.Connection) -> set[tuple[str, str]]:
    """(course folder, file stem) of every 녹음본 copy the app ever made, also ones later recycled."""
    stems = set()
    for (detail,) in connection.execute(
            "SELECT detail FROM Evidence WHERE subject_type = 'MediaAsset' AND kind = 'source_alias'"):
        try:
            data = json.loads(detail)
        except ValueError:
            continue
        if data.get("role") == ROLE and data.get("path"):
            path = Path(data["path"])
            stems.add((path.parent.name, path.stem))
    return stems


def _other_references(connection: sqlite3.Connection, lecture_id: str, own_evidence: set[str]) -> list[str]:
    """Every row anywhere that names this lecture, except the lecture itself and its file-name evidence."""
    found = []
    tables = [r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    for table in tables:
        columns = [r[1] for r in connection.execute(f'PRAGMA table_info("{table}")')]
        for column in columns:
            if table == "Lecture" and column == "id":
                continue
            try:
                rows = connection.execute(
                    f'SELECT * FROM "{table}" WHERE CAST("{column}" AS TEXT) LIKE ?', (f"%{lecture_id}%",)).fetchall()
            except sqlite3.Error:
                found.append(f"{table}.{column} (읽지 못함)")  # a check that could not run keeps the lecture
                continue
            for row in rows:
                if table == "Evidence" and row[0] in own_evidence:
                    continue
                found.append(f"{table}.{column}")
    return sorted(set(found))


def empty_copy_lectures(connection: sqlite3.Connection) -> list[dict]:
    stems = _copy_stems(connection)
    out = []
    for lecture_id, title, day, folder in connection.execute(
            "SELECT l.id, l.title, l.date, c.folder_name FROM Lecture l JOIN Course c ON c.id = l.course_id "
            "WHERE l.title IS NOT NULL ORDER BY l.date, l.session_index, l.id").fetchall():
        if (folder, title) not in stems:
            continue
        own = {r[0] for r in connection.execute(
            "SELECT id FROM Evidence WHERE subject_type = 'Lecture' AND subject_id = ? AND kind = 'filename_keyword'",
            (lecture_id,))}
        if not own:
            continue
        others = _other_references(connection, lecture_id, own)
        out.append({"lecture_id": lecture_id, "course": folder, "date": day, "title": title,
                    "removable": not others, "kept_because": others})
    return out


def remove_empty_copy_lectures(connection: sqlite3.Connection) -> list[dict]:
    """Remove the ones nothing refers to; the removal is written to the event log. The caller commits."""
    removed = []
    for item in empty_copy_lectures(connection):
        if not item["removable"]:
            continue
        connection.execute("DELETE FROM Evidence WHERE subject_type = 'Lecture' AND subject_id = ? "
                           "AND kind = 'filename_keyword'", (item["lecture_id"],))
        connection.execute("DELETE FROM Lecture WHERE id = ?", (item["lecture_id"],))
        EventStore(connection).append("LECTURE_REMOVED", "Lecture", item["lecture_id"], {
            "course": item["course"], "date": item["date"], "title": item["title"],
            "reason": "empty lecture made from the name of a 녹음본 copy", "by": "user_request"})
        removed.append(item)
    return removed
