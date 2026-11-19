"""Speech language of a recording (user report 2026-10-19: a German YouTube recording came back as two lines).

Both engines were told "Korean" unconditionally (Gemini ``language_codes: ["ko-KR"]``, Whisper ``language: ko``),
so other languages were mostly dropped. The language now travels in the job's preset (``preset["language"]``),
so a resumed job keeps it. Order: explicit ``--language`` → the course default in
``_lecturehub\\transcribe_languages.json`` (``{"과목A": "ko", "과목B": "de"}``; the user's file, read only) → ``ko``.
``auto`` lets Whisper detect the language; Gemini needs a language code, so ``auto`` is refused there.
"""
from __future__ import annotations

import json
from pathlib import Path

DEFAULT = "ko"
LOCALES = {"ko": "ko-KR", "de": "de-DE", "en": "en-US", "ja": "ja-JP", "zh": "zh-CN", "fr": "fr-FR", "es": "es-ES"}
CHOICES = (*LOCALES, "auto")
MAP_FILE = "transcribe_languages.json"


def check(language: str, provider: str) -> str:
    language = (language or "").strip().lower()
    if language not in CHOICES:
        raise ValueError(f"모르는 전사 언어입니다: {language!r} (가능: {', '.join(CHOICES)})")
    if language == "auto" and provider == "gemini_transcribe":
        raise ValueError("Google 전사는 언어를 정해야 합니다(auto는 Whisper만). 예: --language de")
    return language


def course_default(root: Path, course: str | None) -> str | None:
    from .store import app_dir

    try:
        data = json.loads((app_dir(Path(root)) / MAP_FILE).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    value = data.get(course or "") if isinstance(data, dict) else None
    return value if isinstance(value, str) and value.strip().lower() in CHOICES else None


def resolve(root: Path, course: str | None, explicit: str | None, provider: str) -> str:
    return check(explicit or course_default(root, course) or DEFAULT, provider)


def gemini_codes(language: str) -> list[str]:
    return [LOCALES[language]]
