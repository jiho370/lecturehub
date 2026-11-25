"""D-084 window lifecycle. Fakes only: no windows, icons, balloons or native dialogs."""
from types import SimpleNamespace
import ctypes
import sys
import threading

import pytest

from lecturehub.core import settings, single_instance, startup
from lecturehub.ui import app, launch, notify, tray


@pytest.fixture
def api(monkeypatch):
    calls = []
    backend = SimpleNamespace(recording=False, stop_live_helpers=lambda: calls.append("helpers.stop"))
    api = app.Api(backend)
    window = SimpleNamespace(show=lambda: calls.append("show"), hide=lambda: calls.append("hide"),
                             minimize=lambda: calls.append("minimize"), restore=lambda: calls.append("restore"),
                             destroy=lambda: calls.append("destroy"))
    api._main = window
    monkeypatch.setattr(api, "_main_hwnd", lambda *args: 0)
    monkeypatch.setattr(startup, "status", lambda: {"ok": True, "store": False, "possible": True, "enabled": False})
    monkeypatch.setattr(notify, "toast", lambda *args, **kwargs: pytest.fail("unexpected toast"))
    monkeypatch.setattr(tray, "session_ending", lambda: False)
    made_pending = []
    api._pending_close = made_pending
    monkeypatch.setattr(api, "_schedule_close_choice", lambda: made_pending.append(api._ask_close_choice))
    return api, calls


@pytest.mark.parametrize("answer", [True, False])
def test_STAY_002_first_close_asks_once_and_remembers_both_answers(api, answer):
    made, calls = api
    asked = []
    made._tray = SimpleNamespace(available=True)
    made._main.create_confirmation_dialog = lambda *args: asked.append(args) or answer
    assert made._on_main_closing() is False
    # No dialog or WinForms method may run until closing has returned to the window loop.
    assert asked == [] and calls == [] and not made._notice_stop.is_set()
    made._pending_close.pop()()
    assert startup.close_choice() is answer
    assert len(asked) == 1 and "아니요" in asked[0][1] and "알림" in asked[0][1]
    assert not made._notice_stop.is_set()  # the real closing event follows destroy
    if answer:
        assert calls == ["hide"]
        assert not made._quitting
        assert made._on_main_closing() is False
        assert calls == ["hide", "hide"] and len(asked) == 1
    else:
        assert calls == ["destroy"] and made._quitting
        assert made._on_main_closing() is True
        assert calls == ["destroy", "helpers.stop"] and made._notice_stop.is_set()


def test_STAY_003_saved_choice_settings_switch_and_disabled_reminders(api):
    made, calls = api
    settings.save({"reminders": {"deadlines": False, "briefing": False}})
    assert made._on_main_closing() is True  # no prompt with both D-079 switches off
    assert startup.close_choice() is None
    made._quitting = False
    made._notice_stop.clear()
    assert made.stay_set(True)["keep_running"] is True
    assert made._on_main_closing() is False  # explicit keep choice still applies
    assert calls[-1:] == ["minimize"]  # tray unavailable: asynchronous taskbar fallback
    assert made.stay_set(False)["keep_running"] is False
    assert made._on_main_closing() is True


@pytest.mark.parametrize("failure", ["dialog", "save"])
def test_STAY_004_prompt_or_save_failure_and_reentrant_close_keep_app_alive(api, monkeypatch, failure):
    made, calls = api
    def fail(*args):
        raise OSError("synthetic failure")
    made._main.create_confirmation_dialog = fail if failure == "dialog" else lambda *args: True
    if failure == "save":
        monkeypatch.setattr(startup, "set_close_choice", fail)
    assert made._on_main_closing() is False
    made._pending_close.pop()()
    assert calls == [] and not made._notice_stop.is_set() and not made._quitting
    assert made._close_prompt.acquire(blocking=False)
    try:
        assert made._on_main_closing() is False
    finally:
        made._close_prompt.release()


