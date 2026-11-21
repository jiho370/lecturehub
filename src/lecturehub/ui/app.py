"""Lecture Hub desktop app: pywebview (WebView2) window around web/index.html.

Run without a console: ``pythonw -m lecturehub.ui.app``.
Environment (verification only): LECTUREHUB_UI_CDP_PORT enables the WebView2
DevTools port for screenshots; LECTUREHUB_UI_FAKE_ENGINE uses a synthetic source;
LECTUREHUB_UI_NOW (ISO time) fixes "now" for the sidebar and material pick;
LECTUREHUB_UI_MASK=1 hides transcript text and slide images (verification screenshots only).
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from datetime import UTC
from pathlib import Path

from lecturehub.core import applog

WEB = Path(__file__).resolve().parent / "web"
ICON = Path(__file__).resolve().parent / "assets" / "app.ico"
# Windows groups taskbar buttons by this id; without it the button shows pythonw's icon.
# The "수업 녹음 (새 앱)" shortcuts carry the same id (tools/shortcut_app_id.py) so pinning works.
APP_ID = "LectureHub.App"
APP_ID_PUBLIC = "LectureHub.Public"  # the public app gets its own taskbar button and pin (user 2026-11-14)
MAIN_TITLE = "Lecture Hub"  # also how a second start finds the open window (core/single_instance.py)
SW_MINIMIZE, SW_RESTORE = 6, 9  # ShowWindow commands for the main window
MINI_TITLE = "수업 녹음 · 미니"  # the small window that shows a notice while the main window is minimized
# closing the window while a recording runs: it goes to the taskbar instead (once per recording)
CLOSE_WHILE_RECORDING = ("녹음 중입니다", "창을 닫지 않고 작업 표시줄로 내렸습니다. 녹음을 끝낸 뒤에 닫을 수 있습니다.")
CONSENT_UNCONFIRMED = "동의 확인 창을 거치지 않아 동의를 저장하지 않았습니다. 다시 눌러 주세요."
CONSENT_TOKEN_TTL_S = 300
ASSIGNMENT_PROVIDER = "claude_assignment"  # = index.assignment_pack.PROVIDER (D-050: no index import here)
CONSENT_TITLES = {"gemini_transcribe": "Google로 녹음을 보낼까요?", ASSIGNMENT_PROVIDER: "과제 도우미 동의",
                  "google_live": "Google로 수업 소리를 보낼까요?", "claude_qa": "Anthropic으로 자료 글을 보낼까요?",
                  "whisper_groq": "Groq로 녹음을 보낼까요?", "mai_playground": "Microsoft로 녹음을 보낼까요?",
                  "codex_note": "OpenAI Codex로 자료 글을 보낼까요?", "claude_note": "Anthropic으로 자료 글을 보낼까요?"}




def _set_app_id() -> None:
    if sys.platform == "win32":
        import ctypes

        from lecturehub.core import edition, package

        if package.packaged():  # the Store package gives the window its identity (tile, taskbar, pin)
            return
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID_PUBLIC if edition.name() == "public" else APP_ID)


def _now():
    from datetime import datetime

    fixed = os.environ.get("LECTUREHUB_UI_NOW")
    return (datetime.fromisoformat(fixed) if fixed else datetime.now(UTC)).astimezone()


class _NoLibrary:
    """Stand-in when the library layer cannot even be imported (D-050: recording still works)."""

    def __init__(self, root, error: str):
        self.root, self._error = Path(root), error

    def __getattr__(self, name):
        return lambda *args, **kwargs: {"ok": False, "error": f"라이브러리를 불러오지 못했습니다: {self._error}"}


def open_library(root):
    try:
        from .library import Library

        return Library(root)
    except Exception as exc:  # noqa: BLE001 - any import/setup failure must leave the recorder usable
        applog.swallowed("수업 목록(라이브러리) 열기")
        return _NoLibrary(root, str(exc))


def open_room(backend):
    """그스룸 서비스(P-019). 실패해도 녹음은 그대로 쓴다."""
    try:
        from lecturehub.booking.service import RoomService

        return RoomService(backend.root, lambda: backend.courses, backend.clock)
    except Exception:  # noqa: BLE001 - booking is optional next to the recorder
        return None


class Api:
    """Methods exposed to JavaScript as window.pywebview.api.* (private fields are not exposed)."""

    def __init__(self, backend, library=None, media=None, room=None):
        self._backend = backend
        self._library = library
        self._media = media
        self._room = room
        self._main = None
        self._mini = None  # created the first time a notice needs it (_mini_window), then kept hidden
        self._mini_lock = threading.Lock()  # two notices at once must not make two small windows
        self._quitting = False
        self._close_told = None  # the recording whose "the window stays open" notification was already shown
        self._side_restore = None  # 옆으로 보기: (was maximized, (x, y, w, h)) to go back to
        self._consent_tokens = {}  # one-time tokens from consent_confirm (security review 2026-11-05)

    # data
    def init(self):
        info = self._backend.init_info()
        # Masking was tried as a default and dropped (user, 2026-09-29); only verification screenshots turn it on.
        info["mask"] = os.environ.get("LECTUREHUB_UI_MASK") == "1"
        from lecturehub.core import settings

        info["trial"] = bool(settings.get("trial"))  # synthetic trial semester: the window offers "내 학기 폴더 정하기"
        return info

    def state(self):
        return self._backend.state()

    # 그스룸 saved companions and room order (full build only; backend refuses in public)
    def gsr_companions_get(self):
        return self._backend.gsr_companions_get()

    def gsr_companions_set(self, text):
        if not isinstance(text, (str, list)):
            return {"ok": False, "error": "형식이 맞지 않습니다"}
        return self._backend.gsr_companions_set(text)

    def gsr_rooms_get(self):
        return self._backend.gsr_rooms_get()

    def gsr_rooms_set(self, prefs):
        if not isinstance(prefs, dict):
            return {"ok": False, "error": "형식이 맞지 않습니다"}
        return self._backend.gsr_rooms_set(prefs)

    def live_engine_get(self):
        return self._backend.live_engine_get()

    def live_engine_set(self, engine):
        return self._backend.live_engine_set(str(engine or ""))

    def lab_set(self, name, on):
        return self._backend.lab_set(str(name), bool(on))

    # timetable and term (settings screen, TT-001)
    def timetable_get(self):
        return self._backend.timetable_get()

    def timetable_save(self, courses, term_start="", term_end=""):
        if not isinstance(courses, list):
            return {"ok": False, "error": "시간표 형식이 올바르지 않습니다."}
        result = self._backend.timetable_save(courses, str(term_start or ""), str(term_end or ""))
        if result.get("ok") and self._library is not None and hasattr(self._library, "sync_timetable"):
            synced = self._library.sync_timetable()
            result["library"] = synced if synced.get("ok") else {
                "error": f"시간표 파일은 저장했지만 라이브러리 반영에 실패했습니다: {synced.get('error')}"}
        return result

    def folder_layout_get(self, term="", courses=None):
        names = [str(c) for c in courses] if isinstance(courses, list) else None
        return self._backend.folder_layout_get(str(term or ""), names)

    def folder_layout_set(self, style, archive_copies=None):
        return self._backend.folder_layout_set(str(style or ""), None if archive_copies is None else bool(archive_copies))

    def trial_start(self):
        """First run: open a synthetic trial semester (made in the app's own folder) instead of choosing a folder."""
        from .library import Library

        return Library.make_trial()

    def audio_cleanup_get(self):
        return self._backend.audio_cleanup_get()

    def audio_cleanup_set(self, enabled, days=None):
        return self._backend.audio_cleanup_set(bool(enabled), days)

    # reading class notices out of transcripts (index/class_notices.py): a switch and what it has used
    def notices_setting_get(self):
        return self._library.notices_setting(_now().date())

    def notices_setting_set(self, on):
        return self._library.notices_setting(_now().date(), set_to=bool(on))

    def class_reminder_set(self, on):
        return self._backend.class_reminder_set(bool(on))

    # connections (settings screen)
    def conn_status(self, check_login=False):
        return self._backend.conn_status(bool(check_login))

    def appearance(self, theme=None, text_size=None):
        return self._backend.appearance(theme, text_size)

    def ai_status(self, check_login=False):
        from lecturehub.ai import status

        return {"ok": True, **status(bool(check_login))}

    def ai_select(self, name, feature="default"):
        from lecturehub.ai import set_choice

        try:
            return {"ok": True, **set_choice(str(name), str(feature))}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    def conn_connect(self, provider, key):
        return self._backend.conn_connect(str(provider), str(key))

    def client_error(self, text):
        """A script error in the window (unhandled rejection / uncaught error), kept in a small local log for diagnosis."""
        from lecturehub.core import settings

        try:
            log = settings.settings_path().parent / "ui_errors.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            if log.exists() and log.stat().st_size > 200_000:
                log.replace(log.with_suffix(".old.log"))
            with log.open("a", encoding="utf-8") as fh:
                fh.write(f"{_now().isoformat(timespec='seconds')} {str(text)[:2000]}\n")
        except OSError:
            pass
        applog.LOG.warning("화면 스크립트 오류: %s", str(text)[:2000])
        return {"ok": True}

    # about this app + problem report file (release review 2026-11-16; core/diagnostics.py)
    def about(self):
        from lecturehub import __version__
        from lecturehub.core import diagnostics, edition, settings

        folders = diagnostics.data_folders(settings.data_root())
        app_dir = Path(sys.executable).parent if getattr(sys, "frozen", False) else None
        notices = [name for name in ("LICENSE.txt", "THIRD_PARTY_NOTICES.md") if app_dir and (app_dir / name).is_file()]
        return {"ok": True, "version": __version__, "edition": edition.name(), "installed": app_dir is not None,
                "folders": folders, "notices": notices}

    def about_open(self, which):
        """Open one of the app's own folders in Explorer (a fixed list; never a path from the page)."""
        from lecturehub.core import diagnostics, settings

        folders = diagnostics.data_folders(settings.data_root())
        if which == "notices" and getattr(sys, "frozen", False):
            target = Path(sys.executable).parent
        elif which == "logs":
            target = diagnostics.log_folder()
        elif which in ("settings", "semester") and folders.get(which):
            target = Path(folders[which])
        else:
            return {"ok": False, "error": "열 수 있는 폴더가 아닙니다."}
        if not target.is_dir():
            return {"ok": False, "error": "폴더가 아직 없습니다."}
        from lecturehub.core import package

        subprocess.Popen(["explorer", str(package.real_path(target))])  # Store build: Explorer sees the redirected copy
        return {"ok": True}

    # new-version notice (core/update.py): one daily request for the latest version number, nothing is installed
    def update_status(self, check=False):
        from lecturehub.core import update

        try:
            return update.check(force=False) if check else update.status()
        except Exception:  # noqa: BLE001 - never a problem for the window
            applog.swallowed("새 버전 확인")
            return {"ok": False, "available": False}

    def update_set(self, on):
        from lecturehub.core import update

        return update.set_enabled(bool(on))

    def update_open(self):
        """Open the download page of the newer version in the browser (only the address the check stored, and only
        when it is a release page of this app's own repository)."""
        import webbrowser

        from lecturehub.core import update

        state = update.status()
        url, name = state.get("url") or "", update.repo()
        if not (state.get("available") and name and url.startswith(f"https://github.com/{name}/releases/")):
            return {"ok": False, "error": "열 수 있는 내려받기 주소가 없습니다."}
        webbrowser.open(url)
        return {"ok": True}

    def open_mic_settings(self):
        """Open Windows Settings at the microphone privacy page (the sound check found the switch off)."""
        return self._backend.open_mic_settings()

    def consent_list(self):
        return self._backend.consent_list()

    def consent_revoke(self, provider, course):
        return self._backend.consent_revoke(str(provider), str(course))

    def report_plan(self):
        from lecturehub.core import diagnostics, settings

        return {"ok": True, **diagnostics.plan(settings.data_root())}

    def report_make(self):
        """Write the problem report zip and show it in Explorer. Nothing is sent anywhere."""
        from lecturehub.core import diagnostics, settings

        try:
            target = diagnostics.make(settings.data_root())
        except OSError as exc:
            applog.swallowed("문제 보고 파일 만들기")
            return {"ok": False, "error": f"문제 보고 파일을 만들지 못했습니다: {exc}"}
        from lecturehub.core import package

        subprocess.Popen(["explorer", "/select,", str(package.real_path(target))])
        return {"ok": True, "path": str(target), "name": target.name}

    def conn_save_unchecked(self, provider, key):
        return self._backend.conn_save_unchecked(str(provider), str(key))

    def conn_test(self, provider):
        return self._backend.conn_test(str(provider))

    def conn_remove(self, provider):
        return self._backend.conn_remove(str(provider))

    def conn_etl_site(self, address=None):
        return self._backend.conn_etl_site(None if address is None else str(address))

    def conn_open_help(self, provider):
        """Open the page that explains how to get a key / install a tool (only our own list of addresses)."""
        from lecturehub.core import connections, tools

        url = (connections.meta_for(provider) if provider in connections.PROVIDERS else tools.TOOLS.get(provider) or {}).get("help_url")
        if not url:
            return {"ok": False, "error": "안내 페이지가 없습니다."}
        import webbrowser

        webbrowser.open(url)
        return {"ok": True}

    # first run
    def setup_info(self):
        return self._backend.setup_info()

    def setup_pick_folder(self):
        import webview

        picked = self._main.create_file_dialog(webview.FileDialog.FOLDER)
        path = str(picked[0]) if picked else ""
        from lecturehub.ui import setup

        return {"ok": True, "path": path, "notes": setup.folder_notes(path)}

    def setup_apply(self, parent, term, courses, term_start="", term_end="", layout=""):
        return self._backend.setup_apply(parent, term, courses, str(term_start or ""), str(term_end or ""),
                                         str(layout or ""))

    def setup_close(self):
        self._quitting = True
        for win in (self._mini, self._main):
            try:
                if win:
                    win.destroy()
            except Exception:  # noqa: BLE001, S110 - closing anyway
                pass
        return {"ok": True}

    # recording
    def probe(self, mode="mic", device_name=None):
        return self._backend.probe(mode, device_name)

    def suggest_course(self):
        return {"course": self._backend.suggest_course()}

    def start(self, course, mode="mic", device_name=None, course_source="unknown", with_pc=False):
        return self._backend.start(course, mode, device_name, course_source, bool(with_pc))

    def recording_permissions(self):
        return self._backend.recording_permissions()

    def recording_permission(self, course, state=None):
        return self._backend.recording_permission(str(course or ""), state)

    def set_selection(self, course, mode="mic", device_name=None, course_source="unknown", with_pc=False):
        return self._backend.set_selection(course, mode, device_name, course_source, bool(with_pc))

    def start_selected(self):  # the small window's record button
        selected = self._backend.selection
        course = selected.get("course") if selected.get("course_source") == "user_pick" else self._backend.suggest_course()
        permission = self._backend.recording_permission(course or "")
        if not permission.get("allowed"):
            return {"ok": False, "reason": permission.get("reason")}
        if permission.get("warning") and (
                not self._main or not self._main.create_confirmation_dialog("녹음 허락 확인", permission["warning"])):
            return {"ok": False, "reason": "녹음 허락을 확인한 뒤 시작하세요."}
        return self._backend.start_selected()

    def discard_recording(self):
        return self._backend.discard()

    def pause(self, paused=True):
        return self._backend.pause(bool(paused))

    def class_over_answer(self, action):
        return self._backend.class_over_answer(str(action))

    def next_class_answer(self, action):
        return self._backend.next_class_answer(str(action))

    def short_answer(self, action):
        return self._backend.short_answer(str(action))

    def bookmark(self):
        return self._backend.bookmark()

    def stop(self):
        return self._backend.stop()

    def start_companion(self):
        return self._backend.start_companion()

    def stop_companion(self):
        return self._backend.stop_companion()

    # after recording
    def transcription_check(self, master=None, course=None):
        return self._backend.transcription_check(master, course)

    def transcription_start(self, consent=False, master=None, course=None, token=None):
        if consent:  # a stored (per-course) consent needs the native confirmation window's token
            info = self._backend.transcription_check(master, course)
            if info.get("needs_consent") and not info.get("once_only") \
                    and not self._take_consent_token(token, "gemini_transcribe", info.get("course")):
                return {"ok": False, "reason": CONSENT_UNCONFIRMED, "needs_confirm": True}
        return self._backend.transcription_start(bool(consent), master, course)

    # Persistent consent (security review 2026-11-05): anything that can run JS in the window (e.g. a script in a
    # note) could otherwise pass consent=true. Storing a course's consent therefore needs a one-time token that
    # only this native OS dialog hands out — page scripts cannot press its buttons.
    def consent_confirm(self, provider, course):
        provider, course = str(provider or ""), str(course or "")
        notice = self._consent_notice(provider)
        if notice is None or not course:
            return {"ok": False, "error": "알 수 없는 동의 요청입니다."}
        window = self._main
        if window is None or not hasattr(window, "create_confirmation_dialog"):
            return {"ok": False, "error": "동의 확인 창을 띄우지 못했습니다."}
        title = CONSENT_TITLES.get(provider, "동의")
        from lecturehub.transcription.consent import named

        scope = "이 제공자·이 과목에만 적용합니다." if named(course) else "이번 작업에만 적용하며 저장하지 않습니다."
        message = f"{notice}\n\n과목: {course}\n{scope}"
        if not window.create_confirmation_dialog(f"{course} · {title}", message):
            return {"ok": True, "yes": False}
        import secrets
        import time

        token = secrets.token_urlsafe(18)
        now = time.monotonic()
        self._consent_tokens = {t: v for t, v in self._consent_tokens.items() if now - v[2] < CONSENT_TOKEN_TTL_S}
        self._consent_tokens[token] = (provider, course, now)
        return {"ok": True, "yes": True, "token": token}

    def _consent_notice(self, provider: str) -> str | None:
        from lecturehub.core import edition
        from lecturehub.transcription.consent import NOTICES

        feature = {ASSIGNMENT_PROVIDER: "assignment", "claude_qa": "qa", "mai_playground": "mai"}.get(provider)
        if feature and not edition.has(feature):
            return None
        if provider == ASSIGNMENT_PROVIDER and self._library is not None:
            return self._library.assignment_notice()
        return NOTICES.get(provider)

    def _take_consent_token(self, token, provider, course) -> bool:
        import time

        entry = self._consent_tokens.pop(str(token), None) if token else None
        return bool(entry and entry[0] == provider and entry[1] == (course or "")
                    and time.monotonic() - entry[2] < CONSENT_TOKEN_TTL_S)

    def relink(self, media_id, course, day, with_transcripts=False):
        if self._busy():
            return {"ok": False, "error": "녹음 중에는 옮기지 않습니다. 녹음이 끝난 뒤 다시 누르세요."}
        return self._library.relink(media_id, course, day, with_transcripts)

    def add_extra(self, name):
        return self._backend.add_extra(name)

    def quota(self):
        return self._backend.quota_status()

    # engines (track C): chip data for all three engines, per-recording jobs, [실행] for one engine
    def engines(self):
        return self._backend.engines_overview()

    def recording_engines(self, master, course=""):
        return self._backend.recording_engines(str(master), str(course or ""))

    def engine_start(self, engine, master, course="", consent=False, language="", token=None):
        if consent and str(engine) in ("google", "gemini", "gemini_transcribe"):  # Google may store consent
            from lecturehub.ui.backend import _course_of

            info = self._backend.transcription_check(str(master), str(course or "") or _course_of(str(master)))
            if info.get("needs_consent") and not info.get("once_only") \
                    and not self._take_consent_token(token, "gemini_transcribe", info.get("course")):
                return {"ok": False, "reason": CONSENT_UNCONFIRMED, "needs_confirm": True}
        return self._backend.engine_start(str(engine), str(master), str(course or ""), bool(consent),
                                          str(language or ""))

    # live captions (2026-11-06): a course's consent is stored only with the native dialog's token
    def captions_status(self):
        return self._backend.captions_status()

    def captions_check(self, course=""):
        return self._backend.captions_check(str(course or ""))

    def captions_start(self, course="", source="mic", consent=False, token=None, chirp=False, quick=True):
        course = str(course or "")
        store = bool(token) and self._take_consent_token(token, "google_live", course)
        if token and not store:
            return {"ok": False, "reason": CONSENT_UNCONFIRMED, "needs_confirm": True}
        return self._backend.captions_start(course, str(source or "mic"), bool(consent), store, bool(chirp),
                                            bool(quick))

    # presentation Q&A helper (2026-11-07): consents stored only with the native dialog's tokens
    def qa_status(self):
        from lecturehub.core import edition

        if not edition.has("qa"):
            return {"ok": False, "error": "공개판에 포함되지 않은 기능입니다.", "reason": "공개판에 포함되지 않은 기능입니다.", "state": "disabled", "running": False}
        return self._backend.qa_status()

    def qa_check(self, course=""):
        from lecturehub.core import edition

        if not edition.has("qa"):
            return {"ok": False, "error": "공개판에 포함되지 않은 기능입니다.", "reason": "공개판에 포함되지 않은 기능입니다.", "state": "disabled", "running": False}
        return self._backend.qa_check(str(course or ""))

    def qa_start(self, course="", folder="", token_qa=None, token_live=None, model="sonnet", pack=""):
        from lecturehub.core import edition

        if not edition.has("qa"):
            return {"ok": False, "error": "공개판에 포함되지 않은 기능입니다.", "reason": "공개판에 포함되지 않은 기능입니다.", "state": "disabled", "running": False}
        course, pack = str(course or ""), str(pack or "")
        if pack:  # a presentation pack (P-046): its course is the consent unit, whatever the page sent
            from lecturehub.transcription import qa_packs

            try:
                course = qa_packs.load(self._backend.root, pack)["course"]
            except ValueError as exc:
                return {"ok": False, "reason": str(exc)}
        store_qa = bool(token_qa) and self._take_consent_token(token_qa, "claude_qa", course)
        store_live = bool(token_live) and self._take_consent_token(token_live, "google_live", course)
        if (token_qa and not store_qa) or (token_live and not store_live):
            return {"ok": False, "reason": CONSENT_UNCONFIRMED, "needs_confirm": True}
        return self._backend.qa_start(course, str(folder or ""), store_qa, store_live, str(model or "sonnet"), pack)

    # presentation packs (P-046); made and edited in Claude Code (P-049), the app only lists and starts them
    def qa_packs(self):
        from lecturehub.core import edition

        if not edition.has("qa"):
            return {"ok": False, "error": "공개판에 포함되지 않은 기능입니다.", "reason": "공개판에 포함되지 않은 기능입니다.", "state": "disabled", "running": False}
        return self._backend.qa_packs()

    def qa_ask(self, text="", start=None):
        from lecturehub.core import edition

        if not edition.has("qa"):
            return {"ok": False, "error": "공개판에 포함되지 않은 기능입니다.", "reason": "공개판에 포함되지 않은 기능입니다.", "state": "disabled", "running": False}
        try:
            start = None if start is None else float(start)
        except (TypeError, ValueError):
            start = None
        return self._backend.qa_ask(str(text or ""), start)

    def qa_stop(self):
        from lecturehub.core import edition

        if not edition.has("qa"):
            return {"ok": False, "error": "공개판에 포함되지 않은 기능입니다.", "reason": "공개판에 포함되지 않은 기능입니다.", "state": "disabled", "running": False}
        return self._backend.qa_stop()

    def captions_pause(self, on=True):
        return self._backend.captions_pause(bool(on))

    def captions_stop(self):
        return self._backend.captions_stop()

    # library (DB) — failures come back as {"ok": False, "error": ...}; recording is unaffected
    def sidebar(self):
        return self._library.sidebar(_now().date())

    def lecture(self, lecture_id):
        result = self._library.lecture(lecture_id)
        lecture = result.get("lecture", {})
        self._attach_media(lecture.get("media"))
        for note in lecture.get("notes", []):  # organized HTML notes open large in the lecture view
            if note.get("exists") and str(note.get("path", "")).lower().endswith((".html", ".htm")) \
                    and self._media is not None:
                note["url"] = self._media.url_for(note["path"])
        return result

    def note_collection(self):
        """The rail's 정리본 button: per course, lectures by date with their HTML 정리본 (2026-11-13)."""
        return self._library.note_collection()

    def transcript(self, transcript_id):
        result = self._library.transcript(transcript_id)
        self._attach_media(result.get("media"))
        return result

    def transcript_save(self, transcript_id, folder=None, reveal=True):
        """[내려받기]: the transcript as a .txt in the Downloads folder; an existing file is never overwritten."""
        made = self._library.transcript_export(transcript_id)
        if not made.get("ok", True) or "text" not in made:
            return made
        target_dir = Path(folder) if folder else Path.home() / "Downloads"
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            stem, target, n = Path(made["name"]).stem, target_dir / made["name"], 1
            while target.exists():
                n += 1
                target = target_dir / f"{stem} ({n}).txt"
            target.write_text(made["text"], encoding="utf-8-sig", newline="\r\n")
        except OSError as exc:
            return {"ok": False, "error": f"저장하지 못했습니다: {exc}"}
        if reveal:
            subprocess.Popen(["explorer", "/select,", str(target)])
        return {"ok": True, "path": str(target), "name": target.name, "segments": made["segments"]}

    def confirm_pending(self, ingest_id, course):
        return self._backend.confirm_pending(ingest_id, course)

    def mai_prepare(self, master):
        from lecturehub.core import edition

        if not edition.has("mai"):
            return {"ok": False, "error": "공개판에 포함되지 않은 기능입니다.", "reason": "공개판에 포함되지 않은 기능입니다.", "state": "disabled", "running": False}
        return self._backend.mai_prepare(master)

    def choose_transcript(self, lecture_id, transcript_id):
        return self._library.choose(lecture_id, transcript_id)

    def search(self, query):
        return self._library.search(query)

    # join recordings of one class (user request 2026-09-29)
    def join_candidates(self, media_id):
        return self._library.join_candidates(media_id)

    def join(self, media_ids):
        if self._busy():  # ffmpeg re-encodes: not while a class is being recorded (AGENTS §2.7)
            return {"ok": False, "error": "녹음 중에는 이어 붙이지 않습니다. 녹음이 끝난 뒤 다시 누르세요."}
        return self._library.join(media_ids)

    # recordings made outside the app (phone recorder, KakaoTalk file): drop on the window or pick (2026-10-01)
    def import_pick(self):
        import webview

        kinds = ";".join(f"*{s}" for s in self._import_suffixes(False)) or "*.m4a;*.mp3;*.wav"
        picked = self._main.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=True,
                                               file_types=(f"녹음 파일 ({kinds})", "모든 파일 (*.*)"))
        return {"ok": True, "paths": [str(p) for p in picked or []]}

    def _import_suffixes(self, convert_only):
        found = self._library.import_suffixes(convert_only)  # the library layer owns the list (D-050: no index import here)
        return found if isinstance(found, list) else []

    def import_recent(self):
        return self._library.import_recent(_now())

    def clova_sync(self):
        return self._library.clova_sync()

    def import_probe(self, paths):
        return self._library.import_probe([str(p) for p in paths or []][:20], _now().date())

    def import_commit(self, item):
        item = dict(item or {})
        needs_ffmpeg = Path(str(item.get("path") or "")).suffix.lower() in self._import_suffixes(True)
        if needs_ffmpeg and self._busy():  # AGENTS §2.7
            return {"ok": False, "error": "녹음 중에는 형식을 바꿔야 하는 파일을 가져오지 않습니다. 녹음이 끝난 뒤 다시 하세요."}
        result = self._library.import_commit(item, _now().date())
        if result.get("ok"):
            result["transcription"] = self._backend.after_import(result)
        return result

    def _on_drop(self, event):
        """Files dropped on the window (Explorer, a KakaoTalk chat): hand their full paths to the page."""
        import json

        files = ((event or {}).get("dataTransfer") or {}).get("files") or []
        paths = [f["pywebviewFullPath"] for f in files if f.get("pywebviewFullPath")]
        names = [f.get("name") or "" for f in files]
        try:
            self._main.evaluate_js(f"window.__lecturehubDropped && window.__lecturehubDropped("
                                   f"{json.dumps(paths)}, {json.dumps(names)})")
        except Exception:  # noqa: BLE001, S110 - a failed drop only means the user picks the file instead
            pass

    def _bind_drop(self):
        from webview.dom import DOMEventHandler

        try:
            self._main.dom.document.events.drop += DOMEventHandler(self._on_drop, True, False)
        except Exception:  # noqa: BLE001, S110 - dropping is a convenience; [파일 고르기] still works
            pass

    # D-058: throw away a recording (Recycle Bin)
    def discard_plan(self, media_id):
        return self._library.discard_plan(media_id)

    def discard(self, media_id, with_pair=False):
        if self._busy():
            return {"ok": False, "error": "녹음 중에는 버리지 않습니다. 녹음이 끝난 뒤 다시 누르세요."}
        return self._library.discard(media_id, with_pair)

    def range_plan(self, media_id):
        return self._library.range_plan(media_id)

    def lecture_discard_plan(self, lecture_id):
        return self._library.lecture_discard_plan(lecture_id)

    def lecture_discard(self, lecture_id, with_pair=False):
        if self._busy():
            return {"ok": False, "error": "녹음 중에는 버리지 않습니다. 녹음이 끝난 뒤 다시 누르세요."}
        return self._library.lecture_discard(lecture_id, with_pair)

    def discard_restore(self, media_id):
        return self._library.discard_restore(media_id)

    def sessions(self):
        return self._library.sessions()

    def discard_session(self, name, confirm_crashed=False):
        if self._busy():
            return {"ok": False, "error": "녹음 중에는 정리하지 않습니다."}
        return self._library.discard_session(name, bool(confirm_crashed))

    def recover_session(self, name):
        if self._busy():  # the merge is ffmpeg work (§2.7)
            return {"ok": False, "error": "녹음 중에는 복구·병합하지 않습니다. 녹음이 끝난 뒤 다시 누르세요."}
        return self._library.recover_session(name)

    # D-057: recording range fix
    def range_cut(self, media_id, start_ms, end_ms):
        if self._busy():
            return {"ok": False, "error": "녹음 중에는 자르지 않습니다. 녹음이 끝난 뒤 다시 누르세요."}
        return self._library.range_cut(media_id, start_ms, end_ms)

    def range_keep(self, media_id):
        return self._library.range_keep(media_id)

    def range_revert(self, media_id):
        return self._library.range_revert(media_id)

    # S5: hold inbox
    def holds(self):
        result = self._library.holds()
        for card in result.get("cards", []) if result.get("ok") else []:
            preview = card.get("preview") or {}
            if preview.get("type") == "audio" and self._media is not None and Path(preview["path"]).is_file():
                preview["url"] = self._media.url_for(preview["path"])
        return result

    def hold_answer(self, ids, value):
        if self._busy():
            return {"ok": False, "error": "녹음 중에는 등록하지 않습니다. 녹음이 끝난 뒤 답해 주세요."}
        return self._library.hold_answer(ids, value)

    def hold_defer(self, ids):
        return self._library.hold_defer(ids)

    def hold_reject(self, ids):
        return self._library.hold_reject(ids)

    def hold_undo(self, item_id):
        return self._library.hold_undo(item_id)

    # S4: evidence-linked notes and corrections
    def correct(self, transcript_id, seq, char_from, char_to, after, shown_text):
        return self._library.correct(transcript_id, seq, char_from, char_to, after, shown_text)

    def edit_note_block(self, lecture_id, key, text):
        return self._library.edit_note_block(lecture_id, key, text)

    def note_check(self, lecture_id):
        return self._library.note_check(lecture_id)

    def _authorize_note(self, lecture_id, token=None):
        info = self._library.note_check(lecture_id)
        if not info.get("ok"):
            return info
        if info["needs_consent"]:
            if not self._take_consent_token(token, info["provider"], info["course"]):
                return {"ok": False, "error": "외부 전송 동의를 먼저 확인하세요."}
            return self._library.note_grant(lecture_id, info["provider"])
        return info

    def make_note(self, lecture_id, block=None, token=None):
        """Start the note job off the UI thread; poll note_status(). Never while recording (AGENTS rule 7)."""
        import threading

        if self._busy():
            return {"ok": False, "error": "녹음 중에는 노트를 만들지 않습니다. 녹음이 끝난 뒤 다시 누르세요."}
        job = getattr(self, "_note_job", None)
        if job and job.get("state") == "running":
            return {"ok": False, "error": "노트를 만드는 중입니다."}
        auth = self._authorize_note(lecture_id, token)
        if not auth.get("ok"):
            return auth
        self._note_job = {"state": "running", "lecture_id": lecture_id, "block": block}

        def work():
            busy = lambda: "recording" if self._busy() else None
            result = self._library.make_note(lecture_id, block, busy, consent_provider=auth["provider"])
            self._note_job = {**result, "lecture_id": lecture_id, "block": block,
                              "state": result.get("state") if result.get("ok") else "failed"}

        threading.Thread(target=work, daemon=True).start()
        return {"ok": True}

    def note_status(self):
        return getattr(self, "_note_job", None) or {"state": "idle"}

    def note_html(self, path):
        """An organized HTML note's text for the in-app viewer (read-only; only files inside the semester folder)."""
        from lecturehub.core.textio import read_text_any

        target = Path(path)
        root = self._library.root.resolve()
        if not target.is_file() or not target.resolve().is_relative_to(root) \
                or target.suffix.lower() not in (".html", ".htm"):
            return {"ok": False, "error": "학기 폴더 안의 HTML 노트만 열 수 있습니다."}
        try:
            return {"ok": True, "html": read_text_any(target)}
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    def open_path(self, path):
        """Open a material/note/recording with its default program, or a folder in Explorer."""
        target = Path(path)
        root = self._library.root.resolve()
        if not target.exists() or not target.resolve().is_relative_to(root):
            return {"ok": False, "error": "학기 폴더 안의 파일만 열 수 있습니다."}
        from lecturehub.core import safe_open

        reason = safe_open.refusal(target)  # allow-list: never run programs, scripts or macro documents
        if reason:
            return {"ok": False, "error": reason}
        os.startfile(str(target))
        return {"ok": True}

    # S3 materials: pick with a reason, user override, open in the user's own PDF program (D-056)
    def material(self, course_id=None, day=None, lecture_id=None):
        return self._library.material(_now(), course_id, day, lecture_id)

    def _busy(self) -> bool:
        """Recording in progress: no PowerPoint conversions next to the recorder (AGENTS rule 7)."""
        state = self._backend.state()  # a second source recording on its own counts too
        if state.get("phase") in ("starting", "recording", "stopping", "finalizing") or \
                (state.get("pc") or {}).get("state") in ("starting", "recording", "stopping", "finalizing"):
            return True
        outside = getattr(self._backend, "recording_anywhere", None)  # a recording started outside the app (cached)
        return bool(outside and outside())

    def material_preview(self, path, page):
        result = self._library.preview(path, page, allow_convert=not self._busy())
        if result.get("ok") and result.get("image") and self._media is not None:
            result["url"] = self._media.url_for(result["image"])
        return result

    def choose_material(self, course_id, day, material_id, page):
        return self._library.choose_material(course_id, day, material_id, page)

    def open_material(self, path, page):
        return self._library.open_material(path, page, allow_convert=not self._busy())

    # course folders by category (index/organize.py): list, file one item, undo
    def course_files(self, course):
        return self._library.course_files(course)

    def file_sort(self, path, category):
        return self._library.file_sort(path, category)

    def files_undo(self, batch=None):
        return self._library.files_undo(batch)

    def _prewarm(self):
        """At start-up (not recording): convert today's remaining slides so class time needs no PowerPoint."""
        try:
            if not self._busy() and hasattr(self._library, "files_relocate"):
                self._library.files_relocate()  # files the user moved in Explorer: follow them
            if not self._busy() and hasattr(self._library, "calendar_refresh"):
                refreshed = self._library.calendar_refresh()  # eTL deadlines + notices heard in class (candidates)
                self._announce((refreshed or {}).get("notices"))
                self._tell_board((refreshed or {}).get("board_new"))
            if not self._busy() and hasattr(self._library, "audio_cleanup"):
                self._library.audio_cleanup()  # recordings whose Google transcript is N days old → Recycle Bin
            if not self._busy() and hasattr(self._library, "prewarm"):
                self._library.prewarm(_now(), self._busy)
        except Exception:  # noqa: BLE001 - a failed warm-up only means the preview is made later
            applog.swallowed("슬라이드 미리 만들기")

    # calendar
    def calendar(self, year, month):
        return self._library.calendar(int(year), int(month), _now().date())

    def calendar_add(self, values):
        return self._library.calendar_add(values or {})

    def calendar_update(self, event_id, changes):
        return self._library.calendar_update(event_id, changes or {})

    def calendar_status(self, event_id, status):
        return self._library.calendar_status(event_id, status)

    # schedule notices heard in class, read by AI from the whole transcript (user request 2026-10-30)
    def notices(self):
        return self._library.notices(_now().date())

    def notices_seen(self, keys):
        return self._library.notices_seen(list(keys or []))

    def _announce(self, result):
        """New notices: Windows notification + taskbar flash + the page's banner."""
        added = [item for item in (result or {}).get("added") or [] if not item.get("seen")]
        if not added:
            return
        def line(item):
            when = f"{item['date'][5:].replace('-', '/')}" if item.get("date") else "날짜 미정"
            return f"{item['course']} {when} {item['kind_label']}: {item['summary']}"
        try:
            from .notify import toast

            toast(f"수업 중 공지 {len(added)}건 — 확인해 주세요", "\n".join(line(i) for i in added[:3]),
                  icon=str(ICON) if ICON.exists() else None)
        except Exception:  # noqa: BLE001 - the banner below still shows it
            applog.swallowed("수업 공지 Windows 알림")
        try:
            if self._main is not None:
                self._main.evaluate_js("window.__lecturehubNotices && window.__lecturehubNotices()")
        except Exception:  # noqa: BLE001 - the page polls the inbox as well
            applog.swallowed("수업 공지 화면 갱신")

    def _tell_board(self, posts):
        """New eTL board posts, with or without files (user 2026-11-20): what was posted, by a Windows
        notification, and the page's banner (it shows the text until 확인함)."""
        if not posts:
            return
        def line(post):
            text = " ".join(str(post.get("text") or "").split())
            return f"{post['course']} · {post['title']}" + (f" — {text[:60]}" if text else "")
        try:
            from .notify import toast

            toast(f"eTL 게시판 새 글 {len(posts)}건", "\n".join(line(p) for p in posts[:3]),
                  icon=str(ICON) if ICON.exists() else None)
        except Exception:  # noqa: BLE001 - the banner below still shows it
            applog.swallowed("게시판 새 글 Windows 알림")
        try:
            if self._main is not None:
                self._main.evaluate_js("window.__lecturehubNotices && window.__lecturehubNotices()")
        except Exception:  # noqa: BLE001 - the page reads the list when it opens
            applog.swallowed("게시판 새 글 화면 갱신")

    def etl_board_new(self):
        return self._library.etl_board_new()

    def etl_board_seen(self, ids):
        return self._library.etl_board_seen(list(ids or []))

    def _notice_loop(self, every_seconds=600):
        """Every 10 minutes: read lectures whose transcripts changed (network only, so it runs while recording),
        and once a day the eTL course boards (not while recording, AGENTS §2.7)."""
        import time

        time.sleep(30)  # after start-up (the calendar refresh reads once too)
        while not self._quitting:
            try:
                self._announce(self._library.notices_scan(_now().date()))
            except Exception:  # noqa: BLE001 - tried again next round
                applog.swallowed("수업 공지 읽기")
            try:
                if not self._busy() and hasattr(self._library, "etl_board_auto"):
                    self._tell_board(self._library.etl_board_auto())
            except Exception:  # noqa: BLE001 - tried again next round
                applog.swallowed("게시판 읽기")
            time.sleep(every_seconds)

    def _clova_loop(self, every_seconds=3600):
        """Weekly Clova check while the app is open (P-039, 사용자 결정 2026-11-06): looks every hour whether a
        check is due (7 days since the last good one, 1 day after a failed one). Network and a rename only, so it
        runs while recording. Says so with a Windows notification when notes came in or Naver login is needed."""
        import time

        if os.environ.get("LECTUREHUB_CLOVA_AUTO", "1") == "0" or os.environ.get("LECTUREHUB_UI_FAKE_ENGINE"):
            return  # off on request, and never against scratch roots used for UI checks
        time.sleep(120)  # after start-up
        while not self._quitting:
            try:
                self._clova_tell((self._library.clova_sync(auto=True) or {}).get("result"))
            except Exception:  # noqa: BLE001 - tried again next round
                applog.swallowed("클로바노트 주간 확인")
            time.sleep(every_seconds)

    def _clova_tell(self, result):
        if not result or not (result.get("staged") or result.get("login_required")):
            return
        title = (f"클로바노트 새 노트 {result['staged']}개를 받았습니다" if result.get("staged")
                 else "클로바노트: Aside에서 네이버 로그인 필요")
        try:
            from .notify import toast

            toast(title, result.get("message") or "", icon=str(ICON) if ICON.exists() else None)
        except Exception:  # noqa: BLE001 - the import dialog's Clova line shows it too
            applog.swallowed("클로바노트 Windows 알림")

    # exam prep: board per course (index/exam_prep.py) + per-course exam page links (exam_pages.py, P-045).
    # The in-app practice screen was removed (P-045); quiz_items/quiz_answer stay for the CLI and old records.
    def exam_board(self):
        board = self._library.exam_board(_now().date())
        if board.get("ok", True) and isinstance(board.get("courses"), list):
            links = self._exam_pages().pages()["courses"]
            for course in board["courses"]:
                course["pages"] = links.get(course.get("folder"), [])
        return board

    def _exam_pages(self):
        from .exam_pages import ExamPages

        return ExamPages(self._backend.root)

    def exam_page_add(self, course, target, title=""):
        return self._exam_pages().add(course, target, title)

    def exam_page_remove(self, course, link_id):
        return self._exam_pages().remove(course, link_id)

    def exam_page_open(self, course, link_id):
        return self._exam_pages().open(course, link_id)

    def exam_page_pick(self, course):
        """Pick an HTML file, starting in the course folder (the add step still checks it is inside the root)."""
        import webview

        root = Path(self._backend.root)
        start = root / str(course or "")
        picked = self._main.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=False,
                                               directory=str(start if start.is_dir() else root),
                                               file_types=("HTML 파일 (*.html;*.htm)",))
        return {"ok": True, "path": str(picked[0]) if picked else ""}

    def quiz_items(self, course_id, mode="mix", topic="", limit=20):
        return self._library.quiz_items(course_id, mode, topic, limit)

    def quiz_answer(self, item_id, given, correct=None):
        return self._library.quiz_answer(item_id, given, correct)

    def etl_status(self):
        return self._library.etl_status()

    def etl_sync(self, force=False, token=None):
        return self._library.etl_sync(force, token)

    def etl_board(self):
        return self._library.etl_board()

    def etl_board_check(self):
        if self._busy():  # network next to a recording: wait (AGENTS §2.7)
            return {"ok": False, "error": "녹음 중에는 게시판을 읽지 않습니다. 녹음이 끝난 뒤 다시 누르세요."}
        return self._library.etl_board_check()

    def etl_file_take(self, file_id, beside=False, allow_big=False):
        if self._busy():  # a download next to a recording: wait (AGENTS §2.7)
            return {"ok": False, "error": "녹음 중에는 받지 않습니다. 녹음이 끝난 뒤 다시 누르세요."}
        return self._library.etl_file_take(file_id, beside, allow_big)

    def etl_file_take_all(self):
        if self._busy():  # downloads next to a recording: wait (AGENTS §2.7)
            return {"ok": False, "error": "녹음 중에는 받지 않습니다. 녹음이 끝난 뒤 다시 누르세요."}
        return self._library.etl_file_take_all()

    def etl_file_keep(self, file_id):
        return self._library.etl_file_keep(file_id)

    def assignment_prepare(self, event_id):
        from lecturehub.core import edition

        if not edition.has("assignment"):
            return {"ok": False, "error": "공개판에 포함되지 않은 기능입니다.", "reason": "공개판에 포함되지 않은 기능입니다.", "state": "disabled", "running": False}
        if self._busy():  # reading PDFs/HWP is kept away from a running recording (AGENTS §2.7)
            return {"ok": False, "error": "녹음 중에는 과제 자료를 모으지 않습니다. 녹음이 끝난 뒤 눌러 주세요."}
        return self._library.assignment_prepare(event_id)

    def assignment_consent(self, course, token=None):
        from lecturehub.core import edition

        if not edition.has("assignment"):
            return {"ok": False, "error": "공개판에 포함되지 않은 기능입니다.", "reason": "공개판에 포함되지 않은 기능입니다.", "state": "disabled", "running": False}
        if not self._take_consent_token(token, ASSIGNMENT_PROVIDER, str(course or "")):
            return {"ok": False, "error": CONSENT_UNCONFIRMED}
        return self._library.assignment_consent(course)

    def assignment_open(self, event_id):
        from lecturehub.core import edition

        if not edition.has("assignment"):
            return {"ok": False, "error": "공개판에 포함되지 않은 기능입니다.", "reason": "공개판에 포함되지 않은 기능입니다.", "state": "disabled", "running": False}
        return self._library.assignment_open(event_id)

    def assignment_claude(self, event_id):
        from lecturehub.core import edition

        if not edition.has("assignment"):
            return {"ok": False, "error": "공개판에 포함되지 않은 기능입니다.", "reason": "공개판에 포함되지 않은 기능입니다.", "state": "disabled", "running": False}
        return self._library.assignment_claude(event_id)

    def note_claude(self, lecture_id, token=None):
        if self._busy():  # the note session renders slides: not next to a recording (AGENTS §2.7)
            return {"ok": False, "error": "녹음 중에는 정리본을 만들지 않습니다. 녹음이 끝난 뒤 다시 누르세요."}
        auth = self._authorize_note(lecture_id, token)
        if not auth.get("ok"):
            return auth
        return self._library.note_claude(lecture_id, consent_provider=auth["provider"])
    # 그스룸 (P-019): Claude in Chrome 외부 어댑터(D-003)로 예약·반납. 네트워크 작업이라 녹음 중에도 허용
    def room(self):
        return self._room.state() if self._room else {"ok": False, "error": "그스룸 기능을 불러오지 못했습니다"}

    def room_refresh(self):
        return self._room.refresh() if self._room else self.room()

    def room_book(self, day, start, use="과제", room=None, hours=3, companions=""):
        """``companions``: the '이름 학번' lines typed for this booking only (never saved, never logged)."""
        from lecturehub.core import settings

        if not self._room:
            return self.room()
        try:
            mates = settings.parse_companions(str(companions or ""))
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        args = (str(day), str(start), str(use), room)
        result = self._room.book(*args, hours=hours, companions=mates)
        if not result.get("needs_confirmation"):
            return result
        b = result["booking"]
        window = self._main
        if window is None or not hasattr(window, "create_confirmation_dialog"):
            return result  # headless / page scripts cannot manufacture native confirmation
        message = f"방: {b['room']}\n날짜: {b['day']}\n시각: {b['start']}~{b['end']}\n용도: {b['use']}\n\n실제로 이용할 예약인가요?"
        if not window.create_confirmation_dialog("그스룸 예약 확인", message):
            return {"ok": False, "cancelled": True, "error": "예약하지 않았습니다"}
        return self._room.book(*args, hours=hours, confirmed=True, companions=mates)

    def room_return(self, booking_id):
        return self._room.return_now(str(booking_id)) if self._room else self.room()

    def room_cancel(self, ids):
        return self._room.cancel(ids) if self._room else self.room()

    def room_chat(self, text):
        from lecturehub.ai import selected

        try:
            engine = selected("gsr_chat")
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        if engine == "off":
            return {"ok": False, "error": "AI 사용이 꺼져 있습니다. 예약 입력칸을 이용하세요."}
        notice = (f"입력한 지시, 최근 대화와 내 예약의 날짜·방·번호를 {engine}에 보내 해석합니다. "
                  "함께 쓰는 사람의 이름·학번은 해석기에 보내지 않습니다(예약 화면의 칸에 따로 적습니다). 지시에는 개인정보를 적지 마세요.\n\n보내시겠습니까?")
        if not self._main or not self._main.create_confirmation_dialog("그스룸 지시 외부 전송", notice):
            return {"ok": False, "error": "지시를 보내지 않았습니다."}
        return self._room.chat(str(text or "")) if self._room else self.room()

    def room_chat_confirm(self, intent, ids, booking=None, companions=""):
        if str(intent) == "book" and isinstance(booking, dict):
            return self.room_book(booking.get("day", ""), booking.get("start", ""), booking.get("use", "과제"),
                                  booking.get("room") or None, booking.get("hours", 3), companions)
        return self._room.confirm(str(intent), ids or []) if self._room else self.room()

    def room_stop(self):
        return self._room.stop_job() if self._room else self.room()

    def room_rules(self, rules, auto_return=True):
        return self._room.save_rules(rules or [], bool(auto_return)) if self._room else self.room()

    def _attach_media(self, media):
        if media and media.get("exists") and self._media is not None:
            media["url"] = self._media.url_for(media["path"])

    def save_memo(self, course, text):
        return self._backend.save_memo(course, text)

    def open_folder(self, path):
        return self._backend.open_folder(path)

    # 단어 암기장 아티팩트 창 (P-037 제안): claude.ai 주소를 바깥 브라우저 앱 창으로 연다. 코드는 memorizer_link.py
    def _memorizer(self):
        from lecturehub.core import edition

        if not edition.has("memorizer"):
            raise ValueError("공개판에 포함되지 않은 기능입니다.")
        from .memorizer_link import MemorizerLink

        return MemorizerLink(self._backend.root)

    def memorizer_status(self):
        from lecturehub.core import edition

        if not edition.has("memorizer"):
            return {"ok": False, "error": "공개판에 포함되지 않은 기능입니다.", "reason": "공개판에 포함되지 않은 기능입니다.", "state": "disabled", "running": False}
        return self._memorizer().status()

    def memorizer_config(self, url=None, browser=None, course=None):
        from lecturehub.core import edition

        if not edition.has("memorizer"):
            return {"ok": False, "error": "공개판에 포함되지 않은 기능입니다.", "reason": "공개판에 포함되지 않은 기능입니다.", "state": "disabled", "running": False}
        return self._memorizer().configure(url=url, browser=browser, course=course)

    def memorizer_open(self):
        from lecturehub.core import edition

        if not edition.has("memorizer"):
            return {"ok": False, "error": "공개판에 포함되지 않은 기능입니다.", "reason": "공개판에 포함되지 않은 기능입니다.", "state": "disabled", "running": False}
        return self._memorizer().open()

    # windows
    def show_main(self):
        """The small window's [크게], and closing it: back to the main window, visible and not minimized."""
        if self._mini is not None:
            self._mini.hide()
        self._main.show()
        try:
            hwnd = self._main_hwnd()
            if hwnd:
                import ctypes

                if ctypes.windll.user32.IsIconic(hwnd):  # it was minimized when the notice came: Show() keeps that
                    self._show_main_window(SW_RESTORE)
        except Exception:  # noqa: BLE001 - it is shown; only still on the taskbar
            applog.swallowed("큰 창 다시 펴기")
        return {"ok": True}

    def set_on_top(self, on):  # the small window's [항상 위]
        if self._mini is not None:
            self._mini.on_top = bool(on)
        return {"ok": True, "on_top": bool(on)}

    def _mini_window(self):
        """The small window, created the first time a notice needs it (not at start-up: its WebView2 costs about
        68 MB) and kept, hidden, until the app closes. ``None`` when it cannot be made now.

        pywebview makes a window at once only when ``create_window`` is called after ``webview.start`` from a thread
        other than the main one (the main thread sits in ``start``): it then asks the window thread to build it
        (WinForms ``Invoke``) and waits. The attention hook runs on its own thread, so that is the case here."""
        if self._mini is not None:
            return self._mini
        if self._quitting or self._main is None or threading.current_thread() is threading.main_thread():
            return None
        if not self._mini_lock.acquire(timeout=30):  # another notice is still making it
            return None
        try:
            if self._mini is None and not self._quitting:
                self._mini = _create_mini(self)
            return self._mini
        finally:
            self._mini_lock.release()

    def _show_mini(self):
        """Show the small window in place of the main one (hidden meanwhile; [크게] or closing brings it back)."""
        mini = self._mini_window()
        if mini is None:
            return False
        self._main.hide()
        mini.show()
        return True

    def _notice_out_of_sight(self):
        """A notice while the main window is minimized or hidden: the small window with the notice card; when that
        cannot be made or shown, the main window comes back instead. Never raises (the engines' threads)."""
        try:
            if self._show_mini():
                return
        except Exception:  # noqa: BLE001 - the main window is restored instead
            applog.swallowed("알림 미니 창")
        try:
            self._show_main_window(SW_RESTORE)
        except Exception:  # noqa: BLE001 - the notice is also on the page and in the Windows notification
            applog.swallowed("알림 때 창 다시 보이기")

    def side_view(self, on):
        """옆으로 보기 (user 2026-11-14): the main window takes the right third of its screen at full height, so a
        PDF or Chrome fits on the left; ``on=False`` puts it back the way it was (maximized or the old rectangle)."""
        if sys.platform != "win32" or self._main is None:
            return {"ok": False, "error": "Windows에서만 됩니다"}
        try:
            return _side_view(self, bool(on))
        except Exception as exc:  # noqa: BLE001 - a window that cannot move must not break the app
            return {"ok": False, "error": f"창을 옮기지 못했습니다: {exc}"}

    # window events
    def _main_hwnd(self, title: str = MAIN_TITLE) -> int:
        """The main window's handle (or the small one's), found by its title like ``_side_view``; 0 when none."""
        if sys.platform != "win32":
            return 0
        import ctypes

        return ctypes.windll.user32.FindWindowW(None, title) or 0

    def _main_out_of_sight(self) -> bool:
        """True when the main window is minimized or hidden."""
        hwnd = self._main_hwnd()
        if not hwnd:
            return False
        import ctypes

        user32 = ctypes.windll.user32
        return bool(user32.IsIconic(hwnd) or not user32.IsWindowVisible(hwnd))

    def _show_main_window(self, command: int) -> bool:
        """``SW_MINIMIZE`` or ``SW_RESTORE`` for the main window. Posted (ShowWindowAsync), so it neither waits on the
        window's own thread nor runs inside the closing event; SW_RESTORE brings a maximized window back maximized."""
        hwnd = self._main_hwnd()
        if hwnd:
            import ctypes
            from ctypes import wintypes

            return bool(ctypes.windll.user32.ShowWindowAsync(wintypes.HWND(hwnd), command))
        if self._main is None:
            return False
        (self._main.minimize if command == SW_MINIMIZE else self._main.restore)()  # pywebview, when no handle
        return True

    def _recording_key(self):
        """Which recording is running (its session folder), for "once per recording"; ``None`` when not known."""
        state = self._backend.state() if hasattr(self._backend, "state") else {}
        for part in (state.get("status"), state.get("pc")):
            if isinstance(part, dict) and part.get("session_dir"):
                return part["session_dir"]
        return None

    def _on_main_closing(self):
        if self._backend.recording and not self._quitting:
            # a recording nobody can see runs for hours (user 2026-09-30): the window goes to the taskbar instead
            self._keep_open_while_recording()
            return False
        self._backend.stop_live_helpers()  # P-053: the Q&A helper and captions do not outlive the app
        self._quitting = True
        return True

    def _keep_open_while_recording(self) -> None:
        """Minimize the main window and say why, once per recording. Never raises: the close is refused anyway
        (pywebview would let the window close if the closing handler raised), and the recorder is not touched."""
        try:
            self._show_main_window(SW_MINIMIZE)
        except Exception:  # noqa: BLE001 - the window then simply stays open
            applog.swallowed("녹음 중 창 내리기")
        try:
            key = self._recording_key()
            if key is None or key != self._close_told:
                self._close_told = key
                from .notify import toast

                toast(*CLOSE_WHILE_RECORDING, icon=str(ICON) if ICON.exists() else None)
        except Exception:  # noqa: BLE001 - the recording goes on; only the explanation is missing
            applog.swallowed("녹음 중 창 닫기 알림(Windows)")

    def _attention_text(self, kind):
        """Title and body of the Windows notification for the kinds that get one (the user is probably looking at
        the slides, not at this window), else ``None``."""
        if kind == "stalled":
            return "녹음 소리가 끊겼습니다", "장치를 다시 여는 중입니다. 이어폰·마이크 연결을 확인하세요."
        if kind == "disk_low":  # the minutes: recording that can still be finished with its MP3 (recorder/service.py)
            minutes = self._backend.disk_minutes_left()
            return "녹음할 디스크 공간이 거의 없습니다", disk_low_text(minutes) + " 필요 없는 파일을 지워 주세요."
        return None

    def _attention(self, kind):
        """A notice appeared (class over, next class, long pause, sound lost, disk nearly full): beep and flash the
        taskbar button; when the main window is minimized or hidden, bring up the small window, which stays on top
        and has the notice card with its buttons (the main window if that fails). Lost sound and low disk also get a
        Windows notification."""
        if sys.platform != "win32":
            return
        import ctypes
        from ctypes import wintypes

        try:
            text = self._attention_text(kind)
            if text:
                from .notify import toast

                toast(*text, icon=str(ICON) if ICON.exists() else None)
        except Exception:  # noqa: BLE001 - the window shows it too
            applog.swallowed("녹음 알림(Windows)")
        try:
            import winsound

            winsound.MessageBeep(0x30)
        except (ImportError, RuntimeError):
            pass
        try:
            if self._main_out_of_sight():
                self._notice_out_of_sight()
        except Exception:  # noqa: BLE001 - the notice is also on the page; never break the recorder
            applog.swallowed("알림 때 창 다시 보이기")

        class FLASHWINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("hwnd", wintypes.HWND), ("dwFlags", wintypes.DWORD),
                        ("uCount", wintypes.UINT), ("dwTimeout", wintypes.DWORD)]

        for hwnd in (self._main_hwnd(), self._main_hwnd(MINI_TITLE) if self._mini else 0):
            if hwnd:
                info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd, 0x3 | 0xC, 0, 0)  # FLASHW_ALL | TIMERNOFG
                ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))

    def _on_main_closed(self):
        if self._mini is not None:
            self._mini.destroy()

    def _on_mini_closing(self):
        if self._quitting:
            return True
        self.show_main()  # closing the small window returns to the main window
        return False


