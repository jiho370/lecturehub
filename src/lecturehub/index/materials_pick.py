"""S3: pick this class's material and page by rules, with a reason (§0.6 S3, D-056).

Order: the user's own pick for this class > a file added within 24 h before the
class > continue after the pages covered last time (강의자료_매칭 seeds, user
picks) > the next material in teaching order. The syllabus rule needs Syllabus
rows, which do not exist yet; the result says so instead of guessing.
A pick the user changes is recorded as Evidence and feeds the next pick.
Nothing here opens or writes files.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from lecturehub.core.ids import new_ulid

from .schedule import daily_schedule

# From _노트도구\match_materials.py (legacy, verified): not lecture slides.
SKIP = re.compile(r"강의계획|계획서|assmt|소논문|족보|과제|목록|양식")


def _skip(text: str) -> bool:
    """Not lecture slides: the built-in words, plus the user's own ``materials_skip`` words in settings.json."""
    if SKIP.search(text):
        return True
    from lecturehub.core import settings

    extra = settings.get("materials_skip") or []
    return isinstance(extra, list) and any(str(w) and str(w) in text for w in extra)
_WEEK = re.compile(r"(?i)\bweek\s*(\d+)|\bwk\s*(\d+)|(\d+)\s*주차|제\s*(\d+)\s*장|_(\d{2})_|^(\d{1,2})[_ ]")
_DATED = re.compile(r"^(\d{2})(\d{2})(\d{2})\s")  # "091526 …" = 2026-09-15 (MMDDYY naming)
NEW_UPLOAD_WINDOW = timedelta(hours=24)
BULK_WINDOW_MS = 120_000


@dataclass
class Pick:
    material_id: str
    title: str
    path: str
    kind: str
    page: int
    page_count: int | None
    rule: str        # user | matched | new_upload | continue | next_week | latest
    reason: str
    confidence: str  # the user's own pick → exact; rules → inferred

    def as_dict(self) -> dict:
        return asdict(self)


def week_of(title: str) -> int | None:
    """Week/chapter number in a file name (Week 5, Wk 2-1, 3주차, 제01장, _05_, leading 1_)."""
    match = _WEEK.search(title or "")
    return int(next(g for g in match.groups() if g is not None)) if match else None


def order_of(title: str) -> int | None:
    """Teaching order: a MMDDYY date prefix when the course names files that way, else the week number."""
    dated = _DATED.match(title or "")
    if dated:
        mm, dd, yy = dated.groups()
        return int(f"20{yy}{mm}{dd}")
    return week_of(title)


# path -> ((mtime_ns, size), page count): a pick opens each PDF once, not on every call (it took ~60 s
# for a full course folder). A file that changes (new mtime or size) is read again.
_PAGE_CACHE: dict[str, tuple[tuple[int, int], int | None]] = {}


def _read_page_count(path: str) -> int | None:
    try:
        from lecturehub.materials.pdfdoc import PdfDoc
    except ImportError:
        return None
    try:
        with PdfDoc(path) as document:
            return document.page_count
    except Exception:  # noqa: BLE001 - unreadable file: page count unknown
        return None


def pdf_page_count(path: str) -> int | None:
    """Pages of a PDF read from the file itself (Material.page_count is 0 when never counted).
    Cached by (path, mtime, size)."""
    if not str(path).lower().endswith(".pdf"):
        return None
    try:
        stat = os.stat(path)
    except OSError:
        return None
    key, stamp = str(path), (stat.st_mtime_ns, stat.st_size)
    hit = _PAGE_CACHE.get(key)
    if hit is not None and hit[0] == stamp:
        return hit[1]
    count = _read_page_count(path)
    _PAGE_CACHE[key] = (stamp, count)
    return count


def _inside(folder: str | None, path: str) -> tuple[str, ...]:
    """A material's path below its course folder ("강의자료", "Week 5.pdf"); the file name alone when unknown."""
    parts = Path(path).parts
    return parts[parts.index(folder) + 1:] if folder in parts[:-1] else parts[-1:]


def _materials(connection, course_id: str) -> list[dict]:
    """Class materials: unsorted files at the top of the course folder and those under 강의자료·실습자료.
    Assignments, references, textbooks and notes (the other folders) are never a class's slides."""
    from .organize import PICKABLE

    folder = connection.execute("SELECT folder_name FROM Course WHERE id = ?", (course_id,)).fetchone()
    rows = connection.execute(
        "SELECT id, title, path, kind, page_count, etl_uploaded_at FROM Material WHERE course_id = ? ORDER BY title",
        (course_id,)).fetchall()
    items = []
    for r in rows:
        title = r[1] or Path(r[2]).stem
        inside = _inside(folder[0] if folder else None, r[2])
        if _skip(title) or (len(inside) > 1 and (inside[0] not in PICKABLE or _skip("/".join(inside[1:-1])))):
            continue
        items.append({"id": r[0], "title": title, "path": r[2], "kind": r[3], "page_count": r[4] or None,
                      "uploaded_ms": r[5], "week": order_of(title),
                      "category": inside[0] if len(inside) > 1 else None})
    return sorted(items, key=lambda m: (m["week"] is None, m["week"] or 0, m["title"]))


