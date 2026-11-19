"""Read the app's own live caption files (``_lecturehub\\captions\\<날짜 시각> <과목>.txt``).

The first line is ``# 실시간 자막 · <과목> · <YYYY-MM-DD HH:MM> 시작``; every caption is one line,
``[MM:SS] words`` (time from the start of the captions). Other ``#`` lines are notes and are skipped.
"""

import re

from lecturehub.core.timeutil import parse_timestamp

from .models import TranscriptParseError, TranscriptSegment

HEADER = "# 실시간 자막"
LINE = re.compile(r"^\s*\[(?P<time>\d{1,3}:\d{2}(?::\d{2})?)\][ \t]*(?P<body>.*?)\s*$")


def is_captions(text: str) -> bool:
    first = next((line for line in text.lstrip("﻿").splitlines() if line.strip()), "")
    return first.strip().startswith(HEADER)


def parse(text: str) -> list[TranscriptSegment]:
    segments: list[TranscriptSegment] = []
    for number, line in enumerate(text.lstrip("﻿").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = LINE.match(line)
        if not match:
            raise TranscriptParseError(f"live_captions: line {number} has no [MM:SS]")
        if match.group("body"):
            segments.append(TranscriptSegment(parse_timestamp(match.group("time")), None, None, match.group("body")))
    if not segments:
        raise TranscriptParseError("live_captions: no caption lines found")
    return segments
