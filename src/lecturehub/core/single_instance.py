"""One app window per settings file (release review 2026-11-16).

Two windows on the same library both run the background loops, both show a record button and both write the same
settings file. A second start now brings the first window forward and ends.

The lock is a named Windows mutex, so it disappears with the process (nothing to clean up after a crash). Its name
carries the settings path: an app started with its own ``LECTUREHUB_SETTINGS`` (a test install, a verification
run) is a different app and may run next to the user's. ``RUNNING`` is a second, fixed name every app window
holds: the installer reads it to ask the user to close the app before it replaces files.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

RUNNING = "LectureHub.AppRunning"
ERROR_ALREADY_EXISTS = 183
_held: list[int] = []  # handles stay open for the life of the process


def lock_name(settings_path: Path) -> str:
    digest = hashlib.sha256(str(Path(settings_path).resolve()).casefold().encode("utf-8")).hexdigest()[:16]
    return f"LectureHub.App.{digest}"


def acquire(name: str) -> bool:
    """True when this process now holds ``name`` (or the lock cannot be used here); False when another has it."""
    if os.name != "nt":
        return True
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    handle = kernel32.CreateMutexW(None, False, name)
    if not handle:
        return True  # never keep the app from opening because the lock itself failed
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        return False
    _held.append(handle)
    return True


def show_existing(title: str) -> bool:
    """Bring the already-open window with this title to the front; False when it cannot be found."""
    if os.name != "nt":
        return False
    import ctypes

    user32 = ctypes.windll.user32
    user32.FindWindowW.restype = ctypes.c_void_p
    hwnd = user32.FindWindowW(None, title)
    if not hwnd:
        return False
    hwnd = ctypes.c_void_p(hwnd)
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    user32.SetForegroundWindow(hwnd)
    return True
