"""Read Clova Note exports, including exports with speaker labels hidden."""

from ._blocks import parse_blocks
from .models import TranscriptSegment


def parse(text: str) -> list[TranscriptSegment]:
    return parse_blocks(text, engine="clova")
