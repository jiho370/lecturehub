"""D-079 preferences (settings.json only): reminder switches and the per-course mute list.

Kept out of ``lecturehub.index`` so the app's recording path (ui/app.py) can read them without importing anything
that opens the database (D-050, tests/test_db_guard.py).
"""
from __future__ import annotations

from lecturehub.core import settings

SETTING = "reminders"


def preferences(*, saved: dict | None = None) -> dict:
    """``saved``: a settings snapshot the caller already loaded strictly (the read-only MCP inspector, D-082)."""
    return parse(settings.get(SETTING) if saved is None else saved.get(SETTING))


def preferences_strict() -> dict | None:
    """The same read from settings.json exactly as it is (the AI app link's process, D-083); None when the file is
    missing or broken."""
    data = settings.load_strict()
    return None if data is None else parse(data.get(SETTING))


def parse(raw) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    muted = raw.get("muted_courses")
    return {"deadlines": raw.get("deadlines") is not False, "briefing": raw.get("briefing") is not False,
            "muted_courses": sorted({c for c in muted if isinstance(c, str)}) if isinstance(muted, list) else []}


def set_preferences(changes: dict) -> dict:
    prefs = preferences()
    for key in ("deadlines", "briefing"):
        if key in changes:
            prefs[key] = bool(changes[key])
    if "muted_courses" in changes:
        names = changes["muted_courses"]
        if not isinstance(names, list) or any(not isinstance(c, str) for c in names):
            raise ValueError("알림을 끌 과목 목록이 필요합니다.")
        prefs["muted_courses"] = sorted(set(names))
    settings.save({SETTING: prefs})
    return prefs


def audible(rows: list[dict]) -> list[dict]:
    muted = set(preferences()["muted_courses"])
    return [r for r in rows if r.get("course") not in muted]
