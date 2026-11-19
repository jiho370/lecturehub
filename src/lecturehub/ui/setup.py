"""First-run setup: choose the data folder, enter the timetable, write the recorder settings (release prep, 2026-11).

Pure file work, no window code, so it can be tested. The app calls it from ``Backend.setup_apply`` and then asks the
user to reopen the app (the library and media objects are built from the folder chosen at start).
Writes only: the settings file in %APPDATA%\\LectureHub, recorder.toml in %APPDATA%\\lecturehub, and inside the chosen
folder (course folders, 시간표.csv, _lecturehub). An existing 시간표.csv or recorder.toml is never overwritten.
"""
from __future__ import annotations

import os
import re
from datetime import date
from pathlib import Path

from lecturehub.core import applog, extra_courses, settings, timetable, tools
from lecturehub.core.timetable import DAYS, TimetableError as SetupError  # noqa: F401 - DAYS kept for callers

_TERM = re.compile(r"^[0-9A-Za-z가-힣 ._-]{1,24}$")


def suggested_term(today: date | None = None) -> str:
    """'26-1' (Mar-Aug) or '26-2' (Sep-Dec; Jan-Feb belongs to the previous year's second term)."""
    today = today or date.today()
    year, half = (today.year, 1) if 3 <= today.month <= 8 else (today.year if today.month >= 9 else today.year - 1, 2)
    return f"{year % 100:02d}-{half}"


# A folder is taken for cloud-synced when one part of its path starts with one of these (compared case-folded).
# Google Drive for desktop has no folder called "Google Drive": in stream mode it is a drive letter with
# "My Drive" / "내 드라이브" and "Shared drives" / "공유 드라이브" under it, in mirror mode a folder "My Drive" /
# "내 드라이브" in the user's profile ("My Drive (account)" with several accounts); the client before it used
# "Google Drive" / "Google 드라이브". These names, the "Google Drive" volume label and the stream drive showing up
# as a fixed disk are written from knowledge of the client; they were not checked on a PC that has it installed
# (the development PC has none). core/diagnostics.py uses the same test (``cloud_synced``).
CLOUD_FOLDERS = ("onedrive", "dropbox", "google drive", "googledrive", "google 드라이브", "my drive", "내 드라이브",
                 "shared drives", "공유 드라이브", "icloud", "mybox", "naver mybox")
CLOUD_ENV = ("OneDrive", "OneDriveConsumer", "OneDriveCommercial")
CLOUD_VOLUMES = ("google drive",)  # volume label of the Google Drive stream drive, whatever its folders are called
DRIVE_FIXED = 3


def _volume_label(anchor: str) -> str:
    """The volume label of a drive root, "" when it cannot be read (GetVolumeInformationW)."""
    if os.name != "nt":
        return ""
    import ctypes

    label = ctypes.create_unicode_buffer(261)
    try:
        done = ctypes.windll.kernel32.GetVolumeInformationW(anchor, label, len(label), None, None, None, None, 0)
    except (OSError, ValueError):
        return ""
    return label.value if done else ""


def cloud_synced(path: Path, *, drive_kind=None, volume_label=None) -> bool:
    """Does the folder look like it is inside a cloud-sync folder? Told from names only: a path part that starts
    with a usual sync-folder name (``CLOUD_FOLDERS``), the OneDrive environment folders, or a local drive whose
    volume label is Google Drive's. A sync folder the user renamed is not recognised, and a plain folder that
    happens to be called "My Drive" is. ``drive_kind(anchor)`` / ``volume_label(anchor)`` are test seams for
    GetDriveTypeW / GetVolumeInformationW; the label is read for fixed drives only (a network drive can hang)."""
    path = Path(path)
    if any(part.casefold().startswith(CLOUD_FOLDERS) for part in path.parts):
        return True
    for name in CLOUD_ENV:
        base = os.environ.get(name)
        try:
            if base and path.resolve().is_relative_to(Path(base).resolve()):
                return True
        except OSError:
            continue
    if path.anchor and _drive_type(path, drive_kind) == DRIVE_FIXED:
        try:
            label = (volume_label or _volume_label)(path.anchor)
        except (OSError, ValueError):
            label = ""
        if str(label or "").casefold().startswith(CLOUD_VOLUMES):
            return True
    return False


def _drive_type(path: Path, kind=None) -> int | None:
    """GetDriveTypeW for the path's drive, ``None`` when it cannot be told (``kind(anchor)`` is a test seam)."""
    if os.name != "nt" and kind is None:
        return None
    if kind is None:
        import ctypes

        kind = ctypes.windll.kernel32.GetDriveTypeW
    try:
        return kind(path.anchor or str(path))
    except (OSError, ValueError):
        return None


def _drive_kind(path: Path, kind=None) -> str:
    """"removable", "network" or "" (``kind(anchor)`` is a test seam for GetDriveTypeW)."""
    return {2: "removable", 4: "network"}.get(_drive_type(path, kind), "")


