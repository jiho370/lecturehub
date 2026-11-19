"""Self-check of a built app, without showing anything (release review 2026-11-16).

``LectureHubWorker.exe selfcheck`` (or ``lecturehub selfcheck`` from source) walks what a brand-new user's first
minutes need, in a temporary folder and with its own temporary settings, and says which step failed. The
automatic build runs it on a clean Windows machine; a build that fails it is not released.

It never touches the real settings, the real semester folder or the network, and it opens no window unless
``window=True`` (then two hidden windows, the main page and the small recording window: do they work in WebView2?).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import date
from pathlib import Path

REQUIRED_MODULES = ("webview", "keyring", "keyring.backends.Windows", "pyaudiowpatch", "httpx", "pypdfium2", "PIL",
                    "numpy", "typer", "truststore")
FORBIDDEN_MODULES = ("pymupdf", "fitz")  # AGPL: dev-only, must not be in an app that ships


def _step(results: list, name: str, run) -> bool:
    started = time.monotonic()
    try:
        detail = run()
        results.append({"step": name, "ok": True, "detail": detail, "seconds": round(time.monotonic() - started, 2)})
        return True
    except Exception as exc:  # noqa: BLE001 - every failure is a result line
        import traceback

        results.append({"step": name, "ok": False, "detail": f"{type(exc).__name__}: {exc}",
                        "trace": traceback.format_exc(limit=6), "seconds": round(time.monotonic() - started, 2)})
        return False


def run(*, window: bool = False, keep: bool = False) -> dict:
    """Run every step and return ``{"ok": bool, "steps": [...]}``. Environment changes are undone afterwards."""
    results: list[dict] = []
    scratch = Path(tempfile.mkdtemp(prefix="lecturehub-selfcheck-"))
    saved = {name: os.environ.get(name) for name in ("LECTUREHUB_SETTINGS", "LECTUREHUB_RECORDER_CONFIG",
                                                     "LECTUREHUB_ROOT", "LECTUREHUB_AGENT_WORKSPACE",
                                                     "LECTUREHUB_NO_AUTO_COMPACT", "LECTUREHUB_UPDATE_REPO")}
    os.environ["LECTUREHUB_SETTINGS"] = str(scratch / "appdata" / "settings.json")
    os.environ["LECTUREHUB_RECORDER_CONFIG"] = str(scratch / "appdata" / "recorder.toml")
    os.environ["LECTUREHUB_AGENT_WORKSPACE"] = str(scratch / "agent-workspace")
    os.environ["LECTUREHUB_NO_AUTO_COMPACT"] = "1"  # nothing of the check goes to the real Recycle Bin
    os.environ["LECTUREHUB_UPDATE_REPO"] = ""  # the page asks for a newer version when it starts: not from a check
    os.environ.pop("LECTUREHUB_ROOT", None)
    try:
        _step(results, "앱 정보", _about)
        _step(results, "필요한 부품 불러오기", _imports)
        _step(results, "들어가면 안 되는 부품 없음", _forbidden)
        _step(results, "ffmpeg·ffprobe 실행", _ffmpeg)
        _step(results, "WebView2 런타임", _webview2)
        _step(results, "도우미 프로그램 실행", _worker)
        root_holder: dict = {}
        if _step(results, "첫 설정 → 수업 목록 만들기", lambda: _first_run(scratch, root_holder)):
            _step(results, "수업 목록 읽기", lambda: _sidebar(root_holder["root"]))
        trial: dict = {}
        _step(results, "체험 학기 만들기(녹음·전사·정리본 흐름)", lambda: _trial(scratch, trial))
        _step(results, "도우미 작업 폴더", _agent_workspace)
        _step(results, "문제 보고 파일", lambda: _report(scratch, root_holder.get("root")))
        if window:  # last: it points the temporary settings at the trial semester
            _step(results, "실제 화면 걷기(숨김 창: 회차·녹음·정리본·설정·미니 창·주소 고정)",
                  lambda: _window(trial.get("root")))
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        if not keep:
            shutil.rmtree(scratch, ignore_errors=True)
    return {"ok": all(row["ok"] for row in results), "steps": results}


def _about() -> str:
    from . import applog

    return applog.describe()


def _imports() -> str:
    import importlib

    for name in REQUIRED_MODULES:
        importlib.import_module(name)
    return f"{len(REQUIRED_MODULES)}개"


def _forbidden() -> str:
    import importlib.util

    if not getattr(sys, "frozen", False):
        return "소스 실행: 확인 대상 아님(개발용 부품이 있을 수 있음)"
    found = [name for name in FORBIDDEN_MODULES if importlib.util.find_spec(name) is not None]
    if found:
        raise RuntimeError(f"배포하면 안 되는 부품이 들어 있습니다: {found}")
    return "없음"


def _ffmpeg() -> str:
    from . import proc, tools

    lines = []
    for name in ("ffmpeg", "ffprobe"):
        path = tools.find(name)
        if not path:
            raise RuntimeError(f"{name}를 찾을 수 없습니다")
        done = subprocess.run([path, "-version"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=30, creationflags=proc.NO_WINDOW)
        if done.returncode:
            raise RuntimeError(f"{name} 실행 실패({done.returncode})")
        bundled = getattr(sys, "frozen", False) and Path(path).parent == Path(sys.executable).parent / "bin"
        lines.append(f"{name}: {(done.stdout.splitlines() or ['?'])[0][:60]}{' (앱에 든 것)' if bundled else ''}")
    return " / ".join(lines)


def _webview2() -> str:
    from . import webview2

    found = webview2.version()
    if not found:
        raise RuntimeError("WebView2 런타임이 없습니다")
    return found


def _worker() -> str:
    from lecturehub import __version__

    from . import proc

    done = subprocess.run([proc.console_python(), "-m", "lecturehub", "version"], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=120, creationflags=proc.NO_WINDOW)
    if done.returncode or __version__ not in done.stdout:
        raise RuntimeError(f"도우미가 버전을 답하지 않았습니다({done.returncode}): {done.stdout[-200:]}{done.stderr[-300:]}")
    return Path(proc.console_python()).name


def _first_run(scratch: Path, holder: dict) -> str:
    from lecturehub.ui import setup

    from .paths import database_path

    result = setup.apply(str(scratch / "문서 폴더"), "26-2", [
        {"name": "합성 과목", "slots": [{"day": "월", "start": "09:00", "end": "10:15"}]}, {"name": "시간 없는 과목", "slots": []}])
    root = Path(result["root"])
    holder["root"] = root
    if not database_path(root).is_file():
        raise RuntimeError("설정을 마쳤는데 수업 목록 DB가 없습니다")
    from lecturehub.recorder.classconfig import load_config

    config = load_config()
    if Path(config.out_dir) != root / "_lecturehub" / "recordings":
        raise RuntimeError("녹음 저장 위치가 새 학기 폴더를 가리키지 않습니다")
    if result.get("extra") != ["시간 없는 과목"]:
        raise RuntimeError("수업 시간이 없는 과목이 '수업 외'로 들어가지 않았습니다")
    return "DB·시간표·녹음 설정 생성, 시간 없는 과목은 '수업 외'"


def _sidebar(root: Path) -> str:
    from lecturehub.ui.library import Library

    shown = Library(root).sidebar(date.today())
    if not shown.get("ok"):
        raise RuntimeError(shown.get("error") or "수업 목록을 읽지 못했습니다")
    return f"과목 {len(shown['courses'])}개"


def _trial(scratch: Path, holder: dict) -> str:
    from lecturehub.index import demo

    made = demo.make(scratch / "체험 학기")
    if made.get("state") not in ("made", "ready"):
        raise RuntimeError(f"체험 학기 상태: {made.get('state')}")
    holder["root"] = Path(made["root"])
    return str(made.get("state"))


def _agent_workspace() -> str:
    from . import agent_workspace, proc

    folder = agent_workspace.ensure()
    env = agent_workspace.launch_env(folder)
    if env["LECTUREHUB_PY"] != proc.console_python():
        raise RuntimeError("도우미에게 알려 주는 실행 파일이 잘못되었습니다")
    return f"스킬 {len(agent_workspace.skill_names())}개"


def _report(scratch: Path, root: Path | None) -> str:
    import zipfile

    from . import diagnostics

    target = diagnostics.make(root, dest=scratch / "reports")
    with zipfile.ZipFile(target) as archive:
        names = archive.namelist()
        json.loads(archive.read("info.json").decode("utf-8"))
    return f"파일 {len(names)}개"


READY_WAIT_S = 120.0  # a cold WebView2 on a busy build machine is slow; a healthy app is ready in a few seconds

# what the hidden window is asked, in order. Each is one expression: ``do`` is repeated until it answers 'ok' (the
# thing to press is drawn a moment after the page is ready), then ``wait`` is polled until it is true.
_WALK = (
    {"name": "과목 펼치기", "do": "(function(){var e=document.querySelector('#courseList [data-toggle]');"
                               "if(!e)return 'none';if(!e.classList.contains('open'))e.click();return 'ok'})()",
     "wait": "!!document.querySelector('#courseList [data-lecture]')"},
    {"name": "회차 열기", "do": "(function(){var e=document.querySelector('#courseList [data-lecture]');"
                              "if(!e)return 'none';e.click();return 'ok'})()",
     "wait": "document.getElementById('lvList') && document.getElementById('lvList').style.display !== 'none'"},
    # A window that was never shown does not load sound at all (WebView2 keeps <audio> at "loading" while the page is
    # hidden, with or without a policy; measured 2026-11-16). So this step checks what a hidden window can show: the
    # player got its address on the local media server and nothing was refused. That the policy lets the sound itself
    # load is tested in a page that counts as visible (tests/test_release_csp.py, windowless Edge).
    {"name": "녹음 듣기 준비", "do": "(function(){var a=document.getElementById('audio');return a&&a.src?'ok':'none'})()",
     "wait": "(function(){var a=document.getElementById('audio');return a.src.indexOf('http://127.0.0.1:') === 0 "
             "&& a.src.indexOf('/m/') > 0 && !a.error && "
             "document.getElementById('player').className === 'player'})()",
     "why": "(function(){var a=document.getElementById('audio');return 'src=' + String(a.src).slice(0, 60) + "
            "' error=' + (a.error ? a.error.code + ' ' + a.error.message : '-')})()"},
    {"name": "정리본 열기", "do": "(function(){var t=document.getElementById('htmlTab');if(!t||t.hidden)return 'none';"
                               "t.click();return 'ok'})()",
     "wait": "(function(){var f=document.getElementById('htmlFrame');return !!(f && f.contentDocument && "
             "f.contentDocument.body && f.contentDocument.body.innerText.length > 200)})()"},
    {"name": "설정 열기", "do": "(function(){document.getElementById('settingsBtn').click();return 'ok'})()",
     "wait": "!!document.querySelector('.scrim') && document.body.innerText.indexOf('앱 정보') >= 0"},
)
_BROKEN_IMAGES = ("Array.prototype.filter.call(document.images,function(i){return i.getAttribute('src') && i.complete && "
                  "i.naturalWidth === 0}).map(function(i){return String(i.src).slice(0, 80)}).join(' | ')")
# The small recording window (mini.html) has drawn itself from the real API at least once: its script empties the
# level text ("-∞ dB" in the markup) when nothing is recording, and writes the button label.
_MINI_DRAWN = ("(function(){var d=document.getElementById('db'),l=document.getElementById('label');"
               "return !!(d && l && d.textContent === '' && l.textContent === '녹음 시작')})()")
# An address that is not the app's page. Nothing is asked of it: the window must refuse before any request, and if
# it did not, the engine itself does not connect to this port.
_LEAVE_TO = "http://127.0.0.1:9/lecturehub-selfcheck-leave"
_STILL_HOME = "window.__lecturehubReady === true && location.pathname.indexOf('index.html') >= 0"


def _window(trial_root: Path | None = None) -> str:
    """Hidden windows with the REAL pages and the REAL API on the trial semester (fake sound engine): the main page
    must finish starting, open a lecture with its sound and its note, and open the settings, without one thing
    being blocked by the page's own security policy and without a script error; the small recording window must
    draw itself; and the main window must refuse to leave the app's page. This is what would catch a policy (or a
    packaging slip) that breaks a window while every unit test still passes."""
    import threading

    import webview

    from lecturehub.ui.app import WEB, Api, guard_navigation, open_library
    from lecturehub.ui.backend import Backend
    from lecturehub.ui.media_server import MediaServer

    from . import settings

    if trial_root is not None:  # show the trial semester: it has a lecture with sound, slides and a note
        from lecturehub.ui.setup import write_recorder_config

        table = trial_root / "시간표.csv"
        write_recorder_config(trial_root, table if table.is_file() else None, replace=True)
        settings.save({"data_root": str(trial_root), "trial": True})

    reported: list[str] = []

    class Watched(Api):
        def client_error(self, text):  # the page reports blocked things ("csp: ...") and script errors here
            reported.append(str(text)[:300])
            return super().client_error(text)

    backend = Backend(fake_engine=True)
    api = Watched(backend, open_library(backend.root), MediaServer(), None)
    seen: dict = {"steps": []}
    refused: list[str] = []
    window = webview.create_window("Lecture Hub self-check", url=str(WEB / "index.html"), js_api=api, hidden=True,
                                   width=1400, height=900)
    mini = webview.create_window("Lecture Hub self-check mini", url=str(WEB / "mini.html"), js_api=api, hidden=True,
                                 width=340, height=150)
    api._main, api._mini = window, mini
    guard_navigation(window, on_refused=refused.append)  # as the app does (ui/app.py main)
    guard_navigation(mini)

    def until(expression: str, seconds: float, page=window) -> bool:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if page.evaluate_js(expression):
                return True
            time.sleep(0.4)
        return False

    def walk():
        try:
            started = time.monotonic()
            if not window.events.loaded.wait(READY_WAIT_S) or not until("window.__lecturehubReady === true", READY_WAIT_S):
                seen["error"] = f"화면이 {int(READY_WAIT_S)}초 안에 준비되지 않았습니다"
                return
            seen["ready_s"] = round(time.monotonic() - started, 1)
            for step in _WALK if trial_root is not None else ():
                if not until(f"({step['do']}) === 'ok'", 40):
                    seen["error"] = f"{step['name']}: 누를 것이 화면에 없습니다"
                    return
                if not until(step["wait"], 40):
                    why = window.evaluate_js(step["why"]) if step.get("why") else ""
                    seen["error"] = f"{step['name']}: 눌렀는데 화면이 따라오지 않았습니다 {why}".rstrip()
                    return
                seen["steps"].append(step["name"])
            if trial_root is not None:
                time.sleep(1.5)  # late violations (images, fonts) arrive after the screen is drawn
                seen["broken_images"] = window.evaluate_js(_BROKEN_IMAGES)
            if not mini.events.loaded.wait(READY_WAIT_S) or not until(_MINI_DRAWN, 40, mini):
                seen["error"] = "미니 녹음 창이 그려지지 않았습니다"
                return
            seen["steps"].append("미니 창")
            # last, because a window that did leave is of no further use: the app's page must not be left
            window.evaluate_js(f"location.href = {json.dumps(_LEAVE_TO)}")
            end = time.monotonic() + 20
            while not refused and time.monotonic() < end:
                time.sleep(0.2)
            if not refused or not window.evaluate_js(_STILL_HOME):
                seen["error"] = f"창이 앱 화면이 아닌 주소로 가는 것을 막지 못했습니다(막은 것: {refused[:1]})"
                return
            seen["steps"].append("밖으로 나가기 막힘")
        except Exception as exc:  # noqa: BLE001
            seen["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                backend.stop_live_helpers()
            finally:
                try:
                    mini.destroy()
                finally:
                    window.destroy()

    threading.Thread(target=walk, daemon=True).start()
    webview.start(private_mode=True)
    blocked = [line for line in reported if line.startswith("csp:")]
    errors = [line for line in reported if line.startswith("error:")]
    if seen.get("error"):
        raise RuntimeError(f"{seen['error']} · 화면이 알린 것: {reported[:5]}")
    if blocked:
        raise RuntimeError(f"화면 보안 정책이 앱 자신의 것을 막았습니다: {blocked[:5]}")
    if errors:
        raise RuntimeError(f"화면 스크립트 오류: {errors[:3]}")
    if seen.get("broken_images"):
        raise RuntimeError(f"그림이 뜨지 않았습니다: {seen['broken_images']}")
    return f"준비 {seen.get('ready_s')}초, {' → '.join(seen['steps']) or '첫 화면'}, 막힌 것 없음"
