"""Bounded, hidden CLI execution. Errors never echo lecture input or raw provider logs."""
from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path


def cli_env() -> dict:
    env = dict(os.environ)
    for key in ("OPENAI_API_KEY", "CODEX_API_KEY"):
        env.pop(key, None)  # the Codex engine uses the user's CLI login, never inherited API billing
    return env


def execute(args: list[str], prompt: str, *, timeout: float, cwd: Path | None = None, stop=None) -> str:
    flags = 0x08004000 if os.name == "nt" else 0  # hidden + BelowNormal
    process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding="utf-8", errors="replace", cwd=cwd, env=cli_env(), creationflags=flags)
    deadline = time.monotonic() + timeout
    first = True
    try:
        while True:
            if stop is not None and stop.is_set():
                raise RuntimeError("AI 작업을 취소했습니다.")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("AI 응답 대기 시간을 넘었습니다. 잠시 뒤 다시 시도하세요.")
            try:
                data = prompt if first else None
                first = False
                stdout, _ = process.communicate(data, timeout=min(.25, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
        if process.returncode:
            raise RuntimeError("AI 명령이 실패했습니다. 로그인·사용 한도와 CLI 버전을 확인하세요.")
        return stdout
    finally:
        if process.poll() is None:
            if os.name == "nt":
                try:
                    subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"], capture_output=True,
                                   creationflags=0x08000000, timeout=10, check=False)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            if process.poll() is None:
                process.kill()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                pass


def open_console(engine: str, exe: str | None, workspace: Path, root: Path, lecture_id: str, *, launcher=None,
                 writable=(), pack: Path | None = None) -> None:
    """Only a user-invoked console can write. Reject cmd metacharacters before crossing its shell.

    Codex may write only its own workspace and ``writable`` (the course's 정리본 folder and the cues folder under
    ``_lecturehub\\quiz``), never the whole semester folder. Anything else (the database through ``attach-note``)
    is asked for in the console (``-a on-request``), so the user sees and approves it."""
    from lecturehub.core import agent_workspace

    if not re.fullmatch(r"[A-Za-z0-9_-]+", lecture_id):
        raise ValueError("회차 ID를 확인하세요.")
    if not exe:
        raise FileNotFoundError(f"{engine} 명령을 찾지 못했습니다. 설정에서 설치 안내를 확인하세요.")
    folders = agent_workspace.note_write_dirs(root, writable)
    if engine == "codex":
        args = [exe, "--sandbox", "workspace-write", "-a", "on-request", "-C", str(workspace)]
        for folder in folders:
            args += ["--add-dir", str(folder)]
        args.append(f"$lecture-note {lecture_id}")
    else:
        args = [exe, f"/lecture-note {lecture_id}"]
    if any(any(c in str(a) for c in '\r\n"&|<>^%!') for a in args):
        raise ValueError("도우미 경로에 명령 기호가 있습니다. 해당 기호가 없는 폴더를 사용하세요.")
    # Quote every argument, including parentheses without spaces. cmd /s strips the outer pair only.
    line = " ".join('"' + str(a) + '"' for a in args)
    command = 'cmd.exe /d /s /k "' + line + '"' 
    if launcher:
        launcher(command, workspace)
    else:
        env = agent_workspace.launch_env(root, pack)
        env.pop("OPENAI_API_KEY", None)
        env.pop("CODEX_API_KEY", None)
        subprocess.Popen(command, cwd=workspace, env=env, creationflags=subprocess.CREATE_NEW_CONSOLE)
