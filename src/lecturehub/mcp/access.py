"""The one gate of the AI app link (D-072): which courses an AI app may read, decided fresh on every tool call.

A course is visible only when BOTH hold, read again for each call so that turning either off applies at once:
- the master switch ``ai_link.enabled`` in settings.json, read strictly (``core.settings.load_strict``): a
  missing, unreadable or broken settings file means "off". The app's own reader falls back on
  ``settings.json.bak``, which after "switch off" may still say "on"; this process never does (review 2026-11-24,
  L3). Off on a new install. The one thing this process ever writes there is the user's own press of
  [이 종류는 앞으로 자동] in its confirmation dialog (D-080, ``settings.save_ai_link_auto_strict``).
- a per-course consent for provider ``ai_app_link`` in ``<root>/_lecturehub/consent.json``, stored by the app's
  native confirmation dialog only (``ui/app.py`` ``ai_link_allow``). There is no standing all-courses consent for
  this provider (``consent.PER_COURSE_ONLY``).

The course key is ``Course.folder_name``, as for the other providers. "미분류" and blank names are never courses
(``consent.named``). Items without a course are never visible. Nothing here writes: a broken consent.json reads as
"no consent" without the copy ``transcription.store.read_json`` would leave next to it, and a malformed entry
(not an object) counts as no consent for that course only (review P6).
"""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from lecturehub.core import settings
from lecturehub.transcription.consent import ConsentStore, named

PROVIDER = "ai_app_link"
OFF = ("Lecture Hub의 AI 앱 연결이 꺼져 있습니다. Lecture Hub 앱의 설정 → AI → 'AI 앱 연결'에서 켜고, "
       "보여 줄 과목을 고르세요.")
NO_ROOT = "Lecture Hub의 학기 폴더가 아직 정해지지 않았습니다. Lecture Hub 앱에서 첫 설정을 마쳐 주세요."
NO_DATABASE = "Lecture Hub의 수업 목록이 아직 없습니다. Lecture Hub 앱을 한 번 열어 수업 목록을 만들어 주세요."
# One answer for "does not exist" and "exists in a course that is not allowed": the reply never confirms existence.
NOT_FOUND = "찾을 수 없습니다. 없는 항목이거나, Lecture Hub 앱에서 AI 앱 연결을 허용하지 않은 과목입니다."


class Refused(Exception):
    """The call cannot be answered (switch off, no data folder, nothing to read); ``str()`` is the user message."""


class _ReadOnlyConsent(ConsentStore):
    """ConsentStore's rules over a read that never writes (no .corrupt copy, no repair). Only object-shaped
    entries are kept, so one malformed entry cannot make every call fail."""

    def entries(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {provider: {course: entry for course, entry in courses.items() if isinstance(entry, dict)}
                for provider, courses in data.items() if isinstance(courses, dict)}


def _settings() -> dict:
    return settings.load_strict() or {}


def enabled() -> bool:
    saved = _settings().get("ai_link")
    return isinstance(saved, dict) and saved.get("enabled") is True


WRITE_OFF = ("Lecture Hub에서 'AI 앱이 고칠 수 있게'가 꺼져 있어 아무것도 바꾸지 않았습니다. 바꾸게 하려면 Lecture Hub 앱의 "
             "설정 › AI › AI 앱 연결에서 'AI 앱이 고칠 수 있게'를 켜고 이 AI 앱을 다시 시작하세요. 읽기는 지금처럼 됩니다.")


def write_enabled() -> bool:
    """D-076: the second switch ``ai_link.write``, read strictly like the master switch, and only with it on."""
    saved = _settings().get("ai_link")
    return isinstance(saved, dict) and saved.get("enabled") is True and saved.get("write") is True


def auto(kind: str) -> bool:
    """D-080: True only when the user set this kind of change to automatic (``ai_link.auto``), read strictly; a
    missing, broken or odd entry means "ask first"."""
    saved = _settings().get("ai_link")
    table = saved.get("auto") if isinstance(saved, dict) else None
    return isinstance(table, dict) and table.get(kind) is True


def data_root() -> Path:
    """The data folder of this install; raises Refused when the switch is off or no folder is set up."""
    saved = _settings()
    switch = saved.get("ai_link")
    if not (isinstance(switch, dict) and switch.get("enabled") is True):
        raise Refused(OFF)
    value = os.environ.get(settings.ENV_ROOT) or saved.get("data_root")
    if not value or not isinstance(value, str):
        raise Refused(NO_ROOT)
    return Path(value).expanduser()


def consent_path(root: Path) -> Path:
    return Path(root) / "_lecturehub" / "consent.json"


def course_allowed(root: Path, folder_name: str | None) -> bool:
    """One course by its folder name. False for blank, "미분류" and every course while the switch is off."""
    return enabled() and named(folder_name) and _ReadOnlyConsent(consent_path(root)).granted(PROVIDER, folder_name)


@dataclass(frozen=True)
class Gate:
    """What one tool call may see. Built per call by ``open_gate``; handlers never decide visibility themselves."""

    root: Path
    courses: dict[str, str]  # allowed Course.id -> folder_name
    others: frozenset[str] = frozenset()  # folder names of the courses that are NOT allowed (note file attribution)

    def allows(self, course_id: str | None) -> bool:
        return course_id is not None and course_id in self.courses

    def name(self, course_id: str) -> str:
        return self.courses[course_id]

    def course_id(self, name: str | None) -> str | None:
        """An allowed course by the name an AI app typed (spaces ignored), else None (not found or not allowed)."""
        wanted = "".join(str(name or "").split())
        if not wanted:
            return None
        return next((cid for cid, folder in self.courses.items() if "".join(folder.split()) == wanted), None)

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self.courses))

    def folder_course(self, first_part: str) -> str | None:
        """The allowed course whose folder is ``<root>/<first_part>``, else None.

        The folder of a course is ``<root>/<folder_name>``; an outside-class item named "<과목> …" may share the
        folder of "<과목>" (index/note_pack.py ``_course_dir``). A folder is attributed only when that is
        unambiguous: a folder that is itself a course that is not allowed belongs to that course."""
        if first_part in self.others:
            return None
        exact = next((cid for cid, folder in self.courses.items() if folder == first_part), None)
        if exact is not None:
            return exact
        sharing = [cid for cid, folder in self.courses.items() if folder.startswith(first_part + " ")]
        others_sharing = any(folder.startswith(first_part + " ") for folder in self.others)
        return sharing[0] if len(sharing) == 1 and not others_sharing else None


def open_gate(connection: sqlite3.Connection, root: Path) -> Gate:
    """Read the switch and consent.json now and keep only the allowed courses of this database."""
    if not enabled():
        raise Refused(OFF)
    store = _ReadOnlyConsent(consent_path(root))
    allowed, others = {}, set()
    for course_id, folder in connection.execute("SELECT id, folder_name FROM Course"):
        if named(folder) and store.granted(PROVIDER, folder):
            allowed[course_id] = folder
        elif folder:
            others.add(folder)
    return Gate(Path(root), allowed, frozenset(others))
