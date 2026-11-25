"""Material page text prepared in the background for the AI app link (D-072).

The AI app link reads material text from MaterialPage only (the MCP process never parses a file). Before this,
MaterialPage was filled only by the command ``lecturehub materials extract``, so in normal use it stayed empty.
While the link is switched on, the app starts this module as a windowless, low-priority helper
(``<worker> -m lecturehub.materials.background --root <학기>``) when some material has no page text yet, never
while a recording runs (ui/app.py ``Api._material_text_kick``, which also stops a running helper when a recording
starts).

A file that gave no text (a format without text such as binary .ppt, an encrypted or distribution-protected HWP,
a file that could not be read, a file past the size bounds of ``extract``, a file changed or gone since indexing) is
written down in ``_lecturehub\\cache\\material_text.json`` with its sha256 and the reason as soon as it is done, so
it is not tried again and again; a changed file is a new Material (new id) after re-indexing, and a newer extractor
version tries everything again (version 2, D-078: .hwp/.hwpx/.docx are read now). Uses pypdfium2 / python-pptx and
the in-repo readers of materials/doctext.py only: no PowerPoint, no Hancom Office, no network.

Review 2026-11-24:
- H1: before each file the helper writes ``material_text.inprogress.json`` (material id, sha256) and clears it
  after. A file that kills the helper (native crash, out of memory, a kill) leaves the marker; the next run writes
  that file down as given up and goes on with the rest. The app clears the marker when it stops the helper itself
  for a recording (that file was not at fault).
- H4: one helper at a time across app restarts: the helper holds an OS file lock (the recorder's ``SessionLock``,
  released by the OS even after a kill) in ``cache\\material_text_helper\\`` and writes its pid and start time
  next to it, so the app can end a helper that has run past ``TIMEOUT_S``.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from .extract import EXTRACTOR_VERSION

RECORDING_REASON = "녹음 중"
CRASHED_REASON = "이 파일을 읽던 도우미가 끝까지 가지 못하고 멈춤(다시 시도하지 않음)"
TIMEOUT_S = 2 * 60 * 60  # a helper running longer than this is stuck: the app ends it


def _cache(root: Path) -> Path:
    return Path(root) / "_lecturehub" / "cache"


def state_path(root: Path) -> Path:
    return _cache(root) / "material_text.json"


def marker_path(root: Path) -> Path:
    return _cache(root) / "material_text.inprogress.json"


def lock_dir(root: Path) -> Path:
    """Holds the helper's lock file (named ``.recording.lock`` by SessionLock; nothing looks for recordings here)
    and ``helper.json`` (pid, start time)."""
    return _cache(root) / "material_text_helper"


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(temporary, path)


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _tried(root: Path) -> dict:
    data = _read_json(state_path(root))
    return data if data.get("version") == EXTRACTOR_VERSION else {}


def _given_up(connection: sqlite3.Connection, root: Path) -> set[str]:
    """Materials already tried without text whose file (sha256 in the database) is still the one tried."""
    tried = _tried(root).get("materials") or {}
    shas = dict(connection.execute("SELECT id, sha256 FROM Material"))
    return {mid for mid, entry in tried.items() if isinstance(entry, dict) and shas.get(mid) == entry.get("sha256")}


def pending(connection: sqlite3.Connection, root: Path) -> list[str]:
    """Material ids with no page text yet that are worth a try. One query and one small file: cheap to ask often.
    Works on a read-only connection."""
    missing = [row[0] for row in connection.execute(
        "SELECT m.id FROM Material m WHERE NOT EXISTS (SELECT 1 FROM MaterialPage p WHERE p.material_id = m.id) "
        "ORDER BY m.rowid")]
    if not missing:
        return []
    skip = _given_up(connection, root)
    return [mid for mid in missing if mid not in skip]


def _record(root: Path, material_id: str, sha256: str | None, reason: str) -> None:
    """Write one file down as tried without text, now (not at the end of the run: review H1)."""
    data = _tried(root)
    materials = dict(data.get("materials") or {})
    materials[material_id] = {"sha256": sha256, "reason": reason[:200], "at": int(time.time() * 1000)}
    _write_json(state_path(root), {"version": EXTRACTOR_VERSION, "materials": materials})


def clear_marker(root: Path) -> None:
    """The app calls this after it stopped the helper for a recording: the file in progress was not at fault."""
    try:
        marker_path(root).unlink(missing_ok=True)
    except OSError:
        pass


def _recover_marker(root: Path) -> str | None:
    """A marker left by a helper that died in the middle of a file: write that file down as given up."""
    marker = _read_json(marker_path(root))
    material_id = marker.get("material_id")
    if isinstance(material_id, str) and material_id:
        _record(root, material_id, marker.get("sha256"), CRASHED_REASON)
    clear_marker(root)
    return material_id if isinstance(material_id, str) else None


def running(root: Path) -> bool:
    """A helper holds the lock now (started by this app or by one before a restart)."""
    from lecturehub.recorder.heartbeat import SessionLock

    folder = lock_dir(root)
    if not (folder / ".recording.lock").exists():
        return False
    try:
        with SessionLock(folder):
            return False
    except RuntimeError:
        return True
    except OSError:
        return False


def helper_info(root: Path) -> dict:
    """{"pid", "started_at"} of the helper that holds (or last held) the lock."""
    return _read_json(lock_dir(root) / "helper.json")


def run(root: Path) -> dict:
    """Extract every pending material once; stops (between files and between pages) when a recording starts."""
    from lecturehub.core import db
    from lecturehub.core.paths import app_dir, database_path
    from lecturehub.recorder.heartbeat import SessionLock

    from .extract import extract

    root = Path(root)
    busy = lambda: db.recording_session(app_dir(root))  # asked again before each file and between pages
    if busy():
        return {"state": "recording"}
    folder = lock_dir(root)
    folder.mkdir(parents=True, exist_ok=True)
    try:
        lock = SessionLock(folder).__enter__()
    except RuntimeError:
        return {"state": "running"}  # another helper is at work
    try:
        _write_json(folder / "helper.json", {"pid": os.getpid(), "started_at": time.time()})
        recovered = _recover_marker(root)
        with closing(db.connect(database_path(root))) as connection:
            shas = dict(connection.execute("SELECT id, sha256 FROM Material"))
            remembered = [0]

            def before(material_id: str) -> None:
                _write_json(marker_path(root), {"material_id": material_id, "sha256": shas.get(material_id),
                                                "pid": os.getpid(), "at": int(time.time() * 1000)})

            def after(item: dict) -> None:
                clear_marker(root)
                reason = item.get("reason")
                if reason is None and item.get("pages") == 0:
                    reason = "쪽 없음"  # a document with no pages: nothing to store in MaterialPage either
                if reason and not reason.startswith(RECORDING_REASON):
                    _record(root, item["material_id"], shas.get(item["material_id"]), reason)
                    remembered[0] += 1

            report = extract(connection, root, busy=busy, skip=_given_up(connection, root), before=before,
                             after=after)
    finally:
        clear_marker(root)  # a Python error is not the file's fault; only a dead process leaves the marker
        lock.__exit__(None, None, None)
    stopped = any(item["reason"].startswith(RECORDING_REASON) for item in report.skipped)
    return {"state": "recording" if stopped else "done", "materials": len(report.extracted), "pages": report.pages,
            "no_text": remembered[0] + (1 if recovered else 0),
            "capped": [item["material_id"] for item in report.extracted if item.get("capped_at")]}


def main(argv: list[str] | None = None) -> int:
    import argparse

    from lecturehub.core import applog

    parser = argparse.ArgumentParser(prog="lecturehub.materials.background")
    parser.add_argument("--root", required=True)
    args = parser.parse_args(argv)
    applog.setup("worker", network=False)
    try:
        result = run(Path(args.root))
    except Exception:  # noqa: BLE001 - a background helper: the reason goes to the log, the app tries again later
        applog.LOG.error("자료 글 준비 실패", exc_info=True)
        return 1
    applog.LOG.info("자료 글 준비: %s", {k: v for k, v in result.items()})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
