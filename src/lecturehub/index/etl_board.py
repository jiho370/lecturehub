"""eTL course boards (the "게시판" tab) — posts the Canvas API does not show (user request 2026-11-20).

A TA put a homework ("… 과제제출, 12월 1일까지") only on the course board, not as an eTL
assignment or announcement, so the deadline sync never saw it. The board is the LearningX board tool inside
eTL (an LTI tool, ``external_tools/<id>``). No browser is needed: with the user's own eTL token Canvas hands out a
one-time launch (``sessionless_launch``); its form, posted back to the same host, gives the board's own API key
(cookie ``xn_api_token``), and the board API lists boards and posts as JSON. Everything stays on the eTL site's host (etl_site.base(); SNU's eTL unless another school's Canvas is set — a
Canvas without the LearningX board simply has no 게시판 tab).

Files attached to those posts (eTL files with their own download link; comments' files are students') are kept
with the post and joined to the 파일 tab's list by ``etl_files.listed_files``, so they are compared and fetched
like any other eTL file.

Read-only GETs (the one POST is the launch form itself). Written: ``_lecturehub\\etl\\board_posts.json`` and
``board_sync.json``. Runs when the user presses [게시판 확인] or once a day while the app is open (``due``) — not
with every deadline sync. A new post (with or without files or dates) is told once by a Windows notification and
stays in the app's banner with its text until 확인함 (``unseen``/``to_tell``/``mark_seen``, ``board_seen.json``). A post that names 과제/시험/퀴즈 and a date becomes a calendar candidate (never confirmed by
itself) unless a confirmed event of that course, kind and day already exists. Its row is source ``etl`` (the
column allows user/etl/transcript) with ref ``lxb:<post>#<date words>`` and an Evidence ``board_quote``.
"""
from __future__ import annotations

import html
import http.cookiejar
import json
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path

from lecturehub.core import etl_site
from lecturehub.core.ids import new_ulid

from . import calendar_events, etl_files, etl_sync

CHECK_S = 86400               # read the boards once a day
TELL_DAYS = 14                # a post older than this is not told as new
POSTS_PER_BOARD = 20          # newest page of each board
MAX_TEXT = 4000
HORIZON_DAYS = 150

Getter = Callable[[str], object]                       # board API path → JSON
Launch = Callable[[int, int, str], Getter]             # (course id, tool id, eTL token) → getter


def _form(page: str) -> tuple[str, dict[str, str]]:
    found = re.search(r"<form[^>]*action=\"([^\"]+)\"[^>]*>(.*?)</form>", page, re.S)
    if not found:
        raise ValueError("게시판 실행 화면을 읽지 못했습니다")
    fields = {}
    for tag in re.findall(r"<input[^>]*>", found[2]):
        name, value = re.search(r"name=\"([^\"]*)\"", tag), re.search(r"value=\"([^\"]*)\"", tag)
        if name:
            fields[html.unescape(name[1])] = html.unescape(value[1]) if value else ""
    return html.unescape(found[1]), fields


def _launch(course_id: int, tool_id: int, token: str) -> Getter:
    """Open the board tool the way the eTL page does, without a browser; every URL must stay on eTL's host."""
    data, _ = etl_sync._http(f"{etl_site.base()}/api/v1/courses/{course_id}/external_tools/sessionless_launch"
                             f"?id={tool_id}", token)
    url = str(data.get("url") or "")
    if not etl_sync._same_host(url):
        raise ValueError("게시판 실행 주소가 eTL 밖을 가리켜 열지 않았습니다")
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    with opener.open(url, timeout=20) as response:
        if not etl_sync._same_host(response.geturl()):
            raise ValueError("게시판 실행 주소가 eTL 밖으로 넘어가 멈췄습니다")
        action, fields = _form(response.read().decode("utf-8", "replace"))
    if not etl_sync._same_host(action):
        raise ValueError("게시판 실행 양식이 eTL 밖을 가리켜 보내지 않았습니다")
    with opener.open(urllib.request.Request(action, data=urllib.parse.urlencode(fields).encode()), timeout=20) as r:
        r.read()
    key = next((c.value for c in jar if c.name == "xn_api_token"), None)
    if not key:
        raise ValueError("게시판이 열쇠를 주지 않았습니다(eTL 화면이 바뀌었을 수 있음)")
    base = f"{etl_site.base()}/learningx/api/v1/learningx_board/courses/{course_id}"

    def get(path: str) -> object:
        request = urllib.request.Request(base + path, headers={"Accept": "application/json",
                                                               "Authorization": f"Bearer {key}"})
        with opener.open(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))

    return get


