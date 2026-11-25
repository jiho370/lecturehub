"""What AI apps changed through the AI app link (D-076): the log, the change marker, the short write connection and
the undo the app offers.

- Log: ``<root>/_lecturehub/ai_link/changes.jsonl``, one JSON object per line, append-only:
  ``{"id", "at", "at_ms", "tool", "course", "summary", "undo": {...}}`` for a change,
  ``{"undo_of": id, "at", "at_ms", "by": "user_app"}`` when the user undid it in the app, and
  ``{"void_of": id, ...}`` when a write was logged but its transaction then failed (nothing changed).
  The change's line is written before the database commit, so a change that landed always has its undo
  information; a commit that fails voids the line again.
- Marker: ``<root>/_lecturehub/ai_link/changed`` is rewritten after every commit (and every undo). The running app
  compares its time and size on its state poll and reloads the library/calendar (no restart).
- Write connection: opened for one call, ``busy_timeout`` 2 s, WAL as the app keeps it, ``BEGIN IMMEDIATE`` for a
  short transaction, closed at once; nothing is held between calls (D-050: the recorder never touches the database,
  and a write here never waits more than the busy timeout). Refused when the database's schema version is not
  exactly the one this code knows (D-051 and the other way round: an older database waits for the app to migrate).
- Undo is done by the app only (settings › AI › AI 앱 연결 › AI가 바꾼 것), never through MCP. An item is undone only
  while it is still as the AI app left it; anything changed since is left alone with a message.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from lecturehub.core.ids import new_ulid

LOG_NAME = "changes.jsonl"
MARKER = "changed"
BUSY_MS = 2_000
READ_TAIL = 2 * 1024 * 1024  # bytes of the log read for the recent list
UNDONE_NOTES = "되돌린 정리본"
SCHEMA_MISMATCH = ("Lecture Hub 수업 목록의 버전이 이 연결 프로그램과 달라 바꾸지 않았습니다. Lecture Hub 앱을 최신으로 "
                   "바꾸고 한 번 연 뒤 다시 시도하세요.")
NOT_WAL = "Lecture Hub 앱을 한 번 열어 수업 목록을 준비한 뒤 다시 시도하세요."
BUSY = "Lecture Hub가 지금 수업 목록을 쓰는 중이라 바꾸지 못했습니다. 잠시 뒤 다시 시도하세요."


class WriteRefused(Exception):
    """A write that cannot go ahead; ``str()`` is the message for the AI app (or the app's toast)."""


def folder(root: Path) -> Path:
    return Path(root) / "_lecturehub" / "ai_link"


def log_path(root: Path) -> Path:
    return folder(root) / LOG_NAME


def marker_path(root: Path) -> Path:
    return folder(root) / MARKER


def signature(root: Path) -> tuple[int, int] | None:
    """(mtime_ns, size) of the marker; None when no AI app has changed anything yet. A cheap stat for the app's poll."""
    try:
        stat = marker_path(root).stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


def _now() -> tuple[str, int]:
    now = datetime.now().astimezone()
    return now.isoformat(timespec="seconds"), time.time_ns() // 1_000_000


def entry(tool: str, course: str | None, summary: str, undo: dict, **extra) -> dict:
    at, at_ms = _now()
    return {"id": new_ulid(), "at": at, "at_ms": at_ms, "tool": tool, "course": course, "summary": summary[:300],
            "undo": undo, **extra}


def append(root: Path, record: dict) -> None:
    """One line, UTF-8, flushed to disk (a change's undo information must survive a crash right after the commit)."""
    path = log_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())


def touch(root: Path) -> None:
    """Tell a running app that something changed (it polls ``signature``)."""
    path = marker_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    _, at_ms = _now()
    path.write_text(f"{at_ms} {new_ulid()}\n", encoding="utf-8")


