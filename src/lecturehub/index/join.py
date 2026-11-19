"""Join recordings of one class into one (user request 2026-09-29: "이거 이따 다음 녹음과 연결해줘").

A class recorded in pieces (stopped by mistake, restarted) becomes one recording again, without touching
the pieces (§6.1): ffmpeg writes a new file under ``_lecturehub\\derived`` (re-encoded MP3 96k mono, so
pieces from different sessions fit), registered as a master of the first piece's lecture
(``derived_from`` = the first piece). Each piece gets an Evidence ``joined_into`` (source user) and is
then hidden like a discarded one (lecture view, sidebar, search, 녹음본 copy) — its files stay.

Transcripts are joined too, so nothing is sent to Google again: for every engine that transcribed *all*
pieces, the segments are shifted by each piece's offset into one derived transcript of the joined
recording (its own file under ``derived``; the raw transcripts stay as they are).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import time
from pathlib import Path

from lecturehub.core import proc
from lecturehub.core.events import append
from lecturehub.core.ids import new_ulid


def joined_parts(connection: sqlite3.Connection) -> set[str]:
    """Pieces hidden behind a joined recording. Throwing the joined one away (D-058) brings its pieces back."""
    from .discard import discarded_ids

    gone = discarded_ids(connection)
    return {r[0] for r in connection.execute(
        "SELECT subject_id, detail FROM Evidence WHERE subject_type = 'MediaAsset' AND kind = 'joined_into' "
        "AND source = 'user'") if json.loads(r[1] or "{}").get("joined_id") not in gone}


def _kind(origin: str) -> str:
    """PC sound stays apart; every room recording (app mic, phone, other device) can be joined with another."""
    return "pc" if origin == "app_loopback" else "room"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _evidence(connection, subject_type: str, subject_id: str, kind: str, detail: dict) -> None:
    connection.execute(
        "INSERT INTO Evidence (id, subject_type, subject_id, field, kind, detail, weight, source, created_at) "
        "VALUES (?, ?, ?, NULL, ?, ?, NULL, 'user', ?)",
        (new_ulid(), subject_type, subject_id, kind, json.dumps(detail, ensure_ascii=False), time.time_ns() // 1_000_000))


def _pieces(connection, media_ids: list[str]) -> list[dict]:
    from .discard import hidden_ids

    hidden = hidden_ids(connection)
    pieces = []
    for media_id in dict.fromkeys(media_ids):
        row = connection.execute(
            "SELECT m.id, m.lecture_id, m.path, m.duration_ms, m.origin, m.device_type, m.recorded_start, "
            "l.date, l.course_id, c.folder_name FROM MediaAsset m JOIN Lecture l ON l.id = m.lecture_id "
            "JOIN Course c ON c.id = l.course_id WHERE m.id = ? AND m.is_master = 1", (media_id,)).fetchone()
        if row is None or media_id in hidden:
            raise ValueError(f"이어 붙일 녹음을 찾지 못했습니다: {media_id}")
        pieces.append(dict(zip(("id", "lecture_id", "path", "duration_ms", "origin", "device_type", "recorded_start",
                                "date", "course_id", "course"), row, strict=True)))
    return sorted(pieces, key=lambda p: (p["recorded_start"], p["id"]))


def plan(connection: sqlite3.Connection, media_ids: list[str]) -> dict:
    """Check the pieces and say what joining would make (nothing is written)."""
    pieces = _pieces(connection, media_ids)
    if len(pieces) < 2:
        raise ValueError("이어 붙이려면 녹음이 두 개 이상 있어야 합니다")
    first = pieces[0]
    if any(p["course_id"] != first["course_id"] or p["date"] != first["date"] for p in pieces):
        raise ValueError("같은 날 같은 과목의 녹음만 이어 붙일 수 있습니다")
    if len({_kind(p["origin"]) for p in pieces}) > 1:
        raise ValueError("마이크 녹음과 PC 소리 녹음은 서로 이어 붙이지 않습니다(각각 따로 이어 붙이세요)")
    offset = 0
    for index, piece in enumerate(pieces):
        if not Path(piece["path"]).is_file():
            raise ValueError(f"녹음 파일이 없습니다: {piece['path']}")
        piece["offset_ms"] = offset
        if index:
            previous = pieces[index - 1]
            piece["gap_before_ms"] = max(0, piece["recorded_start"] - previous["recorded_start"] - previous["duration_ms"])
        offset += piece["duration_ms"]
        piece["transcripts"] = [dict(zip(("id", "engine", "role", "status"), r, strict=True)) for r in connection.execute(
            "SELECT id, engine, role, status FROM Transcript WHERE media_id = ? AND EXISTS "
            "(SELECT 1 FROM TranscriptSegment s WHERE s.transcript_id = Transcript.id) ORDER BY id", (piece["id"],))]
    engines = set.intersection(*({t["engine"] for t in p["transcripts"]} for p in pieces))
    missing = sorted({t["engine"] for p in pieces for t in p["transcripts"]} - engines)
    return {"pieces": pieces, "duration_ms": offset, "lecture_id": first["lecture_id"], "course": first["course"],
            "date": first["date"], "engines": sorted(engines), "engines_partial": missing}


def _ffmpeg_concat(sources: list[Path], target: Path) -> None:
    from lecturehub.transcription.audio import _tool

    # the concat filter, not the concat demuxer: the demuxer silently drops a piece with another codec
    # (2026-11-09: a phone M4A after laptop MP3s came out 83 of 108 minutes)
    inputs = [arg for source in sources for arg in ("-i", str(source))]
    graph = "".join(f"[{i}:a]aresample=48000,aformat=channel_layouts=mono[a{i}];" for i in range(len(sources)))
    graph += "".join(f"[a{i}]" for i in range(len(sources))) + f"concat=n={len(sources)}:v=0:a=1[out]"
    try:
        subprocess.run([_tool("ffmpeg"), "-hide_banner", "-nostdin", "-v", "error", "-y", "-threads", "1", *inputs,
                        "-filter_complex", graph, "-map", "[out]", "-c:a", "libmp3lame", "-b:a", "96k", str(target)],
                       check=True, capture_output=True, creationflags=proc.BELOW_NORMAL | proc.NO_WINDOW)
    except subprocess.CalledProcessError as exc:
        raise ValueError("녹음을 이어 붙이지 못했습니다: " + exc.stderr.decode("utf-8", "replace")[-300:]) from exc


def join(connection: sqlite3.Connection, root: Path, media_ids: list[str], *, concat=None, probe=None) -> dict:
    """Make the joined recording (+ joined transcripts) and hide the pieces. Caller commits."""
    from .lectures import refresh_policy
    from .media import probe_media

    p = plan(connection, media_ids)
    pieces, first = p["pieces"], p["pieces"][0]
    folder = Path(root) / "_lecturehub" / "derived"
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"joined_{Path(first['path']).parent.name}_{len(pieces)}pieces"
    target = folder / f"{stem}.mp3"
    (concat or _ffmpeg_concat)([Path(x["path"]) for x in pieces], target)
    meta = (probe or probe_media)(target)
    if meta.duration_ms < p["duration_ms"] - 5000:
        target.unlink(missing_ok=True)
        raise ValueError(f"이어 붙인 녹음이 조각들보다 짧습니다({meta.duration_ms // 60000}분 / "
                         f"{p['duration_ms'] // 60000}분) — 만들지 않았습니다")
    digest = _sha256(target)
    if connection.execute("SELECT 1 FROM MediaAsset WHERE sha256 = ?", (digest,)).fetchone():
        raise ValueError("이미 이어 붙인 녹음입니다")
    joined_id = new_ulid()
    connection.execute(
        "INSERT INTO MediaAsset (id, lecture_id, path, sha256, duration_ms, codec, sample_rate, channels, bytes, origin, "
        "device_type, is_master, derived_from, preset_id, recorded_start, partial, loudness_profile, decided_by, "
        "confidence_class) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, NULL, ?, 0, '{}', 'user', 'exact')",
        (joined_id, first["lecture_id"], str(target), digest, meta.duration_ms, meta.codec, meta.sample_rate,
         meta.channels, target.stat().st_size, first["origin"], first["device_type"], first["id"],
         first["recorded_start"]))
    detail = [{"media_id": x["id"], "offset_ms": x["offset_ms"], "duration_ms": x["duration_ms"],
               "recorded_start": x["recorded_start"], "gap_before_ms": x.get("gap_before_ms", 0)} for x in pieces]
    _evidence(connection, "MediaAsset", joined_id, "joined_from", {"pieces": detail, "decided_by": "user"})

    transcripts = []
    for engine in p["engines"]:
        chosen = [next(t for t in x["transcripts"] if t["engine"] == engine) for x in pieces]
        rows, seq = [], 0
        for piece, transcript in zip(pieces, chosen, strict=True):
            for start, end, speaker, text in connection.execute(
                    "SELECT start_ms, end_ms, speaker, text FROM TranscriptSegment WHERE transcript_id = ? ORDER BY seq",
                    (transcript["id"],)):
                rows.append((seq, start + piece["offset_ms"], None if end is None else end + piece["offset_ms"],
                             speaker, text))
                seq += 1
        out = folder / f"{stem}_{engine}.json"
        out.write_text(json.dumps({"joined_media": joined_id, "from_transcripts": [t["id"] for t in chosen],
                                   "offsets_ms": [x["offset_ms"] for x in pieces],
                                   "segments": [{"start_ms": r[1], "end_ms": r[2], "speaker": r[3], "text": r[4]}
                                                for r in rows]}, ensure_ascii=False, indent=1), encoding="utf-8")
        transcript_id = new_ulid()
        connection.execute(
            "INSERT INTO Transcript (id, lecture_id, media_id, engine, engine_version, role, role_decided_by, "
            "chunk_plan_id, status, source_file, sha256) VALUES (?, ?, ?, ?, NULL, ?, 'policy', NULL, ?, ?, ?)",
            (transcript_id, first["lecture_id"], joined_id, engine, chosen[0]["role"],
             "done" if all(t["status"] == "done" for t in chosen) else "qa_warning", str(out), _sha256(out)))
        for row in rows:
            connection.execute("INSERT INTO TranscriptSegment (transcript_id, seq, start_ms, end_ms, speaker, text, "
                               "chunk_index) VALUES (?, ?, ?, ?, ?, ?, 0)", (transcript_id, *row))
        _evidence(connection, "Transcript", transcript_id, "joined_transcript",
                  {"from": [t["id"] for t in chosen], "offsets_ms": [x["offset_ms"] for x in pieces]})
        transcripts.append(transcript_id)

    for piece in pieces:
        _evidence(connection, "MediaAsset", piece["id"], "joined_into", {"joined_id": joined_id, "decided_by": "user"})
    refresh_policy(connection, first["lecture_id"])
    append(connection, "MEDIA_JOINED", "MediaAsset", joined_id,
           {"pieces": [x["id"] for x in pieces], "transcripts": transcripts})
    return {"joined_id": joined_id, "path": str(target), "lecture_id": first["lecture_id"], "duration_ms": meta.duration_ms,
            "pieces": len(pieces), "transcripts": transcripts, "engines_partial": p["engines_partial"],
            "gaps_ms": [x.get("gap_before_ms", 0) for x in pieces[1:]]}


def candidates(connection: sqlite3.Connection, media_id: str) -> list[dict]:
    """Other recordings that could be joined with this one (same course, same day, same kind)."""
    from .discard import hidden_ids

    row = connection.execute(
        "SELECT l.course_id, l.date, m.origin FROM MediaAsset m JOIN Lecture l ON l.id = m.lecture_id "
        "WHERE m.id = ? AND m.is_master = 1", (media_id,)).fetchone()
    if row is None:
        return []
    hidden = hidden_ids(connection)
    return [{"id": r[0], "path": r[1], "duration_ms": r[2], "recorded_start": r[3], "lecture_id": r[4]}
            for r in connection.execute(
                "SELECT m.id, m.path, m.duration_ms, m.recorded_start, m.lecture_id, m.origin FROM MediaAsset m "
                "JOIN Lecture l ON l.id = m.lecture_id WHERE m.is_master = 1 AND l.course_id = ? AND l.date = ? "
                "AND m.id != ? ORDER BY m.recorded_start", (row[0], row[1], media_id))
            if r[0] not in hidden and _kind(r[5]) == _kind(row[2])]
