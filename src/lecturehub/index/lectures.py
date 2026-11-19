"""Lecture-centred reads and the transcript-in-use pointer (§0.1, D-049).

Everything the app shows about one lecture comes from here: its recordings,
transcripts (with engine badges), materials, notes, the transcript in use and
what the user can do next. Only ``choose_in_use``/``refresh_policy`` write, and
they only append TranscriptInUse rows.
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import date, timedelta
from pathlib import Path

from lecturehub.core.events import EventStore
from lecturehub.core.ids import new_ulid

# D-049 default order (settable later): user choice > canonical > MAI > Google > Daglo/Clova > Whisper.
ENGINE_RANK = {"canonical": 0, "mai": 1, "gemini_transcribe": 2, "daglo": 3, "clova": 3, "whisper": 4,
               "live_captions": 5}
ENGINE_LABEL = {"canonical": "통합본", "mai": "MAI", "gemini_transcribe": "Google", "daglo": "다글로",
                "clova": "클로바", "whisper": "Whisper", "live_captions": "실시간 자막"}
WEEKDAYS = "월화수목금토일"


def engine_label(engine: str) -> str:
    return ENGINE_LABEL.get(engine, engine)


# ------------------------------------------------------------------ pointer
def _transcripts(connection: sqlite3.Connection, lecture_id: str) -> list[dict]:
    rows = connection.execute(
        "SELECT t.id, t.engine, t.status, t.media_id, t.source_file, t.role, "
        "(SELECT count(*) FROM TranscriptSegment s WHERE s.transcript_id = t.id) AS segments, "
        "(SELECT max(start_ms) FROM TranscriptSegment s WHERE s.transcript_id = t.id) AS last_ms "
        "FROM Transcript t WHERE t.lecture_id = ? ORDER BY t.id", (lecture_id,)).fetchall()
    qa: dict[str, list[str]] = {}
    for subject_id, detail in connection.execute(
            "SELECT subject_id, detail FROM Evidence WHERE subject_type='Transcript' AND kind='qa_warning' "
            "AND subject_id IN (SELECT id FROM Transcript WHERE lecture_id = ?)", (lecture_id,)):
        try:
            qa.setdefault(subject_id, []).extend(json.loads(detail).get("checks", []))
        except ValueError:
            pass
    from .discard import hidden_ids

    gone = hidden_ids(connection)  # D-058: a thrown-away (or joined, index/join.py) recording's transcripts are hidden
    return [{"id": r[0], "engine": r[1], "label": engine_label(r[1]), "status": r[2], "media_id": r[3],
             "source_file": r[4], "role": r[5], "segments": r[6], "last_ms": r[7], "qa": sorted(set(qa.get(r[0], [])))}
            for r in rows if r[3] not in gone]


def policy_pick(transcripts: list[dict]) -> dict | None:
    """Best usable transcript by engine rank; more segments breaks ties, then the newer one (ids are ULIDs, so a
    re-transcription with ``--again`` wins over the run it repeats). Deterministic."""
    usable = [t for t in transcripts if t["segments"] and t["engine"] in ENGINE_RANK]
    if not usable:
        return None
    best = min((ENGINE_RANK[t["engine"]], -t["segments"]) for t in usable)
    return max((t for t in usable if (ENGINE_RANK[t["engine"]], -t["segments"]) == best), key=lambda t: t["id"])


def _latest_row(connection: sqlite3.Connection, lecture_id: str):
    return connection.execute(
        "SELECT transcript_id, decided_by, reason, created_at, id FROM TranscriptInUse WHERE lecture_id = ? "
        "ORDER BY created_at DESC, id DESC LIMIT 1", (lecture_id,)).fetchone()


def in_use(connection: sqlite3.Connection, lecture_id: str, transcripts: list[dict] | None = None) -> dict | None:
    """The transcript in use: the latest recorded decision, else what the policy would pick."""
    transcripts = transcripts if transcripts is not None else _transcripts(connection, lecture_id)
    row = _latest_row(connection, lecture_id)
    if row and any(t["id"] == row[0] for t in transcripts):  # a pointer to a thrown-away recording is ignored
        return {"transcript_id": row[0], "decided_by": row[1], "reason": row[2], "recorded": True}
    pick = policy_pick(transcripts)
    if pick is None:
        return None
    return {"transcript_id": pick["id"], "decided_by": "policy", "reason": _policy_reason(pick), "recorded": False}


def _policy_reason(pick: dict) -> str:
    return f"우선순위 규칙(사용자 > 통합본 > MAI > Google > 다글로·클로바 > Whisper): {engine_label(pick['engine'])}"


def choose_in_use(connection: sqlite3.Connection, lecture_id: str, transcript_id: str, *, decided_by: str,
                  reason: str) -> str:
    """Append a decision; the caller commits. The previous pointer is kept as history."""
    row = _latest_row(connection, lecture_id)
    # "From" is the last recorded decision; a user's first choice replaces the policy pick they were shown.
    current = {"transcript_id": row[0]} if row else (in_use(connection, lecture_id) if decided_by == "user" else None)
    row_id = new_ulid()
    connection.execute(
        "INSERT INTO TranscriptInUse(id,lecture_id,transcript_id,previous_transcript_id,decided_by,reason,created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (row_id, lecture_id, transcript_id, current["transcript_id"] if current else None, decided_by, reason,
         time.time_ns() // 1_000_000))
    EventStore(connection).append("TRANSCRIPT_IN_USE_CHANGED", "Lecture", lecture_id, {
        "from": current["transcript_id"] if current else None, "to": transcript_id,
        "decided_by": decided_by, "reason": reason})
    return row_id


def refresh_policy(connection: sqlite3.Connection, lecture_id: str) -> str | None:
    """Record the policy pick when it changes. A user choice is never overridden."""
    row = _latest_row(connection, lecture_id)
    transcripts = _transcripts(connection, lecture_id)
    if row and row[1] == "user" and any(t["id"] == row[0] for t in transcripts):
        return None
    pick = policy_pick(transcripts)
    if pick is None or (row and row[0] == pick["id"]):
        return None
    return choose_in_use(connection, lecture_id, pick["id"], decided_by="policy", reason=_policy_reason(pick))


def history(connection: sqlite3.Connection, lecture_id: str) -> list[dict]:
    rows = connection.execute(
        "SELECT created_at, previous_transcript_id, transcript_id, decided_by, reason FROM TranscriptInUse "
        "WHERE lecture_id = ? ORDER BY created_at, id", (lecture_id,)).fetchall()
    return [{"at": r[0], "from": r[1], "to": r[2], "decided_by": r[3], "reason": r[4]} for r in rows]


# ------------------------------------------------------------------ lecture reads
def _gone(connection: sqlite3.Connection) -> set[str]:
    from .discard import hidden_ids

    return hidden_ids(connection)


def lecture_list(connection: sqlite3.Connection) -> list[dict]:
    """All lectures, newest first, with the n-th lecture of its course and quick status.

    A lecture whose only content was a recording the user threw away (D-058) is left out."""
    rows = connection.execute(
        "SELECT l.id, l.date, l.session_index, l.title, c.id, c.folder_name, "
        "(SELECT count(*) FROM MediaAsset m WHERE m.lecture_id = l.id AND m.is_master = 1), "
        "(SELECT count(*) FROM Transcript t WHERE t.lecture_id = l.id) "
        "FROM Lecture l JOIN Course c ON c.id = l.course_id ORDER BY l.date, l.session_index, l.id").fetchall()
    gone = _gone(connection)
    only_gone = set()
    if gone:
        marks = ",".join("?" * len(gone))
        with_gone = {r[0] for r in connection.execute(
            f"SELECT DISTINCT lecture_id FROM MediaAsset WHERE id IN ({marks})", tuple(gone))}
        kept = {r[0] for r in connection.execute(
            f"SELECT DISTINCT lecture_id FROM MediaAsset WHERE is_master = 1 AND id NOT IN ({marks}) "
            f"UNION SELECT DISTINCT lecture_id FROM Transcript WHERE media_id IS NULL OR media_id NOT IN ({marks}) "
            "UNION SELECT DISTINCT mm.lecture_id FROM MaterialMatch mm", tuple(gone) * 2)}
        only_gone = with_gone - kept
    from .note_attach import titles

    named = titles(connection)  # a lecture the user named shows that name and takes no number
    ordinal: dict[str, int] = {}
    lectures = []
    for r in rows:
        if r[0] in only_gone:
            continue
        name = named.get(r[0])
        if name is None:
            ordinal[r[4]] = ordinal.get(r[4], 0) + 1
        n = None if name else ordinal[r[4]]
        lectures.append({"id": r[0], "date": r[1], "session_index": r[2], "title": r[3], "course_id": r[4],
                         "course": r[5], "n": n, "name": name, "label": name or f"{n}강",
                         "recordings": r[6], "transcripts": r[7]})
    return lectures[::-1]


def ordinal(connection: sqlite3.Connection, lecture_id: str) -> int | None:
    """The n-th lecture of its course as the sidebar counts it (lectures left with only thrown-away
    recordings are not counted), so a header, a dialog and the sidebar say the same "3강"."""
    return next((x["n"] for x in lecture_list(connection) if x["id"] == lecture_id), None)


def label(connection: sqlite3.Connection, lecture_id: str) -> str | None:
    """What the lecture is called everywhere: the user's own name for it, else "n강"."""
    return next((x["label"] for x in lecture_list(connection) if x["id"] == lecture_id), None)