def disk_low_text(minutes) -> str:
    """The low-disk sentence of the Windows notification. ``minutes`` is the engine's ``disk_minutes`` (both
    engines together, backend.py ``_note_disk``): how much more can be recorded and still be finished with its
    MP3; 0 = even the part so far no longer fits; ``None`` = not known. Same words as the window (app.js
    ``diskText``)."""
    if isinstance(minutes, bool) or not isinstance(minutes, int):
        return "녹음을 끝까지 저장할 공간이 얼마 남지 않았습니다."
    if minutes <= 0:
        return "지금까지 녹음한 것도 끝까지 저장할 공간이 모자랍니다."
    return f"끝까지 저장할 수 있는 녹음은 약 {minutes}분 남았습니다."


def stays_on_own_page(own: str | None, target: str | None) -> bool:
    """True when ``target`` is on the address the window's own page is served from: same scheme, host and port
    (and the same file for a ``file:`` page). ``about:blank`` is an empty page and is let through."""
    from urllib.parse import urlsplit

    if not own or not target:
        return False
    if target.strip().lower() == "about:blank":
        return True
    try:
        mine, theirs = urlsplit(own), urlsplit(target.strip())
        same = (mine.scheme.lower(), mine.hostname, mine.port) == (theirs.scheme.lower(), theirs.hostname, theirs.port)
    except ValueError:  # e.g. a port that is not a number
        return False
    if not same or mine.scheme.lower() not in ("http", "https", "file"):
        return False
    return mine.scheme.lower() != "file" or mine.path == theirs.path


