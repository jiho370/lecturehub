"""eTL news told by a Windows notification (owner 2026-11-25, D-077). No AI, nothing sent anywhere.

What is told, each thing once:
- ``assignment_new``  an eTL assignment that was not in the list before (and is not handed in or past due),
- ``due_changed``     an assignment whose due date or time changed (not when it is handed in or now past due),
- ``files_new``       course files that appeared in eTL's file list (or a board post's files) and are not in the
                      course folder yet — one line per course,
- ``submit_check``    after a submission is first seen: its files as eTL lists them look wrong (a file of 0 bytes,
                      or an extension the assignment does not allow). Silent when they look fine.

It only compares the files the eTL sync already wrote (``etl\\assignments.json``, ``files.json``/``board_posts.json``)
with what it saw last time (``etl\\news_seen.json``). Nothing is told before a first successful fetch of the
assignment list with submission states (``sync.json`` ok_at and ``with_submission``); then everything there is taken
as known and nothing is told (no flood of old assignments on the first connection, nor when eTL was not connected or
the first fetch failed: integration review). A course seen for the first time (newly matched, or its file list
newly open) is taken as known the same way, on its own. Items stay in ``news.json`` for the app's banner until 확인함.
One collect at a time (``_LOCK``: start-up, the 10-minute loop and [지금 가져오기] may overlap); ``news.json`` is
written before ``news_seen.json`` so a crash between them tells something twice rather than never; a state file of
the wrong shape is read as far as it makes sense and rewritten, instead of stopping the notifications for good.

The first three each have a switch in settings › 기본 › 알림 (``etl_notify`` in settings.json, all on by default).
A switched-off kind is noted as seen without being told, so turning it on later does not bring old news. The caller
(``ui.app.Api``) does not run this while a recording runs, like the other notices: what came in meanwhile is told
after the recording.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from datetime import datetime
from pathlib import Path

from . import etl_submissions

_LOCK = threading.Lock()

SETTING = "etl_notify"
SWITCHES = {"assignment_new": "새 eTL 과제", "due_changed": "eTL 과제 마감이 바뀜", "files_new": "eTL에 새 강의자료"}
LABELS = {**SWITCHES, "submit_check": "제출한 파일 확인"}
KEEP_DAYS = 21       # news older than this leaves the banner
MAX_NEWS = 300


def switches() -> dict[str, bool]:
    from lecturehub.core import settings

    saved = settings.get(SETTING)
    saved = saved if isinstance(saved, dict) else {}
    return {kind: saved.get(kind) is not False for kind in SWITCHES}


def set_switch(kind: str, on: bool) -> dict[str, bool]:
    from lecturehub.core import settings

    if kind not in SWITCHES:
        raise ValueError("알 수 없는 알림 종류입니다.")
    settings.save({SETTING: {**switches(), kind: bool(on)}})
    return switches()


def _dir(root: Path) -> Path:
    return Path(root) / "_lecturehub" / "etl"


def _read(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, RecursionError):
        return default


def _write(path: Path, data) -> None:
    from lecturehub.core.fileio import atomic_write_json

    atomic_write_json(path, data)


def _key(*parts) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:16]


def _handed_in(item: dict) -> str | None:
    """"<id>@<submitted_at>" for a submission eTL lists as handed in; a new attempt gets a new mark."""
    if etl_submissions.state(item.get("submission")) != "submitted":  # "graded" has no submission record
        return None
    return f"{item['id']}@{(item.get('submission') or {}).get('submitted_at') or '?'}"


def _files(root: Path) -> list[dict]:
    from . import etl_files

    return [f for f in etl_files.listed_files(root) if isinstance(f, dict) and "id" in f]


SEEN_VERSION = 2


def ready(root: Path) -> bool:
    """A successful fetch of the assignment list with submission states has happened (``etl_sync.sync`` writes
    ``with_submission`` next to ok_at). Before that nothing is compared, so no baseline is taken from an empty or an
    old-format list."""
    data = _read(_dir(root) / "sync.json", {})
    stamp = data.get("ok_at") if isinstance(data, dict) else None
    return isinstance(stamp, (int, float)) and not isinstance(stamp, bool) and data.get("with_submission") is True


def _strings(value) -> set[str]:
    return {str(x) for x in value if isinstance(x, (str, int)) and not isinstance(x, bool)} if isinstance(value, list) \
        else set()


def _seen(path: Path) -> dict | None:
    """The last look, each field of the right type (a field of the wrong shape counts as empty); None when there is
    no usable record yet (absent, not an object, or from before ``SEEN_VERSION``)."""
    raw = _read(path, None)
    if not isinstance(raw, dict) or raw.get("version") != SEEN_VERSION:
        return None
    due = raw.get("assignments")
    return {"assignments": {str(k): str(v) for k, v in due.items() if isinstance(v, (str, int))} if isinstance(due, dict)
            else {},
            "files": _strings(raw.get("files")), "handed_in": _strings(raw.get("handed_in")),
            "courses": _strings(raw.get("courses")), "file_courses": _strings(raw.get("file_courses"))}


def collect(root: Path, now: datetime | None = None, *, look_in: list[Path] | None = None) -> list[dict]:
    """New news since the last call (also added to ``news.json``). [] before the first good fetch and the first
    time after it (all taken as known). One call at a time."""
    with _LOCK:
        return _collect(Path(root), now, look_in)


def _collect(root: Path, now: datetime | None, look_in: list[Path] | None) -> list[dict]:
    if not ready(root):
        return []
    now = now or datetime.now(etl_submissions.KST)
    stamp = now.astimezone(etl_submissions.KST).strftime("%Y-%m-%dT%H:%M")
    assignments = etl_submissions.items(root)
    files = _files(root)
    current_due = {str(i["id"]): str(i.get("due") or "") for i in assignments}
    current_files = {str(f["id"]) for f in files}
    marks = {mark for i in assignments if (mark := _handed_in(i))}
    courses = {str(i.get("course") or "") for i in assignments}
    file_courses = {str(f.get("course") or "") for f in files}
    path = _dir(root) / "news_seen.json"
    seen = _seen(path)
    if seen is None:  # first look after a good fetch: what is there now is known; nothing is told
        _write(path, {"version": SEEN_VERSION, "assignments": current_due, "files": sorted(current_files),
                      "handed_in": sorted(marks), "courses": sorted(courses), "file_courses": sorted(file_courses)})
        return []
    on = switches()
    news: list[dict] = []
    old_due = seen["assignments"]
    handed = seen["handed_in"]
    for item in assignments:
        if str(item.get("course") or "") not in seen["courses"]:
            continue  # a course seen for the first time (newly matched): its list is taken as known, silently
        ident, due = str(item["id"]), str(item.get("due") or "")
        found = etl_submissions.state(item.get("submission"))
        done = found in etl_submissions.DONE
        base = {"course": item.get("course"), "title": item.get("title"), "due": due, "url": item.get("url"),
                "assignment_id": ident}
        if ident not in old_due:
            if on["assignment_new"] and not done and due >= stamp:
                news.append({**base, "kind": "assignment_new", "key": _key("new", ident)})
        elif old_due[ident] != due and on["due_changed"] and not done and due >= stamp:
            news.append({**base, "kind": "due_changed", "before": old_due[ident], "key": _key("due", ident, due)})
        mark = _handed_in(item)
        if mark and mark not in handed:
            problems = etl_submissions.check(item)
            if problems:
                news.append({**base, "kind": "submit_check", "problems": problems, "key": _key("check", mark)})
    fresh = [f for f in files if str(f["id"]) not in seen["files"] and str(f.get("course") or "") in seen["file_courses"]]
    if fresh and on["files_new"]:
        from . import etl_files

        try:  # only what the course folder does not have yet (the user may have fetched it already)
            here = {r["id"]: r["state"] for r in etl_files.compare(root, look_in=look_in)}
        except Exception:  # noqa: BLE001 - when the folders cannot be read, every new file counts as new
            here = {}
        by_course: dict[str, list[dict]] = {}
        for item in fresh:
            if here.get(item["id"]) in ("have", "kept"):
                continue
            by_course.setdefault(str(item.get("course") or ""), []).append(item)
        for course, rows in by_course.items():
            ids = sorted(str(r["id"]) for r in rows)
            news.append({"kind": "files_new", "course": course, "names": [str(r.get("name") or "") for r in rows][:20],
                         "count": len(rows), "key": _key("files", course, *ids)})
    if news:  # first the news, then the record that they were seen: a crash in between repeats, never loses
        found_at = time.time()
        for item in news:
            item.update(label=LABELS[item["kind"]], found_at=found_at, seen=False)
        stored = inbox(root)
        known = {n.get("key") for n in stored}
        news = [n for n in news if n["key"] not in known]
        cutoff = found_at - KEEP_DAYS * 86400
        kept = [n for n in stored if n["found_at"] >= cutoff]
        _write(_dir(root) / "news.json", (kept + news)[-MAX_NEWS:])
    # what was seen stays seen (a course whose list is closed for a while must not bring its items back as new)
    _write(path, {"version": SEEN_VERSION, "assignments": {**old_due, **current_due},
                  "files": sorted(seen["files"] | current_files), "handed_in": sorted(handed | marks),
                  "courses": sorted(seen["courses"] | courses), "file_courses": sorted(seen["file_courses"] | file_courses)})
    return news


def inbox(root: Path) -> list[dict]:
    """The stored news, each with a text ``key`` and a numeric ``found_at`` (a broken entry is left out or dated 0,
    never an error that would stop the notifications)."""
    items = _read(_dir(root) / "news.json", [])
    out = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("key"), str) or not item["key"]:
            continue
        stamp = item.get("found_at")
        if not isinstance(stamp, (int, float)) or isinstance(stamp, bool):
            item = {**item, "found_at": 0}
        out.append(item)
    return out


def unseen(root: Path, now: float | None = None) -> list[dict]:
    """Not marked 확인함 and not older than ``KEEP_DAYS``, newest first (the banner)."""
    cutoff = (now or time.time()) - KEEP_DAYS * 86400
    rows = [i for i in inbox(root) if not i.get("seen") and (i.get("found_at") or 0) >= cutoff]
    return [{**i, "line": line(i)} for i in sorted(rows, key=lambda i: -(i.get("found_at") or 0))]


def mark_seen(root: Path, keys: list[str]) -> int:
    with _LOCK:  # not between a collect's read and write of news.json
        items, changed = inbox(root), 0
        wanted = {str(k) for k in keys}
        for item in items:
            if item["key"] in wanted and not item.get("seen"):
                item["seen"], changed = True, changed + 1
        if changed:
            _write(_dir(root) / "news.json", items)
        return changed


def line(item: dict) -> str:
    """One line of a Windows notification or the banner."""
    course = f"{item.get('course')} · " if item.get("course") else ""
    when = lambda value: f"{value[5:7]}/{value[8:10]} {value[11:16]}".strip() if value else "?"
    kind = item.get("kind")
    if kind == "assignment_new":
        return f"{course}새 과제: {item.get('title')} ({when(item.get('due'))} 마감)"
    if kind == "due_changed":
        return f"{course}마감 바뀜: {item.get('title')} {when(item.get('before'))} → {when(item.get('due'))}"
    if kind == "files_new":
        names = ", ".join(item.get("names") or [])
        return f"{course}새 자료 {item.get('count', 0)}개: {names[:80]}"
    if kind == "submit_check":
        return f"{course}제출 확인: {item.get('title')} — {'; '.join(item.get('problems') or [])[:120]}"
    return f"{course}{item.get('title') or ''}"
