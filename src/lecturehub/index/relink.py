"""Move a recording to another lecture — another class day or a '수업 외' topic (user fix of a wrong link).

MediaAsset.lecture_id is linkage, not audio fact, so it may change (the audio columns stay locked).
Its transcripts move with it only when the user allows it in the confirmation (``with_transcripts``):
each gets an Evidence ``user_relink`` (source user) first, which migration 0007 requires before a
transcript's lecture may change; the text and file identity never change. Both lectures' "in use"
pointers are refreshed. Things built on the transcript inside the old lecture (note citations,
corrections, canonical transcript, calendar candidates) block the move instead of being left behind.
A wrong 녹음본 copy (D-055) goes to the Recycle Bin when its bytes still match what was copied,
and the next archive run copies it into the right folder.

The link is a database change and does not wait for the Recycle Bin (release review 2026-11-16): when the
old copy cannot be recycled (a USB or network drive has no bin, the file is open elsewhere) or is not known
to be the bytes that were copied, it stays where it is, the link moves anyway, and the result names the file
that was left (``copy_left``) and why (``copy_note``). Nothing is deleted here. While that copy is still
registered and in place, ``archive_copy`` counts it as the recording's copy and makes no second one in the
new folder.
"""
from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable
from datetime import date
from pathlib import Path

from lecturehub.core import extra_courses
from lecturehub.core.events import append
from lecturehub.core.ids import new_ulid


def _dependents(connection: sqlite3.Connection, transcript_ids: list[str]) -> list[str]:
    """What was built on these transcripts and would stay behind in the old lecture."""
    found = []
    for label, sql in (
            ("노트 인용", "SELECT count(*) FROM NoteCitation WHERE transcript_id = ?"),
            ("교정", "SELECT count(*) FROM Correction WHERE status = 'active' AND anchor LIKE '%' || ? || '%'"),
            ("통합 전사", "SELECT count(*) FROM CanonicalTranscript WHERE inputs LIKE '%' || ? || '%'"),
            ("달력 후보", ("SELECT count(*) FROM CalendarEvent WHERE status <> 'cancelled' AND source = 'transcript' "
                         "AND source_ref LIKE '%' || ? || '%'"))):
        count = sum(connection.execute(sql, (tid,)).fetchone()[0] for tid in transcript_ids)
        if count:
            found.append(f"{label} {count}건")
    return found


