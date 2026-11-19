"""Immutable, engine-independent transcript parser output."""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Engine = Literal["daglo", "clova", "gemini_transcribe", "mai", "live_captions", "unknown"]


@dataclass(frozen=True, slots=True)
class TranscriptSegment:
    start_ms: int
    end_ms: int | None
    speaker: str | None
    text: str


@dataclass(frozen=True, slots=True)
class ParsedTranscript:
    engine: Engine
    segments: tuple[TranscriptSegment, ...]
    source_path: Path


class TranscriptParseError(ValueError):
    """A recognized transcript format did not contain usable timestamp blocks."""
