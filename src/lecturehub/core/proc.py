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


def console_python() -> str:
    """The program that takes ``-m lecturehub …``: the worker exe in the installed build, else python.exe next
    to the running interpreter (the app itself may run under pythonw)."""
    exe = Path(sys.executable)
    if frozen():
        return str(exe.with_name(WORKER_EXE))
    candidate = exe.with_name("python.exe")
    return str(candidate if candidate.exists() else exe)
