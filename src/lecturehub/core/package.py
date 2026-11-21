"""Is the app running from its Microsoft Store (MSIX) package? (user decision 2026-11-19: publish on the Store,
which signs the package, instead of waiting for a code-signing certificate.)

A packaged app differs from the installer build in three ways that matter here:
- the Store updates it, so the app's own "new version" check is off (``core/update.py``);
- Windows gives it its taskbar identity from the package, so the app must not set its own AppUserModelID;
- files it creates under ``%APPDATA%`` / ``%LOCALAPPDATA%`` are redirected into the package's private folder. The
  app and the programs it starts see them at the usual path, but Explorer does not, so a folder shown to the user
  goes through :func:`real_path` first.
"""
from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path

APPMODEL_ERROR_NO_PACKAGE = 15700
ERROR_INSUFFICIENT_BUFFER = 122


def _ask(function_name: str) -> str | None:
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        function = getattr(ctypes.windll.kernel32, function_name)
        function.argtypes = (ctypes.POINTER(wintypes.UINT), wintypes.LPWSTR)
        function.restype = wintypes.LONG
        length = wintypes.UINT(0)
        if function(ctypes.byref(length), None) != ERROR_INSUFFICIENT_BUFFER:
            return None  # APPMODEL_ERROR_NO_PACKAGE: an ordinary (installer or source) run
        buffer = ctypes.create_unicode_buffer(length.value)
        return buffer.value if function(ctypes.byref(length), buffer) == 0 else None
    except (AttributeError, OSError):  # Windows older than 8 has no such function: never packaged
        return None


@lru_cache(maxsize=1)
def full_name() -> str | None:
    """The package full name, or None when the app does not run from an MSIX package."""
    if os.environ.get("LECTUREHUB_FAKE_PACKAGE"):  # tests only
        return os.environ["LECTUREHUB_FAKE_PACKAGE"]
    return _ask("GetCurrentPackageFullName")


@lru_cache(maxsize=1)
def family_name() -> str | None:
    if os.environ.get("LECTUREHUB_FAKE_PACKAGE_FAMILY"):  # tests only
        return os.environ["LECTUREHUB_FAKE_PACKAGE_FAMILY"]
    return _ask("GetCurrentPackageFamilyName")


def packaged() -> bool:
    return bool(full_name())


def real_path(path: str | Path) -> Path:
    """Where Explorer finds a folder the packaged app made under %APPDATA% or %LOCALAPPDATA%.

    Unchanged when the app is not packaged, the path is elsewhere, or the redirected copy does not exist (a folder
    that already existed before the package was installed is used in place by Windows)."""
    path = Path(path)
    family = family_name() if packaged() else None
    local = os.environ.get("LOCALAPPDATA")
    if not family or not local:
        return path
    cache = Path(local) / "Packages" / family / "LocalCache"
    for env, sub in (("LOCALAPPDATA", "Local"), ("APPDATA", "Roaming")):
        base = os.environ.get(env)
        if not base:
            continue
        try:
            relative = path.resolve().relative_to(Path(base).resolve())
        except ValueError:
            continue
        candidate = cache / sub / relative
        return candidate if candidate.exists() else path
    return path
