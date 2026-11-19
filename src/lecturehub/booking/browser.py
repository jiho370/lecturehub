"""그스룸 작업을 어느 브라우저 어댑터로 돌릴지 고른다(D-069, 2026-11-05 사용자 결정: 기본은 Aside).

``LECTUREHUB_GSR_BROWSER=chrome``이면 예전 방식인 ``claude -p --chrome``(``claude_cli``), ``aside``이거나 없으면
Aside CLI(``aside_cli``). 단, Aside CLI를 찾지 못하면 chrome으로 돌린다. 부를 때마다 다시 고른다.
결과에 ``browser``와 걸린 초 ``seconds``를 붙인다.
"""
from __future__ import annotations

import os
import time

from . import aside_cli, claude_cli

ENV = "LECTUREHUB_GSR_BROWSER"
CHOICES = ("chrome", "aside")


def choice() -> str:
    value = (os.environ.get(ENV) or "").strip().lower()
    if value == "chrome":
        return "chrome"
    return "aside" if aside_cli.find_aside() else "chrome"


def run(action: dict, *, stop=None, which: str | None = None) -> dict:
    from . import store

    if store.public_mode() and action.get("action") == "book":
        try:
            store.clean_hours(action.get("hours", 3))
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        if action.get("user_confirmed") is not True:
            return {"ok": False, "error": "공개판 예약은 방·시각의 사용자 확인이 필요합니다"}
        action = {**action, "maxHours": 3}

    # companions: typed on the 그스룸 screen for this booking (service.book puts them in the action); never saved or logged
    action = {**action, "companions": list(action.get("companions") or [])}
    from lecturehub.core import edition, settings

    if edition.has("gsr_prefs") and "rooms" not in action:  # the owner's room order (full build; gsr_core.js useRoomPrefs)
        action = {**action, "rooms": settings.gsr_rooms()}
    if edition.has("gsr_prefs") and action.get("action") == "book" and not action["companions"]:
        # full build: a booking without typed names (scheduled booking, chat) uses the list saved on this PC, as the
        # owner's app always did (user 2026-11-19: the owner's build keeps its own 그스룸 features)
        action = {**action, "companions": settings.gsr_companions()}
    name = which or choice()
    module = aside_cli if name == "aside" else claude_cli
    started = time.monotonic()
    result = module.run(action, stop=stop)
    result["browser"] = name
    result["seconds"] = round(time.monotonic() - started, 1)
    return result
