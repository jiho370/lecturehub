"""Windows console integration: stop safely when the window closes, show status.

Python maps only Ctrl+C/Ctrl+Break to signals. Closing the console window
(CTRL_CLOSE_EVENT) or logging off terminates the process as soon as the
handler returns, so the main loop would never write stop.json. This handler
requests a stop and then holds the handler thread until the recorder reports
that stop.json is written, within the ~5 s grace Windows allows.
"""
from __future__ import annotations

import os
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Self

CTRL_CLOSE_EVENT = 2
CTRL_LOGOFF_EVENT = 5
CTRL_SHUTDOWN_EVENT = 6
# Windows kills the process ~5 s after CTRL_CLOSE_EVENT; keep a margin.
CLOSE_GRACE_SECONDS = 4.5

_TRIGGERS = {
    CTRL_CLOSE_EVENT: "console_close",
    CTRL_LOGOFF_EVENT: "logoff",
    CTRL_SHUTDOWN_EVENT: "system_shutdown",
}


def make_close_handler(request_stop: Callable[[str], None], stopped: threading.Event,
                       grace_seconds: float = CLOSE_GRACE_SECONDS) -> Callable[[int], bool]:
    """Return the handler body; separated from ctypes so it can be tested."""

    def handler(ctrl_type: int) -> bool:
        trigger = _TRIGGERS.get(ctrl_type)
        if trigger is None:
            return False  # Ctrl+C / Ctrl+Break: let Python's signal handlers run.
        request_stop(trigger)
        stopped.wait(grace_seconds)
        return True

    return handler


class CloseGuard:
    """Install/remove the console close handler; no-op outside Windows."""

    def __init__(self, request_stop: Callable[[str], None]):
        self.stopped = threading.Event()
        self._body = make_close_handler(request_stop, self.stopped)
        self._native = None

    def __enter__(self) -> Self:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            # A process can start with Ctrl+C ignored (conhost.exe launches its
            # client that way; so does CREATE_NEW_PROCESS_GROUP). Then neither the
            # keyboard nor GenerateConsoleCtrlEvent reaches SIGINT. Re-enable it.
            ctypes.windll.kernel32.SetConsoleCtrlHandler(None, False)
            prototype = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
            self._native = prototype(lambda ctrl_type: bool(self._body(int(ctrl_type))))
            if not ctypes.windll.kernel32.SetConsoleCtrlHandler(self._native, True):
                self._native = None
        return self

    def mark_stopped(self) -> None:
        self.stopped.set()

    def __exit__(self, *_: object) -> None:
        self.stopped.set()
        if self._native is not None:
            import ctypes

            ctypes.windll.kernel32.SetConsoleCtrlHandler(self._native, False)
            self._native = None


def set_title(text: str) -> None:
    if os.name == "nt":
        import ctypes

        ctypes.windll.kernel32.SetConsoleTitleW(text)


@contextmanager
def quick_edit_disabled() -> Iterator[None]:
    """A stray click in QuickEdit mode freezes console output until Esc.

    Recording continues on other threads, but a frozen meter looks like a hang.
    The previous console mode is restored on exit.
    """
    if os.name != "nt":
        yield
        return
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
    mode = wintypes.DWORD()
    changed = bool(handle) and bool(kernel32.GetConsoleMode(handle, ctypes.byref(mode)))
    if changed:
        enable_quick_edit, enable_extended_flags = 0x0040, 0x0080
        kernel32.SetConsoleMode(handle, (mode.value & ~enable_quick_edit) | enable_extended_flags)
    try:
        yield
    finally:
        if changed:
            kernel32.SetConsoleMode(handle, mode.value)
