"""D-084 notification-area owner. A dedicated Win32 message loop, separate from pywebview.

All Shell_NotifyIcon calls live on this thread. Callbacks run on short worker
threads so a WinForms Invoke/dialog can never block the native message pump.
"""
from __future__ import annotations

import queue
import sys
import threading

CALLBACK = 0x8001
WAKE = 0x8002
WM_CLOSE = 0x0010
WM_LBUTTONUP, WM_RBUTTONUP, NIN_BALLOONUSERCLICK = 0x0202, 0x0205, 0x0405


def session_ending() -> bool:
    """The main form may receive shutdown before our tray window gets WM_QUERYENDSESSION."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        return bool(ctypes.windll.user32.GetSystemMetrics(0x2000))  # SM_SHUTTINGDOWN
    except (AttributeError, OSError):
        return False


class Tray:
    def __init__(self, on_open, on_quit, *, on_session=None, icon=None, native_factory=None):
        self._open, self._quit = on_open, on_quit
        self._session = on_session
        self._icon = icon
        self._factory = native_factory or _Native
        self._native = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._messages = queue.SimpleQueue()
        self._thread = None
        self.available = False

    def start(self, timeout=2.0) -> bool:
        self._thread = threading.Thread(target=self._run, name="LectureHub tray", daemon=True)
        self._thread.start()
        self._ready.wait(timeout)
        return self.available

    def _dispatch(self, event):
        if event == "available":
            self.available = True
            return
        if event == "unavailable":
            self.available = False
            event = "open"
        if event in ("session_end", "session_cancel"):
            if self._session:
                self._session(event == "session_end")  # only sets a flag; must finish before Windows closes forms
            return
        action = self._open if event == "open" else self._quit if event == "quit" else None
        if action:
            threading.Thread(target=action, daemon=True).start()

    def _drain(self):
        while not self._messages.empty():
            self._native.balloon(*self._messages.get())

    def _run(self):
        try:
            self._native = self._factory(self._dispatch, self._drain, self._icon)
            self.available = self._native.add()
            self._ready.set()
            if self.available and not self._stop.is_set():
                self._native.run()
                if not self._stop.is_set():
                    self._dispatch("unavailable")
        except Exception:  # tray failure must leave a taskbar window usable
            from lecturehub.core import applog
            applog.swallowed("알림 영역 아이콘")
            if self.available:
                self._dispatch("open")
        finally:
            self.available = False
            self._ready.set()
            if self._native:
                self._native.close()

    def toast(self, title, message) -> bool:
        if not self.available:
            return False
        self._messages.put((title, message))
        self._native.post(WAKE)
        return True

    def stop(self):
        self._stop.set()
        if self._native:
            self._native.post(WM_CLOSE)
        # Do not join from a closing handler: pywebview may be waiting on it.


class _Native:
    """Pointer-sized ctypes signatures are explicit (the shipped app is x64)."""
    def __init__(self, dispatch, drain, icon):
        if sys.platform != "win32":
            raise OSError("Windows only")
        import ctypes as c
        from ctypes import wintypes as w
        from .notify import _structs

        self.c, self.w = c, w
        self.dispatch, self.drain = dispatch, drain
        self.u = c.WinDLL("user32", use_last_error=True)
        self.s = c.WinDLL("shell32", use_last_error=True)
        self.k = c.WinDLL("kernel32", use_last_error=True)
        result = c.c_ssize_t
        proc = c.WINFUNCTYPE(result, w.HWND, w.UINT, w.WPARAM, w.LPARAM)

        class WNDCLASS(c.Structure):
            _fields_ = [("style", w.UINT), ("lpfnWndProc", proc), ("cbClsExtra", c.c_int),
                        ("cbWndExtra", c.c_int), ("hInstance", w.HINSTANCE), ("hIcon", w.HICON),
                        ("hCursor", w.HANDLE), ("hbrBackground", w.HANDLE),
                        ("lpszMenuName", w.LPCWSTR), ("lpszClassName", w.LPCWSTR)]

        def signature(dll, name, ret, *args):
            fn = getattr(dll, name)
            fn.restype, fn.argtypes = ret, args

        signature(self.k, "GetModuleHandleW", w.HMODULE, w.LPCWSTR)
        signature(self.u, "DefWindowProcW", result, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
        signature(self.u, "RegisterClassW", w.ATOM, c.POINTER(WNDCLASS))
        signature(self.u, "UnregisterClassW", w.BOOL, w.LPCWSTR, w.HINSTANCE)
        signature(self.u, "CreateWindowExW", w.HWND, w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD,
                  c.c_int, c.c_int, c.c_int, c.c_int, w.HWND, w.HMENU, w.HINSTANCE, c.c_void_p)
        signature(self.u, "DestroyWindow", w.BOOL, w.HWND)
        signature(self.u, "PostMessageW", w.BOOL, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
        signature(self.u, "RegisterWindowMessageW", w.UINT, w.LPCWSTR)
        signature(self.u, "LoadImageW", w.HANDLE, w.HINSTANCE, w.LPCWSTR, w.UINT, c.c_int, c.c_int, w.UINT)
        signature(self.u, "LoadIconW", w.HICON, w.HINSTANCE, c.c_void_p)
        signature(self.u, "DestroyIcon", w.BOOL, w.HICON)
        signature(self.u, "CreatePopupMenu", w.HMENU)
        signature(self.u, "AppendMenuW", w.BOOL, w.HMENU, w.UINT, c.c_size_t, w.LPCWSTR)
        signature(self.u, "TrackPopupMenu", w.UINT, w.HMENU, w.UINT, c.c_int, c.c_int, c.c_int, w.HWND, c.c_void_p)
        signature(self.u, "DestroyMenu", w.BOOL, w.HMENU)
        signature(self.u, "SetForegroundWindow", w.BOOL, w.HWND)
        signature(self.u, "GetCursorPos", w.BOOL, c.POINTER(w.POINT))
        signature(self.u, "GetMessageW", w.BOOL, c.POINTER(w.MSG), w.HWND, w.UINT, w.UINT)
        signature(self.u, "TranslateMessage", w.BOOL, c.POINTER(w.MSG))
        signature(self.u, "DispatchMessageW", result, c.POINTER(w.MSG))
        signature(self.u, "PostQuitMessage", None, c.c_int)
        data_type, _ = _structs()
        signature(self.s, "Shell_NotifyIconW", w.BOOL, w.DWORD, c.POINTER(data_type))
        self.recreated = self.u.RegisterWindowMessageW("TaskbarCreated")
        self.instance = self.k.GetModuleHandleW(None)
        self.name = f"LectureHub.Tray.{id(self)}"
        self.proc = proc(self._wndproc)  # hold the callback until after UnregisterClass
        self.wc = WNDCLASS(lpfnWndProc=self.proc, hInstance=self.instance, lpszClassName=self.name)
        self.hwnd, self.file_icon = None, None
        if not self.u.RegisterClassW(c.byref(self.wc)):
            raise c.WinError(c.get_last_error())
        self.hwnd = self.u.CreateWindowExW(0x80, self.name, "Lecture Hub notifications", 0,
                                          0, 0, 0, 0, None, None, self.instance, None)
        if not self.hwnd:
            self.close()
            raise c.WinError(c.get_last_error())
        self.file_icon = self.u.LoadImageW(None, icon, 1, 0, 0, 0x10) if icon else None
        handle = self.file_icon or self.u.LoadIconW(None, c.c_void_p(32512))
        self.data = data_type()
        self.data.cbSize = c.sizeof(data_type)
        self.data.hWnd, self.data.uID = self.hwnd, 1
        self.data.uFlags, self.data.uCallbackMessage = 0x1 | 0x2 | 0x4, CALLBACK
        self.data.hIcon, self.data.szTip = handle, "Lecture Hub · 열기 / 끝내기"

    def add(self):
        return bool(self.s.Shell_NotifyIconW(0, self.c.byref(self.data)))

    def balloon(self, title, message):
        self.data.uFlags = 0x1 | 0x2 | 0x4 | 0x10
        self.data.szInfoTitle, self.data.szInfo = title[:63], message[:255]
        self.data.dwInfoFlags = 1
        try:
            self.s.Shell_NotifyIconW(1, self.c.byref(self.data))
        finally:
            self.data.uFlags = 0x1 | 0x2 | 0x4
            self.data.szInfoTitle, self.data.szInfo = "", ""

    def post(self, message):
        if self.hwnd:
            self.u.PostMessageW(self.hwnd, message, 0, 0)

    def _wndproc(self, hwnd, message, wp, lp):
        try:
            if message == CALLBACK:
                if lp in (WM_LBUTTONUP, NIN_BALLOONUSERCLICK):
                    self.dispatch("open")
                elif lp == WM_RBUTTONUP:
                    self._menu(hwnd)
                return 0
            if message == self.recreated:
                self.dispatch("available" if self.add() else "unavailable")
                return 0
            if message == WAKE:
                self.drain()
                return 0
            if message == WM_CLOSE:
                self.u.PostQuitMessage(0)
                return 0
            if message == 0x0011:  # WM_QUERYENDSESSION: never veto logoff, shutdown or a Store update
                self.dispatch("session_end")
                return 1
            if message == 0x0016 and not wp:  # WM_ENDSESSION, another app cancelled shutdown
                self.dispatch("session_cancel")
                return 0
        except Exception:
            from lecturehub.core import applog
            applog.swallowed("알림 영역 메시지")
        return self.u.DefWindowProcW(hwnd, message, wp, lp)

    def _menu(self, hwnd):
        menu = self.u.CreatePopupMenu()
        try:
            self.u.AppendMenuW(menu, 0, 1, "열기")
            self.u.AppendMenuW(menu, 0, 2, "끝내기")
            point = self.w.POINT()
            self.u.GetCursorPos(self.c.byref(point))
            self.u.SetForegroundWindow(hwnd)
            choice = self.u.TrackPopupMenu(menu, 0x100 | 0x2, point.x, point.y, 0, hwnd, None)
            self.post(0)  # WM_NULL: dismiss correctly after clicking outside the menu
            if choice in (1, 2):
                self.dispatch("open" if choice == 1 else "quit")
        finally:
            self.u.DestroyMenu(menu)

    def run(self):
        msg = self.w.MSG()
        while self.u.GetMessageW(self.c.byref(msg), None, 0, 0) > 0:
            self.u.TranslateMessage(self.c.byref(msg))
            self.u.DispatchMessageW(self.c.byref(msg))

    def close(self):
        if self.hwnd:
            if hasattr(self, "data"):
                self.s.Shell_NotifyIconW(2, self.c.byref(self.data))
            self.u.DestroyWindow(self.hwnd)
            self.hwnd = None
        if self.file_icon:
            self.u.DestroyIcon(self.file_icon)
        self.u.UnregisterClassW(self.name, self.instance)