def folder_notes(parent: str, *, drive_kind=None, volume_label=None) -> list[str]:
    """Things worth knowing about a folder before the semester goes there (release review 2026-11-16). Advice only:
    the user may have good reasons, and nothing here blocks the choice."""
    if not parent or not str(parent).strip():
        return []
    path = Path(parent).expanduser()
    notes = []
    if cloud_synced(path, drive_kind=drive_kind, volume_label=volume_label):
        notes.append("이 폴더는 클라우드 동기화 폴더(OneDrive·Google Drive 등) 안에 있는 것으로 보입니다. 녹음하는 동안 큰 파일이 "
                     "계속 올라가고, 동기화 프로그램이 수업 목록 파일을 잠가 오류가 날 수 있습니다. 동기화되지 않는 폴더를 권합니다.")
    kind = _drive_kind(path, drive_kind)
    no_bin = " 녹음 뒤 남는 WAV 사본은 MP3를 확인한 다음 휴지통 없이 바로 지웁니다."  # recorder/compact.py, 2026-11-19
    if kind == "removable":
        notes.append("USB 같은 이동식 드라이브입니다. 뽑히면 녹음이 끊기고, 이 드라이브에는 휴지통이 없어 '되돌릴 수 있는 삭제'가 되지 않습니다."
                     + no_bin)
    elif kind == "network":
        notes.append("네트워크 드라이브입니다. 연결이 끊기면 녹음이 멈출 수 있고, 휴지통이 없어 '되돌릴 수 있는 삭제'가 되지 않습니다."
                     + no_bin)
    if len(str(path)) > 150:
        notes.append("폴더 경로가 깁니다. Windows에서 긴 경로는 일부 프로그램이 열지 못할 수 있으니 더 짧은 위치를 권합니다.")
    return notes


def suggested_parent() -> Path:
    """``Documents/LectureHub``, unless Documents itself is synced by OneDrive (the default on many PCs): then a plain
    folder in the user's profile, so recordings are not uploaded while they are being written."""
    documents = Path.home() / "Documents"
    onedrive = [Path(os.environ[name]) for name in CLOUD_ENV if os.environ.get(name)]
    redirected = any((base / name).is_dir() for base in onedrive for name in ("Documents", "문서")) and not documents.is_dir()
    if redirected or cloud_synced(documents):
        return Path.home() / "LectureHub"
    return documents / "LectureHub"


folder_name = timetable.folder_name
timetable_csv = timetable.to_csv


def clean_courses(courses: list[dict]) -> list[dict]:
    """Validate rows from the wizard: ``{name, slots: [{day, start, end}]}`` → same shape with a folder name."""
    return timetable.clean([{"name": row.get("name", ""), "slots": row.get("slots")} for row in courses or []])


def _toml_str(value) -> str:
    """A TOML string for a Windows path: literal ('…', backslashes as they are) unless the path holds a quote."""
    text = str(value)
    if "'" not in text:
        return f"'{text}'"
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def recorder_toml(out_dir: Path, timetable: Path | None) -> str:
    lines = ["[recorder]", f"out_dir = {_toml_str(out_dir)}", 'mode = "mic"']
    if timetable:
        lines.append(f"timetable = {_toml_str(timetable)}")
    return "\n".join(lines) + "\n"


def info() -> dict:
    root = settings.data_root()
    return {"needs_setup": root is None, "trial": bool(settings.get("trial")), "root": str(root) if root else "",
            "term": suggested_term(),
            "parent": str(suggested_parent()), "parent_notes": folder_notes(str(suggested_parent())),
            "tools": tools.status(check_login=False)}


def layout_preview(term: str, courses: list[str], style: str) -> dict:
    """The folder tree the wizard shows beside the layout choice. Category folders appear on first use only."""
    if style not in settings.LAYOUTS:
        raise SetupError("폴더 방식을 고르세요")
    names = [n for n in (str(c).strip() for c in courses or []) if n][:4] or ["과목 이름"]
    cats = " · ".join(settings.LAYOUTS[style])
    lines = [f"{term or '학기'}\\"]
    if style == "existing":
        lines += ["  (고른 폴더를 그대로 읽습니다. 옮기지 않고, 정리는 제안 → 확인 → 되돌리기로만 합니다)"]
    else:
        lines += [f"  {name}\\   {cats}  (파일이 생길 때 만듦)" for name in names]
        if style == "detailed":
            lines += ["  녹음본\\과목\\   녹음 보관용 복사본", "  전사본\\과목\\"]
    lines += ["  시간표.csv", "  _lecturehub\\   앱이 쓰는 폴더 (녹음 원본·전사·기록)"]
    return {"style": style, "label": settings.LAYOUT_NAMES[style], "lines": lines}


