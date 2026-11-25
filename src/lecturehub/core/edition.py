"""Which build this is: the owner's full app or the public edition (release prep, 2026-11).

The public export writes ``edition.json`` next to this file and deletes the modules of the removed features
(MAI automation, live Q&A helper, room booking, the owner's 암기장 link). The window hides their buttons, so nothing calls a deleted
module. ``LECTUREHUB_EDITION=public`` (or ``full``) overrides the file for tests; an installed (frozen) public
build ignores the override and stays public.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

MARKER = Path(__file__).with_name("edition.json")
PUBLIC_OFF = ("mai", "qa", "gsr", "gsr_prefs", "captions", "etl_board")  # features the public edition does not have (gsr_prefs: saved companions + room order; etl_board: the LearningX board is read through its unpublished API, owner only — user 2026-11-21)


def _marked_public() -> bool:
    try:
        return json.loads(MARKER.read_text(encoding="utf-8")).get("edition") == "public"
    except (OSError, ValueError, AttributeError):
        return False


def name() -> str:
    # An installed public build can never become "full": its modules of the removed features are not there, so
    # with LECTUREHUB_EDITION=full set on a user's PC the window would call modules that do not exist.
    marked = _marked_public()
    if marked and getattr(sys, "frozen", False):
        return "public"
    forced = (os.environ.get("LECTUREHUB_EDITION") or "").strip().lower()
    if forced in ("public", "full"):
        return forced
    return "public" if marked else "full"


def has(feature: str) -> bool:
    return not (name() == "public" and feature in PUBLIC_OFF)


def flags() -> dict[str, bool]:
    return {feature: has(feature) for feature in PUBLIC_OFF}
