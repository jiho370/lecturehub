"""User corrections over transcripts (§6 Correction, S4). Raw segments never change.

A correction is anchored by the corrected words and a little text on each side, plus the
segment it was made on as a hint. Whatever transcript a lecture currently uses, the
corrections of that lecture are located again by that context:

1. the transcript it was made on: the same segment, context first, then the bare words;
2. another transcript (the in-use one was replaced): a segment near ``approx_ms`` that
   holds the context (or the words, when they occur once there).

What cannot be placed is "orphaned": it is shown for the user to check and never applied
by guesswork (CORR-002).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time

from lecturehub.core.events import append
from lecturehub.core.ids import new_ulid

CONTEXT = 12  # characters kept on each side of the corrected words
NEAR_MS = 90_000  # how far from the original time a replaced transcript may place it


def _context_hash(before: str, words: str, after: str) -> str:
    return hashlib.sha256(f"{before}␟{words}␟{after}".encode()).hexdigest()[:16]


def _segment(connection: sqlite3.Connection, transcript_id: str, seq: int):
    return connection.execute(
        "SELECT t.lecture_id, s.start_ms, s.text FROM TranscriptSegment s JOIN Transcript t ON t.id = s.transcript_id "
        "WHERE s.transcript_id = ? AND s.seq = ?", (transcript_id, seq)).fetchone()


def add(connection: sqlite3.Connection, transcript_id: str, seq: int, char_from: int, char_to: int, after: str,
        *, kind: str = "other", decided_by: str = "user", shown_text: str | None = None) -> str:
    """Record a correction of ``shown_text[char_from:char_to]`` (the segment as the user saw it,
    corrections included; defaults to the raw text). Caller commits."""
    row = _segment(connection, transcript_id, seq)
    if row is None:
        raise ValueError("그 전사 구간이 없습니다")
    lecture_id, start_ms, raw = row
    text = raw if shown_text is None else shown_text
    if not (0 <= char_from < char_to <= len(text)):
        raise ValueError("고칠 글자 범위가 잘못됐습니다")
    words = text[char_from:char_to]
    if words == after:
        raise ValueError("바뀐 내용이 없습니다")
    before_ctx, after_ctx = text[max(0, char_from - CONTEXT):char_from], text[char_to:char_to + CONTEXT]
    anchor = {"transcript_id": transcript_id, "seg_seq": seq, "char_from": char_from, "char_to": char_to,
              "context_before": before_ctx, "context_after": after_ctx,
              "context_hash": _context_hash(before_ctx, words, after_ctx), "approx_ms": start_ms}
    correction_id = new_ulid()
    connection.execute(
        "INSERT INTO Correction (id, lecture_id, anchor, before, after, kind, status, decided_by, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)",
        (correction_id, lecture_id, json.dumps(anchor, ensure_ascii=False), words, after, kind, decided_by,
         time.time_ns() // 1_000_000))
    append(connection, "CORRECTION_ADDED", "Correction", correction_id,
           {"lecture_id": lecture_id, "transcript_id": transcript_id, "seg_seq": seq, "kind": kind})
    return correction_id


def active(connection: sqlite3.Connection, lecture_id: str) -> list[dict]:
    return [{"id": r[0], "anchor": json.loads(r[1]), "before": r[2], "after": r[3], "kind": r[4], "created_at": r[5]}
            for r in connection.execute(
                "SELECT id, anchor, before, after, kind, created_at FROM Correction "
                "WHERE lecture_id = ? AND status = 'active' ORDER BY created_at, id", (lecture_id,))]


def _replace_in(text: str, correction: dict) -> str | None:
    """Apply one correction inside ``text``; None when it cannot be placed without guessing."""
    anchor = correction["anchor"]
    full = anchor["context_before"] + correction["before"] + anchor["context_after"]
    if full and text.count(full) == 1:
        start = text.index(full) + len(anchor["context_before"])
        return text[:start] + correction["after"] + text[start + len(correction["before"]):]
    if correction["before"] and text.count(correction["before"]) == 1:
        start = text.index(correction["before"])
        return text[:start] + correction["after"] + text[start + len(correction["before"]):]
    return None


def corrected_segments(connection: sqlite3.Connection, transcript_id: str) -> dict:
    """Segments of ``transcript_id`` with the lecture's active corrections applied.

    Returns {"segments": [{seq, start_ms, speaker, text, raw, corrections}], "orphaned": [correction ids]}.
    """
    lecture = connection.execute("SELECT lecture_id FROM Transcript WHERE id = ?", (transcript_id,)).fetchone()
    segments = [{"seq": r[0], "start_ms": r[1], "speaker": r[2], "text": r[3], "raw": r[3], "corrections": []}
                for r in connection.execute(
                    "SELECT seq, start_ms, speaker, text FROM TranscriptSegment WHERE transcript_id = ? ORDER BY seq",
                    (transcript_id,))]
    if lecture is None:
        return {"segments": segments, "orphaned": []}
    by_seq = {segment["seq"]: segment for segment in segments}
    orphaned = []
    for correction in active(connection, lecture[0]):
        anchor = correction["anchor"]
        if anchor["transcript_id"] == transcript_id:
            candidates = [by_seq[anchor["seg_seq"]]] if anchor["seg_seq"] in by_seq else []
        else:
            candidates = sorted((s for s in segments if abs(s["start_ms"] - anchor["approx_ms"]) <= NEAR_MS),
                                key=lambda s: abs(s["start_ms"] - anchor["approx_ms"]))
        placed = False
        for segment in candidates:
            changed = _replace_in(segment["text"], correction)
            if changed is not None:
                segment["text"] = changed
                segment["corrections"].append(correction["id"])
                placed = True
                break
        if not placed:
            orphaned.append(correction["id"])
    return {"segments": segments, "orphaned": orphaned}


def mark_orphans(connection: sqlite3.Connection, transcript_id: str) -> list[str]:
    """After the in-use transcript changed: record corrections that no longer place (caller commits)."""
    orphaned = corrected_segments(connection, transcript_id)["orphaned"]
    for correction_id in orphaned:
        connection.execute("UPDATE Correction SET status = 'orphaned' WHERE id = ? AND status = 'active'",
                           (correction_id,))
        append(connection, "CORRECTION_ORPHANED", "Correction", correction_id, {"transcript_id": transcript_id})
    return orphaned
