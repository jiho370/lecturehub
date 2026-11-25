"""Desktop app logic without any webview dependency (testable).

The recorder runs as a separate engine process (lecturehub.recorder.service);
this module starts it, reads its JSON lines and writes its control files.
When the app restarts during a recording it re-attaches through live.json.
"""
from __future__ import annotations

import csv
import io
import json
import math
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from lecturehub.core import applog
from lecturehub.core.fileio import atomic_write_json, atomic_write_text
from lecturehub.core.textio import read_text_any
from lecturehub.recorder.service import BOOKMARK_FILE, DISCARD_FILE, LIVE, PAUSE_FILE, STOP_FILE

WEEKDAYS = "월화수목금토일"
# class notices through Codex (D-073): = index.class_notices.CODEX_PROVIDER, not imported (this module stays
# clear of index/ and the database, D-050)
NOTICES_PROVIDER = "codex_notices"
ETL_NOTICES_PROVIDER = "codex_etl_notices"  # eTL announcements through Codex (D-077) = index.etl_notices.PROVIDER
CLASS_OVER_GRACE_MIN = 5      # "수업이 끝난 것 같아요": timetable end + 5 minutes…
CLASS_OVER_QUIET_MIN = 5      # …or this long with almost no sound
CLASS_OVER_SNOOZE_MIN = 10
AUTO_STOP_QUIET_MIN = 15      # user 2026-09-30 ("조용할 때만 멈춤"): stop and save after this long with no sound
PAUSED_REMIND_MIN = 10        # a pause left on: remind (never stops)
NEXT_CLASS_LEAD_MIN = 10      # user 2026-09-30: offer to switch to the next class this long before it starts
AUDIO_SUFFIXES = {".m4a", ".mp3", ".wav"}
CREATE_NO_WINDOW = 0x08000000
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
BELOW_NORMAL_PRIORITY = 0x00004000  # live transcription never competes with the recorder


def console_python() -> str:
    """The program that takes ``-m lecturehub …`` (core/proc.py: the worker exe in the installed build)."""
    from lecturehub.core import proc

    return proc.console_python()


def spawn(args: list[str], *, stdout=subprocess.PIPE, cwd: str | None = None,
          below_normal: bool = False) -> subprocess.Popen:
    """Start a console child without a window that outlives this process.

    Breakaway lets it survive a job object that kills its members with the
    app (some launchers use one); fall back when the job forbids breakaway.
    """
    flags = (CREATE_NO_WINDOW | (BELOW_NORMAL_PRIORITY if below_normal else 0)) if os.name == "nt" else 0
    common = {"stdin": subprocess.DEVNULL, "stdout": stdout, "stderr": subprocess.STDOUT, "cwd": cwd,
              "env": {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}}
    if os.name == "nt":
        try:
            return subprocess.Popen(args, creationflags=flags | CREATE_BREAKAWAY_FROM_JOB, **common)
        except OSError:
            pass
    return subprocess.Popen(args, creationflags=flags, **common)


# ------------------------------------------------------------------ timetable
@dataclass
class Course:
    folder: str
    name: str
    slots: list[tuple[str, str, str]] = field(default_factory=list)  # (요일, 시작, 종료)
    recordings: int = 0


def load_courses(timetable: Path | None, root: Path) -> list[Course]:
    courses: dict[str, Course] = {}
    if timetable and timetable.is_file():
        for row in csv.DictReader(io.StringIO(read_text_any(timetable))):
            folder = (row.get("폴더") or "").strip()
            if not folder:
                continue
            course = courses.setdefault(folder, Course(folder, (row.get("과목") or folder).strip()))
            course.slots.append(((row.get("요일") or "").strip(), (row.get("시작") or "").strip(),
                                 (row.get("종료") or "").strip()))
    for course in courses.values():
        folder = root / "녹음본" / course.folder
        if folder.is_dir():  # read only: count files, never touch them
            course.recordings = sum(1 for p in folder.iterdir() if p.suffix.lower() in AUDIO_SUFFIXES)
    return list(courses.values())


def today_slots(courses: list[Course], now: datetime) -> list[dict]:
    day = WEEKDAYS[now.weekday()]
    slots = [{"folder": c.folder, "name": c.name, "start": s, "end": e}
             for c in courses for d, s, e in c.slots if d == day]
    return sorted(slots, key=lambda slot: slot["start"])


# ------------------------------------------------------------------ recorder link
class EngineLink:
    """One recording engine: spawned now (stdout JSON lines) or re-attached (live.json)."""

    def __init__(self, on_event):
        self.on_event = on_event
        self.process: subprocess.Popen | None = None
        self.session_dir: Path | None = None
        self.attached = False
        self._thread: threading.Thread | None = None

    def spawn(self, args: list[str], cwd: str | None = None) -> None:
        self.process = spawn(args, cwd=cwd)
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def _read(self) -> None:
        assert self.process and self.process.stdout
        for raw in self.process.stdout:
            try:
                event = json.loads(raw.decode("utf-8", errors="replace"))
            except ValueError:
                continue
            if event.get("type") == "started":
                self.session_dir = Path(event["session_dir"])
            self.on_event(event)
        code = self.process.wait()
        self.on_event({"type": "exited", "code": code})

    def attach(self, session_dir: Path) -> None:
        self.session_dir, self.attached = session_dir, True
        self._thread = threading.Thread(target=self._follow, daemon=True)
        self._thread.start()

    def _follow(self) -> None:
        """Poll live.json of an engine started by an earlier app instance."""
        assert self.session_dir
        last_state = None
        while True:
            live = _read_json(self.session_dir / LIVE) or {}
            state = live.get("state")
            if state == "recording":
                self.on_event({"type": "status", **{k: v for k, v in live.items() if k not in ("state", "pid")}})
            elif state == "discarded":
                self.on_event({"type": "discarded", "session_dir": str(self.session_dir)})
                self.on_event({"type": "exited", "code": 0})
                return
            elif state != last_state and state in ("stopped", "finalized"):
                if state == "stopped" or last_state is None or last_state == "recording":
                    self.on_event({"type": "stopped", "report": live.get("report"),
                                   "session_dir": str(self.session_dir)})
                if state == "finalized":
                    self.on_event({"type": "finalized", "master": live.get("master"),
                                   "session_dir": str(self.session_dir)})
                    self.on_event({"type": "exited", "code": 0})
                    return
            last_state = state
            if state == "stopped" and not _lock_held(self.session_dir):
                time.sleep(2)  # finalize may still be writing; give it a moment, then settle
                if (_read_json(self.session_dir / LIVE) or {}).get("state") == "stopped":
                    self.on_event({"type": "exited", "code": 0})
                    return
            time.sleep(0.5)

    def request_stop(self) -> None:
        if self.session_dir:
            (self.session_dir / STOP_FILE).write_text(str(int(time.time() * 1000)), encoding="utf-8")

    def request_discard(self) -> None:
        if self.session_dir:
            (self.session_dir / DISCARD_FILE).write_text(str(int(time.time() * 1000)), encoding="utf-8")

    def request_pause(self, paused: bool) -> None:
        if self.session_dir:
            flag = self.session_dir / PAUSE_FILE
            if paused:
                flag.write_text(str(int(time.time() * 1000)), encoding="utf-8")
            else:
                flag.unlink(missing_ok=True)  # our own control file

    def request_bookmark(self) -> None:
        if self.session_dir:
            with (self.session_dir / BOOKMARK_FILE).open("a", encoding="utf-8") as stream:
                stream.write(f"{int(time.time() * 1000)}\n")


AUTO_MIN_MS = 60_000  # a recording shorter than a minute (a mis-press) is never sent automatically


_UPLOADER: dict[tuple, tuple[float, str]] = {}


def _mai_uploader() -> str:
    """Who uploads MAI chunks: the app through Aside (default when the CLI is there), a Claude window, or the user.

    Remembered for two minutes: each answer costs two PATH scans (~60 ms) and init/engines ask on every call."""
    from lecturehub.core import aside_cli

    key = (aside_cli.find_aside, *(os.environ.get(name, "") for name in (
        "LECTUREHUB_MAI_BROWSER", "LECTUREHUB_ASIDE_EXE", "LECTUREHUB_CLAUDE_EXE", "PATH", "PYTEST_CURRENT_TEST")))
    hit = _UPLOADER.get(key)
    if hit and time.monotonic() - hit[0] < 120:
        return hit[1]
    if os.environ.get("LECTUREHUB_MAI_BROWSER", "").lower() != "chrome" and aside_cli.find_aside():
        answer = "aside"
    else:
        answer = "agent" if _claude_exe() else "manual"
    _UPLOADER[key] = (time.monotonic(), answer)
    return answer


def _claude_exe() -> str | None:
    import shutil

    found = os.environ.get("LECTUREHUB_CLAUDE_EXE") or shutil.which("claude")
    local = Path.home() / ".local" / "bin" / "claude.exe"
    return found or (str(local) if local.is_file() else None)


def _named(course: str | None) -> bool:
    """A real course name; blank and "미분류" are asked for each recording and never stored (D-052)."""
    from lecturehub.transcription.consent import named

    return named(course)


def _course_of(master: str) -> str:
    """Course of a recording as the transcription CLI would infer it (녹음본 folder or session.json)."""
    from lecturehub.transcription.cli import _course_for

    try:
        return _course_for(Path(master), None)
    except (OSError, ValueError):
        return ""


def _too_short(master: str) -> bool:
    """True when the session's stop report says the recording is under a minute."""
    report = _read_json(Path(master).parent / "stop.json") or {}
    total = report.get("total_ms")
    return total is not None and total < AUTO_MIN_MS


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, PermissionError):
        return None


def _gap_summary(report: dict) -> dict:
    """What the summary line needs from a stop report's gaps (recorder: ``[{at, audio_ms, lost_ms, reason, open}]``):
    how many there were, how long they were together, and ``tail_lost_ms`` when one is still open, i.e. the sound
    never came back before the recording ended (``None`` when every gap closed). Odd values count as 0."""
    def lost(gap: dict) -> int:
        try:
            return max(0, int(gap.get("lost_ms") or 0))
        except (TypeError, ValueError):
            return 0

    gaps = [gap for gap in (report.get("gaps") or []) if isinstance(gap, dict)]
    tail = [gap for gap in gaps if gap.get("open")]
    return {"gaps": len(gaps), "lost_ms": sum(lost(gap) for gap in gaps),
            "tail_lost_ms": sum(lost(gap) for gap in tail) if tail else None}


CLEANUP_LEFT = ("skipped", "flac_only", "partial", "error")  # recorder/compact.py: not every WAV file went


def _cleanup_note(result) -> dict | None:
    """What the summary says about the engine's ``compacted`` event (the clean-up of the WAV files after a
    recording, recorder/compact.py), or ``None`` when there is nothing to say.

    ``{"state", "deleted": n, "later": True, "reason": text, "error": text}``, each after the first only when it
    applies: ``deleted`` = WAV files deleted for good because the drive has no Recycle Bin (``permanent``);
    ``later`` = more of them will be deleted after this summary (the pieces a live transcription still reads);
    ``reason`` =
    why the clean-up was skipped or left files (states in ``CLEANUP_LEFT``); ``error`` = the error text of a
    clean-up that failed. A clean-up that was done normally, is switched off, or was put off because another
    recording or a live transcription job still uses the files (it runs again later) says nothing, except when
    the master WAV was already deleted for good while putting it off (``deferred`` carries that under ``master``).
    """
    if not isinstance(result, dict):
        return None
    later = result.get("state") == "deferred" and isinstance(result.get("master"), dict)
    if later:
        result = result["master"]
    state, note = result.get("state"), {}
    gone = result.get("deleted_permanently")
    if result.get("permanent") and isinstance(gone, list) and gone:
        note["deleted"] = len(gone)
        if later:  # only the merged WAV went now; the 1-minute pieces follow when the live transcription lets
            note["later"] = True  # go of them, in another process that reports nothing to this summary
    if state in CLEANUP_LEFT:
        if result.get("reason"):
            note["reason"] = str(result["reason"])[:300]
        elif state == "error":
            note["error"] = str(result.get("error") or "")[-300:]
        else:
            note["reason"] = ""
    return {"state": state, **note} if note else None


def _lock_held(session_dir: Path) -> bool:
    from lecturehub.recorder.heartbeat import SessionLock

    if not (session_dir / ".recording.lock").exists():
        return False
    try:
        with SessionLock(session_dir):
            return False
    except RuntimeError:
        return True


def find_running_session(out_dir: Path) -> Path | None:
    """A session whose engine still holds its lock (e.g. the app was closed while recording)."""
    if not out_dir.is_dir():
        return None
    for folder in sorted(out_dir.iterdir(), reverse=True):
        if folder.is_dir() and not (folder / "stop.json").exists() and _lock_held(folder):
            return folder
    return None


