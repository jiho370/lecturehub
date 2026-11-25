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


CONFIRM_STATES = ("allowed", "syllabus")  # set for every course at once only through the app's native dialog


def bulk_message(state: str, courses: list[str]) -> str:
    """The native dialog of "모든 과목을 [상태]로": the user states that every listed lecturer allowed recording (or
    that every syllabus allows it), so the dialog names the courses and repeats WARNING's advice."""
    basis = "교수님이 녹음을 허락했다" if state == "allowed" else "강의계획서에서 녹음을 허용한다"
    listed = "\n".join(f"· {course}" for course in courses)
    advice = WARNING.split(". ", 1)[1]  # "교수님 허락이나 강의계획서를 확인하고, 사담·쉬는 시간에는 멈춰 주세요."
    return (f"아래 과목 {len(courses)}개의 녹음 허락을 모두 '{STATES[state]}'(으)로 바꿉니다.\n{listed}\n\n"
            f"과목마다 {basis}는 것을 직접 확인했을 때만 계속하세요. 확인하지 않은 과목이 있으면 취소하고 과목마다 고르세요.\n"
            f"{advice}")


def set_many(root: Path, courses: list[str], state: str) -> list[dict]:
    """Every listed course to one state in one write (all or nothing: a bad name changes none)."""
    if state not in STATES or not courses or any(not c.strip() or c == "*" or len(c) > 120 for c in courses):
        raise ValueError("과목과 녹음 허락 상태를 확인하세요.")
    data = load(root)
    data.update({course: state for course in courses})
    _path(root).parent.mkdir(parents=True, exist_ok=True)
    write_json(_path(root), data)
    return [check(root, course) for course in courses]


def for_output(out: Path, course: str) -> dict:
    """Find the semester owning an app output directory; never guess a different configured library."""
    out = Path(out).resolve()
    root = next((p.parent for p in (out, *out.parents) if p.name == "_lecturehub"), None)
    return check(root, course) if root else {"allowed": True, "warning": WARNING, "reason": ""}