def group_label(day: str, today: date) -> str:
    """오늘 / 어제 / week range such as "9월 21일 – 27일" (Monday–Sunday)."""
    d = date.fromisoformat(day)
    if d == today:
        return "오늘"
    if d == today - timedelta(days=1):
        return "어제"
    monday = d - timedelta(days=d.weekday())
    sunday = monday + timedelta(days=6)
    end = f"{sunday.day}일" if sunday.month == monday.month else f"{sunday.month}월 {sunday.day}일"
    return f"{monday.month}월 {monday.day}일 – {end}"


def lecture_detail(connection: sqlite3.Connection, lecture_id: str) -> dict:
    row = connection.execute(
        "SELECT l.id, l.date, l.session_index, l.title, l.lecture_type, c.id, c.folder_name, c.code "
        "FROM Lecture l JOIN Course c ON c.id = l.course_id WHERE l.id = ?", (lecture_id,)).fetchone()
    if row is None:
        raise ValueError("없는 회차입니다.")
    from .note_attach import titles

    name = titles(connection).get(lecture_id)
    n = None if name else ordinal(connection, lecture_id) or connection.execute(
        "SELECT count(*) FROM Lecture WHERE course_id = ? AND (date < ? OR (date = ? AND (session_index < ? "
        "OR (session_index = ? AND id <= ?))))", (row[5], row[1], row[1], row[2], row[2], row[0])).fetchone()[0]
    d = date.fromisoformat(row[1])
    recordings = [
        {"id": r[0], "path": r[1], "duration_ms": r[2], "origin": r[3], "exists": Path(r[1]).is_file()}
        for r in connection.execute(
            "SELECT id, path, duration_ms, origin FROM MediaAsset WHERE lecture_id = ? AND is_master = 1 "
            "ORDER BY recorded_start, id", (lecture_id,)) if r[0] not in _gone(connection)]
    transcripts = _transcripts(connection, lecture_id)
    pointer = in_use(connection, lecture_id, transcripts)
    for t in transcripts:
        t["in_use"] = bool(pointer and pointer["transcript_id"] == t["id"])
    materials = [
        {"id": r[0], "title": r[1] or Path(r[2]).stem, "path": r[2], "kind": r[3], "page_from": r[4],
         "page_to": r[5], "confidence": r[6], "decided_by": r[7], "exists": Path(r[2]).is_file()}
        for r in connection.execute(
            "SELECT m.id, m.title, m.path, m.kind, mm.page_from, mm.page_to, mm.confidence_class, mm.decided_by "
            "FROM MaterialMatch mm JOIN Material m ON m.id = mm.material_id WHERE mm.lecture_id = ? "
            "ORDER BY m.title", (lecture_id,))]
    notes = []
    # Note files only; evidence-linked notes (kind lecture_note) live in the DB and load via study.notes (S4).
    for r in connection.execute("SELECT id, path, kind, lecture_ids FROM StudyArtifact WHERE kind != 'lecture_note'"):
        try:
            ids = json.loads(r[3] or "[]")
        except ValueError:
            ids = []
        if lecture_id in ids:
            notes.append({"id": r[0], "path": r[1], "kind": r[2], "exists": Path(r[1]).is_file()})
    detail = {
        "id": row[0], "date": row[1], "weekday": WEEKDAYS[d.weekday()], "session_index": row[2], "title": row[3],
        "lecture_type": row[4], "course_id": row[5], "course": row[6], "course_code": row[7], "n": n,
        "name": name, "label": name or f"{n}강",
        "recordings": recordings, "transcripts": transcripts, "in_use": pointer, "materials": materials,
        "notes": notes, "history": history(connection, lecture_id),
    }
    detail["state"] = lecture_state(detail)
    return detail


