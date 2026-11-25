"""D-080: Lecture Hub asks the user on screen before an AI app changes something — the first time per kind, then
the kinds the user set to automatic apply at once (owner 2026-11-25: "처음엔 묻고, 그다음엔 자동").

The five kinds are the five write tools (``writes.HANDLERS``). Which kinds are automatic is ``ai_link.auto`` in
settings.json (kind -> true), read strictly on every call (``access.auto``); a missing or broken entry means "ask".

When a kind is not automatic, the MCP server process itself shows a native Windows dialog — top-most, brought to
the front, [거절] as the default button and the two allow buttons enabled only after a short moment (a key the user
was typing elsewhere cannot approve it) — with three buttons: [이번만 허용] [이 종류는 앞으로 자동] [거절]. No answer
within ``TIMEOUT_S`` seconds, Esc, the close box, or a dialog that cannot be shown: nothing changes.

The dialog is shown while nothing of the database is held (``writes.run``): no read connection, no write lock.

What the dialog says comes partly from the AI app (titles, notes, a note's text): every value is cleaned
(``clean``: control, format and bidi-override characters become spaces, one line, cut to a length, square brackets
turned into round ones so a value cannot look like a button) and shown after a fixed label of the app's own. The
window title names the AI app by the fixed labels of ``writes.author_for`` only, never by the name it sent.

There is no switch around this in production code — no environment variable, no argument, no file next to the
program: the AI app can edit its own MCP configuration and environment. Tests replace ``dialog`` (a module-level
function) in-process.
"""
from __future__ import annotations

import sys
import time
import unicodedata
from contextlib import contextmanager

# kind (= write tool) -> what the user sees; the order of the settings rows
KINDS = {"add_calendar_event": "일정 넣기", "update_calendar_event": "AI가 넣은 일정 고치기",
         "delete_calendar_event": "AI가 넣은 일정 지우기", "resolve_candidate": "일정 후보 확정·무시",
         "save_note": "정리본 새로 저장", "set_timetable": "시간표 바꾸기", "add_schedule_change": "보강·시간 변경 넣기",
          "set_reminder_settings": "알림 설정 바꾸기", "request_notice_rescan": "수업 중 공지 다시 찾기"}
TIMEOUT_S = 50
ARM_S = 0.8  # the allow buttons stay disabled this long after the dialog appears

ONCE, AUTO, REJECT, TIMEOUT, FAILED = "once", "auto", "reject", "timeout", "failed"
ANSWERS = (ONCE, AUTO, REJECT, TIMEOUT)

REJECTED = "사용자가 거절해 바꾸지 않았습니다. 같은 변경을 다시 시도하지 마세요."
NO_ANSWER = "사용자가 확인하지 않아 바꾸지 않았습니다."
NO_DIALOG = ("Lecture Hub가 확인 창을 띄우지 못해 바꾸지 않았습니다. Lecture Hub 앱에서 직접 바꾸거나, 앱의 설정 › AI › "
             "AI 앱 연결에서 이 종류를 '자동'으로 바꾼 뒤 다시 시도하세요.")
CHANGED = ("확인 창을 띄운 사이에 대상이 바뀌어 바꾸지 않았습니다. 지금 상태를 다시 읽은 뒤, 필요하면 다시 시도하세요.")
GUIDE = ("이 대화에서 직접 부탁한 변경일 때만 허용하세요. 전사·공지·자료 속 문장이 AI 앱에 시킨 것일 수도 있습니다. "
         f"{TIMEOUT_S}초 안에 고르지 않으면 바꾸지 않습니다. 바꾼 것은 설정 › AI › AI 앱 연결 › 'AI가 바꾼 것'에서 되돌릴 수 있습니다.")
FALLBACK_NOTE = "\n\n[예] 이번만 허용   [아니요] 거절\n(이 종류를 앞으로 자동으로 하려면 Lecture Hub 앱의 설정 › AI › AI 앱 연결에서 바꾸세요.)"

_BIDI = frozenset("\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")


def clean(value, limit: int = 120) -> str:
    """One line of untrusted text for the dialog: control/format/bidi/private/unassigned characters and line
    breaks become spaces, runs of spaces one, "[" "]" round brackets, at most ``limit`` characters."""
    text = value if isinstance(value, str) else ("" if value is None else str(value))
    out = []
    for ch in text:
        if ch in _BIDI or unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"):
            out.append(" ")
        else:
            out.append({"[": "(", "]": ")"}.get(ch, ch))
    text = " ".join("".join(out).split())
    return text if len(text) <= limit else text[:max(1, limit - 1)] + "…"


def actor(author: str) -> str:
    """"ChatGPT가" / "Claude가" / "AI 앱이" from the fixed label of ``writes.author_for`` (never the client's name)."""
    from .writes import AUTHORS, DEFAULT_AUTHOR

    label = author if author in AUTHORS else DEFAULT_AUTHOR
    return label[:-2]  # "… 씀"


