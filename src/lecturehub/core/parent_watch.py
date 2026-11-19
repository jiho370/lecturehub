"""Stop a background helper when the app that started it is gone (user decision 2026-11-09, P-053).

The app starts live captions and the Q&A helper as separate processes outside its job object, so they
used to keep running (and calling paid services) after the app closed or crashed. The app passes its
process id; ``watch`` holds a handle to that process from the start (so a reused id cannot fool it) and
sets ``stop`` when the process ends.
"""
from __future__ import annotations

import os
import threading
from collections.abc import Callable

SYNCHRONIZE = 0x00100000
WAIT_OBJECT_0 = 0


def _windows_waiter(pid: int) -> Callable[[float], bool] | None:
    """A function that waits up to ``seconds`` and says whether the process ended; None if it is gone already."""
    import ctypes

    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = ctypes.c_void_p
    handle = kernel32.OpenProcess(SYNCHRONIZE, False, int(pid))
    if not handle:
        return None

    def wait(seconds: float) -> bool:
        return kernel32.WaitForSingleObject(ctypes.c_void_p(handle), int(seconds * 1000)) == WAIT_OBJECT_0

    return wait


def _posix_waiter(pid: int) -> Callable[[float], bool] | None:
    import time

    def alive() -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    if not alive():
        return None

    def wait(seconds: float) -> bool:
        time.sleep(seconds)
        return not alive()

    return wait


def watch(pid: int | None, stop: threading.Event, *, poll_s: float = 5.0, say: Callable[[str], None] | None = None,
          waiter: Callable[[int], Callable[[float], bool] | None] | None = None) -> threading.Thread | None:
    """Set ``stop`` when process ``pid`` ends (checked every ``poll_s``). No pid → nothing to watch."""
    if not pid:
        return None
    wait_for = (waiter or (_windows_waiter if os.name == "nt" else _posix_waiter))(int(pid))

    def gone() -> None:
        if say:
            say("앱이 닫혀 멈춥니다")
        stop.set()

    if wait_for is None:
        gone()
        return None

    def loop() -> None:
        while not stop.is_set():
            if wait_for(poll_s):
                gone()
                return

    thread = threading.Thread(target=loop, name="parent-watch", daemon=True)
    thread.start()
    return thread