def board_tool(tabs: list) -> int | None:
    """The course's 게시판 tab → its external tool id (``context_external_tool_85`` → 85)."""
    for tab in tabs:
        found = re.fullmatch(r"context_external_tool_(\d+)", str(tab.get("id") or ""))
        if found and "게시판" in str(tab.get("label") or ""):
            return int(found[1])
    return None


def _kst(stamp: str | None) -> str:
    if not stamp:
        return ""
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(etl_sync.KST).strftime("%Y-%m-%dT%H:%M")


def _who(name: str) -> str:
    """"조교(학번)" and "조교" are the same person; the board shows the first, Canvas either."""
    return re.sub(r"\s*\(.*\)\s*$", "", name or "").strip()


def staff(course_id: int, token: str, fetch_with: etl_sync.Fetch = etl_sync._http) -> set[str] | None:
    """Names of the course's professors and TAs (None when eTL does not list them)."""
    try:
        users = etl_sync._all(fetch_with, f"{etl_site.base()}/api/v1/courses/{course_id}/users"
                              "?enrollment_type[]=teacher&enrollment_type[]=ta&per_page=100", token)
    except Exception:  # noqa: BLE001 - an unlisted roster keeps every post rather than none
        return None
    return {_who(str(u.get("name") or "")) for u in users} - {""} or None


def fetch(root: Path, token: str, fetch_with: etl_sync.Fetch = etl_sync._http, launch: Launch = _launch) -> dict:
    """Newest top-level posts by the professor/TAs (or marked 공지) of every board in this term's courses.

    Students post their submissions as top-level posts too (real board 2026-11-20: 15 midterm write-ups with names
    and student numbers), so a post counts only when its author is on the course's teacher/TA list."""
    courses, unmatched = etl_files._courses(root, token, fetch_with)
    posts, missing, failed = [], [], []
    for course_id, folder in courses:
        tool = board_tool(etl_sync._all(fetch_with, f"{etl_site.base()}/api/v1/courses/{course_id}/tabs", token))
        if tool is None:
            missing.append(folder)
            continue
        teachers = staff(course_id, token, fetch_with)
        try:
            get = launch(course_id, tool, token)
            boards = get("/boards")
        except Exception as exc:  # noqa: BLE001 - one course's board never stops the others
            failed.append(f"{folder}({type(exc).__name__})")
            continue
        for board in boards:
            if not board.get("total_post_count"):
                continue
            listed = get(f"/boards/{board['id']}/posts?page=1&per_page={POSTS_PER_BOARD}")
            for item in listed.get("items", []) if isinstance(listed, dict) else []:
                if item.get("depth") or item.get("is_secret"):  # replies (often classmates' submissions) and secret posts
                    continue
                if teachers is not None and not item.get("is_notice") \
                        and _who(str(item.get("user_name") or "")) not in teachers:
                    continue  # a student's post (a classmate's submission, a question)
                files = []
                if item.get("attachment_count"):  # the post's own files (eTL files); comments' files are students'
                    detail = get(f"/boards/{board['id']}/posts/{item['id']}")
                    updated = _kst(item.get("post_updated_at") or item.get("created_at"))
                    for found in (detail.get("attachments") or []) if isinstance(detail, dict) else []:
                        if found.get("canvas_file_id") and found.get("url"):
                            files.append({"id": int(found["canvas_file_id"]), "name": str(found.get("filename") or ""),
                                          "size": int(found.get("filesize") or 0), "url": str(found["url"]),
                                          "updated": updated})
                posts.append({
                    "files": files,
                    "id": f"lxb:{item['id']}", "course": folder, "board": str(board.get("title") or "").strip(),
                    "title": str(item.get("title") or "").strip(), "author": str(item.get("user_name") or ""),
                    "posted": _kst(item.get("created_at")), "updated": _kst(item.get("updated_at")),
                    "text": etl_files._plain(item.get("content"))[:MAX_TEXT],
                    "notice": bool(item.get("is_notice")), "attachments": int(item.get("attachment_count") or 0),
                    "url": f"{etl_site.base()}/courses/{course_id}/external_tools/{tool}"})
    posts.sort(key=lambda p: p["posted"], reverse=True)
    return {"posts": posts, "courses": len(courses), "unmatched": unmatched, "missing": missing, "failed": failed}


