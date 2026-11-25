"""What a MaterialPage number means for a material (D-078). No imports: the AI app link (mcp) uses it.

PDF and slides have real pages. Word processor files (.hwp, .hwpx, .docx) have no fixed pages (they depend on
the printer and fonts), so their text is stored in pieces of about ``CHUNK_CHARS`` characters, a new piece at
each section; the number of such a piece is a 구간 (section), not a 쪽, wherever it is shown.
"""
from __future__ import annotations

CHUNKED_KINDS = frozenset({"hwp", "hwpx", "docx"})
CHUNK_CHARS = 2_000
CHUNK_NOTE = (f"한글·워드 문서는 쪽이 정해져 있지 않아 약 {CHUNK_CHARS:,}자씩(구역마다 새로) 나눈 구간 번호입니다. "
              "인쇄했을 때의 쪽 번호와 다릅니다.")


def is_chunked(kind_or_path: str | None) -> bool:
    """``kind`` as stored in Material ("hwp") or a file name/path ("공지.HWP")."""
    text = str(kind_or_path or "").lower()
    return text.rsplit(".", 1)[-1] in CHUNKED_KINDS


def page_unit(kind_or_path: str | None) -> str:
    return "구간" if is_chunked(kind_or_path) else "쪽"
