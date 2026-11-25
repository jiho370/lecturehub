"""Find the outside programs the app can use and say what each one is for (first-run check, settings screen).

Nothing here runs the tools except ``--version`` style probes with a short timeout; nothing needs the network.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

# name -> (what it enables, how to get it, required for recording/linking)
TOOLS: dict[str, dict] = {
    "ffmpeg": {"label": "ffmpeg", "needed_for": "녹음 마무리(MP3), 전사 조각 만들기", "required": True,
               "hint": "앱에 들어 있습니다. 보이지 않으면 앱을 다시 설치하세요. "
                       "(소스에서 실행할 때: winget install Gyan.FFmpeg 후 앱을 다시 켭니다)"},
    "ffprobe": {"label": "ffprobe", "needed_for": "녹음 길이 확인과 강의 연결", "required": True,
                "hint": "ffmpeg와 함께 들어 있습니다"},
    "codex": {"label": "Codex (ChatGPT 계정)", "needed_for": "선택한 ChatGPT 계정으로 정리본 만들기·수업 중 공지 찾기(Edu 여부는 계정에서 확인)",
              "required": False,
              "hint": "npm install -g @openai/codex 후 codex login (학교 ChatGPT Edu 계정)",
              "help_url": "https://developers.openai.com/codex/cli"},
    "claude": {"label": "Claude Code", "needed_for": "정리본 만들기(호환 엔진)", "required": False,
               "hint": "설치 안내를 따른 뒤 claude 를 한 번 실행해 로그인합니다",
               "help_url": "https://claude.com/claude-code"},
    "gcloud": {"label": "Google Cloud CLI", "needed_for": "실시간 자막(Chirp)", "required": False,
               "hint": "선택 기능입니다. 필요할 때만 설치합니다"},
    "aside": {"label": "Aside", "needed_for": "Clova 가져오기·그스룸 예약(브라우저 자동화)", "required": False,
              "hint": "선택 기능입니다. 필요할 때만 설치합니다"},
}


def _extra_dirs() -> list[Path]:
    home = Path.home()
    local = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
    return [home / ".local" / "bin", local / "Aside" / "CLI" / "current", Path(os.environ.get("APPDATA") or "") / "npm"]


# PATH scans cost ~45 ms per tool on Windows (every PATH entry x PATHEXT) and the window asks on every start and on
# each settings refresh; remember answers briefly. ``status(check_login=True)`` ("프로그램 다시 찾기") clears it.
_FOUND: dict[tuple, tuple[float, str | None]] = {}
CACHE_SECONDS = 120.0


def clear_cache() -> None:
    _FOUND.clear()


def find(name: str) -> str | None:
    """Full path of ``name`` (also checks the usual per-user install folders), else ``None``."""
    override = os.environ.get(f"LECTUREHUB_{name.upper()}_EXE")
    if override and Path(override).is_file():
        return override
    key = (name, os.environ.get("PATH", ""), os.environ.get("LOCALAPPDATA", ""), os.environ.get("APPDATA", ""))
    hit = _FOUND.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    found = _scan(name)
    _FOUND[key] = (time.monotonic(), found)
    return found


def _scan(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for folder in _extra_dirs():
        for suffix in (".exe", ".cmd", ""):
            candidate = folder / f"{name}{suffix}"
            if candidate.is_file():
                return str(candidate)
    return None


def codex_signed_in(exe: str, timeout: float = 8.0) -> bool | None:
    """``True``/``False`` from ``codex login status``; ``None`` when it cannot be asked."""
    try:
        done = subprocess.run([exe, "login", "status"], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, creationflags=0x08000000 if os.name == "nt" else 0)
    except (OSError, subprocess.SubprocessError):
        return None
    text = f"{done.stdout}\n{done.stderr}".lower()
    return done.returncode == 0 and "logged in" in text


def status(check_login: bool = False) -> dict[str, dict]:
    """Per tool: ``{found, path, label, needed_for, required, hint}`` (plus ``signed_in`` for Codex on request)."""
    if check_login:  # the user asked to look again (e.g. just installed something)
        clear_cache()
    out: dict[str, dict] = {}
    for name, meta in TOOLS.items():
        path = find(name)
        row = {**meta, "found": bool(path), "path": path}
        if check_login and name == "codex" and path:
            row["signed_in"] = codex_signed_in(path)
        out[name] = row
    return out
