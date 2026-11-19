"""Problem report file (release review 2026-11-16).

When the app misbehaves on a user's PC the developer cannot look at that PC. The user can press one button in
settings, see exactly which files would go in, and get one zip to send by hand. Nothing is ever sent by the app.

In the zip: ``info.json`` (app version, Windows version, which tools and connections are present, a few settings,
the library's schema and row counts, the audio devices) and the app's own logs (core/applog.py).

Never in the zip: recordings, transcripts, notes, slides, keys, course names, file names of the user's lectures.
Paths are masked before anything is written: everything under the semester folder becomes ``<학기폴더>\\…`` and
the user's home folder becomes ``<사용자폴더>``, so neither the Windows user name nor a course or file name inside
a path leaves the PC.
"""
from __future__ import annotations

import json
import os
import platform
import re
import sys
import zipfile
from datetime import datetime
from pathlib import Path

from . import applog

LOG_TAIL_BYTES = 400_000  # per log file: the newest part is what explains the problem
EXCLUDED = ["녹음 파일", "전사", "정리본·노트", "강의자료", "API 키·토큰", "과목 이름과 파일 이름(경로는 가려서 넣습니다)",
            "전사 작업 기록(_lecturehub\\logs: 파일 이름이 들어 있어 넣지 않습니다)"]


def masker(root: Path | None, home: Path | None = None):
    """A function that hides the semester folder (and everything under it) and the home folder in a text."""
    rules: list[tuple[re.Pattern, str]] = []
    for base, label, whole in ((root, "<학기폴더>", True), (home or Path.home(), "<사용자폴더>", False)):
        if not base:
            continue
        for form in {str(base), str(base).replace("\\", "/"), str(base).replace("\\", "\\\\")}:
            tail = r"(?:[\\/][^\r\n\"'<>|]*)?" if whole else ""
            rules.append((re.compile(re.escape(form) + tail, re.IGNORECASE), label + ("\\…" if whole else "")))

    def mask(text: str) -> str:
        for pattern, label in rules:
            text = pattern.sub(lambda _m, label=label: label, text)
        return applog.redact(text)

    return mask


def _report_dir() -> Path:
    return applog.log_dir().parent / "reports"


def _log_files() -> list[Path]:
    folder = applog.log_dir()
    names = ["app.log", "app.1.log", "worker.log", "worker.1.log", "app_crash.log", "worker_crash.log"]
    found = [folder / name for name in names if (folder / name).is_file() and (folder / name).stat().st_size]
    try:
        from . import settings

        for name in ("ui_errors.log",):  # script errors of the window (ui/app.py client_error)
            path = settings.settings_path().parent / name
            if path.is_file() and path.stat().st_size:
                found.append(path)
    except Exception:  # noqa: BLE001
        applog.swallowed("diagnostics: ui_errors.log")
    return found


def _part(name: str, about: str, fill) -> dict:
    """One section of info.json; a section that fails says so instead of breaking the report."""
    try:
        return {name: fill()}
    except Exception as exc:  # noqa: BLE001
        applog.swallowed(f"diagnostics: {about}")
        return {name: {"확인 실패": type(exc).__name__}}


