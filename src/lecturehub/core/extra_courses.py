"""'수업 외' (outside-class) recording topics, e.g. an online AI lecture — no timetable, no course folder.

The list lives in ``_lecturehub\\extra_courses.json`` (not the DB) so the recording app can offer
it without opening the database (D-050). The DB gets a Course row only when a recording is
linked (index/extra.py). Recordings of these topics are copied to ``녹음본\\수업 외\\<이름>\\``.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from lecturehub.core.textio import read_text_any

FILE = "extra_courses.json"
ARCHIVE_GROUP = "수업 외"
UNSORTED = "미분류"  # where a recording made without a course goes; never in the list, never a consent unit
_BAD = re.compile(r'[\\/:*?"<>|]')


def _path(root: Path) -> Path:
    return Path(root) / "_lecturehub" / FILE


def names(root: Path) -> list[str]:
    try:
        data = json.loads(read_text_any(_path(root)))
    except (OSError, ValueError):
        return []
    return [n for n in data.get("names", []) if isinstance(n, str) and n.strip()]


def add(root: Path, name: str, timetable_folders: list[str] = ()) -> list[str]:
    """Add a topic (idempotent). Refuses names that clash with a class or cannot be a folder name."""
    name = " ".join(str(name).split())
    if not name:
        raise ValueError("이름을 적어 주세요")
    if len(name) > 40 or _BAD.search(name) or name in (".", "..") or name in (ARCHIVE_GROUP, UNSORTED):
        raise ValueError('이름에 \\ / : * ? " < > | 는 쓸 수 없고 40자까지입니다')
    if name in timetable_folders:
        raise ValueError(f"'{name}'은(는) 시간표 과목 이름입니다. 다른 이름을 쓰세요")
    current = names(root)
    if name not in current:
        current.append(name)
        path = _path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"names": current}, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)
    return current


def is_extra(root: Path, name: str) -> bool:
    return bool(name) and (name == UNSORTED or name in names(root))
