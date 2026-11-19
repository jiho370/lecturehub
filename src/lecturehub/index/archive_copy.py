"""D-055: copy a linked app recording into 26-2\\녹음본\\<과목>\\ (add-only).

The recording stays under _lecturehub\\recordings (D-050); this adds a copy
with the September naming ("26.9.29 구조역학.mp3" — the master's own extension; a
second one that day " 2"). Existing files are never overwritten, moved or deleted: a name that is
taken by a different file moves on to the next number, and a name that already
holds the same bytes is simply registered. The copy is checked by sha256 and
registered as a path alias of the same MediaAsset. Only linked recordings are
copied; a pending ("미확정") one waits for the user's confirmation. A failure
here never affects the recording and is retried at the next app start.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import time
from datetime import date, datetime
from pathlib import Path

from lecturehub.core.events import EventStore
from lecturehub.core.ids import new_ulid
from lecturehub.core.timeutil import KST

ARCHIVE = "녹음본"
APP_ORIGINS = ("app_mic", "app_loopback")
ALIAS_KIND = "source_alias"
ROLE = "archive_copy_D055"
OVERLONG_MS = 15 * 60_000  # D-057: a recording running this far past the class end waits for its range
MIN_COPY_MS = 60_000  # a recording shorter than this (a mis-press, 9/29 13:59: 2 s) is not copied to 녹음본


def range_hold(connection: sqlite3.Connection, media_id: str) -> tuple[str | None, str]:
    """(reason to wait, media id to copy). D-057: 녹음본 holds only the trimmed file, so a recording that runs
    15+ minutes past the timetable end waits until its range is decided (trimmed, or "the end is right")."""
    row = connection.execute(
        "SELECT m.recorded_start, m.duration_ms, l.date, l.course_id FROM MediaAsset m "
        "JOIN Lecture l ON l.id = m.lecture_id WHERE m.id = ?", (media_id,)).fetchone()
    if row is None:
        return None, media_id
    try:
        from .media_range import decided_range, suggest  # trim track (D-057)
    except ImportError:
        decided_range = suggest = None
    decided = decided_range(connection, media_id) if decided_range else None
    if decided:
        return None, decided.get("media_id_to_use") or media_id
    if suggest:
        # Also catches a class that ended early: 15+ min of recording after the last lecture speech.
        proposal = suggest(connection, media_id)
        if proposal.get("ask"):
            return (f"녹음 끝 부분에 수업이 아닌 구간이 길어 보임({proposal.get('reason', '')}) — 끝 지점을 정하면"
                    "(자르기 또는 '끝까지 맞음') 그 녹음을 복사합니다"), media_id
    from .schedule import daily_schedule

    started, duration, day, course_id = row
    end_local = datetime.fromtimestamp((started + duration) / 1000, KST)  # timetable is KST
    classes = [c for c in daily_schedule(connection, date.fromisoformat(day)) if c.course_id == course_id]
    if not classes or end_local.date().isoformat() != day:
        return None, media_id
    class_end = max(c.end_ms for c in classes)
    end_ms = ((end_local.hour * 60 + end_local.minute) * 60 + end_local.second) * 1000
    over = end_ms - class_end
    if over >= OVERLONG_MS:
        return (f"수업 끝 시각보다 {over // 60_000}분 길게 녹음됨 — 끝 지점을 정하면(자르기 또는 '끝까지 맞음') "
                "그 녹음을 복사합니다"), media_id
    return None, media_id


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def archive_name(day: date, course: str, n: int = 1, suffix: str = ".m4a", label: str = "") -> str:
    """"26.9.29 구조역학.mp3"; a PC-sound recording gets " (PC 소리)" so the two files of one class stay apart."""
    return f"{day.year % 100}.{day.month}.{day.day} {course}{label}{f' {n}' if n > 1 else ''}{suffix}"


def copied_path(connection: sqlite3.Connection, media_id: str) -> str | None:
    for (detail,) in connection.execute(
            "SELECT detail FROM Evidence WHERE subject_type='MediaAsset' AND subject_id=? AND kind=? "
            "ORDER BY created_at DESC, id DESC",
            (media_id, ALIAS_KIND)):
        data = json.loads(detail)
        if data.get("role") == ROLE:
            return data["path"]
    return None


def _register(connection, media_id: str, dest: Path, digest: str) -> None:
    connection.execute(
        "INSERT INTO Evidence(id,subject_type,subject_id,kind,detail,source,created_at) VALUES (?,?,?,?,?,?,?)",
        (new_ulid(), "MediaAsset", media_id, ALIAS_KIND,
         json.dumps({"path": str(dest), "role": ROLE, "sha256": digest}, ensure_ascii=False, sort_keys=True),
         "metadata", time.time_ns() // 1_000_000))
    EventStore(connection).append("ARCHIVE_COPIED", "MediaAsset", media_id, {"path": str(dest)})


def copy_media(connection: sqlite3.Connection, root: Path, media_id: str) -> dict:
    from lecturehub.core import settings

    if not settings.folder_layout()["archive_copies"]:  # 기본 layout: the recording stays in _lecturehub only
        return {"state": "skipped", "reason": "폴더 방식 설정에서 녹음본 복사가 꺼져 있습니다"}
    return _copy_media(connection, root, media_id)


def _copy_media(connection: sqlite3.Connection, root: Path, media_id: str) -> dict:
    """Copy one linked app recording; the caller commits."""
    row = connection.execute(
        "SELECT m.path, m.sha256, m.origin, l.date, c.folder_name, m.duration_ms FROM MediaAsset m "
        "JOIN Lecture l ON l.id = m.lecture_id JOIN Course c ON c.id = l.course_id WHERE m.id = ?",
        (media_id,)).fetchone()
    if row is None:
        return {"state": "skipped", "reason": "회차에 연결되지 않은 녹음은 복사하지 않습니다(미확정)"}
    source, digest, origin, day, course = Path(row[0]), row[1], row[2], date.fromisoformat(row[3]), row[4]
    if origin not in APP_ORIGINS:
        return {"state": "skipped", "reason": "앱 녹음만 복사합니다"}
    if row[5] < MIN_COPY_MS:
        return {"state": "skipped", "reason": "1분보다 짧은 녹음은 녹음본에 복사하지 않습니다"}
    done = copied_path(connection, media_id)
    if done and Path(done).is_file():
        return {"state": "already", "path": done}
    hold, use_id = range_hold(connection, media_id)
    if hold:
        return {"state": "held", "reason": hold}
    if use_id != media_id:  # the trimmed (derived) recording is the one that goes to 녹음본
        derived = connection.execute("SELECT path, sha256 FROM MediaAsset WHERE id=?", (use_id,)).fetchone()
        if derived is None:
            return {"state": "failed", "reason": "잘라 둔 녹음을 찾지 못했습니다"}
        source, digest = Path(derived[0]), derived[1]
    if not source.is_file():
        return {"state": "failed", "reason": f"원본 녹음이 없습니다: {source}"}
    from lecturehub.core.extra_courses import ARCHIVE_GROUP, is_extra

    folder = Path(root) / ARCHIVE / (Path(ARCHIVE_GROUP) / course if is_extra(root, course) else Path(course))
    folder.mkdir(parents=True, exist_ok=True)
    for n in range(1, 100):
        label = " (PC 소리)" if origin == "app_loopback" else ""
        dest = folder / archive_name(day, course, n, source.suffix.lower() or ".m4a", label)
        if dest.exists():
            if dest.is_file() and dest.stat().st_size == source.stat().st_size and _sha256(dest) == digest:
                _register(connection, media_id, dest, digest)  # the same recording is already there
                return {"state": "already", "path": str(dest)}
            continue  # someone else's file: never overwrite
        try:
            out = dest.open("xb")  # exclusive create: add-only
        except FileExistsError:
            continue
        try:
            with source.open("rb") as src, out:
                shutil.copyfileobj(src, out, 1024 * 1024)
                out.flush()
                os.fsync(out.fileno())
        except OSError as exc:
            dest.unlink(missing_ok=True)  # our own incomplete copy, created just above
            return {"state": "failed", "reason": f"복사 실패(다음 실행 때 다시 시도): {exc}"}
        if _sha256(dest) != digest:
            dest.unlink()  # our own incomplete copy, created just above
            return {"state": "failed", "reason": "복사본 해시가 원본과 다릅니다(다음 실행 때 다시 시도)"}
        _register(connection, media_id, dest, digest)
        return {"state": "copied", "path": str(dest)}
    return {"state": "failed", "reason": "같은 날 이름이 모두 사용 중입니다"}


def pending(connection: sqlite3.Connection) -> list[str]:
    """Linked app recordings without an archive copy yet (none while the folder layout turns copies off)."""
    from lecturehub.core import settings

    if not settings.folder_layout()["archive_copies"]:
        return []
    rows = connection.execute(
        "SELECT id FROM MediaAsset WHERE lecture_id IS NOT NULL AND is_master = 1 AND origin IN (?, ?) "
        f"AND duration_ms >= {MIN_COPY_MS} ORDER BY recorded_start",
        APP_ORIGINS).fetchall()
    from .discard import hidden_ids

    gone = hidden_ids(connection)  # D-058: a thrown-away recording is never copied; a joined piece neither
    return [r[0] for r in rows if r[0] not in gone and copied_path(connection, r[0]) is None]
