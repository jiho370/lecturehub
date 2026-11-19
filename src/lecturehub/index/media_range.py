"""Recording range fix (D-057): a recording that ran past the class is cut once, with one click.

The original never changes (§6.1): cutting makes a *derived* MediaAsset (is_master 0,
derived_from the original, ``ffmpeg -c copy`` so nothing is re-encoded) under
``_lecturehub\\derived``, and an Evidence ``lecture_range`` (source user) says which part is the
lecture. Transcript segments outside that part stay in the raw transcript but are left out of
the lecture view, search hits and note input. Undo is a new Evidence; the derived file stays.

The end is *suggested*, never applied by itself: the last minute with lecture speech in the
in-use transcript (repetition such as "음. 음. 음." is not speech), else the timetable end, plus
a two-minute margin.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from lecturehub.core.events import append
from lecturehub.core.ids import new_ulid
from lecturehub.core.timeutil import KST

MARGIN_MS = 2 * 60_000
LONG_TAIL_MS = 15 * 60_000  # ask when the recording runs this long past the class end or past the last speech
SPEECH_CHARS_PER_MIN = 80


def _media(connection: sqlite3.Connection, media_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT id, lecture_id, path, duration_ms, codec, sample_rate, channels, origin, device_type, recorded_start "
        "FROM MediaAsset WHERE id = ? AND is_master = 1", (media_id,)).fetchone()
    if row is None:
        raise ValueError("원본 녹음을 찾지 못했습니다")
    return row


def _evidence(connection, media_id: str, kind: str, detail: dict) -> str:
    evidence_id = new_ulid()
    connection.execute(
        "INSERT INTO Evidence (id, subject_type, subject_id, field, kind, detail, weight, source, created_at) "
        "VALUES (?, 'MediaAsset', ?, NULL, ?, ?, NULL, 'user', ?)",
        (evidence_id, media_id, kind, json.dumps(detail, ensure_ascii=False), time.time_ns() // 1_000_000))
    return evidence_id


def decided_range(connection: sqlite3.Connection, media_id: str) -> dict | None:
    """The user's latest decision for this recording: a cut, "the whole thing is the lecture", or None."""
    row = connection.execute(
        "SELECT kind, detail FROM Evidence WHERE subject_type = 'MediaAsset' AND subject_id = ? AND source = 'user' "
        "AND kind IN ('lecture_range', 'lecture_range_full', 'lecture_range_revoked') "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1", (media_id,)).fetchone()
    if row is None or row[0] == "lecture_range_revoked":
        return None
    media = _media(connection, media_id)
    if row[0] == "lecture_range_full":
        return {"start_ms": 0, "end_ms": media["duration_ms"], "media_id_to_use": media_id, "cut": False}
    detail = json.loads(row[1])
    return {"start_ms": detail["start_ms"], "end_ms": detail["end_ms"], "media_id_to_use": detail["derived_id"],
            "cut": True}


def _speech_minutes(connection: sqlite3.Connection, media_id: str) -> list[int]:
    """Minutes (from the recording start) that hold lecture speech in the transcripts of this recording."""
    per: dict[int, int] = defaultdict(int)
    for start_ms, text in connection.execute(
            "SELECT s.start_ms, s.text FROM TranscriptSegment s JOIN Transcript t ON t.id = s.transcript_id "
            "WHERE t.media_id = ?", (media_id,)):
        words = text.split()
        if len(words) >= 20 and len(set(words)) / len(words) < 0.2:
            continue  # "음. 음. 음. …" — a repetition, not speech
        per[start_ms // 60_000] += len(text)
    return sorted(m for m, chars in per.items() if chars >= SPEECH_CHARS_PER_MIN)


def _class_end_ms(connection: sqlite3.Connection, lecture_id: str, recorded_start: int) -> int | None:
    """Timetable end of this lecture's class, relative to the recording start."""
    from .schedule import daily_schedule

    row = connection.execute("SELECT course_id FROM Lecture WHERE id = ?", (lecture_id,)).fetchone()
    if row is None:
        return None
    started = datetime.fromtimestamp(recorded_start / 1000, UTC).astimezone(KST)  # timetable is KST
    day_start = int(started.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
    at = recorded_start - day_start
    ends = [c.end_ms for c in daily_schedule(connection, started.date())
            if c.course_id == row[0] and c.start_ms - 30 * 60_000 <= at <= c.end_ms]
    return (min(ends) + day_start - recorded_start) if ends else None


def _session_file(media: sqlite3.Row, name: str) -> dict | None:
    """A JSON object the recorder/app left next to the recording's master (auto_stop.json, pauses.json,
    gaps.json, stop.json); ``None`` when it is missing, unreadable or not an object."""
    try:
        data = json.loads((Path(media["path"]).parent / name).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _entries(data: dict | None, key: str) -> list[dict]:
    items = (data or {}).get(key)
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def pauses(connection: sqlite3.Connection, media_id: str) -> list[dict]:
    """[{at_ms (audio position), paused_ms}] in audio order — every stretch of wall-clock time that is not in
    the audio: the user's pauses (pauses.json) and the gaps left by a device that stopped delivering
    (gaps.json, else the list in stop.json). A gap shifts the clock exactly as a pause does.

    A device can die while the recording is paused; the two stretches then cover the same wall-clock time.
    Stretches that overlap on the wall clock are merged, so no minute is counted twice. An entry whose numbers
    cannot be read is left out.
    """
    media = _media(connection, media_id)
    spans = []  # (wall-clock from, wall-clock to, audio position)
    for p in _entries(_session_file(media, "pauses.json"), "pauses"):
        try:
            if p.get("resumed_at") and p.get("paused_at") is not None and p.get("audio_ms") is not None:
                spans.append((int(p["paused_at"]), int(p["resumed_at"]), int(p["audio_ms"])))
        except (TypeError, ValueError):
            continue
    gaps = _session_file(media, "gaps.json")
    for g in _entries(gaps if gaps is not None else _session_file(media, "stop.json"), "gaps"):
        try:  # ``at`` is when the gap ended, ``lost_ms`` how long it lasted (recorder/session.py note_gap)
            if g.get("at") is not None and g.get("audio_ms") is not None and int(g.get("lost_ms") or 0) > 0:
                spans.append((int(g["at"]) - int(g["lost_ms"]), int(g["at"]), int(g["audio_ms"])))
        except (TypeError, ValueError):
            continue
    merged: list[list[int]] = []
    for start, end, audio in sorted(spans):
        end = max(start, end)
        if merged and start < merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
            merged[-1][2] = min(merged[-1][2], audio)
        else:
            merged.append([start, end, audio])
    return sorted(({"at_ms": audio, "paused_ms": end - start} for start, end, audio in merged),
                  key=lambda item: item["at_ms"])


def audio_position(wall_ms: int, holes: list[dict]) -> int:
    """Audio position of a wall-clock offset from the recording start, given ``pauses()``: the stretches
    before it are taken out. A moment inside a pause or gap maps to where the audio stopped."""
    shift = 0
    for hole in holes:
        begins = hole["at_ms"] + shift  # wall-clock offset at which this stretch begins
        if wall_ms <= begins:
            break
        if wall_ms < begins + hole["paused_ms"]:
            return hole["at_ms"]
        shift += hole["paused_ms"]
    return wall_ms - shift


def plan(connection: sqlite3.Connection, media_id: str) -> dict:
    """What the [자르기] dialog starts from: the decided range, else the suggested end, else the whole recording.
    ``pauses`` (pauses and gaps, see ``pauses()``) lets the dialog show the wall-clock time of an audio position."""
    media = _media(connection, media_id)
    decided = decided_range(connection, media_id)
    hint = suggest(connection, media_id)
    start = decided["start_ms"] if decided and decided.get("cut") else 0
    end = decided["end_ms"] if decided and decided.get("cut") else hint.get("end_ms") or media["duration_ms"]
    return {"media_id": media_id, "recorded_start": media["recorded_start"], "duration_ms": media["duration_ms"],
            "start_ms": start, "end_ms": end, "reason": None if decided and decided.get("cut") else hint.get("reason"),
            "pauses": pauses(connection, media_id)}


def suggest(connection: sqlite3.Connection, media_id: str) -> dict:
    """{ask, end_ms, reason, speech_end_ms, class_end_ms, duration_ms, recorded_start, pauses} — whether to show
    the one-click card. ``end_ms`` and ``speech_end_ms`` are audio positions. ``class_end_ms`` is the timetable
    end as a wall-clock offset from the recording start; the end suggested from it is its audio position
    (pauses and gaps before it taken out) plus the margin. ``pauses`` is ``pauses()``, for turning a typed
    clock time into an audio position. When there is nothing to suggest: {ask: False, duration_ms}."""
    media = _media(connection, media_id)
    duration = media["duration_ms"]
    speech = _speech_minutes(connection, media_id)
    speech_end = (speech[-1] + 1) * 60_000 if speech else None
    class_end = _class_end_ms(connection, media["lecture_id"], media["recorded_start"]) if media["lecture_id"] else None
    auto = _session_file(media, "auto_stop.json") or {}
    quiet_from = auto.get("quiet_since_ms")
    holes = pauses(connection, media_id)
    if speech_end is not None:
        end, reason = speech_end + MARGIN_MS, "전사에서 강의 말소리가 끝난 뒤 2분"
    elif isinstance(quiet_from, int):  # stopped by itself after a long silence (user 2026-09-30)
        end, reason = quiet_from + MARGIN_MS, "소리가 끊긴 뒤 2분(자동 정지한 녹음)"
    elif class_end is not None:
        end, reason = audio_position(class_end, holes) + MARGIN_MS, "시간표 종료 시각 뒤 2분(전사가 아직 없음)"
    else:
        return {"ask": False, "duration_ms": duration}
    end = min(end, duration)
    tail = duration - end
    ask = tail >= LONG_TAIL_MS and decided_range(connection, media_id) is None
    return {"ask": ask, "end_ms": end, "reason": reason, "speech_end_ms": speech_end, "class_end_ms": class_end,
            "duration_ms": duration, "recorded_start": media["recorded_start"], "pauses": holes}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def cut(connection: sqlite3.Connection, root: Path, media_id: str, start_ms: int, end_ms: int, *, cutter=None) -> dict:
    """Make the derived clip [start_ms, end_ms) and record the user's range. Caller commits."""
    media = _media(connection, media_id)
    start_ms, end_ms = int(start_ms), int(end_ms)
    if not (0 <= start_ms < end_ms <= media["duration_ms"]):
        raise ValueError("자를 범위가 녹음 길이를 벗어났습니다")
    if end_ms - start_ms < 60_000:
        raise ValueError("1분보다 짧게 자를 수 없습니다")
    source = Path(media["path"])
    if not source.is_file():
        raise ValueError("원본 녹음 파일이 없습니다")
    folder = Path(root) / "_lecturehub" / "derived"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{source.stem}_{start_ms // 1000}s-{end_ms // 1000}s{source.suffix}"
    (cutter or _ffmpeg_cut)(source, target, start_ms, end_ms)
    digest = _sha256(target)
    existing = connection.execute("SELECT id FROM MediaAsset WHERE sha256 = ?", (digest,)).fetchone()
    derived_id = existing[0] if existing else new_ulid()
    if not existing:
        connection.execute(
            "INSERT INTO MediaAsset (id, lecture_id, path, sha256, duration_ms, codec, sample_rate, channels, bytes, "
            "origin, device_type, is_master, derived_from, preset_id, recorded_start, partial, loudness_profile, "
            "decided_by, confidence_class) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, NULL, ?, 0, '{}', 'user', 'exact')",
            (derived_id, media["lecture_id"], str(target), digest, end_ms - start_ms, media["codec"], media["sample_rate"],
             media["channels"], target.stat().st_size, media["origin"], media["device_type"], media_id,
             media["recorded_start"] + start_ms))
    evidence_id = _evidence(connection, media_id, "lecture_range",
                            {"start_ms": start_ms, "end_ms": end_ms, "derived_id": derived_id, "decided_by": "user"})
    append(connection, "MEDIA_RANGE_SET", "MediaAsset", media_id,
           {"start_ms": start_ms, "end_ms": end_ms, "derived_id": derived_id, "evidence_id": evidence_id})
    return {"derived_id": derived_id, "path": str(target), "start_ms": start_ms, "end_ms": end_ms}


def _ffmpeg_cut(source: Path, target: Path, start_ms: int, end_ms: int) -> None:
    from lecturehub.transcription.audio import _run, _tool

    _run([_tool("ffmpeg"), "-hide_banner", "-nostdin", "-v", "error", "-y", "-ss", f"{start_ms / 1000:.3f}",
          "-to", f"{end_ms / 1000:.3f}", "-i", str(source), "-map", "0:a", "-c", "copy", str(target)])


def keep_whole(connection: sqlite3.Connection, media_id: str) -> None:
    """"The whole recording is the lecture" — the card stops asking; archive copy may proceed."""
    _media(connection, media_id)
    _evidence(connection, media_id, "lecture_range_full", {"decided_by": "user"})
    append(connection, "MEDIA_RANGE_SET", "MediaAsset", media_id, {"whole": True})


def revert(connection: sqlite3.Connection, media_id: str) -> None:
    """Undo the range decision (the derived file stays; the original is used again)."""
    _media(connection, media_id)
    _evidence(connection, media_id, "lecture_range_revoked", {"decided_by": "user"})
    append(connection, "MEDIA_RANGE_REVOKED", "MediaAsset", media_id, {})


def in_range(connection: sqlite3.Connection, transcript_id: str) -> tuple[int, int] | None:
    """The lecture part of the recording this transcript belongs to, or None when not cut."""
    row = connection.execute("SELECT media_id FROM Transcript WHERE id = ?", (transcript_id,)).fetchone()
    if row is None or row[0] is None:
        return None
    try:
        decided = decided_range(connection, row[0])
    except ValueError:
        return None
    return (decided["start_ms"], decided["end_ms"]) if decided and decided["cut"] else None
