"""Course folders sorted by what the files are for (user request 2026-10-01).

``<과목>\\강의자료 · 실습자료 · 과제 · 정리본 · 참고자료 · 교재``. The folder a file sits in *is* its
category; nothing about it is stored in the database. Files move only on the user's word (a plan
the user reviewed, or one file picked in the app), by rename inside the same course folder, never
over an existing file. Every move is written to MoveLog so a batch can be undone, and the rows that
remember a path (Material, StudyArtifact, IngestItem) follow the file. A transcript's source file
never moves (its path is identity), nor does anything under ``_lecturehub``, 녹음본 or 전사본.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path

from lecturehub.core import settings
from lecturehub.core.events import append
from lecturehub.core.ids import new_ulid
from lecturehub.parsers.dedupe import sha256_file

from .scanner import MATERIAL_EXTENSIONS

CATEGORIES = ("강의자료", "실습자료", "과제", "정리본", "참고자료", "교재")
PICKABLE = ("강의자료", "실습자료")  # what a class's material may be picked from (plus unsorted files)
_STAYS = re.compile(r"(?i)강의\s*계획|계획서|syllabus")  # the syllabus stays at the top of the course folder
_GUESS = (
    ("정리본", re.compile(r"(?i)\.html?$|강의\s*노트|실습\s*노트|강의\s*정리|수업\s*정리|lecture[ _-]?notes?|study[ _-]?notes?")),
    ("과제", re.compile(r"(?i)assmt|assignment|homework|\bhw\d|problem[ _-]?set|pset|과제|소논문|제출|레포트|report")),
    ("실습자료", re.compile(r"(?i)실습|\blab\b|tutorial|exercise|practice|mesh|\.msh$|\.wbpz$|\.xlsx?$|예제")),
    ("참고자료", re.compile(r"(?i)족보|논문|지침|참고|매뉴얼|reference|paper|journal|manual|guideline")),
    ("교재", re.compile(r"(?i)교재|음원|textbook|chapter|lektion")),
)
_HIDDEN = re.compile(r"(?i)^(?:[._]|~\$|desktop\.ini$|thumbs\.db$)")


def _plain(name: str) -> str:
    return name.replace(" ", "")


def _is_old_notes_folder(name: str) -> bool:
    """A folder an earlier semester used for lecture notes (configured in settings; none on a new install)."""
    return _plain(name) in settings.legacy_note_folders()


def category_of(course_dir: Path, path: Path) -> str | None:
    """The category folder a file sits in, None for a file at the top of the course folder or in another folder."""
    try:
        parts = Path(path).relative_to(course_dir).parts
    except ValueError:
        return None
    if len(parts) < 2:
        return None
    return parts[0] if parts[0] in CATEGORIES else "정리본" if _is_old_notes_folder(parts[0]) else None


def folder_for(course_dir: Path, category: str) -> Path:
    """The folder that holds a category: the older notes folder while it is the only one for 정리본."""
    if category == "정리본" and not (course_dir / category).is_dir():
        old = [p for p in Path(course_dir).iterdir() if p.is_dir() and _is_old_notes_folder(p.name)]
        if old:
            return old[0]
    return Path(course_dir) / category


def active_categories() -> tuple[str, ...]:
    """The categories the chosen folder layout uses (기본: 강의자료·과제·정리본). All of CATEGORIES are still read."""
    return tuple(settings.folder_layout()["categories"])


def guess(name: str) -> str | None:
    """Where an unsorted file or folder probably belongs, from its name alone. A suggestion, never applied by itself.

    A category the layout does not use folds into 강의자료 (기본 has no 실습자료·참고자료·교재 folders)."""
    if _STAYS.search(name):
        return None
    for category, pattern in _GUESS:
        if pattern.search(name):
            return category if category in active_categories() else "강의자료"
    return "강의자료" if Path(name).suffix.lower() in MATERIAL_EXTENSIONS else None


def course_folders(connection: sqlite3.Connection, root: Path) -> list[str]:
    names = [r[0] for r in connection.execute("SELECT folder_name FROM Course ORDER BY folder_name")]
    return [name for name in names if (Path(root) / name).is_dir()]


def _registered(connection: sqlite3.Connection) -> dict[str, str]:
    known = {os.path.normcase(r[0]): "material" for r in connection.execute("SELECT path FROM Material")}
    known.update({os.path.normcase(r[0]): "note" for r in connection.execute(
        "SELECT path FROM StudyArtifact WHERE path IS NOT NULL")})
    return known


def _count(folder: Path) -> int:
    return sum(1 for _, _, files in os.walk(folder) for name in files if not _HIDDEN.match(name))


def inventory(connection: sqlite3.Connection, root: Path, course: str) -> dict:
    """What is in a course folder, by category, for the app. Reads names and sizes only."""
    root = Path(root).resolve()
    if course not in course_folders(connection, root):
        raise ValueError("과목 폴더를 찾지 못했습니다")
    folder = root / course
    known = _registered(connection)

    def entry(path: Path) -> dict:
        item = {"name": path.name, "path": str(path), "folder": path.is_dir()}
        if item["folder"]:
            item["count"] = _count(path)
        else:
            item["bytes"] = path.stat().st_size
            item["known"] = known.get(os.path.normcase(str(path)))
        return item

    def listing(parent: Path) -> list[dict]:
        children = [p for p in parent.iterdir() if not _HIDDEN.match(p.name)]
        return [entry(p) for p in sorted(children, key=lambda p: (p.is_dir(), p.name))]

    groups = {name: [] for name in CATEGORIES}
    loose, top = [], []
    for path in sorted(folder.iterdir(), key=lambda p: (p.is_dir(), p.name)):
        if _HIDDEN.match(path.name):
            continue
        if path.is_dir() and path.name in CATEGORIES:
            groups[path.name] += listing(path)
        elif path.is_dir() and _is_old_notes_folder(path.name):
            groups["정리본"] += listing(path)
        elif path.is_file() and _STAYS.search(path.name):
            top.append(entry(path))
        else:
            loose.append({**entry(path), "suggest": guess(path.name)})
    return {"course": course, "folder": str(folder), "top": top, "loose": loose,
            "groups": [{"category": name, "items": items} for name, items in groups.items() if items]}


# ---------- moving

def _inside(root: Path, relative: str) -> Path:
    path = (root / relative).resolve() if not Path(relative).is_absolute() else Path(relative).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError(f"학기 폴더 밖입니다: {relative}")
    return path


def check(connection: sqlite3.Connection, root: Path, moves: list[dict]) -> list[str]:
    """Why this plan cannot run as written (empty = it can). Simulates the moves in order; touches nothing."""
    root = Path(root).resolve()
    courses = set(course_folders(connection, root))
    fixed = [os.path.normcase(r[0]) for r in connection.execute(
        "SELECT source_file FROM Transcript WHERE source_file IS NOT NULL UNION SELECT path FROM MediaAsset")]
    renames: list[tuple[str, str]] = []  # the moves earlier in the plan, as (old, new)
    problems = []

    def exists(path: Path) -> bool:
        """Whether the path would exist once the earlier moves have run."""
        key = os.path.normcase(str(path))
        if any(new.startswith(key + os.sep) for _, new in renames):
            return True  # a folder an earlier move creates on the way
        for old, new in reversed(renames):
            if key == new or key.startswith(new + os.sep):
                key = old + key[len(new):]
            elif key == old or key.startswith(old + os.sep):
                return False
        return Path(key).exists()

    for move in moves:
        label = f"{move.get('from')} → {move.get('to')}"
        try:
            source, target = _inside(root, move["from"]), _inside(root, move["to"])
        except (KeyError, ValueError, OSError) as exc:
            problems.append(f"{label}: {exc}")
            continue
        src_parts, dst_parts = source.relative_to(root).parts, target.relative_to(root).parts
        key = os.path.normcase(str(source))
        if src_parts[0] not in courses or dst_parts[0] != src_parts[0] or len(src_parts) < 2 or len(dst_parts) < 2:
            problems.append(f"{label}: 같은 과목 폴더 안에서만 옮깁니다")
        elif any(part.lower() == "_lecturehub" for part in (*src_parts, *dst_parts)):
            problems.append(f"{label}: 앱 폴더는 옮기지 않습니다")
        elif not exists(source):
            problems.append(f"{label}: 옮길 파일이 없습니다")
        elif any(p.is_symlink() or p.is_junction() for p in (source, *source.parents) if p != root and p.exists()):
            problems.append(f"{label}: 바로가기(링크)는 옮기지 않습니다")
        elif target.is_relative_to(source):
            problems.append(f"{label}: 자기 안으로는 옮길 수 없습니다")
        elif exists(target):
            problems.append(f"{label}: 같은 이름이 이미 있습니다")
        elif any(f == key or f.startswith(key + os.sep) for f in fixed):
            problems.append(f"{label}: 전사 원본·녹음 파일은 옮기지 않습니다")
        else:
            renames.append((key, os.path.normcase(str(target))))
    return problems


def _follow(connection: sqlite3.Connection, old: Path, new: Path, at: int, undo: bool) -> int:
    """Point the rows that remember ``old`` (a file, or files under a folder) at ``new``."""
    old_text, changed = str(old), 0
    for table, column, subject in (("Material", "path", "Material"), ("StudyArtifact", "path", "StudyArtifact"),
                                   ("IngestItem", "source_path", None)):
        rows = connection.execute(f"SELECT id, {column} FROM {table} WHERE {column} IS NOT NULL").fetchall()
        for row_id, path in rows:
            same = os.path.normcase(path) == os.path.normcase(old_text)
            under = os.path.normcase(path).startswith(os.path.normcase(old_text) + os.sep)
            if not (same or under):
                continue
            moved = str(new) if same else str(new) + path[len(old_text):]
            connection.execute(f"UPDATE {table} SET {column} = ? WHERE id = ?", (moved, row_id))
            if subject:
                connection.execute(
                    "INSERT INTO Evidence(id,subject_type,subject_id,field,kind,detail,source,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (new_ulid(), subject, row_id, "path", "user_move",
                     json.dumps({"from": path, "to": moved, **({"undo": True} if undo else {})}, ensure_ascii=False),
                     "user", at))
                changed += 1
    return changed


def _prune(folder: Path, stop: Path) -> None:
    """Remove folders a move left empty, up to (not including) the course folder."""
    while folder != stop and folder.is_relative_to(stop):
        try:
            folder.rmdir()  # only ever removes an empty folder
        except OSError:
            return
        folder = folder.parent


def apply(connection: sqlite3.Connection, root: Path, moves: list[dict], *, undo: bool = False) -> dict:
    """Run a checked plan. Each move is committed with its log row; a failure stops there and says what was done."""
    root = Path(root).resolve()
    problems = check(connection, root, moves)
    if problems:
        raise ValueError("옮길 수 없습니다 — " + " / ".join(problems[:5]) + (f" 외 {len(problems) - 5}건" if len(problems) > 5 else ""))
    at, done, rows = time.time_ns() // 1_000_000, [], 0
    for move in moves:
        source, target = _inside(root, move["from"]), _inside(root, move["to"])
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.rename(source, target)  # same volume: nothing is copied; refuses an existing target on Windows
        except OSError as exc:
            return {"batch": at, "moved": done, "rows": rows,
                    "stopped": f"{source.relative_to(root)}: {exc.strerror or exc}"}
        try:
            with connection:
                rows += _follow(connection, source, target, at, undo)
                connection.execute("INSERT INTO MoveLog(from_path,to_path,at,estimated_start,estimated_end) "
                                   "VALUES (?,?,?,0,0)", (str(source), str(target), at))
        except sqlite3.Error:
            os.rename(target, source)  # the record failed: the file goes back
            raise
        _prune(source.parent, root / source.relative_to(root).parts[0])
        done.append({"from": str(source.relative_to(root)), "to": str(target.relative_to(root))})
    if done:
        with connection:
            append(connection, "FILES_UNDONE" if undo else "FILES_ORGANIZED", "MoveLog", str(at),
                   {"moves": len(done), "rows": rows})
    return {"batch": at, "moved": done, "rows": rows}


def batches(connection: sqlite3.Connection) -> list[dict]:
    return [{"batch": r[0], "moves": r[1]} for r in connection.execute(
        "SELECT at, count(*) FROM MoveLog WHERE estimated_start = 0 AND estimated_end = 0 GROUP BY at ORDER BY at DESC")]


def undo(connection: sqlite3.Connection, root: Path, batch: int | None = None) -> dict:
    """Put a batch back (the latest when not named). Written as a new batch, so an undo can itself be undone."""
    known = batches(connection)
    if not known:
        raise ValueError("되돌릴 이동 기록이 없습니다")
    batch = known[0]["batch"] if batch is None else int(batch)
    rows = connection.execute("SELECT from_path, to_path FROM MoveLog WHERE at = ? AND estimated_start = 0 "
                              "ORDER BY rowid DESC", (batch,)).fetchall()
    if not rows:
        raise ValueError("그 이동 기록을 찾지 못했습니다")
    return apply(connection, root, [{"from": to_path, "to": from_path} for from_path, to_path in rows], undo=True)


# ---------- files moved by hand

def _find(folder: Path, name: str, suffix: str, digest: str, deep: bool) -> Path | None:
    """The file with these bytes somewhere in the course folder: same name first, then (``deep``) same type."""
    candidates = []
    for base, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if not _HIDDEN.match(d)]
        candidates += [Path(base) / f for f in files if Path(f).suffix.lower() == suffix and (deep or f == name)]
    for path in sorted(candidates, key=lambda p: (p.name != name, str(p))):
        try:
            if sha256_file(path) == digest:
                return path
        except OSError:
            continue
    return None


def relocate(connection: sqlite3.Connection, root: Path, *, deep: bool = True) -> list[dict]:
    """Rows whose file is no longer where they remember it: find the same bytes in the same course
    folder (moved in Explorer) and follow them. ``deep=False`` (app start) reads only files that kept
    their name, so a deleted file does not cost a hash of the whole folder each time. Caller commits.

    Hashing (disk work) happens first with no write pending; all updates are then made together, so
    the SQLite write lock is only held for the short write at the end (review #1)."""
    root = Path(root).resolve()
    found, moves, claimed = [], [], set()
    rows = [("Material", r[0], r[1], r[2]) for r in connection.execute("SELECT id, path, sha256 FROM Material")]
    rows += [("StudyArtifact", r[0], r[1], r[2]) for r in connection.execute(
        "SELECT id, path, json_extract(inputs,'$.source_sha256') FROM StudyArtifact "
        "WHERE path IS NOT NULL AND json_valid(inputs)")]
    for table, row_id, path, digest in rows:
        old = Path(path)
        if not digest or old.is_file() or not old.is_relative_to(root):
            continue
        parts = old.relative_to(root).parts
        if len(parts) < 2 or parts[0].startswith("_") or not (root / parts[0]).is_dir():
            continue
        new = _find(root / parts[0], old.name, old.suffix.lower(), digest, deep)
        if new is None or (table, str(new)) in claimed or connection.execute(
                f"SELECT 1 FROM {table} WHERE path = ?", (str(new),)).fetchone():
            continue
        claimed.add((table, str(new)))  # what an earlier UPDATE of this run would have made visible
        moves.append((table, row_id, path, digest, old, new))
    for table, row_id, path, digest, old, new in moves:
        connection.execute(f"UPDATE {table} SET path = ? WHERE id = ?", (str(new), row_id))
        connection.execute(
            "INSERT INTO Evidence(id,subject_type,subject_id,field,kind,detail,source,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (new_ulid(), table, row_id, "path", "relocated",
             json.dumps({"from": path, "to": str(new), "sha256": digest}, ensure_ascii=False), "metadata",
             time.time_ns() // 1_000_000))
        found.append({"from": str(old.relative_to(root)), "to": str(new.relative_to(root))})
    return found
