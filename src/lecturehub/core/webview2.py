"""Is the Microsoft Edge WebView2 Runtime on this PC? (release review 2026-11-16)

The window is drawn by WebView2. Windows 11 always has it and Windows 10 got it through updates, but a PC
without it would start the app and show nothing at all. The launcher asks first and says what to install.
"""
from __future__ import annotations

import os

CLIENT = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"  # the Evergreen runtime's id (Microsoft's documented check)
DOWNLOAD = "https://developer.microsoft.com/microsoft-edge/webview2/"
_KEYS = (("HKLM", rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{CLIENT}"),
         ("HKLM", rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{CLIENT}"),
         ("HKCU", rf"Software\Microsoft\EdgeUpdate\Clients\{CLIENT}"))


def version(read=None) -> str | None:
    """The installed runtime's version, or ``None`` when it is not there. ``read(hive, key)`` is a test seam."""
    if os.name != "nt" and read is None:
        return None
    read = read or _read
    for hive, key in _KEYS:
        value = read(hive, key)
        if value and value != "0.0.0.0":
            return value
    return None


def _read(hive: str, key: str) -> str | None:
    import winreg

    root = winreg.HKEY_LOCAL_MACHINE if hive == "HKLM" else winreg.HKEY_CURRENT_USER
    try:
        with winreg.OpenKey(root, key) as handle:
            value, _kind = winreg.QueryValueEx(handle, "pv")
    except OSError:
        return None
    return str(value) if value else None
