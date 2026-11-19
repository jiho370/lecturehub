"""Run the app's screen (ui/web) in a normal browser, without pywebview (dev tool; Linux/cloud friendly).

  serve   build a synthetic semester (Korean + space path, timetable with 기초어학, a short sine recording with a
          synthetic transcript, generated slide PDFs, a synthetic 암기장 page), then serve ui/web on
          http://127.0.0.1:PORT/ with ``window.pywebview.api`` bridged to the real ``Api`` object over HTTP.
          /memo serves the 암기장 page chosen in the app, with mem_load/mem_save bridged to the real MemoStore.

Nothing here touches the real semester folder or sends anything out: the recorder uses its synthetic source
(fake engine), Gemini is faked, and the background loops of ``app.main()`` (resume jobs, notices, prewarm)
are not started. Calls that need a native window (file dialogs, new windows) return ``ok: false`` with
"headless" in the error and are printed as ``API-NATIVE`` on stderr.

Usage: .venv/bin/python tools/ui_headless.py serve [--port 8765] [--scratch DIR]
Then open http://127.0.0.1:8765/ in headless chromium (see tools/ui_headless_check.js).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import traceback
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WEB = REPO / "src" / "lecturehub" / "ui" / "web"
SHIM = """<script>
(() => {
  const call = (name) => async (...args) => {
    const r = await fetch('/api/' + name, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(args) });
    if (!r.ok) throw new Error('api ' + name + ' ' + r.status);
    return r.json();
  };
  window.pywebview = { api: new Proxy({}, { get: (t, name) => (typeof name === 'string' ? call(name) : undefined) }) };
})();
</script>"""

# The memorizer window gets only mem_load/mem_save (MemoBridge); pywebview injects its api a moment after load.
MEMO_SHIM = """<script>
setTimeout(() => {
  const call = (name) => async (...args) => (await fetch('/memo-api/' + name, { method: 'POST', body: JSON.stringify(args) })).json();
  window.pywebview = { api: { mem_load: call('mem_load'), mem_save: call('mem_save') } };
  window.dispatchEvent(new Event('pywebviewready'));
}, 150);
</script>"""

MEMO_PAGE = """<!doctype html><html lang="ko"><head><meta charset="utf-8"><title>합성 암기장</title></head>
<body><h1>합성 암기장 (시험용)</h1><p id="sync">이 기기에 저장</p>
<script>
// 시험용 합성 페이지: 진짜 암기장의 저장소 어댑터와 같은 약속(mem_load/mem_save, v:1)만 흉내 낸다.
</script></body></html>
"""


# fictional semester for screenshots and the contest demo (--demo): no real course, person or lecture
DEMO = {
    "courses": [("물의 과학", "물의 과학 (가상)"), ("도시환경 입문", "도시환경 입문 (가상)"), ("기초 외국어", "기초 외국어 1 (가상)")],
    "title": "Week {week} Water Systems - {topic}",
    "topics": ((4, "Viscous flow"), (5, "Pipe friction")),
    "lines": ["가상 강의를 시작합니다. 오늘은 물이 관을 흐르는 모습을 봅니다.",
              "슬라이드 3쪽의 그림처럼, 속도가 빨라지면 압력이 낮아집니다. 이것을 베르누이 식이라고 부릅니다.",
              "관이 거칠수록 에너지를 더 잃습니다. 이 손실은 다음 시간에 계산해 봅니다.",
              "중간 점검 퀴즈가 다음 주 화요일에 있습니다. 범위는 오늘까지입니다(가상).",
              "질문 있으면 해 주세요. 없으면 여기서 마칩니다."],
}
SYNTH = {  # the default dev profile
    "courses": [("구조역학", "구조역학 및 실험(합성)"), ("기초어학", "기초어학 1(합성)"), ("토질역학", "수자원설계(합성)")],
    "title": "Week {week} Hydraulics - {topic}",
    "topics": ((4, "Viscous flow"), (5, "Pipe friction")),
    "lines": ["합성 강의를 시작합니다. 오늘은 가상의 관로 흐름을 봅니다.",
              "베르누이 식을 합성 예제로 설명합니다.", "마찰 손실은 다음 시간에 이어서 봅니다.",
              "과제는 다음 주 화요일까지입니다(합성).", "질문 있으면 해 주세요. 합성 문장입니다."],
}


def build_root(scratch: Path, demo: bool = False) -> tuple[Path, Path]:
    profile = DEMO if demo else SYNTH
    (main, main_name), (second, second_name), (third, third_name) = profile["courses"]
    root = scratch / ("학기 (가상 데모)" if demo else "학기 (합성 헤드리스)")
    (root / "_lecturehub").mkdir(parents=True, exist_ok=True)
    now = datetime.now().astimezone()
    today = "월화수목금토일"[now.weekday()]
    later = (now + timedelta(hours=2)).strftime("%H:%M")
    later_end = (now + timedelta(hours=3)).strftime("%H:%M")
    if later < now.strftime("%H:%M"):
        later, later_end = "23:00", "23:50"
    (root / "시간표.csv").write_text(
        f"\ufeff폴더,과목,요일,시작,종료\n{main},{main_name},화,10:00,11:50\n"
        f"{second},{second_name},월,14:00,15:15\n"
        f"{third},{third_name},{today},{later},{later_end}\n", encoding="utf-8")
    day = now.date() - timedelta(days=(now.weekday() - 1) % 7 or 7)  # the last Tuesday before today
    name = f"{day.year % 100}.{day.month}.{day.day} {main}"
    (root / "녹음본" / main).mkdir(parents=True, exist_ok=True)
    (root / "전사본" / main).mkdir(parents=True, exist_ok=True)
    audio = root / "녹음본" / main / f"{name}.m4a"
    if not audio.exists():
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=90",
                        "-c:a", "aac", "-b:a", "48k", str(audio)], check=True)
    lines = []
    for k, text in enumerate(profile["lines"]):
        lines.append(f"[{k * 15 // 60:02d}:{k * 15 % 60:02d}] \n{text}\n")
    (root / "전사본" / main / f"{name}.txt").write_text("\ufeff" + "\n".join(lines), encoding="utf-8")
    try:
        import pymupdf

        (root / main).mkdir(exist_ok=True)
        for week, topic in profile["topics"]:
            target = root / main / (profile["title"].format(week=week, topic=topic) + ".pdf")
            if target.exists():
                continue
            document = pymupdf.open()
            for n in range(1, 7):
                page = document.new_page(width=960, height=540)
                page.insert_text((60, 90), f"Week {week} - {topic} (synthetic)", fontsize=34)
                page.insert_text((60, 150), f"page {n} / 6", fontsize=22)
            document.save(target)
            document.close()
    except ImportError:
        pass
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONPATH": str(REPO / "src")}
    subprocess.run([sys.executable, "-m", "lecturehub", "index", str(root)], check=True, capture_output=True, env=env)
    (root / "_lecturehub" / "synthetic_root").write_text("synthetic headless library", encoding="utf-8")
    memo = scratch / "암기장 (합성)" / "local.html"
    memo.parent.mkdir(parents=True, exist_ok=True)
    memo.write_text(MEMO_PAGE, encoding="utf-8")
    config = scratch / "headless.toml"
    config.write_text("\n".join([
        "[recorder]", f"out_dir = '{root / '_lecturehub' / 'recordings'}'", 'device_name = "FakeSource"',
        'mode = "mic"', f"timetable = '{root / '시간표.csv'}'"]), encoding="utf-8")
    return root, config


def make_api(config: Path):
    os.environ["LECTUREHUB_RECORDER_CONFIG"] = str(config)
    os.environ["LECTUREHUB_FAKE_GEMINI"] = "1"
    sys.path.insert(0, str(REPO / "src"))
    from lecturehub.ui.app import Api, open_library, open_room
    from lecturehub.ui.backend import Backend
    from lecturehub.ui.media_server import MediaServer

    backend = Backend(fake_engine=True)
    api = Api(backend, open_library(backend.root), MediaServer(), open_room(backend))
    backend.discard_hook = lambda name: api._library.discard_session(name)
    return api


def serve(port: int, scratch: Path, first_run: bool = False, demo: bool = False) -> None:
    # Every demo profile stays inside scratch, including credentials and agent workspace.
    scratch.mkdir(parents=True, exist_ok=True)
    os.environ["LECTUREHUB_SETTINGS"] = str(scratch / "settings.json")
    os.environ["LECTUREHUB_AGENT_WORKSPACE"] = str(scratch / "agent-workspace")
    os.environ.pop("LECTUREHUB_ROOT", None)
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GROQ_API_KEY", "LECTUREHUB_ETL_TOKEN", "OPENAI_API_KEY", "CODEX_API_KEY"):
        os.environ.pop(name, None)
    import keyring

    keyring.get_password = lambda *args: None
    if first_run:  # a brand-new PC: no settings file, no recorder.toml, nothing chosen yet (all inside the scratch folder)
        fresh = scratch / "first-run-appdata"
        fresh.mkdir(parents=True, exist_ok=True)
        os.environ["LECTUREHUB_SETTINGS"] = str(fresh / "settings.json")
        os.environ["LECTUREHUB_RECORDER_CONFIG"] = str(fresh / "recorder.toml")
        os.environ["LECTUREHUB_AGENT_WORKSPACE"] = str(fresh / "agent-workspace")
        os.environ.pop("LECTUREHUB_ROOT", None)
        os.environ["LECTUREHUB_FAKE_GEMINI"] = "1"
        sys.path.insert(0, str(REPO / "src"))
        from lecturehub.ui.app import Api
        from lecturehub.ui.backend import Backend
        from lecturehub.ui.media_server import MediaServer

        root = scratch / "first-run-docs"
        api = Api(Backend(fake_engine=True), None, MediaServer(), None)
        api.setup_pick_folder = lambda: {"ok": True, "path": str(root)}  # no native dialog in a browser
        api.setup_close = lambda: {"ok": True}
    else:
        root, config = build_root(scratch, demo)
        api = make_api(config)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, body: bytes, kind: str):
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.split("?")[0] == "/memo":  # the page picked in the 암기 panel, as its own window would load it
                info = api.memo()
                if info["state"] != "ready":
                    return self._send(404, info["message"].encode("utf-8"), "text/plain; charset=utf-8")
                body = Path(info["path"]).read_bytes()
                return self._send(200, MEMO_SHIM.encode() + body, "text/html; charset=utf-8")
            rel = self.path.split("?")[0].lstrip("/") or "index.html"
            target = (WEB / rel).resolve()
            if not target.is_relative_to(WEB.resolve()) or not target.is_file():
                return self._send(404, b"not found", "text/plain")
            body = target.read_bytes()
            if rel == "index.html":  # the page's CSP forbids inline scripts and fetch: this dev bridge needs both
                import re

                body = re.sub(rb'<meta http-equiv="Content-Security-Policy"[^>]*>', b"", body, count=1)
            if rel == "index.html" or rel == "mini.html":
                body = body.replace(b'<script src="app.js"></script>', SHIM.encode() + b'<script src="app.js"></script>', 1)
                if SHIM.encode() not in body:  # mini.html has its own inline script
                    body = body.replace(b"<head>", b"<head>" + SHIM.encode(), 1)
            kinds = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                     ".css": "text/css; charset=utf-8", ".woff2": "font/woff2", ".svg": "image/svg+xml"}
            self._send(200, body, kinds.get(target.suffix, "application/octet-stream"))

        def do_POST(self):
            args = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"[]")
            if self.path.startswith("/memo-api/"):
                from lecturehub.ui.memo import MemoBridge

                bridge = MemoBridge(api._memo_store())
                name = self.path.rsplit("/", 1)[-1]
                result = getattr(bridge, name)(*args) if name in ("mem_load", "mem_save") else {"ok": False}
                return self._send(200, json.dumps(result, ensure_ascii=False).encode("utf-8"), "application/json")
            name = self.path.split("/api/", 1)[-1]
            offline = {"conn_connect", "conn_test", "conn_remove", "make_note", "note_claude", "note_agent",
                       "room_refresh", "room_book", "room_chat", "room_chat_confirm", "room_cancel", "room_return",
                       "captions_start", "qa_start", "engine_start", "transcription_start", "etl_sync", "etl_files_sync",
                       "clova_fetch", "clova_check", "clova_sync", "etl_file_take"}
            if name in offline:
                return self._send(200, json.dumps({"ok": False, "error": "가상 데모에서는 외부 서비스를 실행하지 않습니다."}, ensure_ascii=False).encode(), "application/json")
            method = getattr(api, name, None)
            if name.startswith("_") or not callable(method):
                return self._send(404, json.dumps({"ok": False, "error": f"no api {name}"}).encode(), "application/json")
            try:
                result = method(*args)
            except Exception as exc:  # noqa: BLE001 - report, keep serving
                native = isinstance(exc, (AttributeError, ImportError, ModuleNotFoundError)) and (
                    "_main" in str(exc) or "webview" in str(exc) or "NoneType" in str(exc))
                print(("API-NATIVE " if native else "API-ERROR ") + name + ": " + repr(exc), file=sys.stderr, flush=True)
                if not native:
                    traceback.print_exc()
                result = {"ok": False, "error": f"headless: {exc}"}
            self._send(200, json.dumps(result, ensure_ascii=False, default=str).encode("utf-8"), "application/json")

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(json.dumps({"url": f"http://127.0.0.1:{port}/", "root": str(root),
                      "memo_page": str(scratch / "암기장 (합성)" / "local.html")}, ensure_ascii=False), flush=True)
    server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["serve"])
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--scratch", type=Path, default=None)
    parser.add_argument("--demo", action="store_true", help="a fictional semester (no real course or person) for screenshots")
    parser.add_argument("--first-run", action="store_true", help="serve the first-run wizard on an empty, scratch-only profile")
    args = parser.parse_args()
    scratch = args.scratch or Path(tempfile.mkdtemp(prefix="lh-headless-"))
    serve(args.port, scratch, args.first_run, args.demo)


if __name__ == "__main__":
    main()
