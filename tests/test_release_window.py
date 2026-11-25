"""The window side of the release review of 2026-11-16 (docs/reviews/release_bundle2_review_open_2026-11-16.md).

What both windows show and announce about a recording that lost sound or runs out of disk, for either engine; the
start notices; the settings switch for reading notices; and the rule that a window never leaves the app's page.

Python parts run against ``Backend`` with the engine process stubbed (events are fed in by hand). The pages' own
logic is run by node from the real files (app.js, mini.html) with a stub page and a stub API; those tests are skipped
where node is not installed. Nothing here opens a window, a sound device or the network.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from test_ui_backend import semester, tuesday  # noqa: F401 - shared synthetic semester fixture

from lecturehub.ui import app as ui_app
from lecturehub.ui import backend as backend_module
from lecturehub.ui.backend import Backend

WEB = Path(ui_app.__file__).resolve().parent / "web"
needs_node = pytest.mark.skipif(not shutil.which("node"), reason="node is not installed: the page logic cannot be run")

STATUS = {"type": "status", "elapsed_ms": 60000, "rms": -30.0, "peak": -20.0, "quiet": False, "paused": False,
          "dropped_samples": 0, "course": "구조역학", "mode": "mic", "device": "Fake"}
GAP = {"at": 1, "audio_ms": 60000, "lost_ms": 42000, "reason": "source_lost", "open": False}
OPEN_GAP = {"at": 2, "audio_ms": 90000, "lost_ms": 600000, "reason": "source_lost", "open": True}


def _app(monkeypatch, main_mode: str = "mic", second: str | None = None) -> tuple[Backend, list]:
    """A backend that believes it is recording (no engine process), and the list of what it announced."""
    monkeypatch.setattr(backend_module.EngineLink, "spawn", lambda self, args, cwd=None: None)
    monkeypatch.setattr(backend_module.time, "sleep", lambda seconds: None)
    app = Backend(clock=tuesday, fake_engine=True)
    said: list[str] = []
    app._attention = said.append  # the real one starts a thread for the app's hook; here: what was asked for
    with_pc = second == "partner"
    assert app.start("구조역학", main_mode, "Fake", "user_pick", with_pc=with_pc)["ok"]
    app._on_event({"type": "started", "device": "Fake", "course": "구조역학", "mode": main_mode, "session_dir": "main"})
    if second == "own":
        assert app.start_companion()["ok"]
    if second:
        app._on_pc_event({"type": "started", "session_dir": "second"})
    return app, said


# ---------------------------------------------------------------- the second engine and a re-attached engine
@pytest.mark.parametrize(("main_mode", "second", "second_mode"), [("loopback", "own", "mic"), ("mic", "partner", "loopback")])
def test_second_engine_stall_low_disk_and_gaps_are_kept_and_announced(semester, monkeypatch, main_mode, second,  # noqa: F811
                                                                         second_mode):
    app, said = _app(monkeypatch, main_mode, second)
    app._on_pc_event({"type": "stalled", "reason": "source_lost"})
    app._on_pc_event({**STATUS, "stalled": True, "recoveries": 0, "disk_low": True, "disk_minutes": 30})
    pc = app.state()["pc"]
    assert pc["mode"] == second_mode
    assert pc["status"]["stalled"] is True and pc["status"]["disk_low"] is True and pc["status"]["disk_minutes"] == 30
    assert said == ["stalled", "disk_low"]  # one each: the status line that repeats the stall adds nothing
    app._on_pc_event({**STATUS, "stalled": True, "recoveries": 0, "disk_low": True, "disk_minutes": 30})
    app._on_pc_event({"type": "recovered", "device": "Fake", "recoveries": 1, **GAP})
    app._on_pc_event({**STATUS, "stalled": False, "recoveries": 1, "disk_low": True, "disk_minutes": 29})
    assert said == ["stalled", "disk_low"] and app.state()["pc"]["status"]["recoveries"] == 1
    app._on_pc_event({"type": "stalled", "reason": "no_data"})  # a second stall is a new announcement
    assert said == ["stalled", "disk_low", "stalled"]
    app._on_pc_event({"type": "stopped", "session_dir": "second", "bookmarks": 0, "report": {
        "total_ms": 90000, "gaps": [GAP, OPEN_GAP], "cause": "source_lost", "partial": True}})
    pc = app.state()["pc"]
    assert (pc["gaps"], pc["lost_ms"], pc["tail_lost_ms"], pc["cause"], pc["partial"]) == (2, 642000, 600000, "source_lost", True)
    assert app.state()["status"].get("stalled") is None  # the main engine's picture is its own


def test_an_engine_that_sends_status_lines_only_announces_a_stall_once(semester, monkeypatch):  # noqa: F811
    """After an app restart the engine is followed through live.json: there are no ``stalled`` events, only status
    lines whose ``stalled`` stays true for the whole stall."""
    app, said = _app(monkeypatch, "mic", "own")
    for _ in range(3):
        app._on_event({**STATUS, "stalled": True})
    assert said == ["stalled"]
    app._on_event({**STATUS, "stalled": False})
    app._on_event({**STATUS, "stalled": True})
    assert said == ["stalled", "stalled"]
    for _ in range(2):  # the second engine, followed the same way
        app._on_pc_event({**STATUS, "stalled": True})
    assert said == ["stalled", "stalled", "stalled"]


def test_a_spawned_engine_is_not_announced_twice_for_one_stall(semester, monkeypatch):  # noqa: F811
    app, said = _app(monkeypatch)
    app._on_event({"type": "stalled", "reason": "no_data"})
    for _ in range(3):  # reopened, but no sound yet: the status keeps saying stalled
        app._on_event({**STATUS, "stalled": True})
    app._on_event({"type": "recovered", "device": "Fake", "recoveries": 1, **GAP})
    app._on_event({**STATUS, "stalled": False, "recoveries": 1})
    assert said == ["stalled"]


# ---------------------------------------------------------------- low disk while recording
def test_low_disk_is_announced_once_per_recording(semester, monkeypatch):  # noqa: F811
    app, said = _app(monkeypatch)
    app._on_event({**STATUS, "disk_low": False, "disk_minutes": 400})
    assert said == [] and app.state()["disk"] == {"low": False, "minutes": 400}
    for minutes in (40, 39, 38):
        app._on_event({**STATUS, "disk_low": True, "disk_minutes": minutes})
    assert said == ["disk_low"] and app.state()["disk"] == {"low": True, "minutes": 38}
    assert app.disk_minutes_left() == 38
    app._on_event({"type": "stopped", "session_dir": "main", "bookmarks": 0, "report": {"total_ms": 1}})
    assert app.state()["disk"] is None  # nothing is recording: nothing to show
    app._on_event({"type": "discarded", "session_dir": None})
    assert app.start("구조역학", "mic", "Fake", "user_pick")["ok"]  # the next recording is told again
    app._on_event({"type": "started", "device": "Fake", "course": "구조역학", "mode": "mic", "session_dir": "next"})
    app._on_event({**STATUS, "disk_low": True, "disk_minutes": 20})
    assert said == ["disk_low", "disk_low"]


def test_two_engines_share_the_disk_so_the_minutes_are_for_both(semester, monkeypatch):  # noqa: F811
    app, said = _app(monkeypatch, "mic", "partner")
    app._on_event({**STATUS, "disk_low": True, "disk_minutes": 40})
    app._on_pc_event({**STATUS, "disk_low": True, "disk_minutes": 40})
    assert app.state()["disk"] == {"low": True, "minutes": 20} and said == ["disk_low"]
    app._on_pc_event({**STATUS, "disk_low": True, "disk_minutes": 0})
    assert app.state()["disk"]["minutes"] == 0


def test_bookkeeping_that_fails_never_ends_the_reader_of_engine_lines(semester, monkeypatch):  # noqa: F811
    """D-050: the thread that calls ``_on_event`` reads the engine's output. If it died, the engine's lines would
    pile up unread, so nothing that only informs may raise out of it."""
    app, _ = _app(monkeypatch, "mic", "partner")
    app._on_event({**STATUS, "stalled": "yes", "disk_low": "?", "disk_minutes": "many"})  # values of the wrong kind
    app._on_pc_event({**STATUS, "disk_low": None, "disk_minutes": None})

    def broken():
        raise RuntimeError("synthetic")

    monkeypatch.setattr(app, "_note_disk", broken)
    app._on_event({**STATUS, "disk_low": True, "disk_minutes": 5})
    app._on_pc_event({**STATUS, "disk_low": True, "disk_minutes": 5})
    assert app.state()["status"]["disk_minutes"] == 5  # the line itself was still taken


def test_a_notice_that_cannot_be_given_never_raises_into_the_engine_reader(semester, monkeypatch):  # noqa: F811
    app, _ = _app(monkeypatch)
    del app._attention  # the real one this time
    app.attention_hook = lambda kind: None

    class NoThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(backend_module.threading, "Thread", NoThread)
    app._on_event({"type": "stalled", "reason": "no_data"})
    app._on_event({**STATUS, "stalled": True, "disk_low": True, "disk_minutes": 3})
    assert app.state()["status"]["stalled"] is True


# ---------------------------------------------------------------- the summary
def test_summary_keeps_an_open_gap_apart_from_gaps_in_the_middle(semester, monkeypatch):  # noqa: F811
    app, _ = _app(monkeypatch)
    app._on_event({"type": "stopped", "session_dir": "main", "bookmarks": 0, "report": {
        "total_ms": 90000, "cause": "user_stop", "partial": True,
        "gaps": [GAP, OPEN_GAP, "not a gap", {"lost_ms": "x"}, {"lost_ms": -5}]}})
    summary = app.state()["summary"]
    assert (summary["gaps"], summary["lost_ms"], summary["tail_lost_ms"]) == (4, 642000, 600000)
    assert backend_module._gap_summary({"gaps": [GAP]}) == {"gaps": 1, "lost_ms": 42000, "tail_lost_ms": None}
    assert backend_module._gap_summary({}) == {"gaps": 0, "lost_ms": 0, "tail_lost_ms": None}


def test_cleanup_that_left_the_wav_files_is_kept_for_the_summary(semester, monkeypatch):  # noqa: F811
    app, _ = _app(monkeypatch, "mic", "partner")
    app._on_event({"type": "stopped", "session_dir": "main", "bookmarks": 0, "report": {"total_ms": 90000}})
    app._on_event({"type": "compacted", "session_dir": "main", "result": {"state": "compacted", "bytes_freed": 1}})
    assert "cleanup" not in app.state()["summary"]  # done: nothing to say
    app._on_event({"type": "compacted", "session_dir": "another recording", "result": {"state": "skipped", "reason": "x"}})
    assert "cleanup" not in app.state()["summary"]  # an older engine's result never lands on this summary
    app._on_event({"type": "compacted", "session_dir": "main",
                   "result": {"state": "skipped", "reason": "세그먼트도 FLAC도 없어 병합 WAV를 남김"}})
    assert app.state()["summary"]["cleanup"] == {"state": "skipped", "reason": "세그먼트도 FLAC도 없어 병합 WAV를 남김"}
    app._on_pc_event({"type": "compacted", "session_dir": "second", "result": {"state": "error", "error": "boom"}})
    assert app.state()["pc"]["cleanup"] == {"state": "error", "error": "boom"}
    note = backend_module._cleanup_note
    for state in ("off", "deferred", "deferred_recording", "done", "none", "masters_only", "compacted", "recycled"):
        assert note({"state": state, "reason": "r"}) is None, state
    assert note({"state": "flac_only", "reason": "r"}) == {"state": "flac_only", "reason": "r"}
    assert note(None) is None and note("x") is None


def test_wav_copies_deleted_for_good_are_counted_for_the_summary():
    """recorder/compact.py on a drive without a Recycle Bin: ``permanent``, ``deleted_permanently``,
    ``not_deleted`` and, when something stayed, ``reason`` (safety commit 1ec25f0)."""
    note = backend_module._cleanup_note
    gone = ["segment_0001.wav", "segment_0002.wav", "master_x.wav"]
    assert note({"state": "compacted", "permanent": True, "deleted_permanently": gone, "not_deleted": []}) == {
        "state": "compacted", "deleted": 3}
    assert note({"state": "masters_only", "permanent": True, "deleted_permanently": gone[2:]}) == {
        "state": "masters_only", "deleted": 1}
    reason = "휴지통이 없는 드라이브에서 WAV 파일을 지우지 못해 남겨 두었습니다: segment_0002.wav"
    assert note({"state": "partial", "permanent": True, "deleted_permanently": gone[:1], "not_deleted": gone[1:2],
                 "reason": reason}) == {"state": "partial", "deleted": 1, "reason": reason}
    # put off for a live job, but the master WAV already went for good
    # ...and the summary must say that the 1-minute pieces follow later (they go when the live transcription lets
    # go of them, in another process): without ``later`` the line read "1개를 바로 지웠습니다" and nothing more
    assert note({"state": "deferred", "master": {"state": "deleted", "permanent": True,
                                                 "deleted_permanently": ["master_x.wav"]}}) == {
        "state": "deleted", "deleted": 1, "later": True}
    assert note({"state": "compacted", "permanent": True, "deleted_permanently": []}) is None  # nothing went


# ---------------------------------------------------------------- the Windows notification
def test_low_disk_has_its_own_notification_text():
    class Minutes:
        def __init__(self, left):
            self.left = left

        def disk_minutes_left(self):
            return self.left

    api = ui_app.Api(Minutes(12))
    title, body = api._attention_text("disk_low")
    # the engine's disk_minutes: recording that can still be finished with its MP3, not "until the disk is full"
    assert "디스크" in title and "끝까지 저장할 수 있는 녹음은 약 12분 남았습니다" in body and "멈춥니다" not in body
    assert "지금까지 녹음한 것도" in ui_app.Api(Minutes(0))._attention_text("disk_low")[1]
    assert "약" not in ui_app.Api(Minutes(None))._attention_text("disk_low")[1]  # unknown: no made-up number
    assert "끊겼습니다" in api._attention_text("stalled")[0]
    assert api._attention_text("class_over") is None and api._attention_text("next_class") is None


# ---------------------------------------------------------------- a window never leaves the app's page
OWN = "http://127.0.0.1:42001/index.html"


@pytest.mark.parametrize(("target", "stays"), [
    (OWN, True), (OWN + "#part", True), ("http://127.0.0.1:42001/mini.html", True), ("about:blank", True),
    ("http://127.0.0.1:42002/m/note.html", False),  # the media server: another port is another origin
    ("http://localhost:42001/index.html", False), ("https://127.0.0.1:42001/index.html", False),
    ("https://example.com/", False), ("http://127.0.0.1:x/", False), ("data:text/html,x", False),
    ("javascript:alert(1)", False), ("file:///C:/x.html", False), ("", False), (None, False)])
def test_only_the_apps_own_address_counts_as_staying(target, stays):
    assert ui_app.stays_on_own_page(OWN, target) is stays


def test_a_file_page_stays_only_on_that_file():
    own = "file:///C:/app/web/index.html"
    assert ui_app.stays_on_own_page(own, own + "#x") and not ui_app.stays_on_own_page(own, "file:///C:/other.html")
    assert not ui_app.stays_on_own_page(None, OWN)


class _Hook:
    """Stands in for a pywebview event and for a WebView2 event: ``+=`` adds a handler."""

    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self


class _Holder:
    def __init__(self, **fields):
        self.__dict__.update(fields)


def _window(native=True):
    control = _Holder(NavigationStarting=_Hook())
    window = _Holder(real_url=OWN, events=_Holder(before_show=_Hook()),
                     native=_Holder(webview=control) if native else None)
    return window, control


def test_navigation_away_from_the_app_page_is_cancelled_before_it_starts():
    window, control = _window()
    refused = []
    ui_app.guard_navigation(window, on_refused=refused.append)
    assert control.NavigationStarting.handlers == []  # attached when the WebView2 control exists (before_show)
    for attach in window.events.before_show.handlers:
        attach()
    (handler,) = control.NavigationStarting.handlers
    for target, cancelled in ((OWN, False), (OWN + "#toc", False), ("https://attacker.example/", True),
                              ("http://127.0.0.1:42002/m/note.html", True)):
        args = _Holder(Uri=target, Cancel=False)
        handler(control, args)
        assert args.Cancel is cancelled, target
    assert refused == ["https://attacker.example/", "http://127.0.0.1:42002/m/note.html"]


def test_a_window_whose_engine_control_cannot_be_reached_still_opens():
    window, _ = _window(native=False)
    ui_app.guard_navigation(window)
    for attach in window.events.before_show.handlers:
        attach()  # written to the log, never raised: the window opens as before


def test_a_window_made_after_start_is_guarded_at_once():
    """The small window is made on the first notice, after webview.start: pywebview has already raised its
    before_show, so the guard is attached right away instead of waiting for an event that will not come."""
    window, control = _window()

    class Shown(_Hook):
        def is_set(self):
            return True

    window.events.before_show = Shown()
    ui_app.guard_navigation(window)
    (handler,) = control.NavigationStarting.handlers
    args = _Holder(Uri="https://attacker.example/", Cancel=False)
    handler(control, args)
    assert args.Cancel is True


def test_the_app_guards_both_of_its_windows():
    """Both windows carry the whole API: the main one from the start, the small one when a notice first needs it
    (Api._mini_window → _create_mini). (That the guard works in the real engine is the last step of the
    hidden-window self-check: core/selfcheck.py.)"""
    import inspect

    from lecturehub.core import selfcheck

    main = inspect.getsource(ui_app.main)
    assert "guard_navigation(main_window)" in main and main.count("create_window(") == 1  # no small window at start
    made = inspect.getsource(ui_app._create_mini)
    assert "guard_navigation(mini)" in made and "api._on_mini_closing" in made
    walk = inspect.getsource(selfcheck._window)
    # the self-check makes the small window the app's way (after start, through Api._mini_window), not up front
    assert walk.count("create_window(") == 1 and "api._mini_window()" in walk and "_show_mini" not in walk.split('"""')[2]
    assert "guard_navigation(window, on_refused=refused.append)" in walk and "_LEAVE_TO" in walk