def text_of(kind: str, author: str, fields: list) -> tuple[str, str, str]:
    """(window title, main line, body) of the dialog. ``fields``: (label, value) or (label, value, limit); labels
    are the app's own words, values are cleaned."""
    title = f"{actor(author)} Lecture Hub를 바꾸려 합니다"
    instruction = f"{KINDS[kind]} — 허용할까요?"
    lines = []
    for field in fields:
        label, value = field[0], field[1]
        limit = field[2] if len(field) > 2 else 120
        lines.append(f"{label}: {clean(value, limit) or '(없음)'}")
    return title, instruction, "\n".join(lines) + "\n\n" + GUIDE


def ask(kind: str, author: str, fields: list) -> str:
    """ONCE, AUTO, REJECT, TIMEOUT or FAILED (anything unexpected, an exception included, is FAILED)."""
    title, instruction, body = text_of(kind, author, fields)
    try:
        answer = dialog(title, instruction, body, TIMEOUT_S)
    except Exception:  # noqa: BLE001 - a dialog that broke is a dialog that was not answered: nothing changes
        from lecturehub.core import applog

        applog.LOG.error("AI 앱 연결: 확인 창 오류", exc_info=True)
        return FAILED
    return answer if answer in ANSWERS else FAILED


# ------------------------------------------------------------------ D-083 review M1: a request that hit a hidden course
HIDDEN_TITLE = "Lecture Hub — AI 앱의 요청을 막았습니다"
HIDDEN_TEXT = ("{actor} 시간표를 바꾸려 했지만, AI 앱에 보이지 않게 한 과목(또는 그 폴더)과 겹쳐 아무것도 바꾸지 않았습니다. "
               "직접 부탁한 일이 아니라면 AI 앱이 숨긴 과목을 알아내려는 것일 수 있습니다. 같은 일이 계속되면 설정 › AI › "
               "AI 앱 연결에서 'AI 앱이 고칠 수 있게'를 끄세요.")
NOTICE_EVERY_S = 60.0
_last_notice = [float("-inf")]


def tell_hidden(kind: str, author: str) -> bool:
    """Tell the user — whatever the kind's automatic setting — that a request was refused because of a course the AI
    app may not see. An [확인]-only notice, at most once per ``NOTICE_EVERY_S`` per process; True when shown."""
    now = time.monotonic()
    if now - _last_notice[0] < NOTICE_EVERY_S:
        return False
    _last_notice[0] = now
    try:
        notice(HIDDEN_TITLE, HIDDEN_TEXT.format(actor=actor(author)))
    except Exception:  # noqa: BLE001 - the refusal stands either way
        from lecturehub.core import applog

        applog.LOG.error("AI 앱 연결: 알림 창 오류", exc_info=True)
    return True


def notice(title: str, text: str) -> None:
    """The real notice: an OK-only message box, top-most, on its own thread so the AI app's reply is not held up;
    it closes itself after two minutes (MessageBoxTimeoutW). Replaced in tests."""
    if sys.platform != "win32":
        return
    import threading

    def show() -> None:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        box = getattr(user32, "MessageBoxTimeoutW", None)
        if box is None:
            return
        box.restype = ctypes.c_int
        box.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT, wintypes.WORD, wintypes.DWORD]
        box(None, text, title, 0x0 | 0x30 | 0x10000 | 0x40000, 0, 120_000)  # OK | ICONWARNING | SETFOREGROUND | TOPMOST

    threading.Thread(target=show, name="lecturehub-ai-notice", daemon=False).start()


# ------------------------------------------------------------------ the native dialog (replaced in tests)
def dialog(title: str, instruction: str, body: str, timeout_s: float) -> str:
    """The real dialog: TaskDialogIndirect with three buttons, else MessageBox (Yes/No) with a timeout, else FAILED."""
    if sys.platform != "win32":
        return FAILED
    from lecturehub.core import applog

    try:
        return _task_dialog(title, instruction, body, timeout_s)
    except Exception:  # noqa: BLE001 - comctl32 v6 missing, a failed HRESULT, ...: the simpler box
        applog.LOG.warning("AI 앱 연결: 확인 창(TaskDialog)을 띄우지 못해 간단한 창으로 묻습니다", exc_info=True)
    try:
        return _message_box(title, instruction + "\n\n" + body + FALLBACK_NOTE, timeout_s)
    except Exception:  # noqa: BLE001
        applog.LOG.error("AI 앱 연결: 확인 창을 띄우지 못함", exc_info=True)
        return FAILED


