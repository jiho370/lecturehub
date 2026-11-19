"""Move a finished lecture's WAV files to the Recycle Bin (user decision 2026-09-29).

A recording session keeps two uncompressed copies: 1-minute segments written
during capture (crash safety) and the merged master WAV (~630 MB each per
110 minutes). They go to the Recycle Bin — never deleted outright — only when
all of these hold:

1. the compressed master (MP3/M4A) was verified by finalize (length within 100 ms);
2. its range is decided when it ran long (D-057), and its D-055 copy in 녹음본\\<과목>\\ exists
   (same sha256 as the recording that was copied);
3. the lecture has a *finished* transcript: CanonicalTranscript at stage C3
   (three engines merged → material check → context cleanup) with a final status.

Session metadata (session.json, stop.json, finalize_*.json) and the compressed
master stay. Nothing runs while a recording is in progress (the caller checks).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from lecturehub.core.events import EventStore

from .archive_copy import APP_ORIGINS, copied_path, range_hold

FINISHED_STAGE = "C3"
FINISHED_STATUS = ("done", "final", "approved")


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


from lecturehub.core.recycle_bin import (  # noqa: F401 - moved; re-exported for existing callers
    FO_DELETE,
    FOF_ALLOWUNDO,
    FOF_NOCONFIRMATION,
    FOF_NOERRORUI,
    FOF_SILENT,
    FOF_WANTNUKEWARNING,
    RECYCLE_FLAGS,
    _in_recycle_bin,
    from_recycle_bin,
    recycle_bin_entries,
    to_recycle_bin,
)


def _finished(connection, lecture_id: str) -> bool:
    marks = ",".join("?" * len(FINISHED_STATUS))
    return connection.execute(
        f"SELECT 1 FROM CanonicalTranscript WHERE lecture_id=? AND stage_reached=? AND status IN ({marks})",
        (lecture_id, FINISHED_STAGE, *FINISHED_STATUS)).fetchone() is not None


def candidates(connection: sqlite3.Connection) -> list[dict]:
    """Sessions whose WAV files may go to the Recycle Bin, with the reason each one is (not) ready."""
    out = []
    for media_id, path, lecture_id, sha in connection.execute(
            "SELECT id, path, lecture_id, sha256 FROM MediaAsset WHERE lecture_id IS NOT NULL AND is_master = 1 "
            "AND origin IN (?, ?)",
            APP_ORIGINS):
        master = Path(path)
        session = master.parent
        wavs = sorted(session.glob("segment_*.wav")) + sorted(session.glob("master_*.wav"))
        if not wavs:
            continue
        item = {"media_id": media_id, "session_dir": str(session), "wav": [str(p) for p in wavs],
                "bytes": sum(p.stat().st_size for p in wavs), "ready": False}
        reports = [json.loads(p.read_text(encoding="utf-8")) for p in session.glob("finalize_*.json")]
        if not any(r.get("verified") and Path(r.get("master", "")) == master for r in reports):
            item["reason"] = "압축 마스터 검증 기록 없음"
        elif (hold := range_hold(connection, media_id))[0]:
            item["reason"] = "녹음 범위(끝 지점) 결정 전"
        elif not (copy := copied_path(connection, media_id)) or not Path(copy).is_file() or (
                hold[1] == media_id and _sha256(Path(copy)) != sha):
            item["reason"] = "녹음본 복사본이 없거나 원본과 다름"
        elif not _finished(connection, lecture_id):
            item["reason"] = "완성 전사본(3엔진 통합 → 자료 검증 → 문맥 정리) 전"
        else:
            item.update(ready=True, reason="조건 충족")
        out.append(item)
    return out


def clean(connection: sqlite3.Connection, trash=to_recycle_bin) -> list[dict]:
    """Recycle the WAV files of ready sessions; the caller commits (events only)."""
    done = []
    for item in candidates(connection):
        if not item["ready"]:
            continue
        trash([Path(p) for p in item["wav"]])
        EventStore(connection).append("WAV_RECYCLED", "MediaAsset", item["media_id"],
                                      {"files": len(item["wav"]), "bytes": item["bytes"]})
        done.append(item)
    return done
