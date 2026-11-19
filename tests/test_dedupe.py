"""DEDUP-001: identical-byte transcript inputs are counted once with a warning."""

import hashlib

import pytest

from lecturehub.parsers import parse_files
from lecturehub.parsers.dedupe import DuplicateTranscriptWarning, deduplicate_files, sha256_file


def test_DEDUP_001_identical_bytes_are_one_transcript_and_warn(tmp_path):
    first = tmp_path / "합성 다글로.txt"
    duplicate = tmp_path / "다른 이름 (Transcribe).txt"
    content = "00:01 \n중복 검증용 합성 문장입니다.\n".encode()
    first.write_bytes(content)
    duplicate.write_bytes(content)
    with pytest.warns(DuplicateTranscriptWarning, match="same sha256"):
        parsed = parse_files([first, duplicate])
    assert len(parsed) == 1
    assert parsed[0].source_path == first
    assert parsed[0].engine == "daglo"
    assert sha256_file(first) == hashlib.sha256(content).hexdigest()
    assert first.read_bytes() == duplicate.read_bytes() == content


def test_DEDUP_001_byte_differences_are_not_silently_collapsed(tmp_path):
    first = tmp_path / "합성 LF.txt"
    second = tmp_path / "합성 CRLF.txt"
    first.write_bytes(b"00:01 \nsynthetic text\n")
    second.write_bytes(b"00:01 \r\nsynthetic text\r\n")
    assert deduplicate_files([first, second]) == [first, second]
