"""Shared Aside CLI discovery and process transport for browser adapters (D-087)."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

CREATE_NO_WINDOW = 0x08000000
BELOW_NORMAL_PRIORITY = 0x00004000
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def find_aside() -> str | None:
    override = os.environ.get("LECTUREHUB_ASIDE_EXE")
    if override:
        return override
    if os.environ.get("PYTEST_CURRENT_TEST"):  # tests never drive the real browser
        return None
    found = shutil.which("aside")
    if found:
        return found
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Aside" / "CLI" / "current" / "aside.exe"
    return str(local) if local.is_file() else None


def _kill_tree(proc) -> None:
    """Stop the process and its browser adapter children."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, check=False,
                       creationflags=CREATE_NO_WINDOW)
    else:
        proc.kill()


def _stopped(proc) -> None:
    _kill_tree(proc)
    try:
        proc.communicate(timeout=5)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        pass


def execute(args: list[str], script: str, *, timeout: int, stop=None, stop_process=None):
    """aside를 창 없이 낮은 우선순위로 실행하고 스크립트를 stdin으로 준다. ``(stdout, stderr, returncode)`` 또는 오류 dict."""
    stop_process = stop_process or _stopped
    flags = CREATE_NO_WINDOW | BELOW_NORMAL_PRIORITY if os.name == "nt" else 0
    try:
        proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                encoding="utf-8", errors="replace", creationflags=flags)
    except OSError as exc:
        return {"ok": False, "error": f"Aside를 실행하지 못했습니다: {exc}"}
    deadline = time.monotonic() + timeout
    pending = script  # communicate() takes input only on the first call; retries keep the output
    while True:
        try:
            stdout, stderr = proc.communicate(input=pending, timeout=0.5)
            return stdout, stderr, proc.returncode
        except subprocess.TimeoutExpired:
            pending = None
            if stop is not None and stop.is_set():
                stop_process(proc)
                return {"ok": False, "stopped": True, "error": "사용자가 중지했습니다"}
            if time.monotonic() > deadline:
                stop_process(proc)
                return {"ok": False, "error": f"Aside가 {max(timeout, 0) // 60 or 1}분 안에 끝내지 못했습니다"}



def parse_output(stdout: str) -> dict:
    """``aside repl`` 출력(색 코드·안내 줄 포함)에서 마지막 RESULT 줄을 꺼낸다. 종료 코드는 오류여도 0이라 보지 않는다."""
    text = ANSI.sub("", stdout or "")
    at = text.rfind("RESULT:")
    brace = text.find("{", at) if at >= 0 else -1
    if brace < 0:
        errors = [ln.strip() for ln in text.splitlines() if "Error" in ln or "[error" in ln]
        detail = (errors[0] if errors else text.strip()[-200:]) or "출력 없음"
        return {"ok": False, "error": "Aside가 결과를 돌려주지 않았습니다: " + detail[:200]}
    try:
        result, _ = json.JSONDecoder().raw_decode(text[brace:])
    except ValueError:
        return {"ok": False, "error": "결과 JSON이 깨졌습니다: " + text[brace:brace + 200]}
    if not isinstance(result, dict):
        return {"ok": False, "error": "결과 형식이 다릅니다"}
    return result
