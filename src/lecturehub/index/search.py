"""Literal Korean text search over derived FTS5 transcript/material indexes."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class SearchResult:
    source_kind: str
    source_id: str
    course_id: str
    course: str
    date: str | None
    lecture_id: str | None
    start_ms: int | None
    engine: str | None
    text: str
    context_before: str
    context_after: str
    page_no: int | None
    path: str | None
    no_media: bool = False


def rebuild_search_index(connection: sqlite3.Connection) -> int:
    """Rebuild derived documents without touching source rows or committing.

    Migration 0002 installs incremental source-table triggers. A manual rebuild
    repairs the derived index and is safe to run repeatedly within a transaction.
    """
    if not connection.in_transaction:
        connection.execute("BEGIN")
    connection.execute("SAVEPOINT rebuild_search_index")
    try:
        connection.execute("DELETE FROM SearchIndex")
        connection.execute(
            "INSERT INTO SearchIndex(id, source_kind, source_id, position, text) "
            "SELECT id, 'transcript', transcript_id, seq, text FROM TranscriptSegment"
        )
        connection.execute(
            "INSERT INTO SearchIndex(id, source_kind, source_id, position, text) "
            "SELECT id, 'material', material_id, page_no, text FROM MaterialPage"
        )
        count = connection.execute("SELECT count(*) FROM SearchIndex").fetchone()[0]
    except BaseException:
        connection.execute("ROLLBACK TO rebuild_search_index")
        connection.execute("RELEASE rebuild_search_index")
        raise
    connection.execute("RELEASE rebuild_search_index")
    return count


def _neighbor(
    connection: sqlite3.Connection, source_kind: str, source_id: str, position: int, *, before: bool
) -> str:
    # Identifiers below are fixed application constants, never user query text.
    table, parent, ordinal = (
        ("TranscriptSegment", "transcript_id", "seq")
        if source_kind == "transcript"
        else ("MaterialPage", "material_id", "page_no")
    )
    comparison, order = ("<", "DESC") if before else (">", "ASC")
    row = connection.execute(
        f"SELECT text FROM {table} WHERE {parent} = ? AND {ordinal} {comparison} ? "
        f"ORDER BY {ordinal} {order} LIMIT 1",
        (source_id, position),
    ).fetchone()
    return row[0] if row else ""


def search(connection: sqlite3.Connection, query: str, *, limit: int = 50) -> list[SearchResult]:
    """Find literal substrings, with nearby source text and navigation metadata.

    Queries of at least three characters use FTS5 trigram. Short queries use
    LIKE with escaped wildcard characters. FTS operators supplied by the user
    are quoted as literal text, rather than interpreted as a query language.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer")
    query = query.strip()
    if not query:
        return []
    if len(query) < 3:
        pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        documents = connection.execute(
            "SELECT source_kind, source_id, position, text FROM SearchIndex "
            "WHERE text LIKE ? ESCAPE '\\' ORDER BY source_kind, source_id, CAST(position AS INTEGER) LIMIT ?",
            (pattern, limit),
        ).fetchall()
    else:
        expression = '"' + query.replace('"', '""') + '"'
        documents = connection.execute(
            "SELECT source_kind, source_id, position, text FROM SearchIndex "
            "WHERE SearchIndex MATCH ? ORDER BY rank, source_kind, source_id, CAST(position AS INTEGER) LIMIT ?",
            (expression, limit),
        ).fetchall()
    results = []
    for kind, source_id, raw_position, text in documents:
        position = int(raw_position)
        before = _neighbor(connection, kind, source_id, position, before=True)
        after = _neighbor(connection, kind, source_id, position, before=False)
        if kind == "transcript":
            row = connection.execute(
                "SELECT c.id, c.folder_name, l.date, l.id, s.start_ms, t.engine, t.source_file, t.media_id "
                "FROM TranscriptSegment s JOIN Transcript t ON t.id = s.transcript_id "
                "JOIN Lecture l ON l.id = t.lecture_id JOIN Course c ON c.id = l.course_id "
                "WHERE s.transcript_id = ? AND s.seq = ?",
                (source_id, position),
            ).fetchone()
            if row:
                results.append(
                    SearchResult(kind, source_id, row[0], row[1], row[2], row[3], row[4], row[5],
                                 text, before, after, None, row[6], no_media=row[7] is None)
                )
        elif kind == "material":
            rows = connection.execute(
                "SELECT DISTINCT c.id, c.folder_name, l.date, l.id, m.path "
                "FROM Material m JOIN Course c ON c.id = m.course_id "
                "LEFT JOIN MaterialMatch mm ON mm.material_id = m.id AND ? BETWEEN mm.page_from AND mm.page_to "
                "LEFT JOIN Lecture l ON l.id = mm.lecture_id WHERE m.id = ? ORDER BY l.date DESC, l.id",
                (position, source_id),
            ).fetchall()
            for row in rows:
                results.append(
                    SearchResult(kind, source_id, row[0], row[1], row[2], row[3], None, None,
                                 text, before, after, position, row[4])
                )
        if len(results) >= limit:
            break
    return results[:limit]