def info(root: Path | None) -> dict:
    """What info.json holds. No names of people, courses or files; counts and versions only."""
    from lecturehub import __version__

    from . import edition

    out: dict = {"made": datetime.now().astimezone().isoformat(timespec="seconds"),
                 "app": {"version": __version__, "edition": edition.name(), "installed_build": bool(getattr(sys, "frozen", False))},
                 "pc": {"windows": platform.platform(), "machine": platform.machine(), "python": platform.python_version()}}

    def tools_part():
        from . import tools

        return {name: bool(row["found"]) for name, row in tools.status(check_login=False).items()}

    def connections_part():
        from . import connections

        return {row["id"]: {"connected": bool(row.get("connected")), "source": row.get("source")}
                for row in connections.status()}

    def settings_part():
        from . import settings

        data = settings.load()
        safe = {key: data.get(key) for key in ("folder_layout", "labs", "trial", "keep_lossless", "term") if key in data}
        safe["keys_present"] = sorted(data)
        return safe

    def folder_part():
        if root is None:
            return {"chosen": False}
        import shutil

        from lecturehub.ui import setup  # the wizard's own test, so the report and the wizard's note agree

        exists = Path(root).is_dir()
        free = shutil.disk_usage(root).free if exists else None
        return {"chosen": True, "exists": exists, "free_gb": round(free / 2**30, 1) if free is not None else None,
                "looks_cloud_synced": setup.cloud_synced(Path(root))}

    def library_part():
        from . import db
        from .paths import database_path

        path = database_path(root) if root else None
        if not path or not path.is_file():
            return {"exists": False}
        import sqlite3
        from contextlib import closing

        with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as connection:
            version = max((row[0] for row in connection.execute("SELECT version FROM schema_version")), default=0)
            counts = {table: connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]  # noqa: S608 - fixed names
                      for table in ("Course", "Lecture", "MediaAsset", "Transcript")}
        return {"exists": True, "schema": version, "app_knows": db.known_schema_version(), "rows": counts,
                "size_mb": round(path.stat().st_size / 1e6, 1)}

    def audio_part():
        from lecturehub.recorder import devices

        return [{"name": d.name, "hz": d.sample_rate, "channels": d.channels, "pc_sound": d.is_loopback,
                 "default": d.is_default} for d in devices.list_devices()]

    for name, about, fill in (("tools", "프로그램", tools_part), ("connections", "계정 연결", connections_part),
                              ("settings", "설정", settings_part), ("semester_folder", "학기 폴더", folder_part),
                              ("library", "수업 목록", library_part), ("audio_devices", "소리 장치", audio_part)):
        out.update(_part(name, about, fill))
    return out


def plan(root: Path | None) -> dict:
    """What the report would contain, for the user to read before it is made."""
    files = [{"name": "info.json", "about": "앱·Windows 버전, 연결된 프로그램과 계정(있음/없음), 설정 몇 가지, 수업 목록의 개수, 소리 장치 이름"}]
    for path in _log_files():
        files.append({"name": f"logs/{path.name}", "about": "앱이 남긴 기록(시작, 경고, 오류). 경로와 키는 가립니다.",
                      "bytes": min(path.stat().st_size, LOG_TAIL_BYTES)})
    return {"files": files, "excluded": EXCLUDED, "folder": str(_report_dir())}


def make(root: Path | None, dest: Path | None = None) -> Path:
    """Write the report zip and return its path."""
    mask = masker(Path(root) if root else None)
    folder = Path(dest) if dest else _report_dir()
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"LectureHub-report-{datetime.now():%Y%m%d-%H%M%S}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("info.json", mask(json.dumps(info(root), ensure_ascii=False, indent=1, default=str)))
        for path in _log_files():
            try:
                with path.open("rb") as handle:
                    size = path.stat().st_size
                    handle.seek(max(0, size - LOG_TAIL_BYTES))
                    text = handle.read().decode("utf-8", errors="replace")
            except OSError:
                continue
            archive.writestr(f"logs/{path.name}", mask(text))
        archive.writestr("README.txt", "Lecture Hub 문제 보고 파일입니다. 앱이 자동으로 보내지 않습니다.\n"
                                       "들어 있지 않은 것: " + ", ".join(EXCLUDED) + "\n")
    applog.LOG.info("문제 보고 파일을 만들었습니다")
    return target


def log_folder() -> Path:
    folder = applog.log_dir()
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def data_folders(root: Path | None) -> dict:
    """Where this app keeps things on this PC (the about box shows them; uninstalling leaves them)."""
    from . import settings

    return {"semester": str(root) if root else "", "settings": str(settings.settings_path().parent),
            "logs": str(applog.log_dir()), "local": os.environ.get("LOCALAPPDATA", "")}
