"""The main window's Content-Security-Policy against a real browser engine (release review 2026-11-16).

The app's window is WebView2, the same engine as Edge. A window that was never shown does not load sound at all, so
the build's hidden-window walk cannot show that the policy lets a recording play; a windowless Edge page counts as
visible and can. Three pages with the same <audio> from the app's media server (another loopback port):

- no policy: the sound must load (otherwise this machine cannot run the test: skipped, with the reason),
- the app's policy, taken from index.html: the sound must load and nothing may be reported as blocked,
- the app's policy with ``media-src 'self'`` only: the sound must be blocked AND reported, which shows that a block
  would not go unnoticed in the case above.

Two more things are tried the same way (review of 2026-11-16):

- the note frame: a note is shown in a srcdoc frame, which inherits the window's policy on top of the note's own
  (NOTE_CSP in app.js). Sound embedded in a note as ``data:``, which the note policy allows on purpose, must load;
- reporting: with the real index.html and app.js, something blocked before app.js ran, and something blocked before
  the Python bridge existed, must both reach ``client_error`` once the bridge is there.
"""
from __future__ import annotations

import base64
import functools
import http.server
import io
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
import wave
from pathlib import Path

import pytest

from lecturehub.ui.app import WEB
from lecturehub.ui.media_server import MediaServer

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="the app's window engine is Windows-only")

PAGE_JS = """
var out = {blocked: [], events: []};
function show() { document.getElementById('out').textContent = JSON.stringify(out); }
document.addEventListener('securitypolicyviolation', function (e) {
  out.blocked.push(e.violatedDirective + ' ' + e.blockedURI); show();
});
window.addEventListener('DOMContentLoaded', function () {
  var a = document.getElementById('a'), i = document.getElementById('i');
  ['loadedmetadata', 'error'].forEach(function (n) {
    a.addEventListener(n, function () { out.events.push(n); out.ready = a.readyState; show(); });
  });
  i.addEventListener('load', function () { out.image = i.naturalWidth; show(); });
  i.addEventListener('error', function () { out.image = 0; show(); });
  out.visible = document.visibilityState; show();
});
"""


def _edge() -> str | None:
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
        path = Path(base or "") / "Microsoft" / "Edge" / "Application" / "msedge.exe"
        if base and path.is_file():
            return str(path)
    return None


def _policy() -> str:
    found = re.search(r'<meta http-equiv="Content-Security-Policy" content="[^"]+">',
                      (WEB / "index.html").read_text(encoding="utf-8"))
    assert found, "index.html has no Content-Security-Policy"
    return found.group(0)


MEDIA = "media-src 'self' file: data: blob: http://127.0.0.1:*"  # as written in index.html; controls narrow exactly this


def _tone(path: Path | io.BytesIO) -> None:
    with wave.open(path if isinstance(path, io.BytesIO) else str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(8000)
        out.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(n / 3.0))) for n in range(8000)))


