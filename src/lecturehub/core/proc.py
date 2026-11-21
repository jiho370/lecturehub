"""Starting other programs from the app (release review 2026-11-16).

Two things differ on a user's PC from the developer's:
- the installed app is a windowed exe, so a console child (ffmpeg, a helper) opens a black window unless it is
  started with ``NO_WINDOW``;
- ``sys.executable`` is ``LectureHub.exe`` (the window), which ignores ``-m module`` arguments. Helpers and
  anything that tells another program how to call the app must use ``console_python()``.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

NO_WINDOW = 0x08000000 if os.name == "nt" else 0       # CREATE_NO_WINDOW
BELOW_NORMAL = 0x00004000 if os.name == "nt" else 0    # BELOW_NORMAL_PRIORITY_CLASS
WORKER_EXE = "LectureHubWorker.exe"


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _package_alias() -> Path | None:
    """In the Store (MSIX) build: the worker's execution alias. A program the user's terminal starts (the note
    helper's agent) runs it inside the package, so it sees the same redirected settings and data as the window;
    the exe in the install folder started by path would run outside the package and see none of them."""
    from . import package

    if not package.packaged():
        return None
    local = os.environ.get("LOCALAPPDATA")
    alias = Path(local) / "Microsoft" / "WindowsApps" / WORKER_EXE if local else None
    return alias if alias is not None and alias.exists() else None


def console_python() -> str:
    """The program that takes ``-m lecturehub …``: the worker exe in the installed build, else python.exe next
    to the running interpreter (the app itself may run under pythonw)."""
    exe = Path(sys.executable)
    if frozen():
        alias = _package_alias()
        return str(alias or exe.with_name(WORKER_EXE))
    candidate = exe.with_name("python.exe")
    return str(candidate if candidate.exists() else exe)
