"""Security review 2026-11-05: note iframe isolation, open_path refusal, consent only via the native dialog."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from lecturehub.ui import app as app_module
from lecturehub.ui.app import Api

WEB = Path(app_module.__file__).resolve().parent / "web"


def test_SEC_001_note_frame_is_sandboxed_without_scripts_and_gets_a_csp():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    frame = re.search(r'<iframe id="htmlFrame"[^>]*>', html).group(0)
    assert 'sandbox="allow-same-origin"' in frame and "allow-scripts" not in frame
    js = (WEB / "app.js").read_text(encoding="utf-8")
    csp = re.search(r"const NOTE_CSP = `(.*?)`;", js).group(1)
    for rule in ("script-src 'none'", "connect-src 'none'", "object-src 'none'", "base-uri 'none'",
                 "img-src 'self' data:", "font-src 'self' data:", "media-src 'self' data:", "frame-src 'none'"):
        assert rule in csp
    assert "ui.htmlDoc = NOTE_CSP +" in js


def test_SEC_002_note_links_never_follow_javascript_or_data():
    js = (WEB / "app.js").read_text(encoding="utf-8")
    body = js[js.index("function noteLink"):js.index("function noteFrameLoaded")]
    assert body.index("e.preventDefault()") < body.index("javascript|data|vbscript")


CHROME = shutil.which("google-chrome") or shutil.which("chromium")


def _playwright() -> str | None:
    try:
        root = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True, timeout=20, check=False).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    path = Path(root) / "playwright"
    return str(path) if path.is_dir() else None


@pytest.mark.skipif(os.name == "nt" or not CHROME or not shutil.which("node"), reason="headless chromium check (Linux)")
def test_SEC_003_injected_script_in_a_note_never_reaches_the_app_api(tmp_path):
    pw = _playwright()
    if not pw:
        pytest.skip("playwright not installed")
    html = (WEB / "index.html").read_text(encoding="utf-8")
    frame = re.search(r'<iframe id="htmlFrame"[^>]*>', html).group(0)
    js = (WEB / "app.js").read_text(encoding="utf-8")
    csp = re.search(r"const NOTE_CSP = `(.*?)`;", js).group(1)
    note = ('<a id="toc" href="#s2">s2</a><a id="js" href="javascript:parent.pywebview.api.discard()">x</a>'
            '<img src=x onerror="parent.pywebview.api.discard()">'
            '<script>parent.pywebview.api.discard();top.HIT2=1</script><div style="height:3000px"></div><h2 id="s2">S2</h2>')
    script = tmp_path / "t.js"
    script.write_text("""
