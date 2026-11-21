"""Fetch this term's eTL (Canvas) assignment deadlines into ``_lecturehub\\etl\\assignments.json``.

User request 2026-09-29: "eTL 과제 자동으로 가져오게 해줘". Read-only GETs to myetl.snu.ac.kr with the
user's own Canvas personal access token, which the user makes on eTL (계정 → 설정 → 새 액세스 토큰) and
pastes into the app. The token lives in Windows Credential Manager (keyring ``lecturehub``/``etl-canvas``),
never in the course folder or the repository, and is never logged.

Courses are matched by the timetable (시간표.csv 과목 → 폴더): eTL names look like
"2026-2 도시계획론 (001)". The file this writes is what ``calendar_events.import_etl`` already reads
(add / update due date; nothing is deleted). A run at most every ``MIN_INTERVAL_S`` unless forced.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lecturehub.core import etl_site
from lecturehub.core.textio import read_text_any

BASE = etl_site.DEFAULT  # SNU's eTL; the site in use is etl_site.base() (another school's Canvas, 2026-11-20)
KEYRING_SERVICE, KEYRING_NAME = "lecturehub", "etl-canvas"
MIN_INTERVAL_S = 3 * 3600
KST = timezone(timedelta(hours=9))  # Korea has no daylight saving time
_SECTION = re.compile(r"\s*\(\d+\)\s*$")

Fetch = Callable[[str, str], tuple[object, str | None]]  # (url, token) -> (json, next page url)


def load_token() -> str | None:
    """The saved token, only while it belongs to the site in use (a token made for one school never goes to
    another school's address after the user changes it)."""
    try:
        import keyring

        token = keyring.get_password(KEYRING_SERVICE, KEYRING_NAME)
        if token and etl_site.token_fits():
            return token
    except Exception:  # noqa: BLE001, S110 - a broken keyring backend means "not connected"
        pass
    return os.environ.get("LECTUREHUB_ETL_TOKEN") or None


def save_token(token: str | None) -> None:
    import keyring

    if token:
        keyring.set_password(KEYRING_SERVICE, KEYRING_NAME, token.strip())
        etl_site.remember_token_site()
    else:
        try:
            keyring.delete_password(KEYRING_SERVICE, KEYRING_NAME)
        except keyring.errors.PasswordDeleteError:
            pass


def _same_host(url: str) -> bool:
    """https on the eTL site's own host: the only place the eTL token may go (security review 2026-11-05)."""
    from urllib.parse import urlsplit

    target, base = urlsplit(url), urlsplit(etl_site.base())
    return target.scheme == "https" and target.hostname == base.hostname and target.port == base.port


class _KeepTokenHome(urllib.request.HTTPRedirectHandler):
    """urllib copies Authorization onto every redirect; drop it when the redirect leaves BASE's host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and not _same_host(new.full_url):
            for name in [k for k in new.headers if k.lower() == "authorization"]:
                del new.headers[name]
            new.unredirected_hdrs.pop("Authorization", None)
        return new


_OPENER = urllib.request.build_opener(_KeepTokenHome)


def _http(url: str, token: str) -> tuple[object, str | None]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    if _same_host(url):
        request.add_header("Authorization", f"Bearer {token}")
    with _OPENER.open(request, timeout=15) as response:
        body = response.read().decode("utf-8").removeprefix("while(1);")
        links = response.headers.get("Link") or ""
    following = re.search(r'<([^>]+)>;\s*rel="next"', links)
    return json.loads(body), following[1] if following else None


def _all(fetch: Fetch, url: str, token: str) -> list:
    """Every page; a rel="next" link to another host (or not https) is not followed."""
    items: list = []
    while url:
        page, url = fetch(url, token)
        items.extend(page)
        if url and not _same_host(url):
            break
    return items


def timetable(root: Path) -> dict[str, str]:
    """시간표.csv 과목 name → course folder."""
    rows = csv.DictReader(io.StringIO(read_text_any(Path(root) / "시간표.csv")))
    return {row["과목"].strip(): row["폴더"].strip() for row in rows if row.get("과목") and row.get("폴더")}


def term_prefix(root: Path) -> str:
    """"26-2" → "2026-2" (eTL course names start with it)."""
    found = re.search(r"(\d{2,4})-([12])", Path(root).name)
    if not found:
        raise ValueError(f"학기를 폴더 이름에서 알 수 없습니다: {Path(root).name}")
    year = found[1] if len(found[1]) == 4 else "20" + found[1]
    return f"{year}-{found[2]}"


def match_subject(course_name: str, subjects: dict[str, str]) -> str | None:
    """The folder of the timetable subject this course name is, or contains (the longest such subject).
    Other schools' Canvas sites name courses like "[2026-2] 도시계획론-01" or "도시계획론 (Urban Planning)"."""
    if course_name in subjects:
        return subjects[course_name]
    squeezed = "".join(course_name.split())
    inside = [s for s in subjects if len("".join(s.split())) >= 2 and "".join(s.split()) in squeezed]
    return subjects[max(inside, key=len)] if inside else None


def fetch_assignments(root: Path, token: str, fetch: Fetch = _http) -> dict:
    prefix, subjects = term_prefix(root), timetable(root)
    items, unmatched, courses = [], [], 0
    site = etl_site.base()
    for course in _all(fetch, f"{site}/api/v1/courses?enrollment_state=active&per_page=100", token):
        name = str(course.get("name") or "")
        if name.startswith(prefix + " "):
            subject = _SECTION.sub("", name[len(prefix) + 1:]).strip()
            folder = subjects.get(subject)
        elif etl_site.is_default():
            continue  # SNU's eTL: another term's course
        else:  # another school's Canvas names courses its own way: find a timetable subject inside the name
            subject = _SECTION.sub("", name).strip()
            folder = match_subject(subject, subjects)
        if folder is None:
            unmatched.append(subject)
            continue
        courses += 1
        for item in _all(fetch, f"{site}/api/v1/courses/{course['id']}/assignments?per_page=100&order_by=due_at", token):
            if not item.get("published", True) or not item.get("due_at"):
                continue
            due = datetime.fromisoformat(str(item["due_at"])).astimezone(KST)
            items.append({"id": f"etl:{item['id']}", "course": folder, "title": str(item.get("name") or "").strip(),
                          "due": due.strftime("%Y-%m-%dT%H:%M"), "url": item.get("html_url")})
    return {"items": items, "courses": courses, "unmatched": unmatched}


def _state_path(root: Path) -> Path:
    return Path(root) / "_lecturehub" / "etl" / "sync.json"


def status(root: Path) -> dict:
    try:
        state = json.loads(_state_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    return {"connected": load_token() is not None, **state}


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(temp, path)


def sync(root: Path, *, force: bool = False, fetch: Fetch = _http, token: str | None = None) -> dict:
    """Refresh assignments.json. Returns {"state": "synced"|"skipped"|"not_connected"|"error", ...}."""
    token = token or load_token()
    if not token:
        return {"state": "not_connected"}
    previous = status(root)
    if not force and previous.get("ok_at") and time.time() - previous["ok_at"] < MIN_INTERVAL_S:
        return {"state": "skipped", "ok_at": previous["ok_at"]}
    now = time.time()
    try:
        found = fetch_assignments(root, token, fetch)
    except urllib.error.HTTPError as exc:
        error = "eTL 토큰이 만료됐거나 잘못됐습니다. eTL에서 새로 만들어 다시 넣어 주세요" if exc.code == 401 \
            else f"eTL이 {exc.code}로 응답했습니다"
        _write(_state_path(root), {**{k: v for k, v in previous.items() if k != "connected"}, "error": error,
                                   "error_at": now})
        return {"state": "error", "error": error}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        error = f"eTL에 연결하지 못했습니다({type(exc).__name__})"
        _write(_state_path(root), {**{k: v for k, v in previous.items() if k != "connected"}, "error": error,
                                   "error_at": now})
        return {"state": "error", "error": error}
    _write(Path(root) / "_lecturehub" / "etl" / "assignments.json", found["items"])
    _write(_state_path(root), {"ok_at": now, "count": len(found["items"]), "courses": found["courses"],
                               "unmatched": found["unmatched"]})
    return {"state": "synced", "count": len(found["items"]), "unmatched": found["unmatched"]}
