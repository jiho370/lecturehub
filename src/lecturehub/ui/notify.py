"""Windows notification (balloon → toast on Windows 10/11) and taskbar flash, with ctypes only.

Used for schedule notices heard in class (user request 2026-10-30): the page shows a banner as well, so a
failure here only means the user sees it when they look at the window. No PowerShell, no new dependency.
"""
from __future__ import annotations

import sys
import threading

_ICON_ID = 0x4C48  # one icon per window; a newer notice replaces the older balloon


def _structs():
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                    ("Data4", ctypes.c_ubyte * 8)]

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
                    ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
                    ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
                    ("szInfo", wintypes.WCHAR * 256), ("uVersion", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64),
                    ("dwInfoFlags", wintypes.DWORD), ("guidItem", GUID), ("hBalloonIcon", wintypes.HICON)]

    class FLASHWINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("hwnd", wintypes.HWND), ("dwFlags", wintypes.DWORD),
                    ("uCount", wintypes.UINT), ("dwTimeout", wintypes.DWORD)]

    return NOTIFYICONDATAW, FLASHWINFO


def toast(title: str, message: str, *, window_title: str = "Lecture Hub", icon: str | None = None,
          keep_seconds: float = 30.0) -> bool:
    """Show a notification owned by the app window. True when Windows accepted it."""
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    try:
        NOTIFYICONDATAW, FLASHWINFO = _structs()
        user32, shell32 = ctypes.windll.user32, ctypes.windll.shell32
        user32.FindWindowW.restype = wintypes.HWND
        user32.LoadImageW.restype = wintypes.HANDLE
        user32.LoadIconW.restype = wintypes.HICON
        user32.LoadIconW.argtypes = (wintypes.HINSTANCE, ctypes.c_void_p)
        shell32.Shell_NotifyIconW.argtypes = (wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW))
        hwnd = user32.FindWindowW(None, window_title)
        if not hwnd:
            return False
        handle = user32.LoadImageW(None, icon, 1, 0, 0, 0x10 | 0x40) if icon else None  # IMAGE_ICON, from file
        handle = handle or user32.LoadIconW(None, ctypes.c_void_p(32516))  # IDI_INFORMATION
        data = NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        data.hWnd, data.uID = hwnd, _ICON_ID
        data.uFlags = 0x2 | 0x4 | 0x10  # NIF_ICON | NIF_TIP | NIF_INFO
        data.hIcon = handle
        data.szTip = "Lecture Hub"
        data.szInfoTitle = title[:63]
        data.szInfo = message[:255]
        data.dwInfoFlags = 0x1  # NIIF_INFO
        ok = bool(shell32.Shell_NotifyIconW(0, ctypes.byref(data)) or shell32.Shell_NotifyIconW(1, ctypes.byref(data)))
        flash = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd, 0x3 | 0xC, 0, 0)  # FLASHW_ALL | FLASHW_TIMERNOFG
        user32.FlashWindowEx(ctypes.byref(flash))

        def remove():
            try:
                shell32.Shell_NotifyIconW(2, ctypes.byref(data))  # NIM_DELETE; the toast stays in the action centre
            except Exception:  # noqa: BLE001, S110 - the icon goes away with the window anyway
                pass

        timer = threading.Timer(keep_seconds, remove)
        timer.daemon = True
        timer.start()
        return ok
    except Exception:  # noqa: BLE001 - a notification must never break the app
        return False