# ---------------------------------------------------------------- the pages' own logic, run by node
HARNESS = r"""
const fs = require("fs");
const [mode, appJs, miniHtml] = process.argv.slice(2);
const src = fs.readFileSync(appJs, "utf8"), mini = fs.readFileSync(miniHtml, "utf8");
const cut = (text, from, to) => { const a = text.indexOf(from), b = text.indexOf(to, a + 1); if (a < 0 || b < 0) throw new Error("marker not found: " + (a < 0 ? from : to)); return text.slice(a, b); };
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const out = {};

function health() {  // app.js: healthHtml and the helpers written in front of it, as they are
  const first = src.includes("  function lostText(ms) {") ? "  function lostText(ms) {" : "  function healthHtml(st) {";
  const body = cut(src, first, "  function renderDock() {");
  return new Function("esc", "shortDevice", "db", "fmt", "ui", body + "\n return { healthHtml, gapsText: typeof gapsText === 'function' ? gapsText : () => null, diskText: typeof diskText === 'function' ? diskText : () => null };")(
    esc, (x) => String(x || ""), (x) => (x === null || x === undefined ? "-inf dB" : Math.round(x) + " dB"), (x) => "T" + x, {});
}

function miniPage() {  // mini.html: its inline script against a stub page and a stub API
  const code = mini.slice(mini.indexOf("<script>") + 8, mini.lastIndexOf("</script>"));
  const els = {}, timers = [], calls = [];
  const el = () => ({ textContent: "", title: "", hidden: false, disabled: false, dataset: {}, style: {}, onclick: null, firstElementChild: { style: {} },
    classList: { on: new Set(), toggle(n, v) { if (v === undefined ? !this.on.has(n) : v) this.on.add(n); else this.on.delete(n); } } });
  const page = { state: { phase: "idle" }, start: { ok: true }, mic: { ok: true } };
  const api = { state: async () => page.state, start_selected: async () => { calls.push("start_selected"); return page.start; },
    open_mic_settings: async () => { calls.push("open_mic_settings"); return page.mic; }, stop: async () => ({ ok: true }),
    next_class_answer: async (a) => { calls.push("next:" + a); }, class_over_answer: async (a) => { calls.push("over:" + a); },
    pause: async () => ({}), bookmark: async () => ({}), show_main: async () => ({}), set_on_top: async () => ({}) };
  for (const id of ["note", "noteText", "noteA", "noteB", "levelRow", "level", "what", "recBtn"]) els[id] = el();
  new Function("document", "window", "setTimeout", code)(
    { getElementById: (id) => els[id] || (els[id] = el()) }, { pywebview: { api }, addEventListener() {} }, (fn) => { timers.push(fn); return 0; });
  const tick = () => new Promise((resolve) => setImmediate(resolve));
  const show = async (state) => { page.state = state; const poll = timers.pop(); if (poll) await poll(); await tick(); return look(); };
  const look = () => ({ note: els.note.hidden ? null : els.noteText.textContent, title: els.noteText.title, level: !els.levelRow.hidden,
    a: els.noteA.hidden ? null : els.noteA.textContent, b: els.noteB.hidden ? null : els.noteB.textContent,
    what: els.what.textContent, alarm: els.what.classList.on.has("warn"), bar: els.level.classList.on.has("warn") });
  return { els, page, calls, show, look, tick };
}

(async () => {
  if (mode === "health") {
    const { healthHtml } = health(), cases = JSON.parse(fs.readFileSync(process.argv[5], "utf8"));
    for (const [name, st] of Object.entries(cases)) out[name] = healthHtml(st);
  } else if (mode === "gaps") {
    const { gapsText, diskText } = health(), samples = JSON.parse(fs.readFileSync(process.argv[5], "utf8"));
    const theirs = new Function(cut(mini, "  function lostText(ms) {", "  const noSound") + "\n return { gapsText, diskText: typeof diskText === 'function' ? diskText : () => null };")();
    out.app = samples.map(gapsText); out.mini = samples.map(theirs.gapsText);
    const minutes = [null, undefined, 0, -3, 12, 12.7];
    out.disk_app = minutes.map(diskText); out.disk_mini = minutes.map(theirs.diskText);
  } else if (mode === "mini") {
    const rec = { phase: "recording", status: { course: "구조역학", mode: "mic", rms: -30, elapsed_ms: 5000 } };
    let m = miniPage(); await m.tick();
    out.normal = await m.show(rec);
    out.stalled = await m.show({ ...rec, status: { ...rec.status, stalled: true } });
    out.second_stalled = await m.show({ ...rec, pc: { state: "recording", mode: "loopback", status: { stalled: true } } });
    out.disk = await m.show({ ...rec, disk: { low: true, minutes: 12 } });
    out.disk_and_over = await m.show({ ...rec, disk: { low: true, minutes: 12 }, class_over: { kind: "over", reason: "수업 종료" } });
    out.stall_and_over = await m.show({ ...rec, status: { ...rec.status, stalled: true }, class_over: { kind: "over", reason: "수업 종료" } });
    out.lost_tail = await m.show({ phase: "done", summary: { total_ms: 9, dropped_samples: 0, gaps: 1, lost_ms: 600000, tail_lost_ms: 600000, cause: "source_lost" } });
    out.lost_middle_second = await m.show({ phase: "done", summary: { total_ms: 9, dropped_samples: 0, gaps: 0, lost_ms: 0, tail_lost_ms: null },
      pc: { state: "done", mode: "loopback", gaps: 2, lost_ms: 65000, tail_lost_ms: null } });
    out.clean = await m.show({ phase: "done", summary: { total_ms: 9, dropped_samples: 0, gaps: 0, lost_ms: 0, tail_lost_ms: null } });
    // start notices: the answer of start() arrives while the last state read is still the old recording's "done"
    m = miniPage(); await m.tick();
    await m.show({ phase: "done", summary: { total_ms: 9, dropped_samples: 0 } });
    m.page.start = { ok: true, notices: [{ kind: "mic_blocked", text: "MIC TEXT" }, { kind: "disk_low", text: "DISK TEXT" }] };
    await m.els.recBtn.onclick(); await m.tick();
    out.notice_before_running = m.look();
    out.notice_mic = await m.show(rec);
    await m.els.noteA.onclick(); await m.tick(); await m.tick();
    out.notice_disk = m.look(); out.calls = m.calls.slice();
    await m.els.noteB.onclick(); await m.tick();
    out.notice_closed = m.look();
    // Windows Settings could not be opened: said in the card
    m = miniPage(); await m.tick();
    m.page.start = { ok: true, notices: [{ kind: "mic_blocked", text: "MIC TEXT" }] }; m.page.mic = { ok: false, error: "열지 못했습니다" };
    await m.els.recBtn.onclick(); await m.show(rec);
    await m.els.noteA.onclick(); await m.tick(); await m.tick();
    out.settings_failed = m.look();
    // a start that was refused stays on screen
    m = miniPage(); await m.tick();
    m.page.start = { ok: false, reason: "녹음 허락이 필요합니다" };
    await m.els.recBtn.onclick(); await m.tick();
    out.refused = await m.show({ phase: "idle" });
  } else if (mode === "settings") {
    const body = cut(src, "  const ed =", "  function applyLabs() {") + cut(src, "  function layoutSection(L, mode, pick) {", "  /* ---------- timetable and term editor (TT-001)");
    const run = async ({ startOn, clickTo, fails, redrawWhileSaving }) => {
      const saved = { on: startOn }, toasts = [], handlers = {};
      const scrim = { className: "", innerHTML: "", addEventListener(name, fn) { handlers[name] = fn; }, remove() {},
        querySelector(sel) { return sel === "[data-notices]" ? scrim.box : null; }, box: null };
      const press = (a) => handlers.click({ target: { closest: (sel) => (sel === "button[data-a]" ? { dataset: { a }, closest: () => null } : null) } });
      const api = {
        conn_status: async () => ({ ok: true, features: [], keys: [], tools: [], labs: {}, lab_names: {} }),
        recording_permissions: async () => ({ courses: [], states: {} }), appearance: async () => ({ theme: "system", text_size: "normal" }),
        ai_status: async () => ({ choices: {}, settings: {} }), folder_layout_get: async () => ({}), audio_cleanup_get: async () => ({}),
        live_engine_get: async () => null, about: async () => ({ ok: true, version: "0", folders: {}, notices: [] }),
        consent_list: async () => ({ rows: [] }), update_status: async () => ({ possible: false }),
        ai_link_status: async () => ({ enabled: false, courses: [], snippets: {} }),
        notices_setting_get: async () => ({ ok: true, on: saved.on, readings: 3, characters: 4200 }),
        notices_setting_set: async (on) => {
          if (redrawWhileSaving) await press("report");  // another redraw lands while the save is on its way
          if (fails === "throws") throw new Error("bridge");
          if (fails) return { ok: false, error: "설정 파일을 쓰지 못했습니다" };
          saved.on = !!on; return { ok: true, on: saved.on, readings: 3, characters: 4200 };
        },
        report_plan: async () => ({ ok: true, files: [], excluded: [] }), report_make: async () => ({ ok: true, name: "report.zip" }),
      };
      const env = { document: { createElement: () => scrim, body: { appendChild() {} } }, keepFocus() {}, loadEngines() {}, renderLive() {},
        toast: (m) => toasts.push(m), esc, FEATURE_BADGE: {}, ui: { info: { edition: {} } }, api, matesFrom() {}, matesHtml() { return ""; },
        matesInput() {}, matesClick() {}, matesText() {}, applyAppearance() {}, applyLabs() {}, openTimetable: async () => false,
        dialog: async () => false, askText: async () => "", ic: (n) => `<svg data-i="${n}"></svg>` };
      const names = Object.keys(env);
      const openSettings = new Function(...names, body + "\n return openSettings;")(...names.map((n) => env[n]));
      const drawn = () => /data-notices checked/.test(scrim.innerHTML);
      await openSettings("ai");  // the group the card is in (settings menu, 2026-11-25)
      const text = (scrim.innerHTML.match(/<h5>수업 중 공지 찾기<\/h5>[\s\S]*?<\/p>/) || [""])[0];
      scrim.box = { matches: (sel) => sel === "[data-notices]", checked: clickTo, closest: () => null };  // the user clicked: the box flipped
      try { await handlers.change({ target: scrim.box }); } catch (err) { toasts.push("the handler threw: " + err.message); }
      const box = scrim.box.checked;
      await press("report");  // a redraw from the cached screen state (no reload)
      return { text, saved: saved.on, box, redrawn: drawn(), toasts };
    };
    out.on = await run({ startOn: false, clickTo: true });
    out.off = await run({ startOn: true, clickTo: false });
    out.fails = await run({ startOn: true, clickTo: false, fails: true });
    out.throws = await run({ startOn: false, clickTo: true, fails: "throws" });
    out.redraw = await run({ startOn: false, clickTo: true, redrawWhileSaving: true });
  } else if (mode === "groups") {  // the settings menu: groups, redraws, folds, "all at once" (user 2026-11-25)
    const body = cut(src, "  const ed =", "  function applyLabs() {") + cut(src, "  function layoutSection(L, mode, pick) {", "  /* ---------- timetable and term editor (TT-001)");
    const config = process.argv[5] ? JSON.parse(fs.readFileSync(process.argv[5], "utf8")) : {};
    const many = Number(config.courses == null ? 10 : config.courses);
    const courses = Array.from({ length: many }, (_, i) => `합성과목 ${i + 1}`);
    const STATES = { allowed: "허락함", syllabus: "강의계획서 허용", unknown: "아직 확인 안 함", forbidden: "금지" };
    const perm = Object.fromEntries(courses.map((c) => [c, "unknown"])), calls = [], toasts = [], handlers = {};
    const reminders = { deadlines: true, briefing: true, muted_courses: [courses[1]] };
    const stay = { ok: true, keep_running: null, startup: { ok: true, possible: true, store: !!config.store, enabled: config.store ? null : false } };
    let startupFail = false;
    const scrim = { className: "", innerHTML: "", addEventListener(name, fn) { handlers[name] = fn; }, remove() {}, querySelector: () => null };
    const press = (a, data = {}) => handlers.click({ target: { closest: (sel) => (sel === "button[data-a]" ? { dataset: { a, ...data }, closest: () => null } : null) } });
    const change = (target) => handlers.change({ target: { matches: () => false, closest: () => null, dataset: {}, ...target } });
    const api = {
      conn_status: async () => ({ ok: true, features: [{ id: "notes", label: "정리본(AI)", state: "ready", hint: "h" }],
        keys: [{ id: "gemini", label: "Gemini", connected: false, steps: "s", note: "n" }],
        tools: [{ id: "ffmpeg", label: "ffmpeg", found: true, required: true, needed_for: "x", hint: "h" },
          { id: "codex", label: "Codex", found: false, required: false, needed_for: "x", hint: "h", help_url: "u" },
          { id: "gcloud", label: "Google Cloud CLI", found: false, required: false, needed_for: "x", hint: "h" }],
        labs: { gsr: true }, lab_names: { gsr: "그스룸 예약" } }),
      recording_permissions: async () => ({ ok: true, states: STATES, courses: courses.map((c) => ({ course: c, state: perm[c] })) }),
      recording_permission: async (c, s) => { perm[c] = s; return { ok: true, state: s }; },
      recording_permission_confirm_many: async (state, names) => { calls.push(["confirm_many", state, names.length]); return { ok: true, yes: true, token: "T" }; },
      recording_permission_all: async (state, names, token) => { calls.push(["all", state, names.length, token]); names.forEach((c) => { perm[c] = state; }); return { ok: true, changed: names.length }; },
      appearance: async () => ({ theme: "system", text_size: "normal" }),
      ai_status: async () => ({ choices: { codex: "Codex (ChatGPT)", claude: "Claude Code", off: "사용 안 함" }, settings: {} }),
      ai_select: async () => ({ ok: true }),
      folder_layout_get: async () => ({ ok: false, error: "합성" }), audio_cleanup_get: async () => ({ ok: true, enabled: false, days: 7 }),
      live_engine_get: async () => ({ ok: true, engine: "quick", choices: { quick: "Gemini Live" } }),
      about: async () => ({ ok: true, version: "0", folders: {}, notices: [] }),
      consent_list: async () => ({ rows: courses.map((c) => ({ provider: "ai_app_link", course: c, label: "AI 앱 연결" })) }),
      update_status: async () => ({ possible: true, on: true }),
      ai_link_status: async () => ({ ok: true, enabled: true, courses: courses.map((c) => ({ course: c, allowed: false })), codex: "A", claude_desktop: "B", claude_code: "C" }),
      notices_setting_get: async () => ({ ok: true, on: true, engine: "codex", codex_found: true, courses: courses.map((c) => ({ course: c, allowed: false })), needs_consent: [courses[0]],
        etl_courses: courses.map((c, i) => ({ course: c, allowed: i === 1 })), etl_needs_consent: [courses[2]] }),
      etl_notify_get: async () => ({ ok: true, switches: { assignment_new: true, due_changed: true, files_new: false },
        labels: { assignment_new: "새 eTL 과제", due_changed: "eTL 과제 마감이 바뀜", files_new: "eTL에 새 강의자료" } }),
      etl_notify_set: async (kind, on) => { calls.push(["notify", kind, on]); return { ok: true, switches: {}, labels: { [kind]: "LABEL" } }; },
      reminders_get: async () => ({ ok: true, ...reminders, courses: courses.map((c) => ({ course: c, muted: reminders.muted_courses.includes(c) })) }),
      reminders_set: async (changes) => { calls.push(["reminders", changes]); Object.assign(reminders, changes); return api.reminders_get(); },
      stay_get: async () => stay,
      stay_set: async (on) => { calls.push(["stay", on]); stay.keep_running = on; return stay; },
      startup_set: async (on) => { calls.push(["startup", on]); if (startupFail) return { ok: false, error: "합성 저장 실패" }; stay.startup.enabled = on; return { ok: true }; },
      startup_settings: async () => { calls.push(["startup-settings"]); return { ok: true }; },
      quit_app: async () => { calls.push(["quit-app"]); return { ok: true }; },
      etl_notices_allow_all: async (names, t) => { calls.push(["etl_all", names.length, t]); return { ok: true }; },
      consent_confirm: async (p, c) => { calls.push(["consent_one", p, c]); return { ok: true, yes: true, token: "ONE" }; },
      etl_notices_allow: async (c, t) => { calls.push(["etl_one", c, t]); return { ok: true }; },
      consent_revoke: async (p, c) => { calls.push(["revoke_one", p, c]); return { ok: true }; },
      consent_confirm_many: async (p, names) => { calls.push(["consent_many", p, names.length]); return { ok: true, yes: true, token: "C" }; },
      ai_link_allow_all: async (names, t) => { calls.push(["link_all", names.length, t]); return { ok: true }; },
      notices_allow_all: async (names, t) => { calls.push(["notices_all", names.length, t]); return { ok: true }; },
      consent_revoke_many: async (p, names) => { calls.push(["revoke_many", p, names.length]); return { ok: true, revoked: names.length }; },
      gsr_companions_get: async () => ({ ok: true, rows: [] }), gsr_rooms_get: async () => ({ ok: true, rooms: { avoid: [], center: null, floor_first: null } }),
    };
    const env = { document: { createElement: () => scrim, body: { appendChild() {} } }, keepFocus() {}, loadEngines() {}, renderLive() {},
      toast: (m) => toasts.push(m), esc, FEATURE_BADGE: { ready: ["on", "사용 가능"] }, ui: { info: { edition: { name: "full", gsr: src.includes("function openRoom(") }, class_reminder: true } }, api,
      matesFrom: () => [], matesHtml: () => "", matesInput() {}, matesClick() {}, matesText() {}, applyAppearance() {}, applyLabs() {},
      openTimetable: async () => false, dialog: async () => false, askText: async () => "", ic: (n) => `<svg data-i="${n}"></svg>` };
    const names = Object.keys(env);
    const made = new Function(...names, body + "\n return { openSettings, SETTINGS_GROUPS, settingsView };")(...names.map((n) => env[n]));
    const h5 = () => [...scrim.innerHTML.matchAll(/<h5>([^<]*)<\/h5>/g)].map((m) => m[1]);
    const pane = () => (scrim.innerHTML.match(/data-pane="([^"]+)"/) || [])[1];
    await made.openSettings();
    out.first = pane();
    out.groups = made.SETTINGS_GROUPS.map((g) => ({ id: g.id, label: g.label, sections: g.sections }));
    out.titles = {}; out.current = {};
    for (const g of made.SETTINGS_GROUPS) {
      await press("group", { id: g.id });
      out.titles[g.id] = h5();
      out.current[g.id] = [...scrim.innerHTML.matchAll(/data-id="([^"]+)" aria-current="page"/g)].map((m) => m[1]);
      out[`html_${g.id}`] = scrim.innerHTML;
    }
    // a redraw after an action (load) stays on the group; the next opening comes back to it; a caller can point at one
    await press("group", { id: "record" });
    await change({ closest: (sel) => (sel === "[data-ai-feature]" ? { value: "claude", dataset: { aiFeature: "notes" } } : null) });
    out.after_reload = pane();
    await made.openSettings(); out.reopened = pane();
    await made.openSettings("ai"); out.pointed = pane();
    await made.openSettings("nonsense"); out.unknown_group = pane();
    // folds: a long list starts folded; its state is kept across redraws (and a toggle elsewhere does not touch it)
    await press("group", { id: "record" });
    out.fold_before = /data-fold="permission"( open)?/.exec(scrim.innerHTML);
    handlers.toggle({ target: { matches: (sel) => sel === "details[data-fold]", dataset: { fold: "permission" }, open: true } });
    await change({ closest: (sel) => (sel === "[data-ai-feature]" ? { value: "codex", dataset: { aiFeature: "notes" } } : null) });
    out.fold_after = /data-fold="permission"( open)?/.exec(scrim.innerHTML);
    // all at once: recording permission (asks only for 허락함 / 강의계획서 허용), the AI link and the notices lists
    await press("pick-all", { id: "permission", to: "state" });  // nothing chosen yet
    await change({ matches: (sel) => sel === "[data-pick-state]", dataset: { pickState: "permission" }, value: "allowed" });
    await press("group", { id: "record" });
    out.pick_kept = /data-pick-state="permission"[^]*?<option value="allowed" selected>/.test(scrim.innerHTML);
    await press("pick-all", { id: "permission", to: "state" });
    out.states_after_allowed = [...new Set(Object.values(perm))];
    await change({ matches: (sel) => sel === "[data-pick-state]", dataset: { pickState: "permission" }, value: "forbidden" });
    await press("pick-all", { id: "permission", to: "state" });
    out.states_after_forbidden = [...new Set(Object.values(perm))];
    await press("pick-all", { id: "permission", to: "state" });  // already all 금지
    await press("pick-all", { id: "ailink", to: "on" });
    await press("pick-all", { id: "notices", to: "off" });  // none allowed: nothing to take back
    out.calls = calls.slice(); out.toasts = toasts.slice();
    // eTL (D-077): announcements list (its own consent) all at once and one by one; a notification switch
    const before = calls.length;
    await press("pick-all", { id: "etlnotices", to: "on" });
    await change({ checked: true, closest: (sel) => (sel === "[data-etlnotices-course]" ? { checked: true, dataset: { etlnoticesCourse: courses[0] } } : null) });
    await change({ checked: false, closest: (sel) => (sel === "[data-etlnotices-course]" ? { checked: false, dataset: { etlnoticesCourse: courses[1] } } : null) });
    await change({ closest: (sel) => (sel === "[data-etl-notify]" ? { checked: true, dataset: { etlNotify: "files_new" } } : null) });
    out.etl_calls = calls.slice(before); out.etl_toasts = toasts.slice(out.toasts.length);
    const remindBefore = calls.length;
    await press("pick-all", { id: "mute", to: "on" }); out.all_muted = reminders.muted_courses.length;
    await press("pick-all", { id: "mute", to: "off" }); out.none_muted = reminders.muted_courses.length;
    await change({ closest: (sel) => sel === "[data-mute-course]" ? { checked: true, dataset: { muteCourse: courses[0] } } : null });
    await change({ closest: (sel) => sel === "[data-local-alert]" ? { checked: false, dataset: { localAlert: "briefing" } } : null });
    await change({ closest: (sel) => sel === "[data-local-alert]" ? { checked: false, dataset: { localAlert: "deadlines" } } : null });
    await press("group", { id: "basic" }); out.reminders_after = scrim.innerHTML;
    out.reminder_calls = calls.slice(remindBefore);
    handlers.toggle({ target: { matches: (sel) => sel === "details[data-fold]", dataset: { fold: "mute-courses" }, open: true } });
    await press("group", { id: "basic" }); out.mute_open = scrim.innerHTML;
    const stayBefore = calls.length;
    out.stay_initial = scrim.innerHTML;
    const stayChange = async (which, checked) => change({ checked, matches: (sel) => sel === "[data-stay], [data-startup]" || sel === `[data-${which}]` });
    await stayChange("stay", true); out.stay_on = scrim.innerHTML;
    await stayChange("stay", false); out.stay_off = scrim.innerHTML;
    if (config.store) { await press("startup-settings"); }
    else {
      await stayChange("startup", true); out.startup_on = scrim.innerHTML;
      await stayChange("startup", false); out.startup_off = scrim.innerHTML;
      startupFail = true; await stayChange("startup", true); out.startup_failed = scrim.innerHTML;
      stay.startup.enabled = true; await press("refind"); out.startup_external = scrim.innerHTML;
    }
    await press("quit-app");
    out.stay_calls = calls.slice(stayBefore); out.stay_toasts = toasts.slice();
  } else if (mode === "todaycard") {
    const el = { innerHTML: "", classList: { toggle() {} } }, view = JSON.parse(fs.readFileSync(process.argv[5], "utf8"));
    const body = cut(src, '  // D-081: week and day', "  async function renderHome() {");
    const ui = { view: "home", info: { today_iso: view.date }, muted_courses: ["합성과목 가"] };
    const made = new Function("esc", "hhmm", "ui", "api", "$", "renderHome", "store", body + "\nreturn { dayList, refreshTodayCard };")(
      esc, (ms) => `${String(Math.floor(ms / 3600000)).padStart(2, '0')}:${String(Math.floor(ms / 60000) % 60).padStart(2, '0')}`,
      ui, { today_briefing: async () => view }, () => el, () => {}, { get: (k, d) => d });
    out.html = made.dayList(view);
    out.empty = made.dayList({ ok: true, date: view.date, classes: [], dues: [], exams: [] });
    out.error = made.dayList({ ok: false });
    ui.view = "live"; el.innerHTML = "untouched"; await made.refreshTodayCard(); out.live = el.innerHTML;
  } else if (mode === "weekcard") {
    const view = JSON.parse(fs.readFileSync(process.argv[5], "utf8")), calls = [], saved = {}, classes = {};
    const el = { innerHTML: "", classList: { toggle: (k, v) => { classes[k] = v; } } };
    const body = cut(src, '  // D-081: week and day', "  async function renderHome() {");
    const store = { get: (k, d) => saved[k] ?? d, set: (k, v) => { saved[k] = v; } };
    const api = { week_view: async (offset) => { calls.push(["week", offset]); return view; },
      today_briefing: async () => { calls.push(["today"]); return { ok: true, date: view.date, classes: [], dues: [], exams: [] }; } };
    const make = (ui) => new Function("esc", "hhmm", "ui", "api", "$", "renderHome", "store", "ic", "openToday", "openCalendar", "renderLive",
      body + "\nreturn { todayCard, refreshTodayCard, todayCardClick, weekGrid, weekColour };")(
        esc, (ms) => `${String(Math.floor(ms / 3600000)).padStart(2, '0')}:${String(Math.floor(ms / 60000) % 60).padStart(2, '0')}`,
        ui, api, () => el, () => {}, store, (name) => `<svg data-i="${name}"></svg>`,
        (name) => calls.push(["course", name]), (...args) => calls.push(["calendar", ...args]), () => {});
    const ui = { view: "home", info: { today_iso: view.date } }, made = make(ui);
    const press = (dataset) => made.todayCardClick({ target: { closest: () => ({ dataset }) } });
    await made.refreshTodayCard(); out.html = el.innerHTML; out.classes = { ...classes };
    out.colour = [made.weekColour("합성과목 가"), made.weekColour("합성과목 가")];
    out.distinct = ["가", "나", "다", "라", "마", "바", "사", "아", "자", "차"].map((x) => made.weekColour("합성과목 " + x));
    out.outside = made.weekGrid({ ...view, now_ms: view.end_ms });
    out.empty = made.weekGrid({ ...view, days: view.days.map((d) => ({ ...d, classes: [], items: [] })) });
    out.error = made.todayCard({ ok: false });
    await press({ course: "합성과목 가" }); await press({ day: "2026-11-28" });
    await press({ weekStep: "-1" }); await press({ weekStep: "1" }); await press({ weekStep: "1" }); await press({ weekStep: "0" });
    out.day = ui.dayData;
    // A late response from a previous week must not replace the next week's data.
    let finish;
    api.week_view = () => new Promise((resolve) => { finish = resolve; });
    const pending = made.refreshTodayCard();
    api.week_view = async () => ({ ...view, start: "2026-12-07", end: "2026-12-13" });
    await press({ weekStep: "1" });
    finish(view); await pending; out.race = el.innerHTML;
    api.week_view = async () => { throw Error("synthetic"); };
    await made.refreshTodayCard(); out.failed_week_day = ui.dayData;
    ui.view = "live"; el.innerHTML = "untouched"; await made.refreshTodayCard(); out.live = el.innerHTML;
    out.calls = calls;
  } else if (mode === "daylive") {
    const view = JSON.parse(fs.readFileSync(process.argv[5], "utf8"));
    const elements = Object.fromEntries(["liveHead", "liveBody", "c-live", "rTitle"].map((id) => [id, {
      innerHTML: "", dataset: {}, classList: { toggle() {}, remove() {} }, scrollHeight: 100, scrollTop: 0, clientHeight: 100
    }]));
    const ui = { view: "home", info: { today_iso: view.date }, dayData: view, state: { phase: "idle" } }, cap = {};
    const helpers = cut(src, '  // D-081: week and day', "  async function renderHome() {");
    const live = cut(src, '  function renderLive() {', '  async function liveAction(');
    const env = { esc, hhmm: (ms) => `${String(Math.floor(ms / 3600000)).padStart(2,'0')}:${String(Math.floor(ms / 60000) % 60).padStart(2,'0')}`,
      ui, cap, $: (id) => elements[id], renderSideLive() {}, sett(el, text) { el.textContent = text; },
      capCourse: () => "합성과목 가", liveReady: () => false, clock: (ms) => String(ms),
      document: { body: { classList: { contains: () => false } } } };
    const names = Object.keys(env);
    const made = new Function(...names, helpers + live + '\nreturn { renderLive, liveOwnsPanel };')(...names.map((n) => env[n]));
    made.renderLive(); out.idle = elements.liveBody.innerHTML; out.idle_head = elements.liveHead.innerHTML;
    for (const phase of ["starting", "recording", "stopping", "finalizing"]) {
      ui.state.phase = phase; made.renderLive(); out[phase] = elements.liveBody.innerHTML;
    }
    ui.state.phase = "idle"; cap.status = { running: true, lines: [{ at: 1, text: "합성 실시간 문장" }] };
    made.renderLive(); out.running = elements.liveBody.innerHTML;
    cap.status.running = false; made.renderLive(); out.retained = elements.liveBody.innerHTML;
    cap.status = {}; made.renderLive(); out.restored = elements.liveBody.innerHTML;
    ui.dayData = { ok: false }; made.renderLive(); out.failed = elements.liveBody.innerHTML;
    ui.state.phase = "recording"; made.renderLive(); out.failed_recording = elements.liveBody.innerHTML;
  } else if (mode === "homemonth") {
    const view = JSON.parse(fs.readFileSync(process.argv[5], "utf8")), calls = [];
    const elements = Object.fromEntries(["home", "homeGrid", "homeTitle", "homeWeek", "homeCandidates"].map((id) => [id, {
      innerHTML: "", style: { setProperty(k,v) { this[k]=v; } }
    }]));
    const ui = { view: "home", info: { today_iso: view.today } };
    const api = { calendar: async (y,m) => { calls.push([y,m]); return view; }, etl_board: async () => ({ok:true}) };
    const helpers = cut(src, "  const isoOf =", "  /* eTL submission state") +
      cut(src, "  const evMissing =", "  function openToday(") + cut(src, "  function weekColour(", "  function weekGrid(");
    const env = { ui, api, esc, $: (id) => elements[id], refreshTodayCard: async () => {}, showExamChip() {}, renderTodo() {},
      dayAttrs: (iso, today, count) => ` role="button" aria-label="${iso}, 일정 ${count}개"` };
    const names = Object.keys(env);
    const made = new Function(...names, helpers + cut(src, "  async function renderHome() {", "  function renderTodo(r) {") + '\nreturn {renderHome};')(...names.map((n) => env[n]));
    for (const [year,month] of [[2026,11],[2027,2],[2026,4]]) {
      ui.home={year,month}; await made.renderHome();
      out[`${year}-${month}`]={html:elements.homeGrid.innerHTML,title:elements.homeTitle.textContent,min:elements.home.style['--month-min']};
    }
    out.candidates = elements.homeCandidates.textContent;
    let finish;
    api.calendar = () => new Promise(resolve => {finish=resolve;}); const stale=made.renderHome();
    api.calendar = async () => view; ui.home={year:2027,month:2}; await made.renderHome();
    finish(view); await stale; out.final_title=elements.homeTitle.textContent;
    api.calendar = async () => ({ok:false,error:'합성 오류'}); await made.renderHome();
    out.error=elements.homeGrid.innerHTML; out.candidates_hidden=elements.homeCandidates.hidden; out.calls=calls;
  } else if (mode === "etlrows") {  // the start screen's 마감·시험 list with eTL submission state (D-077)
    const settingsView = { folds: {} }, el = { innerHTML: "" };
    const helpers = cut(src, "  const evMissing = (e) =>", "  async function renderHome() {");
    const foldSrc = cut(src, "  function fold(key, summary, inner,", "  function foldList(");
    const todoSrc = cut(src, "  function renderTodo(r) {", "  /* eTL on the home list:");
    const env = { esc, settingsView, ic: (n) => `<svg data-i="${n}"></svg>`, mdText: (x) => String(x), dday: (d) => "D-" + d, hhmm: () => "",
      $: () => el, etlTodo: () => "", ui: { info: { today_iso: "2026-11-25", today: [] }, side: null } };
    const names = Object.keys(env);
    const made = new Function(...names, helpers + foldSrc + todoSrc + "\n return { renderTodo, evClass, evLabel, etlAge, missingRow, submittedRow, agoText };")(...names.map((n) => env[n]));
    const view = JSON.parse(fs.readFileSync(process.argv[5], "utf8"));
    view.etl_at = Date.now() / 1000 - 3 * 3600;
    made.renderTodo(view);
    out.todo = el.innerHTML;
    out.cls = view.upcoming.map(made.evClass); out.label = view.upcoming.map(made.evLabel);
    out.age = made.etlAge(view); out.none = made.etlAge({});
    settingsView.folds["todo-submitted"] = true; made.renderTodo(view); out.todo_open = el.innerHTML;
  } else if (mode === "ailink") {  // settings > AI > AI 앱 연결: second switch, [연결]/[연결 해제], Codex button, undo (D-076)
    const body = cut(src, "  const ed =", "  function applyLabs() {") + cut(src, "  function layoutSection(L, mode, pick) {", "  /* ---------- timetable and term editor (TT-001)");
    const opts = JSON.parse(fs.readFileSync(process.argv[5], "utf8"));
    const calls = [], toasts = [], handlers = {}, dialogs = [];
    const scrim = { className: "", innerHTML: "", addEventListener(name, fn) { handlers[name] = fn; }, remove() {}, querySelector: () => null };
    const press = (a, data = {}) => handlers.click({ target: { closest: (sel) => (sel === "button[data-a]" ? { dataset: { a, ...data }, closest: () => null } : null) } });
    const state = { enabled: opts.enabled, write: !!opts.write, auto: Object.assign({}, opts.auto || {}) };
    const KINDS = opts.kinds;  // approve.KINDS, passed in by _node (one row per kind)
    const apps = () => [{ app: "codex", label: "Codex · ChatGPT 데스크톱", state: opts.codex_state || "off", path: "C:\\합성\\.codex\\config.toml" },
      { app: "claude_desktop", label: "Claude 데스크톱", state: opts.claude_state || "off", path: "C:\\합성\\claude_desktop_config.json", message: opts.claude_message || "" }];
    const api = {
      conn_status: async () => ({ ok: true, features: [], keys: [], tools: [], labs: {}, lab_names: {} }),
      recording_permissions: async () => ({ courses: [], states: {} }), appearance: async () => ({ theme: "system", text_size: "normal" }),
      ai_status: async () => ({ choices: {}, settings: {} }), folder_layout_get: async () => ({}), audio_cleanup_get: async () => ({}),
      live_engine_get: async () => null, about: async () => ({ ok: true, version: "0", folders: {}, notices: [] }),
      consent_list: async () => ({ rows: [] }), update_status: async () => ({ possible: false }),
      notices_setting_get: async () => ({ ok: true, on: false }),
      ai_link_status: async () => ({ ok: true, enabled: state.enabled, write: state.write, courses: [{ course: "합성과목", allowed: true }],
        codex: "CODEX SNIPPET", claude_desktop: "CLAUDE SNIPPET", claude_code: "CODE SNIPPET", apps: apps(), codex_found: opts.codex_found !== false,
        auto: KINDS.map(([kind, label]) => ({ kind, label, auto: !!state.auto[kind] })),
        changes: [{ id: "c1", tool: "add_calendar_event", course: "합성과목", summary: "일정 넣음: 합성 시험", at: "2026-11-25T10:00:00", undoable: true },
          { id: "c0", tool: "save_note", course: "합성과목", summary: "정리본 저장", at: "2026-11-24T09:00:00", undoable: false, undone: true }] }),
      ai_link_write: async (on) => { calls.push(["write", on]); state.write = !!on; return { ok: true, enabled: state.enabled, write: state.write }; },
      ai_change_undo: async (id) => { calls.push(["undo", id]); return { ok: true, message: "되돌렸습니다" }; },
      ai_change_undo_since: async (id) => { calls.push(["undo_since", id]); return { ok: true, message: "2건을 되돌렸습니다." }; },
      ai_link_open_codex: async (id) => { calls.push(["codex", id]); return opts.codex_found === false ? { ok: false, error: "Codex(codex.cmd)를 찾지 못했습니다." } : { ok: true }; },
      ai_link_connect_confirm: async (app) => { calls.push(["confirm", app]); return { ok: true, yes: opts.say_yes !== false, token: opts.say_yes !== false ? "TOKEN" : null }; },
      ai_link_connect: async (app, token) => { calls.push(["connect", app, token]); return { ok: true }; },
      ai_link_disconnect: async (app) => { calls.push(["disconnect", app]); return { ok: true }; },
      // D-080: 묻기 / 자동 per kind; 자동 only with the native dialog's one-time token
      ai_link_auto_confirm: async (kind) => { calls.push(["auto_confirm", kind]); return { ok: true, yes: opts.auto_yes !== false, token: opts.auto_yes !== false ? "AUTO-TOKEN" : null }; },
      ai_link_auto: async (kind, on, token) => { calls.push(["auto", kind, on, token || null]); if (on && token !== "AUTO-TOKEN") return { ok: false, error: "확인 창을 거치지 않아" }; if (kind === "*") { for (const [k] of KINDS) state.auto[k] = true; } else state.auto[kind] = !!on; return { ok: true }; },
      ai_link_auto_reset: async () => { calls.push(["auto_reset"]); state.auto = {}; return { ok: true }; },
      // D-083 [사진으로 시간표 넣기]: the native picker + confirmation live in the Api; the page only passes the token on
      timetable_photo_confirm: async () => { calls.push(["photo_confirm"]); return opts.photo_off ? { ok: false, error: "'AI 앱 연결'과 'AI 앱이 고칠 수 있게'를 먼저 켜 주세요." }
        : { ok: true, yes: opts.photo_yes !== false, token: opts.photo_yes !== false ? "PHOTO-TOKEN" : null }; },
      timetable_photo_open: async (token) => { calls.push(["photo_open", token]); return { ok: true }; },
    };
    const env = { document: { createElement: () => scrim, body: { appendChild() {} } }, keepFocus() {}, loadEngines() {}, renderLive() {},
      toast: (m) => toasts.push(m), esc, FEATURE_BADGE: {}, ui: { info: { edition: { name: "full" } } }, api,
      matesFrom: () => [], matesHtml: () => "", matesInput() {}, matesClick() {}, matesText() {}, applyAppearance() {}, applyLabs() {},
      openTimetable: async () => false, askText: async () => "", ic: (n) => `<svg data-i="${n}"></svg>`,
      // only the undo-all dialog is answered yes (integration review: "이 시각 이후 모두 되돌리기")
      dialog: async (o) => { dialogs.push(o && o.title); return !!(o && o.title === "AI가 바꾼 것 모두 되돌리기"); } };
    const names = Object.keys(env);
    const openSettings = new Function(...names, body + "\n return openSettings;")(...names.map((n) => env[n]));
    await openSettings("ai");
    out.html = scrim.innerHTML;
    if (opts.tt_photo) {  // settings > 기본 > 시간표 · 학기: [사진으로 시간표 넣기 (ChatGPT·Codex)]
      await press("group", { id: "basic" });
      out.html_basic = scrim.innerHTML;
      await press("tt-photo");
    } else if (opts.kinds_flow) {  // the per-kind rows: to 자동 (dialog first), back to 묻기, then [모두 다시 묻기]
      const pick = (kind, value) => { const t = { matches: () => false, value, dataset: { ailinkAuto: kind } }; t.closest = (sel) => (sel === "[data-ailink-auto]" ? t : null); return handlers.change({ target: t }); };
      await pick("save_note", "auto");
      out.html_after_auto = scrim.innerHTML;
      await pick("add_calendar_event", "auto");
      await pick("save_note", "ask");
      await press("ailink-auto-all");
      out.html_after_all = scrim.innerHTML;
      await press("ailink-auto-reset");
      out.html_after_reset = scrim.innerHTML;
    } else if (state.enabled) {
      await handlers.change({ target: { matches: (sel) => sel === "[data-ailink-write]", checked: true, closest: () => null, dataset: {} } });
      out.html_after_write = scrim.innerHTML;
      await press("ailink-connect", { id: "codex" });
      await press("ailink-disconnect", { id: "claude_desktop" });
      await press("ailink-codex");
      await press("ai-undo", { id: "c1" });
      await press("ai-undo-since", { id: "c1", at: "2026-11-25 10:00" });
    }
    out.ui_ailink = env.ui.aiLink || null;
    out.calls = calls; out.toasts = toasts; out.dialogs = dialogs;
  } else if (mode === "range") {  // the range card's [끝 지점 조정]: a typed clock time becomes an audio position
    const body = cut(src, "  const wall = (t0, ms) =>", "  /* manual cut of any recording");
    const MIN = 60000, t0 = new Date(2026, 10, 17, 10, 0, 0).getTime();
    const holes = [{ at_ms: 20 * MIN, paused_ms: 8 * MIN }, { at_ms: 50 * MIN, paused_ms: 5 * MIN }];
    const asked = [], cuts = [], toasts = [];
    const env = { ui: { lecture: { id: "l", range: { media_id: "m", suggest: { recorded_start: t0, end_ms: 40 * MIN, duration_ms: 100 * MIN, pauses: holes } } } },
      askText: async (o) => { asked.push(o.value); return "11:12"; }, toast: (m) => toasts.push(m), jumpTo() {}, openLecture() {}, dialog: async () => true,
      api: { range_cut: async (id, a, b) => { cuts.push([a, b]); return { ok: true }; } } };
    const names = Object.keys(env);
    const made = new Function(...names, body + "\n return { rangeAction, audioPosition: typeof audioPosition === 'function' ? audioPosition : null };")(...names.map((n) => env[n]));
    await made.rangeAction({ dataset: { range: "adjust", end: "0" } });
    const walls = JSON.parse(fs.readFileSync(process.argv[5], "utf8"));
    Object.assign(out, { asked, cuts, toasts, positions: made.audioPosition ? walls.map((w) => made.audioPosition(w, holes)) : null, holes });
  } else if (mode === "relink") {
    const relinkText = new Function(cut(src, "  function relinkText(r) {", "  function moveRecording(anchor) {") + "\n return relinkText;")();
    out.texts = JSON.parse(fs.readFileSync(process.argv[5], "utf8")).map(relinkText);
  } else if (mode === "start") {
    const loop = cut(src, "    for (const n of r.notices || []) {", "\n    if (ui.course) {");
    const dialogs = [], toasts = [], opened = [];
    const r = { notices: [{ kind: "mic_blocked", text: "MIC TEXT" }, { kind: "disk_low", text: "DISK TEXT" }, { kind: "later", text: "OTHER" }] };
    new Function("r", "dialog", "toast", "api", loop)(r, (d) => { dialogs.push(d); return Promise.resolve(d.ok === "Windows 설정 열기"); },
      (m) => toasts.push(m), { open_mic_settings: () => opened.push(1) });
    await new Promise((resolve) => setImmediate(resolve));
    Object.assign(out, { dialogs, toasts, opened: opened.length });
    // the dialog itself: with cancel "" there is one button
    const made = [];
    const scrim = () => { const s = { className: "", innerHTML: "", addEventListener() {}, remove() {}, querySelector: () => ({ focus() {} }), querySelectorAll: () => [] }; made.push(s); return s; };
    const dialog = new Function("document", "esc", "keepFocus", "isTop", cut(src, "  function dialog({", "  /* keyboard for the big dialogs") + "\n return dialog;")(
      { createElement: scrim, body: { appendChild() {} }, addEventListener() {}, removeEventListener() {} }, esc, () => {}, () => true);
    dialog({ title: "t", body: "b", ok: "확인", cancel: "" }); dialog({ title: "t", body: "b" });
    out.buttons = made.map((s) => (s.innerHTML.match(/<button/g) || []).length);
  } else if (mode === "setup") {  // D-086: the first-run wizard with its Codex step and the timetable step's ways
    const opts = JSON.parse(fs.readFileSync(process.argv[5], "utf8"));
    const body = cut(src, "  /* ---------- first-run setup wizard", "  function startFailed(err) {");
    const calls = [], handlers = {}, timers = [];
    const fake = { connected: false, enabled: false, write: false, table: { ok: true, courses: [], term: {}, base: "B0" } };
    let dom = { courses: [], start: "", end: "", term: "" };
    const parse = (html) => {  // what the inputs drawn last hold, as a browser would keep them
      const val = (re, s) => { const m = re.exec(s); return m ? m[1].replace(/&quot;/g, '"').replace(/&amp;/g, "&") : ""; };
      const courses = html.split('<div class="crs"').slice(1).map((part) => ({
        name: val(/class="ask cname" placeholder="과목 이름" value="([^"]*)"/, part),
        slots: part.split('<span class="slot"').slice(1).map((s) => ({ day: val(/<option selected>([^<]*)<\/option>/, s) || "월",
          start: val(/class="ask short sstart" value="([^"]*)"/, s), end: val(/class="ask short send" value="([^"]*)"/, s) })) }));
      return { courses, start: val(/id="su-start" class="ask" value="([^"]*)"/, html), end: val(/id="su-end" class="ask" value="([^"]*)"/, html),
        term: val(/id="su-term" class="ask short" value="([^"]*)"/, html) };
    };
    let html = "";
    const input = (get, set) => ({ get value() { return get(); }, set value(v) { set(v); } });
    const scrim = { className: "", addEventListener(name, fn) { handlers[name] = fn; }, remove() {},
      get innerHTML() { return html; }, set innerHTML(v) { html = v; dom = parse(v); },
      querySelector(sel) {
        if (sel === "#su-term") return input(() => dom.term, (v) => { dom.term = v; });
        if (sel === "#su-start") return input(() => dom.start, (v) => { dom.start = v; });
        if (sel === "#su-end") return input(() => dom.end, (v) => { dom.end = v; });
        return { disabled: false, focus() {} };
      },
      querySelectorAll(sel) {
        if (sel === "[data-note]") return [1, 2, 3, 4, 5].map(() => ({ checked: true }));
        if (sel !== ".crs") return [];
        return dom.courses.map((c) => ({ querySelector: () => ({ value: c.name }),
          querySelectorAll: () => c.slots.map((s) => ({ querySelector: (q) => ({ value: q === ".sday" ? s.day : q === ".sstart" ? s.start : s.end }) })) }));
      } };
    const api = {
      setup_info: async () => ({ ok: true, parent: "C:\\합성\\문서", parent_notes: [], term: "26-2", tools: {}, needs_setup: true, prepared: opts.prepared || null }),
      folder_layout_get: async () => ({ ok: true, style: "basic" }),
      setup_prepare: async (...a) => { calls.push(["prepare", ...a]); return { ok: true, root: "C:\\합성\\문서\\26-2", courses: [], term: {}, base: fake.table.base }; },
      setup_codex_state: async (login) => { calls.push(["state", !!login]); return { ok: true, folder: true,
        tool: { label: "Codex (ChatGPT 계정)", found: opts.found !== false, signed_in: login ? (opts.signed_in === undefined ? true : opts.signed_in) : null, hint: "npm install -g @openai/codex", help_url: "https://developers.openai.com/codex/cli" },
        config: { state: fake.connected ? "connected" : "off" }, enabled: fake.enabled, write: fake.write }; },
      ai_link_setup_confirm: async () => { calls.push(["setup_confirm"]); return { ok: true, yes: opts.say_yes !== false, token: opts.say_yes !== false ? "SETUP-TOKEN" : null }; },
      ai_link_setup_connect: async (token) => { calls.push(["setup_connect", token]); if (token !== "SETUP-TOKEN") return { ok: false, error: "확인 창을 거치지 않아" };
        fake.connected = fake.enabled = fake.write = true; return { ok: true }; },
      setup_timetable: async () => { calls.push(["timetable"]); return { ...fake.table }; },
      timetable_photo_confirm: async () => { calls.push(["photo_confirm"]); return { ok: true, yes: true, token: "PHOTO-TOKEN" }; },
      timetable_photo_open: async (token) => { calls.push(["photo_open", token]); return { ok: true }; },
      setup_codex_open: async () => { calls.push(["codex_first"]); return { ok: true }; },
      conn_open_help: async (id) => { calls.push(["help", id]); return { ok: true }; },
      setup_apply: async (...a) => { calls.push(["apply", ...a]);
        if (opts.change_before_save && a[6] !== "B2") { fake.table = { ok: true, courses: [{ name: "합성과목 마", folder: "합성과목 마", slots: [{ day: "금", start: "11:00", end: "12:00" }] }], term: {}, base: "B2" }; }
        return a[6] === fake.table.base ? { ok: true, extra: [], skipped: [] } : { ok: false, changed: true, error: "그 사이 시간표가 바뀌었습니다", ...fake.table }; },
      setup_close: async () => ({ ok: true }),
    };
    const tick = async () => { for (let k = 0; k < 6; k++) await new Promise((r) => setImmediate(r)); };
    const press = async (act, data = {}) => { await handlers.click({ target: { closest: () => ({ dataset: { act, ...data } }) } }); await tick(); };
    const env = { document: { createElement: () => scrim, body: { appendChild() {} } }, keepFocus() {}, api, esc,
      ic: (n) => `<svg data-i="${n}"></svg>`, layoutSection: () => "<div>폴더 구성 미리보기</div>",
      setInterval: (fn) => { timers.push(fn); return timers.length; }, clearInterval: (n) => { timers[n - 1] = null; } };
    const names = Object.keys(env);
    const runSetup = new Function(...names, body + "\n return runSetup;")(...names.map((n) => env[n]));
    const head = () => (html.match(/<h4>([^<]*)<\/h4>/) || [])[1];
    const nextLabel = () => (html.match(/data-act="next"[^>]*>([^<]*)</) || [])[1];
    runSetup(); await tick();
    out.first = head();
    await press("next"); out.folder = head();
    if (!opts.prepared) { await press("next"); out.layout = head(); await press("next"); }
    out.codex_head = head(); out.codex = html; out.codex_next = nextLabel();
    out.codex_back = /data-act="back"/.test(html);
    if (opts.connect) { await press("codex-connect"); out.codex_after = html; out.codex_after_next = nextLabel(); }
    if (opts.help) await press("codex-help");
    await press("next");
    out.tt_head = head(); out.tt = html;
    if (opts.type) { dom.courses = [{ name: "합성과목 다", slots: [{ day: "화", start: "10:00", end: "11:15" }] }]; dom.start = "2026-09-01"; dom.end = "2026-12-20"; }
    if (opts.photo) { await press("tt-photo-setup"); out.waiting = html; }
    if (opts.first) { await press("codex-first"); out.waiting_first = html; }
    if (opts.arrive) {
      fake.table = { ok: true, courses: [{ name: "합성과목 가", folder: "합성과목 가", slots: [{ day: "월", start: "09:00", end: "10:15" }] },
        { name: "합성과목 나", folder: "합성과목 나", slots: [{ day: "목", start: "13:00", end: "14:15" }] }], term: { start: "2026-09-02", end: "2026-12-19" }, base: "B1" };
      const live = timers.filter(Boolean); out.watchers = live.length;
      for (const fn of live) await fn();
      await tick(); out.arrived = html;
      for (const fn of timers.filter(Boolean)) await fn();  // nothing new: no redraw needed, nothing lost
      out.arrived_again = html === out.arrived;
    }
    await press("next"); out.tools_head = head();
    out.watchers_after = timers.filter(Boolean).length;
    await press("next");
    out.after_save = html; out.after_head = head();
    if (opts.change_before_save) { out.back_to = head(); await press("next"); await press("next"); out.saved_second = head(); }
    out.calls = calls;
  }
  console.log(JSON.stringify(out));
})().catch((err) => { console.error(err && err.stack || err); process.exit(1); });
"""


