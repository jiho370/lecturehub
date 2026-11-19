"""The app's own log (release review 2026-11-16).

On the owner's PC a failure was looked at in a console. On someone else's PC there is no console and nobody to
look, so before this file a background failure left no trace at all. Now the window and its helper processes
write to ``%LOCALAPPDATA%\\LectureHub\\logs``:

- ``app.log`` / ``worker.log``: what started, warnings, and every error with its traceback, including errors
  nobody caught (``sys.excepthook``, ``threading.excepthook``);
- ``app_crash.log`` / ``worker_crash.log``: what Python was doing if the process died hard (``faulthandler``).

Rules:
- never the content of a lecture: no transcript text, no note text, no audio. Messages are about what the app
  did and what failed;
- anything that looks like a key or token is replaced before it is written (``redact``);
- logging can never stop the app or a recording (D-050): every step here swallows its own failures.

Several processes append to the same file (short lines, append mode). A file is rolled over only when a process
starts and finds it too big, so no process ever renames a file another one is writing.
"""
from __future__ import annotations

import logging
import os
import re
import sys
import threading
from pathlib import Path

LOG = logging.getLogger("lecturehub")
MAX_BYTES = 1_000_000
KEEP = 3
_state: dict = {"process": None, "crash_file": None}

_SECRETS = [
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),                       # Google API key
    re.compile(r"\bgsk_[0-9A-Za-z]{20,}"),                        # Groq
    re.compile(r"\bsk-[0-9A-Za-z_\-]{20,}"),                      # OpenAI / Anthropic style
    re.compile(r"(?i)\bBearer\s+[0-9A-Za-z._~+/\-]{12,}=*"),
    re.compile(r"(?i)\b((?:api[_-]?key|access[_-]?token|token|authorization|password|secret)\"?\s*[:=]\s*\"?)"
               r"[^\s\"',;&]{8,}"),
    re.compile(r"(?i)([?&](?:key|token|access_token)=)[^&\s\"']{8,}"),
]


def redact(text: str) -> str:
    """Replace anything that looks like a key or token with ``[가림]``."""
    for pattern in _SECRETS:
        text = pattern.sub(lambda m: (m.group(1) if m.lastindex else "") + "[가림]", text)
    return text


class _Redact(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
            cleaned = redact(message)
            if cleaned != message:
                record.msg, record.args = cleaned, None
            if record.exc_info and not record.exc_text:  # format once here so the traceback is cleaned too
                record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
            elif record.exc_text:
                record.exc_text = redact(record.exc_text)
        except Exception:  # noqa: BLE001 - a log line that cannot be cleaned is dropped, never written raw
            return False
        return True


def log_dir() -> Path:
    override = os.environ.get("LECTUREHUB_LOG_DIR")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "LectureHub" / "logs"


def _roll(path: Path) -> None:
    """Start-up only: ``name.log`` → ``name.1.log`` → … when it grew past MAX_BYTES. Failures are ignored (another
    process may hold the file; it is rolled on a later start)."""
    try:
        if not path.is_file() or path.stat().st_size <= MAX_BYTES:
            return
        for n in range(KEEP, 0, -1):
            older = path.with_name(f"{path.stem}.{n}{path.suffix}")
            if n == KEEP:
                older.unlink(missing_ok=True)
            newer = path if n == 1 else path.with_name(f"{path.stem}.{n - 1}{path.suffix}")
            if newer.is_file():
                newer.replace(older)
    except OSError:
        pass


def setup(process: str) -> Path | None:
    """Start logging for this process (``"app"`` or ``"worker"``); returns the log file, or ``None`` when the
    folder cannot be used. Safe to call twice.

    Also the start of a process that may talk to a service, so this is where Python is told to trust what
    Windows trusts (core/net.py). That happens once the log file is attached, so a failure of it is written to
    the log; with an unusable log folder it still happens, there is only nowhere to write a failure."""
    if _state["process"]:
        return log_dir() / f"{_state['process']}.log"
    logging.raiseExceptions = False
    try:
        folder = log_dir()
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{process}.log"
        _roll(path)
        handler = logging.FileHandler(path, encoding="utf-8", delay=True)
        handler.setFormatter(logging.Formatter(
            f"%(asctime)s %(levelname)s {process}[{os.getpid()}] %(name)s: %(message)s"))
        handler.addFilter(_Redact())
        LOG.addHandler(handler)
        LOG.setLevel(logging.INFO)
        LOG.propagate = False
    except OSError:
        _system_certificates()
        return None
    _state["process"] = process
    _hook_uncaught()
    _hook_hard_crash(folder / f"{process}_crash.log")
    LOG.info("시작: %s", describe())
    _system_certificates()  # after the handler: a failure is a line in this log, right under the start line
    return path


def _system_certificates() -> None:
    """Switch HTTPS checking to the Windows certificate store. ``net`` reports its own failure through
    ``swallowed``; the guard here is for anything else, because starting the log must never raise."""
    try:
        from . import net

        net.use_system_certificates()
    except Exception:  # noqa: BLE001
        swallowed("Windows 인증서 저장소 사용")


def describe() -> str:
    """One line about this copy of the app and this PC (no names, no paths)."""
    import platform

    try:
        from lecturehub import __version__
        from lecturehub.core import edition

        app = f"Lecture Hub {__version__} ({edition.name()}{', 설치판' if getattr(sys, 'frozen', False) else ''})"
    except Exception:  # noqa: BLE001
        app = "Lecture Hub"
    return f"{app} · Python {platform.python_version()} · {platform.platform()}"


def _hook_uncaught() -> None:
    previous = sys.excepthook

    def on_error(kind, value, trace):
        if not issubclass(kind, KeyboardInterrupt):
            LOG.critical("처리되지 않은 오류로 멈춤", exc_info=(kind, value, trace))
        previous(kind, value, trace)

    sys.excepthook = on_error
    previous_thread = threading.excepthook

    def on_thread_error(args):
        if args.exc_type is not SystemExit:
            name = args.thread.name if args.thread else "?"
            LOG.error("백그라운드 작업(%s)이 오류로 끝남", name,
                      exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
        previous_thread(args)

    threading.excepthook = on_thread_error


def _hook_hard_crash(path: Path) -> None:
    try:
        import faulthandler

        _roll(path)
        _state["crash_file"] = path.open("a", encoding="utf-8")  # must stay open for the life of the process
        faulthandler.enable(file=_state["crash_file"])
    except (OSError, RuntimeError, ValueError):
        pass


def swallowed(where: str) -> None:
    """Call inside ``except`` where the app goes on on purpose: the failure is kept in the log instead of lost."""
    try:
        LOG.warning("넘어간 오류: %s", where, exc_info=True)
    except Exception:  # noqa: BLE001, S110
        pass
