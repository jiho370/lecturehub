"""Is there a newer version? (release review 2026-11-16, user decision: notice only, never an automatic install)

Once a day at most, the app reads the latest release of its public GitHub repository: one request, no account,
nothing about the user or this PC in it (only a generic ``User-Agent`` with the app's own version). When the
release is newer, the window says so and offers to open the download page in the browser. The user downloads
and installs it themselves.

Off when: the user switched it off in settings (``update_check: false``), no repository is named (``REPO`` empty,
or ``LECTUREHUB_UPDATE_REPO`` set to an empty string), the app runs from source (developers update with git), or it
runs from the Microsoft Store package (the Store updates it).
Until the repository is public the request finds nothing, which reads as "no newer version".
A failed check is silent: no network must never look like a problem with the app.
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.request

from . import applog, settings

REPO = "jiho370/lecturehub"  # "owner/name" of the public repository. LECTUREHUB_UPDATE_REPO overrides it; set to an
#                             empty string it switches the check off (tests and the self-check do that)
EVERY_SECONDS = 24 * 3600
TIMEOUT = 6
_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def repo() -> str:
    import os

    value = os.environ.get("LECTUREHUB_UPDATE_REPO", REPO).strip()  # set but empty = off; not set = REPO
    return value if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value) else ""


def parse(text: str) -> tuple[int, int, int] | None:
    found = _VERSION.match((text or "").strip())
    return tuple(int(part) for part in found.groups()) if found else None


def _possible() -> bool:
    """A frozen build that is not the Store package (the Store updates its own apps) and has a repository."""
    from . import package

    return bool(repo()) and bool(getattr(sys, "frozen", False)) and not package.packaged()


def enabled() -> bool:
    return _possible() and settings.get("update_check", True) is not False


def set_enabled(on: bool) -> dict:
    settings.save({"update_check": bool(on)})
    return status()


def status() -> dict:
    """What the window shows without asking the network: the last answer that was stored."""
    from lecturehub import __version__

    seen = settings.get("update_seen") or {}
    latest = parse(seen.get("latest", ""))
    current = parse(__version__)
    available = bool(latest and current and latest > current)
    return {"ok": True, "current": __version__, "possible": _possible(),
            "on": settings.get("update_check", True) is not False,
            "available": available, "latest": seen.get("latest", "") if available else "",
            "url": seen.get("url", "") if available else ""}


def check(*, now: float | None = None, opener=urllib.request.urlopen, force: bool = False) -> dict:
    """Ask GitHub when a day has passed since the last answer (or ``force``); returns ``status()``."""
    from lecturehub import __version__

    from . import package

    if not enabled() and not (force and repo() and not package.packaged()):
        return status()
    now = time.time() if now is None else now
    seen = settings.get("update_seen") or {}
    if not force and now - float(seen.get("at") or 0) < EVERY_SECONDS:
        return status()
    name = repo()
    request = urllib.request.Request(  # noqa: S310 - a fixed https address built from our own repository name
        f"https://api.github.com/repos/{name}/releases/latest",
        headers={"User-Agent": f"LectureHub/{__version__}", "Accept": "application/vnd.github+json"})
    try:
        with opener(request, timeout=TIMEOUT) as response:
            data = json.loads(response.read(200_000).decode("utf-8"))
        tag, page = str(data.get("tag_name") or ""), str(data.get("html_url") or "")
        if parse(tag) is None or not page.startswith(f"https://github.com/{name}/releases/"):
            raise ValueError("unexpected answer")
        settings.save({"update_seen": {"at": now, "latest": tag.lstrip("v"), "url": page}})
    except Exception:  # noqa: BLE001 - offline, rate limit, a changed answer: try again tomorrow, say nothing
        applog.LOG.info("새 버전 확인을 건너뜀(네트워크 또는 응답 문제)")
        settings.save({"update_seen": {**seen, "at": now}})
    return status()