def _dir(root: Path) -> Path:
    return Path(root) / "_lecturehub" / "etl"


def status(root: Path) -> dict:
    return etl_files._read(_dir(root) / "board_sync.json", {})


def posts(root: Path) -> list[dict]:
    return etl_files._read(_dir(root) / "board_posts.json", [])


def due(root: Path, now: float | None = None) -> bool:
    """Once a day while the app is open (first a week, the user's choice; once a day since new posts are told,
    user 2026-11-20: "파일 없어도 무슨 내용인지, 올라왔다고 알려 주었으면")."""
    ok_at = status(root).get("ok_at")
    return not ok_at or (now or time.time()) - ok_at >= CHECK_S


# ------------------------------------------------------------------ new posts are told (with or without files)
def _seen(root: Path) -> dict:
    found = etl_files._read(_dir(root) / "board_seen.json", {})
    return {"seen": list(found.get("seen") or []), "told": list(found.get("told") or [])}


def unseen(root: Path, now: datetime | None = None) -> list[dict]:
    """Posts of the last ``TELL_DAYS`` not marked 확인함, newest first (older ones are not news any more)."""
    since = ((now or datetime.now()) - timedelta(days=TELL_DAYS)).strftime("%Y-%m-%dT%H:%M")
    seen = set(_seen(root)["seen"])
    return [p for p in posts(root) if p["id"] not in seen and p.get("posted", "") >= since]


def mark_seen(root: Path, ids: list[str]) -> int:
    state = _seen(root)
    known = {p["id"] for p in posts(root)}
    state["seen"] = [i for i in dict.fromkeys(state["seen"] + [str(i) for i in ids]) if i in known]
    etl_sync._write(_dir(root) / "board_seen.json", state)
    return len(state["seen"])


def to_tell(root: Path, now: datetime | None = None) -> list[dict]:
    """New posts not yet told by a Windows notification; they are noted as told (the banner stays until 확인함)."""
    state = _seen(root)
    told = set(state["told"])
    new = [p for p in unseen(root, now) if p["id"] not in told]
    if new:
        known = {p["id"] for p in posts(root)}
        state["told"] = [i for i in state["told"] if i in known] + [p["id"] for p in new]
        etl_sync._write(_dir(root) / "board_seen.json", state)
    return new


def refresh(root: Path, token: str, fetch_with: etl_sync.Fetch = etl_sync._http, launch: Launch = _launch) -> dict:
    """Fetch and write the post list; a failure is kept in board_sync.json and raised."""
    previous = status(root)
    try:
        found = fetch(root, token, fetch_with, launch)
    except Exception as exc:
        etl_sync._write(_dir(root) / "board_sync.json", {**previous, "error": f"게시판을 읽지 못했습니다({type(exc).__name__})",
                                                         "error_at": time.time()})
        raise
    etl_sync._write(_dir(root) / "board_posts.json", found["posts"])
    etl_sync._write(_dir(root) / "board_sync.json", {"ok_at": time.time(), "count": len(found["posts"]),
                                                     "missing": found["missing"], "failed": found["failed"]})
    return {"posts": len(found["posts"]), "missing": found["missing"], "failed": found["failed"]}


