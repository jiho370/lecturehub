"""Read-only transcript parsing API shared by all tracks.

``parse_file(path)`` returns engine metadata and immutable segments.
``parse_files(paths)`` removes identical-byte inputs with a warning first.
Unknown formats return ``engine='unknown'`` with no inferred segments.
"""

from collections.abc import Iterable
from pathlib import Path

from lecturehub.core.textio import decode_text

from . import clova, daglo, gemini_transcribe, live_captions, mai
from .dedupe import deduplicate_files
from .detect import detect_engine
from .models import ParsedTranscript, TranscriptParseError, TranscriptSegment

__all__ = [
    "ParsedTranscript", "TranscriptParseError", "TranscriptSegment",
    "detect_engine", "parse_file", "parse_files",
]

_PARSERS = {"daglo": daglo.parse, "clova": clova.parse, "gemini_transcribe": gemini_transcribe.parse, "mai": mai.parse,
            "live_captions": live_captions.parse}


def parse_file(path: str | Path) -> ParsedTranscript:
    path = Path(path)
    data = path.read_bytes()
    text = decode_text(data, source=path)
    engine = detect_engine(data)
    segments = tuple(_PARSERS[engine](text)) if engine in _PARSERS else ()
    return ParsedTranscript(engine=engine, segments=segments, source_path=path)


def parse_files(paths: Iterable[str | Path]) -> list[ParsedTranscript]:
    return [parse_file(path) for path in deduplicate_files(paths)]
