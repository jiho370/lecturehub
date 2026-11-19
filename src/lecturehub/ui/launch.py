"""How the installed app starts (LectureHub.exe → packaging/launcher_app.py → here). Release review 2026-11-16.

A windowed exe has no console: before this file a failure before the window appeared left the user looking at
nothing. Now:
- logging starts first (core/applog.py);
- a PC without the WebView2 Runtime gets a message that says what to install;
- any other start-up failure gets a message with where the log is;
- ``--smoke [--out FILE]`` runs the self-check (core/selfcheck.py, one hidden window) and writes its JSON —
  used by the automatic build, never by users.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

TITLE = "Lecture Hub"


def message_box(text: str, *, error: bool = True) -> None:
    if sys.platform != "win32":
        return
    import ctypes

    ctypes.windll.user32.MessageBoxW(None, text, TITLE, 0x10 if error else 0x40)


def _smoke(argv: list[str]) -> int:
    from lecturehub.core import selfcheck

    result = selfcheck.run(window=True)
    if "--out" in argv and argv.index("--out") + 1 < len(argv):
        Path(argv[argv.index("--out") + 1]).write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0 if result["ok"] else 1


def run(argv: list[str] | None = None, *, show=message_box) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    from lecturehub.core import applog, webview2

    log = applog.setup("app")
    try:
        if "--smoke" in argv:
            return _smoke(argv)
        if sys.platform == "win32" and getattr(sys, "frozen", False) and webview2.version() is None:
            applog.LOG.error("WebView2 런타임이 없어 창을 띄울 수 없음")
            show("화면을 그리는 Windows 부품(Microsoft Edge WebView2 Runtime)이 이 PC에 없습니다.\n\n"
                 f"아래 주소에서 'Evergreen Bootstrapper'를 받아 설치한 뒤 다시 실행해 주세요.\n{webview2.DOWNLOAD}")
            return 2
        from lecturehub.ui.app import main

        return int(main() or 0)
    except SystemExit:
        raise
    except BaseException:  # noqa: BLE001 - the user must be told, whatever it was
        applog.LOG.critical("앱을 시작하지 못함", exc_info=True)
        where = f"\n\n기록 파일: {log}" if log else ""
        show("Lecture Hub를 시작하지 못했습니다. 다시 실행해도 같으면 앱을 다시 설치해 보세요."
             f"{where}\n\n녹음 파일과 학기 폴더는 그대로 있습니다.")
        return 1