def move(connection: sqlite3.Connection, root: Path, media_id: str, course: str, day: str, *,
         with_transcripts: bool = False, trash: Callable[[list[Path]], None] | None = None) -> dict:
    """Relink a master recording (and its derived cuts). Caller commits.

    A recording with transcripts is left untouched and ``{"moved": False, "confirm": {...}}`` comes back
    unless ``with_transcripts`` (the user's yes to moving them too).

    About the old 녹음본 copy a moved result says one of: ``recycled_copy`` (its path: it is in the Recycle Bin),
    ``copy_left`` (its path: it is still in the old folder) with ``copy_note`` (a short Korean reason to show),
    or neither (there was no copy). ``copy_note`` without ``copy_left`` means recycling failed and the file is
    no longer in its place."""
    from .archive_copy import copied_path
    from .discard import _sha256, _transcribing
    from .lectures import refresh_policy
    from .wav_cleanup import to_recycle_bin

    row = connection.execute("SELECT lecture_id FROM MediaAsset WHERE id = ? AND is_master = 1", (media_id,)).fetchone()
    from .discard import hidden_ids

    if row is None or media_id in hidden_ids(connection):
        raise ValueError("옮길 녹음을 찾지 못했습니다")
    date.fromisoformat(day)
    transcripts = [r[0] for r in connection.execute(
        "SELECT id FROM Transcript WHERE media_id = ? OR media_id IN (SELECT id FROM MediaAsset WHERE derived_from = ?) "
        "ORDER BY id", (media_id, media_id))]
    if transcripts:
        blockers = _dependents(connection, transcripts)
        if blockers:
            raise ValueError("이 녹음의 전사로 만든 것이 옛 회차에 있어 옮기지 않습니다: " + ", ".join(blockers))
        audio = {str(Path(r[0])) for r in connection.execute(
            "SELECT path FROM MediaAsset WHERE id = ? OR derived_from = ?", (media_id, media_id))}
        if _transcribing(root, audio | {str(Path(a).resolve()) for a in audio}):
            raise ValueError("이 녹음의 전사가 진행 중입니다. 끝난 뒤 다시 누르세요")
        if not with_transcripts:
            return {"lecture_id": row[0], "moved": False, "confirm": {"transcripts": len(transcripts)}}
    if extra_courses.is_extra(root, course):
        from .extra import ensure_course, new_lecture

        target = new_lecture(connection, ensure_course(connection, course), day)
    else:
        course_row = connection.execute("SELECT id FROM Course WHERE folder_name = ?", (course,)).fetchone()
        if course_row is None:
            raise ValueError(f"없는 과목: {course}")
        from .app_ingest import _lecture_for

        target = _lecture_for(connection, course_row[0], day)
    if target == row[0]:
        return {"lecture_id": target, "moved": False}
    connection.execute("UPDATE MediaAsset SET lecture_id = ? WHERE id = ? OR derived_from = ?", (target, media_id, media_id))
    now = time.time_ns() // 1_000_000
    for transcript_id in transcripts:  # the user's yes, written first: migration 0007 checks it
        old = connection.execute("SELECT lecture_id FROM Transcript WHERE id = ?", (transcript_id,)).fetchone()[0]
        connection.execute(
            "INSERT INTO Evidence (id, subject_type, subject_id, field, kind, detail, weight, source, created_at) "
            "VALUES (?, 'Transcript', ?, 'lecture_id', 'user_relink', ?, NULL, 'user', ?)",
            (new_ulid(), transcript_id, json.dumps({"from": old, "to": target, "with_media": media_id,
                                                    "decided_by": "user"}), now))
        connection.execute("UPDATE Transcript SET lecture_id = ? WHERE id = ?", (target, transcript_id))
        append(connection, "TRANSCRIPT_RELINKED", "Transcript", transcript_id, {"from": old, "to": target})
    if transcripts:
        refresh_policy(connection, row[0])  # the old lecture falls back to what it has left
        refresh_policy(connection, target)
    trashed, left, note = None, None, None
    old_copy = copied_path(connection, media_id)
    if old_copy and Path(old_copy).is_file():
        recorded = None
        for (detail,) in connection.execute("SELECT detail FROM Evidence WHERE subject_type='MediaAsset' AND "
                                            "subject_id=? AND kind='source_alias'", (media_id,)):
            data = json.loads(detail)
            if data.get("path") == old_copy:
                recorded = data.get("sha256")
        try:  # the link has moved; the old copy is housekeeping and must not undo it
            if recorded and _sha256(Path(old_copy)) == recorded:
                (trash or to_recycle_bin)([Path(old_copy)])
                trashed = old_copy
            else:
                note = "복사해 둔 파일과 내용이 같은지 확인되지 않아 그대로 두었습니다."
        except OSError as exc:  # no Recycle Bin on this drive (NoRecycleBin), file in use, unreadable, ...
            note = str(exc)
        if trashed is None and Path(old_copy).exists():
            left = old_copy
    connection.execute(
        "INSERT INTO Evidence (id, subject_type, subject_id, field, kind, detail, weight, source, created_at) "
        "VALUES (?, 'MediaAsset', ?, 'lecture_id', 'user_relink', ?, NULL, 'user', ?)",
        (new_ulid(), media_id, json.dumps({"from": row[0], "to": target, "course": course, "date": day,
                                           "recycled_copy": trashed, "copy_left": left, "copy_note": note},
                                          ensure_ascii=False), time.time_ns() // 1_000_000))
    append(connection, "MEDIA_RELINKED", "MediaAsset", media_id,
           {"from": row[0], "to": target, "recycled_copy": trashed, "copy_left": left})
    return {"lecture_id": target, "moved": True, "recycled_copy": trashed, "copy_left": left, "copy_note": note,
            "transcripts": len(transcripts)}
