"""Recordings whose Google transcript is done go to the Recycle Bin after a while (user decision 2026-11-13).

A lecture's app recording (the MP3 master and, if still there, the lossless ``segments.flac``) is sent to the
Recycle Bin once
- the lecture has a finished ``gemini_transcribe`` transcript whose file is older than ``days`` (default 7), and
- the lecture has a note (an evidence note or an attached HTML note).
Only app recordings in ``_lecturehub/recordings`` are touched: imported files and 녹음본 archive copies stay.
Nothing is deleted for good (emptying the bin is the user's job), and database rows stay; the lecture view then
shows the recording as missing. Never runs while recording (the caller checks).
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from lecturehub.core import settings
from lecturehub.core.ids import new_ulid

APP_ORIGINS = ("app_mic", "app_loopback")
DEFAULT_DAYS = settings.AUDIO_CLEANUP_DAYS


options = settings.audio_cleanup  # kept in core.settings: the settings screen must not import this package (D-050)


def _has_note(connection: sqlite3.Connection, lecture_id: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM StudyArtifact a WHERE a.kind = 'note' AND EXISTS "
        "(SELECT 1 FROM json_each(a.lecture_ids) j WHERE j.value = ?) LIMIT 1", (lecture_id,)).fetchone() is not None


def _google_done_at(connection: sqlite3.Connection, lecture_id: str) -> float | None:
    """When the newest finished Google transcript file of the lecture was written (seconds), else None."""
    newest = None
    for (source,) in connection.execute(
            "SELECT source_file FROM Transcript WHERE lecture_id = ? AND engine = 'gemini_transcribe' "
            "AND source_file IS NOT NULL", (lecture_id,)):
        try:
            stamp = Path(source).stat().st_mtime
        except OSError:
            continue
        newest = stamp if newest is None or stamp > newest else newest
    return newest


def candidates(connection: sqlite3.Connection, root: Path, *, now: float | None = None, days: int = DEFAULT_DAYS) -> list[dict]:
    """App recordings that may go to the Recycle Bin now, with the files that would go."""
    now = time.time() if now is None else now
    recordings = (Path(root) / "_lecturehub" / "recordings").resolve()
    out = []
    rows = connection.execute(
        "SELECT id, lecture_id, path FROM MediaAsset WHERE is_master = 1 AND lecture_id IS NOT NULL "
        "AND origin IN (?, ?) ORDER BY recorded_start", APP_ORIGINS).fetchall()
    for media_id, lecture_id, path in rows:
        master = Path(path)
        try:
            inside = master.resolve().is_relative_to(recordings)
        except OSError:
            continue
        if not inside or not master.is_file():
            continue
        done = _google_done_at(connection, lecture_id)
        if done is None or now - done < days * 86400 or not _has_note(connection, lecture_id):
            continue
        files = [master] + [p for p in (master.parent / "segments.flac",) if p.is_file()]
        out.append({"media_id": media_id, "lecture_id": lecture_id, "files": [str(p) for p in files],
                    "bytes": sum(p.stat().st_size for p in files)})
    return out


def run(connection: sqlite3.Connection, root: Path, *, now: float | None = None, trash=None) -> dict:
    """Send the candidates to the Recycle Bin and note it on each recording. The caller commits."""
    opts = options()
    if not opts["enabled"]:
        return {"state": "off", "recycled": 0, "bytes": 0}
    if trash is None:
        from lecturehub.core.recycle_bin import to_recycle_bin as trash
    done, freed, errors = 0, 0, []
    for item in candidates(connection, root, now=now, days=opts["days"]):
        try:
            trash([Path(p) for p in item["files"]])
        except OSError as exc:
            errors.append(f"{Path(item['files'][0]).name}: {exc}")
            continue
        connection.execute(
            "INSERT INTO Evidence(id,subject_type,subject_id,field,kind,detail,source,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (new_ulid(), "MediaAsset", item["media_id"], "path", "recycled_after_transcript",
             json.dumps({"files": item["files"], "days": opts["days"]}, ensure_ascii=False), "user",
             time.time_ns() // 1_000_000))
        done += 1
        freed += item["bytes"]
    return {"state": "done", "recycled": done, "bytes": freed, "errors": errors}