def guard_navigation(window, on_refused=None) -> None:
    """Keep a window on the app's own page (release review 2026-11-16).

    The page's Content-Security-Policy does not govern where the window itself goes, and pywebview hands the whole
    Python API to whatever page the window lands on (edgechromium.py: ``inject_pywebview`` at every
    NavigationCompleted, no address check). So a top-level navigation to any other address is cancelled at
    WebView2's ``NavigationStarting``, before a request is made. Nothing in the app navigates the window: outside
    pages are opened by Python in the browser, and links that ask for a new window still go to the browser
    (pywebview's NewWindowRequested). Frames (the note viewer) are not top-level and are not touched here; the
    page policy's ``frame-src`` limits those.

    The handler is attached at ``before_show``, which pywebview raises on the window's own thread right after
    the WebView2 control exists and before its first page is asked for. If pywebview's internals are not as
    expected the guard cannot be attached; that is written to the log and the window opens as before."""
    def on_start(sender, args):
        try:
            target = str(args.Uri)
            own = window.real_url
            if not own:  # the window's own address is not known (yet): nothing to compare with. Letting the
                return   # page load matters more than this extra guard; a blank app would be the worse failure
            if stays_on_own_page(own, target):
                return
            args.Cancel = True
        except Exception:  # noqa: BLE001 - never break the window's own loading
            applog.swallowed("창 주소 확인")
            return
        applog.LOG.warning("창이 앱 화면이 아닌 주소로 가려는 것을 막았습니다: %s", target[:200])
        if on_refused:
            on_refused(target)

    def attach():
        try:
            window.native.webview.NavigationStarting += on_start  # the WinForms WebView2 control
        except Exception:  # noqa: BLE001
            applog.swallowed("창 주소 고정(이 창은 주소 확인 없이 열립니다)")

    window._lecturehub_navigation_guard = on_start  # the control only borrows the function: keep it alive
    window.events.before_show += attach
    made = getattr(window.events.before_show, "is_set", None)
    if callable(made) and made():  # made after webview.start (the small window): before_show has already passed
        _on_window_thread(window, attach)