def records(root: Path) -> list[dict]:
    path = log_path(root)
    try:
        with open(path, "rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            handle.seek(max(0, size - READ_TAIL))
            raw = handle.read()
    except OSError:
        return []
    lines = raw.decode("utf-8", "replace").splitlines()
    if len(raw) >= READ_TAIL and lines:
        lines = lines[1:]  # the first line may be cut
    out = []
    for line in lines:
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            out.append(value)
    return out


def count_since(root: Path, since_ms: int) -> int:
    """Changes AI apps made from ``since_ms`` on (undone ones count, voided ones do not): the daily cap."""
    every = records(root)
    void = {r["void_of"] for r in every if isinstance(r.get("void_of"), str)}
    return sum(1 for r in every if isinstance(r.get("id"), str) and "tool" in r and r["id"] not in void
               and isinstance(r.get("at_ms"), int) and r["at_ms"] >= since_ms)


def recent(root: Path, limit: int = 30) -> list[dict]:
    """The latest changes, newest first, each with ``undone`` (and when) and ``undoable``."""
    every = records(root)
    undone = {r["undo_of"]: r.get("at") for r in every if isinstance(r.get("undo_of"), str)}
    void = {r["void_of"] for r in every if isinstance(r.get("void_of"), str)}
    changes = [r for r in every if isinstance(r.get("id"), str) and "tool" in r and r["id"] not in void]
    out = []
    for r in reversed(changes[-limit:]):
        out.append({"id": r["id"], "at": r.get("at"), "at_ms": r.get("at_ms"), "tool": r.get("tool"), "course": r.get("course"),
                    "summary": r.get("summary"), "undone": r["id"] in undone, "undone_at": undone.get(r["id"]),
                    "undoable": r["id"] not in undone and isinstance(r.get("undo"), dict)})
    return out


# ------------------------------------------------------------------ the write connection
@contextmanager
def writer(database: Path):
    """A writable connection for one call; ``BEGIN IMMEDIATE`` is the caller's (``transaction``)."""
    from lecturehub.core import db

    database = Path(database)
    if not database.is_file():
        raise WriteRefused(NOT_WAL)
    db._precheck_file(database)  # D-051 before opening for writing (raises SchemaTooNewError)
    connection = sqlite3.connect(str(database), timeout=BUSY_MS / 1000, isolation_level=None)
    try:
        connection.execute(f"PRAGMA busy_timeout = {BUSY_MS}")
        if str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower() != "wal":
            raise WriteRefused(NOT_WAL)
        if db.check_schema_version(connection) != db.known_schema_version():
            raise WriteRefused(SCHEMA_MISMATCH)
        db._register_functions(connection)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA recursive_triggers = ON")
        connection.text_factory = lambda raw: raw.decode("utf-8", "replace")
        connection.row_factory = sqlite3.Row
        yield connection
    finally:
        connection.close()


def _begin(connection: sqlite3.Connection) -> None:
    try:
        connection.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc) or "busy" in str(exc):
            raise WriteRefused(BUSY) from exc
        raise


@contextmanager
def trial(connection: sqlite3.Connection):
    """A write tried out and always rolled back (D-080): what it would change, for the confirmation dialog. As short
    as a real write, and over before the dialog is shown."""
    _begin(connection)
    try:
        yield connection
    finally:
        connection.execute("ROLLBACK")


@contextmanager
def transaction(connection: sqlite3.Connection):
    _begin(connection)
    try:
        yield connection
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    try:
        connection.execute("COMMIT")
    except sqlite3.OperationalError as exc:
        connection.execute("ROLLBACK")
        if "locked" in str(exc) or "busy" in str(exc):
            raise WriteRefused(BUSY) from exc
        raise


# ------------------------------------------------------------------ undo (the app only)
CHANGED_SINCE = "그 뒤에 바뀐 항목이라 되돌리지 않았습니다. 앱에서 직접 고쳐 주세요."


def _find(root: Path, change_id: str) -> dict:
    every = records(root)
    if any(r.get("undo_of") == change_id for r in every):
        raise WriteRefused("이미 되돌린 항목입니다.")
    if any(r.get("void_of") == change_id for r in every):
        raise WriteRefused("실제로 바뀌지 않은 항목입니다.")
    found = next((r for r in every if r.get("id") == change_id and isinstance(r.get("undo"), dict)), None)
    if found is None:
        raise WriteRefused("되돌릴 항목을 찾지 못했습니다.")
    return found


def undo(root: Path, change_id: str) -> dict:
    """Undo one change (the app's [되돌리기]). Returns {"ok", "message"}; raises WriteRefused with the reason."""
    from lecturehub.core.paths import database_path

    from . import more_writes

    found = _find(root, change_id)
    info = found["undo"]
    moved: list[tuple[Path, Path]] = []
    reverts: list = []  # D-083: puts back a file or setting the undo changed when the database part fails
    with writer(database_path(root)) as connection:
        try:
            with transaction(connection):
                if info.get("kind") == "calendar":
                    message = _undo_calendar(connection, info)
                elif info.get("kind") == "note":
                    message = _undo_note(connection, root, info, moved)
                elif info.get("kind") in more_writes.UNDO:
                    message = more_writes.UNDO[info["kind"]](connection, Path(root), info, reverts)
                else:
                    raise WriteRefused("되돌릴 수 없는 종류입니다.")
        except BaseException:
            for target, source in moved:  # the file goes back where it was when the database part failed
                try:
                    target.replace(source)
                except OSError:
                    pass
            for put_back in reversed(reverts):
                try:
                    put_back()
                except Exception:  # noqa: BLE001, S110 - one that fails must not stop the others
                    pass
            raise
    # after the commit: a line that failed to be written leaves the item "not undone", and a second press then finds
    # it changed (CHANGED_SINCE) instead of undoing twice
    at, at_ms = _now()
    append(root, {"undo_of": change_id, "at": at, "at_ms": at_ms, "by": "user_app"})
    touch(root)
    return {"ok": True, "message": message}


