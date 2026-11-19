"""The window side of the release review of 2026-11-16 (docs/reviews/release_bundle2_review_open_2026-11-16.md).

What both windows show and announce about a recording that lost sound or runs out of disk, for either engine; the
start notices; the settings switch for reading notices; and the rule that a window never leaves the app's page.

Python parts run against ``Backend`` with the engine process stubbed (events are fed in by hand). The pages' own
logic is run by node from the real files (app.js, mini.html) with a stub page and a stub API; those tests are skipped
where node is not installed. Nothing here opens a window, a sound device or the network.
"""
from __future__ import annotations

import json
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


def test_the_app_guards_both_of_its_windows():
    """Both windows carry the whole API. (That the guard works in the real engine is the last step of the
    hidden-window self-check: core/selfcheck.py.)"""
    import inspect

    from lecturehub.core import selfcheck

    assert "for each in (main_window, mini):" in inspect.getsource(ui_app.main)
    assert "guard_navigation(each)" in inspect.getsource(ui_app.main)
    walk = inspect.getsource(selfcheck._window)
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
    const body = cut(src, "  function layoutSection(L, mode, pick) {", "  /* ---------- timetable and term editor (TT-001)");
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
        dialog: async () => false, askText: async () => "" };
      const names = Object.keys(env);
      const openSettings = new Function(...names, body + "\n return openSettings;")(...names.map((n) => env[n]));
      const drawn = () => /data-notices checked/.test(scrim.innerHTML);
      await openSettings();
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
  }
  console.log(JSON.stringify(out));
})().catch((err) => { console.error(err && err.stack || err); process.exit(1); });
"""


def _node(tmp_path: Path, mode: str, data=None) -> dict:
    script = tmp_path / "harness.js"
    script.write_text(HARNESS, encoding="utf-8")
    extra = []
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