def _on_window_thread(window, func) -> None:
    """Run ``func`` on the window's own thread (WinForms ``Invoke``), or right here when that is not possible."""
    try:
        from System import Action  # pythonnet

        window.native.Invoke(Action(func))
        return
    except Exception:  # noqa: BLE001, S110 - no WinForms form (tests) or no pythonnet: run it here
        pass
    func()


def _create_mini(api):
    """The small window that shows a notice while the main window is minimized (Api._mini_window). Same guards as
    the main window: it carries the whole API, so it may not leave the app's page; closing it goes back."""
    import webview

    mini = webview.create_window(
        MINI_TITLE, url=str(WEB / "mini.html"), js_api=api, width=340, height=150,
        resizable=False, hidden=True, on_top=True, background_color="#f5f0e8")
    if mini is None:
        raise RuntimeError("pywebview did not make the small window")
    guard_navigation(mini)
    mini.events.closing += api._on_mini_closing
    return mini


MAIN_MIN = (760, 640)  # the normal window's smallest size (logical px); 옆으로 보기 lowers it while on
SIDE_MIN = (300, 420)


def _side_view(api, on: bool) -> dict:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    hwnd = user32.FindWindowW(None, "Lecture Hub")
    if not hwnd:
        return {"ok": False, "error": "창을 찾지 못했습니다"}
    form = api._main.native

    def set_min(size):
        if form is None:
            return
        from System import Action  # pythonnet, WinForms: change the minimum size on the window's own thread
        from System.Drawing import Size

        scale = getattr(form, "_scale", 1) or 1
        form.Invoke(Action(lambda: setattr(form, "MinimumSize", Size(int(size[0] * scale), int(size[1] * scale)))))

    class RECT(ctypes.Structure):
        _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG), ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", RECT), ("rcWork", RECT), ("dwFlags", wintypes.DWORD)]

    SWP = 0x0004 | 0x0040  # NOZORDER | SHOWWINDOW
    if on:
        if api._side_restore is None:
            rect = RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            api._side_restore = (bool(user32.IsZoomed(hwnd)), (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top))
        set_min(SIDE_MIN)
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE: a maximized window cannot be placed
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        user32.GetMonitorInfoW(user32.MonitorFromWindow(hwnd, 2), ctypes.byref(info))
        work = info.rcWork
        width, height = (work.right - work.left) // 3, work.bottom - work.top
        x, y = work.right - width, work.top
        user32.SetWindowPos(hwnd, None, x, y, width, height, SWP)
        # Windows 10/11 windows have an invisible border: line the visible frame up with the screen edges
        frame, rect = RECT(), RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        if ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(frame), ctypes.sizeof(frame)) == 0:
            dl, dt, dr, db = frame.left - rect.left, frame.top - rect.top, rect.right - frame.right, rect.bottom - frame.bottom
            user32.SetWindowPos(hwnd, None, x - dl, y - dt, width + dl + dr, height + dt + db, SWP)
        return {"ok": True, "on": True, "width": width}
    saved, api._side_restore = api._side_restore, None
    set_min(MAIN_MIN)
    if saved is None or saved[0]:
        user32.ShowWindow(hwnd, 3)  # SW_MAXIMIZE (the app opens maximized)
    else:
        user32.SetWindowPos(hwnd, None, *saved[1], SWP)
    return {"ok": True, "on": False}