def test_STAY_005_recording_close_and_tray_quit_never_prompt_hide_or_stop(api, monkeypatch):
    made, calls = api
    made._backend.recording = True
    monkeypatch.setattr(startup, "close_choice", lambda: pytest.fail("read during recording"))
    monkeypatch.setattr(made, "_keep_open_while_recording", lambda: calls.append("original-recording-path"))
    assert made._on_main_closing() is False
    assert not made.quit_app()["ok"]
    assert calls == ["original-recording-path", "show", "original-recording-path"]
    assert not made._notice_stop.is_set() and not made._quitting


def test_STAY_006_explicit_quit_bypasses_keep_choice_and_cleans_up(api):
    made, calls = api
    startup.set_close_choice(True)
    made._tray = SimpleNamespace(available=True, stop=lambda: calls.append("tray.stop"))
    def destroy():
        assert made._on_main_closing() is True
        made._on_main_closed()
        calls.append("destroy")
    made._main.destroy = destroy
    assert made.quit_app()["ok"]
    assert calls == ["helpers.stop", "tray.stop", "destroy"]
    assert made._notice_stop.is_set() and notify._tray is None


def test_STAY_007_windows_session_end_is_not_held_open_and_cancel_resets_it(api):
    made, calls = api
    startup.set_close_choice(True)
    made._session_end(True)
    assert made._on_main_closing() is True and calls == ["helpers.stop"]
    made._quitting = False
    made._session_end(False)
    assert made._on_main_closing() is False and calls[-1] == "minimize"


@pytest.mark.parametrize("available", [False, True])
def test_STAY_008_background_starts_hidden_with_tray_or_taskbar_fallback(api, monkeypatch, available):
    made, calls = api
    class FakeTray:
        def __init__(self, on_open, on_quit, **kwargs):
            self.available = available
            assert on_open == made.show_main and on_quit == made.quit_app
            assert kwargs["on_session"] == made._session_end
        def start(self):
            return self.available
    monkeypatch.setattr(tray, "Tray", FakeTray)
    monkeypatch.setattr(notify, "_tray", None)
    made._desktop_ready(background=True)
    assert calls == (["hide"] if available else ["minimize"])
    assert notify._tray is made._tray
    assert not made._notice_stop.is_set()


@pytest.mark.parametrize("background", [False, True])
def test_STARTUP_005_launcher_propagates_background_and_store_startup_copy(monkeypatch, background):
    from lecturehub.core import webview2
    monkeypatch.setattr(webview2, "version", lambda: "synthetic")
    calls = []
    monkeypatch.setattr(app, "main", lambda **kw: calls.append(kw) or 0)
    assert launch.run(["--background"] if background else []) == 0
    assert calls == [{"background": background}]
    monkeypatch.setattr(sys, "executable", r"C:\合成 (test)\LectureHubStartup.exe")
    assert launch.run([]) == 0 and calls[-1] == {"background": True}


@pytest.mark.parametrize("background", [False, True])
def test_STARTUP_006_main_creates_hidden_window_only_for_background(tmp_path, monkeypatch, background):
    from lecturehub.ui import backend, media_server
    calls = []
    class Event:
        def __iadd__(self, fn):
            return self
    events = SimpleNamespace(loaded=Event(), closing=Event(), closed=Event())
    def create(*args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(events=events)
    def start(fn, args, **kwargs):
        assert fn.__name__ == "_desktop_ready" and args == (background,)
    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(create_window=create, start=start, settings={}))
    monkeypatch.setattr(app, "_set_app_id", lambda: None)
    monkeypatch.setattr(app, "_already_open", lambda: False)
    monkeypatch.setattr(app, "guard_navigation", lambda window: None)
    monkeypatch.setattr(app, "open_room", lambda backend: None)
    monkeypatch.setattr(app, "open_library", lambda root: SimpleNamespace())
    monkeypatch.setattr(backend, "Backend", lambda **kwargs: SimpleNamespace(root=tmp_path, resume_jobs=lambda: None))
    monkeypatch.setattr(media_server, "MediaServer", lambda: None)
    monkeypatch.setattr(threading.Thread, "start", lambda self: None)
    assert app.main(background=background) == 0
    assert len(calls) == 1 and calls[0]["hidden"] is background