def _added_ms(material: dict) -> int | None:
    """eTL upload time when known, else the file's creation time in the course folder."""
    if material["uploaded_ms"]:
        return int(material["uploaded_ms"])
    try:
        stat = Path(material["path"]).stat()
    except OSError:
        return None
    return int(getattr(stat, "st_birthtime", stat.st_ctime) * 1000)


def _bulk_added(connection) -> set[str]:
    """Files that arrived in one batch (3+ files within 2 minutes across the library, e.g. a whole-semester
    download). Their creation time says nothing about when the professor uploaded them."""
    stamps = []
    for material_id, path, uploaded in connection.execute("SELECT id, path, etl_uploaded_at FROM Material"):
        if uploaded:
            continue
        added = _added_ms({"uploaded_ms": None, "path": path})
        if added is not None:
            stamps.append((added, material_id))
    stamps.sort()
    bulk: set[str] = set()
    for added, _ in stamps:
        group = [mid for t, mid in stamps if abs(t - added) <= BULK_WINDOW_MS]
        if len(group) >= 3:
            bulk.update(group)
    return bulk


def _user_choices(connection, course_id: str) -> list[dict]:
    rows = connection.execute(
        "SELECT detail, created_at FROM Evidence WHERE subject_type='Course' AND subject_id=? AND kind='material_choice' "
        "AND source='user' ORDER BY created_at, id", (course_id,)).fetchall()
    return [{**json.loads(r[0]), "at": r[1]} for r in rows]