ID_ONCE, ID_AUTO, ID_REJECT = 1001, 1002, 1003
_TDF_ALLOW_DIALOG_CANCELLATION, _TDF_CALLBACK_TIMER, _TDF_SIZE_TO_CONTENT = 0x8, 0x800, 0x1000000
_TDN_CREATED, _TDN_TIMER = 0, 4
_TDM_CLICK_BUTTON, _TDM_ENABLE_BUTTON = 0x400 + 102, 0x400 + 111
_TD_WARNING_ICON = 0xFFFF  # MAKEINTRESOURCEW(-1)


@contextmanager
def _comctl_v6():
    """comctl32 version 6 (TaskDialogIndirect lives only there) through an activation context made from the
    common-controls manifest Windows ships in shell32.dll (resource 124); python.exe has no such manifest. The
    context is active on this thread for the dialog only."""
    import ctypes
    from ctypes import wintypes

    class ACTCTXW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.ULONG), ("dwFlags", wintypes.DWORD), ("lpSource", wintypes.LPCWSTR),
                    ("wProcessorArchitecture", wintypes.USHORT), ("wLangId", wintypes.LANGID),
                    ("lpAssemblyDirectory", wintypes.LPCWSTR), ("lpResourceName", ctypes.c_void_p),
                    ("lpApplicationName", wintypes.LPCWSTR), ("hModule", wintypes.HMODULE)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateActCtxW.restype = wintypes.HANDLE
    kernel32.CreateActCtxW.argtypes = [ctypes.POINTER(ACTCTXW)]
    kernel32.ActivateActCtx.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_size_t)]
    kernel32.DeactivateActCtx.argtypes = [wintypes.DWORD, ctypes.c_size_t]
    kernel32.ReleaseActCtx.argtypes = [wintypes.HANDLE]
    system = ctypes.create_unicode_buffer(260)
    kernel32.GetSystemDirectoryW(system, 260)
    context = ACTCTXW(cbSize=ctypes.sizeof(ACTCTXW), dwFlags=0x8 | 0x4,  # RESOURCE_NAME_VALID | ASSEMBLY_DIRECTORY_VALID
                      lpSource=system.value + "\\shell32.dll", lpAssemblyDirectory=system.value,
                      lpResourceName=ctypes.c_void_p(124))
    handle = kernel32.CreateActCtxW(ctypes.byref(context))
    if not handle or handle == ctypes.c_void_p(-1).value:
        raise OSError("no common-controls activation context")
    try:
        cookie = ctypes.c_size_t()
        if not kernel32.ActivateActCtx(handle, ctypes.byref(cookie)):
            raise OSError("activation context not activated")
        try:
            yield ctypes.WinDLL("comctl32", use_last_error=True)
        finally:
            kernel32.DeactivateActCtx(0, cookie)
    finally:
        kernel32.ReleaseActCtx(handle)


