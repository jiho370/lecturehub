"""Integer time, safe paths and synthetic Unicode transcript regressions."""

import codecs
import re
from pathlib import Path

import pytest

from lecturehub.core.ids import new_ulid
from lecturehub.core.paths import app_dir, database_path
from lecturehub.core.textio import EncodingSuspectError, read_text_any
from lecturehub.core.timeutil import add_chunk_offset, format_timestamp, parse_timestamp


@pytest.mark.parametrize(
    ("timestamp", "expected"),
    [
        ("00:00", 0),
        ("01:30", 90_000),
        ("90:00", 5_400_000),
        ("1:29:59.9", 5_399_900),
        ("[01:23.456]", 83_456),
        (" [12:34] ", 754_000),
        ("2:03:04.01", 7_384_010),
    ],
)
def test_time_001_parse_integer_milliseconds(timestamp: str, expected: int) -> None:
    """TIME-001: Parse timestamps without a floating-point round trip."""
    assert parse_timestamp(timestamp) == expected


def test_time_001_floor_across_hour_boundary() -> None:
    """TIME-001: 1:29:59.9 remains in the 29th minute; the next second is 1:30."""
    local_ms = parse_timestamp("29:59.9")
    assert format_timestamp(add_chunk_offset(local_ms, 3_600_000)) == "1:29:59"
    assert format_timestamp(add_chunk_offset(local_ms + 100, 3_600_000)) == "1:30:00"
    assert format_timestamp(59_999) == "00:59"
    assert format_timestamp(3_599_999) == "59:59"
    assert format_timestamp(3_600_000) == "1:00:00"


def test_time_002_uses_actual_non_integer_minute_chunk_boundary() -> None:
    """TIME-002: Add the measured silence boundary, not nominal 20 minutes."""
    boundary_ms = 1_193_427
    local_ms = parse_timestamp("00:07.573")
    assert add_chunk_offset(local_ms, boundary_ms) == 1_201_000
    assert format_timestamp(add_chunk_offset(local_ms, boundary_ms)) == "20:01"


@pytest.mark.parametrize("value", ["1:60", "1:60:00", "-01:00", "[01:00", "01:00]", "Speaker 1 00:01"])
def test_rejects_invalid_or_unstripped_timestamp(value: str) -> None:
    with pytest.raises(ValueError):
        parse_timestamp(value)


@pytest.mark.parametrize("value", [1.5, True, "1000"])
def test_time_utilities_require_integer_milliseconds(value: object) -> None:
    with pytest.raises(TypeError):
        format_timestamp(value)
    with pytest.raises(TypeError):
        add_chunk_offset(0, value)


def test_time_utilities_reject_negative_positions() -> None:
    with pytest.raises(ValueError):
        format_timestamp(-1)
    with pytest.raises(ValueError):
        add_chunk_offset(-1, 10)


def test_ulid_is_canonical_and_unique() -> None:
    ids = [new_ulid() for _ in range(1000)]
    assert len(set(ids)) == 1000
    assert all(re.fullmatch(r"[0-7][0-9A-HJKMNP-TV-Z]{25}", value) for value in ids)


def test_paths_preserve_korean_spaces_parentheses_without_creating(tmp_path: Path) -> None:
    root = tmp_path / "대학 자료 (26-2)"
    assert app_dir(root) == root / "_lecturehub"
    assert database_path(root) == root / "_lecturehub" / "lecturehub.sqlite"
    assert not root.exists()


@pytest.mark.parametrize("bom", [b"", codecs.BOM_UTF8])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_parse_005_utf8_bom_and_line_endings(tmp_path: Path, bom: bytes, newline: str) -> None:
    """PARSE-005: BOM presence and Windows newlines do not change parsed text."""
    path = tmp_path / "합성 전사본 (1).txt"
    payload = bom + newline.join(["[00:00] 합성 문장입니다.", "[00:03] 두 번째 문장."]).encode("utf-8")
    path.write_bytes(payload)
    assert read_text_any(path) == "[00:00] 합성 문장입니다.\n[00:03] 두 번째 문장."
    assert path.read_bytes() == payload


def test_parse_005_cp949_has_explicit_error_without_mutating_source(tmp_path: Path) -> None:
    """PARSE-005: cp949 must be reported instead of silently decoded or replaced."""
    path = tmp_path / "합성 cp949.txt"
    payload = "[00:00] 합성 한글 문장입니다.".encode("cp949")
    path.write_bytes(payload)
    with pytest.raises(EncodingSuspectError, match="cp949"):
        read_text_any(path)
    assert path.read_bytes() == payload


@pytest.mark.parametrize("encoding", ["utf-16", "utf-32"])
def test_explicit_unicode_bom_is_honored(tmp_path: Path, encoding: str) -> None:
    path = tmp_path / "unicode.txt"
    path.write_bytes("합성 문장\r\n끝".encode(encoding))
    assert read_text_any(path) == "합성 문장\n끝"


def test_invalid_bytes_raise_without_lossy_replacement(tmp_path: Path) -> None:
    path = tmp_path / "invalid.txt"
    path.write_bytes(b"\xff\xff\xff")
    with pytest.raises(EncodingSuspectError, match="UTF-8"):
        read_text_any(path)
