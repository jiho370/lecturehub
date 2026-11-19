"""Atomic text/JSON writes (review #2).

A plain ``Path.write_text`` truncates the file first, so a crash, power loss or a full disk in the
middle leaves an empty or half-written memo/settings file. These helpers write to a temporary file
in the *same directory* (so ``os.replace`` stays on one volume and is atomic on NTFS and POSIX),
flush+fsync it, then swap it in.

Windows: ``os.replace`` fails with ``PermissionError`` while another process (antivirus, indexer,
OneDrive, an editor) briefly holds the target open. We retry a few times with a short backoff before
giving up; the temp file is always removed on failure so no ``*.tmp`` litter is left behind.

Paths with Korean letters, spaces and brackets are fine: nothing here builds shell strings.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

_RETRIES = 6
_BACKOFF_S = 0.05  # 50, 100, 200, 400, 800 ms → ~1.5 s worst case


def _replace(src: str, dst: Path, retries: int, backoff_s: float) -> None:
    for attempt in range(retries):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == retries - 1:
                raise
            time.sleep(backoff_s * (2 ** attempt))


def atomic_write_text(path: str | os.PathLike, text: str, *, encoding: str = "utf-8",
                      backup: bool = False, mkdir: bool = True, retries: int = _RETRIES, backoff_s: float = _BACKOFF_S) -> Path:
    """Write ``text`` to ``path`` atomically: readers see either the old file or the new one.

    The parent directory is created if missing (``mkdir=False``: a missing folder raises, e.g. a recorder
    session folder that should already exist must not be conjured up). With ``backup=True`` the previous content (if any) is
    copied to ``<name>.bak`` first. Newlines are translated exactly like ``Path.write_text`` (CRLF on
    Windows, LF elsewhere) so switching a call site over does not change the bytes on disk.
    """
    target = Path(path)
    if mkdir:
        target.parent.mkdir(parents=True, exist_ok=True)
    if backup and target.is_file():
        try:
            shutil.copy2(target, target.with_name(target.name + ".bak"))
        except OSError:
            pass  # a missing backup must never block the real save
    fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding=encoding) as handle:
            handle.write(text)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        _replace(tmp, target, retries, backoff_s)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return target


def atomic_write_json(path: str | os.PathLike, data: Any, *, indent: int | None = 1,
                      ensure_ascii: bool = False, **kwargs) -> Path:
    """``json.dumps`` then :func:`atomic_write_text`. Serialisation errors happen before any file is touched."""
    text = json.dumps(data, ensure_ascii=ensure_ascii, indent=indent)
    return atomic_write_text(path, text, **kwargs)
