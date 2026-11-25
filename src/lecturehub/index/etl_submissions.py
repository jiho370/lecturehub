"""My own submission state of eTL assignments (owner 2026-11-25, D-077): "제출하면 이제 사라지는 걸로".

``etl_sync.fetch_assignments`` asks Canvas for the assignment list with ``include[]=submission`` (read-only GET, the
user's own token, as before) and keeps, per assignment, only what ``clean`` lets through: the submission's
``workflow_state``, ``submitted_at``, ``missing``, ``late`` and ``excused``, and the name and size of each file the
user handed in (for ``check``). Scores and grades are never read into the app: whatever else Canvas sends with the
submission (score, grade, entered_score, points_deducted, comments, …) is dropped here, before anything is written.

What the screens do with it (``annotate``): a submitted, graded or excused assignment leaves the calendar grid, the
upcoming list and the start screen's "마감·시험" list and is kept under a folded "제출함 N건"; a missing one stays and
is marked 누락 (red); a late submission is flagged. An assignment the file says nothing about (a list written before
this change) is shown as before.

No network and no heavy imports here: the AI app link (``mcp/tools.list_assignments``) reads the same file through
this module in its lean process.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
FLAGS = ("missing", "late", "excused")
DONE = ("submitted", "excused")   # these leave the calendar (kept under "제출함")
# "graded" here is graded WITHOUT any submission record (a paper hand-in, or a 0 entered for nothing handed in): it
# stays with the unsubmitted ones and says so (integration review: it used to hide as 제출함)
LABELS = {"submitted": "제출함", "graded": "채점됨 · 제출 기록 없음", "excused": "면제", "missing": "누락", "open": "미제출"}
MAX_FILES = 20


def _kst(stamp) -> str | None:
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp).astimezone(KST).strftime("%Y-%m-%dT%H:%M")
    except ValueError:
        return None


def clean(raw) -> dict | None:
    """The submission fields the app keeps, from Canvas's submission object; None when there is none.

    A whitelist: nothing about points, scores or grades can get through, whatever Canvas adds later."""
    if not isinstance(raw, dict):
        return None
    out: dict = {}
    state = raw.get("workflow_state")
    if isinstance(state, str) and state:
        out["workflow_state"] = state[:40]
    submitted = _kst(raw.get("submitted_at"))
    if submitted:
        out["submitted_at"] = submitted
    for flag in FLAGS:
        out[flag] = raw.get(flag) is True
    files = []
    for item in raw.get("attachments") or []:
        if not isinstance(item, dict):
            continue
        raw_size = item.get("size")
        try:  # unknown stays unknown (None): only a size eTL states as 0 is a 0-byte file
            size = max(0, int(raw_size)) if raw_size is not None and not isinstance(raw_size, bool) else None
        except (TypeError, ValueError):
            size = None
        files.append({"name": str(item.get("display_name") or item.get("filename") or "")[:200], "size": size})
    if files:
        out["files"] = files[:MAX_FILES]
    return out


def state(submission) -> str:
    """excused | missing | submitted | graded | open, from a ``clean`` submission (None counts as open).

    Missing is checked before anything that could look handed in, and "graded" means graded with no submission
    record (``submitted_at`` empty) — neither may hide an assignment that was not handed in (integration review)."""
    if not isinstance(submission, dict):
        return "open"
    if submission.get("excused") is True:
        return "excused"
    if submission.get("missing") is True:
        return "missing"
    workflow = submission.get("workflow_state")
    if submission.get("submitted_at") or workflow in ("submitted", "pending_review"):
        return "submitted"
    if workflow == "graded":
        return "graded"
    return "open"


def summary(item: dict) -> dict | None:
    """What a screen needs about one stored assignment: state, its label, late, when handed in. None when the
    stored item carries no submission (a list written before 2026-11-25)."""
    submission = item.get("submission") if isinstance(item, dict) else None
    if not isinstance(submission, dict):
        return None
    found = state(submission)
    return {"state": found, "label": LABELS[found], "done": found in DONE, "late": submission.get("late") is True,
            "submitted_at": submission.get("submitted_at")}


def _ext(name: str) -> str:
    return Path(str(name)).suffix.lower().lstrip(".")


def check(item: dict) -> list[str]:
    """What looks wrong with the files of a submission, in the user's words; [] when nothing does (silent).

    Only two cheap checks on what eTL lists: a file of 0 bytes, and a file whose extension the assignment does not
    allow (when the assignment lists ``allowed_extensions``)."""
    submission = item.get("submission") if isinstance(item, dict) else None
    files = submission.get("files") if isinstance(submission, dict) else None
    if not files:
        return []
    allowed = [str(x).lower().lstrip(".") for x in item.get("allowed_extensions") or [] if str(x).strip()]
    problems = []
    for entry in files:
        name = str(entry.get("name") or "이름 없는 파일")
        size = entry.get("size")
        if isinstance(size, int) and not isinstance(size, bool) and size == 0:  # unknown (None) is not 0 bytes
            problems.append(f"0바이트 파일: {name}")
        if allowed and _ext(name) not in allowed:
            problems.append(f"허용되지 않은 형식({', '.join(allowed)}만 허용): {name}")
    return problems


# ------------------------------------------------------------------ the stored list
def _etl(root: Path) -> Path:
    return Path(root) / "_lecturehub" / "etl"


def _read(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, RecursionError):
        return default


def items(root: Path) -> list[dict]:
    data = _read(_etl(root) / "assignments.json", [])
    return [i for i in data if isinstance(i, dict) and i.get("id")] if isinstance(data, list) else []


def fetched_at(root: Path) -> float | None:
    """When the assignment list was last fetched (``sync.json`` ok_at, epoch seconds)."""
    data = _read(_etl(root) / "sync.json", {})
    stamp = data.get("ok_at") if isinstance(data, dict) else None
    return float(stamp) if isinstance(stamp, (int, float)) and not isinstance(stamp, bool) else None


def states(root: Path) -> dict[str, dict]:
    """``source_ref`` of an eTL calendar row ("etl:<id>") → ``summary``, for the assignments the file knows about."""
    out = {}
    for item in items(root):
        found = summary(item)
        if found is not None:
            out[str(item["id"])] = found
    return out


def _due_day(due) -> date | None:
    """The day (Korean time) an eTL due falls on, or None when it cannot be read."""
    try:
        stamp = datetime.fromisoformat(str(due or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return (stamp.replace(tzinfo=KST) if stamp.tzinfo is None else stamp.astimezone(KST)).date()


def lapsed(due, today: date) -> bool:
    """A missing assignment is told only on the day its due passed (user 2026-11-25): from the next day on it is
    not "to do" any more (and may well be a team task a teammate handed in), so it no longer shows as 누락."""
    day = _due_day(due)
    return day is not None and day < today


def annotate(view: dict, root: Path, today: date | None = None) -> dict:
    """The calendar view (``calendar_events.month_view``) with eTL submission states applied, in place.

    Rows of submitted/graded/excused assignments leave ``events`` and ``upcoming`` and are listed once each in
    ``submitted``; every other eTL assignment row gets ``submission`` (missing ones are drawn red as 누락 on the day
    the due passed; from the next day on ``submission["lapsed"]`` is set and they are drawn as ordinary past items).
    Also ``missing`` (missing assignments whose due was today, for the 누락 list) and ``etl_at`` (when eTL was last
    read, for "N시간 전에 받은 정보")."""
    today = today or datetime.now(KST).date()
    known = states(root)
    done: dict[str, dict] = {}
    for key in ("events", "upcoming"):
        kept = []
        for row in view.get(key) or []:
            found = known.get(str(row.get("source_ref") or "")) if row.get("source") == "etl" \
                and row.get("kind") == "assignment" else None
            if found:
                if found["state"] == "missing" and lapsed(row.get("date"), today):
                    found = {**found, "lapsed": True}
                row["submission"] = found
                if found["done"]:
                    done.setdefault(row["id"], row)
                    continue
            kept.append(row)
        view[key] = kept
    view["submitted"] = sorted(done.values(), key=lambda r: (r.get("date") or "", r.get("start_ms") or 0))
    missing = []
    for item in items(root):
        found = summary(item)
        if found and found["state"] == "missing" and not lapsed(item.get("due"), today):
            missing.append({"id": item["id"], "course": item.get("course"), "title": item.get("title"),
                            "due": item.get("due"), "url": item.get("url")})
    view["missing"] = sorted(missing, key=lambda m: str(m.get("due") or ""), reverse=True)
    view["etl_at"] = fetched_at(root)
    return view