def _dot(path: Path) -> None:  # a 1x1 GIF
    path.write_bytes(bytes.fromhex("47494638396101000100800000000000ffffff21f90401000000002c00000000010001000002024401003b"))


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def _open(edge: str, profile: Path, url: str) -> dict:
    try:
        done = subprocess.run(
            [edge, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
             f"--user-data-dir={profile}", "--virtual-time-budget=8000", "--dump-dom", url],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
    except subprocess.TimeoutExpired:
        # Edge itself hung (seen once on a PC busy with another full test run): that says nothing about the page.
        # A skip is shown in the test summary; a policy that really blocks something fails the assertions instead.
        pytest.skip("windowless Edge did not finish within 180 s on this machine")
    found = re.search(r'<pre id="out">(.*?)</pre>', done.stdout, re.S)
    try:
        return json.loads(found.group(1).replace("&quot;", '"').replace("&amp;", "&")) if found else {}
    except ValueError:
        return {}


def test_the_window_policy_lets_recordings_and_pictures_load_from_the_media_server(tmp_path):
    edge = _edge()
    if not edge:
        pytest.skip("Edge is not installed: the policy cannot be tried against the engine here")
    tone, dot = tmp_path / "tone.wav", tmp_path / "dot.gif"
    _tone(tone)
    _dot(dot)
    media = MediaServer()
    policy = _policy()
    allowed = MEDIA
    assert allowed in policy  # the control below is made by narrowing exactly this
    site = tmp_path / "site"
    site.mkdir()
    (site / "page.js").write_text(PAGE_JS, encoding="utf-8")
    for name, meta in (("none", ""), ("app", policy), ("narrow", policy.replace(allowed, "media-src 'self'"))):
        (site / f"{name}.html").write_text(
            f'<!doctype html><html><head><meta charset="utf-8">{meta}<script src="page.js"></script></head><body>'
            f'<audio id="a" preload="metadata" src="{media.url_for(tone)}"></audio>'
            f'<img id="i" src="{media.url_for(dot)}"><pre id="out"></pre></body></html>', encoding="utf-8")
    pages = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Quiet, directory=str(site)))
    threading.Thread(target=pages.serve_forever, daemon=True).start()
    try:
        origin = f"http://127.0.0.1:{pages.server_address[1]}"  # like the app: a loopback page, media on another port
        seen = {name: _open(edge, tmp_path / "profile", f"{origin}/{name}.html") for name in ("none", "app", "narrow")}
    finally:
        pages.shutdown()
        media.close()
    if "loadedmetadata" not in seen["none"].get("events", []):
        pytest.skip(f"windowless Edge did not load sound even without a policy on this machine: {seen['none']}")
    assert seen["app"].get("events") == ["loadedmetadata"] and seen["app"]["blocked"] == [], seen["app"]
    assert seen["app"].get("image") == 1, seen["app"]
    assert seen["narrow"].get("events") == ["error"], seen["narrow"]
    assert any(line.startswith("media-src ") for line in seen["narrow"]["blocked"]), seen["narrow"]


NOTE_PAGE_JS = """
var out = {blocked: [], events: []};
function show() { document.getElementById('out').textContent = JSON.stringify(out); }
window.addEventListener('DOMContentLoaded', function () {
  var f = document.getElementById('f');
  f.addEventListener('load', function () {
    var d = f.contentDocument, a = d.getElementById('a');
    out.href = f.contentWindow.location.href;
    d.addEventListener('securitypolicyviolation', function (e) {
      out.blocked.push(e.violatedDirective + ' ' + String(e.blockedURI).slice(0, 20)); show();
    });
    ['loadedmetadata', 'error'].forEach(function (n) {
      a.addEventListener(n, function () { out.events.push(n); show(); });
    });
    a.load();  // ask for the sound again now that the frame's own document is listened to
    show();
  });
  f.srcdoc = window.NOTE_DOC;  // as app.js showHtml(): NOTE_CSP + the note's HTML
});
"""


def test_the_window_policy_keeps_what_the_note_frame_allows_on_purpose(tmp_path):
    """A srcdoc frame must pass the window's policy AND the note's own. 63f88de left ``data:`` out of the window's
    media-src, so sound embedded in a note stopped playing although NOTE_CSP allows it."""
    edge = _edge()
    if not edge:
        pytest.skip("Edge is not installed: the policy cannot be tried against the engine here")
    policy = _policy()
    assert MEDIA in policy
    note_policy = re.search(r"const NOTE_CSP = `([^`]+)`", (WEB / "app.js").read_text(encoding="utf-8")).group(1)
    assert "media-src 'self' data:" in note_policy  # what the note may do on purpose
    sound = io.BytesIO()
    _tone(sound)
    note = ('<!doctype html><html><body><h1>note</h1><audio id="a" controls src="data:audio/wav;base64,'
            + base64.b64encode(sound.getvalue()).decode("ascii") + '"></audio></body></html>')
    site = tmp_path / "site"
    site.mkdir()
    (site / "page.js").write_text(NOTE_PAGE_JS, encoding="utf-8")
    (site / "doc.js").write_text("window.NOTE_DOC = " + json.dumps(note_policy + note) + ";", encoding="utf-8")
    without = policy.replace(MEDIA, MEDIA.replace(" data:", ""))  # the policy as it was: the control
    for name, meta in (("none", ""), ("app", policy), ("without", without)):
        (site / f"{name}.html").write_text(
            f'<!doctype html><html><head><meta charset="utf-8">{meta}<script src="doc.js"></script>'
            '<script src="page.js"></script></head><body>'
            '<iframe id="f" title="note" sandbox="allow-same-origin"></iframe><pre id="out"></pre></body></html>',
            encoding="utf-8")
    pages = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Quiet, directory=str(site)))
    threading.Thread(target=pages.serve_forever, daemon=True).start()
    try:
        origin = f"http://127.0.0.1:{pages.server_address[1]}"
        seen = {name: _open(edge, tmp_path / "profile", f"{origin}/{name}.html") for name in ("none", "app", "without")}
    finally:
        pages.shutdown()
    if seen["none"].get("events") != ["loadedmetadata"]:
        pytest.skip(f"windowless Edge did not load embedded sound even without a policy on this machine: {seen['none']}")
    assert seen["app"].get("href") == "about:srcdoc", seen["app"]
    assert seen["app"].get("events") == ["loadedmetadata"] and seen["app"]["blocked"] == [], seen["app"]
    assert seen["without"].get("events") == ["error"], seen["without"]  # the control: this test can fail
    assert any(line.startswith("media-src ") for line in seen["without"]["blocked"]), seen["without"]


