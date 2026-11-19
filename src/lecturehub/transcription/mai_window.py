"""Bring the Chrome window that shows the MAI playground back on screen without taking the keyboard.

A page in a minimized window is hidden to the browser, and the playground then fails to read an
uploaded file without saying so (L-004, L-005). The tab group Claude opens is sometimes made
minimized, so the upload stopped until the user restored the window by hand (2026-10-11).
``restore`` shows it again behind the other windows: the user's own window stays in front and keeps
the keyboard. Measured on that day: minimized → ``visibilityState`` hidden; after ``restore`` →
visible, foreground window unchanged. Windows only; elsewhere it finds nothing.
"""
from __future__ import annotations

import os

TITLE = "MAI Playground"
_CHROME = "Chrome_WidgetWin_1"
_SW_SHOWNOACTIVATE, _HWND_BOTTOM = 4, 1
_NOSIZE_NOMOVE_NOACTIVATE = 0x0001 | 0x0002 | 0x0010


def restore(title: str = TITLE) -> list[dict]:
    """Every visible Chrome window whose title has ``title``: restored when minimized. Returns what was found."""
    if os.name != "nt":
        return []
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    found: list[tuple[int, str]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            kind = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, kind, 64)
            text = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, text, 512)
            if kind.value == _CHROME and title in text.value:
                found.append((hwnd, text.value))
        return True

    user32.EnumWindows(each, 0)
    front = user32.GetForegroundWindow()
    out = []
    for hwnd, text in found:
        was_minimized = bool(user32.IsIconic(hwnd))
        if was_minimized:
            user32.ShowWindow(hwnd, _SW_SHOWNOACTIVATE)
            user32.SetWindowPos(hwnd, _HWND_BOTTOM, 0, 0, 0, 0, _NOSIZE_NOMOVE_NOACTIVATE)
        out.append({"title": text, "was_minimized": was_minimized, "minimized": bool(user32.IsIconic(hwnd)),
                    "focus_kept": user32.GetForegroundWindow() == front})
    return out
