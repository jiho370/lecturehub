"""Provider presets from lecturehub/presets/*.yaml (§8.3). Changes need LOOP-002 records."""
from __future__ import annotations

from pathlib import Path

import yaml

PRESET_DIR = Path(__file__).resolve().parents[1] / "presets"  # package data (inside the wheel)
ALIASES = {"gemini": "gemini_transcribe", "google": "gemini_transcribe", "mai": "mai_playground",
           "whisper": "whisper_groq", "groq": "whisper_groq"}


def load_preset(provider: str, directory: Path = PRESET_DIR) -> dict:
    name = ALIASES.get(provider, provider)
    path = directory / f"{name}.yaml"
    if not path.is_file():
        raise ValueError(f"프리셋이 없습니다: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or data.get("provider") != name:
        raise ValueError(f"프리셋 형식 오류: {path}")
    if not data.get("verified_at"):
        raise ValueError(f"프리셋에 verified_at이 없습니다(§8.3): {path}")
    return data