def test_STAY_009_second_start_reveals_existing_hidden_window_without_another_backend(monkeypatch):
    locks, shown = [], []
    show_existing = single_instance.show_existing
    monkeypatch.setattr(single_instance, "acquire", lambda name: locks.append(name) or False)
    monkeypatch.setattr(single_instance, "show_existing", lambda title: shown.append(title) or True)
    for name in ("LECTUREHUB_UI_MULTI", "LECTUREHUB_UI_FAKE_ENGINE", "LECTUREHUB_UI_CDP_PORT"):
        monkeypatch.delenv(name, raising=False)
    assert app._already_open()
    assert locks[0] == single_instance.RUNNING and shown == [app.MAIN_TITLE]
    calls = []
    class Find:
        restype = None
        def __call__(self, *args):
            return 123
    user = SimpleNamespace(FindWindowW=Find(), IsIconic=lambda hwnd: False, IsWindowVisible=lambda hwnd: False,
                           ShowWindow=lambda hwnd, cmd: calls.append(("show", cmd)),
                           SetForegroundWindow=lambda hwnd: calls.append(("front",)))
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(user32=user))
    assert show_existing(app.MAIN_TITLE)
    assert calls == [("show", 9), ("front",)]


def test_TRAY_001_dedicated_thread_queues_toasts_dispatches_clicks_and_removes_icon(monkeypatch):
    calls, messages = [], []
    open_done, quit_done, stop = threading.Event(), threading.Event(), threading.Event()
    native_thread = []
    class Native:
        def __init__(self, dispatch, drain, icon):
            self.dispatch, self.drain = dispatch, drain
            native_thread.append(threading.get_ident())
        def add(self):
            calls.append("add")
            return True
        def run(self):
            self.dispatch("open")
            self.dispatch("quit")
            stop.wait(2)
            self.drain()
        def post(self, message):
            if message == tray.WM_CLOSE:
                stop.set()
        def balloon(self, *args):
            messages.append(args)
            assert threading.get_ident() == native_thread[0]
        def close(self):
            calls.append("delete")
    owner = tray.Tray(open_done.set, quit_done.set, native_factory=Native)
    monkeypatch.setattr(notify, "_tray", owner)
    assert owner.start()
    assert native_thread[0] != threading.get_ident()
    assert notify.toast("synthetic title", "synthetic message")
    assert open_done.wait(2) and quit_done.wait(2)
    owner.stop()
    owner._thread.join(2)
    assert not owner._thread.is_alive() and not owner.available
    assert calls == ["add", "delete"] and messages == [("synthetic title", "synthetic message")]


def test_TRAY_002_add_failure_cleans_up_and_native_messages_restore_after_explorer_restart():
    calls = []
    class Native:
        def __init__(self, *args):
            pass
        def add(self):
            return False
        def close(self):
            calls.append("delete")
    owner = tray.Tray(lambda: None, lambda: None, native_factory=Native)
    assert not owner.start()
    owner._thread.join(2)
    assert calls == ["delete"] and not owner.toast("a", "b")
    native = tray._Native.__new__(tray._Native)
    native.recreated = 999
    native.dispatch = calls.append
    native.add = lambda: calls.append("add") or False
    native.u = SimpleNamespace(DefWindowProcW=lambda *args: 0)
    for message in (tray.WM_LBUTTONUP, tray.NIN_BALLOONUSERCLICK):
        assert native._wndproc(1, tray.CALLBACK, 0, message) == 0
    native._wndproc(1, 999, 0, 0)
    assert calls[-4:] == ["open", "open", "add", "unavailable"]
    assert native._wndproc(1, 0x0011, 0, 0) == 1
    native._wndproc(1, 0x0016, 0, 0)
    assert calls[-2:] == ["session_end", "session_cancel"]