def undo_since(root: Path, change_id: str) -> dict:
    """"이 시각 이후 AI가 바꾼 것 모두 되돌리기": every change from that one's time on, newest first (so an edit is
    taken back before the add it edited), each with ``undo``. One that was changed since, already undone or cannot be
    undone is skipped and counted, never forced. Returns {"ok", "undone", "skipped", "message"}."""
    every = records(root)
    start = next((r for r in every if r.get("id") == change_id and "tool" in r), None)
    if start is None or not isinstance(start.get("at_ms"), int):
        raise WriteRefused("되돌릴 항목을 찾지 못했습니다.")
    undone = {r["undo_of"] for r in every if isinstance(r.get("undo_of"), str)}
    void = {r["void_of"] for r in every if isinstance(r.get("void_of"), str)}
    since = start["at_ms"]
    wanted = [r for r in every if isinstance(r.get("id"), str) and "tool" in r and isinstance(r.get("at_ms"), int)
              and r["at_ms"] >= since and r["id"] not in undone and r["id"] not in void and isinstance(r.get("undo"), dict)]
    done, skipped = 0, []
    for record in reversed(wanted):  # the log is in time order: newest first
        try:
            undo(root, record["id"])
            done += 1
        except WriteRefused as exc:
            skipped.append({"id": record["id"], "summary": record.get("summary"), "why": str(exc)})
    message = f"{done}건을 되돌렸습니다." + (f" {len(skipped)}건은 그 뒤에 바뀌었거나 되돌릴 수 없어 그대로 두었습니다."
                                         if skipped else "")
    return {"ok": True, "undone": done, "skipped": skipped, "message": message}


def _undo_calendar(connection: sqlite3.Connection, info: dict) -> str:
    from lecturehub.index import calendar_events

    current = calendar_events.snapshot(connection, str(info.get("event_id")))
    if current is None:
        raise WriteRefused("일정을 찾지 못했습니다.")
    after = info.get("after") or {}
    if any(current.get(k) != v for k, v in after.items()):
        raise WriteRefused(CHANGED_SINCE)
    before = info.get("before")
    calendar_events.restore(connection, info["event_id"], before if isinstance(before, dict) else {"status": "cancelled"},
                            by="user_undo")
    return "일정을 AI 앱이 바꾸기 전으로 되돌렸습니다." if isinstance(before, dict) else "AI 앱이 넣은 일정을 취소했습니다(기록은 남음)."


def _undo_note(connection: sqlite3.Connection, root: Path, info: dict, moved: list) -> str:
    """Unlink the note from its lecture and move the page the AI app wrote out of the course folder into
    ``_lecturehub/ai_link/되돌린 정리본`` (kept, never deleted). A page edited since (by the user, the /lecture-note
    skill, …) is not undone at all: it stays attached and where it is (integration review)."""
    from lecturehub.core.events import append as event

    row = connection.execute("SELECT lecture_ids, path, inputs FROM StudyArtifact WHERE id = ?",
                             (str(info.get("artifact_id")),)).fetchone()
    if row is None:
        raise WriteRefused("정리본을 찾지 못했습니다.")
    if json.loads(row["lecture_ids"] or "[]") != info.get("lecture_ids") or row["path"] != info.get("path"):
        raise WriteRefused(CHANGED_SINCE)
    page = Path(row["path"])
    inputs = json.loads(row["inputs"] or "{}")
    message = "AI 앱이 쓴 정리본을 회차에서 떼고 파일을 앱 폴더(_lecturehub\\ai_link\\되돌린 정리본)로 옮겼습니다."
    new_path = page
    if page.is_file():
        import hashlib

        if hashlib.sha256(page.read_bytes()).hexdigest() != info.get("sha256"):
            raise WriteRefused(CHANGED_SINCE)  # edited since: stays attached and where it is
        target_dir = folder(root) / UNDONE_NOTES
        target_dir.mkdir(parents=True, exist_ok=True)
        new_path, n = target_dir / page.name, 1
        while new_path.exists():
            n += 1
            new_path = target_dir / f"{page.stem}_{n}{page.suffix}"
        page.replace(new_path)
        moved.append((new_path, page))
    else:
        message = "정리본을 회차에서 뗐습니다(파일은 이미 없었습니다)."
    inputs.update(undone_at=_now()[1], undone_from=str(page))
    connection.execute("UPDATE StudyArtifact SET lecture_ids = '[]', path = ?, inputs = ? WHERE id = ?",
                       (str(new_path), json.dumps(inputs, ensure_ascii=False), info["artifact_id"]))
    event(connection, "STUDY_ARTIFACT_UNLINKED", "StudyArtifact", info["artifact_id"],
          {"lecture_ids": info.get("lecture_ids"), "by": "user_undo"})
    return message
