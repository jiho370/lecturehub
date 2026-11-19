"""Atomic sidecars and a crash-released lock shared by capture and recovery."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .paths import ensure_writable


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def write_json(path: Path, payload: dict) -> None:
    path = ensure_writable(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    # Windows readers can briefly deny delete-sharing during replace. Retry on
    # the disk worker only; the audio callback never touches these files.
    for attempt in range(20):
        try:
            os.replace(temporary, path)
            break
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.01)


class SessionLock:
    """An OS lock, not a stale PID heuristic: released even after terminate()."""

    def __init__(self, session_dir: Path):
        self.path = ensure_writable(Path(session_dir) / ".recording.lock")
        self.stream = None

    def __enter__(self):
        self.stream = self.path.open("a+b")
        self.stream.seek(0, os.SEEK_END)
        if self.stream.tell() == 0:
            self.stream.write(b"\0")
            self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.stream.close()
            self.stream = None
            raise RuntimeError("사용 중인 세션입니다. 녹음/복구/병합을 먼저 끝내세요.") from exc
        return self

    def __exit__(self, *args):
        if self.stream is not None:
            self.stream.close()
            self.stream = None


session_lock = SessionLock
