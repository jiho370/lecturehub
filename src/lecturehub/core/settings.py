"""Per-user app settings and the data-folder resolver (release prep, 2026-11).

The data folder ("학기 폴더") used to be a literal path in five modules. Now there is one answer:
``LECTUREHUB_ROOT`` (environment) first, then ``data_root`` in ``%APPDATA%\\LectureHub\\settings.json``,
else ``None`` — callers show "setup needed" instead of guessing a folder.

Settings live outside the data folder so a new install never writes into someone else's library.
Keep this module free of sqlite3 / index imports (D-050: the recorder imports it).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

ENV_ROOT = "LECTUREHUB_ROOT"
ENV_SETTINGS = "LECTUREHUB_SETTINGS"
NEEDS_SETUP = "학기 폴더가 아직 정해지지 않았습니다. 앱의 첫 실행 설정에서 고르거나 --root 로 지정하세요."


class NeedsSetup(RuntimeError):
    """No data folder is configured yet."""


def settings_path() -> Path:
    override = os.environ.get(ENV_SETTINGS)
    if override:
        return Path(override)
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(base) / "LectureHub" / "settings.json"


def load() -> dict:
    """The settings dict; a missing or unreadable file is an empty dict (never raises).

    A file that is there but broken (a crash or a full disk while it was written, a hand edit) is not thrown
    away: it is kept as ``settings.json.corrupt-<time>`` and the last good copy (``settings.json.bak``, written
    by ``save``) is used instead. Before 2026-11-16 a broken file read as "nothing configured", the first-run
    setup came back and the next save replaced everything."""
    path = settings_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError:
        return {}
    except ValueError:
        return _recover(path)
    return data if isinstance(data, dict) else {}


def load_strict() -> dict | None:
    """settings.json exactly as it is, for a reader that must fail closed (the AI app link's process, D-072):
    ``None`` when it is missing, unreadable, broken or not an object. Never falls back on ``settings.json.bak``
    (after "switch off" that copy may still say "on") and never writes anything (no ``.corrupt-*`` copy)."""
    try:
        data = json.loads(settings_path().read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _recover(path: Path) -> dict:
    from . import applog

    try:
        kept = path.with_name(f"{path.name}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}")
        if not list(path.parent.glob(f"{path.name}.corrupt-*")):  # once: later reads of the same broken file add nothing
            kept.write_bytes(path.read_bytes())
            applog.LOG.error("설정 파일을 읽을 수 없어 사본을 남겼습니다: %s", kept.name)
        backup = json.loads(path.with_name(path.name + ".bak").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return backup if isinstance(backup, dict) else {}


def save(patch: dict) -> dict:
    """Merge ``patch`` into the settings file (atomic replace) and return the result.

    A ``None`` value removes the key.
    """
    data = load()
    for key, value in patch.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.part")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:  # keep the last file that could be read, for load() to fall back on
        previous = path.read_bytes()
        if isinstance(json.loads(previous.decode("utf-8-sig")), dict):
            path.with_name(path.name + ".bak").write_bytes(previous)
    except (OSError, ValueError):
        pass
    os.replace(tmp, path)
    return data


def get(key: str, default=None):
    return load().get(key, default)


def data_root() -> Path | None:
    """The configured data folder, or ``None`` when setup has not happened."""
    value = os.environ.get(ENV_ROOT) or load().get("data_root")
    return Path(value).expanduser() if value else None


def parking_root() -> Path:
    """An app-private folder used only while no data folder is configured (keeps callers from writing to CWD)."""
    return settings_path().parent / "unconfigured"


def require_root(explicit: Path | str | None = None) -> Path:
    """``explicit`` if given, else the configured folder; raises :class:`NeedsSetup` when neither exists."""
    if explicit:
        return Path(explicit)
    root = data_root()
    if root is None:
        raise NeedsSetup(NEEDS_SETUP)
    return root


def root_option_callback(value: Path | None) -> Path:
    """Typer callback for every ``--root`` option: fills in the configured folder or fails clearly."""
    try:
        return require_root(value)
    except NeedsSetup as exc:
        import typer

        raise typer.BadParameter(str(exc)) from exc


def legacy_note_folders() -> frozenset[str]:
    """Extra folder names (spaces ignored) that older semesters used for lecture notes; read as 정리본.

    ``legacy_note_folders`` in settings.json, e.g. ``["노트 모음"]``. Empty on a new install.
    """
    names = load().get("legacy_note_folders") or []
    return frozenset(str(n).replace(" ", "") for n in names if str(n).strip()) if isinstance(names, list) else frozenset()


def course_abbreviations() -> dict[str, str]:
    """``{folder: short ASCII code}`` the user set in settings.json (``course_abbreviations``); empty on a new install.

    Used only when a course row is first created (the ``Course.abbr`` column); existing rows keep their value.
    """
    stored = load().get("course_abbreviations")
    if not isinstance(stored, dict):
        return {}
    return {str(k).strip(): str(v).strip() for k, v in stored.items() if str(k).strip() and str(v).strip()}


LABS = {"gsr": "그스룸 예약", "clova": "Clova 가져오기"}  # optional features, off until switched on


def labs() -> dict[str, bool]:
    """Which optional features are on. A new install has them all off; the owner's settings.json turns them on."""
    stored = load().get("labs")
    stored = stored if isinstance(stored, dict) else {}
    from . import edition

    return {name: bool(stored.get(name, False)) for name in LABS if edition.has(name)}


def set_lab(name: str, on: bool) -> dict[str, bool]:
    from . import edition

    if name not in LABS or not edition.has(name):
        raise KeyError(name)
    current = labs()
    current[name] = bool(on)
    save({"labs": current})
    return current


# ---------- AI app link (D-072): the master switch of the read-only MCP server (lecturehub.mcp). Off on a new install.
def ai_link() -> dict:
    """{"enabled", "write"}. Read on every tool call of the MCP process, so turning either off applies at once.
    ``write`` (D-076, "AI 앱이 고칠 수 있게") lets the AI app add/change calendar items and save new notes; it counts
    only while ``enabled`` is on too (the MCP process reads both strictly, ``mcp/access.py``)."""
    saved = load().get("ai_link")
    saved = saved if isinstance(saved, dict) else {}
    return {"enabled": saved.get("enabled") is True, "write": saved.get("write") is True}


def set_ai_link(enabled: bool) -> dict:
    """The master switch; the write switch keeps its value (it applies again when the link is turned back on)."""
    saved = load().get("ai_link")
    saved = saved if isinstance(saved, dict) else {}
    save({"ai_link": {**saved, "enabled": bool(enabled)}})
    return ai_link()


def set_ai_link_write(write: bool) -> dict:
    saved = load().get("ai_link")
    saved = saved if isinstance(saved, dict) else {}
    save({"ai_link": {**saved, "write": bool(write)}})
    return ai_link()


# D-080: per kind of change (= write tool of the AI app link), whether Lecture Hub still asks on screen (false, the
# default) or lets it apply at once (true). ``ai_link.auto`` = {kind: true}; read strictly by the MCP process.
AI_AUTO_KINDS = ("add_calendar_event", "update_calendar_event", "delete_calendar_event", "resolve_candidate", "save_note",
                 "set_timetable", "add_schedule_change", "set_reminder_settings",
                 "request_notice_rescan")  # D-083 kinds after the first five


def _auto_table(saved) -> dict[str, bool]:
    table = saved.get("auto") if isinstance(saved, dict) else None
    table = table if isinstance(table, dict) else {}
    return {kind: table.get(kind) is True for kind in AI_AUTO_KINDS}


def ai_link_auto(*, saved: dict | None = None) -> dict[str, bool]:
    """Optional strict snapshot for the read-only MCP inspector (never recover a broken file there)."""
    return _auto_table((load() if saved is None else saved).get("ai_link"))


def set_ai_link_auto(kind: str, on: bool) -> dict[str, bool]:
    """The app's settings rows. Turning a kind to automatic needs the native dialog's token (``ui/app.py``)."""
    if kind not in AI_AUTO_KINDS:
        raise KeyError(kind)
    saved = load().get("ai_link")
    saved = saved if isinstance(saved, dict) else {}
    table = _auto_table(saved)
    table[kind] = bool(on)
    save({"ai_link": {**saved, "auto": {k: True for k, v in table.items() if v}}})
    return ai_link_auto()


def set_ai_link_auto_all() -> dict[str, bool]:
    """[모두 자동]: every kind automatic (the native dialog's token is checked in ``ui/app.py``)."""
    saved = load().get("ai_link")
    saved = saved if isinstance(saved, dict) else {}
    save({"ai_link": {**saved, "auto": {kind: True for kind in AI_AUTO_KINDS}}})
    return ai_link_auto()


def reset_ai_link_auto() -> dict[str, bool]:
    """[모두 다시 묻기]."""
    saved = load().get("ai_link")
    saved = saved if isinstance(saved, dict) else {}
    save({"ai_link": {**saved, "auto": {}}})
    return ai_link_auto()


def save_ai_link_auto_strict(kind: str) -> bool:
    """[이 종류는 앞으로 자동] pressed in the MCP process's dialog: mark ``kind`` automatic, reading the file strictly
    (a missing or broken file, or a switch turned off meanwhile, writes nothing and returns False) and replacing it
    atomically through a temporary file of its own name. The previous file is kept as ``settings.json.bak`` as
    ``save`` does."""
    if kind not in AI_AUTO_KINDS:
        return False
    data = load_strict()
    saved = data.get("ai_link") if data is not None else None
    if not isinstance(saved, dict) or saved.get("enabled") is not True or saved.get("write") is not True:
        return False
    table = _auto_table(saved)
    table[kind] = True
    data["ai_link"] = {**saved, "auto": {k: True for k, v in table.items() if v}}
    return _replace_strict(data)


AI_WRITABLE_SETTINGS = ("reminders",)  # D-083: the only keys an AI app may change (set_reminder_settings)


def save_from_ai_strict(key: str, value) -> bool:
    """D-083 ``set_reminder_settings`` (the MCP process): one of ``AI_WRITABLE_SETTINGS`` replaced, read strictly like
    ``save_ai_link_auto_strict`` — a missing or broken file, or the link or the write switch off, writes nothing and
    returns False."""
    if key not in AI_WRITABLE_SETTINGS:
        return False
    data = load_strict()
    saved = data.get("ai_link") if data is not None else None
    if not isinstance(saved, dict) or saved.get("enabled") is not True or saved.get("write") is not True:
        return False
    data[key] = value
    return _replace_strict(data)


def _replace_strict(data: dict) -> bool:
    import tempfile

    path = settings_path()
    try:
        previous = path.read_bytes()
        handle, part = tempfile.mkstemp(prefix=path.name + ".", suffix=".part", dir=path.parent)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                out.write(json.dumps(data, ensure_ascii=False, indent=2))
                out.flush()
                os.fsync(out.fileno())
            path.with_name(path.name + ".bak").write_bytes(previous)
            for attempt in range(5):  # the app may be reading the file this very moment (Windows sharing)
                try:
                    os.replace(part, path)
                    break
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(0.05)
        except BaseException:
            try:
                os.unlink(part)
            except OSError:
                pass
            raise
    except OSError:
        return False
    return True


MIN_COMPANIONS = 3  # 그스룸 needs 4 people; the applicant (the signed-in account) is added by the site script


def parse_companions(text: str) -> list[list[str]]:
    """Lines like ``이름 20xx12345`` or ``이름,20xx12345`` → rows. Raises ValueError naming the bad line.

    Typed on the 그스룸 screen per booking (public build: never saved). The full build can also keep a list in settings (gsr_companions)."""
    rows = []
    for number, line in enumerate((text or "").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        parts = [p for p in line.replace(",", " ").replace("\t", " ").split(" ") if p]
        if len(parts) < 2 or not parts[-1].isdigit() or not 6 <= len(parts[-1]) <= 12:
            raise ValueError(f"{number}번째 줄: '이름 학번' 모양으로 적어 주세요")
        rows.append([" ".join(parts[:-1]), parts[-1]])
    if len(rows) > 8:
        raise ValueError("동반 이용자는 8명까지 넣을 수 있습니다")
    return rows


# ---------- course folder layout (2026-11-13): chosen at first run, changeable in settings
# Every category folder below is always *read*; the layout only decides which ones the app suggests and creates
# (on first use, never ahead of time) and whether a recording is also copied to <학기>\녹음본.
LAYOUTS = {
    "basic": ("강의자료", "과제", "정리본"),
    "detailed": ("강의자료", "실습자료", "과제", "정리본", "참고자료", "교재"),
    "existing": ("강의자료", "실습자료", "과제", "정리본", "참고자료", "교재"),  # the user's own folders, read as they are
}
LAYOUT_NAMES = {"basic": "기본", "detailed": "자세히", "existing": "이미 쓰는 폴더"}


def folder_layout() -> dict:
    """{"style", "categories", "archive_copies"}. Unset: 기본 in the public build, the owner's 자세히 in the full one."""
    from lecturehub.core import edition

    saved = load().get("folders") if isinstance(load().get("folders"), dict) else {}
    style = saved.get("style") if saved.get("style") in LAYOUTS else ("basic" if edition.name() == "public" else "detailed")
    archive = saved.get("archive_copies")
    archive = bool(archive) if isinstance(archive, bool) else style == "detailed"
    return {"style": style, "label": LAYOUT_NAMES[style], "categories": list(LAYOUTS[style]), "archive_copies": archive}


def set_folder_layout(style: str, archive_copies: bool | None = None) -> dict:
    if style not in LAYOUTS:
        raise ValueError("폴더 방식은 기본·자세히·이미 쓰는 폴더 중에서 고르세요")
    value = {"style": style}
    if archive_copies is not None:
        value["archive_copies"] = bool(archive_copies)
    save({"folders": value})
    return folder_layout()


# ---------- recordings to the Recycle Bin after the Google transcript (2026-11-13, index/audio_cleanup.py)
AUDIO_CLEANUP_DAYS = 7


def audio_cleanup() -> dict:
    """{"enabled", "days"}. Unset: on in the owner's full build, off (opt-in) in the public one."""
    from lecturehub.core import edition

    saved = load().get("audio_cleanup") if isinstance(load().get("audio_cleanup"), dict) else {}
    enabled = saved.get("enabled")
    enabled = enabled if isinstance(enabled, bool) else edition.name() != "public"
    days = saved.get("days")
    days = days if isinstance(days, int) and not isinstance(days, bool) and 1 <= days <= 365 else AUDIO_CLEANUP_DAYS
    return {"enabled": enabled, "days": days}


def set_audio_cleanup(enabled: bool, days: int = AUDIO_CLEANUP_DAYS) -> dict:
    if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 365:
        raise ValueError("며칠 뒤에 정리할지 1~365 사이로 고르세요")
    save({"audio_cleanup": {"enabled": bool(enabled), "days": days}})
    return audio_cleanup()


def keep_lossless() -> bool:
    """Keep the segments' lossless FLAC next to the MP3 master (off: MP3 only, user decision 2026-11-13)."""
    return load().get("keep_lossless") is True

# ---------- 그스룸 owner preferences (full edition only; the public build never shows or reads them, user 2026-11-14)
ROOMS_NEUTRAL = {"floor_first": None, "even_first": False, "avoid": [], "prefer_monitor": False, "center": None}


def gsr_companions() -> list[list[str]]:
    """``[[name, student_no], ...]`` saved in settings on this PC (full edition): fills the booking form, editable."""
    gsr = load().get("gsr")
    rows = gsr.get("companions") if isinstance(gsr, dict) else None
    out = []
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, (list, tuple)) and len(row) == 2 and str(row[0]).strip() and str(row[1]).strip().isdigit():
            out.append([str(row[0]).strip(), str(row[1]).strip()])
    return out


def set_gsr_companions(rows) -> list[list[str]]:
    """``rows``: the text form ('이름 학번' lines) or ``[[name, no], ...]``."""
    if not isinstance(rows, str):
        rows = "\n".join(f"{r[0]} {r[1]}" for r in rows or [] if isinstance(r, (list, tuple)) and len(r) == 2)
    rows = parse_companions(rows)
    gsr = load().get("gsr")
    gsr = dict(gsr) if isinstance(gsr, dict) else {}
    gsr["companions"] = rows
    save({"gsr": gsr})
    return rows


def gsr_rooms() -> dict:
    """How 그스룸 picks among free rooms (gsr_core.js useRoomPrefs). Unset: no preference."""
    gsr = load().get("gsr")
    saved = gsr.get("rooms") if isinstance(gsr, dict) else None
    try:
        return clean_gsr_rooms({**ROOMS_NEUTRAL, **saved}) if isinstance(saved, dict) else dict(ROOMS_NEUTRAL)
    except ValueError:
        return dict(ROOMS_NEUTRAL)


def clean_gsr_rooms(prefs: dict) -> dict:
    floor = prefs.get("floor_first")
    try:
        floor = None if floor in (None, "", 0, "0") else int(floor)
        center = prefs.get("center")
        center = None if center in (None, "") else float(center)
    except (TypeError, ValueError) as exc:
        raise ValueError("층과 방 번호는 숫자로 적어 주세요") from exc
    if floor not in (None, 2, 4):
        raise ValueError("먼저 볼 층은 2층이나 4층만 고를 수 있습니다")
    avoid = prefs.get("avoid") or []
    if isinstance(avoid, str):
        avoid = avoid.replace(",", " ").split()
    avoid = [str(a).strip().upper() for a in avoid if str(a).strip()]
    if len(avoid) > 12 or any(not (len(a) == 4 and a[0] == "G" and a[1:].isdigit()) for a in avoid):
        raise ValueError("미룰 방은 G404처럼 적어 주세요(12개까지)")
    if center is not None and not 200 <= center < 500:
        raise ValueError("가까운 방 번호는 416처럼 방 번호로 적어 주세요")
    return {"floor_first": floor, "even_first": bool(prefs.get("even_first")), "avoid": avoid,
            "prefer_monitor": bool(prefs.get("prefer_monitor")), "center": center}


def set_gsr_rooms(prefs: dict) -> dict:
    rows = clean_gsr_rooms(prefs if isinstance(prefs, dict) else {})
    gsr = load().get("gsr")
    gsr = dict(gsr) if isinstance(gsr, dict) else {}
    gsr["rooms"] = rows
    save({"gsr": gsr})
    return rows


# ---------- 실시간 전사 engine (user 2026-11-14: "치르프를 할지 트랜스크라이브 라이브를 할지")
LIVE_ENGINES = {
    "quick": "Gemini Live — 말하자마자 글로(무료 Gemini 키, 소음에 약함)",
    "chirp": "Chirp 3 — 더 정확, 15~25초 늦게 나옴(Google Cloud 로그인, 월 60분 무료 뒤 유료)",
    "both": "둘 다 — 빠른 줄을 먼저 보이고 Chirp 줄로 확정",
}


def live_engine() -> str:
    value = load().get("live_engine")
    return value if value in LIVE_ENGINES else "quick"


def set_live_engine(value: str) -> str:
    if value not in LIVE_ENGINES:
        raise ValueError("실시간 전사 엔진을 다시 고르세요")
    save({"live_engine": value})
    return value