def lecture_state(detail: dict) -> dict:
    """What the user can do with this lecture now, and the one next action (§0.4)."""
    if detail["in_use"]:
        used = next((t for t in detail["transcripts"] if t["id"] == detail["in_use"]["transcript_id"]), None)
        has_mai = any(t["engine"] == "mai" for t in detail["transcripts"])
        conversational = any(word in (detail.get("lecture_type") or "") for word in ("실습", "대화", "practice", "dialog"))
        weak = used and used["engine"] == "gemini_transcribe" and not has_mai and (
            conversational or {"density", "coverage", "empty_chunk"} & set(used.get("qa", [])))
        if weak:  # §0.2: known weaknesses of the rough transcript become the next action
            why = "실습·대화형 수업" if conversational else "전사 밀도·길이 경고"
            return {"key": "reviewable", "label": "지금 복습 가능", "action": "mai_prepare",
                    "action_label": "MAI 보강 권장", "why": why}
        return {"key": "reviewable", "label": "지금 복습 가능", "action": "open_transcript", "action_label": "전사 보기"}
    if any(r["exists"] for r in detail["recordings"]):
        return {"key": "needs_transcript", "label": "전사 대기", "action": "transcribe",
                "action_label": "Google 전사 시작"}
    if detail["recordings"]:
        return {"key": "missing_audio", "label": "녹음 파일 확인 필요", "action": None, "action_label": None}
    return {"key": "no_sources", "label": "녹음·전사 없음", "action": None, "action_label": None}


def segments(connection: sqlite3.Connection, transcript_id: str) -> list[dict]:
    return [{"seq": r[0], "start_ms": r[1], "speaker": r[2], "text": r[3]} for r in connection.execute(
        "SELECT seq, start_ms, speaker, text FROM TranscriptSegment WHERE transcript_id = ? ORDER BY seq",
        (transcript_id,))]


def media_for_transcript(connection: sqlite3.Connection, transcript_id: str) -> dict | None:
    """The recording this transcript's timeline belongs to, only when that link is recorded."""
    row = connection.execute(
        "SELECT m.id, m.path, m.duration_ms FROM Transcript t JOIN MediaAsset m ON m.id = t.media_id "
        "WHERE t.id = ?", (transcript_id,)).fetchone()
    if row is None:
        return None
    return {"id": row[0], "path": row[1], "duration_ms": row[2], "exists": Path(row[1]).is_file()}
