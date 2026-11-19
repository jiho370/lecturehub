"""Attach a finished HTML note to a lecture, and let the user name a lecture (user request 2026-10-02).

Notes made outside the app (the organized HTML notes) are found by indexing only when they sit in a
timetable course folder and their file name carries the date. A note of an outside-class talk, or one
the user simply points at, is attached here instead: the file stays where it is, and a StudyArtifact
row links it to the lecture so the lecture view opens it. Attaching the same file again after it was
rebuilt keeps one row and records the new content hash.

A lecture's own name ("무료 특강 1강") is an Evidence ``user_title`` (source user). A named lecture shows
its name instead of "n강" and does not take a number, so the other lectures of the course still count
1강, 2강, … without a gap.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from lecturehub.core.events import append
from lecturehub.core.ids import new_ulid
from lecturehub.parsers.dedupe import sha256_file

TITLE = "user_title"
HTML = (".html", ".htm")


def _now() -> int:
    return time.time_ns() // 1_000_000


def _evidence(connection: sqlite3.Connection, subject_type: str, subject_id: str, kind: str, detail: dict,
              field: str | None = None) -> None:
    connection.execute(
        "INSERT INTO Evidence(id,subject_type,subject_id,field,kind,detail,source,created_at) VALUES (?,?,?,?,?,?,?,?)",
        (new_ulid(), subject_type, subject_id, field, kind, json.dumps(detail, ensure_ascii=False), "user", _now()))


def attach(connection: sqlite3.Connection, root: Path, lecture_id: str, path: str | Path) -> dict:
    """Link an HTML note file inside the semester folder to a lecture. Caller commits."""
    root, note = Path(root).resolve(), Path(path).resolve()
    if connection.execute("SELECT 1 FROM Lecture WHERE id = ?", (lecture_id,)).fetchone() is None:
        raise ValueError("없는 회차입니다.")
    if not note.is_file() or note.suffix.lower() not in HTML:
        raise ValueError("HTML 노트 파일이 아닙니다.")
    if not note.is_relative_to(root) or "_lecturehub" in (p.lower() for p in note.relative_to(root).parts):
        raise ValueError("학기 폴더 안(앱 폴더 밖)의 노트만 연결합니다.")
    digest = sha256_file(note)
    row = connection.execute("SELECT id, lecture_ids, inputs, path FROM StudyArtifact WHERE path = ? AND kind = 'note'",
                             (str(note),)).fetchone()
    if row is None:
        row = connection.execute(
            "SELECT id, lecture_ids, inputs, path FROM StudyArtifact WHERE kind = 'note' AND "
            "json_extract(inputs,'$.source_sha256') = ?", (digest,)).fetchone()
    if row is not None:
        artifact_id, lectures, inputs = row[0], json.loads(row[1]), json.loads(row[2])
        changed = inputs.get("source_sha256") != digest
        added = lecture_id not in lectures
        # the same file was moved (old path gone): follow it instead of keeping a dead link
        moved = row[3] != str(note) and not Path(row[3]).exists()
        if added:
            lectures = sorted({*lectures, lecture_id})
        if changed or added or moved:
            connection.execute("UPDATE StudyArtifact SET lecture_ids = ?, inputs = ?, path = ? WHERE id = ?",
                               (json.dumps(lectures), json.dumps({**inputs, "source_sha256": digest}), str(note),
                                artifact_id))
            detail = {"lecture_id": lecture_id, "sha256": digest, "content_changed": changed}
            if moved:
                detail["moved_from"] = row[3]
            _evidence(connection, "StudyArtifact", artifact_id, "user_attached", detail)
        state = "updated" if changed else "linked" if added else "moved" if moved else "already"
        return {"artifact_id": artifact_id, "state": state, "lecture_ids": lectures}
    artifact_id = new_ulid()
    connection.execute(
        "INSERT INTO StudyArtifact (id, lecture_ids, path, kind, version, parent_version, build_script_path, mode, "
        "inputs, created_by, qa_status) VALUES (?,?,?,'note',1,NULL,NULL,'new',?,'user_attach','unverified')",
        (artifact_id, json.dumps([lecture_id]), str(note), json.dumps({"source_sha256": digest})))
    _evidence(connection, "StudyArtifact", artifact_id, "user_attached", {"lecture_id": lecture_id, "sha256": digest})
    append(connection, "STUDY_ARTIFACT_INDEXED", "StudyArtifact", artifact_id, {"lecture_ids": [lecture_id], "by": "attach"})
    return {"artifact_id": artifact_id, "state": "attached", "lecture_ids": [lecture_id]}


NAMEABLE = ("clova", "daglo")  # tools whose text exports come from outside the app


def attach_transcript(connection: sqlite3.Connection, root: Path, lecture_id: str, path: str | Path, *,
                      engine: str | None = None, origin: str = "") -> dict:
    """Link a transcript text file the user got from someone else (a classmate's export) to a lecture.

    The lecture needs no recording. The file stays where it is. ``engine`` is the tool the user names when
    the file's layout alone reads as another one (a Clova Note export without its header reads like Daglo);
    both the layout that was read and the name given are written down. Caller commits."""
    from lecturehub.parsers import parse_file

    from .lectures import refresh_policy

    root, source = Path(root).resolve(), Path(path).resolve()
    if connection.execute("SELECT 1 FROM Lecture WHERE id = ?", (lecture_id,)).fetchone() is None:
        raise ValueError("없는 회차입니다.")
    if not source.is_file() or source.suffix.lower() != ".txt":
        raise ValueError("전사 텍스트(.txt) 파일이 아닙니다.")
    if not source.is_relative_to(root) or "_lecturehub" in (p.lower() for p in source.relative_to(root).parts):
        raise ValueError("학기 폴더 안(앱 폴더 밖)의 전사 파일만 연결합니다.")
    digest = sha256_file(source)
    existing = connection.execute("SELECT id, lecture_id FROM Transcript WHERE sha256 = ?", (digest,)).fetchone()
    if existing is not None:
        if existing[1] != lecture_id:
            raise ValueError("같은 내용의 전사가 이미 다른 회차에 있습니다.")
        return {"transcript_id": existing[0], "state": "already", "lecture_id": lecture_id}
    parsed = parse_file(source)
    if parsed.engine == "unknown" or not parsed.segments:
        raise ValueError("전사 형식을 읽지 못했습니다(시각과 문단이 있는 클로바·다글로 형식만).")
    named = engine or parsed.engine
    if parsed.engine == "live_captions" and named == "live_captions":
        pass  # the app's own live captions, saved where the user keeps transcripts
    elif named not in NAMEABLE or parsed.engine not in NAMEABLE:
        raise ValueError(f"여기서 연결할 수 있는 것은 {', '.join(NAMEABLE)} 형식의 받은 전사뿐입니다.")
    transcript_id = new_ulid()
    connection.execute(
        "INSERT INTO Transcript (id,lecture_id,media_id,engine,engine_version,role,role_decided_by,chunk_plan_id,"
        "status,source_file,sha256) VALUES (?,?,NULL,?,NULL,'timeline','policy',NULL,'done',?,?)",
        (transcript_id, lecture_id, named, str(source), digest))
    for seq, segment in enumerate(parsed.segments):
        connection.execute("INSERT INTO TranscriptSegment (transcript_id,seq,start_ms,end_ms,speaker,text,chunk_index) "
                           "VALUES (?,?,?,?,?,?,0)", (transcript_id, seq, segment.start_ms, segment.end_ms,
                                                      segment.speaker, segment.text))
    _evidence(connection, "Transcript", transcript_id, "user_attached", {
        "lecture_id": lecture_id, "sha256": digest, "layout_read_as": parsed.engine, "engine_named": named,
        "named_by_user": bool(engine), "origin": origin, "no_recording": True})
    append(connection, "TRANSCRIPT_IMPORTED", "Transcript", transcript_id,
           {"lecture_id": lecture_id, "engine": named, "by": "attach"})
    changed = refresh_policy(connection, lecture_id)
    return {"transcript_id": transcript_id, "state": "attached", "lecture_id": lecture_id, "engine": named,
            "segments": len(parsed.segments), "in_use_changed": bool(changed)}


def lecture_on(connection: sqlite3.Connection, course: str, day: str) -> str:
    """The lecture of this course on this day, made when there is none yet (a talk with live captions but no
    recording). Several lectures that day: refused, the user picks one. Caller commits."""
    from datetime import date

    date.fromisoformat(day)
    row = connection.execute("SELECT id FROM Course WHERE folder_name = ?", (course,)).fetchone()
    if row is None:
        raise ValueError(f"없는 과목입니다: {course}")
    found = connection.execute("SELECT id FROM Lecture WHERE course_id = ? AND date = ? ORDER BY session_index, id",
                               (row[0], day)).fetchall()
    if len(found) > 1:
        raise ValueError("그 날짜에 같은 과목 회차가 여러 개입니다. 회차 번호로 연결하세요.")
    if found:
        return found[0][0]
    lecture_id = new_ulid()
    connection.execute("INSERT INTO Lecture (id, course_id, date, session_index, title, lecture_type) "
                       "VALUES (?, ?, ?, 1, NULL, NULL)", (lecture_id, row[0], day))
    append(connection, "LECTURE_CREATED", "Lecture", lecture_id, {"course_id": row[0], "date": day, "by": "captions_attach"})
    return lecture_id


def set_title(connection: sqlite3.Connection, lecture_id: str, title: str) -> dict:
    """Name a lecture (empty = back to its number). Caller commits."""
    if connection.execute("SELECT 1 FROM Lecture WHERE id = ?", (lecture_id,)).fetchone() is None:
        raise ValueError("없는 회차입니다.")
    title = " ".join(str(title or "").split())
    if len(title) > 40:
        raise ValueError("회차 이름은 40자까지입니다.")
    _evidence(connection, "Lecture", lecture_id, TITLE, {"title": title}, field="title")
    return {"lecture_id": lecture_id, "title": title or None}


def titles(connection: sqlite3.Connection) -> dict[str, str]:
    """Lecture id → the name the user gave it (the latest; an empty name removes it)."""
    named: dict[str, str] = {}
    for lecture_id, detail in connection.execute(
            "SELECT subject_id, detail FROM Evidence WHERE subject_type = 'Lecture' AND kind = ? AND source = 'user' "
            "ORDER BY created_at, id", (TITLE,)):
        title = json.loads(detail).get("title") or ""
        if title:
            named[lecture_id] = title
        else:
            named.pop(lecture_id, None)
    return named
