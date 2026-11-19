"""AI-001: shared interface and user selection; no database or credentials."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol, runtime_checkable

from lecturehub.core import settings

CHOICES = {"codex": "Codex (ChatGPT)", "claude": "Claude Code", "off": "사용 안 함"}
FEATURES = ("notes", "gsr_chat")


@runtime_checkable
class Engine(Protocol):
    name: str

    def available(self) -> bool: ...
    def run_json(self, prompt: str, schema: dict, *, timeout: float = 900, stop=None) -> dict: ...
    def run_text(self, prompt: str, *, timeout: float = 900, stop=None) -> str: ...
    def open_console(self, workspace: Path, root: Path, lecture_id: str, *, launcher=None, writable=(), pack=None) -> None: ...
    def session(self): ...


def selected(feature: str = "notes") -> str:
    value = settings.get("ai_engine", {})
    value = {"default": value} if isinstance(value, str) else value if isinstance(value, dict) else {}
    name = (os.environ.get("LECTUREHUB_AI_ENGINE") or value.get(feature) or value.get("default") or "codex").strip().lower()
    if name not in CHOICES:
        raise ValueError("AI 엔진 설정을 확인하세요(codex / claude / off).")
    return name


def set_choice(name: str, feature: str = "default") -> dict:
    if feature not in ("default", *FEATURES) or name not in (*CHOICES, "inherit") or (feature == "default" and name == "inherit"):
        raise ValueError("AI 엔진과 기능을 확인하세요.")
    value = settings.get("ai_engine", {})
    value = {"default": value} if isinstance(value, str) else dict(value) if isinstance(value, dict) else {}
    if name == "inherit":
        value.pop(feature, None)
    else:
        value[feature] = name
    settings.save({"ai_engine": value})
    return status()


def get_engine(feature: str = "notes") -> Engine:
    name = selected(feature)
    if name == "off":
        raise RuntimeError("AI 사용이 꺼져 있습니다. 설정에서 엔진을 고르세요.")
    if name == "codex":
        from .codex import CodexEngine

        return CodexEngine()
    from .claude import ClaudeEngine

    return ClaudeEngine()


def status(check_login: bool = False) -> dict:
    from lecturehub.core import tools

    rows = {}
    for feature in FEATURES:
        try:
            name = selected(feature)
            engine = get_engine(feature) if name != "off" else None
            found = bool(engine and engine.available())
            rows[feature] = {"name": name, "label": CHOICES[name], "available": found}
        except ValueError as exc:
            rows[feature] = {"name": "off", "label": "설정 확인 필요", "available": False, "error": str(exc)}
    value = settings.get("ai_engine", {})
    value = {"default": value} if isinstance(value, str) else value if isinstance(value, dict) else {}
    login = None
    if check_login:
        from .codex import CodexEngine

        exe = CodexEngine().executable()
        login = tools.codex_signed_in(exe) if exe else None
    return {"choices": CHOICES, "settings": value, "features": rows, "env_override": bool(os.environ.get("LECTUREHUB_AI_ENGINE")),
            "codex_signed_in": login, "account_notice": "CLI 로그인만으로 Edu 작업 공간인지 알 수 없습니다. ChatGPT 계정에서 확인하세요."}