def _node(tmp_path: Path, mode: str, data=None) -> dict:
    script = tmp_path / "harness.js"
    script.write_text(HARNESS, encoding="utf-8")
    extra = []
    if mode == "ailink":  # the kinds rows come from the server's own list (D-080, D-083)
        from lecturehub.mcp.approve import KINDS as kinds

        data = {"kinds": [[k, v] for k, v in kinds.items()], **(data or {})}
    if data is not None:
        (tmp_path / "data.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        extra = [str(tmp_path / "data.json")]
    done = subprocess.run(["node", str(script), mode, str(WEB / "app.js"), str(WEB / "mini.html"), *extra],
                          capture_output=True, text=True, encoding="utf-8", timeout=120, check=False)
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


REC = {"device": "Mic", "mode": "mic", "rms": -30}


@needs_node
def test_low_disk_is_said_in_front_of_the_sound_line_not_instead_of_it(tmp_path):
    low = {"low": True, "minutes": 69}
    cases = {
        "paused": {"phase": "recording", "disk": low, "status": {**REC, "paused": True}},
        "pc_silent": {"phase": "recording", "disk": low, "status": {**REC, "mode": "loopback", "rms": None}},
        "quiet": {"phase": "recording", "disk": low, "status": {**REC, "quiet": True}},
        "dropped": {"phase": "recording", "disk": low, "status": {**REC, "dropped_samples": 4800}},
        "normal": {"phase": "recording", "disk": low, "status": REC},
        "stalled": {"phase": "recording", "disk": low, "status": {**REC, "stalled": True}},
        "enough": {"phase": "recording", "disk": {"low": False, "minutes": 400}, "status": REC},
        "unknown_minutes": {"phase": "recording", "disk": {"low": True, "minutes": None}, "status": REC},
        "nothing_left": {"phase": "recording", "disk": {"low": True, "minutes": 0}, "status": REC},
    }
    shown = _node(tmp_path, "health", cases)
    for name, words in (("paused", "일시정지 중"), ("pc_silent", "PC에서 소리가 나지 않는 중"), ("quiet", "소리 매우 작음"),
                        ("dropped", "캡처 누락 4800"), ("normal", "소리 정상")):
        assert "끝까지 저장할 수 있는 녹음은 약 69분 남았습니다" in shown[name] and words in shown[name], name
        assert shown[name].index("디스크 공간 부족") < shown[name].index(words), name
    assert shown["stalled"].index("소리가 들어오지 않습니다") < shown["stalled"].index("디스크 공간 부족")  # the stall stays first
    assert "디스크" not in shown["enough"]
    assert "디스크 공간 부족 — 파일을 지워 주세요" in shown["unknown_minutes"] and "약" not in shown["unknown_minutes"]
    assert "지금까지 녹음한 것도 끝까지 저장할 공간이 모자랍니다" in shown["nothing_left"]
    assert "멈출 수 있어요" not in "".join(shown.values())  # HEAD's sentence: "약 N분 뒤 녹음이 멈출 수 있어요"


@needs_node
def test_a_stall_of_the_second_engine_is_on_the_status_line(tmp_path):
    cases = {
        "second": {"phase": "recording", "status": REC, "pc": {"state": "recording", "mode": "loopback", "status": {"stalled": True, "rms": None}}},
        "second_mic": {"phase": "recording", "status": {**REC, "mode": "loopback"}, "pc": {"state": "recording", "mode": "mic", "status": {"stalled": True}}},
        "main_of_two": {"phase": "recording", "status": {**REC, "stalled": True}, "pc": {"state": "recording", "mode": "loopback", "status": {}}},
        "alone": {"phase": "recording", "status": {**REC, "stalled": True}},
    }
    shown = _node(tmp_path, "health", cases)
    assert "PC 소리가 들어오지 않습니다" in shown["second"] and "소리 정상" in shown["second"]  # the mic's own line stays
    assert "마이크 소리가 들어오지 않습니다" in shown["second_mic"]
    assert "마이크 소리가 들어오지 않습니다" in shown["main_of_two"]
    assert "<i class=\"dot rec\"></i>소리가 들어오지 않습니다" in shown["alone"]


@needs_node
def test_summary_words_an_open_gap_as_no_sound_until_the_end(tmp_path):
    base = {"total_ms": 1, "dropped_samples": 0, "session_dir": "s"}
    cases = {
        "middle": {"phase": "done", "summary": {**base, "partial": True, "gaps": 1, "lost_ms": 42000, "tail_lost_ms": None}},
        "tail_stopped": {"phase": "done", "summary": {**base, "partial": True, "cause": "user_stop", "gaps": 1,
                                                      "lost_ms": 125000, "tail_lost_ms": 125000}},
        "tail_gave_up": {"phase": "done", "summary": {**base, "partial": False, "cause": "source_lost", "gaps": 2,
                                                      "lost_ms": 642000, "tail_lost_ms": 600000}},
        "source_lost_only": {"phase": "done", "summary": {**base, "partial": False, "cause": "source_lost", "gaps": 0,
                                                          "lost_ms": 0, "tail_lost_ms": None}},
        "clean": {"phase": "done", "summary": {**base, "partial": False, "cause": "user_stop", "gaps": 0, "lost_ms": 0,
                                               "tail_lost_ms": None}},
        "second_lost": {"phase": "done", "summary": {**base, "partial": False, "gaps": 0, "lost_ms": 0, "tail_lost_ms": None},
                        "pc": {"state": "done", "mode": "mic", "cause": "source_lost", "gaps": 1, "lost_ms": 600000,
                               "tail_lost_ms": 600000}},
        "second_gaps": {"phase": "done", "summary": {**base, "partial": False, "gaps": 0, "lost_ms": 0, "tail_lost_ms": None},
                        "pc": {"state": "done", "mode": "loopback", "gaps": 2, "lost_ms": 65000, "tail_lost_ms": None}},
    }
    shown = _node(tmp_path, "health", cases)
    warned = '<span class="warn"><i class="dot warn"></i>저장됨'
    assert "중간에 끊김 1번(42초 빠짐)" in shown["middle"] and warned in shown["middle"]
    assert "끝날 때까지 소리가 돌아오지 않음(마지막 2분 5초 빠짐)" in shown["tail_stopped"]
    assert "중간에 끊김" not in shown["tail_stopped"]  # the open gap is not an interruption in the middle
    assert "중간에 끊김 1번(42초 빠짐)" in shown["tail_gave_up"]
    assert "장치가 돌아오지 않아 여기까지 저장(마지막 10분 빠짐)" in shown["tail_gave_up"] and warned in shown["tail_gave_up"]
    assert warned in shown["source_lost_only"]  # the cause alone is enough for the warning dot
    assert '<span class=""><i class="dot ok"></i>저장됨' in shown["clean"] and "끊김" not in shown["clean"]
    assert "마이크 따로 저장: 장치가 돌아오지 않아 여기까지 저장(마지막 10분 빠짐)" in shown["second_lost"]
    assert warned in shown["second_lost"]
    assert "PC 소리 따로 저장: 중간에 끊김 2번(1분 5초 빠짐)" in shown["second_gaps"] and warned in shown["second_gaps"]


@needs_node
def test_summary_says_once_when_the_cleanup_left_the_wav_files(tmp_path):
    base = {"total_ms": 1, "dropped_samples": 0, "session_dir": "s", "gaps": 0, "lost_ms": 0, "tail_lost_ms": None}
    note = {"state": "skipped", "reason": "세그먼트도 FLAC도 없어 병합 WAV를 남김"}
    gone = {"state": "compacted", "deleted": 3}
    kept = {"state": "partial", "deleted": 1, "reason": "휴지통이 없는 드라이브에서 WAV 파일을 지우지 못해 남겨 두었습니다: a.wav"}
    cases = {
        "main": {"phase": "done", "summary": {**base, "cleanup": note}},
        "both": {"phase": "done", "summary": {**base, "cleanup": note}, "pc": {"state": "done", "cleanup": note}},
        "second": {"phase": "done", "summary": base, "pc": {"state": "done", "cleanup": {"state": "error", "error": "Traceback"}}},
        "none": {"phase": "done", "summary": base},
        "permanent": {"phase": "done", "summary": {**base, "cleanup": gone}},
        "permanent_both": {"phase": "done", "summary": {**base, "cleanup": gone}, "pc": {"state": "done", "cleanup": kept}},
        "markup": {"phase": "done", "summary": {**base, "cleanup": {"state": "skipped", "reason": "<img src=x>"}}},
    }
    shown = _node(tmp_path, "health", cases)
    words = "WAV 정리를 다 하지 못했습니다"
    assert shown["main"].count(words) == 1 and "세그먼트도 FLAC도 없어 병합 WAV를 남김" in shown["main"]
    assert shown["both"].count(words) == 1  # once, also when both files were left
    assert shown["second"].count("WAV 정리 중 오류") == 1 and ">Traceback" not in shown["second"]  # error text: hint only
    assert "WAV" not in shown["none"]
    deleted = "휴지통이 없는 드라이브라 WAV 사본 {}개를 바로 지웠습니다"
    assert shown["permanent"].count(deleted.format(3)) == 1 and words not in shown["permanent"]
    assert shown["permanent_both"].count(deleted.format(4)) == 1  # both files, said once
    assert shown["permanent_both"].count(words) == 1 and "a.wav" in shown["permanent_both"]
    assert "<img" not in shown["markup"] and "&lt;img" in shown["markup"]  # the reason is escaped


@needs_node
def test_both_windows_word_the_lost_sound_the_same(tmp_path):
    samples = [{"gaps": 1, "lost_ms": 42000, "tail_lost_ms": None}, {"gaps": 1, "lost_ms": 500, "tail_lost_ms": 500, "cause": "user_stop"},
               {"gaps": 3, "lost_ms": 3725000, "tail_lost_ms": 3600000, "cause": "source_lost"}, {"cause": "source_lost"}, {}]
    both = _node(tmp_path, "gaps", samples)
    assert both["app"] == both["mini"]
    assert both["app"][1] == " · 끝날 때까지 소리가 돌아오지 않음(마지막 1초 빠짐)"  # floor, at least one second
    assert both["app"][2] == " · 중간에 끊김 2번(2분 5초 빠짐) · 장치가 돌아오지 않아 여기까지 저장(마지막 60분 빠짐)"
    assert both["app"][4] == ""
    assert both["disk_app"] == both["disk_mini"] and "약 12분" in both["disk_app"][5]


@needs_node
def test_mini_window_shows_the_stall_low_disk_and_what_was_lost(tmp_path):
    seen = _node(tmp_path, "mini")
    assert seen["normal"]["note"] is None and seen["normal"]["what"] == "구조역학 · 녹음 중" and not seen["normal"]["alarm"]
    stalled = seen["stalled"]
    assert stalled["note"] == "소리가 들어오지 않습니다 — 장치를 다시 여는 중" and stalled["a"] is None and stalled["b"] is None
    assert stalled["what"] == "소리 끊김" and stalled["alarm"] and stalled["bar"]
    assert seen["second_stalled"]["note"] == "PC 소리가 들어오지 않습니다 — 장치를 다시 여는 중"
    assert seen["disk"]["note"] == "디스크 부족 · 끝까지 저장 가능 약 12분"
    assert seen["disk"]["title"] == "디스크 공간 부족 — 끝까지 저장할 수 있는 녹음은 약 12분 남았습니다. 파일을 지워 주세요"
    assert seen["disk"]["what"] == "디스크 공간 부족" and seen["disk"]["alarm"]
    # a question with buttons keeps its card; low disk stays on the foot line. A stall goes before the question.
    assert seen["disk_and_over"]["a"] == "멈추기" and seen["disk_and_over"]["what"] == "디스크 공간 부족"
    assert "들어오지 않습니다" in seen["stall_and_over"]["note"] and seen["stall_and_over"]["a"] is None
    assert seen["lost_tail"]["note"] == "저장됨 · 장치가 돌아오지 않아 여기까지 저장(마지막 10분 빠짐)"
    assert seen["lost_middle_second"]["note"] == "저장됨 · PC 소리 쪽 중간에 끊김 2번(1분 5초 빠짐)"
    assert seen["clean"]["note"] is None and seen["clean"]["what"] == "저장됨 · 누락 0"


@needs_node
def test_mini_window_shows_what_start_said_until_it_is_closed(tmp_path):
    seen = _node(tmp_path, "mini")
    assert seen["notice_before_running"]["note"] is None  # the old recording's summary is still the last state read
    mic = seen["notice_mic"]
    assert mic["note"] == "Windows에서 마이크가 꺼져 있어요" and mic["title"] == "MIC TEXT"
    assert (mic["a"], mic["b"]) == ("설정 열기", "닫기")
    assert seen["calls"] == ["start_selected", "open_mic_settings"]
    disk = seen["notice_disk"]
    assert disk["note"] == "디스크 공간이 얼마 남지 않았어요" and disk["title"] == "DISK TEXT" and (disk["a"], disk["b"]) == (None, "닫기")
    assert seen["notice_closed"]["note"] is None and seen["notice_closed"]["level"]
    assert seen["settings_failed"]["note"].startswith("열지 못했습니다") and "MIC TEXT" in seen["settings_failed"]["note"]
    assert seen["refused"]["what"] == "녹음 허락이 필요합니다"  # still there after the next state read


@needs_node
def test_notice_switch_follows_the_saved_value_through_redraws(tmp_path):
    seen = _node(tmp_path, "settings")
    assert seen["on"] == {**seen["on"], "saved": True, "box": True, "redrawn": True}
    assert seen["off"] == {**seen["off"], "saved": False, "box": False, "redrawn": False}
    for name, saved in (("fails", True), ("throws", False)):  # nothing was saved: the box goes back and stays back
        assert (seen[name]["saved"], seen[name]["box"], seen[name]["redrawn"]) == (saved, saved, saved), name
    assert seen["fails"]["toasts"] == ["설정 파일을 쓰지 못했습니다"] and seen["throws"]["toasts"] == ["설정을 저장하지 못했습니다"]
    assert seen["redraw"]["saved"] is True and seen["redraw"]["box"] is True and seen["redraw"]["redrawn"] is True


@needs_node
def test_notice_switch_text_says_what_is_sent_and_how_often(tmp_path):
    """The words follow index/class_notices.py: the consented course's transcript with the timetable and calendar
    titles from 7 days before the lecture (entries without a course too); once per change, repeated with growing
    waits up to MAX_SENDS requests in all; the month's figure counts every request."""
    from lecturehub.index import class_notices

    seen = _node(tmp_path, "settings")
    text = seen["on"]["text"]
    for words in ("Google 전송에 동의한 과목의 전사 글", "과목 이름·수업 날짜", "그 과목 시간표",
                  "수업 7일 전부터의 달력 일정 제목·시간표 변경", "과목 없는 것 포함", "전사가 바뀔 때 한 번",
                  "서버가 오류로 답하면 곧바로 다른 모델로", "실패 포함", "3회, 약 4천 자"):
        assert words in text, words
    # the numbers on screen are the engine's own
    waits = "·".join(f"{ms // 3_600_000}시간" for ms in class_notices.RETRY_AFTER_MS)
    assert f"그 밖에는 {waits} 뒤). 같은 전사는 {class_notices.MAX_SENDS}번까지만" in text
    assert text.count(". ") + text.count(".<") <= 4  # a few short sentences
    # off: a request already on its way still finishes (class_notices.scan checks the switch before each send)
    assert seen["off"]["toasts"] == ["수업 중 공지 찾기를 껐습니다 · 이미 보내는 중인 것 말고는 더 보내지 않습니다"]


@needs_node
def test_a_typed_class_end_time_is_turned_into_an_audio_position(tmp_path):
    """Review item "Wall-clock times in the cut dialog ... ignore gaps": the clock time typed in [끝 지점 조정] is a
    wall-clock moment; the pauses and gaps before it are not in the audio, so they are taken out (HEAD cut at the
    wall-clock offset itself, 13 minutes too late here), and the value offered is the clock time of the suggested end."""
    from lecturehub.index import media_range

    minute = 60_000
    walls = [w * minute for w in (10, 20, 24, 28, 40, 60, 100)] + [0, 1, 20 * minute + 1]
    seen = _node(tmp_path, "range", walls)
    assert seen["cuts"] == [[0, 59 * minute]]  # typed 11:12 = wall 72:00 = audio 72 - 8 - 5 = 59:00
    assert seen["asked"] == ["10:48"]  # the suggested end (audio 40:00) is wall 48:00, after the 8-minute gap
    assert seen["toasts"] == ["11:12까지로 잘랐습니다 · 원본은 그대로 보관"]
    assert seen["positions"] == [media_range.audio_position(w, seen["holes"]) for w in walls]  # same as the engine


@needs_node
def test_moving_a_recording_says_when_the_old_copy_stayed(tmp_path):
    texts = _node(tmp_path, "relink", [
        {"transcripts": 0, "recycled_copy": "C:/x.mp3"},
        {"transcripts": 2, "copy_left": "E:/old/x.mp3", "copy_note": "E:\\ 드라이브에는 휴지통이 없음"},
        {"transcripts": 0, "copy_left": "E:/old/x.mp3", "copy_note": None},
        {"transcripts": 0, "copy_note": "휴지통으로 보내지 못했습니다"},
        {"transcripts": 0}])["texts"]
    assert texts[0] == "옮겼습니다 · 잘못된 녹음본 복사본은 휴지통으로"
    assert texts[1] == "옮겼습니다 · 전사 2개 포함 · 옛 녹음본 복사본은 그대로 두었습니다(E:\\ 드라이브에는 휴지통이 없음)"
    assert texts[2] == "옮겼습니다 · 옛 녹음본 복사본은 그대로 두었습니다"
    assert texts[3] == "옮겼습니다 · 휴지통으로 보내지 못했습니다"
    assert texts[4] == "옮겼습니다"


@needs_node
def test_start_notices_stay_in_a_dialog_that_nothing_waits_for(tmp_path):
    seen = _node(tmp_path, "start")
    assert [d["body"] for d in seen["dialogs"]] == ["MIC TEXT", "DISK TEXT", "OTHER"] and seen["toasts"] == []
    assert seen["dialogs"][1]["title"] == "디스크 공간이 얼마 남지 않았어요" and seen["dialogs"][1]["cancel"] == ""
    assert seen["opened"] == 1  # [Windows 설정 열기] of the microphone notice
    assert seen["buttons"] == [1, 2]  # cancel "": one button; the usual dialog keeps two


# ---------------------------------------------------------------- the settings menu (user 2026-11-25)
SECTION_TITLES = ["화면", "시간표 · 학기", "폴더 구성", "알림 · 업데이트", "과목별 녹음 허락", "녹음 자동 정리", "실시간 전사 엔진",
                  "AI 엔진", "수업 중 공지 찾기", "AI 앱 연결", "계정 연결", "프로그램", "기능 상태", "외부 전송 동의",
                  "실험실 (선택 기능)", "앱 정보 · 문제 보고"]
if (WEB.parents[1] / "booking").is_dir():
    SECTION_TITLES.append("그스룸")


@needs_node
def test_UI_STAY_001_both_settings_rows_save_redraw_and_read_back_actual_state(tmp_path):
    seen = _node(tmp_path, "groups")
    initial = seen["stay_initial"]
    assert "창을 닫아도 알림 받기" in initial and "Windows를 켤 때 Lecture Hub도 켜기" in initial
    assert 'data-stay>' in initial and 'data-startup>' in initial and "아직 고르지 않았습니다" in initial
    assert "창을 숨긴 동안 포함" in initial and "아래 두 설정" in initial and "절전" in initial
    assert 'data-stay checked' in seen["stay_on"] and 'data-stay>' in seen["stay_off"]
    assert 'data-startup checked' in seen["startup_on"]
    assert 'data-startup>' in seen["startup_off"] and 'data-startup>' in seen["startup_failed"]
    assert 'data-startup checked' in seen["startup_external"]
    assert "합성 저장 실패" in seen["stay_toasts"]
    assert seen["stay_calls"] == [["stay", True], ["stay", False], ["startup", True],
                                  ["startup", False], ["startup", True], ["quit-app"]]


@needs_node
def test_UI_STAY_002_store_has_explanation_and_opens_windows_page_without_a_fake_toggle(tmp_path):
    seen = _node(tmp_path, "groups", {"store": True})
    initial = seen["stay_initial"]
    assert 'data-stay>' in initial and 'data-startup' not in initial
    assert 'data-a="startup-settings"' in initial and "Store판은 Windows 설정" in initial
    assert "현재 상태도 그곳에서 확인" in initial
    assert seen["stay_calls"] == [["stay", True], ["stay", False], ["startup-settings"], ["quit-app"]]


@needs_node
def test_every_settings_section_is_in_exactly_one_menu_group(tmp_path):
    """Nothing of the one long page disappeared: each section title the screen had is drawn in exactly one group
    (full build, 그스룸 on, so every optional section is drawn), and the menu marks only the group shown."""
    seen = _node(tmp_path, "groups")
    ids = [s for g in seen["groups"] for s in g["sections"]]
    assert len(ids) == len(set(ids)) == len(SECTION_TITLES)
    assert [g["label"] for g in seen["groups"]] == ["기본", "녹음·전사", "AI", "연결", "동의", "선택 기능", "앱 정보"]
    for title in SECTION_TITLES:
        where = [g for g, titles in seen["titles"].items() if title in titles]
        assert len(where) == 1, (title, where)
    assert sum(len(t) for t in seen["titles"].values()) == len(SECTION_TITLES)
    assert all(seen["current"][g] == [g] for g in seen["titles"])  # aria-current on the shown group only
    assert seen["first"] == "basic"
    assert "앱 정보" in seen["html_basic"]  # the self-check's settings walk waits for this text (menu label)


@needs_node
def test_settings_group_survives_redraws_and_the_next_opening(tmp_path):
    seen = _node(tmp_path, "groups")
    assert seen["after_reload"] == "record" and seen["reopened"] == "record"
    assert seen["pointed"] == "ai" and seen["unknown_group"] == "ai"  # an unknown name keeps the last group


@needs_node
def test_long_lists_fold_with_a_count_and_keep_their_state(tmp_path):
    seen = _node(tmp_path, "groups")
    assert seen["fold_before"] == ['data-fold="permission"', None]  # 10 courses: folded at first
    assert seen["fold_after"] == ['data-fold="permission" open', " open"]  # opened, then a reload: still open
    assert "하나씩 고르기" in seen["html_record"] and "10개" in seen["html_record"]
    ai = seen["html_ai"]
    # the [연결] rows stay open; every paste-in snippet folds together (D-076); what is handed over stays in view
    assert ai.index("AI 앱에 연결") < ai.index('data-fold="ailink-snips"') < ai.index("ChatGPT 데스크톱 · Codex")
    assert "건네는 것: 고른 과목의 전사 글" in ai and "허용이 필요한 과목: 합성과목 1" in ai
    assert ai.index("허용이 필요한 과목") < ai.index('data-fold="notices-courses"')
    assert 'data-fold="consents"' in seen["html_consent"] and "10건" in seen["html_consent"]
    assert 'data-fold="tools-optional"' in seen["html_connect"]  # Google Cloud CLI (optional, missing)
    connect = seen["html_connect"]
    assert connect.index("Codex") < connect.index('data-fold="tools-optional"')  # the default AI's program stays out


@needs_node
def test_short_lists_stay_open(tmp_path):
    seen = _node(tmp_path, "groups", {"courses": 3})
    assert 'data-fold="permission"' not in seen["html_record"] and 'data-permission="합성과목 3"' in seen["html_record"]
    assert 'data-fold="ailink-courses"' not in seen["html_ai"] and 'data-fold="notices-courses"' not in seen["html_ai"]


@needs_node
def test_every_per_course_list_has_an_all_at_once_control(tmp_path):
    """Recording permission: one native dialog only for 허락함 / 강의계획서 허용 (the user states every lecturer allowed
    it); 금지 needs none. The AI link and class-notice lists keep [모두 선택]/[모두 해제] through the shared helper."""
    seen = _node(tmp_path, "groups")
    assert 'data-pick-state="permission"' in seen["html_record"]
    for list_ in ("ailink", "notices"):
        assert f'data-id="{list_}" data-to="on"' in seen["html_ai"] and f'data-id="{list_}" data-to="off"' in seen["html_ai"]
    assert seen["pick_kept"] is True  # the chosen state survives a redraw
    assert seen["calls"] == [["confirm_many", "allowed", 10], ["all", "allowed", 10, "T"], ["all", "forbidden", 10, None],
                             ["consent_many", "ai_app_link", 10], ["link_all", 10, "C"]]
    assert seen["states_after_allowed"] == ["allowed"] and seen["states_after_forbidden"] == ["forbidden"]
    assert seen["toasts"][0] == "모든 과목에 줄 상태를 먼저 고르세요"
    assert "이미 모든 과목이 '금지'입니다" in seen["toasts"] and seen["toasts"][-1] == "고른 과목이 없습니다"


@needs_node
def test_UI_ETL_001_notification_switches_and_the_etl_notice_consent_list(tmp_path):
    """D-077: settings › 기본 › 알림 has one switch per kind of eTL change (as saved); settings › AI has the eTL
    announcement list with its own consent (separate provider), [모두 선택]/[모두 해제] through the shared helper, a
    native dialog per course, and the courses that still need consent."""
    seen = _node(tmp_path, "groups")
    basic, ai = seen["html_basic"], seen["html_ai"]
    for kind in ("assignment_new", "due_changed"):
        assert f'data-etl-notify="{kind}" checked' in basic
    assert 'data-etl-notify="files_new">' in basic  # saved as off: drawn off
    assert "eTL에 새 강의자료 알리기" in basic and "AI를 쓰지 않고" in basic
    assert 'data-id="etlnotices" data-to="on"' in ai and 'data-id="etlnotices" data-to="off"' in ai
    assert 'data-etlnotices-course="합성과목 2" checked' in ai and 'data-etlnotices-course="합성과목 1">' in ai
    assert "eTL 공지 허용이 필요한 과목: 합성과목 3" in ai
    assert ai.index("ChatGPT(Codex)로 보낼 과목") < ai.index("eTL 공지도 ChatGPT(Codex)로 보낼 과목")
    assert "첨부 파일, 고르지 않은 과목, 소리는 보내지 않습니다" in ai
    assert seen["etl_calls"] == [
        ["consent_many", "codex_etl_notices", 9], ["etl_all", 9, "C"],  # one was allowed already
        ["consent_one", "codex_etl_notices", "합성과목 1"], ["etl_one", "합성과목 1", "ONE"],
        ["revoke_one", "codex_etl_notices", "합성과목 2"],
        ["notify", "files_new", True]]
    assert seen["etl_toasts"][-1] == "LABEL: 알려 줍니다"


@needs_node
def test_UI_ETL_002_start_screen_shows_missing_in_red_and_folds_handed_in_work(tmp_path):
    view = {"events": [], "exceptions": [], "candidates": [],
            "upcoming": [{"id": 1, "kind": "assignment", "kind_label": "과제", "title": "합성 과제 A", "course": "합성과목",
                          "date": "2026-11-27", "d_day": 2, "start_ms": None, "source": "etl",
                          "submission": {"state": "missing", "label": "누락", "done": False, "late": False}},
                         {"id": 2, "kind": "exam", "kind_label": "시험", "title": "합성 시험", "course": "합성과목",
                          "date": "2026-12-01", "d_day": 6, "start_ms": None, "source": "user"}],
            "submitted": [{"id": 3, "kind": "assignment", "kind_label": "과제", "title": "합성 과제 B", "course": "합성과목",
                           "date": "2026-11-26", "source": "etl",
                           "submission": {"state": "submitted", "label": "제출함", "done": True, "late": True,
                                          "submitted_at": "2026-11-24T21:05"}}],
            "missing": [{"id": "etl:9", "course": "합성과목", "title": "합성 과제 C", "due": "2026-11-20T23:59", "url": "u"}]}
    seen = _node(tmp_path, "etlrows", view)
    todo = seen["todo"]
    assert seen["cls"][0].endswith(" missing") and seen["label"] == ["누락", "시험"]
    assert '<span class="dd missing">누락</span>' in todo and "합성 과제 C" in todo and "eTL에서 아직 안 냄" in todo
    assert todo.index("합성 과제 C") < todo.index("합성 시험")  # missing first
    assert 'data-fold="todo-submitted"' in todo and "1건" in todo and 'data-fold="todo-submitted" open' not in todo
    assert "늦게 냄" in todo and "11/24 21:05 냄" in todo
    assert 'data-fold="todo-submitted" open' in seen["todo_open"]  # the fold state is kept
    assert seen["age"] == "eTL 3시간 전에 받은 정보" and seen["none"] == ""
    assert "eTL 3시간 전에 받은 정보" in todo


@needs_node
def test_UI_REMIND_001_alerts_mute_fold_all_at_once_and_saved_switches(tmp_path):
    seen = _node(tmp_path, "groups")
    basic = seen["html_basic"]
    assert 'data-local-alert="deadlines" checked' in basic and 'data-local-alert="briefing" checked' in basic
    assert "알림 끄기" in basic and 'data-fold="mute-courses"' in basic
    assert 'data-mute-course="합성과목 2" checked' in basic and 'data-mute-course="합성과목 1">' in basic
    assert 'data-id="mute" data-to="on"' in basic and 'data-id="mute" data-to="off"' in basic
    assert seen["all_muted"] == 10 and seen["none_muted"] == 0
    calls = seen["reminder_calls"]
    assert len(calls) == 5 and all(c[0] == "reminders" for c in calls)  # no external-transfer consent dialogs
    assert calls[-3:] == [["reminders", {"muted_courses": ["합성과목 1"]}],
                          ["reminders", {"briefing": False}], ["reminders", {"deadlines": False}]]
    assert 'data-local-alert="briefing">' in seen["reminders_after"]
    assert 'data-local-alert="deadlines">' in seen["reminders_after"]
    assert 'data-fold="mute-courses" open' in seen["mute_open"]


@needs_node
def test_UI_REMIND_002_today_card_shows_muted_courses_makeups_and_escapes_text(tmp_path):
    view = {"ok": True, "date": "2026-11-25",
            "classes": [{"course": "합성과목 가", "start_ms": 50_399_999, "end_ms": 55_800_000, "kind": "보강"}],
            "dues": [{"course": "합성과목 가", "at": "2026-11-26T08:00:00+09:00", "timed": True,
                      "title": "합성 과제 <img>"}],
            "exams": [{"course": "합성과목 나", "at": "2026-11-30T23:59:00+09:00", "timed": False,
                       "title": "합성 퀴즈"}]}
    seen = _node(tmp_path, "todaycard", view)
    html = seen["html"]
    assert "수업 1개" in html and "13:59–15:30" in html and "보강" in html  # floor, no rounding
    assert "D-DAY 마감" in html and "11/26 08:00" in html
    assert "7일 내 시험·퀴즈" in html and "11/30 시각 미정" in html and "D-5" in html
    assert "&lt;img&gt;" in html and "<img>" not in html
    assert "오늘은 여유로운 하루예요" in seen["empty"]
    assert "불러오지 못했습니다" in seen["error"] and seen["live"] == "untouched"
    page = (WEB / "index.html").read_text(encoding="utf-8")
    assert page.index('id="todayCard"') < page.index('id="homeTitle"')


@needs_node
def test_UI_WEEK_001_picture_positions_exceptions_chips_and_now_line(tmp_path):
    """D-081 / UI-WEEK-001: real app.js renders a synthetic week with exact percentages."""
    hour = 3_600_000
    view = {"ok": True, "date": "2026-11-25", "start": "2026-11-23", "end": "2026-11-29",
            "start_ms": 9 * hour, "end_ms": 17 * hour, "now_ms": 10 * hour,
            "days": [{"date": f"2026-11-{d}", "classes": [], "items": []} for d in range(23, 28)]}
    view["days"][2]["classes"] = [
        {"course": "합성과목 가", "start_ms": 33_300_000, "end_ms": 38_700_000, "kind": "휴강", "note": "합성 휴강"},
        {"course": "합성과목 나", "start_ms": 36_000_000, "end_ms": 39_600_000, "kind": "정규"},
        {"course": "합성과목 가", "start_ms": 50_400_000, "end_ms": 55_800_000, "kind": "보강", "room": "합성 101호"}]
    view["days"][0]["items"] = [{"course": "합성과목 가", "title": "합성 과제 <img>", "kind": "assignment",
                                 "at": "2026-11-23T13:59:00+09:00", "timed": True}]
    view["days"][2]["items"] = [{"course": "합성과목 나", "title": "합성 퀴즈", "kind": "quiz",
                                 "at": "2026-11-25T23:59:00+09:00", "timed": False}]
    seen = _node(tmp_path, "weekcard", view)
    html = seen["html"]
    assert '--week-days:5' in html and html.count('data-week-day=') == 5
    assert 'week-class cancelled' in html and 'top:3.125%;height:18.75%;left:0%;width:50%' in html
    assert 'week-class changed' in html and 'top:62.5%;height:18.75%;left:0%;width:100%' in html
    assert '합성 101호' in html and 'week-kind">보강' in html
    assert 'week-day is-today" data-week-day="2026-11-25"' in html
    assert 'week-now" style="top:12.5%' in html and 'week-now' not in seen["outside"]
    assert 'data-day="2026-11-23" title="합성과목 가 · 합성 과제 &lt;img&gt; · 마감 13:59"' in html
    assert '퀴즈 시각 미정' in html and 'week-chip exam' in html and '<img>' not in html and '누락' not in html
    assert seen["colour"][0] == seen["colour"][1]
    assert len(set(seen["distinct"])) == 10  # ten courses, ten colours (similar names used to share one)
    colours = re.findall(r'--course-rgb:([\d,]+)', html)
    assert colours[0] == colours[2]  # same course before/after makeup, independent of its position
    assert seen["classes"] == {"with-week": True}
    assert '등록된 수업이 없습니다' in seen["empty"] and '불러오지 못했습니다' in seen["error"]
    css = (WEB / "app.css").read_text(encoding="utf-8")
    assert '.week-class.cancelled{opacity:.5}' in css and 'text-decoration:line-through' in css
    assert '.week-class.changed{border-style:dashed' in css
    assert 'repeat(var(--week-days),minmax(0,1fr))' in css


@needs_node
def test_UI_WEEK_002_navigation_handlers_and_stale_response(tmp_path):
    """D-081 / UI-WEEK-002: simultaneous week/day, native navigation, isolated responses."""
    view = {"ok": True, "date": "2026-11-25", "start": "2026-11-23", "end": "2026-11-29",
            "start_ms": 32_400_000, "end_ms": 61_200_000, "now_ms": 0,
            "days": [{"date": "2026-11-25", "classes": [], "items": []}]}
    seen = _node(tmp_path, "weekcard", view)
    assert 'data-today-view' not in seen["html"] and '이번 주로 돌아가기' in seen["html"]
    assert '12/07 – 12/13' in seen["race"] and '11/23 – 11/29' not in seen["race"]
    assert seen["day"]["ok"] and seen["failed_week_day"]["ok"]
    assert [c for c in seen["calls"] if c[0] == "week"] == [["week", n] for n in (0, -1, 0, 1, 0)]
    assert ["course", "합성과목 가"] in seen["calls"]
    assert ["calendar", 2026, 11, {"day": "2026-11-28"}] in seen["calls"]
    assert seen["live"] == "untouched"
    source = (WEB / "app.js").read_text(encoding="utf-8")
    assert '$("todayCard").addEventListener("click", todayCardClick)' in source


def _buttons(html: str, action: str) -> list:
    return re.findall(r'<button[^>]*data-a="%s"[^>]*>' % re.escape(action), html)


@needs_node
def test_UI_WEEK_003_day_panel_yields_to_recording_and_retained_transcript(tmp_path):
    """D-081 / UI-WEEK-003 / D-050: day rows never own the recording surface."""
    view = {"ok": True, "date": "2026-11-25", "now": "2026-11-25T10:00:00+09:00",
            "classes": [{"course": "합성과목 가", "start_ms": 50_400_000, "end_ms": 54_000_000, "kind": "시간변경"},
                        {"course": "합성과목 나", "start_ms": 32_400_000, "end_ms": 37_800_000, "kind": "휴강"}],
            "dues": [{"course": "합성과목 가", "at": "2026-11-25T13:30:00+09:00", "timed": True, "title": "합성 과제"},
                     {"course": "합성과목 가", "at": "2026-11-25T10:45:00+09:00", "timed": True, "title": "합성 빠른 과제"}],
            "exams": [{"course": "합성과목 나", "at": "2026-11-28T11:00:00+09:00", "timed": True, "title": "합성 시험"},
                      {"course": "합성과목 나", "at": "2026-11-25T17:00:00+09:00", "timed": True, "title": "합성 당일 퀴즈"}]}
    seen = _node(tmp_path, "daylive", view)
    html = seen["idle"]
    assert "2026-11-25" in seen["idle_head"]
    assert html.index("09:00–10:30") < html.index("14:00–15:00") < html.index("합성 과제") < html.index("합성 시험")
    assert 'day-row cancelled' in html and "시간변경" in html
    assert 'data-course="합성과목 나"' in html and 'data-day="2026-11-28"' in html
    assert "3시간 30분 남음" in html and "45분 남음" in html and "D-3" in html and "D-day" in html
    assert html.index("합성 빠른 과제") < html.index("합성 과제") and html.index("합성 당일 퀴즈") < html.index("합성 시험")
    for phase in ("starting", "recording", "stopping", "finalizing", "failed_recording"):
        assert "day-row" not in seen[phase] and "합성 과제" not in seen[phase]
    assert "합성 실시간 문장" in seen["running"] and seen["running"] == seen["retained"]
    assert seen["restored"] == html and "불러오지 못했습니다" in seen["failed"]


@needs_node
def test_UI_WEEK_004_five_columns_and_saturday_item(tmp_path):
    """D-081 / UI-WEEK-004: the same UI displays five weekday columns or the supplied weekend."""
    days = [{"date": f"2026-11-{d}", "classes": [], "items": []} for d in range(23, 28)]
    view = {"ok": True, "date": "2026-11-25", "start": "2026-11-23", "end": "2026-11-29",
            "start_ms": 32_400_000, "end_ms": 61_200_000, "now_ms": 0, "days": days}
    ordinary = _node(tmp_path, "weekcard", view)["html"]
    assert '--week-days:5' in ordinary and ordinary.count('data-week-day=') == 5 and "토 28" not in ordinary
    days.append({"date": "2026-11-28", "classes": [], "items": [
        {"course": "합성과목 가", "kind": "exam", "title": "합성 토요일 시험", "at": "2026-11-28T11:00:00+09:00", "timed": True}]})
    weekend = _node(tmp_path, "weekcard", view)["html"]
    assert '--week-days:6' in weekend and weekend.count('data-week-day=') == 6 and "합성 토요일 시험" in weekend


@needs_node
def test_UI_WEEK_005_full_month_weeks_items_candidates_and_stale_response(tmp_path):
    """D-081 / UI-WEEK-005: four, five and six complete weeks, compact items and existing day targets."""
    view = {"ok": True, "today": "2026-11-25", "exceptions": [], "candidates": [{"id": "candidate"}],
            "events": [{"id": f"synthetic-{n}", "course": "합성과목 가", "date": "2026-11-25", "kind": "quiz",
                        "title": f"합성 일정 {n}", "kind_label": "퀴즈"} for n in range(5)]}
    seen = _node(tmp_path, "homemonth", view)
    for key, days, minimum in (("2026-11", 30, "336px"), ("2027-2", 28, "272px"), ("2026-4", 30, "304px")):
        assert seen[key]["html"].count('class="day') == days
        assert seen[key]["min"] == minimum
    html = seen["2026-11"]["html"]
    assert 'data-day="2026-11-30"' in html and 'data-ev="synthetic-0"' in html
    # the month has the whole column now (week / month switch, user 2026-11-26): three lines a day, then "+N"
    assert '일정 5개' in html and '>+2</div>' in html
    assert all(f'data-ev="synthetic-{n}"' in html for n in range(3)) and 'data-ev="synthetic-3"' not in html
    assert html.count("<div") == html.count("</div>")  # entries are joined as elements, never cut mid-tag
    assert seen["candidates"] == "확인할 일정 후보 1건"
    assert seen["final_title"] == "2027년 2월" and seen["candidates_hidden"] and "합성 오류" in seen["error"]
    assert seen["calls"] == [[2026,11],[2027,2],[2026,4]]


@needs_node
def test_ai_link_off_disables_the_write_switch_connect_and_codex_buttons(tmp_path):
    seen = _node(tmp_path, "ailink", {"enabled": False})
    html = seen["html"]
    assert re.search(r'<input type="checkbox" data-ailink-write[^>]* disabled>', html)
    assert len(_buttons(html, "ailink-connect")) == 2 and all(" disabled" in b for b in _buttons(html, "ailink-connect"))
    assert " disabled" in _buttons(html, "ailink-codex")[0]
    assert seen["calls"] == []


@needs_node
def test_ai_link_on_write_switch_connect_disconnect_codex_and_undo(tmp_path):
    seen = _node(tmp_path, "ailink", {"enabled": True, "claude_state": "connected"})
    html = seen["html"]
    assert re.search(r'<input type="checkbox" data-ailink-write>', html)  # on, not yet checked
    assert "data-ailink-write checked" in seen["html_after_write"]
    assert "Codex에 연결" in html and "연결 해제" in html and "연결됨" in html
    assert " disabled" not in _buttons(html, "ailink-codex")[0]
    # the change list: undo only where it can still be undone
    assert html.count('data-a="ai-undo"') == 1 and "일정 넣음: 합성 시험" in html and "되돌림" in html
    assert html.count('data-a="ai-undo-since"') == 1 and 'data-at="2026-11-25 10:00"' in html
    assert seen["calls"] == [["write", True], ["confirm", "codex"], ["connect", "codex", "TOKEN"],
                             ["disconnect", "claude_desktop"], ["codex", None], ["undo", "c1"], ["undo_since", "c1"]]
    assert seen["dialogs"] == ["AI가 바꾼 것 모두 되돌리기"] and "2건을 되돌렸습니다." in seen["toasts"]
    assert seen["ui_ailink"] == {"enabled": True, "write": True}
    assert "연결했습니다 · 그 앱을 다시 시작하세요" in seen["toasts"] and "되돌렸습니다" in seen["toasts"]


from lecturehub.mcp.approve import KINDS as KIND_LABELS  # noqa: E402 - one row per kind (D-080, D-083)


def _kind_select(html: str, kind: str) -> str:
    found = re.search(rf'<select data-ailink-auto="{re.escape(kind)}"[^>]*>(.*?)</select>', html)
    assert found, kind
    return found.group(0)


@needs_node
def test_ai_link_kinds_rows_ask_by_default_and_follow_the_switches(tmp_path):
    """D-080: one row per kind, 묻기 selected on a new install; disabled until both switches are on."""
    seen = _node(tmp_path, "ailink", {"enabled": True, "write": True})
    html = seen["html"]
    assert "바꾸기 전에 묻기" in html and html.count("data-ailink-auto=") == len(KIND_LABELS)
    for kind, label in (("add_calendar_event", "일정 넣기"), ("update_calendar_event", "AI가 넣은 일정 고치기"),
                        ("delete_calendar_event", "AI가 넣은 일정 지우기"), ("resolve_candidate", "일정 후보 확정·무시"),
                        ("save_note", "정리본 새로 저장")):
        row = _kind_select(html, kind)
        assert '<option value="ask" selected>묻기</option>' in row and '<option value="auto">자동</option>' in row
        assert " disabled" not in row and label in html
    assert " disabled" in _buttons(html, "ailink-auto-reset")[0]  # nothing to reset yet
    assert "50초 안에 고르지 않으면 바꾸지 않음" in html and "[이 종류는 앞으로 자동]" in html
    off = _node(tmp_path, "ailink", {"enabled": True, "write": False})["html"]
    assert all(" disabled" in _kind_select(off, k) for k in ("add_calendar_event", "save_note"))
    on = _node(tmp_path, "ailink", {"enabled": True, "write": True, "auto": {"save_note": True}})["html"]
    assert '<option value="auto" selected>자동</option>' in _kind_select(on, "save_note")
    assert " disabled" not in _buttons(on, "ailink-auto-reset")[0]


@needs_node
def test_ai_link_kind_to_automatic_needs_the_native_dialog_and_back_and_reset_do_not(tmp_path):
    seen = _node(tmp_path, "ailink", {"enabled": True, "write": True, "kinds_flow": True})
    assert seen["calls"] == [["auto_confirm", "save_note"], ["auto", "save_note", True, "AUTO-TOKEN"],
                             ["auto_confirm", "add_calendar_event"], ["auto", "add_calendar_event", True, "AUTO-TOKEN"],
                             ["auto", "save_note", False, None],
                             ["auto_confirm", "*"], ["auto", "*", True, "AUTO-TOKEN"], ["auto_reset"]]
    every = seen["html_after_all"]  # [모두 자동]: one dialog, every row 자동, the button itself greyed out
    assert every.count('<option value="auto" selected>자동</option>') == len(KIND_LABELS)
    assert " disabled" in _buttons(every, "ailink-auto-all")[0]
    assert any(t.startswith("모든 종류를 자동으로") for t in seen["toasts"])
    assert '<option value="auto" selected>자동</option>' in _kind_select(seen["html_after_auto"], "save_note")
    assert "정리본 새로 저장: 앞으로 묻지 않고 바로 바꿉니다" in seen["toasts"]
    assert "정리본 새로 저장: 바꾸기 전에 묻습니다" in seen["toasts"]
    assert any(t.startswith("모든 종류를 다시 묻습니다") for t in seen["toasts"])
    after = seen["html_after_reset"]
    assert after.count('<option value="ask" selected>묻기</option>') == len(KIND_LABELS)
    # the native dialog said no: nothing stored, the row shows 묻기 again
    no = _node(tmp_path, "ailink", {"enabled": True, "write": True, "kinds_flow": True, "auto_yes": False})
    assert ["auto_confirm", "save_note"] in no["calls"] and not [c for c in no["calls"] if c[0] == "auto" and c[2]]
    assert '<option value="ask" selected>묻기</option>' in _kind_select(no["html_after_auto"], "save_note")


@needs_node
def test_timetable_photo_button_passes_the_native_dialogs_token_and_explains_switches(tmp_path):
    """D-083: settings > 기본 > 시간표 · 학기 has [사진으로 시간표 넣기 (ChatGPT·Codex)]; the page passes only the token the
    native confirmation gave; switches off → the Api's message is shown and nothing is opened."""
    seen = _node(tmp_path, "ailink", {"enabled": True, "write": True, "tt_photo": True})
    assert 'data-a="tt-photo"' in seen["html_basic"] and "사진으로 시간표 넣기 (ChatGPT·Codex)" in seen["html_basic"]
    assert "OpenAI" in seen["html_basic"]
    assert seen["calls"] == [["photo_confirm"], ["photo_open", "PHOTO-TOKEN"]]
    assert any(t.startswith("Codex를 열었습니다") for t in seen["toasts"])
    no = _node(tmp_path, "ailink", {"enabled": True, "write": True, "tt_photo": True, "photo_yes": False})
    assert no["calls"] == [["photo_confirm"]] and no["toasts"] == []
    off = _node(tmp_path, "ailink", {"enabled": True, "write": False, "tt_photo": True, "photo_off": True})
    assert off["calls"] == [["photo_confirm"]] and any("고칠 수 있게" in t for t in off["toasts"])


@needs_node
def test_ai_link_rows_include_the_d083_kinds(tmp_path):
    html = _node(tmp_path, "ailink", {"enabled": True, "write": True})["html"]
    assert '<option value="ask" selected>묻기</option>' in _kind_select(html, "set_timetable") and "시간표 바꾸기" in html


@needs_node
def test_connect_needs_the_native_dialog_yes(tmp_path):
    seen = _node(tmp_path, "ailink", {"enabled": True, "say_yes": False})
    assert ["confirm", "codex"] in seen["calls"] and not [c for c in seen["calls"] if c[0] == "connect"]


@needs_node
def test_codex_missing_shows_the_install_message(tmp_path):
    seen = _node(tmp_path, "ailink", {"enabled": True, "codex_found": False})
    assert "Codex(ChatGPT)가 이 PC에 없습니다" in seen["html"]
    assert "Codex(codex.cmd)를 찾지 못했습니다." in seen["toasts"]


@needs_node
def test_claude_desktop_in_the_store_build_says_paste(tmp_path):
    seen = _node(tmp_path, "ailink", {"enabled": False, "claude_state": "unsupported", "claude_message": "스토어판에서는 붙여 넣기로"})
    html = seen["html"]
    assert "스토어판에서는 붙여 넣기로" in html
    assert len(_buttons(html, "ailink-connect")) == 1  # Codex only


@needs_node
def test_UI_MCP_INSPECT_001_settings_help_lists_inspection_tools_and_skills(tmp_path):
    """D-082: the real rendered settings card explains inspection and its privacy boundary."""
    html = _node(tmp_path, "ailink", {"enabled": True})["html"]
    for name in ("get_app_status", "get_timetable", "get_lecture_status", "list_ai_changes", "check-notices", "app-checkup"):
        assert name in html
    assert "강의자 이름 없음" in html and "비밀값 없는 설정 개요" in html


# ---------------------------------------------------------------- D-086: first run, Codex step and the timetable's ways
def _setup(tmp_path, **opts) -> dict:
    return _node(tmp_path, "setup", opts)


def _names(calls, kind) -> list:
    return [c for c in calls if c[0] == kind]


@needs_node
def test_UI_SETUP_001_codex_step_comes_after_the_folder_and_connects_with_one_dialog(tmp_path):
    seen = _setup(tmp_path, connect=True, photo=True, type=True, arrive=True)
    assert seen["first"].startswith("처음 설정 · 1/6 안내") and seen["folder"] == "처음 설정 · 2/6 폴더"
    assert seen["layout"] == "처음 설정 · 3/6 폴더 구성" and seen["codex_head"] == "처음 설정 · 4/6 ChatGPT(Codex) 연결"
    assert seen["tt_head"] == "처음 설정 · 5/6 시간표" and seen["tools_head"] == "처음 설정 · 6/6 도구"
    calls = seen["calls"]
    prepare = _names(calls, "prepare")
    assert prepare == [["prepare", "C:\\합성\\문서", "26-2", "basic"]]
    assert calls.index(prepare[0]) < calls.index(["state", False])  # the folder exists before the Codex step
    # the Codex step: found and signed in (the settings' check), not connected yet -> [건너뛰기]; no way back
    assert "있음 · 로그인됨" in seen["codex"] and "학기 폴더를 만들었습니다" in seen["codex"]
    assert seen["codex_next"] == "건너뛰기" and not seen["codex_back"]
    for words in ("Codex 설정 파일의 Lecture Hub 항목", "'AI 앱 연결'", "'AI 앱이 고칠 수 있게'", "바꿀 때마다 Lecture Hub가 화면에 묻습니다",
                  "과목 자료를 읽게 하는 허용은 켜지 않습니다", "설정 › AI › AI 앱 연결에서 끌 수 있습니다", "아무것도 켜지 않고"):
        assert words in seen["codex"], words
    assert ["state", True] in calls  # 프로그램 다시 찾기 / the login check, as settings › 연결
    # one native dialog, its token passed on unchanged
    assert _names(calls, "setup_confirm") == [["setup_confirm"]] and _names(calls, "setup_connect") == [["setup_connect", "SETUP-TOKEN"]]
    assert "연결했습니다" in seen["codex_after"] and seen["codex_after_next"] == "다음"
    assert "있음 · 로그인됨" in seen["codex_after"]  # the quick re-read after connecting keeps the login result
    # the timetable step: three ways, the photo flow, the waiting line, the arrival
    assert 'data-act="tt-photo-setup">' in seen["tt"] and 'data-act="codex-first">' in seen["tt"]
    assert "사진은 확인 창을 거친 뒤 내 Codex 로그인으로 OpenAI에 보내집니다" in seen["tt"] and 'class="ask cname"' in seen["tt"]
    assert _names(calls, "photo_open") == [["photo_open", "PHOTO-TOKEN"]]
    assert "Codex 창에서 읽은 시간표를 확인하면 Lecture Hub가 한 번 더 묻습니다" in seen["waiting"]
    assert "기다리지 않고 아래에 직접 적어도 됩니다" in seen["waiting"] and 'data-act="next"' in seen["waiting"]
    assert seen["watchers"] == 1
    arrived = seen["arrived"]
    assert "Codex가 넣은 시간표가 들어왔습니다(2과목)" in arrived and "직접 적은 과목 1개는 아래에 그대로 두었습니다" in arrived
    assert arrived.index('value="합성과목 가"') < arrived.index('value="합성과목 나"') < arrived.index('value="합성과목 다"')
    assert 'id="su-start" class="ask" value="2026-09-02"' in arrived and "Codex 창에서 읽은 시간표를 확인하면" not in arrived
    assert seen["arrived_again"] and seen["watchers_after"] == 0  # leaving the step stops watching
    apply = _names(calls, "apply")[-1]
    assert [c["name"] for c in apply[3]] == ["합성과목 가", "합성과목 나", "합성과목 다"]
    assert apply[4:8] == ["2026-09-02", "2026-12-19", "basic", "B1"] and seen["after_head"] == "설정을 저장했어요"
    assert "!" not in seen["codex"] + seen["tt"] + seen["waiting"] + arrived


@needs_node
def test_UI_SETUP_002_codex_not_installed_shows_the_hint_and_typing_still_works(tmp_path):
    seen = _setup(tmp_path, found=False, help=True, type=True)
    assert "Codex가 이 PC에 없습니다" in seen["codex"] and "npm install -g @openai/codex" in seen["codex"]
    assert re.search(r'data-act="codex-connect" disabled', seen["codex"]) and 'data-act="codex-help"' in seen["codex"]
    assert seen["codex_next"] == "건너뛰기" and ["help", "codex"] in seen["calls"]
    assert re.search(r'data-act="tt-photo-setup" disabled', seen["tt"]) and re.search(r'data-act="codex-first" disabled', seen["tt"])
    assert "앞 단계에서 [Codex에 연결]을 누르세요. 아래에 직접 적어도 됩니다." in seen["tt"]
    assert not _names(seen["calls"], "setup_confirm") and not _names(seen["calls"], "setup_connect")
    apply = _names(seen["calls"], "apply")[-1]
    assert [c["name"] for c in apply[3]] == ["합성과목 다"] and apply[4:6] == ["2026-09-01", "2026-12-20"]
    assert seen["after_head"] == "설정을 저장했어요"


@needs_node
def test_UI_SETUP_003_skipping_turns_on_nothing_and_saying_no_to_the_dialog_too(tmp_path):
    for opts in ({}, {"connect": True, "say_yes": False}):
        seen = _setup(tmp_path, type=True, **opts)
        assert not _names(seen["calls"], "setup_connect"), opts
        assert re.search(r'data-act="tt-photo-setup" disabled', seen["tt"])
        assert not _names(seen["calls"], "photo_confirm") and not _names(seen["calls"], "codex_first")
        assert seen["after_head"] == "설정을 저장했어요" and [c["name"] for c in _names(seen["calls"], "apply")[-1][3]] == ["합성과목 다"]
    seen = _setup(tmp_path, found=False, signed_in=False)
    assert "Codex가 이 PC에 없습니다" in seen["codex"]
    seen = _setup(tmp_path, signed_in=False)
    assert "로그인 필요" in seen["codex"] and "codex login" in seen["codex"]


@needs_node
def test_UI_SETUP_004_codex_first_setup_waits_and_a_timetable_changed_before_saving_is_shown_not_overwritten(tmp_path):
    seen = _setup(tmp_path, connect=True, first=True, type=True, change_before_save=True)
    assert _names(seen["calls"], "codex_first") == [["codex_first"]]
    assert "Codex가 묻는 말에 답하세요" in seen["waiting_first"] and "Lecture Hub가 한 번 더 묻습니다" in seen["waiting_first"]
    # the save found another timetable: back on the timetable step with it, the typed course kept below, nothing saved
    assert seen["back_to"] == "처음 설정 · 5/6 시간표" and "그 사이 시간표가 바뀌었습니다" in seen["after_save"]
    assert seen["after_save"].index('value="합성과목 마"') < seen["after_save"].index('value="합성과목 다"')
    applies = _names(seen["calls"], "apply")
    assert len(applies) == 2 and applies[0][7] == "B0" and applies[1][7] == "B2"
    assert [c["name"] for c in applies[1][3]] == ["합성과목 마", "합성과목 다"] and seen["saved_second"] == "설정을 저장했어요"


@needs_node
def test_UI_SETUP_005_a_wizard_closed_half_way_goes_on_from_the_codex_step(tmp_path):
    seen = _setup(tmp_path, prepared={"root": "C:\\합성\\문서\\26-2", "term": "26-2", "layout": "basic", "parent": "C:\\합성\\문서"}, type=True)
    assert seen["folder"] == "처음 설정 · 4/6 ChatGPT(Codex) 연결" and not _names(seen["calls"], "prepare")
    apply = _names(seen["calls"], "apply")[-1]
    assert apply[1:3] == ["C:\\합성\\문서", "26-2"] and apply[6] == "basic" and seen["after_head"] == "설정을 저장했어요"