const { chromium } = require(process.env.PW);
(async () => {
  const b = await chromium.launch({ executablePath: process.env.CHROME, args: ['--no-sandbox'] });
  const p = await b.newPage();
  await p.setContent(`<body>${process.env.FRAME}</iframe></body>`);
  await p.evaluate(({ csp, note }) => {
    window.pywebview = { api: { discard: () => { window.HIT = (window.HIT || 0) + 1; } } };
    window.KEYS = 0;
    const f = document.getElementById('htmlFrame');
    f.addEventListener('load', () => {
      f.contentDocument.addEventListener('keydown', (e) => { if (e.key === 'Escape') window.KEYS++; });
      f.contentDocument.addEventListener('click', (e) => { const a = e.target.closest('a[href]'); if (!a) return;
        e.preventDefault(); const t = a.ownerDocument.getElementById(a.getAttribute('href').slice(1));
        if (t) { t.scrollIntoView({ block: 'start' }); window.SCROLLED = a.ownerDocument.defaultView.scrollY; } });
      window.LOADED = 1;
    });
    f.srcdoc = csp + note;
  }, { csp: process.env.CSP, note: process.env.NOTE });
  await p.waitForFunction(() => window.LOADED);
  await p.waitForTimeout(300);
  const fr = p.frames()[1];
  await fr.click('#toc'); await fr.press('body', 'Escape'); await fr.click('#js');
  await p.waitForTimeout(300);
  console.log(JSON.stringify(await p.evaluate(() => [window.HIT || 0, window.HIT2 || 0, window.KEYS, window.SCROLLED || 0])));
  await b.close();
})();
""", encoding="utf-8")
    env = {**os.environ, "PW": pw, "CHROME": CHROME, "FRAME": frame, "CSP": csp, "NOTE": note}
    out = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=90, env=env, check=False)
    assert out.returncode == 0, out.stderr
    hit, hit2, keys, scrolled = __import__("json").loads(out.stdout.strip().splitlines()[-1])
    assert hit == 0 and hit2 == 0  # no script, handler or javascript: link ran
    assert keys == 1 and scrolled > 0  # the app's own Esc and table-of-contents handling still work


class _Library:
    def __init__(self, root):
        self.root = root


class _Window:
    def __init__(self, answer):
        self.answer, self.asked = answer, []

    def create_confirmation_dialog(self, title, message):
        self.asked.append((title, message))
        return self.answer


def test_SEC_004_open_path_refuses_programs_and_shortcuts(tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(os, "startfile", lambda p: opened.append(p), raising=False)
    api = Api(object(), _Library(tmp_path))
    for name in ("setup.exe", "run.BAT", "a.cmd", "x.lnk", "y.url", "z.hta", "s.js", "v.vbs", "p.ps1", "i.msi", "q.scr"):
        (tmp_path / name).write_bytes(b"x")
        result = api.open_path(str(tmp_path / name))
        assert result["ok"] is False and "열지 않습니다" in result["error"]
    (tmp_path / "slides.pdf").write_bytes(b"%PDF")
    assert api.open_path(str(tmp_path / "slides.pdf"))["ok"] and len(opened) == 1


RISKY = ("tool.py", "tool.pyw", "help.chm", "add.xll", "macro.docm", "sheet.xlsm", "deck.pptm", "x.xlam", "noext",
         "trail.pdf.", "double.pdf.exe", "s.js", "a.cmd", "r.reg")


def test_SEC_008_one_allow_list_guards_every_open(tmp_path, monkeypatch):
    """open_path, open_material and open_folder share core.safe_open (security review 2026-11-13)."""
    from lecturehub.core import safe_open
    from lecturehub.materials import opener
    from lecturehub.ui.backend import Backend

    opened = []
    monkeypatch.setattr(os, "startfile", lambda p: opened.append(p), raising=False)
    for name in RISKY:
        path = tmp_path / name
        try:
            path.write_bytes(b"x")
        except OSError:
            continue  # Windows strips a trailing dot; nothing to open then
        assert safe_open.refusal(path), name
        with pytest.raises(PermissionError):
            safe_open.startfile(path)
        assert opener.open_material(path, 1, tmp_path)["ok"] is False, name
        assert Api(object(), _Library(tmp_path)).open_path(str(path))["ok"] is False, name
    for name in ("a.pdf", "b.PPTX", "c.hwp", "d.xlsx", "e.png", "f.m4a", "g.mp4", "h.txt", "i.zip", "j.html"):
        (tmp_path / name).write_bytes(b"x")
        assert safe_open.refusal(tmp_path / name) is None, name
    assert safe_open.refusal(tmp_path) is None
    assert opened == []
    jobs = tmp_path / "_lecturehub" / "jobs"
    jobs.mkdir(parents=True)
    (jobs / "evil.pyw").write_bytes(b"x")
    backend = Backend.__new__(Backend)
    backend.root = tmp_path
    assert backend.open_folder(str(jobs / "evil.pyw"))["ok"] is False and opened == []
    assert backend.open_folder(str(jobs))["ok"] and opened == [str(jobs)]


def test_SEC_005_consent_token_comes_only_from_the_native_dialog_and_is_used_once(tmp_path):
    api = Api(object(), _Library(tmp_path))
    assert api._take_consent_token(None, "gemini_transcribe", "도시계") is False  # straight from page JS: refused
    assert api._take_consent_token("made-up", "gemini_transcribe", "도시계") is False
    api._main = _Window(False)
    assert api.consent_confirm("gemini_transcribe", "도시계") == {"ok": True, "yes": False}
    api._main = _Window(True)
    q = api.consent_confirm("gemini_transcribe", "도시계")
    assert q["yes"] and "도시계" in api._main.asked[0][1]
    assert api._take_consent_token(q["token"], "gemini_transcribe", "구조역학") is False  # another course (used up)
    q = api.consent_confirm("gemini_transcribe", "도시계")
    assert api._take_consent_token(q["token"], "whisper_groq", "도시계") is False  # another provider (used up)
    q = api.consent_confirm("gemini_transcribe", "도시계")
    assert api._take_consent_token(q["token"], "gemini_transcribe", "도시계") is True
    assert api._take_consent_token(q["token"], "gemini_transcribe", "도시계") is False  # one time only
    assert api.consent_confirm("other", "도시계")["ok"] is False
    # the assignment helper was removed (2026-11-22): its old provider is unknown now, never a dialog
    assert api.consent_confirm("claude_assignment", "도시계")["ok"] is False


class _Backend:
    def __init__(self, course, granted=False):
        self.course, self.granted, self.started = course, granted, []

    def transcription_check(self, master=None, course=None):
        course = course if course is not None else self.course
        named = bool(course)
        return {"master": "m.mp3", "course": course, "needs_consent": not named or not self.granted,
                "once_only": not named, "notice": "n"}

    def transcription_start(self, consent, master=None, course=None, **kw):
        self.started.append((consent, master, course))
        return {"ok": True}

    def engine_start(self, engine, master, course, consent, language):
        self.started.append((engine, consent))
        return {"ok": True}


def test_SEC_006_stored_transcription_consent_needs_the_token_but_once_only_and_granted_flows_work(tmp_path):
    backend = _Backend("구조역학")
    api = Api(backend, _Library(tmp_path))
    result = api.transcription_start(True)  # a page script passing consent=true
    assert result["ok"] is False and result["needs_confirm"] and backend.started == []
    assert api.engine_start("google", "m.mp3", "구조역학", True)["ok"] is False and backend.started == []
    api._main = _Window(True)
    q = api.consent_confirm("gemini_transcribe", "구조역학")
    assert api.transcription_start(True, None, None, q["token"])["ok"] and backend.started == [(True, None, None)]
    q = api.consent_confirm("gemini_transcribe", "구조역학")
    assert api.engine_start("google", "m.mp3", "구조역학", True, "ko", q["token"])["ok"]
    # nothing is stored for a blank course (asked each time) or an already-agreed course: no token needed
    blank = _Backend("")
    assert Api(blank, _Library(tmp_path)).transcription_start(True)["ok"] and blank.started
    agreed = _Backend("구조역학", granted=True)
    assert Api(agreed, _Library(tmp_path)).transcription_start(True)["ok"]
    assert Api(agreed, _Library(tmp_path)).transcription_start(False)["ok"]
