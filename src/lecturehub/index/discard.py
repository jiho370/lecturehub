"""D-058: throw away a recording made by mistake — to the Windows Recycle Bin, never a hard delete.

What goes to the Recycle Bin (only after the user confirms in the app):
- the recording's session folder under ``_lecturehub\\recordings`` (segments, master, status files),
  or the master file itself when it lives elsewhere inside ``_lecturehub``;
- derived clips of it (D-057 cuts) under ``_lecturehub``;
- the copy the app made in ``녹음본\\<과목>\\`` (D-055) — only when its bytes still match what was
  copied (user-approved exception to "course folders are add-only").
Nothing outside those is touched. DB rows stay (raw records are immutable): an Evidence ``discarded``
(source user) hides the recording and its transcripts from lectures, search, notes, copies and
automatic transcription. Undo ("되살리기") takes the files back out of the Recycle Bin by itself.

Recording sessions that were never linked (test runs, pending ones) can be thrown away as folders.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

from lecturehub.core.events import append
from lecturehub.core.ids import new_ulid


def _evidence(connection, subject_id: str, kind: str, detail: dict) -> None:
    connection.execute(
        "INSERT INTO Evidence (id, subject_type, subject_id, field, kind, detail, weight, source, created_at) "
        "VALUES (?, 'MediaAsset', ?, NULL, ?, ?, NULL, 'user', ?)",
        (new_ulid(), subject_id, kind, json.dumps(detail, ensure_ascii=False), time.time_ns() // 1_000_000))


def discarded_ids(connection: sqlite3.Connection) -> set[str]:
    """Recordings whose latest decision is 'discarded' (not brought back)."""
    latest: dict[str, str] = {}
    for subject_id, kind in connection.execute(
            "SELECT subject_id, kind FROM Evidence WHERE subject_type = 'MediaAsset' AND source = 'user' "
            "AND kind IN ('discarded', 'discard_revoked') ORDER BY created_at, rowid"):
        latest[subject_id] = kind
    return {media_id for media_id, kind in latest.items() if kind == "discarded"}


def hidden_ids(connection: sqlite3.Connection) -> set[str]:
    """Recordings not shown, copied or searched: thrown away (D-058) or joined into another (index/join.py)."""
    from .join import joined_parts

    return discarded_ids(connection) | joined_parts(connection)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _inside(path: Path, folder: Path) -> bool:
    try:
        return path.resolve().is_relative_to(folder.resolve())
    except OSError:
        return False


def _recording_now(folder: Path) -> bool:
    if not ((folder / ".recording.lock").exists() and not (folder / "stop.json").exists()):
        return False
    from lecturehub.recorder.heartbeat import SessionLock

    try:
        with SessionLock(folder):
            return False
    except RuntimeError:
        return True


def _transcribing(root: Path, audio_paths: set[str], session: Path | None = None) -> bool:
    """A job still working on this audio — including a live job (D-059), which knows only its session
    folder until the recording's master exists."""
    from lecturehub.transcription.providers import job_roots

    for state_path in (path for jobs in job_roots(Path(root)) if jobs.is_dir() for path in jobs.glob("*/state.json")):
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if state.get("state") not in ("queued", "running", "retrying", "waiting_for_quota"):
            continue
        live_session = (state.get("live") or {}).get("session_dir")
        if state.get("audio") in audio_paths or (session is not None and live_session
                                                  and Path(live_session).name == session.name):
            return True
    return False