# ------------------------------------------------------------------ backend
class Backend:
    """State shared by the main and mini windows; the UI polls ``state()``."""

    def __init__(self, *, config_path: Path | None = None, python: str | None = None,
                 clock=lambda: datetime.now().astimezone(), fake_engine: bool = False):
        from lecturehub.recorder.classconfig import ConfigError, load_config

        self.clock, self.python, self.fake_engine = clock, python or console_python(), fake_engine
        try:
            self.config = load_config(config_path)
            self.config_error = None
        except ConfigError as exc:
            self.config, self.config_error = None, str(exc)
        timetable = self.config.timetable if self.config else None
        from lecturehub.core.settings import NEEDS_SETUP, data_root, parking_root

        self.root = timetable.parent if timetable else data_root()
        self.needs_setup = self.root is None
        self.setup_root = None  # D-086: the folder the first-run wizard already made (before its timetable step)
        from lecturehub.core import settings as _settings

        if self.root is not None and isinstance(_settings.get("setup_open"), dict):
            self.setup_root, self.needs_setup = data_root() or self.root, True  # the wizard was left half-way
        if self.needs_setup:  # inert, app-private folder until the first-run setup picks a real one
            self.root = parking_root()
            self.config_error = self.config_error or NEEDS_SETUP
        self.out_dir = self.config.out_dir if self.config else self.root / "_lecturehub" / "recordings"
        # TT-001: a recorder.toml written before the timetable existed has no path; the semester's file still counts
        self.timetable_path = timetable or self.root / "시간표.csv"
        self.courses = load_courses(self.timetable_path, self.root)
        self._lock = threading.RLock()
        self._state: dict = {"phase": "idle", "probe": None, "status": None, "summary": None,
                             "bookmarks": [], "error": None, "attached": False, "transcription": None,
                             "link": None, "mai": None, "library_version": 0}
        self.engine: EngineLink | None = None
        self.pc_engine: EngineLink | None = None  # PC sound recorded alongside the mic (separate file)
        self._pair: dict | None = None
        self._companion_mode: str | None = None  # "loopback" or "mic": what the second engine records
        self._common: list[str] = []  # course/out args of the running recording (a second source reuses them)
        self.probe_engine: EngineLink | None = None
        self.selection = {"course": "", "mode": "mic", "device_name": None, "with_pc": False}
        running = find_running_session(self.out_dir)
        if running:
            self._attach(running)

    # ---------- info for the first render
    def suggest_course(self) -> str:
        """The timetable's class for right now (lead window included), or ""."""
        from lecturehub.core import timetable
        from lecturehub.recorder.classconfig import current_course

        now = self.clock()
        if not self.timetable_path.is_file() or not timetable.in_term(self.root, now.date()):
            return ""
        try:
            return current_course(self.timetable_path, now, self.config.lead_minutes if self.config else 15) or ""
        except (OSError, ValueError):
            return ""

    def ordered_courses(self) -> list[Course]:
        from lecturehub.core.timetable import order_key

        now = self.clock()
        return sorted(self.courses, key=lambda c: order_key(c.name, c.slots, now))

    # ---------- timetable and term (TT-001): edited in settings, 시간표.csv stays the source
    def timetable_get(self) -> dict:
        from lecturehub.core import settings, timetable

        return {"ok": True, "courses": timetable.read(self.root), "term": timetable.load_term(self.root),
                "reminder": bool(settings.get("class_reminder", True)), "days": list(timetable.DAYS)}

    def timetable_save(self, courses: list, term_start: str = "", term_end: str = "") -> dict:
        """Write the file only (D-050: this module never opens the database); app.py asks the library to follow."""
        from lecturehub.core import timetable

        if self.needs_setup:
            return {"ok": False, "error": "처음 설정을 먼저 마치세요."}
        try:
            timetable.save_term(self.root, term_start, term_end)  # validate dates before touching the timetable
            result = timetable.write(self.root, list(courses or []))
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}
        self.courses = load_courses(self.timetable_path, self.root)
        return {"ok": True, "courses": [c["folder"] for c in result["courses"]], "new_folders": result["new_folders"],
                "backup": result["backup"], "term": timetable.load_term(self.root), "library": None}

    # ---------- course folder layout (2026-11-13): 기본 · 자세히 · 이미 쓰는 폴더
    def folder_layout_get(self, term: str = "", courses: list | None = None) -> dict:
        from lecturehub.core import settings
        from lecturehub.ui import setup

        current = settings.folder_layout()
        names = courses if courses is not None else [c.folder for c in self.courses]
        return {"ok": True, **current, "choices": settings.LAYOUT_NAMES,
                "previews": {style: setup.layout_preview(term or self.root.name, names, style)["lines"]
                             for style in settings.LAYOUTS}}

    def folder_layout_set(self, style: str, archive_copies=None) -> dict:
        from lecturehub.core import settings

        try:
            return {"ok": True, **settings.set_folder_layout(style, archive_copies)}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    # ---------- recordings to the Recycle Bin after the Google transcript (index/audio_cleanup.py runs it)
    @staticmethod
    def audio_cleanup_get() -> dict:
        from lecturehub.core import settings

        return {"ok": True, **settings.audio_cleanup(), "keep_lossless": settings.keep_lossless()}

    @staticmethod
    def audio_cleanup_set(enabled: bool, days=None) -> dict:
        from lecturehub.core import settings

        try:
            days = settings.AUDIO_CLEANUP_DAYS if days in (None, "") else int(days)
            return {"ok": True, **settings.set_audio_cleanup(bool(enabled), days)}
        except (TypeError, ValueError) as exc:
            return {"ok": False, "error": str(exc) if isinstance(exc, ValueError) and "1~365" in str(exc)
                    else "며칠 뒤에 정리할지 1~365 사이로 고르세요"}

    @staticmethod
    def _class_reminder() -> bool:
        from lecturehub.core import settings

        return bool(settings.get("class_reminder", True))

    def class_reminder_set(self, on: bool) -> dict:
        from lecturehub.core import settings

        settings.save({"class_reminder": bool(on)})
        return {"ok": True, "reminder": bool(on)}

    def init_info(self) -> dict:
        now = self.clock()
        guess = self.suggest_course()
        return {
            "courses": [{"folder": c.folder, "name": c.name, "recordings": c.recordings} for c in self.ordered_courses()],
            "class_reminder": self._class_reminder(),
            "extras": self.extras(),
            "today": today_slots(self.courses, now),
            "course": guess,
            "date": f"{now.month}/{now.day}({WEEKDAYS[now.weekday()]})",
            "today_iso": now.date().isoformat(),
            "devices": self.devices(),
            "device_name": self.config.device_name if self.config else None,
            "out_dir": str(self.out_dir),
            "config_error": self.config_error,
            "needs_setup": self.needs_setup,
            "labs": self._labs(),
            "features": self._features(),
            "edition": self._edition(),
            "engines": self.engine_status(),
            "prefs": self.prefs(),
        }

    def _edition(self) -> dict:
        from lecturehub.core import edition

        return {"name": edition.name(), **edition.flags()}

    def _labs(self) -> dict:
        from lecturehub.core import settings

        return settings.labs()

    def _features(self) -> dict:
        """feature id -> {state, via, hint}, so the window can grey out what cannot work on this PC."""
        try:
            from lecturehub.core import connections

            return {f["id"]: f for f in connections.features()}
        except Exception:  # noqa: BLE001 - the window still opens without it
            return {}

    # Saved companions and room order: the full build only.
    def _gsr_prefs_off(self) -> dict | None:
        from lecturehub.core import edition

        return None if edition.has("gsr") and edition.has("gsr_prefs") else {"ok": False, "state": "disabled", "error": "공개판에는 없는 기능입니다"}

    def gsr_companions_get(self) -> dict:
        from lecturehub.core import settings

        if (off := self._gsr_prefs_off()) is not None:
            return off
        rows = settings.gsr_companions()
        return {"ok": True, "rows": rows, "text": "\n".join(f"{n} {no}" for n, no in rows), "count": len(rows)}

    def gsr_companions_set(self, text) -> dict:
        from lecturehub.core import settings

        if (off := self._gsr_prefs_off()) is not None:
            return off
        try:
            rows = settings.set_gsr_companions(text)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "count": len(rows)}

    def gsr_rooms_get(self) -> dict:
        from lecturehub.core import settings

        if (off := self._gsr_prefs_off()) is not None:
            return off
        return {"ok": True, "rooms": settings.gsr_rooms()}

    def gsr_rooms_set(self, prefs: dict) -> dict:
        from lecturehub.core import settings

        if (off := self._gsr_prefs_off()) is not None:
            return off
        try:
            return {"ok": True, "rooms": settings.set_gsr_rooms(prefs)}
        except (TypeError, ValueError) as exc:
            return {"ok": False, "error": str(exc) if isinstance(exc, ValueError) and str(exc) else "방 순서 값을 확인하세요"}

    # 실시간 전사 engine (user 2026-11-14): Gemini Live (quick, the Gemini key), Chirp 3 (accurate, Google Cloud), or both
    def live_engine_get(self) -> dict:
        from lecturehub.core import settings

        return {"ok": True, "engine": settings.live_engine(), "choices": settings.LIVE_ENGINES}

    def live_engine_set(self, engine: str) -> dict:
        from lecturehub.core import settings

        try:
            return {"ok": True, "engine": settings.set_live_engine(engine)}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    def lab_set(self, name: str, on: bool) -> dict:
        from lecturehub.core import settings

        try:
            return {"ok": True, "labs": settings.set_lab(str(name), bool(on))}
        except KeyError:
            return {"ok": False, "error": "알 수 없는 기능입니다."}

    # ---------- first run (release prep 2026-11): folder, timetable, recorder settings
    def setup_info(self) -> dict:
        from lecturehub.ui import setup

        return {"ok": True, **setup.info()}

    def _setup_allowed(self) -> bool:
        """The first run, or leaving the trial semester (its bar opens the same wizard; after setup_prepare the trial
        flag is gone, the folder the wizard made is what counts)."""
        from lecturehub.core import settings

        return self.needs_setup or self.setup_root is not None or bool(settings.get("trial"))

    def setup_apply(self, parent: str, term: str, courses: list, term_start: str = "", term_end: str = "",
                    layout: str = "", base: str | None = None) -> dict:
        from lecturehub.ui import setup

        if not self._setup_allowed():
            return {"ok": False, "error": "이미 설정되어 있습니다. 설정 화면에서 바꾸세요."}
        try:
            return setup.apply(parent, term, courses, term_start, term_end, layout, base=base or None)
        except setup.SetupError as exc:
            return {"ok": False, "error": str(exc)}

    def setup_prepare(self, parent: str, term: str, layout: str = "") -> dict:
        """D-086: make the semester folder before the timetable step (the AI app link needs a folder to write into)."""
        from lecturehub.ui import setup

        if not self._setup_allowed():
            return {"ok": False, "error": "이미 설정되어 있습니다. 설정 화면에서 바꾸세요."}
        try:
            result = setup.prepare(parent, term, layout)
        except setup.SetupError as exc:
            return {"ok": False, "error": str(exc)}
        self.setup_root = Path(result["root"])
        return result

    def setup_timetable(self) -> dict:
        """The timetable in the folder the wizard made, read now (the wizard polls this while Codex works)."""
        from lecturehub.ui import setup

        if self.setup_root is None:
            return {"ok": False, "error": "학기 폴더를 아직 만들지 않았습니다."}
        return {"ok": True, **setup.timetable_state(self.setup_root)}

    # ---------- connections: keys, tools, what each feature needs (settings screen)
    def conn_status(self, check_login: bool = False) -> dict:
        from lecturehub.core import connections, settings, tools

        rows = tools.status(check_login=bool(check_login))
        keys = connections.status()
        return {"ok": True, "keys": keys, "tools": [{"id": name, **row} for name, row in rows.items()],
                "features": connections.features(rows, keys), "labs": self._labs(), "lab_names": {key: label for key, label in settings.LABS.items() if self._edition().get(key, True)}}

    def conn_connect(self, provider: str, key: str) -> dict:
        from lecturehub.core import connections

        return connections.connect(provider, key)

    def conn_save_unchecked(self, provider: str, key: str) -> dict:
        from lecturehub.core import connections

        return connections.save_unchecked(provider, key)

    def conn_test(self, provider: str) -> dict:
        from lecturehub.core import connections

        return connections.test(provider)

    def conn_remove(self, provider: str) -> dict:
        from lecturehub.core import connections

        return connections.remove(provider)

    def conn_etl_site(self, address: str | None) -> dict:
        """Which Canvas site eTL means: blank = SNU's eTL, else another school's Canvas address (2026-11-20).
        A token saved for the old site is not used for the new one; the user connects again."""
        from lecturehub.core import etl_site

        try:
            return {"ok": True, **etl_site.set_base(address)}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    # ---------- small UI memory (user 2026-09-30): the source each course was last recorded with
    def _prefs_path(self) -> Path:
        return self.root / "_lecturehub" / "ui_prefs.json"

    def prefs(self) -> dict:
        data = _read_json(self._prefs_path()) or {}
        return {"source_by_course": data.get("source_by_course") or {}, "last_extra": data.get("last_extra")}

    def appearance(self, theme=None, text_size=None) -> dict:
        from lecturehub.core import settings

        current = settings.get("appearance", {})
        current = current if isinstance(current, dict) else {}
        value = {"theme": current.get("theme", "system"), "text_size": current.get("text_size", "normal")}
        if theme is not None or text_size is not None:
            value.update({k: v for k, v in (("theme", theme), ("text_size", text_size)) if v is not None})
            if value["theme"] not in ("system", "light", "dark") or value["text_size"] not in ("normal", "large", "larger"):
                return {"ok": False, "error": "테마와 글자 크기를 확인하세요."}
            settings.save({"appearance": value})
        return {"ok": True, **value}

    def _remember_source(self, course: str, mode: str, with_pc: bool) -> None:
        """Remembered from a recording that actually started, not from menu clicks; never blocks a recording."""
        if not course:
            return
        data = _read_json(self._prefs_path()) or {}
        data.setdefault("source_by_course", {})[course] = "both" if with_pc else "pc" if mode == "loopback" else "mic"
        if course in self.extras():
            data["last_extra"] = course
        try:
            atomic_write_json(self._prefs_path(), data)
        except OSError:
            pass

    def extras(self) -> list[str]:
        """'수업 외' topics (e.g. an online AI lecture) from _lecturehub/extra_courses.json — no DB (D-050)."""
        from lecturehub.core import extra_courses

        return extra_courses.names(self.root)

    def add_extra(self, name: str) -> dict:
        from lecturehub.core import extra_courses

        try:
            return {"ok": True, "extras": extra_courses.add(self.root, name, [c.folder for c in self.courses])}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    def recording_permissions(self) -> dict:
        from lecturehub.core import recording_permission as permission

        courses = list(dict.fromkeys([c.folder for c in self.courses] + self.extras()))
        return {"ok": True, "states": permission.STATES,
                "courses": [permission.check(self.root, c) for c in courses]}

    def recording_permission(self, course: str, state: str | None = None) -> dict:
        from lecturehub.core import recording_permission as permission

        try:
            value = permission.set_state(self.root, course, state) if state is not None else permission.check(self.root, course)
            return {"ok": True, **value}
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    def recording_permission_many(self, courses: list[str], state: str) -> dict:
        """settings › 녹음·전사 "모든 과목을 [상태]로": only the courses this screen lists (timetable and 수업 외)."""
        from lecturehub.core import recording_permission as permission

        known = {c.folder for c in self.courses} | set(self.extras())
        unknown = [c for c in courses if c not in known]
        if unknown:
            return {"ok": False, "error": f"시간표에 없는 과목입니다: {', '.join(unknown[:3])}"}
        try:
            rows = permission.set_many(self.root, list(courses), state)
            return {"ok": True, "changed": len(rows), "courses": rows}
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    def devices(self) -> dict:
        if self.fake_engine:
            return {"mic": [{"name": "FakeSource (synthetic sine)", "default": True}], "loopback": []}
        try:
            from lecturehub.recorder.devices import list_devices

            found = list_devices()
        except Exception as exc:  # noqa: BLE001 - the UI shows the reason instead of crashing
            return {"mic": [], "loopback": [], "error": str(exc)}
        return {
            "mic": [{"name": d.name, "default": d.is_default} for d in found if not d.is_loopback],
            "loopback": [{"name": d.name, "default": d.is_default} for d in found if d.is_loopback],
        }

    def quota_status(self) -> dict:
        """Google requests left today (D-054), from the job ledger; one request = one ≤20-minute chunk."""
        try:
            from lecturehub.transcription import quota
            from lecturehub.transcription.presets import load_preset

            preset = load_preset("gemini_transcribe")
            limits = preset.get("quota", {})
            result = quota.summary(self.root / "_lecturehub" / "quota.json", "gemini_transcribe",
                                   int(limits.get("requests_per_day", 25)), limits.get("personal_requests_per_day"))
            result.update(ok=True, chunk_s=int(preset.get("chunk_max_duration_s", 1200)))
            return result
        except Exception as exc:  # noqa: BLE001 - the chip just shows no number
            return {"ok": False, "error": str(exc)}

    def engine_status(self) -> dict:
        try:
            from lecturehub.transcription.gemini import load_api_key

            load_api_key()
            google = "ok"
        except Exception:  # noqa: BLE001 - only presence matters here
            google = "off"
        try:
            from lecturehub.transcription.groq import load_api_key as groq_key

            groq_key()
            whisper = "ok"  # D-061: Whisper via Groq, per-course consent still applies
        except Exception:  # noqa: BLE001 - only presence matters here
            whisper = "off"
        return {"google": google, "mai": _mai_uploader(), "whisper": whisper}

    # ---------- engines: what is left, per recording, start one (user request 2026-09-30)
    def engines_overview(self) -> dict:
        """Everything the engine chip needs, refreshed by the UI (poll): key/consent state, use and what is left."""
        from lecturehub.transcription import usage

        now = usage.now_ms()
        status = self.engine_status()
        return {"ok": True, "at": now, "recording": bool(self.recording),
                "google": {"state": status["google"], "quota": self.quota_status(),
                           "note": "Google(Gemini) 무료 요청 한도 · 조각 길이는 현재 프리셋 기준"},
                "whisper": {"state": status["whisper"], "usage": usage.whisper_usage(self.root, now),
                            "note": "Groq 무료 티어: 오디오 시간 한도(시간당 2시간, 하루 8시간)"},
                "mai": {"state": status["mai"], "usage": usage.mai_usage(self.root, now),
                        "note": {"aside": "앱이 Aside 브라우저로 올림(조각당 약 30초, AI·비용 없음)",
                                 "agent": "Claude가 사용자 Chrome으로 올림(창을 최소화하지 말 것)"}.get(
                            status["mai"], "Aside·Claude Code가 없어 수동으로만")}}

    def recording_engines(self, master: str, course: str = "") -> dict:
        """Per engine for one recording: the latest job and whether [실행] can be pressed now (and why not)."""
        from lecturehub.transcription import usage

        jobs = usage.jobs_for_recording(self.root, master)
        status = self.engine_status()
        course = course or _course_of(master)
        exists = Path(master).is_file()
        blank = not _named(course)
        busy = {"queued", "running", "retrying", "waiting_for_quota", "waiting_agent", "waiting_master"}

        def can(engine: str, key_state: str) -> tuple[bool, str]:
            job = jobs.get(engine)
            if not exists:
                return False, "녹음 파일을 찾지 못함"
            if key_state == "off" or (engine == "mai" and key_state not in ("aside", "agent")):
                return False, {"gemini_transcribe": "Google API 키 없음", "whisper": "Groq API 키 없음(GROQ_API_KEY)",
                               "mai": "Aside CLI·Claude Code(claude)를 찾지 못함"}[engine]
            if job and job["state"] in busy:
                return False, f"이미 진행 중 · {job['label']}"
            if engine == "mai" and self.recording:
                return False, "녹음 중에는 MAI 조각을 만들지 않음(녹음이 끝나면 가능)"
            if engine == "gemini_transcribe" and self.recording:
                return False, "녹음 중에는 Google 전사를 새로 시작하지 않음(녹음 중 전사는 자동)"
            return True, "과목 미지정: 이 녹음에 한해 동의를 묻습니다" if blank else ""

        engines = {}
        for engine, key_state in (("gemini_transcribe", status["google"]), ("whisper", status["whisper"]),
                                  ("mai", status["mai"])):
            ok, why = can(engine, key_state)
            job = jobs.get(engine)
            engines[engine] = {"job": job, "can_start": ok, "why": why,
                               "again": bool(job and job["state"] in ("done", "qa_warning", "failed", "cancelled"))}
        finished = {j["job_id"] for j in jobs.values() if j and j["state"] in ("done", "qa_warning")}
        seen = self.__dict__.setdefault("_registered_seen", set())
        if finished - seen:  # a Whisper/MAI job finished since we last looked: register it once
            seen |= finished
            threading.Thread(target=self._register_finished_jobs, daemon=True).start()
        from lecturehub.transcription import language as lang

        return {"ok": True, "master": master, "course": course, "engines": engines,
                "language": lang.course_default(self.root, course) or lang.DEFAULT, "languages": list(lang.CHOICES)}

    def engine_start(self, engine: str, master: str, course: str = "", consent: bool = False,
                     language: str = "") -> dict:
        """[실행] for one engine. Google keeps its own path (consent dialog, duplicate guard). Whisper and MAI
        run their CLI; a blank course needs the user's yes for this recording only (never stored, D-052)."""
        engine = {"google": "gemini_transcribe", "gemini": "gemini_transcribe"}.get(engine, engine)
        course = course or _course_of(master)
        info = self.recording_engines(master, course)["engines"].get(engine)
        if info is None:
            return {"ok": False, "reason": f"알 수 없는 엔진: {engine}"}
        again = info["again"]
        if not info["can_start"]:
            return {"ok": False, "reason": info["why"]}
        if engine == "gemini_transcribe":
            if again:
                # 2026-10-19: the same recording may go again only in another language (a German video sent as ko)
                done_in = (info.get("job") or {}).get("language") or "ko"
                if not language or language == done_in:
                    return {"ok": False, "reason": "Google 전사는 이미 있습니다(한도를 아끼려고 같은 언어로는 다시 보내지 "
                                                   "않음 — 언어를 바꾸면 다시 보낼 수 있음)"}
            return self.transcription_start(consent, master, course, language=language or None, again=again)
        job = info.get("job") or {}
        if engine == "mai" and job.get("via") == "aside" and job.get("state") == "failed":
            return self._mai_upload(job["job_id"])  # [다시] after a stopped Aside run: same chunks, only what is missing
        blank = not _named(course)
        if blank and not consent:
            from lecturehub.transcription.consent import NOTICES

            key = "whisper_groq" if engine == "whisper" else "mai_playground"
            return {"ok": False, "needs_consent": True, "notice": NOTICES[key], "once_only": True}
        once = ["--consent-once"] if blank else []
        if engine == "whisper":
            args = [self.python, "-m", "lecturehub", "transcribe", "run", master, "--provider", "whisper",
                    "--course", course or "", "--root", str(self.root), *once, *(["--again"] if again else []),
                    *(["--language", language] if language else [])]
            process = spawn(args, stdout=subprocess.DEVNULL, below_normal=True)
            threading.Thread(target=self._wait_then_register, args=(process,), daemon=True).start()
            return {"ok": True, "engine": "whisper"}
        return self._mai_start(master, course, once + (["--again"] if again else []))

    def _mai_start(self, master: str, course: str, extra: list[str]) -> dict:
        """mai prepare (chunks + manifest), then a console with Claude Code running /mai-transcribe for it."""
        from lecturehub.core import edition

        if not edition.has("mai"):
            return {"ok": False, "error": "공개판에 포함되지 않은 기능입니다.", "reason": "공개판에 포함되지 않은 기능입니다.", "state": "disabled", "running": False}
        try:
            result = subprocess.run(
                [self.python, "-m", "lecturehub", "mai", "prepare", master, "--course", course or "",
                 "--root", str(self.root), *extra], capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=900, stdin=subprocess.DEVNULL, check=False,
                creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
                env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
        except subprocess.TimeoutExpired:
            return {"ok": False, "reason": "MAI 조각 준비가 15분 안에 끝나지 않았습니다"}
        job_id = next((line.split()[2].rstrip(":") for line in result.stdout.splitlines()
                       if line.startswith("MAI 작업 ")), None)
        if result.returncode or not job_id:
            message = (result.stderr or result.stdout).strip()
            return {"ok": False, "reason": message.splitlines()[-1] if message else "MAI 조각 준비 실패"}
        if _mai_uploader() == "aside":
            return self._mai_upload(job_id)
        opened = self._open_claude(f"/mai-transcribe {job_id}", "Lecture Hub MAI")
        return {"ok": True, "engine": "mai", "job_id": job_id, "claude": opened, "via": "claude",
                "hint": "Claude 창이 MAI playground에 올립니다. Chrome의 Claude 창을 최소화하지 마세요."}

    def _mai_upload(self, job_id: str) -> dict:
        """The app uploads the chunks itself through Aside (``mai run``, no Claude, 2026-11-06), then registers."""
        process = spawn([self.python, "-m", "lecturehub", "mai", "run", job_id, "--root", str(self.root)],
                        stdout=subprocess.DEVNULL, below_normal=True)
        threading.Thread(target=self._wait_then_register, args=(process,), daemon=True).start()
        return {"ok": True, "engine": "mai", "job_id": job_id, "via": "aside", "claude": False,
                "hint": "Aside 브라우저가 MAI playground에 올립니다(조각당 약 30초). 끝나면 전사본으로 등록됩니다."}

    # ---------- live captions (user request 2026-11-06): Gemini 3.5 Transcribe Live; Chirp 3 confirmed line opt-in
    def captions_status(self) -> dict:
        from lecturehub.transcription.store import app_dir

        if not (app_dir(self.root) / "captions" / "live.json").exists():  # never started: skip importing numpy (~0.6 s)
            return {"state": "none", "running": False, "paused": False}
        from lecturehub.transcription import captions

        data = captions.status(self.root)
        data["running"] = data.get("state") in ("running", "paused", "finishing")
        data["paused"] = data.get("state") == "paused"
        return data

    # ---------- stored consents for sending to outside services: list and take back (settings screen)
    CONSENT_LABELS = {"gemini_transcribe": "Google · 녹음 전사", "whisper_groq": "Groq · 녹음 전사",
                      "google_live": "Google · 실시간 자막", "mai_playground": "Microsoft · MAI 전사",
                      "codex_note": "OpenAI Codex · 정리본", "codex_notices": "OpenAI Codex · 수업 중 공지 찾기",
                      "codex_etl_notices": "OpenAI Codex · eTL 공지에서 일정 찾기",
                      "claude_note": "Anthropic · 정리본",
                      "claude_qa": "Anthropic · 질의응답 도우미", "ai_app_link": "AI 앱 연결 · 내가 연결한 AI 앱",
                      # the assignment helper was removed (2026-11-22); an old consent still shows so it can be taken back
                      "claude_assignment": "Anthropic · 과제 도우미(없어진 기능)"}

    def consent_list(self) -> dict:
        from lecturehub.transcription.consent import ConsentStore

        rows = ConsentStore(self.root / "_lecturehub" / "consent.json").listing()
        for row in rows:
            row["label"] = self.CONSENT_LABELS.get(row["provider"], row["provider"])
        return {"ok": True, "rows": rows}

    def consent_revoke(self, provider: str, course: str) -> dict:
        from lecturehub.transcription.consent import ConsentStore

        store = ConsentStore(self.root / "_lecturehub" / "consent.json")
        if not store.revoke(str(provider), str(course), int(time.time() * 1000)):
            return {"ok": False, "error": "저장된 동의가 없습니다."}
        applog.LOG.info("외부 전송 동의 하나를 철회함")
        return {"ok": True, **self.consent_list()}

    # ---------- AI app link (D-072): master switch, per-course allow, what to paste into the AI app
    def _ai_link_courses(self) -> list[str]:
        from lecturehub.transcription.consent import named

        return [c for c in dict.fromkeys([c.folder for c in self.courses] + self.extras()) if named(c)]

    def ai_link_status(self) -> dict:
        from lecturehub.core import settings
        from lecturehub.mcp import config
        from lecturehub.mcp.access import PROVIDER
        from lecturehub.transcription.consent import ConsentStore

        from lecturehub.ai.codex import CodexEngine
        from lecturehub.mcp import changes, connect

        from lecturehub.mcp.approve import KINDS

        store = ConsentStore(self.root / "_lecturehub" / "consent.json")
        courses = [{"course": c, "allowed": store.granted(PROVIDER, c)} for c in self._ai_link_courses()]
        state = settings.ai_link()
        auto = settings.ai_link_auto()  # D-080: per kind "묻기" (false) or "자동" (true)
        return {"ok": True, "enabled": state["enabled"], "write": state["write"], "courses": courses,
                "auto": [{"kind": kind, "label": label, "auto": auto[kind]} for kind, label in KINDS.items()],
                "changes": changes.recent(self.root, 20), "apps": [connect.status(a) for a in connect.APPS],
                "codex_found": CodexEngine().available(), **config.snippets()}

    def ai_link_enable(self, on: bool) -> dict:
        from lecturehub.core import settings

        state = settings.set_ai_link(bool(on))
        applog.LOG.info("AI 앱 연결 %s", "켬" if state["enabled"] else "끔")
        return {"ok": True, **self.ai_link_status()}

    def ai_link_write(self, on: bool) -> dict:
        """D-076: "AI 앱이 고칠 수 있게" — a plain switch (no dialog, owner 2026-11-25). Only with the link on."""
        from lecturehub.core import settings

        if on and not settings.ai_link()["enabled"]:
            return {"ok": False, "error": "AI 앱 연결을 먼저 켜세요."}
        state = settings.set_ai_link_write(bool(on))
        applog.LOG.info("AI 앱 연결: 고치기 %s", "켬" if state["write"] else "끔")
        return {"ok": True, **self.ai_link_status()}

    def ai_link_auto(self, kind: str, on: bool) -> dict:
        """D-080: one kind of AI app change to "자동" (on) or back to "묻기". Going to 자동 is allowed only through
        ``Api.ai_link_auto`` with the native dialog's token; going back needs nothing (it only narrows)."""
        from lecturehub.core import settings

        if kind == "*" and on:  # [모두 자동] (the Api checked its one dialog's token)
            settings.set_ai_link_auto_all()
            applog.LOG.info("AI 앱 연결: 모든 종류 → 자동")
            return {"ok": True, **self.ai_link_status()}
        if kind not in settings.AI_AUTO_KINDS:
            return {"ok": False, "error": "알 수 없는 종류입니다."}
        settings.set_ai_link_auto(kind, bool(on))
        applog.LOG.info("AI 앱 연결: %s → %s", kind, "자동" if on else "묻기")
        return {"ok": True, **self.ai_link_status()}

    def ai_link_auto_reset(self) -> dict:
        """[모두 다시 묻기]."""
        from lecturehub.core import settings

        settings.reset_ai_link_auto()
        applog.LOG.info("AI 앱 연결: 모든 종류를 다시 묻기로")
        return {"ok": True, **self.ai_link_status()}

    def ai_change_undo(self, change_id: str) -> dict:
        """[되돌리기] of one item of "AI가 바꾼 것" (the app does undo, never the AI app)."""
        from lecturehub.mcp import changes

        if self.needs_setup:
            return {"ok": False, "error": "처음 설정을 먼저 마치세요."}
        try:
            result = changes.undo(self.root, str(change_id))
        except changes.WriteRefused as exc:
            return {**self.ai_link_status(), "ok": False, "error": str(exc)}
        applog.LOG.info("AI 앱 연결: AI가 바꾼 것 하나를 되돌림")
        self._bump_library()
        return {**self.ai_link_status(), **result}

    def ai_change_undo_since(self, change_id: str) -> dict:
        """"이 시각 이후 모두 되돌리기": every AI change from that item's time on, newest first; what was changed since
        is skipped and counted (integration review: one click after a prompt-injection run)."""
        from lecturehub.mcp import changes

        if self.needs_setup:
            return {"ok": False, "error": "처음 설정을 먼저 마치세요."}
        try:
            result = changes.undo_since(self.root, str(change_id))
        except changes.WriteRefused as exc:
            return {**self.ai_link_status(), "ok": False, "error": str(exc)}
        applog.LOG.info("AI 앱 연결: AI가 바꾼 것 %d건을 되돌림(건너뜀 %d건)", result["undone"], len(result["skipped"]))
        if result["undone"]:
            self._bump_library()
        return {**self.ai_link_status(), **result}

    def ai_link_disconnect(self, app: str) -> dict:
        """[연결 해제]: takes out exactly what [연결] added (no dialog: it only narrows)."""
        from lecturehub.mcp import connect

        try:
            connect.disconnect(str(app))
        except connect.ConfigError as exc:
            return {**self.ai_link_status(), "ok": False, "error": str(exc)}
        applog.LOG.info("AI 앱 연결: %s 설정에서 뺌", app)
        return {"ok": True, **self.ai_link_status()}

    def ai_link_connect(self, app: str) -> dict:
        """[연결] after the native dialog (only through ``Api.ai_link_connect``, which takes the dialog's token)."""
        from lecturehub.mcp import connect

        try:
            connect.connect(str(app))
        except connect.ConfigError as exc:
            return {**self.ai_link_status(), "ok": False, "error": str(exc)}
        applog.LOG.info("AI 앱 연결: %s 설정에 넣음", app)
        return {"ok": True, **self.ai_link_status()}

    def _ai_changes_check(self) -> None:
        """A running app shows what an AI app changed without a restart: the MCP process rewrites a marker after every
        commit; a cheap stat here (at most every 1.5 s, on the state poll) bumps the library version."""
        now = time.monotonic()
        if now - getattr(self, "_ai_seen_at", 0.0) < 1.5 or self.needs_setup or not self.root:
            return
        self._ai_seen_at = now
        from lecturehub.mcp import changes

        mark = changes.signature(self.root)
        seen = getattr(self, "_ai_mark", "unset")
        self._ai_mark = mark
        if seen == "unset":
            self._timetable_check(reload=False)  # the first look only remembers, here too
        elif mark != seen:
            self._bump_library()
            self._timetable_check()  # D-083: an AI app may have changed 시간표.csv (set_timetable, or its undo)

    def _timetable_check(self, reload: bool = True) -> None:
        """Re-read the courses when 시간표.csv changed since the last look, and tell the page (``timetable_version``:
        it reloads the course list). The library's weekly slots were already synced by whoever wrote the file."""
        try:
            stat = self.timetable_path.stat()
            signature = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            signature = None
        if signature == getattr(self, "_timetable_seen", "unset"):
            return
        self._timetable_seen = signature
        if not reload:
            return
        try:
            courses = load_courses(self.timetable_path, self.root)
        except Exception:  # noqa: BLE001 - review L5: a file that cannot be read (cp949, ...) keeps the old timetable
            from lecturehub.core import applog

            applog.swallowed("시간표 다시 읽기")
            return
        self.courses = courses
        with self._lock:
            self._state["timetable_version"] = self._state.get("timetable_version", 0) + 1

    def ai_link_allow(self, course: str) -> dict:
        """Store this course's consent. Only through ``Api.ai_link_allow``, which takes the native dialog's token."""
        from lecturehub.mcp.access import PROVIDER
        from lecturehub.transcription.consent import NOTICES, ConsentStore

        if self.needs_setup:
            return {"ok": False, "error": "처음 설정을 먼저 마치세요."}
        if course not in self._ai_link_courses():
            return {"ok": False, "error": "시간표나 수업 외 목록에 있는 과목만 허용할 수 있습니다."}
        ConsentStore(self.root / "_lecturehub" / "consent.json").grant(
            PROVIDER, course, int(time.time() * 1000), NOTICES[PROVIDER], by="user_app_dialog")
        applog.LOG.info("AI 앱 연결: 과목 하나를 허용함")
        return {"ok": True, **self.ai_link_status()}

    def _grant_many(self, provider: str, courses: list[str]) -> str | None:
        """Store one consent per listed course (several courses named in one dialog). All or nothing: a name that
        is not a course of this install stores nothing. Returns the error, or None."""
        from lecturehub.transcription.consent import NOTICES, ConsentStore

        if self.needs_setup:
            return "처음 설정을 먼저 마치세요."
        known = self._ai_link_courses()
        if not courses or any(course not in known for course in courses):
            return "시간표나 수업 외 목록에 있는 과목만 허용할 수 있습니다."
        store = ConsentStore(self.root / "_lecturehub" / "consent.json")
        now = int(time.time() * 1000)
        for course in courses:
            store.grant(provider, course, now, NOTICES[provider], by="user_app_dialog")
        return None

    def ai_link_allow_many(self, courses: list[str]) -> dict:
        """Only through ``Api.ai_link_allow_all`` (the token of the dialog that named these courses)."""
        from lecturehub.mcp.access import PROVIDER

        error = self._grant_many(PROVIDER, list(courses))
        if error:
            return {"ok": False, "error": error}
        applog.LOG.info("AI 앱 연결: 과목 %d개를 허용함", len(courses))
        return {"ok": True, **self.ai_link_status()}

    def notices_allow_many(self, courses: list[str]) -> dict:
        """Only through ``Api.notices_allow_all`` (the token of the dialog that named these courses)."""
        error = self._grant_many(NOTICES_PROVIDER, list(courses))
        if error:
            return {"ok": False, "error": error}
        applog.LOG.info("수업 중 공지 찾기(Codex): 과목 %d개를 허용함", len(courses))
        return {"ok": True, "courses": self.notices_courses()}

    def consent_revoke_many(self, provider: str, courses: list[str]) -> dict:
        from lecturehub.transcription.consent import ConsentStore

        store = ConsentStore(self.root / "_lecturehub" / "consent.json")
        now = int(time.time() * 1000)
        revoked = sum(1 for course in courses if store.revoke(str(provider), str(course), now))
        applog.LOG.info("외부 전송 동의 %d개를 철회함", revoked)
        return {"ok": True, "revoked": revoked, **self.consent_list()}

    # ---------- class notices through ChatGPT (Codex), D-073: per-course consent ``codex_notices``
    def notices_courses(self) -> list[dict]:
        """The courses of the timetable and the extra list, and whether each may go to Codex for class notices."""
        from lecturehub.transcription.consent import ConsentStore

        store = ConsentStore(self.root / "_lecturehub" / "consent.json")
        return [{"course": c, "allowed": store.granted(NOTICES_PROVIDER, c)} for c in self._ai_link_courses()]

    def notices_allow(self, course: str) -> dict:
        """Store this course's ``codex_notices`` consent. Only through ``Api.notices_allow`` (native dialog token)."""
        from lecturehub.transcription.consent import NOTICES, ConsentStore

        if self.needs_setup:
            return {"ok": False, "error": "처음 설정을 먼저 마치세요."}
        if course not in self._ai_link_courses():
            return {"ok": False, "error": "시간표나 수업 외 목록에 있는 과목만 허용할 수 있습니다."}
        ConsentStore(self.root / "_lecturehub" / "consent.json").grant(
            NOTICES_PROVIDER, course, int(time.time() * 1000), NOTICES[NOTICES_PROVIDER], by="user_app_dialog")
        applog.LOG.info("수업 중 공지 찾기(Codex): 과목 하나를 허용함")
        return {"ok": True, "courses": self.notices_courses()}

    # ---------- eTL announcements through ChatGPT (Codex), D-077: per-course consent ``codex_etl_notices``
    def etl_notices_courses(self) -> list[dict]:
        """The courses and whether each may send its eTL announcements to Codex."""
        from lecturehub.transcription.consent import ConsentStore

        store = ConsentStore(self.root / "_lecturehub" / "consent.json")
        return [{"course": c, "allowed": store.granted(ETL_NOTICES_PROVIDER, c)} for c in self._ai_link_courses()]

    def etl_notices_allow(self, course: str) -> dict:
        """Store this course's ``codex_etl_notices`` consent. Only through ``Api.etl_notices_allow`` (native dialog
        token)."""
        from lecturehub.transcription.consent import NOTICES, ConsentStore

        if self.needs_setup:
            return {"ok": False, "error": "처음 설정을 먼저 마치세요."}
        if course not in self._ai_link_courses():
            return {"ok": False, "error": "시간표나 수업 외 목록에 있는 과목만 허용할 수 있습니다."}
        ConsentStore(self.root / "_lecturehub" / "consent.json").grant(
            ETL_NOTICES_PROVIDER, course, int(time.time() * 1000), NOTICES[ETL_NOTICES_PROVIDER], by="user_app_dialog")
        applog.LOG.info("eTL 공지 일정 찾기(Codex): 과목 하나를 허용함")
        return {"ok": True, "etl_courses": self.etl_notices_courses()}

    def etl_notices_allow_many(self, courses: list[str]) -> dict:
        """Only through ``Api.etl_notices_allow_all`` (the token of the dialog that named these courses)."""
        error = self._grant_many(ETL_NOTICES_PROVIDER, list(courses))
        if error:
            return {"ok": False, "error": error}
        applog.LOG.info("eTL 공지 일정 찾기(Codex): 과목 %d개를 허용함", len(courses))
        return {"ok": True, "etl_courses": self.etl_notices_courses()}

    def captions_check(self, course: str) -> dict:
        from lecturehub.transcription import captions
        from lecturehub.transcription.consent import NOTICES, ConsentStore, named

        store = ConsentStore(self.root / "_lecturehub" / "consent.json")
        granted = named(course) and store.granted(captions.PROVIDER, course)
        return {"ok": True, "course": course, "needs_consent": not granted, "once_only": not named(course),
                "notice": NOTICES[captions.PROVIDER]}

    def captions_start(self, course: str, source: str = "mic", consent: bool = False, store_consent: bool = False,
                       chirp: bool = False, quick: bool = True) -> dict:
        """Start ``lecturehub captions start`` in the background. Consent is stored only when the native dialog said
        yes (``store_consent``, checked by the Api); a blank course gets a yes for this run only (never stored)."""
        # No edition check here: in the public edition "captions" only hides the separate 자막 button; the live
        # transcription pane of the recording screen starts through this same call (found 2026-11-16 by running
        # the installed app: a guard added here would have switched live transcription off for every public user).
        from lecturehub.transcription import captions
        from lecturehub.transcription.consent import NOTICES, ConsentStore, named

        permission = self.recording_permission(course)
        if not permission.get("allowed"):
            return {"ok": False, "reason": permission.get("reason")}
        if self.captions_status()["running"]:
            return {"ok": False, "reason": "실시간 자막이 이미 돌고 있습니다"}
        check = self.captions_check(course)
        if check["needs_consent"]:
            if named(course) and store_consent:
                ConsentStore(self.root / "_lecturehub" / "consent.json").grant(
                    captions.PROVIDER, course, int(time.time() * 1000), NOTICES[captions.PROVIDER], by="user_app_dialog")
            elif not (check["once_only"] and consent):
                return {"ok": False, "needs_consent": True, **check}
        args = [self.python, "-m", "lecturehub", "captions", "start", "--course", course or "",
                "--source", "pc" if source in ("pc", "loopback") else "mic", "--root", str(self.root),
                "--parent-pid", str(os.getpid())]  # P-053: stops when the app is gone
        if check["once_only"]:
            args.append("--consent-once")
        if chirp:
            args.append("--confirmed")
        if not quick and chirp:
            args.append("--no-quick")  # Chirp alone through the class; the Q&A helper turns Gemini Live on later
        process = spawn(args, stdout=subprocess.DEVNULL)
        self.__dict__["_captions_process"] = process
        return {"ok": True, "source": source, "course": course, "chirp": bool(chirp)}

    def captions_pause(self, on: bool) -> dict:
        """Pause or resume listening; the same captions file goes on (P-050)."""
        from lecturehub.transcription import captions

        if not self.captions_status()["running"]:
            return {"ok": False, "reason": "실시간 자막이 꺼져 있습니다"}
        captions.request_pause(self.root, bool(on))
        return {"ok": True, "paused": bool(on)}

    def captions_stop(self) -> dict:
        from lecturehub.transcription import captions

        captions.request_stop(self.root)
        return {"ok": True}

    # ---------- presentation Q&A helper (user request 2026-11-07): materials + live captions → Claude answer cards
    def qa_status(self) -> dict:
        from lecturehub.core import edition

        if not edition.has("qa"):
            return {"ok": False, "error": "공개판에 포함되지 않은 기능입니다.", "reason": "공개판에 포함되지 않은 기능입니다.", "state": "disabled", "running": False}
        from lecturehub.transcription import qa_assist

        data = qa_assist.status(self.root)
        data["running"] = data.get("state") in ("loading", "ready", "answering")
        live = self.captions_status()
        now = None  # the captions clock right now (P-051: [질문 시작] marks it; live.json is written only on new words)
        try:
            now = round((datetime.now().astimezone() - datetime.fromisoformat(live["started_at"])).total_seconds(), 1)
        except (KeyError, TypeError, ValueError):
            pass
        data["captions"] = {"running": live.get("running", False), "quick": (live.get("engines") or {}).get("quick"),
                            "confirmed": (live.get("engines") or {}).get("confirmed"), "now": now,
                            "hints": live.get("hints", 0),
                            "recent": [{"at": l.get("at", 0), "text": l.get("text", "")}
                                       for l in (live.get("quick") or live.get("lines") or [])[-8:]],
                            "interim": live.get("interim", "")}
        return data

    def qa_check(self, course: str) -> dict:
        from lecturehub.core import edition

        if not edition.has("qa"):
            return {"ok": False, "error": "공개판에 포함되지 않은 기능입니다.", "reason": "공개판에 포함되지 않은 기능입니다.", "state": "disabled", "running": False}
        from lecturehub.transcription import captions, qa_assist
        from lecturehub.transcription.consent import ConsentStore, named

        store = ConsentStore(self.root / "_lecturehub" / "consent.json")
        need = []
        if not (named(course) and store.granted(qa_assist.PROVIDER, course)):
            need.append(qa_assist.PROVIDER)
        if not self.captions_status()["running"] and not (named(course) and store.granted(captions.PROVIDER, course)):
            need.append(captions.PROVIDER)
        return {"ok": True, "course": course, "needs": need, "named": named(course)}

    # presentation packs (P-046): the helper is set up per presentation, the course comes from its materials folder;
    # packs are made and edited in Claude Code (skill qa-pack, P-049), so the app only lists them
    def qa_packs(self) -> dict:
        from lecturehub.core import edition

        if not edition.has("qa"):
            return {"ok": False, "error": "공개판에 포함되지 않은 기능입니다.", "reason": "공개판에 포함되지 않은 기능입니다.", "state": "disabled", "running": False}
        from lecturehub.transcription import qa_packs

        return {"ok": True, "packs": qa_packs.list_packs(self.root)}

    def qa_start(self, course: str, folder: str, store_qa: bool = False, store_live: bool = False,
                 model: str = "sonnet", pack: str = "") -> dict:
        """Start the helper; Gemini Live is switched on in the running captions, or captions start with it.
        With ``pack`` the course and folder come from the presentation pack."""
        from lecturehub.core import edition

        if not edition.has("qa"):
            return {"ok": False, "error": "공개판에 포함되지 않은 기능입니다.", "reason": "공개판에 포함되지 않은 기능입니다.", "state": "disabled", "running": False}
        from lecturehub.transcription import captions, qa_assist, qa_packs
        from lecturehub.transcription.consent import NOTICES, ConsentStore, named

        if pack:
            try:
                info = qa_packs.load(self.root, pack)
            except ValueError as exc:
                return {"ok": False, "reason": str(exc)}
            course, folder = info["course"], str(info["folder_path"] or "")
            if not (info["folder_path"] or info["digest_chars"] or info["questions"]):
                return {"ok": False, "reason": "이 발표 묶음에는 자료 폴더·요약본·예상 질문이 없습니다"}
        if not named(course):
            return {"ok": False, "reason": "과목을 먼저 고르세요(동의는 과목마다 받습니다)"}
        if self.qa_status()["running"]:
            return {"ok": False, "reason": "질의응답 도우미가 이미 켜져 있습니다"}
        if not pack and (not folder or not Path(folder).is_dir()):
            return {"ok": False, "reason": "자료 폴더를 찾지 못했습니다"}
        store = ConsentStore(self.root / "_lecturehub" / "consent.json")
        if store_qa and not store.granted(qa_assist.PROVIDER, course):
            store.grant(qa_assist.PROVIDER, course, int(time.time() * 1000), NOTICES[qa_assist.PROVIDER],
                        by="user_app_dialog")
        check = self.qa_check(course)
        if qa_assist.PROVIDER in check["needs"]:
            return {"ok": False, "needs_consent": True, **check}
        if self.captions_status()["running"]:
            self.__dict__.pop("_qa_started_captions", None)  # the user's own captions: [끝내기] leaves them on
            self.__dict__["_qa_quick_was_off"] = not (self.captions_status().get("engines") or {}).get("quick")
            if self.captions_status().get("paused"):
                captions.request_pause(self.root, False)  # the Q&A needs to hear the questions
            captions.request_quick(self.root, True)
        else:
            started = self.captions_start(course, "mic", False, store_live)
            if not started.get("ok"):
                return started
            self.__dict__["_qa_quick_was_off"] = False
            self.__dict__["_qa_started_captions"] = True  # [끝내기] stops these captions too (user 2026-11-17)
        where = ["--pack", pack] if pack else ["--course", course, "--folder", str(folder)]
        args = [self.python, "-m", "lecturehub", "qa", "start", *where,
                "--model", model if model in ("sonnet", "opus", "haiku") else "sonnet", "--root", str(self.root),
                "--parent-pid", str(os.getpid())]
        self.__dict__["_qa_process"] = spawn(args, stdout=subprocess.DEVNULL)
        return {"ok": True, "course": course, "folder": str(folder), "pack": pack}

    def qa_ask(self, text: str = "", start: float | None = None) -> dict:
        from lecturehub.core import edition

        if not edition.has("qa"):
            return {"ok": False, "error": "공개판에 포함되지 않은 기능입니다.", "reason": "공개판에 포함되지 않은 기능입니다.", "state": "disabled", "running": False}
        from lecturehub.transcription import qa_assist

        if not self.qa_status()["running"]:
            return {"ok": False, "reason": "질의응답 도우미가 꺼져 있습니다"}
        qa_assist.request_answer(self.root, text, start)  # text: typed (P-050); start: where the question begins (P-051)
        return {"ok": True}

    def qa_stop(self) -> dict:
        from lecturehub.core import edition

        if not edition.has("qa"):
            return {"ok": False, "error": "공개판에 포함되지 않은 기능입니다.", "reason": "공개판에 포함되지 않은 기능입니다.", "state": "disabled", "running": False}
        from lecturehub.transcription import captions, qa_assist

        qa_assist.request_stop(self.root)
        if self.__dict__.pop("_qa_started_captions", False):
            # the helper switched the microphone on itself: ending the helper ends listening too (user 2026-11-17:
            # "끝내도 계속 녹음이 되고") — captions the user had started before stay on, back to how they were
            self.__dict__.pop("_qa_quick_was_off", None)
            captions.request_stop(self.root)
            return {"ok": True, "captions_stopped": True}
        if self.__dict__.pop("_qa_quick_was_off", False):
            captions.request_quick(self.root, False)  # back to Chirp alone, as before the Q&A
        return {"ok": True}

    def stop_live_helpers(self) -> list[str]:
        """The app is closing (user decision 2026-11-09, P-053): the Q&A helper and live captions stop with it.
        A recording goes on as before. Never raises — closing must not be blocked."""
        from lecturehub.core import edition
        from lecturehub.transcription import captions

        def stop_qa():
            if edition.has("qa"):
                from lecturehub.transcription import qa_assist

                qa_assist.request_stop(self.root)

        stopped = []
        for name, running, stop in (
                ("qa", lambda: edition.has("qa") and self.qa_status()["running"], stop_qa),
                ("captions", lambda: self.captions_status()["running"], lambda: captions.request_stop(self.root))):
            try:
                if running():
                    stop()
                    stopped.append(name)
            except Exception:  # noqa: BLE001, S112 - the helper's own parent watch stops it anyway
                continue
        return stopped

    def _open_claude(self, prompt: str, title: str) -> bool:
        """A console with Claude Code in this app's repository."""
        claude = _claude_exe()
        if not claude:
            return False
        repo = Path(__file__).resolve().parents[3]
        subprocess.Popen(["cmd", "/c", "start", title, "cmd", "/k", claude, prompt], cwd=str(repo),
                         creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0)
        return True

    # ---------- state
    def state(self) -> dict:
        try:
            self._ai_changes_check()
        except OSError:
            pass
        with self._lock:
            return json.loads(json.dumps(self._state))

    # ---------- "수업이 끝난 것 같아요" (user request 2026-09-29; benchmark speakly §3)
    def _class_end(self, course: str, started: datetime) -> datetime | None:
        """Timetable end of the recording's class: its course today, else the class that covers the start."""
        best = None
        for slot in today_slots(self.courses, started):
            try:
                begin, end = (started.replace(hour=int(t[:2]), minute=int(t[3:5]), second=0, microsecond=0)
                              for t in (slot["start"].zfill(5), slot["end"].zfill(5)))
            except ValueError:
                continue
            if (course and slot["folder"] == course and end >= started) or (
                    not course and begin - timedelta(minutes=30) <= started <= end):
                best = end if best is None else min(best, end)
        return best

    @staticmethod
    def _new_over(started: datetime) -> dict:
        return {"started": started, "quiet_since": None, "quiet_since_ms": None, "snooze_until": None,
                "muted": False, "paused_since": None, "next_offered": set(), "auto_stopping": False}

    def _all_quiet(self, status: dict) -> bool:
        """No sound on every source that is recording (a PC-sound partner playing a lecture counts as sound)."""
        if not status.get("quiet"):
            return False
        pc = self._state.get("pc") or {}
        return not (pc.get("state") == "recording" and pc.get("status") and not pc["status"].get("quiet"))

    def _next_class(self, course: str, now: datetime) -> dict | None:
        """Today's next class of another course starting within NEXT_CLASS_LEAD_MIN (or started < 15 min ago)."""
        best = None
        for slot in today_slots(self.courses, now):
            try:
                begin = now.replace(hour=int(slot["start"].zfill(5)[:2]), minute=int(slot["start"].zfill(5)[3:5]),
                                    second=0, microsecond=0)
            except ValueError:
                continue
            if slot["folder"] == course or not (begin - timedelta(minutes=NEXT_CLASS_LEAD_MIN) <= now
                                                 <= begin + timedelta(minutes=15)):
                continue
            if best is None or begin < best["begin"]:
                best = {"course": slot["folder"], "start": f"{begin:%H:%M}", "begin": begin}
        return best

    def _check_class_over(self, status: dict) -> None:
        """Called with the lock held on each status line. Sets state["class_over"] / state["next_class"].
        Stops by itself only after AUTO_STOP_QUIET_MIN with no sound on any source (user 2026-09-30)."""
        over = getattr(self, "_over", None)
        if not over:
            return
        now = self.clock()
        if status.get("paused"):  # a pause is on purpose: no silence counting, but a long one is reminded
            over["quiet_since"] = over["quiet_since_ms"] = None
            over["paused_since"] = over["paused_since"] or now
            if (not over["muted"] and not self._state.get("class_over")
                    and now - over["paused_since"] >= timedelta(minutes=PAUSED_REMIND_MIN)
                    and not (over["snooze_until"] and now < over["snooze_until"])):
                self._state["class_over"] = {"reason": f"{PAUSED_REMIND_MIN}분 넘게 일시정지 중 — 지금 소리는 저장되지 않습니다",
                                             "kind": "paused", "at_ms": int(now.timestamp() * 1000)}
            return
        over["paused_since"] = None
        if self._all_quiet(status):
            if over["quiet_since"] is None:  # the engine says "quiet" after 30 s of it
                over["quiet_since"] = now - timedelta(seconds=30)
                over["quiet_since_ms"] = max(0, int(status.get("elapsed_ms") or 0) - 30_000)
        else:
            over["quiet_since"] = over["quiet_since_ms"] = None
            notice = self._state.get("class_over") or {}
            if notice.get("auto_stop_at_ms"):  # sound came back: no automatic stop any more
                notice.pop("auto_stop_at_ms", None)
        course = status.get("course") or ""
        nxt = self._next_class(course, now)
        if nxt and nxt["course"] not in over["next_offered"] and not over["muted"]:
            over["next_offered"].add(nxt["course"])
            self._state["next_class"] = {"course": nxt["course"], "start": nxt["start"],
                                         "at_ms": int(now.timestamp() * 1000)}
            self._attention("next_class")
        quiet_for = now - over["quiet_since"] if over["quiet_since"] else timedelta(0)
        if not over["muted"] and quiet_for >= timedelta(minutes=AUTO_STOP_QUIET_MIN) and not over["auto_stopping"]:
            over["auto_stopping"] = True
            reason = f"{AUTO_STOP_QUIET_MIN}분 넘게 소리가 없어 자동으로 멈춤"
            self._state["auto_stop"] = {"reason": reason, "quiet_since_ms": over["quiet_since_ms"],
                                        "at_ms": int(now.timestamp() * 1000)}
            threading.Thread(target=self._auto_stop, args=(reason, over["quiet_since_ms"]), daemon=True).start()
            return
        if self._state.get("class_over") or over["muted"] or (over["snooze_until"] and now < over["snooze_until"]):
            return
        reason = None
        end = self._class_end(course, over["started"])
        if end and now >= end + timedelta(minutes=CLASS_OVER_GRACE_MIN):
            reason = f"시간표 수업 종료 {end:%H:%M}에서 {CLASS_OVER_GRACE_MIN}분 지남"
        elif quiet_for >= timedelta(minutes=CLASS_OVER_QUIET_MIN):
            reason = f"{CLASS_OVER_QUIET_MIN}분 넘게 소리가 거의 없음"
        if reason:
            notice = {"reason": reason, "kind": "over", "at_ms": int(now.timestamp() * 1000)}
            if over["quiet_since"]:
                notice["auto_stop_at_ms"] = int((over["quiet_since"] + timedelta(minutes=AUTO_STOP_QUIET_MIN)).timestamp() * 1000)
            self._state["class_over"] = notice
            self._attention("class_over")

    attention_hook = None  # set by the app: flash the taskbar button, beep, show the mini window's card

    def _attention(self, kind: str) -> None:
        """Beep, flash, Windows notification (ui/app.py Api._attention) on its own thread. Called from the threads
        that read the engines' lines, so it never raises (D-050): a notice that cannot be given is only logged."""
        hook = getattr(self, "attention_hook", None)
        if not hook:
            return
        try:
            threading.Thread(target=hook, args=(kind,), daemon=True).start()
        except Exception:  # noqa: BLE001 - e.g. no thread can be started; the window still shows the state
            applog.swallowed(f"알림({kind})")

    # ---------- stall and low disk of either engine: said once, never in the way of the recording (D-050)
    def _told(self) -> dict:
        """What the running recording has announced already: engines with an open stall, and low disk."""
        told = getattr(self, "_told_state", None)
        if told is None:
            told = self._told_state = {"stall": set(), "disk": False}
        return told

    def _note_stall(self, who: str, stalled: bool, reason=None) -> None:
        """Called with the lock held. One announcement per stall of one engine (``"main"`` or ``"second"``): at its
        ``stalled`` event, or when its status line first says ``stalled`` - an engine followed through live.json
        after an app restart sends status lines only."""
        open_stalls = self._told()["stall"]
        if not stalled:
            open_stalls.discard(who)
            return
        if who in open_stalls:
            return
        open_stalls.add(who)
        applog.LOG.warning("녹음 장치에서 소리가 끊김(%s, %s): 다시 여는 중", "둘째 소리" if who == "second" else "주 소리",
                           reason or "상태 줄에서 확인")
        self._attention("stalled")

    def _note_disk(self) -> None:
        """Called with the lock held. ``state["disk"]`` = ``{"low", "minutes"}`` for both windows, from the status
        lines of the engines that are recording now, and one announcement per recording when room runs low.

        Each engine reports the minutes its own file could go on for. Two engines fill the same disk, so together
        it lasts 1/(1/a + 1/b): half when both write at the same rate. It stays an estimate: PC sound is written
        only while something plays."""
        rows = []
        if self._state.get("phase") == "recording":
            rows.append(self._state.get("status") or {})
        pc = self._state.get("pc") or {}
        if pc.get("state") == "recording":
            rows.append(pc.get("status") or {})
        rows = [row for row in rows if row.get("disk_low") is not None or row.get("disk_minutes") is not None]
        if not rows:
            self._state["disk"] = None
            return
        low = any(row.get("disk_low") for row in rows)
        each = [float(row["disk_minutes"]) for row in rows if isinstance(row.get("disk_minutes"), int | float)]
        if not each:
            minutes = None
        elif min(each) <= 0:
            minutes = 0
        else:
            minutes = int(1 / sum(1 / value for value in each))
        self._state["disk"] = {"low": bool(low), "minutes": minutes}
        told = self._told()
        if low and not told["disk"]:
            told["disk"] = True
            applog.LOG.warning("녹음 중 디스크 공간 부족: 약 %s분 분량 남음", minutes)
            self._attention("disk_low")

    def _watch(self, who: str, status: dict | None) -> None:
        """Called with the lock held on every status line of an engine (``status=None``: that engine stopped).
        It only informs. An error here must not end the thread that reads the engine's lines, so it is logged
        and dropped."""
        try:
            self._note_stall(who, bool(status and status.get("stalled")))
            self._note_disk()
        except Exception:  # noqa: BLE001
            applog.swallowed("녹음 상태 알림(끊김·디스크)")

    def disk_minutes_left(self) -> int | None:
        """About how many minutes of recording still fit, as last reported (``None`` when unknown)."""
        with self._lock:
            return (self._state.get("disk") or {}).get("minutes")

    def _auto_stop(self, reason: str, quiet_since_ms: int | None) -> None:
        """Save the recording the user forgot to stop; the silent tail is suggested for cutting afterwards."""
        with self._lock:
            sessions = [str(e.session_dir) for e in self._each_engine() if getattr(e, "session_dir", None)]
        for session in sessions:
            try:
                atomic_write_json(Path(session) / "auto_stop.json",
                                  {"reason": reason, "quiet_since_ms": quiet_since_ms, "at": int(time.time() * 1000)},
                                  indent=None, mkdir=False)
            except OSError:
                pass
        self.stop(everything=True)
        with self._lock:
            self._state["class_over"] = None

    def class_over_answer(self, action: str) -> dict:
        """[지금 멈추고 저장] / [계속: 10분 뒤 다시] / [이번 수업은 묻지 않기]. "계속" also restarts the silence
        count (the user is there); "묻지 않기" also turns the automatic stop off for this recording."""
        over = getattr(self, "_over", None)
        with self._lock:
            self._state["class_over"] = None
            if over and action == "snooze":
                over["snooze_until"] = self.clock() + timedelta(minutes=CLASS_OVER_SNOOZE_MIN)
                if over["quiet_since"]:
                    over["quiet_since"] = self.clock()
            elif over and action == "mute":
                over["muted"] = True
        if action == "stop":
            return self.stop(everything=True)
        return {"ok": True}

    @staticmethod
    def _is_short(total_ms) -> bool:
        return isinstance(total_ms, int | float) and total_ms < AUTO_MIN_MS

    def _hold_short(self, session_dir: str, role: str, total_ms) -> None:
        """Called with the lock held. A mis-press (under a minute) waits for the user instead of becoming a lecture
        (user 2026-09-30; 9/29 had several such test recordings that each became a lecture)."""
        short = self._state.get("short") or {"sessions": [], "pair": self._pair}
        short["sessions"].append({"session_dir": session_dir, "role": role, "total_ms": total_ms})
        self._state["short"] = short
        if role == "main":
            self._state["link"] = {"state": "short", "total_ms": total_ms}
        else:
            (self._state.get("pc") or {})["link"] = {"state": "short", "total_ms": total_ms}

    def short_answer(self, action: str) -> dict:
        """[버리기] → the held sessions go to the Recycle Bin (D-058); [남기기] → linked like any recording."""
        with self._lock:
            short = self._state.get("short")
            self._state["short"] = None
        if not short:
            return {"ok": False}
        for item in short["sessions"]:
            if action == "discard":
                with self._lock:
                    if item["role"] == "main":
                        self._state["link"] = {"state": "discarding"}
                threading.Thread(target=self._discard_short, args=(item,), daemon=True).start()
            else:
                with self._lock:
                    if item["role"] == "main":
                        self._state["link"] = {"state": "linking"}
                threading.Thread(target=self._after_recording, args=(Path(item["session_dir"]), item["role"],
                                                                     short["pair"]), daemon=True).start()
        return {"ok": True}

    def _discard_short(self, item: dict) -> None:
        result = self.discard_hook(Path(item["session_dir"]).name) if self.discard_hook else {"ok": False}
        with self._lock:
            target = self._state if item["role"] == "main" else (self._state.get("pc") or {})
            target["link"] = {"state": "discarded" if result.get("ok") else "discard_failed",
                              "reason": None if result.get("ok") else result.get("error")}
        self._bump_library()

    def next_class_answer(self, action: str) -> dict:
        """[다음 수업 녹음으로 바꾸기]: save this recording, then record the next class with the same source(s)."""
        with self._lock:
            offer = self._state.get("next_class")
            self._state["next_class"] = None
        if action != "switch" or not offer:
            return {"ok": True}
        status = self._state.get("status") or {}
        mode = status.get("mode") or self.selection.get("mode", "mic")
        with_pc = bool(self._pair and not self._pair.get("independent") and self._pair.get("pc"))
        result = self.stop(everything=True)
        if not result.get("ok"):
            return result
        threading.Thread(target=self._start_when_saved, args=(offer["course"], mode, with_pc), daemon=True).start()
        return {"ok": True, "course": offer["course"]}

    def _start_when_saved(self, course: str, mode: str, with_pc: bool, timeout_s: float | None = None) -> None:
        if timeout_s is None:
            timeout_s = self.FINALIZE_LIMIT_S + 5  # past the limit, start() itself lets a stuck merge go
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._state["phase"] not in ("starting", "recording", "stopping", "finalizing") \
                    and not self._companion_active():
                break
            time.sleep(0.5)
        result = self.start(course, mode, self.selection.get("device_name"), "user_pick", with_pc)
        with self._lock:
            self._state["switched"] = {"course": course, "ok": bool(result.get("ok")), "reason": result.get("reason")}

    def _log_start(self, **decision) -> None:
        """One JSON line per recording start: which course was asked for and what was used (diagnostics)."""
        try:
            logs = self.root / "_lecturehub" / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            with (logs / "app_start.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"at": self.clock().isoformat(), **decision}, ensure_ascii=False) + "\n")
        except OSError:
            pass  # a log must never block a recording

    def _set(self, **changes) -> None:
        with self._lock:
            self._state.update(changes)

    def _on_event(self, event: dict) -> None:
        kind = event.get("type")
        with self._lock:
            if kind == "started":
                self._state.update(phase="recording", status={"elapsed_ms": 0, "device": event["device"],
                                                              "course": event["course"], "mode": event["mode"],
                                                              "session_dir": event["session_dir"]},
                                   bookmarks=[], summary=None, error=None, keep_awake=event.get("keep_awake"),
                                   class_over=None)
                self._over = self._new_over(self.clock())
                if self._pair is not None:
                    self._pair["mic"] = event["session_dir"]
                    self._write_pair()
                else:  # a PC-sound partner, when there is one, is the session transcribed live instead
                    self._start_live(event["session_dir"], event.get("course") or "")
            elif kind == "status":
                if self._state["phase"] in ("starting", "recording"):
                    self._state["phase"] = "recording"
                self._state["status"] = event
                self._check_class_over(event)
                self._watch("main", event)
            elif kind == "stalled":  # no sound from the device: the engine is opening it again (recorder/service.py)
                self._note_stall("main", True, event.get("reason"))
            elif kind == "recovered":
                self._note_stall("main", False)
                applog.LOG.warning("녹음 장치에서 소리가 다시 들어와 이어 녹음함(끊긴 시간 %s ms)", event.get("lost_ms"))
            elif kind == "bookmark":
                self._state["bookmarks"].append({"n": event["n"], "at_ms": event["at_ms"]})
            elif kind == "stopped":
                self._finalizing_since = time.monotonic()
                report = event.get("report") or {}
                auto = self._state.get("auto_stop")
                self._state.update(class_over=None, next_class=None)
                self._state.update(phase="finalizing", summary={
                    "auto_stop": auto,
                    "total_ms": report.get("total_ms"), "dropped_samples": report.get("dropped_samples"),
                    "partial": report.get("partial"), "cause": report.get("cause"),
                    "certainty": report.get("certainty"), "session_dir": event.get("session_dir"),
                    "master": None, "bookmarks": event.get("bookmarks", len(self._state["bookmarks"])),
                    "error": report.get("error"), "paused_ms": report.get("paused_ms") or 0,
                    **_gap_summary(report)})
                self._watch("main", None)
            elif kind == "compacted":  # after "finalized": the clean-up of the WAV files (recorder/compact.py)
                note, summary = _cleanup_note(event.get("result")), self._state.get("summary")
                if note and summary and summary.get("session_dir") == event.get("session_dir"):
                    summary["cleanup"] = note  # said once, on the summary line of this very recording
            elif kind == "finalized":
                summary = self._state.get("summary") or {}
                summary.update(master=event.get("master"))
                self._state.update(phase="done", summary=summary, link={"state": "linking"})
                session_dir = summary.get("session_dir") or event.get("session_dir")
                if session_dir and self._is_short(summary.get("total_ms")):
                    self._hold_short(session_dir, "main", summary.get("total_ms"))
                elif session_dir:  # S2: link to the lecture and maybe transcribe - never blocks the recording
                    threading.Thread(target=self._after_recording, args=(Path(session_dir), "main", self._pair),
                                     daemon=True).start()
            elif kind == "discarded":
                self._state.update(phase="done", summary={"discarded": True, "session_dir": event.get("session_dir"),
                                                          "total_ms": (event.get("report") or {}).get("total_ms")},
                                   link=None, class_over=None)
                threading.Thread(target=self._recycle_discarded, args=(event.get("session_dir"),), daemon=True).start()
            elif kind == "finalize_error":
                summary = self._state.get("summary") or {}
                summary.update(finalize_error=event.get("message"))
                self._state.update(phase="done", summary=summary)
            elif kind == "error":
                self._state.update(phase="error" if self._state["phase"] != "recording" else "recording",
                                   error=event.get("message"))
            elif kind == "exited":
                if self._state["phase"] in ("starting",):
                    self._state["phase"] = "error"
                    self._state["error"] = self._state.get("error") or "녹음 엔진이 시작하지 못했습니다."
                elif self._state["phase"] in ("recording", "stopping", "finalizing"):
                    self._state["phase"] = "done" if self._state.get("summary") else "error"
                    if self._state["phase"] == "error":
                        self._state["error"] = "녹음 엔진이 예기치 않게 끝났습니다. 세그먼트는 보존됩니다."

    def _on_pc_event(self, event: dict) -> None:
        """The second engine (the PC-sound partner, or the other source started on its own: PC sound or the
        microphone): its own status and file. The main engine drives the screen; a stall, low disk and the gaps of
        this one are kept in ``state["pc"]`` and announced like the main engine's."""
        kind = event.get("type")
        with self._lock:
            pc = self._state.get("pc") or {}
            if kind == "started":
                mode = self._companion_mode or "loopback"
                pc = {"state": "recording", "session_dir": event["session_dir"], "status": None, "mode": mode}
                if self._pair is not None:
                    self._pair["mic" if mode == "mic" else "pc"] = event["session_dir"]
                    self._write_pair()
                live = getattr(self, "_live", None)
                if not (live and live["process"].poll() is None):  # one live job at a time (D-059 pressure)
                    self._start_live(event["session_dir"], (self._state.get("status") or {}).get("course") or "")
            elif kind == "status":
                pc["status"] = {k: event.get(k) for k in ("elapsed_ms", "rms", "peak", "quiet", "paused",
                                                          "dropped_samples", "stalled", "recoveries", "disk_low",
                                                          "disk_minutes")}
                self._state["pc"] = pc  # before the bookkeeping below reads it
                self._watch("second", pc["status"])
            elif kind == "stalled":
                self._note_stall("second", True, event.get("reason"))
            elif kind == "recovered":
                self._note_stall("second", False)
                applog.LOG.warning("둘째 소리(%s)가 다시 들어와 이어 녹음함(끊긴 시간 %s ms)", pc.get("mode"),
                                   event.get("lost_ms"))
            elif kind == "stopped":
                report = event.get("report") or {}
                pc.update(state="finalizing", total_ms=report.get("total_ms"), cause=report.get("cause"),
                          partial=report.get("partial"), **_gap_summary(report))
                self._state["pc"] = pc
                self._watch("second", None)
            elif kind == "compacted":
                note = _cleanup_note(event.get("result"))
                if note and pc.get("session_dir") == event.get("session_dir"):
                    pc["cleanup"] = note
            elif kind == "finalized":
                pc.update(state="done", master=event.get("master"))
                session_dir = pc.get("session_dir") or event.get("session_dir")
                if session_dir and self._is_short(pc.get("total_ms")):
                    self._hold_short(session_dir, "pc", pc.get("total_ms"))
                elif session_dir:
                    threading.Thread(target=self._after_recording, args=(Path(session_dir), "pc", self._pair),
                                     daemon=True).start()
            elif kind == "discarded":
                pc.update(state="discarded")
                threading.Thread(target=self._recycle_discarded, args=(event.get("session_dir"),), daemon=True).start()
            elif kind in ("finalize_error", "error"):
                pc.update(state="error", error=event.get("message"))
                if self._pair is not None:
                    self._pair["pc_failed"] = True
                    if self._pair.get("mic_link"):  # the mic was waiting for the PC file: transcribe the mic
                        threading.Thread(target=self._maybe_auto_transcribe, args=(self._pair["mic_link"],),
                                         daemon=True).start()
            elif kind == "exited" and pc.get("state") in ("recording", "finalizing"):
                pc.update(state="error", error=pc.get("error") or "PC 소리 녹음이 예기치 않게 끝났습니다.")
                if self._pair is not None:
                    self._pair["pc_failed"] = True
            self._state["pc"] = pc

    # ---------- live transcription (D-059): consented courses only, below-normal priority, recording first
    def _synthetic_only(self) -> bool:
        """A synthetic source (verification) never touches a real library: no linking, no Google (2026-09-29:
        a UI check with the fake engine sent a sine recording through live transcription and left pending items)."""
        if not self.fake_engine:
            return False
        from lecturehub.transcription.cli import is_synthetic_root

        return not is_synthetic_root(self.root)

    def _start_live(self, session_dir: str, course: str) -> None:
        if self._synthetic_only():
            return
        if not (self.config and getattr(self.config, "live_transcription", True)):
            return
        if not self.auto_transcription_allowed(course):
            return
        try:
            process = spawn([self.python, "-m", "lecturehub", "transcribe", "live", session_dir, "--course", course,
                             "--root", str(self.root)], stdout=subprocess.DEVNULL, below_normal=True)
        except OSError:
            return  # live transcription is a convenience; the normal job runs after the recording
        self._live = {"session_dir": session_dir, "process": process}
        self._state["live"] = {"session_dir": session_dir, "state": "running"}
        threading.Thread(target=self._wait_live, args=(process,), daemon=True).start()

    def _wait_live(self, process: subprocess.Popen) -> None:
        process.wait()
        with self._lock:
            (self._state.get("live") or {})["state"] = "finished"
        self._register_finished_jobs()  # the live job becomes the lecture's transcript like any other job

    def _live_alive(self, session_dir: str) -> bool:
        live = getattr(self, "_live", None)
        return bool(live and live["session_dir"] == session_dir and live["process"].poll() is None)

    def _write_pair(self) -> None:
        """pair.json in both session folders: which mic and PC-sound sessions belong together."""
        pair = {k: self._pair.get(k) for k in ("mic", "pc")}
        if self._pair.get("independent"):
            pair["independent"] = True
        for key in ("mic", "pc"):
            if pair.get(key):
                try:
                    atomic_write_json(Path(pair[key]) / "pair.json", pair, indent=None, mkdir=False)
                except OSError:
                    pass

    def _on_probe(self, event: dict) -> None:
        kind = event.get("type")
        with self._lock:
            probe = self._state.get("probe") or {}
            if kind == "probe_started":
                probe = {"running": True, "device": event.get("device"), "peak": None}
            elif kind == "probe":
                probe.update(running=True, peak=event.get("peak"), rms=event.get("rms"),
                             remaining_ms=event.get("remaining_ms"))
            elif kind == "probe_done":
                probe.update(running=False, verdict=event.get("verdict"), peak=event.get("peak"),
                             rms=event.get("rms"), device=event.get("device"), mic_blocked=None)
                if getattr(self, "_probe_mode", "mic") == "mic" and event.get("verdict") in ("silent", "no_data"):
                    probe["mic_blocked"] = self._mic_blocked()  # silence may be the Windows privacy switch
            elif kind == "error":
                probe.update(running=False, verdict="error", message=event.get("message"))
            elif kind == "exited" and probe.get("running"):
                probe.update(running=False, verdict=probe.get("verdict") or "error")
            self._state["probe"] = probe

    def _engine_args(self, mode: str, device_name: str | None) -> list[str]:
        args = [self.python, "-m", "lecturehub.recorder.service", "--mode", mode]
        if device_name and mode == "mic":
            args += ["--device-name", device_name]
        if self.fake_engine:
            args += ["--fake"]
        return args

    # ---------- actions
    def probe(self, mode: str = "mic", device_name: str | None = None, seconds: float = 5) -> dict:
        if self._state["phase"] in ("starting", "recording", "stopping"):
            return {"ok": False, "reason": "녹음 중에는 점검하지 않습니다."}
        self.probe_engine = EngineLink(self._on_probe)
        self._probe_mode = mode
        self._set(probe={"running": True, "peak": None, "remaining_ms": int(seconds * 1000)})
        self.probe_engine.spawn(self._engine_args(mode, device_name) + ["--probe", str(seconds)])
        return {"ok": True}

    def _mic_blocked(self) -> str | None:
        """The sentence to show when Windows keeps apps from the microphone, else ``None`` (never raises)."""
        if self.fake_engine:
            return None
        try:
            from lecturehub.recorder import preflight

            access = preflight.microphone_access()
            return preflight.BLOCKED_TEXT.get(access["blocked_by"]) if access["allowed"] is False else None
        except Exception:  # noqa: BLE001 - a check that cannot run is not a reason to bother the user
            applog.swallowed("마이크 접근 설정 확인")
            return None

    def _start_notices(self, mode: str) -> list[dict]:
        """Things worth saying before a recording starts; none of them stops it (D-050)."""
        notices = []
        try:
            from lecturehub.recorder import preflight

            if mode == "mic":
                blocked = self._mic_blocked()
                if blocked:
                    notices.append({"kind": "mic_blocked", "text": blocked + " 이대로 녹음하면 소리가 담기지 않습니다."})
            warning = None if self.fake_engine else preflight.disk_warning(self.out_dir)
            if warning:
                notices.append({"kind": "disk_low", "text": warning})
        except Exception:  # noqa: BLE001
            applog.swallowed("녹음 시작 전 확인")
        return notices

    def open_mic_settings(self) -> dict:
        from lecturehub.recorder import preflight

        try:
            os.startfile(preflight.SETTINGS_URI)  # noqa: S606 - a fixed Windows Settings address
        except (OSError, AttributeError) as exc:
            return {"ok": False, "error": f"Windows 설정을 열지 못했습니다: {exc}"}
        return {"ok": True}

    def _synthetic_out_refused(self) -> str | None:
        """A synthetic source never writes into a real library (2026-09-29: a UI check with the fake engine put two
        sine sessions into the real _lecturehub\recordings). Allowed: a folder outside any library, or a
        library that declares itself synthetic."""
        if not self.fake_engine:
            return None
        from lecturehub.transcription.cli import is_synthetic_root

        out = Path(self.out_dir).resolve()
        library = next((folder.parent for folder in (out, *out.parents) if folder.name == "_lecturehub"), None)
        if library is None or is_synthetic_root(library):
            return None
        return (f"합성 소리(검증용)는 실제 라이브러리에 저장하지 않습니다({out}). 녹음 설정의 out_dir을 임시 폴더나 "
                "synthetic_root 표지가 있는 라이브러리로 바꾸세요.")

    def start(self, course: str, mode: str = "mic", device_name: str | None = None,
              course_source: str = "unknown", with_pc: bool = False) -> dict:
        asked = {"course_in": course, "source_in": course_source}
        refused = self._synthetic_out_refused()
        if refused:
            return {"ok": False, "reason": refused}
        if course_source != "user_pick":
            # Not the user's pick: take the timetable's class at the moment recording starts (the window may
            # have been open since before the class window began).
            course = self.suggest_course() or ""
            course_source = "timetable_auto" if course else "unknown"
        elif not course:
            course_source = "unknown"  # "no course" is not a course choice (D-052)
        permission = self.recording_permission(course)
        if not permission.get("allowed"):
            return {"ok": False, "reason": permission.get("reason")}
        with_pc = bool(with_pc) and mode == "mic"
        with self._lock:
            if self._companion_active():
                return {"ok": False, "reason": "따로 녹음 중인 것이 아직 돌고 있습니다. 작은 버튼으로 먼저 멈추세요."}
        self._log_start(**asked, course=course, source=course_source, mode=mode, with_pc=with_pc)
        self._remember_source(course, mode, with_pc)
        with self._lock:
            if self._state["phase"] == "finalizing":
                waited = time.monotonic() - (getattr(self, "_finalizing_since", None) or time.monotonic())
                if waited <= self.FINALIZE_LIMIT_S:
                    left = max(1, math.ceil((self.FINALIZE_LIMIT_S - waited) / 60))
                    return {"ok": False, "reason": "이전 녹음을 정리하는 중이라 아직 새 녹음을 시작할 수 없어요 "
                                                   f"(최대 {left}분)", "finalizing": True}
                self._release_stuck_finalize()  # a merge past its limit never keeps the next class from recording
            if self._state["phase"] in ("starting", "recording", "stopping", "finalizing"):
                return {"ok": False, "reason": "이미 녹음 중입니다."}
            self._state.update(phase="starting", error=None, summary=None, bookmarks=[], transcription=None,
                               link=None, mai=None, pc=None, auto_stop=None, next_class=None, class_over=None,
                               switched=None, short=None, disk=None)
            self._told_state = None  # a new recording: a stall or low disk is announced afresh
            self._pair = {"mic": None, "pc": None, "pc_failed": False} if with_pc else None
            self._companion_mode = "loopback" if with_pc else None
            # From here on the engine of the recording before this one is no longer "the" engine: it may still be
            # tidying up after its merge, and its last words (its process ending) must not reach this recording
            # (``_dispatch_event``). The new process is started below, outside the lock.
            link = EngineLink(lambda event: self._dispatch_event(link, event))
            self.engine = link
            self.pc_engine = None
        if self.probe_engine and self.probe_engine.process and self.probe_engine.process.poll() is None:
            self.probe_engine.process.wait(timeout=10)  # let the level check release the device first
        self.out_dir.mkdir(parents=True, exist_ok=True)
        quiet = self.config.quiet_dbfs if self.config else -50.0
        common = ["--course", course or "", "--course-source",
                  course_source if course_source in ("user_pick", "timetable_auto") else "unknown",
                  "--out", str(self.out_dir), "--quiet-dbfs", str(quiet)]
        self._common = common
        link.spawn(self._engine_args(mode, device_name) + common)
        if with_pc:  # a second engine for the PC sound; a failure there never touches the mic recording
            time.sleep(0.3)  # distinct session folder names (one per second)
            self.pc_engine = self._second_link()
            self.pc_engine.spawn(self._engine_args("loopback", None) + common)
        return {"ok": True, "warning": permission.get("warning", ""), "notices": self._start_notices(mode)}

    FINALIZE_LIMIT_S = 600  # the most a merge may hold back the next recording (recorder/finalize.py)

    def _release_stuck_finalize(self) -> None:
        """Called with the lock held: the merge ran past its limit (ffmpeg stuck). The phase is let go so that a
        new recording can start; the old engine keeps its segments, and if it finishes later its session is still
        linked (``_dispatch_event``). Nothing is deleted."""
        summary = self._state.get("summary") or {}
        summary["finalize_error"] = ("병합이 너무 오래 걸려 기다리지 않고 넘어갔습니다. "
                                     "녹음 조각은 그대로 남아 있습니다.")
        self._state.update(phase="done", summary=summary)
        self._stale_engine = self.engine
        self._finalizing_since = None

    def _dispatch_event(self, link, event: dict) -> None:
        """Only the engine of the current recording drives the screen.

        An engine says ``finalized`` (a new recording may start), then tidies up and only then ends. When the next
        recording has started by then, the old engine's ``exited`` used to be applied to the new one: the screen
        went to "error" and the running recording could no longer be stopped from the app (found in the review of
        2026-11-19; older than that review). Events of an engine that is no longer ``self.engine`` are dropped,
        with one exception: the engine whose merge was let go after its time limit still gets its recording
        linked when it finishes later."""
        if link is not None and link is not self.engine:
            if (link is getattr(self, "_stale_engine", None) and event.get("type") == "finalized"
                    and event.get("session_dir")):
                threading.Thread(target=self._after_recording, args=(Path(event["session_dir"]), "main", None),
                                 daemon=True).start()
            return
        self._on_event(event)

    def _second_link(self) -> EngineLink:
        """A link for the second engine whose events count only while it is the current second engine (the same
        rule as ``_dispatch_event``: an earlier one that ends late must not mark the new one as failed)."""
        link = EngineLink(lambda event: self._on_pc_event(event) if link is self.pc_engine else None)
        return link

    def _companion_active(self) -> bool:
        return (self._state.get("pc") or {}).get("state") in ("starting", "recording", "stopping", "finalizing")

    def start_companion(self) -> dict:
        """While one source records, start the other one on its own: its own file, its own stop, its own
        transcription (user 2026-09-29: "하나 누르면 하나 작게 더 생겨서 … 개별적으로")."""
        refused = self._synthetic_out_refused()
        if refused:
            return {"ok": False, "reason": refused}
        with self._lock:
            if self._state["phase"] != "recording" or not self.engine:
                return {"ok": False, "reason": "먼저 녹음을 시작하세요."}
            if self._companion_active():
                return {"ok": False, "reason": "이미 함께 녹음 중입니다."}
            status = self._state.get("status") or {}
            permission = self.recording_permission(status.get("course") or "")
            if not permission.get("allowed"):
                return {"ok": False, "reason": permission.get("reason")}
            main_mode = status.get("mode") or "mic"
            mode = "loopback" if main_mode == "mic" else "mic"
            main_session = status.get("session_dir")
            self._pair = {"mic": main_session if main_mode == "mic" else None,
                          "pc": main_session if main_mode == "loopback" else None,
                          "pc_failed": False, "independent": True}
            self._companion_mode = mode
            self._state["pc"] = {"state": "starting", "mode": mode, "session_dir": None, "status": None}
            common = self._common or ["--course", status.get("course") or "", "--course-source", "unknown",
                                      "--out", str(self.out_dir), "--quiet-dbfs",
                                      str(self.config.quiet_dbfs if self.config else -50.0)]
        if self.probe_engine and self.probe_engine.process and self.probe_engine.process.poll() is None:
            self.probe_engine.process.wait(timeout=10)
        self.pc_engine = self._second_link()
        self.pc_engine.spawn(self._engine_args(mode, self.selection.get("device_name") if mode == "mic" else None)
                             + common)
        return {"ok": True, "mode": mode}

    def stop_companion(self) -> dict:
        """Stop only the second source; the first keeps recording."""
        with self._lock:
            pc = self._state.get("pc") or {}
            if not self.pc_engine or pc.get("state") not in ("starting", "recording"):
                return {"ok": False}
            pc["state"] = "stopping"
        self.pc_engine.request_stop()
        return {"ok": True}

    def set_selection(self, course: str, mode: str, device_name: str | None,
                      course_source: str = "unknown", with_pc: bool = False) -> dict:
        """The main window's choices; the mini window starts recordings with them."""
        self.selection = {"course": course or "", "mode": mode if mode in ("mic", "loopback") else "mic",
                          "device_name": device_name, "course_source": course_source, "with_pc": bool(with_pc)}
        return {"ok": True}

    def start_selected(self) -> dict:
        return self.start(self.selection["course"], self.selection["mode"], self.selection["device_name"],
                          self.selection.get("course_source", "unknown"), self.selection.get("with_pc", False))

    def _attach(self, session_dir: Path) -> None:
        meta = _read_json(session_dir / "session.json") or {}
        self.engine = EngineLink(self._on_event)
        self._state.update(phase="recording", attached=True, status={
            "elapsed_ms": 0, "device": meta.get("device_name"), "course": meta.get("course", ""),
            "mode": meta.get("mode"), "session_dir": str(session_dir)},
            bookmarks=_read_bookmarks(session_dir))
        self.engine.attach(session_dir)
        started = meta.get("started_at")
        # the "class is over" notice and the automatic stop also cover a recording found at app start
        self._over = self._new_over(datetime.fromtimestamp(started / 1000).astimezone()
                                    if isinstance(started, int | float) else self.clock())
        pair = _read_json(session_dir / "pair.json") or {}
        me = str(session_dir)
        other = pair.get("pc") if pair.get("mic") == me else pair.get("mic") if pair.get("pc") == me else None
        if other and Path(other).is_dir() and not (Path(other) / "stop.json").exists():
            other_mode = "loopback" if pair.get("pc") == other else "mic"
            self._pair = {"mic": other if other_mode == "mic" else me, "pc": other if other_mode == "loopback" else me,
                          "pc_failed": False, **({"independent": True} if pair.get("independent") else {})}
            self._companion_mode = other_mode
            self.pc_engine = self._second_link()
            self._state["pc"] = {"state": "recording", "session_dir": other, "status": None, "mode": other_mode}
            self.pc_engine.attach(Path(other))

    def _each_engine(self):
        return [e for e in (self.engine, self.pc_engine) if e is not None]

    def bookmark(self) -> dict:
        if self._state["phase"] != "recording" or not self.engine:
            return {"ok": False}
        self.engine.request_bookmark()
        status = self._state.get("status") or {}
        return {"ok": True, "at_ms": status.get("elapsed_ms", 0)}

    def pause(self, paused: bool) -> dict:
        """Pause/resume the running recording (the engine polls control.pause)."""
        if self._state["phase"] != "recording" or not self.engine:
            return {"ok": False}
        for engine in self._each_engine():  # the PC-sound file pauses with the mic
            engine.request_pause(bool(paused))
        return {"ok": True, "paused": bool(paused)}

    def discard(self) -> dict:
        """User: throw the running recording away now — engines stop without a master; folders go to the Recycle Bin."""
        if self._state["phase"] != "recording" or not self.engine:
            return {"ok": False}
        self._set(phase="stopping")
        for engine in self._each_engine():  # a source started on its own is thrown away too (user 2026-09-30)
            engine.request_discard()
        with self._lock:
            pc = self._state.get("pc") or {}
            if pc.get("state") in ("starting", "recording"):
                pc["state"] = "stopping"
        return {"ok": True}

    discard_hook = None  # set by the app: recycles a session folder through the library (D-058)

    def _recycle_discarded(self, session_dir: str | None) -> None:
        if not session_dir:
            return
        live = getattr(self, "_live", None)
        if live and live["session_dir"] == session_dir:
            try:
                live["process"].wait(timeout=60)  # the live job cancels itself when it sees the discard
            except subprocess.TimeoutExpired:
                pass
        result = self.discard_hook(Path(session_dir).name) if self.discard_hook else {"ok": False}
        with self._lock:
            summary = self._state.get("summary") or {}
            if summary.get("session_dir") == session_dir:
                summary["recycled"] = bool(result.get("ok"))
                summary["recycle_error"] = None if result.get("ok") else result.get("error")
        self._bump_library()

    def stop(self, everything: bool = False) -> dict:
        """The main button stops what started with it; ``everything`` (auto-stop, class switch) stops all."""
        if self._state["phase"] != "recording" or not self.engine:
            return {"ok": False}
        self._set(phase="stopping")
        engines = self._each_engine() if everything else self._first_engines()
        for engine in engines:
            engine.request_stop()
        if everything and self.pc_engine in engines:
            with self._lock:
                pc = self._state.get("pc") or {}
                if pc.get("state") in ("starting", "recording"):
                    pc["state"] = "stopping"
        return {"ok": True}

    def _first_engines(self):
        """The main button's engines: both when they started together, only the first when the second was
        started on its own (it stops with its own button)."""
        return [self.engine] if (self._pair or {}).get("independent") and self.engine else self._each_engine()

    @property
    def recording(self) -> bool:
        return self._state["phase"] in ("starting", "recording", "stopping") or self._companion_active()

    def recording_anywhere(self, max_age: float = 5.0) -> bool:
        """AGENTS §2.7: this app's recording, or one started outside the app (terminal ``record``/``class``,
        or an engine left running when the app was closed). The lock-file scan is cached for ``max_age`` s."""
        if self.recording:
            return True
        now = time.monotonic()
        cached = getattr(self, "_outside_cache", None)
        if cached is not None and now - cached[0] < max_age:
            return cached[1]
        try:
            busy = find_running_session(self.out_dir) is not None
            default = self.root / "_lecturehub" / "recordings"  # what `transcribe run` checks
            if not busy and default != self.out_dir:
                busy = find_running_session(default) is not None
        except OSError:
            busy = False
        self._outside_cache = (now, busy)
        return busy

    # ---------- after recording
    def transcription_check(self, master: str | None = None, course: str | None = None) -> dict:
        """For the recording just made, or an explicit recording (lecture screen)."""
        from lecturehub.transcription.consent import NOTICES, ConsentStore

        summary = {"master": master} if master else (self._state.get("summary") or {})
        course = course if course is not None else (self._state.get("status") or {}).get("course", "")
        store = ConsentStore(self.root / "_lecturehub" / "consent.json")
        return {"master": summary.get("master"), "course": course,
                # A blank course is asked every time and never stored (D-052).
                "needs_consent": not _named(course) or not store.granted("gemini_transcribe", course),
                "once_only": not _named(course), "notice": NOTICES["gemini_transcribe"]}

    def _jobs_for(self, audio: str) -> list[dict]:
        """Existing transcription jobs for the same recording (by path), newest first."""
        jobs = self.root / "_lecturehub" / "jobs"
        found = []
        for folder in jobs.iterdir() if jobs.is_dir() else []:
            state = _read_json(folder / "state.json") or {}
            live_dir = (state.get("live") or {}).get("session_dir")
            same = (state.get("audio") and Path(state["audio"]) == Path(audio)) or (
                live_dir and Path(live_dir) == Path(audio).parent)
            if not same or state.get("dry_run_1min"):
                continue
            found.append(state)  # a live job counts too; if its process died it is resumed, never replaced
        return sorted(found, key=lambda s: s.get("created_at") or 0, reverse=True)

    def transcription_start(self, consent: bool, master: str | None = None, course: str | None = None,
                            language: str | None = None, again: bool = False) -> dict:
        from lecturehub.transcription.consent import NOTICES, ConsentStore

        info = self.transcription_check(master, course)
        if not info["master"]:
            return {"ok": False, "reason": "병합된 녹음 파일이 없습니다."}
        live_jobs = [s for s in self._jobs_for(info["master"]) if s.get("live") and not s.get("audio")
                     and s.get("state") not in ("cancelled", "done", "qa_warning")]
        if live_jobs and not self._live_alive(live_jobs[0]["live"]["session_dir"]):
            self._resume_job(live_jobs[0]["job_id"])  # review L2: continue the live job, never a second full one
            return {"ok": True, "resumed": live_jobs[0]["job_id"]}
        finished = ("done", "qa_warning")
        existing = [s for s in self._jobs_for(info["master"])
                    if s.get("state") in ("running", "queued", "waiting_for_quota", "waiting_master", *finished)
                    and not (again and language and s.get("state") in finished
                             and ((s.get("preset") or {}).get("language") or "ko") != language)]
        if existing:  # never send the same recording twice (9/29: two jobs spent the quota twice)
            return {"ok": False, "reason": f"이 녹음은 이미 전사 작업이 있습니다({existing[0]['state']})",
                    "job": existing[0].get("job_id")}
        store = ConsentStore(self.root / "_lecturehub" / "consent.json")
        extra: list[str] = []
        if info["needs_consent"]:
            if not consent:
                return {"ok": False, "reason": "동의하지 않아 업로드하지 않았습니다."}
            if info["once_only"]:
                extra = ["--consent-once"]  # this recording only; nothing stored
            else:
                store.grant("gemini_transcribe", info["course"], int(time.time() * 1000),
                            NOTICES["gemini_transcribe"], by="user_app_dialog")
        logs = self.root / "_lecturehub" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        log = logs / f"transcribe_{datetime.now().astimezone():%Y%m%d_%H%M%S}.log"
        started_at = time.time()
        handle = log.open("wb")
        if language:
            extra += ["--language", language]
        if again:
            extra += ["--again"]  # only reached for another language (the guard above)
        process = spawn([self.python, "-m", "lecturehub", "transcribe", "run", info["master"],
                         "--course", info["course"], "--root", str(self.root), *extra], stdout=handle)
        handle.close()
        self._set(transcription={"state": "running", "log": str(log), "pid": process.pid,
                                 "started_at": started_at, "master": info["master"], "job": None})
        threading.Thread(target=self._watch_transcription, args=(process, started_at, info["master"]),
                         daemon=True).start()
        return {"ok": True, "log": str(log)}

    def _watch_transcription(self, process: subprocess.Popen, started_at: float, master: str) -> None:
        from lecturehub.transcription.job import level1

        jobs = self.root / "_lecturehub" / "jobs"
        while True:
            state = None
            if jobs.is_dir():
                for folder in sorted(jobs.iterdir(), reverse=True)[:5]:
                    candidate = _read_json(folder / "state.json")
                    if candidate and candidate.get("audio") == str(Path(master).resolve()) and \
                            candidate.get("created_at", 0) / 1000 >= started_at - 5:
                        state = candidate
                        break
            done = process.poll() is not None
            if state:
                self._set(transcription={**(self._state.get("transcription") or {}), "job": state["job_id"],
                                         "state": state["state"], "label": level1(state),
                                         "output": state.get("output")})
            if done:
                current = self._state.get("transcription") or {}
                if not state:
                    current.update(state="failed", label="실패(로그 확인)")
                self._set(transcription=current)
                if state and state.get("state") in ("done", "qa_warning"):
                    self._register_finished_jobs()  # S2: the lecture screen shows it as the transcript in use
                return
            time.sleep(1.0)

    # ---------- S2: lecture link, automatic rough transcription, pending confirmation
    def _cli(self, *args: str, timeout: float = 180) -> object:
        """Run ``lecturehub <args>`` in its own process (the DB never loads into the recording app)."""
        result = subprocess.run(
            [self.python, "-m", "lecturehub", *args, "--root", str(self.root)], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, stdin=subprocess.DEVNULL, check=False,
            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
            env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
        if result.returncode:
            # the command line draws a box around its errors: keep the words, drop the frame
            frame = "│┃|╭╮╰╯┌┐└┘─━"
            lines = [line.strip().strip(frame).strip() for line in (result.stderr or result.stdout).splitlines()]
            lines = [line for line in lines if line and line.strip("─━ ") not in ("", "Error")]
            raise RuntimeError(lines[-1] if lines else f"lecturehub {args[0]} failed")
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        return json.loads(lines[-1]) if lines else None

    def _bump_library(self) -> None:
        with self._lock:
            self._state["library_version"] += 1

    def auto_transcription_allowed(self, course: str) -> bool:
        """Only a real course with the user's own consent is sent without asking (D-052)."""
        from lecturehub.transcription.consent import ConsentStore

        return _named(course) and ConsentStore(self.root / "_lecturehub" / "consent.json").granted(
            "gemini_transcribe", course)

    def _after_recording(self, session_dir: Path, role: str = "main", pair: dict | None | object = ...) -> None:
        """``pair``: the mic/PC pair as it was when this recording finished (a new recording may start while
        linking runs); omitted → the current one."""
        if pair is ...:
            pair = self._pair
        if self._synthetic_only():
            with self._lock:
                (self._state.get("pc") or {} if role == "pc" else self._state)["link"] = {
                    "state": "skipped", "reason": "합성 소스 녹음은 실제 라이브러리에 연결하지 않습니다"}
            return
        lead = str(self.config.lead_minutes if self.config else 15)
        try:
            link = self._cli("lecture", "ingest-app", str(session_dir), "--lead-min", lead)
        except Exception as exc:  # noqa: BLE001 - linking may fail; the recording is already safe
            link = {"state": "error", "reason": str(exc)}
        with self._lock:
            if role == "pc":
                (self._state.get("pc") or {})["link"] = link
            else:
                self._state["link"] = link
        self._bump_library()
        if getattr(self, "_live", None):  # a live job may have finished before the link existed
            self._register_finished_jobs()
        # One transcription per class: the PC sound (digital, clean) when it exists, else the mic.
        if pair is None or role == "pc" or pair.get("independent"):
            self._maybe_auto_transcribe(link)
        else:
            pair["mic_link"] = link
            if pair.get("pc_failed") or not pair.get("pc"):
                self._maybe_auto_transcribe(link)

    def _maybe_auto_transcribe(self, link: dict) -> None:
        if (link.get("state") == "linked" and link.get("master") and not _too_short(link["master"])
                and self.auto_transcription_allowed(link.get("course", ""))):
            result = self.transcription_start(False, link["master"], link["course"])
            if result.get("ok"):
                with self._lock:
                    (self._state.get("transcription") or {})["auto"] = True
        self._maybe_auto_whisper(link)

    def whisper_allowed(self, course: str) -> bool:
        """D-061: Whisper runs on Groq, so it needs its own per-course consent and the user's Groq key."""
        from lecturehub.transcription.consent import ConsentStore

        if not _named(course) or not ConsentStore(self.root / "_lecturehub" / "consent.json").granted(
                "whisper_groq", course):
            return False
        try:
            from lecturehub.transcription.groq import load_api_key

            load_api_key()
        except Exception:  # noqa: BLE001 - no key: Whisper is simply off
            return False
        return True

    def _maybe_auto_whisper(self, link: dict) -> None:
        """The reference engine after class (§8.3, R1): quiet, low priority, independent of the Google job.
        The CLI itself refuses a recording that already has a Whisper job and waits out a running recording."""
        if not (link.get("state") == "linked" and link.get("master") and not _too_short(link["master"])
                and self.whisper_allowed(link.get("course", ""))):
            return
        process = spawn([self.python, "-m", "lecturehub", "transcribe", "run", link["master"], "--provider", "whisper",
                         "--course", link["course"], "--root", str(self.root)], stdout=subprocess.DEVNULL,
                        below_normal=True)
        threading.Thread(target=self._wait_then_register, args=(process,), daemon=True).start()

    def after_import(self, link: dict) -> dict:
        """A recording brought in from outside was linked (index/import_audio.py). It is sent by itself only when
        it is the lecture's first source and the course already has the user's consent — a second recording of a
        class that is already covered would spend the day's requests twice; [실행] on the lecture screen stays."""
        self._bump_library()
        course, master = link.get("course", ""), link.get("master")
        if not master or (link.get("duration_ms") or 0) < AUTO_MIN_MS:
            return {"started": [], "why": "1분보다 짧은 녹음은 자동으로 전사하지 않습니다"}
        if link.get("other_recordings") or link.get("transcripts"):
            return {"started": [], "why": "이 회차에는 이미 녹음이나 전사가 있어 자동으로 보내지 않았습니다"}
        started = []
        if self.recording:  # Google is not started next to a running recording; Whisper (remote) may be (D-063)
            why = "녹음 중이라 Google 전사는 시작하지 않았습니다"
        elif not self.auto_transcription_allowed(course):
            why = "이 과목은 아직 전사 동의가 없습니다"
        else:
            result = self.transcription_start(False, master, course)
            why = "" if result.get("ok") else result.get("reason", "")
            if result.get("ok"):
                started.append("google")
        if self.whisper_allowed(course):
            self._maybe_auto_whisper({"state": "linked", "master": master, "course": course})
            started.append("whisper")
        return {"started": started, "why": why}

    def confirm_pending(self, ingest_id: str, course: str) -> dict:
        try:
            link = self._cli("lecture", "confirm-pending", ingest_id, "--course", course)
        except Exception as exc:  # noqa: BLE001 - shown to the user
            return {"ok": False, "reason": str(exc)}
        self._set(link=link)
        self._bump_library()
        self._maybe_auto_transcribe(link)
        return {"ok": True, **link}

    def _register_finished_jobs(self) -> None:
        try:
            extra = ("--allow-mock",) if os.environ.get("LECTUREHUB_FAKE_GEMINI") else ()  # synthetic verification only
            results = self._cli("lecture", "sync-jobs", *extra) or []
        except Exception as exc:  # noqa: BLE001 - retried at next start
            with self._lock:
                (self._state.get("transcription") or {})["register_error"] = str(exc)
            return
        if any(r.get("state") == "registered" and not r.get("already") for r in results):
            self._bump_library()

    def resume_jobs(self) -> list[str]:
        """At app start: register finished jobs, resume jobs whose quota wait is over or that stopped midway."""
        if self.needs_setup:  # first run: no semester folder yet, so there is nothing to resume or tidy
            return []
        self._register_finished_jobs()
        recording = self.recording_anywhere(max_age=0)  # the engine may have kept recording while the app was closed
        if not recording:  # AGENTS §2.7: no big copies/hashes or deletions next to a running recorder
            try:
                self._cli("lecture", "archive-copy")  # D-055: retry copies to 녹음본 that failed earlier
                self._cli("lecture", "wav-cleanup")  # WAV → Recycle Bin only when the transcript is finished
            except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired):
                applog.swallowed("시작할 때 녹음 보관 복사·WAV 정리")  # retried at the next start; recording unaffected
        resumed = []
        jobs = self.root / "_lecturehub" / "jobs"
        now_ms = int(time.time() * 1000)
        for folder in sorted(jobs.iterdir()) if jobs.is_dir() else []:
            state = _read_json(folder / "state.json") or {}
            others = [s for s in self._jobs_for(state["audio"]) if s.get("job_id") != state.get("job_id")] \
                if state.get("audio") else []
            if any(s.get("state") in ("done", "qa_warning") for s in others):
                continue  # the same recording was already transcribed by another job: do not send it again
            waiting = state.get("state") == "waiting_for_quota" and (state.get("next_eligible_at") or 0) <= now_ms
            stale = (state.get("state") in ("running", "queued", "waiting_master")
                     and time.time() - (folder / "state.json").stat().st_mtime > 600)
            if (waiting or stale) and not state.get("dry_run_1min"):
                if recording and not state.get("live"):
                    continue  # §2.7: Google waits until the class is over (only live jobs run during class, D-059)
                self._resume_job(state["job_id"], below_normal=True)  # always gently: a recording may start any time
                resumed.append(state["job_id"])
        whisper = self.root / "_lecturehub" / "jobs_whisper"  # D-061: Groq hour/day audio limits park a job
        for folder in sorted(whisper.iterdir()) if whisper.is_dir() else []:
            state = _read_json(folder / "state.json") or {}
            waiting = state.get("state") == "waiting_for_quota" and (state.get("next_eligible_at") or 0) <= now_ms
            stale = (state.get("state") in ("running", "queued")
                     and time.time() - (folder / "state.json").stat().st_mtime > 600)
            if (waiting or stale) and not state.get("dry_run_1min"):  # remote: allowed during class (D-063)
                self._resume_job(state["job_id"])
                resumed.append(state["job_id"])
        return resumed

    def _resume_job(self, job_id: str, below_normal: bool = True) -> None:
        process = spawn([self.python, "-m", "lecturehub", "transcribe", "resume", job_id, "--root", str(self.root)],
                        stdout=subprocess.DEVNULL, below_normal=below_normal)
        threading.Thread(target=self._wait_then_register, args=(process,), daemon=True).start()

    def _wait_then_register(self, process: subprocess.Popen) -> None:
        process.wait()
        self._register_finished_jobs()

    def mai_prepare(self, master: str) -> dict:
        """[MAI 보강]: chunks + manifest (mai prepare), then Claude Code runs /mai-transcribe (§8.6, D-064)."""
        from lecturehub.core import edition

        if not edition.has("mai"):
            return {"ok": False, "error": "공개판에 포함되지 않은 기능입니다.", "reason": "공개판에 포함되지 않은 기능입니다.", "state": "disabled", "running": False}
        self._set(mai={"state": "running", "master": master})

        def work():
            course = _course_of(master)
            if not _named(course):  # never consent for the user: a blank course goes through engine_start's dialog
                self._set(mai={"state": "failed", "master": master,
                               "error": "과목 미지정 녹음입니다. 회차 화면의 MAI [실행]에서 동의를 받은 뒤 진행하세요"})
                return
            result = self._mai_start(master, course, [])
            if not result.get("ok"):
                self._set(mai={"state": "failed", "master": master, "error": result.get("reason", "")})
                return
            job_dir = self.root / "_lecturehub" / "jobs_mai" / result["job_id"]
            chunks = (_read_json(job_dir / "manifest.json") or {}).get("chunks", [])
            self._set(mai={"state": "ready", "master": master, "folder": str(job_dir), "job_id": result["job_id"],
                           "chunks": [Path(c["path"]).name for c in chunks], "claude": result.get("claude"),
                           "via": result.get("via"), "hint": result.get("hint")})

        threading.Thread(target=work, daemon=True).start()
        return {"ok": True}

    def save_memo(self, course: str, text: str) -> dict:
        memos = self.root / "_lecturehub" / "memos"
        memos.mkdir(parents=True, exist_ok=True)
        safe = "".join(ch for ch in (course or "미지정") if ch not in '\\/:*?"<>|')
        path = memos / f"{self.clock():%Y-%m-%d} {safe}.md"
        atomic_write_text(path, text, backup=True)
        return {"ok": True, "path": str(path)}

    def open_folder(self, path: str) -> dict:
        target = Path(path)
        if not target.exists() or not target.resolve().is_relative_to((self.root / "_lecturehub").resolve()):
            return {"ok": False}
        from lecturehub.core import safe_open

        reason = safe_open.refusal(target)
        if reason:
            return {"ok": False, "error": reason}
        os.startfile(str(target))
        return {"ok": True}


def _read_bookmarks(session_dir: Path) -> list[dict]:
    try:
        lines = (session_dir / "bookmarks.jsonl").read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    return [{"n": b["n"], "at_ms": b["at_ms"]} for b in map(json.loads, filter(None, lines))]
