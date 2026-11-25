"""D-083 ``request_notice_rescan``: the small request files an AI app leaves for the running app.

The MCP process never reads a transcript out to a company itself: it leaves one file per lecture under
``_lecturehub/ai_link/rescan`` and the app's notice loop (ui/app.py) reads that lecture with its own engine and the
course's existing consent, then removes the file. Plain files only, no database import, so the app's recording
path can look at the queue (D-050, tests/test_db_guard.py).
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

MAX_PENDING = 5              # requests waiting at once; one more is refused until the app has read some
STALE_MS = 2 * 86_400_000    # a request the app did not get to in two days is dropped (the app was not opened)
PER_DAY = 10                 # D-083 review H2: lectures read again on request per day, in all
DONE = "done.jsonl"          # the app's own record of what it read on request (not *.json: never taken for a request)


def today() -> str:
    from datetime import datetime

    return datetime.now().astimezone().date().isoformat()


def read_today(root: Path) -> list[str]:
    """Lecture ids the app read again on request today (one line per reading in ``done.jsonl``)."""
    day, found = today(), []
    try:
        lines = (folder(root) / DONE).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and item.get("day") == day and isinstance(item.get("lecture_id"), str):
            found.append(item["lecture_id"])
    return found


def mark_read(root: Path, lecture_id: str) -> None:
    """Put one reading on record (today's lines only are kept)."""
    day = today()
    keep = [json.dumps({"lecture_id": x, "day": day}, ensure_ascii=False) for x in read_today(root)]
    keep.append(json.dumps({"lecture_id": lecture_id, "day": day}, ensure_ascii=False))
    path = folder(root) / DONE
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(DONE + ".tmp")
    temp.write_text("\n".join(keep) + "\n", encoding="utf-8")
    os.replace(temp, path)


def folder(root: Path) -> Path:
    return Path(root) / "_lecturehub" / "ai_link" / "rescan"


def _path(root: Path, lecture_id: str) -> Path:
    return folder(root) / (hashlib.sha256(str(lecture_id).encode("utf-8")).hexdigest()[:24] + ".json")


def _now_ms() -> int:
    return int(time.time() * 1000)


def is_pending(root: Path, lecture_id: str) -> bool:
    return _path(root, lecture_id).is_file()


def pending(root: Path) -> list[dict]:
    """Requests waiting, oldest first. A file of the wrong shape, or one too old, is left out (and removed by
    ``drop_stale``)."""
    found = []
    try:
        files = sorted(folder(root).glob("*.json"))
    except OSError:
        return []
    now = _now_ms()
    for file in files:
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not (isinstance(data, dict) and isinstance(data.get("lecture_id"), str) and data["lecture_id"]
                and isinstance(data.get("at_ms"), int) and file.name == _path(root, data["lecture_id"]).name):
            continue
        if now - data["at_ms"] > STALE_MS:
            continue
        found.append(data)
    return sorted(found, key=lambda d: d["at_ms"])


def count(root: Path) -> int:
    return len(pending(root))


def add(root: Path, lecture_id: str, change_id: str, course: str) -> Path | None:
    """Leave one request (exclusive create). None when this lecture already has one waiting."""
    path = _path(root, lecture_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps({"lecture_id": lecture_id, "change_id": change_id, "course": course, "at_ms": _now_ms()},
                      ensure_ascii=False).encode("utf-8")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0))
    except FileExistsError:
        return None
    try:
        os.write(fd, body)
    finally:
        os.close(fd)
    return path


def remove(root: Path, lecture_id: str, change_id: str | None = None) -> bool:
    """Remove a lecture's request (only the one of ``change_id`` when given). True when one was removed."""
    path = _path(root, lecture_id)
    if change_id is not None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if not isinstance(data, dict) or data.get("change_id") != change_id:
            return False
    try:
        path.unlink()
        return True
    except OSError:
        return False


def drop_stale(root: Path) -> None:
    """Remove files that ``pending`` leaves out (too old or of the wrong shape)."""
    keep = {_path(root, d["lecture_id"]).name for d in pending(root)}
    try:
        files = list(folder(root).glob("*.json"))
    except OSError:
        return
    for file in files:
        if file.name not in keep:
            try:
                file.unlink()
            except OSError:
                pass