def plan(connection: sqlite3.Connection, root: Path, media_id: str) -> dict:
    """What [버리기] would do, for the confirmation dialog. Raises ValueError when it must not."""
    from .archive_copy import copied_path

    row = connection.execute("SELECT path, lecture_id, sha256 FROM MediaAsset WHERE id = ? AND is_master = 1",
                             (media_id,)).fetchone()
    if row is None:
        raise ValueError("원본 녹음을 찾지 못했습니다")
    app_dir = Path(root) / "_lecturehub"
    master = Path(row[0])
    recordings = app_dir / "recordings"
    # an app session folder, or the folder of a recording brought in from outside (index/import_audio.py)
    session = next((master.parent for base in (recordings, app_dir / "imports")
                    if _inside(master, base) and master.parent != base), None)
    if session is not None and _recording_now(session):
        raise ValueError("녹음 중인 세션은 버릴 수 없습니다. 녹음을 멈춘 뒤 다시 누르세요")
    trash: list[Path] = []
    if session is not None:
        trash.append(session)
    elif _inside(master, app_dir):
        trash.append(master)
    derived = [Path(r[0]) for r in connection.execute("SELECT path FROM MediaAsset WHERE derived_from = ?", (media_id,))]
    trash += [p for p in derived if _inside(p, app_dir) and p.exists()]
    audio = {str(master.resolve())} | {str(p.resolve()) for p in derived}
    if _transcribing(root, audio | {str(master)}, session):
        raise ValueError("이 녹음의 전사가 진행 중입니다. 전사를 취소하거나 끝난 뒤 다시 누르세요")
    archive, archive_note = copied_path(connection, media_id), None
    archive_digest = None
    if archive:
        for (detail,) in connection.execute(
                "SELECT detail FROM Evidence WHERE subject_type='MediaAsset' AND subject_id=? AND kind='source_alias'",
                (media_id,)):
            data = json.loads(detail)
            if data.get("path") == archive:
                archive_digest = data.get("sha256")
        copy = Path(archive)
        if not copy.is_file():
            archive, archive_note = None, "녹음본 폴더의 복사본은 이미 없습니다"
        elif archive_digest and _sha256(copy) != archive_digest:
            archive, archive_note = None, "녹음본 폴더의 파일이 복사 뒤 바뀌어서 그대로 둡니다"
        else:
            trash.append(copy)
    size = sum(p.stat().st_size for q in trash for p in ([q] if q.is_file() else q.rglob("*")) if p.is_file())
    transcripts = connection.execute("SELECT count(*) FROM Transcript WHERE media_id = ?", (media_id,)).fetchone()[0]
    return {"media_id": media_id, "lecture_id": row[1], "lecture": _lecture_label(connection, row[1]),
            "trash": [str(p) for p in trash], "archive": archive,
            "archive_note": archive_note, "bytes": size, "transcripts": transcripts,
            "pair": _pair(connection, session) if session is not None else None}


def _lecture_label(connection: sqlite3.Connection, lecture_id: str | None) -> str | None:
    """'AI 외부 강의 2강 (9/29)' — which lecture a recording belongs to, for confirmations and the cleanup list."""
    if not lecture_id:
        return None
    row = connection.execute("SELECT c.id, c.folder_name, l.date, l.session_index FROM Lecture l JOIN Course c "
                             "ON c.id = l.course_id WHERE l.id = ?", (lecture_id,)).fetchone()
    if row is None:
        return None
    from .lectures import label

    name = label(connection, lecture_id)
    if name is None:  # a lecture the list leaves out (only thrown-away recordings): count it by position
        name = str(connection.execute(
            "SELECT count(*) FROM Lecture WHERE course_id = ? AND (date < ? OR (date = ? AND session_index <= ?))",
            (row[0], row[2], row[2], row[3])).fetchone()[0]) + "강"
    month, day = int(row[2][5:7]), int(row[2][8:10])
    return f"{row[1]} {name} ({month}/{day})"


