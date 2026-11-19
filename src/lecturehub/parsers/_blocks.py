"""Shared timestamp block handling; no timestamp or end-time inference."""

import re

from lecturehub.core.timeutil import parse_timestamp

from .models import TranscriptParseError, TranscriptSegment

TIMESTAMP = r"\[?\d{1,3}:\d{2}(?::\d{2})?(?:\.\d{1,3})?\]?"
TIME_LINE = re.compile(rf"^\s*(?P<time>{TIMESTAMP})(?:[ \t]+(?P<body>.*))?\s*$")
MAI_SPEAKER = re.compile(r"^\s*(?:Speaker\s+(?P<long>\d+)|\[S(?P<short>\d+)\])\s*", re.IGNORECASE)
CLOVA_SPEAKER = re.compile(r"^\s*(?P<speaker>참석자\s*\d+)\s*")
# Clova Note text copied with the time first: "00:45 참석자 2", the words on the next lines (a classmate's
# export, 2026-10-09). Nothing else may follow the speaker, so "10:30 참석자 1 명만 오세요" stays speech.
CLOVA_TIME_FIRST = re.compile(rf"^\s*(?P<time>{TIMESTAMP})[ \t]+(?P<speaker>참석자\s*\d+)\s*$")
# The playground's own download puts time, speaker tag and words on one line: "12:34 [S1] 본문" (K1).
MAI_INLINE = re.compile(rf"^\s*(?P<time>{TIMESTAMP})[ \t]+(?:Speaker\s+(?P<long>\d+)|\[S(?P<short>\d+)\])"
                        r"[ \t]*(?P<body>.*?)\s*$", re.IGNORECASE)

# Bumped when a fix changes parse results of existing files (K1: 2026-09-29).
PARSER_VERSION = 2


def timestamp_line(line: str) -> re.Match | None:
    """A timestamp line holds the time alone (K1).

    Every engine export puts the time on its own line (26-2: 4,533 of 4,533
    lines). A line that starts with a time and continues with words, such as
    "10:30 에 다시 모입니다", is speech and must stay in the body.
    """
    match = TIME_LINE.fullmatch(line)
    if match and (match.group("body") or "").strip():
        return None
    return match


def split_mai_speaker(line: str) -> tuple[str | None, str]:
    """TIME-003: remove the speaker label *before* matching a timestamp."""
    match = MAI_SPEAKER.match(line)
    if not match:
        return None, line
    return f"S{match.group('long') or match.group('short')}", line[match.end():]


def parse_blocks(text: str, *, engine: str) -> list[TranscriptSegment]:
    segments: list[TranscriptSegment] = []
    start_ms: int | None = None
    speaker: str | None = None
    pending_speaker: str | None = None
    body: list[str] = []

    def finish() -> None:
        if start_ms is not None:
            segments.append(TranscriptSegment(start_ms, None, speaker, "\n".join(body).strip()))

    for line_number, raw_line in enumerate(text.lstrip("\ufeff").splitlines(), 1):
        line = raw_line
        line_speaker: str | None = None
        if engine == "mai":
            inline = MAI_INLINE.match(line)
            if inline:  # a speaker tag right after the time marks a timestamp, not speech (K1 exception)
                finish()
                try:
                    start_ms = parse_timestamp(inline.group("time"))
                except ValueError as exc:
                    raise TranscriptParseError(f"{engine}: invalid timestamp at line {line_number}") from exc
                speaker = f"S{inline.group('long') or inline.group('short')}"
                pending_speaker = None
                body = [inline.group("body")] if inline.group("body") else []
                continue
            line_speaker, line = split_mai_speaker(line)
        elif engine == "clova":
            time_first = CLOVA_TIME_FIRST.match(line)
            if time_first:
                finish()
                try:
                    start_ms = parse_timestamp(time_first.group("time"))
                except ValueError as exc:
                    raise TranscriptParseError(f"{engine}: invalid timestamp at line {line_number}") from exc
                speaker = re.sub(r"\s+", " ", time_first.group("speaker"))
                pending_speaker = None
                body = []
                continue
            match = CLOVA_SPEAKER.match(line)
            if match:
                line_speaker = re.sub(r"\s+", " ", match.group("speaker"))
                line = line[match.end():]
            if line.strip().lower() in {"clovanote.naver.com", "https://clovanote.naver.com"}:
                continue

        timestamp = timestamp_line(line)
        if timestamp:
            finish()
            try:
                start_ms = parse_timestamp(timestamp.group("time"))
            except ValueError as exc:
                raise TranscriptParseError(f"{engine}: invalid timestamp at line {line_number}") from exc
            speaker = line_speaker or pending_speaker
            pending_speaker = None
            inline = timestamp.group("body")
            body = [inline] if inline else []
            continue

        if line_speaker:
            if not line.strip():
                # Separate 'Speaker N' headers precede the next timestamp.
                pending_speaker = line_speaker
                continue
            if start_ms is not None:
                if any(part.strip() for part in body) and speaker != line_speaker:
                    # Multiple speakers may share a coarse timestamp.
                    finish()
                    body = []
                speaker = line_speaker
                pending_speaker = None
        elif pending_speaker and line.strip() and start_ms is not None:
            if any(part.strip() for part in body) and speaker != pending_speaker:
                finish()
                body = []
            speaker = pending_speaker
            pending_speaker = None
        if start_ms is not None:
            body.append(line)

    finish()
    if not segments:
        raise TranscriptParseError(f"{engine}: no timestamped segments found")
    return segments
