"""Detect engines from source bytes, never from filename suffixes (§11)."""

import codecs
import re

from lecturehub.core.textio import decode_text

from ._blocks import CLOVA_SPEAKER, CLOVA_TIME_FIRST, MAI_INLINE, split_mai_speaker, timestamp_line
from .live_captions import is_captions
from .models import Engine


def detect_engine(data: bytes) -> Engine:
    """Return unknown when format evidence does not identify an engine.

    Preserve bytes until detection: universal-newline text reads erase the
    BOM/CRLF distinction used by the existing Transcribe pipeline.
    """
    text = decode_text(data)
    if is_captions(text):  # the app's own live captions: "# 실시간 자막 …" then "[MM:SS] words" lines
        return "live_captions"
    lines = text.splitlines()
    bom = data.startswith((codecs.BOM_UTF8, codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE,
                           codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE))
    has_time = any(timestamp_line(line) for line in lines)
    structural_lines = [line for line in lines if line.strip()]
    if any(MAI_INLINE.match(line) for line in structural_lines):  # "12:34 [S1] 본문" (playground download, K1)
        return "mai"
    for index, line in enumerate(structural_lines):
        speaker, remainder = split_mai_speaker(line)
        if speaker and (
            timestamp_line(remainder)
            or (index > 0 and timestamp_line(structural_lines[index - 1]))
            or (not remainder and index + 1 < len(structural_lines)
                and timestamp_line(structural_lines[index + 1]))
        ):
            return "mai"

    if sum(1 for line in structural_lines if CLOVA_TIME_FIRST.match(line)) >= 2:  # "00:45 참석자 2" lines
        return "clova"

    header = "\n".join(lines[:10])
    clova_header = re.search(r"\d{4}[./-]\d{1,2}[./-]\d{1,2}", header) and re.search(
        r"\d+\s*분(?:\s*\d+\s*초)?", header
    )
    clova_speakers = any(
        (match := CLOVA_SPEAKER.match(line)) and timestamp_line(line[match.end():])
        for line in lines
    )
    if bom and ((clova_header and has_time) or clova_speakers):
        return "clova"
    if data.startswith(codecs.BOM_UTF8) and b"\r\n" in data and has_time:
        return "gemini_transcribe"
    if not bom and any(
        (match := timestamp_line(line))
        and re.match(r"[ \t]", line.lstrip()[len(match.group("time")):])
        for line in lines
    ):
        return "daglo"
    return "unknown"
