"""Transcripts live in 26-2\\전사본\\<과목>\\ (user decision 2026-11-14: "애초에 저기에 저장해야지").

A finished job writes its txt under ``_lecturehub\\out`` (the job does not know the lecture yet); the moment it
is registered to a lecture (``app_ingest``) the file is moved here with the September naming —
``26.9.29 구조역학.txt`` (Google), ``26.9.29 구조역학 (Whisper).txt``, ``26.9.29 구조역학 (MAI).txt``, a second
lecture that day ``26.9.29 토질역학 2 (Whisper).txt``. Nothing in 전사본 is ever replaced: a taken name moves on
to `` (2)``. ``move_registered`` brings transcripts registered earlier from ``_lecturehub\\out`` here (migration
0008 lets the path change once an Evidence ``user_file_move`` names it; the bytes are checked first).
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from datetime import date
from pathlib import Path

from lecturehub.core.ids import new_ulid

FOLDER = "전사본"
LABELS = {"gemini_transcribe": "", "whisper": " (Whisper)", "mai": " (MAI)", "clova": " (클로바)", "daglo": " (다글로)"}
_MAI_RUN = re.compile(r" \(MAI( \d+)?\)")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def label(engine: str, filename: str = "") -> str:
    if engine == "mai":
        found = _MAI_RUN.search(filename)
        return found.group(0) if found else LABELS["mai"]
    return LABELS.get(engine, f" ({engine})")


def lecture_stem(connection: sqlite3.Connection, lecture_id: str) -> tuple[str, str] | None:
    """(course folder, "yy.M.d 과목[ N]") of a lecture, or None."""
    row = connection.execute("SELECT c.folder_name, l.date, l.session_index FROM Lecture l "
                             "JOIN Course c ON c.id = l.course_id WHERE l.id = ?", (lecture_id,)).fetchone()
    if row is None or not row[0]:
        return None
    day = date.fromisoformat(row[1])
    number = f" {row[2]}" if row[2] and row[2] > 1 else ""
    return row[0], f"{day.year % 100}.{day.month}.{day.day} {row[0]}{number}"


def destination(root: Path, connection: sqlite3.Connection, lecture_id: str, engine: str, filename: str) -> Path | None:
    """A free name in 전사본\\<과목> for this lecture and engine (never one that exists)."""
    found = lecture_stem(connection, lecture_id)
    if found is None:
        return None
    course, stem = found
    folder = Path(root) / FOLDER / course
    base = f"{stem}{label(engine, filename)}"
    target, n = folder / f"{base}.txt", 2
    while target.exists():
        target, n = folder / f"{base} ({n}).txt", n + 1
    return target


def is_app_output(root: Path, path: Path) -> bool:
    try:
        Path(path).resolve().relative_to((Path(root) / "_lecturehub" / "out").resolve())
        return True
    except ValueError:
        return False


def place(root: Path, connection: sqlite3.Connection, lecture_id: str, engine: str, source: Path) -> Path:
    """Move a job output into 전사본\\<과목> before it is registered. Returns where it is now (unchanged when the
    lecture has no course folder)."""
    from .etl_files import _move_no_clobber

    target = destination(root, connection, lecture_id, engine, Path(source).name)
    if target is None:
        return Path(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    digest = _sha256(source)
    _move_no_clobber(Path(source), target)
    if _sha256(target) != digest:
        raise ValueError(f"옮긴 전사본의 내용이 다릅니다: {target}")
    return target


def move_registered(connection: sqlite3.Connection, root: Path, *, dry_run: bool = False,
                    course: str | None = None) -> list[dict]:
    """Transcripts registered from ``_lecturehub\\out`` → 전사본\\<과목> (bytes checked). The caller commits."""
    from .etl_files import _move_no_clobber

    moved = []
    rows = connection.execute("SELECT id, lecture_id, engine, source_file, sha256 FROM Transcript "
                              "WHERE source_file IS NOT NULL ORDER BY id").fetchall()
    for transcript_id, lecture_id, engine, source_file, digest in rows:
        source = Path(source_file)
        if not is_app_output(root, source) or not source.is_file():
            continue
        if _sha256(source) != digest:
            moved.append({"id": transcript_id, "from": str(source), "state": "skipped", "reason": "내용이 등록 때와 다름"})
            continue
        found = lecture_stem(connection, lecture_id)
        if found is None or (course and found[0] != course):
            continue
        target = destination(root, connection, lecture_id, engine, source.name)
        if target is None:
            continue
        entry = {"id": transcript_id, "from": str(source), "to": str(target), "state": "planned" if dry_run else "moved"}
        if not dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            _move_no_clobber(source, target)
            connection.execute(
                "INSERT INTO Evidence (id, subject_type, subject_id, field, kind, detail, weight, source, created_at) "
                "VALUES (?, 'Transcript', ?, 'source_file', 'user_file_move', ?, NULL, 'user', ?)",
                (new_ulid(), transcript_id, json.dumps({"from": str(source), "to": str(target), "decided_by": "user"},
                                                       ensure_ascii=False), time.time_ns() // 1_000_000))
            connection.execute("UPDATE Transcript SET source_file = ? WHERE id = ?", (str(target), transcript_id))
        moved.append(entry)
    return moved
