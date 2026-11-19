"""Re-parse indexed transcripts with the current parser and compare (§0.3).

Raw files and stored segments are never changed here. A difference means a
parser fix affects existing derived results; those must be rebuilt as a new
version with a supersedes link (proposal P-001 in DECISIONS.md).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from . import parse_file
from ._blocks import PARSER_VERSION


@dataclass
class RevalidationReport:
    parser_version: int = PARSER_VERSION
    checked: int = 0
    changed: list[str] = field(default_factory=list)   # transcript ids
    missing: list[str] = field(default_factory=list)   # source file not found
    skipped: int = 0                                   # no source file or no stored segments

    def as_payload(self) -> dict:
        return {"parser_version": self.parser_version, "checked": self.checked,
                "changed": self.changed, "missing": self.missing, "skipped": self.skipped}


def revalidate(connection: sqlite3.Connection, root: Path) -> RevalidationReport:
    report = RevalidationReport()
    for transcript_id, source in connection.execute("SELECT id, source_file FROM Transcript ORDER BY id"):
        if not source:
            report.skipped += 1
            continue
        path = Path(source) if Path(source).is_absolute() else Path(root) / source
        if not path.is_file():
            report.missing.append(transcript_id)
            continue
        stored = [tuple(row) for row in connection.execute(
            "SELECT start_ms, text FROM TranscriptSegment WHERE transcript_id=? ORDER BY seq", (transcript_id,))]
        if not stored:
            report.skipped += 1
            continue
        report.checked += 1
        if [(s.start_ms, s.text) for s in parse_file(path).segments] != stored:
            report.changed.append(transcript_id)
    return report
