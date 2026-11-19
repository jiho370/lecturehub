"""Read MAI exports, removing speaker labels before timestamp extraction."""

from ._blocks import parse_blocks
from .models import TranscriptSegment


def parse(text: str) -> list[TranscriptSegment]:
    return parse_blocks(text, engine="mai")
