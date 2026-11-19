"""TT-001: the semester timetable (시간표.csv) and term dates (_lecturehub/term.json), edited from the app.

시간표.csv stays the one source (ingest, recorder, eTL matching all read it). Saving keeps the previous file in
_lecturehub/backups and only adds course folders, never renames or removes one. Holidays are not stored here: a
whole-day closure on the calendar already cancels that day's classes (index/schedule.py).
"""
from __future__ import annotations

import csv
import io
import json
import re
from datetime import date, datetime
from pathlib import Path

from lecturehub.core.textio import read_text_any

DAYS = "월화수목금토일"
FILE = "시간표.csv"
HEADER = ["폴더", "과목", "요일", "시작", "종료"]
_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_TIME = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")


class TimetableError(ValueError):
    """The entered timetable or term is not usable; the message is shown to the user."""


def folder_name(name: str) -> str:
    cleaned = _BAD.sub(" ", name or "").strip().rstrip(". ")
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned or cleaned in {".", ".."} or cleaned.startswith("_") or len(cleaned) > 60:
        raise TimetableError(f"과목 이름을 쓸 수 없습니다: {name!r}")
    return cleaned


def _minutes(text: str) -> int:
    hour, minute = text.split(":")
    return int(hour) * 60 + int(minute)


def clean(courses: list[dict]) -> list[dict]:
    """Validate ``[{name, folder?, slots: [{day, start, end}]}]``; reject duplicates and overlapping classes."""
    out, seen, taken = [], set(), []
    for row in courses or []:
        name = str(row.get("name", "")).strip()
        folder = folder_name(str(row.get("folder") or name))
        if folder in seen:
            raise TimetableError(f"같은 과목이 두 번 있습니다: {folder}")
        seen.add(folder)
        slots = []
        for slot in row.get("slots") or []:
            day, start, end = (str(slot.get(k, "")).strip() for k in ("day", "start", "end"))
            if day not in tuple(DAYS) or not _TIME.match(start) or not _TIME.match(end) or _minutes(start) >= _minutes(end):
                raise TimetableError(f"{folder}: 시간표 칸이 올바르지 않습니다(요일 {day!r}, {start!r}~{end!r})")
            start, end = start.zfill(5), end.zfill(5)
            for other, oday, ostart, oend in taken:
                if oday == day and _minutes(start) < _minutes(oend) and _minutes(ostart) < _minutes(end):
                    raise TimetableError(f"{day} {start}~{end}: '{folder}'·'{other}' 수업 시간이 겹칩니다.")
            taken.append((folder, day, start, end))
            slots.append({"day": day, "start": start, "end": end})
        out.append({"name": name or folder, "folder": folder, "slots": slots})
    return out


def to_csv(courses: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(HEADER)
    for course in courses:
        for slot in course["slots"]:
            writer.writerow([course["folder"], course["name"], slot["day"], slot["start"], slot["end"]])
    return buf.getvalue()


def path(root: Path) -> Path:
    return Path(root) / FILE


def read(root: Path) -> list[dict]:
    """Courses in file order, slots grouped; unreadable rows are skipped (the editor shows what it could read)."""
    file = path(root)
    if not file.is_file():
        return []
    courses: dict[str, dict] = {}
    for row in csv.DictReader(io.StringIO(read_text_any(file))):
        folder = (row.get("폴더") or "").strip()
        if not folder:
            continue
        course = courses.setdefault(folder, {"name": (row.get("과목") or folder).strip(), "folder": folder, "slots": []})
        day, start, end = ((row.get(k) or "").strip() for k in ("요일", "시작", "종료"))
        if day in tuple(DAYS) and _TIME.match(start) and _TIME.match(end):
            course["slots"].append({"day": day, "start": start.zfill(5), "end": end.zfill(5)})
    return list(courses.values())


def write(root: Path, courses: list[dict], *, now: datetime | None = None) -> dict:
    """Save the timetable: back up the old file, add missing course folders. Returns what changed."""
    rows = clean(courses)
    loose = [c["folder"] for c in rows if not c["slots"]]
    if loose:  # ingest needs a weekday and time on every row
        raise TimetableError(f"{', '.join(loose)}: 수업 시간을 하나 이상 넣으세요. 정해진 시간이 없는 강의는 '수업 외'로 추가하세요.")
    root, file = Path(root), path(root)
    backup = None
    if file.is_file():
        stamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
        backup = root / "_lecturehub" / "backups" / f"시간표_{stamp}.csv"
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_bytes(file.read_bytes())
    made = []
    for course in rows:
        folder = root / course["folder"]
        if not folder.exists():
            folder.mkdir(parents=True)
            made.append(course["folder"])
    file.write_text(to_csv(rows), encoding="utf-8-sig", newline="")
    return {"courses": rows, "backup": str(backup) if backup else "", "new_folders": made}


# ------------------------------------------------------------------ term dates
def _term_file(root: Path) -> Path:
    return Path(root) / "_lecturehub" / "term.json"


def load_term(root: Path) -> dict:
    try:
        data = json.loads(_term_file(root).read_text(encoding="utf-8-sig"))
        start, end = date.fromisoformat(data["start"]), date.fromisoformat(data["end"])
        return {"start": start.isoformat(), "end": end.isoformat()} if start <= end else {}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def save_term(root: Path, start: str, end: str) -> dict:
    """Store 개강·종강. Both empty clears it (every week counts, as before)."""
    file = _term_file(root)
    if not (start or "").strip() and not (end or "").strip():
        if file.exists():
            file.unlink()
        return {}
    try:
        first, last = date.fromisoformat(str(start).strip()), date.fromisoformat(str(end).strip())
    except ValueError as exc:
        raise TimetableError("개강일과 종강일은 2026-09-01 형식으로 적어 주세요.") from exc
    if first > last or (last - first).days > 200:
        raise TimetableError("개강일이 종강일보다 앞이어야 하고, 한 학기는 200일을 넘지 않아야 합니다.")
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps({"start": first.isoformat(), "end": last.isoformat()}, ensure_ascii=False), encoding="utf-8")
    return {"start": first.isoformat(), "end": last.isoformat()}


def in_term(root: Path, day: date) -> bool:
    term = load_term(root)
    return not term or term["start"] <= day.isoformat() <= term["end"]


# ------------------------------------------------------------------ order
def order_key(name: str, slots, now: datetime) -> tuple:
    """Today's classes first by start time, then the rest by their first class in the week, then by name.

    ``slots`` holds (day, start, end) tuples or {day, start, end} dicts.
    """
    pairs = [(s["day"], s["start"]) if isinstance(s, dict) else (s[0], s[1]) for s in slots]
    today = DAYS[now.weekday()]
    todays = sorted(start for day, start in pairs if day == today)
    if todays:
        return (0, 0, todays[0], name)
    week = sorted(((DAYS.index(day) - now.weekday()) % 7, start) for day, start in pairs if day in DAYS)
    return (1, *week[0], name) if week else (2, 0, "", name)