def test_TRAY_003_ctypes_wiring_balloon_menu_and_resource_cleanup_use_only_fake_dlls(monkeypatch):
    """The actual ctypes structs/callback are built, but every Windows function is a fake."""
    calls, events = [], []
    class Function:
        def __init__(self, name):
            self.name = name
        def __call__(self, *args):
            calls.append((self.name, args))
            if self.name == "Shell_NotifyIconW":
                data = args[1]._obj
                assert data.cbSize == ctypes.sizeof(data) and data.hWnd == 123
                if args[0] == 1:
                    assert data.szInfoTitle == "synthetic" and data.szInfo == "message"
            return {"CreateWindowExW": 123, "RegisterWindowMessageW": 777, "LoadImageW": 456,
                    "GetMessageW": 0}.get(self.name, 1)
    class Dll:
        def __getattr__(self, name):
            fn = Function(name)
            setattr(self, name, fn)
            return fn
    dlls = {}
    monkeypatch.setattr(ctypes, "WinDLL", lambda name, **kwargs: dlls.setdefault(name, Dll()), raising=False)
    monkeypatch.setattr(ctypes, "WINFUNCTYPE", getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE), raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    native = tray._Native(events.append, lambda: None, "synthetic.ico")
    assert native.add()
    native.balloon("synthetic", "message")
    native._wndproc(123, tray.CALLBACK, 0, tray.WM_RBUTTONUP)
    assert events == ["open"]  # menu command 1
    native._wndproc(123, 777, 0, 0)
    assert events[-1] == "available"  # Explorer successfully re-created the icon
    native.run()
    native.close()
    assert [args[0] for name, args in calls if name == "Shell_NotifyIconW"] == [0, 1, 0, 2]
    assert any(name == "DestroyWindow" for name, args in calls)
    assert any(name == "DestroyIcon" and args == (456,) for name, args in calls)
    assert any(name == "DestroyMenu" for name, args in calls)
    assert any(name == "UnregisterClassW" for name, args in calls)


def test_STAY_010_recording_started_during_first_close_dialog_takes_existing_recording_path(api, monkeypatch):
    made, calls = api
    def answer(*args):
        made._backend.recording = True
        return False
    made._main.create_confirmation_dialog = answer
    monkeypatch.setattr(made, "_keep_open_while_recording", lambda: calls.append("original-recording-path"))
    assert made._on_main_closing() is False
    made._pending_close.pop()()
    assert calls == ["original-recording-path"]
    assert not made._notice_stop.is_set() and not made._quitting


def test_STAY_012_a_recording_still_holds_the_window_at_shutdown(api):
    """Before D-084 a recording kept the window open whatever asked to close it; the session flag must not change that."""
    made, calls = api
    made._backend.recording = True
    made._keep_open_while_recording = lambda: calls.append("original-recording-path")
    made._session_end(True)
    assert made._on_main_closing() is False and calls == ["original-recording-path"]
    assert not made._quitting and not made._notice_stop.is_set()


def test_STAY_011_system_shutdown_before_tray_message_does_not_prompt_or_hide(api, monkeypatch):
    made, calls = api
    monkeypatch.setattr(tray, "session_ending", lambda: True)
    assert startup.close_choice() is None
    assert made._on_main_closing() is True and calls == ["helpers.stop"]
    assert made._notice_stop.is_set()


def test_TRAY_004_session_metric_is_read_without_any_window_or_shutdown_api(monkeypatch):
    calls = []
    user = SimpleNamespace(GetSystemMetrics=lambda code: calls.append(code) or 1)
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(user32=user), raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    assert tray.session_ending() and calls == [0x2000]