def _pair(connection: sqlite3.Connection, session: Path) -> dict | None:
    """The other session recorded together with this one (mic + PC sound, pair.json)."""
    try:
        data = json.loads((session / "pair.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    other = next((Path(v) for v in (data.get("mic"), data.get("pc")) if v and Path(v).name != session.name), None)
    if other is None or not other.is_dir():
        return None
    row = connection.execute("SELECT id FROM MediaAsset WHERE is_master = 1 AND path LIKE ?",
                             (f"%{other.name}%",)).fetchone()
    transcripts = connection.execute("SELECT count(*) FROM Transcript WHERE media_id = ?",
                                     (row[0],)).fetchone()[0] if row else 0
    return {"session": other.name, "media_id": row[0] if row else None, "transcripts": transcripts,
            "kind": "PC 소리" if data.get("pc") and Path(data["pc"]).name == other.name else "마이크"}


def discard(connection: sqlite3.Connection, root: Path, media_id: str, *, with_pair: bool = False,
            trash: Callable[[list[Path]], None] | None = None) -> dict:
    """Move the files to the Recycle Bin and hide the recording. Caller commits."""
    from .lectures import refresh_policy
    from .wav_cleanup import to_recycle_bin

    if media_id in discarded_ids(connection):
        raise ValueError("이미 버린 녹음입니다")
    what = plan(connection, root, media_id)
    paths = [Path(p) for p in what["trash"] if Path(p).exists()]
    if paths:
        (trash or to_recycle_bin)(paths)
    _evidence(connection, media_id, "discarded", {"recycle_bin": what["trash"], "archive": what["archive"],
                                                  "archive_note": what["archive_note"], "decided_by": "user"})
    append(connection, "MEDIA_DISCARDED", "MediaAsset", media_id, {"recycle_bin": what["trash"]})
    if what["lecture_id"]:
        refresh_policy(connection, what["lecture_id"])  # another transcript of the lecture may take over
    pair = what.get("pair")
    if with_pair and pair:  # the mic / PC-sound partner recorded at the same time
        if pair["media_id"] and pair["media_id"] not in discarded_ids(connection):
            discard(connection, root, pair["media_id"], trash=trash)
        elif not pair["media_id"]:
            discard_session(connection, root, pair["session"], trash=trash)
    return what


def lecture_plan(connection: sqlite3.Connection, root: Path, lecture_id: str) -> dict:
    """[이 회차 버리기] (user 2026-09-29: "3강 폐기하고 다시 녹음"): every recording of the lecture that is
    still shown, in one confirmation. Transcripts imported without a recording and matched slides stay."""
    gone = hidden_ids(connection)
    masters = [r[0] for r in connection.execute(
        "SELECT id FROM MediaAsset WHERE lecture_id = ? AND is_master = 1 ORDER BY recorded_start, id", (lecture_id,))
        if r[0] not in gone]
    if not masters:
        raise ValueError("이 회차에는 버릴 녹음이 없습니다(가져온 전사만 있는 회차는 여기서 버리지 않습니다)")
    plans = [plan(connection, root, media_id) for media_id in masters]
    pairs, seen = [], set(masters)
    for p in plans:
        pair = p.get("pair")
        if pair and pair.get("media_id") not in seen and pair.get("media_id") not in gone and pair["session"] not in seen:
            seen.add(pair.get("media_id") or pair["session"])
            pairs.append(pair)
    leaving = [*masters, *gone]  # transcripts of recordings already thrown away are not shown either
    stays = connection.execute(
        f"SELECT count(*) FROM Transcript WHERE lecture_id = ? AND (media_id IS NULL OR media_id NOT IN "
        f"({','.join('?' * len(leaving))}))", (lecture_id, *leaving)).fetchone()[0]
    slides = connection.execute("SELECT count(*) FROM MaterialMatch WHERE lecture_id = ?", (lecture_id,)).fetchone()[0]
    return {"lecture_id": lecture_id, "lecture": _lecture_label(connection, lecture_id), "recordings": plans,
            "trash": [t for p in plans for t in p["trash"]], "bytes": sum(p["bytes"] for p in plans),
            "transcripts": sum(p["transcripts"] for p in plans), "pairs": pairs,
            "stays": {"transcripts": stays, "slides": slides}}


def discard_lecture(connection: sqlite3.Connection, root: Path, lecture_id: str, *, with_pair: bool = False,
                    trash: Callable[[list[Path]], None] | None = None) -> dict:
    """Throw away all shown recordings of a lecture (Recycle Bin, same as [버리기] one by one). Caller commits."""
    what = lecture_plan(connection, root, lecture_id)
    for p in what["recordings"]:
        if p["media_id"] not in discarded_ids(connection):
            discard(connection, root, p["media_id"], with_pair=with_pair, trash=trash)
    append(connection, "LECTURE_RECORDINGS_DISCARDED", "Lecture", lecture_id,
           {"media": [p["media_id"] for p in what["recordings"]], "with_pair": bool(with_pair)})
    return what


def restore(connection: sqlite3.Connection, media_id: str, *, undelete: Callable | None = None) -> dict:
    """Bring a thrown-away recording back: its files come back from the Recycle Bin by themselves
    (user 2026-09-29: a mic discard also took the PC-sound recording and its transcript), then it shows again."""
    from .wav_cleanup import from_recycle_bin

    if media_id not in discarded_ids(connection):
        raise ValueError("버린 녹음이 아닙니다")
    row = connection.execute(
        "SELECT detail, created_at FROM Evidence WHERE subject_type = 'MediaAsset' AND subject_id = ? "
        "AND kind = 'discarded' ORDER BY created_at DESC, rowid DESC LIMIT 1", (media_id,)).fetchone()
    detail, discarded_at = (json.loads(row[0]), row[1]) if row else ({}, 0)
    master = connection.execute("SELECT path FROM MediaAsset WHERE id = ?", (media_id,)).fetchone()[0]
    wanted = [Path(p) for p in detail.get("recycle_bin", [])]
    back = (undelete or from_recycle_bin)(wanted, not_before_ms=discarded_at - 60_000) if wanted else []
    if not Path(master).is_file():
        raise ValueError(f"휴지통에서 '{Path(master).parent.name}'을(를) 찾지 못했습니다(휴지통을 비웠을 수 있음)")
    _evidence(connection, media_id, "discard_revoked", {"decided_by": "user", "restored": [str(p) for p in back]})
    append(connection, "MEDIA_DISCARD_REVOKED", "MediaAsset", media_id, {"restored": [str(p) for p in back]})
    return {"restored": [str(p) for p in back]}


def sessions(connection: sqlite3.Connection, root: Path, *, now_ms: int | None = None) -> list[dict]:
    """Recording folders with what they are: linked, pending, not linked, recording, thrown away. App sessions
    and recordings brought in from outside (``imports``). A thrown-away one is in the Recycle Bin, so its folder
    is gone here: it stays listed for 30 days so that [되살리기] can be pressed."""
    folders = [Path(root) / "_lecturehub" / name for name in ("recordings", "imports")]
    gone = discarded_ids(connection)
    linked = {}
    for media_id, path, lecture_id in connection.execute("SELECT id, path, lecture_id FROM MediaAsset WHERE is_master = 1"):
        parent = Path(path).parent
        if parent.parent in folders:
            linked[parent.name] = (media_id, lecture_id, parent)
    segments = dict(connection.execute(
        "SELECT t.media_id, count(s.seq) FROM Transcript t JOIN TranscriptSegment s ON s.transcript_id = t.id "
        "GROUP BY t.media_id").fetchall())
    pending = {Path(r[0]).parent.name: (r[1], json.loads(r[2]).get("date")) for r in connection.execute(
        "SELECT source_path, id, suggestion FROM IngestItem WHERE state = 'pending'")}
    out = []
    for session in (p for folder in folders if folder.is_dir() for p in folder.iterdir() if p.is_dir()):
        media_id, lecture_id, _parent = linked.get(session.name, (None, None, None))
        state = ("recording" if _recording_now(session) else "discarded" if media_id in gone
                 else "linked" if lecture_id else "pending" if session.name in pending
                 else "needs_recovery" if _crashed(session) else "unlinked")
        size = sum(p.stat().st_size for p in session.rglob("*") if p.is_file())
        out.append({"name": session.name, "path": str(session), "state": state, "media_id": media_id,
                    "lecture_id": lecture_id, "bytes": size, "lecture": _lecture_label(connection, lecture_id),
                    "segments": segments.get(media_id, 0) if media_id else 0,
                    **({"ingest_id": pending[session.name][0], "date": pending[session.name][1]}
                       if state == "pending" else {})})
    listed = {row["name"] for row in out}
    since = (now_ms if now_ms is not None else time.time_ns() // 1_000_000) - 30 * 86_400_000
    thrown = dict(connection.execute(
        "SELECT subject_id, max(created_at) FROM Evidence WHERE subject_type = 'MediaAsset' AND source = 'user' "
        "AND kind = 'discarded' GROUP BY subject_id").fetchall())
    for name, (media_id, lecture_id, parent) in linked.items():
        if media_id in gone and name not in listed and thrown.get(media_id, 0) >= since:
            out.append({"name": name, "path": str(parent), "state": "discarded", "media_id": media_id,
                        "lecture_id": lecture_id, "bytes": 0, "lecture": _lecture_label(connection, lecture_id),
                        "segments": segments.get(media_id, 0)})
    return sorted(out, key=lambda row: row["name"], reverse=True)


def _crashed(session: Path) -> bool:
    """An app recording that ended without stop.json (crash, power loss) and was never merged: a real recording
    that still needs recovering, not test junk (§2.2). Imports have no session.json and are never this."""
    return ((session / "session.json").exists() and not (session / "stop.json").exists()
            and not any(session.glob("master*")))


CRASHED_REFUSAL = ("비정상 종료로 끝난 녹음입니다(복구 필요). 먼저 [복구]를 눌러 살려 보세요. "
                   "그래도 버리려면 두 번 확인해야 합니다")


def discard_session(connection: sqlite3.Connection, root: Path, name: str, *,
                    trash: Callable[[list[Path]], None] | None = None, confirm_crashed: bool = False) -> None:
    """A session folder that never became a lecture recording (test run, abandoned pending one).

    A crashed session (``needs_recovery``) is refused unless ``confirm_crashed`` came from a second confirmation."""
    from .wav_cleanup import to_recycle_bin

    info = next((s for s in sessions(connection, root) if s["name"] == name), None)
    if info is None:
        raise ValueError("그 녹음 세션을 찾지 못했습니다")
    if info["state"] == "recording":
        raise ValueError("녹음 중인 세션은 버릴 수 없습니다")
    if info["state"] == "needs_recovery" and not confirm_crashed:
        raise ValueError(CRASHED_REFUSAL)
    if info["media_id"]:
        raise ValueError("회차에 등록된 녹음입니다. 회차 화면이나 녹음 목록의 [버리기]를 쓰세요")
    (trash or to_recycle_bin)([Path(info["path"])])
    for (item_id,) in connection.execute("SELECT id FROM IngestItem WHERE state = 'pending' AND source_path LIKE ?",
                                         (f"%{name}%",)).fetchall():
        connection.execute("UPDATE IngestItem SET state = 'rejected' WHERE id = ?", (item_id,))
    append(connection, "RECORDING_SESSION_DISCARDED", "RecordingSession", name, {"recycle_bin": [info["path"]]})


def recover_session(root: Path, name: str) -> dict:
    """[복구] for a crashed session: the recorder's own recovery (only the last segment's length fields are fixed,
    every PCM byte kept, stop.json never made up), then its merge. Track A's public functions, imported only."""
    from lecturehub.recorder.finalize import finalize_session
    from lecturehub.recorder.recovery import recover_session as recorder_recover

    session = Path(root) / "_lecturehub" / "recordings" / name
    if name != Path(name).name or not session.is_dir():
        raise ValueError("그 녹음 세션을 찾지 못했습니다")
    if _recording_now(session):
        raise ValueError("녹음 중인 세션입니다")
    if not _crashed(session):
        raise ValueError("복구가 필요한 세션이 아닙니다")
    report = recorder_recover(session) if not (session / "recovery.json").exists() else {}
    merged = finalize_session(session)
    return {"recovery": report, "finalize": merged}
