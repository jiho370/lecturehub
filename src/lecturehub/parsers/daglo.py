"""Read Daglo timestamp blocks without changing the source transcript."""

from ._blocks import parse_blocks
from .models import TranscriptSegment


def parse(text: str) -> list[TranscriptSegment]:
    return parse_blocks(text, engine="daglo")