def record_choice(connection: sqlite3.Connection, course_id: str, day: str, material_id: str, page: int) -> None:
    """The user changed the pick for this class; later picks continue from it. Caller commits."""
    known = connection.execute("SELECT 1 FROM Material WHERE id = ? AND course_id = ?",
                               (material_id, course_id)).fetchone()
    if not known:
        raise ValueError("이 과목의 자료가 아닙니다.")
    connection.execute(
        "INSERT INTO Evidence(id,subject_type,subject_id,kind,detail,source,created_at) VALUES (?,?,?,?,?,?,?)",
        (new_ulid(), "Course", course_id, "material_choice",
         json.dumps({"date": day, "material_id": material_id, "page": int(page)}, ensure_ascii=False), "user",
         time.time_ns() // 1_000_000))


def _covered(connection, course_id: str, before: str) -> list[dict]:
    """Earlier lectures' pages: matching seeds (pages covered) and the user's own picks (a start page only)."""
    covered = [{"date": r[0], "material_id": r[1], "page_to": r[2], "user": False} for r in connection.execute(
        "SELECT l.date, mm.material_id, mm.page_to FROM MaterialMatch mm JOIN Lecture l ON l.id = mm.lecture_id "
        "WHERE l.course_id = ? AND l.date < ?", (course_id, before))]
    covered += [{"date": c["date"], "material_id": c["material_id"], "page_to": int(c["page"]), "user": True}
                for c in _user_choices(connection, course_id) if c["date"] < before]
    return covered


def _md(day: str) -> str:
    return f"{int(day[5:7])}/{int(day[8:10])}"


def _clock(ms: int) -> str:
    moment = datetime.fromtimestamp(ms / 1000, UTC).astimezone()
    return f"{moment.month}/{moment.day} {moment:%H:%M}"


def pick(connection: sqlite3.Connection, course_id: str, day: date, class_start_ms: int | None,
         count_pages=pdf_page_count, lecture_id: str | None = None) -> dict:
    """{pick, alternatives, notes} for the class of ``course_id`` on ``day``.

    ``class_start_ms`` is the class start as epoch ms (for the 24 h new-file rule).
    """
    materials = _materials(connection, course_id)
    for m in materials:
        if not m["page_count"]:
            m["page_count"] = count_pages(m["path"])
    by_id = {m["id"]: m for m in materials}
    covered = _covered(connection, course_id, day.isoformat())
    seen = {c["material_id"] for c in covered}
    bulk = _bulk_added(connection)

    def make(m: dict, page: int, rule: str, reason: str, confidence: str = "inferred") -> Pick:
        count = m["page_count"]
        page = max(1, min(page, count)) if count else max(1, page)
        return Pick(m["id"], m["title"], m["path"], m["kind"], page, count, rule, reason, confidence)

    def after(order: int | None) -> dict | None:
        later = [x for x in materials if x["week"] is not None and x["week"] > (order or 0) and x["id"] not in seen]
        return later[0] if later else None

    def choose() -> Pick | None:
        mine = [c for c in _user_choices(connection, course_id) if c["date"] == day.isoformat()]
        if mine and mine[-1]["material_id"] in by_id:
            return make(by_id[mine[-1]["material_id"]], int(mine[-1]["page"]), "user", "내가 고른 자료", "exact")
        if lecture_id:  # an existing lecture: the pages matched to it (강의자료_매칭 seeds) come first
            own = [r for r in connection.execute(
                "SELECT material_id, page_from, page_to, decided_by FROM MaterialMatch WHERE lecture_id = ?",
                (lecture_id,)) if r[0] in by_id]
            if own:
                material_id, page_from, page_to, decided_by = min(
                    own, key=lambda r: (by_id[r[0]]["week"] or 0, r[1]))
                guess = "" if decided_by == "user" else " · 추정"
                return make(by_id[material_id], page_from, "matched",
                            f"이 회차에 연결된 자료 {page_from}–{page_to}쪽{guess}",
                            "exact" if decided_by == "user" else "inferred")
        past =max((by_id[c["material_id"]]["week"] or 0 for c in covered if c["material_id"] in by_id), default=0)
        if class_start_ms is not None:
            since = class_start_ms - NEW_UPLOAD_WINDOW.total_seconds() * 1000
            fresh = [(m, added) for m in materials if m["id"] not in seen and m["id"] not in bulk
                     and (m["week"] is None or m["week"] > past)
                     and (added := _added_ms(m)) is not None and since <= added <= class_start_ms]
            if fresh:
                m, added = min(fresh, key=lambda item: (item[0]["week"] is None, item[0]["week"] or 0,
                                                        item[0]["title"]))
                source = "eTL에 올라온" if m["uploaded_ms"] else "과목 폴더에 새로 들어온"
                return make(m, 1, "new_upload", f"수업 전 24시간 안에 {source} 자료({_clock(added)})")
        usable = [c for c in covered if c["material_id"] in by_id]
        if usable:
            last_day = max(c["date"] for c in usable)
            last = [c for c in usable if c["date"] == last_day]
            seeds = [c for c in last if not c["user"]] or last
            best = max(seeds, key=lambda c: (by_id[c["material_id"]]["week"] or 0, c["page_to"]))
            m = by_id[best["material_id"]]
            if best["user"]:
                return make(m, best["page_to"], "continue",
                            f"지난 시간({_md(last_day)})에 직접 고른 자료 · {best['page_to']}쪽부터 봄")
            if m["page_count"] and best["page_to"] >= m["page_count"]:
                nxt = after(m["week"])
                if nxt:
                    return make(nxt, 1, "next_week", f"지난 시간({_md(last_day)}) {m['title']} 끝까지 봄 → 다음 자료")
                return None
            return make(m, best["page_to"] + 1, "continue", f"지난 시간({_md(last_day)}) {best['page_to']}쪽까지")
        # No record for this course yet: the latest file received on its own before class, else the first in order.
        before = [(m, added) for m in materials if m["id"] not in bulk and (added := _added_ms(m)) is not None
                  and (class_start_ms is None or added <= class_start_ms)]
        if before:
            m, added = max(before, key=lambda item: item[1])
            return make(m, 1, "latest", f"본 기록이 아직 없어 가장 최근에 받은 자료({_clock(added)})")
        nxt = after(None) or next(iter(materials), None)
        return make(nxt, 1, "next_week", "본 기록이 아직 없어 순서상 첫 자료") if nxt else None

    result = choose()
    notes = ["계획서 주차 정보(Syllabus)가 아직 없어 계획서 기준 규칙은 쓰지 않았습니다"]
    alternatives = [{"material_id": m["id"], "title": m["title"], "path": m["path"], "kind": m["kind"],
                     "page_count": m["page_count"], "week": m["week"], "category": m["category"]} for m in materials]
    return {"pick": result.as_dict() if result else None, "alternatives": alternatives, "notes": notes}


def class_for_now(connection: sqlite3.Connection, now: datetime, horizon_days: int = 7) -> dict | None:
    """The current class, else the next one (within ``horizon_days``), from the timetable with exceptions."""
    at_ms = ((now.hour * 60 + now.minute) * 60 + now.second) * 1000
    for offset in range(horizon_days + 1):
        day = now.date() + timedelta(days=offset)
        for c in daily_schedule(connection, day):
            if offset == 0 and c.end_ms <= at_ms:
                continue
            start = datetime.combine(day, datetime.min.time(), tzinfo=now.tzinfo) + timedelta(milliseconds=c.start_ms)
            return {"course_id": c.course_id, "course": c.course, "date": day.isoformat(), "start_ms": c.start_ms,
                    "end_ms": c.end_ms, "start_epoch_ms": int(start.timestamp() * 1000),
                    "current": offset == 0 and c.start_ms <= at_ms < c.end_ms}
    return None
