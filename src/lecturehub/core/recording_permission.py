"""REC-PERM-001: course recording permission, independent of provider consent; no database."""
from __future__ import annotations

import json
from pathlib import Path

from lecturehub.recorder.heartbeat import write_json

STATES = {"allowed": "허락함", "syllabus": "강의계획서 허용", "unknown": "아직 확인 안 함", "forbidden": "금지"}
WARNING = "녹음 허락을 아직 확인하지 않았습니다. 교수님 허락이나 강의계획서를 확인하고, 사담·쉬는 시간에는 멈춰 주세요."


def _path(root: Path) -> Path:
    return Path(root) / "_lecturehub" / "recording_permissions.json"


def load(root: Path) -> dict:
    try:
        data = json.loads(_path(root).read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def check(root: Path, course: str) -> dict:
    state = load(root).get(course, "unknown")
    if state not in STATES:
        state = "unknown"
    return {"course": course, "state": state, "label": STATES[state], "allowed": state != "forbidden",
            "warning": WARNING if state == "unknown" else "",
            "reason": "녹음이 금지된 과목입니다. 녹음·실시간 자막을 시작할 수 없습니다." if state == "forbidden" else ""}


def set_state(root: Path, course: str, state: str) -> dict:
    if not course.strip() or course == "*" or len(course) > 120 or state not in STATES:
        raise ValueError("과목과 녹음 허락 상태를 확인하세요.")
    data = load(root)
    data[course] = state
    _path(root).parent.mkdir(parents=True, exist_ok=True)
    write_json(_path(root), data)
    return check(root, course)


def for_output(out: Path, course: str) -> dict:
    """Find the semester owning an app output directory; never guess a different configured library."""
    out = Path(out).resolve()
    root = next((p.parent for p in (out, *out.parents) if p.name == "_lecturehub"), None)
    return check(root, course) if root else {"allowed": True, "warning": WARNING, "reason": ""}
