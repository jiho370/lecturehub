"""What differs per transcription provider: job folder, engine name, output name, raw parsing, extra QA.

Google jobs keep ``_lecturehub\\jobs`` (the app watches that folder and treats every job there as
"this recording is already being transcribed by Google"). Other engines get their own folder so
a Whisper or MAI job never blocks, resumes or hides a Google job (ad60211).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .store import app_dir


@dataclass(frozen=True)
class Provider:
    name: str
    engine: str            # Transcript.engine
    role: str              # Transcript.role default (§8.3 role_default)
    job_folder: str        # under _lecturehub
    output_suffix: str     # "<stem><suffix>.txt" in _lecturehub\out
    output_format: str     # parser format the merged output is written in
    extra_qa: Callable[[dict], list] = field(default=lambda raw: [])

    def parse_raw(self, raw: dict):
        if self.name == "whisper_groq":
            from .groq import parse_result
        else:
            from .gemini import parse_result
        return parse_result(raw)


def _groq_qa(raw: dict) -> list:
    from .groq import extra_qa

    return extra_qa(raw)


PROVIDERS = {
    "gemini_transcribe": Provider("gemini_transcribe", "gemini_transcribe", "timeline", "jobs", "",
                                  "gemini_transcribe"),
    "whisper_groq": Provider("whisper_groq", "whisper", "reference", "jobs_whisper", " (Whisper)",
                             "gemini_transcribe", _groq_qa),
    "mai_playground": Provider("mai_playground", "mai", "content", "jobs_mai", " (MAI)", "mai"),
}


def provider(name: str) -> Provider:
    try:
        return PROVIDERS[name]
    except KeyError:
        raise ValueError(f"알 수 없는 전사 제공자: {name}") from None


def job_roots(root: Path) -> list[Path]:
    """Every job folder root, Google first."""
    seen, out = set(), []
    for item in PROVIDERS.values():
        if item.job_folder not in seen:
            seen.add(item.job_folder)
            out.append(app_dir(root) / item.job_folder)
    return out


def find_job_dir(root: Path, job_id: str) -> Path | None:
    for base in job_roots(root):
        if (base / job_id / "state.json").is_file():
            return base / job_id
    return None