# ------------------------------------------------------------------ dates in posts → calendar candidates
# a deadline word wins: "중간고사 문제풀기 (11월 16일까지)" is something to hand in, not the exam
_KINDS = [("assignment", re.compile(r"과제|숙제|레포트|리포트|보고서|제출|까지")),
          ("quiz", re.compile(r"퀴즈|쪽지\s*시험")), ("exam", re.compile(r"중간\s*고사|기말\s*고사|시험"))]
_ABS = re.compile(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일")
# "12/1까지", "10/25 (일) 23:59까지", "10.11(일) 까지" (real posts); a weekday or 까지 must follow, so "1.5일"
# or a fraction is not a date
_SLASH = re.compile(r"(?<![\d/.])(\d{1,2})\s*[./]\s*(\d{1,2})(?![\d.])"
                    r"(?=\s*(?:\([월화수목금토일]\)|까지|[월화수목금토일]요일))")


def post_dates(title: str, text: str, posted: date) -> list[tuple[str, date, str]]:
    """(kind, day, wording) for a written post: the kind word may be in the title and the date in the body
    ("… 과제제출" / "… 12월1일까지 제출하세요"). Dates before the post or past the term are dropped."""
    whole = f"{title}\n{text}"
    kind = next((k for k, pattern in _KINDS if pattern.search(whole)), None)
    if kind is None:
        return []
    found, seen = [], set()
    for pattern in (_ABS, _SLASH):
        for m in pattern.finditer(whole):
            month, day_no = int(m.group(1)), int(m.group(2))
            year = posted.year + 1 if month < posted.month - 6 else posted.year
            try:
                day = date(year, month, day_no)
            except ValueError:
                continue
            if posted <= day <= posted + timedelta(days=HORIZON_DAYS) and day not in seen:
                seen.add(day)
                found.append((kind, day, m.group(0)))
    return found


def import_candidates(connection: sqlite3.Connection, items: list[dict], today: date | None = None) -> dict:
    """Calendar candidates (source etl, ref lxb:…) with the post as evidence; nothing is confirmed here.
    A date already past is not offered (an old board post read for the first time)."""
    from .schedule import resolve_course

    today = today or date.today()
    added = 0
    for post in items:
        if not post.get("posted"):
            continue
        try:
            course_id = resolve_course(connection, post["course"])
        except ValueError:
            continue
        for kind, day, wording in post_dates(post["title"], post["text"], date.fromisoformat(post["posted"][:10])):
            if day < today:
                continue
            ref = f"{post['id']}#{wording}"  # "lxb:…" never meets an assignment's "etl:…" ref
            if connection.execute("SELECT 1 FROM CalendarEvent WHERE source='etl' AND source_ref=?",
                                  (ref,)).fetchone():
                continue
            if connection.execute("SELECT 1 FROM CalendarEvent WHERE course_id=? AND kind=? AND date=? "
                                  "AND status='confirmed'", (course_id, kind, day.isoformat())).fetchone():
                continue  # already on the calendar (typed by the user or from eTL)
            event_id = calendar_events.add(
                connection, kind=kind, title=post["title"] or f"{calendar_events.KIND_LABEL[kind]} — eTL 게시판",
                day=day.isoformat(), course_id=course_id, note=f"eTL 게시판 '{post['board']}' 글에서 찾은 후보(확인 필요)",
                source="etl", source_ref=ref, status="candidate", date_text=wording)
            connection.execute(
                "INSERT INTO Evidence(id,subject_type,subject_id,kind,detail,source,created_at) VALUES (?,?,?,?,?,?,?)",
                (new_ulid(), "CalendarEvent", event_id, "board_quote", json.dumps({
                    "post_id": post["id"], "board": post["board"], "posted": post["posted"], "author": post["author"],
                    "quote": post["text"][:600] or post["title"], "url": post["url"]}, ensure_ascii=False),
                 "heuristic", calendar_events._now_ms()))
            added += 1
    return {"candidates_added": added}