def _already_open() -> bool:
    """True when another window of this app (same settings file) is open; that window is brought forward.
    Verification runs (fake engine, a debugging port) and ``LECTUREHUB_UI_MULTI=1`` are never held back."""
    from lecturehub.core import settings, single_instance

    single_instance.acquire(single_instance.RUNNING)  # the installer asks to close the app while this is held
    if any(os.environ.get(name) for name in ("LECTUREHUB_UI_MULTI", "LECTUREHUB_UI_FAKE_ENGINE",
                                             "LECTUREHUB_UI_CDP_PORT")):
        return False
    if single_instance.acquire(single_instance.lock_name(settings.settings_path())):
        return False
    single_instance.show_existing(MAIN_TITLE)
    return True


def main() -> int:
    import webview

    applog.setup("app")  # first: whatever fails from here on is in the log
    _set_app_id()  # before any window exists
    if _already_open():
        return 0

    from .backend import Backend
    from .media_server import MediaServer

    backend = Backend(fake_engine=bool(os.environ.get("LECTUREHUB_UI_FAKE_ENGINE")))
    room = open_room(backend)
    api = Api(backend, open_library(backend.root), MediaServer(), room)
    backend.discard_hook = lambda name: api._library.discard_session(name)  # D-058 path, Recycle Bin only
    backend.attention_hook = api._attention
    import threading

    threading.Thread(target=backend.resume_jobs, daemon=True).start()  # S2: carry on after a restart
    threading.Thread(target=api._prewarm, daemon=True).start()  # S3: slides converted before class
    threading.Thread(target=api._notice_loop, daemon=True).start()  # notices heard in class, every 10 minutes
    threading.Thread(target=api._clova_loop, daemon=True).start()  # P-039: weekly Clova check while open
    if room is not None:
        threading.Thread(target=room.auto_loop, daemon=True).start()  # P-019: auto-return while the app is open
    port = os.environ.get("LECTUREHUB_UI_CDP_PORT")
    if port:  # verification only: any program on this PC can then drive the window, so it is said in the log
        applog.LOG.warning("검증용 디버깅 포트가 열려 있습니다(LECTUREHUB_UI_CDP_PORT). 평소에는 이 환경 변수를 두지 마세요.")
        webview.settings["REMOTE_DEBUGGING_PORT"] = int(port)
    webview.settings["ALLOW_DOWNLOADS"] = False
    size = os.environ.get("LECTUREHUB_UI_SIZE")  # "WxH" for verification; default is maximized
    width, height = (int(v) for v in size.split("x")) if size else (1280, 760)
    main_window = webview.create_window(
        MAIN_TITLE, url=str(WEB / "index.html"), js_api=api, width=width, height=height,
        min_size=MAIN_MIN, maximized=not size, background_color="#f5f0e8", text_select=True)
    api._main = main_window
    guard_navigation(main_window)  # it carries the whole API: it may not leave the app's own page
    main_window.events.loaded += api._bind_drop
    main_window.events.closing += api._on_main_closing
    main_window.events.closed += api._on_main_closed  # the small window (made on the first notice) goes too
    webview.start(icon=str(ICON) if ICON.exists() else None, private_mode=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