def apply(parent: str, term: str, courses: list[dict], term_start: str = "", term_end: str = "",
          layout: str = "") -> dict:
    """Create ``<parent>/<term>``, its course folders and 시간표.csv, write recorder.toml and remember the folder."""
    term = (term or "").strip()
    if not _TERM.match(term):
        raise SetupError("학기 이름은 '26-2'처럼 짧게 적어 주세요.")
    if not parent or not str(parent).strip():
        raise SetupError("학기 폴더를 고르세요.")
    base = Path(parent).expanduser()
    if not base.is_absolute():
        raise SetupError("폴더는 전체 경로여야 합니다.")
    rows = clean_courses(courses)
    if layout and layout not in settings.LAYOUTS:
        raise SetupError("폴더 방식을 고르세요")
    existing = layout == "existing"  # the picked folder *is* the semester folder; only _lecturehub is added
    if existing and not base.is_dir():
        raise SetupError("이미 쓰는 학기 폴더를 고르세요.")
    root = base if existing else base / term
    try:
        (root / "_lecturehub" / "recordings").mkdir(parents=True, exist_ok=True)
        if (term_start or "").strip() or (term_end or "").strip():
            timetable.save_term(root, term_start, term_end)
        # A course with no class time cannot be a timetable row (a row needs a weekday and a time). Before
        # 2026-11-16 it got a folder and then silently was no course at all; now it becomes a '수업 외' topic,
        # which the record button offers like any course (core/extra_courses.py).
        timed = [course for course in rows if course["slots"]]
        if not existing:
            for course in timed:
                (root / course["folder"]).mkdir(exist_ok=True)
        table = root / "시간표.csv"
        if timed and not table.exists():
            table.write_text(timetable_csv(timed), encoding="utf-8-sig", newline="")
        extra, skipped = [], []
        for course in rows:
            if course["slots"]:
                continue
            try:
                extra_courses.add(root, course["name"], [c["folder"] for c in timed])
                extra.append(course["name"])
            except ValueError as exc:
                skipped.append({"name": course["name"], "why": str(exc)})
        # a config written for the trial semester points at the trial folder: leaving the trial replaces it
        write_recorder_config(root, table if table.exists() else None, replace=bool(settings.get("trial")))
    except OSError as exc:
        raise SetupError(f"폴더를 만들 수 없습니다: {exc}") from exc
    settings.save({"data_root": str(root), "term": term, "trial": None})  # leaving the trial semester
    if layout:
        settings.set_folder_layout(layout)
    try:  # build the library now, so the first recording after the restart links to its class
        from .library import Library

        Library(root)._ensure()
    except Exception:  # noqa: BLE001 - setup itself succeeded; the window builds the library on first use instead
        applog.swallowed("첫 설정 뒤 수업 목록 만들기")
    return {"ok": True, "root": str(root), "courses": [c["folder"] for c in timed], "extra": extra,
            "skipped": skipped, "restart": True}


def write_recorder_config(root: Path, table: Path | None, *, replace: bool = False) -> Path:
    """recorder.toml for this semester folder (recordings under its _lecturehub).

    A file that is already there keeps the user's own choices (device, mode, …); only where recordings go and which
    timetable is read follow the folder (``_repoint``). ``replace`` writes a fresh file (leaving the trial)."""
    config = _recorder_config_path()
    out_dir = Path(root) / "_lecturehub" / "recordings"
    if replace or not config.exists():
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(recorder_toml(out_dir, table), encoding="utf-8")
    else:
        _repoint(config, out_dir, table)
    return config


def _repoint(config: Path, out_dir: Path, table: Path | None) -> bool:
    """Make an existing recorder.toml record into ``out_dir``. Before 2026-11-16 a second setup (a new semester
    folder) left it alone, so recordings kept going to the old folder. A file that cannot be read is left as it is
    (the recorder reports it); the previous text is kept beside it as ``recorder.toml.bak``."""
    import tomllib

    try:
        text = config.read_text(encoding="utf-8-sig")
        section = tomllib.loads(text).get("recorder")
    except (OSError, ValueError):
        return False
    if not isinstance(section, dict) or not isinstance(section.get("out_dir"), str):
        return False
    same_table = (section.get("timetable") or None) == (str(table) if table else None)
    if Path(section["out_dir"]) == out_dir and same_table:
        return False
    lines, done = [], set()
    for line in text.splitlines():
        key = line.split("=", 1)[0].strip() if "=" in line else ""
        if key == "out_dir" and key not in done:
            lines.append(f"out_dir = {_toml_str(out_dir)}")
        elif key == "timetable" and key not in done:
            if table:
                lines.append(f"timetable = {_toml_str(table)}")
        else:
            lines.append(line)
            continue
        done.add(key)
    if table and "timetable" not in done:
        at = next(i for i, line in enumerate(lines) if line.split("=", 1)[0].strip() == "out_dir") + 1
        lines.insert(at, f"timetable = {_toml_str(table)}")
    new_text = "\n".join(lines) + "\n"
    try:
        check = tomllib.loads(new_text).get("recorder", {})
    except ValueError:
        return False
    if check.get("out_dir") != str(out_dir):  # e.g. out_dir written on several lines: do not guess
        return False
    config.with_name(config.name + ".bak").write_text(text, encoding="utf-8")
    config.write_text(new_text, encoding="utf-8")
    return True


def _recorder_config_path() -> Path:
    from lecturehub.recorder.classconfig import default_config_path

    return default_config_path()
