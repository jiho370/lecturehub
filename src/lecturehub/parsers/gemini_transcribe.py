"""Read the existing Gemini Transcribe script's timestamp blocks."""

from ._blocks import parse_blocks
from .models import TranscriptSegment


def parse(text: str) -> list[TranscriptSegment]:
    return parse_blocks(text, engine="gemini_transcribe")