# Added after app.js in a copy of the real page. It stands in for pywebview: the bridge appears 300 ms after the page
# loaded, as it does in the app (never before the page is complete), and client_error records what it is given.
BRIDGE_JS = """
var calls = [];
function show() {
  var p = document.getElementById('out');
  if (!p) { p = document.createElement('pre'); p.id = 'out'; document.body.appendChild(p); }
  p.textContent = JSON.stringify({calls: calls});
}
window.addEventListener('load', function () {
  setTimeout(function () {
    var never = function () { return new Promise(function () {}); };
    window.pywebview = {api: {client_error: function (t) { calls.push(t); show(); return Promise.resolve({ok: true}); },
                              init: never, appearance: never, ai_status: never}};
    window.dispatchEvent(new CustomEvent('pywebviewready'));
    setTimeout(function () {  // after the bridge: the same thing again, and a new one
      var again = new Image(); again.src = 'http://localhost:9/between.png'; document.body.appendChild(again);
      var late = new Image(); late.src = 'http://localhost:9/late.png'; document.body.appendChild(late);
      show();
    }, 300);
    show();
  }, 300);
});
"""


def test_blocks_from_before_the_bridge_and_before_the_script_reach_the_log(tmp_path):
    """The real index.html and app.js. Three pictures on an address the policy does not cover (blocked without any
    request): one in the markup above every script, one after app.js but before the bridge, one after the bridge.
    63f88de dropped the first two - and, having marked them as seen, never reported a repeat either."""
    edge = _edge()
    if not edge:
        pytest.skip("Edge is not installed: the page cannot be tried against the engine here")
    site = tmp_path / "site"
    shutil.copytree(WEB, site)
    page = (site / "index.html").read_text(encoding="utf-8")
    first, script = '<div class="app">', '<script src="app.js"></script>'
    assert first in page and script in page and page.index(first) < page.index("<script")  # above every script
    page = page.replace(first, '<img src="http://localhost:9/early.png" alt="">' + first, 1)
    page = page.replace(script, script + '<script src="bridge.js"></script><img src="http://localhost:9/between.png" alt="">', 1)
    (site / "index.html").write_text(page, encoding="utf-8")
    (site / "bridge.js").write_text(BRIDGE_JS, encoding="utf-8")
    pages = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Quiet, directory=str(site)))
    threading.Thread(target=pages.serve_forever, daemon=True).start()
    try:
        seen = _open(edge, tmp_path / "profile", f"http://127.0.0.1:{pages.server_address[1]}/index.html")
    finally:
        pages.shutdown()
    if "calls" not in seen:
        pytest.skip(f"windowless Edge did not run the page's scripts on this machine: {seen}")
    blocked = [line for line in seen["calls"] if line.startswith("csp:")]
    for name in ("early.png", "between.png", "late.png"):
        assert len([line for line in blocked if f"img-src blocked http://localhost:9/{name}" in line]) == 1, (name, seen)
    assert len(blocked) == 3, seen  # nothing of the app's own was blocked on the way
    assert not [line for line in seen["calls"] if line.startswith("error:")], seen
