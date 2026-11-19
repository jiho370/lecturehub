"""A per-user folder where the AI helper (Codex or Claude Code) is opened with the app's skills.

The skills ship inside the package (``lecturehub/agent_skills``). Opening the helper from a source checkout only
worked for the developer, so the app copies them into ``%LOCALAPPDATA%\\LectureHub\\agent-workspace`` instead:
``.claude/skills`` for Claude Code and ``.agents/skills`` for Codex, plus a short ``AGENTS.md`` / ``CLAUDE.md``.
The folder is rebuilt from the package on every launch, so an app update refreshes the skills.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

SKILLS_SRC = Path(__file__).resolve().parents[1] / "agent_skills"

GUIDE = """# Lecture Hub 도우미 작업 폴더

이 폴더는 Lecture Hub 앱이 AI 도우미(Codex 또는 Claude Code)를 열 때 쓰는 곳입니다. 스킬은 앱이 매번 새로 복사합니다.

- 환경 변수 `LECTUREHUB_PY`(앱 파이썬)와 `LECTUREHUB_ROOT`(학기 폴더)를 앱이 넣어 줍니다.
- 학기 폴더는 읽기 전용입니다. 쓸 수 있는 곳은 그 과목의 `정리본` 폴더와 `_lecturehub\\quiz\\<과목>` 폴더뿐입니다.
- 데이터베이스를 쓰는 명령(`attach-note`, `quiz cues`)은 Codex가 실행 전에 승인을 묻습니다. 명령을 확인하고 승인하세요.
- 강의 내용은 이 작업 폴더에 복사하지 않습니다. 사용자가 동의한 전사·자료 글만 선택한 AI 제공자에게 보냅니다.
"""


def workspace_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(os.environ.get("LECTUREHUB_AGENT_WORKSPACE") or Path(base) / "LectureHub" / "agent-workspace")


def skill_names() -> list[str]:
    return sorted(p.name for p in SKILLS_SRC.iterdir() if (p / "SKILL.md").is_file()) if SKILLS_SRC.is_dir() else []


def ensure(target: Path | None = None) -> Path:
    """Rebuild the workspace from the packaged skills and return it."""
    target = Path(target) if target else workspace_dir()
    target.mkdir(parents=True, exist_ok=True)
    for engine_dir in (".claude/skills", ".agents/skills"):
        base = target / engine_dir
        for name in skill_names():
            dest = base / name
            if not dest.resolve().is_relative_to(target.resolve()) or dest.is_symlink() or base.is_symlink():
                raise ValueError("도우미 스킬 경로가 작업 폴더 밖을 가리킵니다.")
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(SKILLS_SRC / name, dest)
    for name in ("AGENTS.md", "CLAUDE.md"):
        (target / name).write_text(GUIDE, encoding="utf-8")
    if not (target / ".git").exists():  # Codex wants a git folder; an empty one is enough
        try:
            subprocess.run(["git", "init", "-q"], cwd=target, capture_output=True, timeout=20,
                           creationflags=0x08000000 if os.name == "nt" else 0, check=False)
        except (OSError, subprocess.SubprocessError):
            pass
    return target


def note_write_dirs(root: Path, folders) -> list[Path]:
    """The only semester folders the note helper may write: inside the semester folder but never the folder itself,
    and under ``_lecturehub`` only a course folder of ``_lecturehub\\quiz`` (cues files the exam screen imports)."""
    base = Path(root).resolve()
    app = base / "_lecturehub"
    quiz = app / "quiz"
    result: list[Path] = []
    for folder in folders or ():
        path = Path(folder).resolve()
        if path == base or not path.is_relative_to(base):
            raise ValueError("도우미가 쓸 폴더는 학기 폴더 안의 정리본 폴더여야 합니다.")
        if path.is_relative_to(app) and (path == quiz or not path.is_relative_to(quiz)):
            raise ValueError("도우미는 _lecturehub 안에서는 발언 파일 폴더에만 씁니다.")
        if path not in result:
            result.append(path)
    return result


def launch_env(root: Path, pack: Path | None = None) -> dict[str, str]:
    """Environment for the helper's console: the app's own python, the chosen data folder and, when the app
    already made it, the note pack JSON (the sandboxed helper then need not open the database to read it)."""
    from . import proc

    # not sys.executable: in the installed build that is the window exe, which ignores "-m lecturehub …"
    env = {**os.environ, "LECTUREHUB_PY": proc.console_python(), "LECTUREHUB_ROOT": str(root), "PYTHONUTF8": "1"}
    env.pop("LECTUREHUB_NOTE_PACK", None)
    if pack:
        env["LECTUREHUB_NOTE_PACK"] = str(pack)
    return env