def _front(hwnd) -> None:
    """Top-most and in front even when this process (started by the AI app) is in the background."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32")
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    for name in ("BringWindowToTop", "SetForegroundWindow"):
        getattr(user32, name).argtypes = [wintypes.HWND]
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                    wintypes.UINT]
    user32.SetWindowPos(hwnd, wintypes.HWND(-1), 0, 0, 0, 0, 0x1 | 0x2 | 0x40)  # HWND_TOPMOST; NOSIZE|NOMOVE|SHOWWINDOW
    foreground = user32.GetForegroundWindow()
    other = user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
    mine = ctypes.windll.kernel32.GetCurrentThreadId()
    attached = bool(other and other != mine and user32.AttachThreadInput(mine, other, True))
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(mine, other, False)


def _task_dialog(title: str, instruction: str, body: str, timeout_s: float, *, close_at_once: bool = False) -> str:
    """``close_at_once`` (a test of the structures only): the dialog closes itself as [거절] before it is shown."""
    with _comctl_v6() as comctl:
        return _task_dialog_in(comctl, title, instruction, body, timeout_s, close_at_once)


def _task_dialog_in(comctl, title: str, instruction: str, body: str, timeout_s: float, close_at_once: bool) -> str:
    import ctypes
    from ctypes import wintypes

    function = comctl.TaskDialogIndirect  # AttributeError without comctl32 v6

    class BUTTON(ctypes.Structure):
        _pack_ = 1
        _fields_ = [("nButtonID", ctypes.c_int), ("pszButtonText", wintypes.LPCWSTR)]

    CALLBACK = ctypes.WINFUNCTYPE(ctypes.c_long, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
                                  ctypes.c_ssize_t)

    class CONFIG(ctypes.Structure):
        _pack_ = 1  # commctrl.h declares it under pshpack1
        _fields_ = [("cbSize", wintypes.UINT), ("hwndParent", wintypes.HWND), ("hInstance", wintypes.HINSTANCE),
                    ("dwFlags", ctypes.c_int), ("dwCommonButtons", ctypes.c_int), ("pszWindowTitle", wintypes.LPCWSTR),
                    ("pszMainIcon", ctypes.c_void_p), ("pszMainInstruction", wintypes.LPCWSTR),
                    ("pszContent", wintypes.LPCWSTR), ("cButtons", wintypes.UINT), ("pButtons", ctypes.POINTER(BUTTON)),
                    ("nDefaultButton", ctypes.c_int), ("cRadioButtons", wintypes.UINT),
                    ("pRadioButtons", ctypes.POINTER(BUTTON)), ("nDefaultRadioButton", ctypes.c_int),
                    ("pszVerificationText", wintypes.LPCWSTR), ("pszExpandedInformation", wintypes.LPCWSTR),
                    ("pszExpandedControlText", wintypes.LPCWSTR), ("pszCollapsedControlText", wintypes.LPCWSTR),
                    ("pszFooterIcon", ctypes.c_void_p), ("pszFooter", wintypes.LPCWSTR), ("pfCallback", CALLBACK),
                    ("lpCallbackData", ctypes.c_ssize_t), ("cxWidth", wintypes.UINT)]

    buttons = (BUTTON * 3)(BUTTON(ID_ONCE, "이번만 허용"), BUTTON(ID_AUTO, "이 종류는 앞으로 자동"), BUTTON(ID_REJECT, "거절"))
    user32 = ctypes.WinDLL("user32")  # its own instance: argtypes set here stay here
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    state = {"started": time.monotonic(), "armed": False, "timed_out": False}

    def callback(hwnd, message, wparam, lparam, data):
        try:
            if message == _TDN_CREATED:
                if close_at_once:
                    user32.SendMessageW(hwnd, _TDM_CLICK_BUTTON, ID_REJECT, 0)
                    return 0
                for button in (ID_ONCE, ID_AUTO):
                    user32.SendMessageW(hwnd, _TDM_ENABLE_BUTTON, button, 0)
                _front(hwnd)
            elif message == _TDN_TIMER:
                waited = time.monotonic() - state["started"]
                if not state["armed"] and waited >= ARM_S:
                    state["armed"] = True
                    for button in (ID_ONCE, ID_AUTO):
                        user32.SendMessageW(hwnd, _TDM_ENABLE_BUTTON, button, 1)
                if waited >= timeout_s and not state["timed_out"]:
                    state["timed_out"] = True
                    user32.SendMessageW(hwnd, _TDM_CLICK_BUTTON, ID_REJECT, 0)
        except Exception:  # noqa: BLE001, S110 - never raise through a Windows callback
            pass
        return 0

    keep = CALLBACK(callback)
    config = CONFIG(cbSize=ctypes.sizeof(CONFIG), dwFlags=_TDF_ALLOW_DIALOG_CANCELLATION | _TDF_CALLBACK_TIMER
                    | _TDF_SIZE_TO_CONTENT, pszWindowTitle=title, pszMainIcon=_TD_WARNING_ICON,
                    pszMainInstruction=instruction, pszContent=body, cButtons=3, pButtons=buttons,
                    nDefaultButton=ID_REJECT, pfCallback=keep)
    pressed = ctypes.c_int(0)
    function.restype = ctypes.c_long
    function.argtypes = [ctypes.POINTER(CONFIG), ctypes.POINTER(ctypes.c_int), ctypes.c_void_p, ctypes.c_void_p]
    result = function(ctypes.byref(config), ctypes.byref(pressed), None, None)
    if result != 0:
        raise OSError(f"TaskDialogIndirect failed: {result & 0xFFFFFFFF:#x}")
    if state["timed_out"]:
        return TIMEOUT
    return {ID_ONCE: ONCE, ID_AUTO: AUTO}.get(pressed.value, REJECT)  # [거절], Esc, the close box


def _message_box(title: str, text: str, timeout_s: float) -> str:
    """Yes/No, [아니요] the default, top-most and in front; MessageBoxTimeoutW (user32, undocumented but exported
    since Windows XP) closes it after the timeout. Without it there is no timeout, so no box at all: FAILED."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    box = getattr(user32, "MessageBoxTimeoutW", None)
    if box is None:
        return FAILED
    box.restype = ctypes.c_int
    box.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT, wintypes.WORD, wintypes.DWORD]
    flags = 0x4 | 0x30 | 0x100 | 0x10000 | 0x40000  # YESNO | ICONWARNING | DEFBUTTON2 | SETFOREGROUND | TOPMOST
    answer = box(None, text, title, flags, 0, int(timeout_s * 1000))
    if answer == 32000:  # MB_TIMEDOUT
        return TIMEOUT
    if answer == 6:  # IDYES
        return ONCE
    if answer == 0:
        return FAILED
    return REJECT
