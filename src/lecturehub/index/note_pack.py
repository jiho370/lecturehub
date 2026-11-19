"""What a note writer needs for one lecture, gathered in one place (user request 2026-10-11).

"정리하는 HTML 만드는 버튼도 여기 있었으면 좋겠고 … 강의자료 바탕으로 전사본으로 강의 정리본." The app
only gathers: which transcripts, which slides, where notes of this course live, which note to take the
style from, and where the new file goes. Writing is the Claude Code session's work (/lecture-note);
the finished file comes back through ``lecture attach-note``.
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from . import lectures, organize
from .scanner import MATERIAL_EXTENSIONS

HTML = (".html", ".htm")
# Notes of one lecture, not the course-wide pages (exam prep, past-exam analysis, summaries) that sit next to them.
NOTE_WORDS = ("노트", "강의정리")
NOT_NOTES = ("대비", "기출", "요약", "시험", "명심")
_DATES = (  # the date styles of this semester's note names, longest first
    (re.compile(r"(?<!\d)(\d{2})\.(\d{1,2})\.(\d{1,2})(?!\d)"), lambda y, m, d: f"{y % 100:02d}.{m}.{d}"),
    (re.compile(r"(?<!\d)(\d{2})-(\d{2})-(\d{2})(?!\d)"), lambda y, m, d: f"{y % 100:02d}-{m:02d}-{d:02d}"),
    (re.compile(r"(?<![\d.])(\d{1,2})\.(\d{1,2})(?![\d.])"), lambda y, m, d: f"{m}.{d}"),
    (re.compile(r"(?<!\d)(0[1-9]|1[0-2])(\d{2})(?!\d)"), lambda y, m, d: f"{m:02d}{d:02d}"),
)


def _is_note(path: Path) -> bool:
    return any(w in path.stem for w in NOTE_WORDS) and not any(w in path.stem for w in NOT_NOTES)


def _named_like(style: Path | None, course: str, date: str, label: str) -> str:
    """This lecture's file name in the same style as the course's latest note: its date written the same way
    (환경공_1008_실습노트 → 환경공_1015_실습노트, 26.10.12_기초어학 → 26.10.14_기초어학). The writer may still change
    a topic word or 강의/실습 to fit. Without a style: <course>_<MMDD>_강의노트."""
    year, month, day = int(date[:4]), int(date[5:7]), int(date[8:10])
    if style is not None:
        for pattern, write in _DATES:
            if pattern.search(style.stem):
                return pattern.sub(write(year, month, day), style.stem, count=1)
    return f"{course}_{month:02d}{day:02d}_강의노트".replace(" ", "_")


def _course_dir(root: Path, course: str) -> Path:
    """The folder a course's files are in. An outside-class item named "<과목> …" shares that course's folder
    ("경제학원론 영강" → 경제학원론), so its notes sit next to the slides they are written from."""
    own = root / course
    if own.is_dir():
        return own
    for folder in sorted((p for p in root.iterdir() if p.is_dir() and not p.name.startswith(("_", "."))),
                         key=lambda p: -len(p.name)):
        if course.startswith(folder.name + " ") and folder.name not in ("녹음본", "전사본"):
            return folder
    return own  # made when the note is written


def pack(connection: sqlite3.Connection, root: Path, lecture_id: str) -> dict:
    root = Path(root)
    detail = lectures.lecture_detail(connection, lecture_id)
    course_dir = _course_dir(root, detail["course"])
    notes_dir = organize.folder_for(course_dir, "정리본") if course_dir.is_dir() else course_dir / "정리본"
    existing = sorted((p for p in notes_dir.glob("*") if p.suffix.lower() in HTML and _is_note(p)),
                      key=lambda p: p.stat().st_mtime) if notes_dir.is_dir() else []
    attached = [n["path"] for n in detail.get("notes", []) if n.get("exists") and str(n["path"]).lower().endswith(HTML)]
    stem = _named_like(existing[-1] if existing else None, course_dir.name, detail["date"], detail["label"])
    target, n = notes_dir / f"{stem}.html", 1
    while target.exists() and str(target) not in attached:
        n += 1
        target = notes_dir / f"{stem}_{n}.html"
    slides = []
    for name in organize.PICKABLE:  # 강의자료 · 실습자료, plus unsorted files at the top of the course folder
        folder = course_dir / name
        if folder.is_dir():
            slides += [str(p) for p in sorted(folder.rglob("*")) if p.suffix.lower() in MATERIAL_EXTENSIONS]
    if course_dir.is_dir():
        slides += [str(p) for p in sorted(course_dir.iterdir()) if p.is_file() and p.suffix.lower() in MATERIAL_EXTENSIONS]
    transcripts = [{"engine": t["label"], "path": t["source_file"], "in_use": bool(t.get("in_use")),
                    "segments": t["segments"], "qa": t.get("qa") or [],
                    "minutes": round((t.get("last_ms") or 0) / 60000)}
                   for t in detail["transcripts"] if t["segments"] and t.get("source_file")]
    return {
        "lecture_id": lecture_id, "course": detail["course"], "date": detail["date"], "label": detail["label"],
        "recordings": [{"path": r["path"], "minutes": round((r.get("duration_ms") or 0) / 60000),
                        "mode": "PC 소리" if "loopback" in str(r["path"]) else "마이크"} for r in detail["recordings"]],
        "transcripts": transcripts,
        "materials_linked": [{"path": m["path"], "title": m.get("title"), "pages": [m.get("page_from"), m.get("page_to")]}
                             for m in detail.get("materials", [])],
        "materials_in_course_folder": slides,
        "notes_folder": str(notes_dir),
        "style_from": str(existing[-1]) if existing else None,  # the newest lecture note of this course (not exam pages)
        "attached_notes": attached,                               # non-empty: revise that file in place
        "target": attached[0] if attached else str(target),
        # statements about the exam picked while writing go here, then `lecturehub quiz cues` (exam screen, P-032)
        "cues_file": str(root / "_lecturehub" / "quiz" / course_dir.name / f"{detail['date']}_{detail['label']}.cues.json".replace(" ", "_")),
        "ready": bool(transcripts),
        "recording_now": _recording_now(root),  # true: do not start (slides are rendered; AGENTS §2.7)
    }


def _recording_now(root: Path) -> bool:
    """A recording session that has its lock and no stop report yet."""
    folder = root / "_lecturehub" / "recordings"
    return folder.is_dir() and any((p / ".recording.lock").exists() and not (p / "stop.json").exists()
                                   for p in folder.iterdir() if p.is_dir())
