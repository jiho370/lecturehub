"""Approved B-001--004 and B-2 migrations, using synthetic v0 records."""

import json
import re
import shutil
import sqlite3
from contextlib import closing
from importlib import resources

import pytest
from ulid import ULID

from lecturehub.core import db
from lecturehub.core.ids import new_ulid


def insert(connection, table, **values):
    columns = ", ".join(f'"{column}"' for column in values)
    placeholders = ", ".join("?" for _ in values)
    connection.execute(f'INSERT INTO "{table}" ({columns}) VALUES ({placeholders})', tuple(values.values()))


def seed_legacy(connection):
    """The old schema allows synthetic non-ULID IDs; migration preserves them."""
    insert(connection, "Course", id="course", code="C", folder_name="합성", abbr="su", term="26-2")
    insert(connection, "Lecture", id="lecture", course_id="course", date="2026-09-29", session_index=1)
    insert(connection, "MediaAsset", id="media", lecture_id="lecture", path="합성.wav", sha256="audio-hash",
           duration_ms=5000, codec="pcm", sample_rate=16000, channels=1, bytes=160000, origin="app_mic",
           device_type="laptop", is_master=1, recorded_start=1000, partial=0, loudness_profile="[]",
           decided_by="user", confidence_class="exact")
    insert(connection, "RecordingSession", id="session", lecture_id="lecture", media_id="media", mode="mic",
           device_name="synthetic", started_at=1000, heartbeat_path="heartbeat.json", segments="[]", gaps="[]")
    insert(connection, "Transcript", id="transcript", lecture_id="lecture", media_id="media", engine="synthetic",
           role="timeline", role_decided_by="user", status="done", source_file="합성.txt", sha256="text-hash")
    insert(connection, "TranscriptSegment", transcript_id="transcript", seq=0, start_ms=0, end_ms=1000,
           text="구조역학 합성 원문", chunk_index=0)
    insert(connection, "Material", id="material", course_id="course", path="합성.pdf", sha256="pdf-hash",
           kind="pdf", title="합성", page_count=1)
    insert(connection, "MaterialPage", material_id="material", page_no=1, text="구조역학 합성 자료", terms="[]")
    insert(connection, "StudyArtifact", id="artifact", lecture_ids='["lecture"]', path="합성.html", kind="note",
           version=1, mode="new", inputs='{}', created_by="user", qa_status="pending")
    insert(connection, "CanonicalTranscript", id="canonical", lecture_id="lecture", version=1, created_at=1000,
           created_by="pipeline", inputs="[]", material_ids="[]", stage_reached="C0", status="done")
    insert(connection, "QuizSet", id="quiz-set", course_id="course", scope="{}", created_by="ai", created_at=1000,
           generator_version="synthetic")
    insert(connection, "QuizItem", id="quiz", quiz_set_id="quiz-set", type="short", question="합성 질문",
           answer="합성 답", explanation="합성 설명", sources="[]", quoted_evidence="구조역학", difficulty="easy",
           flags="[]", status="active")
    insert(connection, "Bookmark", timestamp=1234, optional_label="합성 북마크")
    insert(connection, "AlignmentAnchor", lecture_id="lecture", start_time_ms=0, end_time_ms=1000,
           transcript_segment_id="legacy-opaque", material_id="material", page_or_slide=1,
           study_artifact_anchor="#legacy", confidence="exact", provenance="manual")
    insert(connection, "Event", id="event", at=1234, type="SYNTHETIC", subject_type="Lecture",
           subject_id="lecture", payload_json='{"합성": true}')
    connection.commit()


@pytest.fixture(scope="module")
def legacy_template(tmp_path_factory):
    path = tmp_path_factory.mktemp("legacy_template") / "synthetic.sqlite"
    with closing(db.connect(path)) as connection:
        connection.execute("BEGIN")  # build the synthetic old schema in one transaction, not one fsync per DDL
        connection.execute("CREATE TABLE schema_version(version INTEGER PRIMARY KEY)")
        script = resources.files("lecturehub.core").joinpath("migrations/0001_init.sql").read_text(encoding="utf-8")
        for statement in db._statements(script):
            connection.execute(statement)
        connection.execute("INSERT INTO schema_version VALUES (1)")
        seed_legacy(connection)
    return path


@pytest.fixture
def legacy(tmp_path, monkeypatch, legacy_template):
    # Each migration still runs against its own populated database and backup directory.
    monkeypatch.setattr(db, "_MIGRATION_NAME", re.compile(r"(000[12])_.+\.sql\Z"))
    path = tmp_path / "마이그레이션 (합성).sqlite"
    shutil.copyfile(legacy_template, path)
    with closing(db.connect(path)) as connection:
        yield connection


@pytest.fixture
def upgraded(legacy):
    assert db.migrate(legacy) == [2]
    return legacy


def domain_tables(connection):
    return [row[0] for row in connection.execute(
        "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'SearchIndex%' ORDER BY name"
    )]


def test_v0_upgrade_preserves_every_existing_column_and_stable_ids(legacy):
    """B-001/B-002/CANON-001/EVENT-001: a real v0 upgrade preserves evidence."""
    before = {}
    for table in domain_tables(legacy):
        columns = [row[1] for row in legacy.execute(f'PRAGMA table_info("{table}")')]
        before[table] = (columns, [tuple(row) for row in legacy.execute(f'SELECT * FROM "{table}"')])
    assert db.migrate(legacy) == [2]
    for table, (columns, rows) in before.items():
        projection = ", ".join(f'"{column}"' for column in columns)
        query = f'SELECT {projection} FROM "{table}"'
        if table == "schema_version":
            query += " WHERE version = 1"
        assert [tuple(row) for row in legacy.execute(query)] == rows, table
    identities = {}
    for table in domain_tables(legacy):
        values = [row[0] for row in legacy.execute(f'SELECT id FROM "{table}"')]
        assert all(value is not None for value in values), table
        if "id" not in before[table][0]:
            assert all(str(ULID.from_str(value)) == value for value in values), table
        identities[table] = values
    assert db.migrate(legacy) == []
    for table, values in identities.items():
        assert [row[0] for row in legacy.execute(f'SELECT id FROM "{table}"')] == values
    assert legacy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert legacy.execute("PRAGMA foreign_key_check").fetchall() == []
    assert legacy.execute("SELECT external_blocks FROM QuizItem").fetchone()[0] == "[]"
    assert legacy.execute("SELECT offset_ms FROM Bookmark").fetchone()[0] == 1234


def test_failed_backfill_rolls_back_columns_and_restores_raw_guards(legacy, monkeypatch):
    """B-002/CANON-001: a failed ULID backfill never leaves evidence writable."""
    original = db.new_ulid

    def broken_generator():
        raise RuntimeError("synthetic generator failure")

    monkeypatch.setattr(db, "new_ulid", broken_generator)
    with pytest.raises(sqlite3.OperationalError):
        db.migrate(legacy)
    assert [row[0] for row in legacy.execute("SELECT version FROM schema_version")] == [1]
    assert "id" not in {row[1] for row in legacy.execute("PRAGMA table_info(TranscriptSegment)")}
    assert "external_blocks" not in {row[1] for row in legacy.execute("PRAGMA table_info(QuizItem)")}
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        legacy.execute("UPDATE TranscriptSegment SET text='changed'")
    legacy.rollback()
    monkeypatch.setattr(db, "new_ulid", original)
    assert db.migrate(legacy) == [2]


@pytest.mark.parametrize("table,values", [
    ("Schedule", {"course_id": "course", "weekday": 1, "start": 1000, "end": 2000}),
    ("ScheduleException", {"course_id": "course", "date": "2026-09-29", "kind": "휴강", "note": "합성"}),
    ("StopReport", {"session_id": "session", "cause": "user_stop", "certainty": "confirmed", "at_ms": 1234,
                    "recovered": 0}),
    ("RecordingAnomaly", {"session_id": "session", "kind": "dropout", "from_ms": 100, "certainty": "estimated"}),
    ("MoveLog", {"from_path": "a", "to_path": "b", "at": 1000, "estimated_start": 0, "estimated_end": 1000}),
    ("TranscriptChunkRun", {"transcript_id": "transcript", "chunk_index": 0, "attempt": 1, "state": "queued",
                            "raw_output_path": "raw.txt"}),
    ("TranscriptSegment", {"transcript_id": "transcript", "seq": 1, "start_ms": 1000, "text": "추가 합성",
                           "chunk_index": 0}),
    ("TranscriptQA", {"transcript_id": "transcript", "kind": "coverage", "detail": "합성", "severity": "low"}),
    ("CanonicalSegment", {"canonical_id": "canonical", "seq": 0, "start_ms": 0, "end_ms": 1000,
                          "time_precision": "single_source", "text": "합성", "confidence_class": "low",
                          "flags": "[]", "source_spans": "[]"}),
    ("MaterialPage", {"material_id": "material", "page_no": 2, "text": "추가 자료", "terms": "[]"}),
    ("MaterialMatch", {"lecture_id": "lecture", "material_id": "material", "page_from": 1, "page_to": 1,
                       "confidence_class": "exact", "decided_by": "user"}),
    ("Syllabus", {"course_id": "course", "week": 1, "topic": "합성"}),
    ("ProviderConsent", {"provider": "synthetic", "granted_at": 1000, "note": "합성"}),
    ("QuotaLedger", {"provider": "synthetic", "date": "2026-09-29", "requests": 0, "audio_seconds": 0}),
    ("Bookmark", {"lecture_id": "lecture", "media_id": "media", "offset_ms": 2000}),
    ("AlignmentAnchor", {"lecture_id": "lecture", "start_ms": 2000, "end_ms": 2500, "provenance": "manual"}),
    ("SlideViewEvent", {"session_id": "session", "material_id": "material", "page_no": 1, "at_ms": 1000}),
    ("QuizAttempt", {"quiz_item_id": "quiz", "at": 1000, "answer_given": "합성", "correct": 1,
                     "review_state": "mastered"}),
    ("ExamOutcome", {"course_id": "course", "exam": "중간", "topic": "합성", "appeared": 1,
                     "recorded_by": "user", "at": 1000}),
    ("EngineMetricAggregate", {"engine": "synthetic", "metric_name": "coverage", "scope": "course",
                               "scope_value": "course", "value": 0.5, "n": 1, "method": "synthetic",
                               "calculated_at": 1000}),
    ("schema_version", {"version": 999}),
])
def test_every_previously_idless_table_generates_stable_ids_and_supports_evidence(upgraded, table, values):
    """B-002: every previously unaddressable domain row now has a stable ULID."""
    old = {row[0] for row in upgraded.execute(f'SELECT id FROM "{table}"')}
    insert(upgraded, table, **values)
    added = {row[0] for row in upgraded.execute(f'SELECT id FROM "{table}"')} - old
    assert len(added) == 1
    identity = added.pop()
    assert str(ULID.from_str(identity)) == identity
    insert(upgraded, "Evidence", subject_type=table, subject_id=identity, kind="user", detail="합성 근거",
           source="user", created_at=1000)
    assert upgraded.execute("SELECT subject_id FROM Evidence").fetchone()[0] == identity
    with pytest.raises(sqlite3.IntegrityError):
        upgraded.execute(f'UPDATE "{table}" SET id = ? WHERE id = ?', (new_ulid(), identity))


def test_explicit_id_and_complete_alignment_references(upgraded):
    """B-001: segment, material page and artifact section references coexist."""
    identity = new_ulid()
    insert(upgraded, "AlignmentAnchor", id=identity, lecture_id="lecture", start_ms=0, end_ms=1000,
           transcript_id="transcript", seq=0, material_id="material", page=1,
           artifact_id="artifact", artifact_anchor="#sec12", provenance="manual")
    row = upgraded.execute("SELECT * FROM AlignmentAnchor WHERE id = ?", (identity,)).fetchone()
    assert (row["start_time_ms"], row["end_time_ms"], row["page_or_slide"], row["confidence"]) == (0, 1000, 1, "none")
    with pytest.raises(sqlite3.IntegrityError, match="segment reference"):
        upgraded.execute("UPDATE AlignmentAnchor SET seq = 99 WHERE id = ?", (identity,))
    with pytest.raises(sqlite3.IntegrityError, match="#secN"):
        upgraded.execute("UPDATE AlignmentAnchor SET artifact_anchor = '#sec1bad' WHERE id = ?", (identity,))
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        upgraded.execute("UPDATE AlignmentAnchor SET artifact_id = 'missing' WHERE id = ?", (identity,))


def test_canonical_and_legacy_time_columns_stay_synchronized(upgraded):
    """B-001: editing either supported time spelling keeps both views coherent."""
    bookmark = upgraded.execute("SELECT id FROM Bookmark").fetchone()[0]
    upgraded.execute("UPDATE Bookmark SET offset_ms=2500 WHERE id=?", (bookmark,))
    assert upgraded.execute("SELECT timestamp,offset_ms FROM Bookmark").fetchone()[:] == (2500, 2500)
    upgraded.execute("UPDATE Bookmark SET timestamp=3000 WHERE id=?", (bookmark,))
    assert upgraded.execute("SELECT timestamp,offset_ms FROM Bookmark").fetchone()[:] == (3000, 3000)
    upgraded.execute("UPDATE AlignmentAnchor SET end_ms=2000")
    assert upgraded.execute("SELECT end_ms,end_time_ms FROM AlignmentAnchor").fetchone()[:] == (2000, 2000)


@pytest.mark.parametrize("table,assignments", [
    ("Bookmark", "timestamp=2000, offset_ms=3000"),
    ("AlignmentAnchor", "start_time_ms=100, start_ms=200"),
    ("AlignmentAnchor", "end_time_ms=2000, end_ms=3000"),
    ("AlignmentAnchor", "page_or_slide=2, page=3"),
])
def test_updating_both_aliases_to_conflicting_values_is_atomic(upgraded, table, assignments):
    """B-001: conflicting dual-name edits never silently discard a supplied value."""
    before = [tuple(row) for row in upgraded.execute(f"SELECT * FROM {table}")]
    with pytest.raises(sqlite3.IntegrityError, match="Conflicting"):
        upgraded.execute(f"UPDATE {table} SET {assignments}")
    assert [tuple(row) for row in upgraded.execute(f"SELECT * FROM {table}")] == before


def test_updating_both_aliases_to_matching_values_remains_allowed(upgraded):
    """B-001: aligned dual-name updates remain valid for compatibility clients."""
    upgraded.execute("UPDATE Bookmark SET timestamp=2000, offset_ms=2000")
    upgraded.execute("UPDATE AlignmentAnchor SET end_time_ms=2000, end_ms=2000, page_or_slide=2, page=2")
    assert upgraded.execute("SELECT timestamp,offset_ms FROM Bookmark").fetchone()[:] == (2000, 2000)
    assert upgraded.execute("SELECT end_ms,end_time_ms,page,page_or_slide FROM AlignmentAnchor").fetchone()[:] == (
        2000, 2000, 2, 2,
    )


def test_references_cannot_cross_lectures(upgraded):
    """B-001: paired references must continue to belong to their lecture."""
    insert(upgraded, "Lecture", id="other-lecture", course_id="course", date="2026-09-30", session_index=1)
    with pytest.raises(sqlite3.IntegrityError, match="segment reference"):
        insert(upgraded, "AlignmentAnchor", lecture_id="other-lecture", start_ms=0, end_ms=1000,
               transcript_id="transcript", seq=0, provenance="manual")
    insert(upgraded, "Bookmark", lecture_id="lecture", media_id="media", offset_ms=1000)
    with pytest.raises(sqlite3.IntegrityError, match="existing bookmark"):
        upgraded.execute("UPDATE MediaAsset SET lecture_id='other-lecture' WHERE id='media'")


@pytest.mark.parametrize("blocks", [
    [{"text": "합성", "confidence": "high", "source_kind": "general"}],
    [{"text": "합성", "confidence": "mid", "source_kind": "web", "source_ref": "https://example.com/"}],
])
def test_external_blocks_round_trip(upgraded, blocks):
    """B-003/SRC-001: source metadata is retained as UTF-8 JSON."""
    upgraded.execute("UPDATE QuizItem SET external_blocks=?", (json.dumps(blocks, ensure_ascii=False),))
    assert json.loads(upgraded.execute("SELECT external_blocks FROM QuizItem").fetchone()[0]) == blocks


@pytest.mark.parametrize("blocks", [
    {}, ["text only"], [{"text": "합성", "source_kind": "general"}],
    [{"text": "합성", "confidence": "high"}],
    [{"text": "합성", "confidence": "certain", "source_kind": "general"}],
])
def test_external_blocks_require_honesty_metadata(upgraded, blocks):
    """B-003/SRC-001: malformed or unlabelled external knowledge is rejected."""
    with pytest.raises(sqlite3.DatabaseError):
        upgraded.execute("UPDATE QuizItem SET external_blocks=?", (json.dumps(blocks),))


def test_chunk_run_cannot_finish_without_real_start(upgraded):
    """B-004: queued/running unknown times remain NULL, never fabricated."""
    values = {"transcript_id": "transcript", "chunk_index": 0, "attempt": 1,
              "state": "queued", "raw_output_path": "raw.txt"}
    with pytest.raises(sqlite3.IntegrityError, match="real start"):
        insert(upgraded, "TranscriptChunkRun", **values, ended_at=2000)
    insert(upgraded, "TranscriptChunkRun", **values)
    with pytest.raises(sqlite3.IntegrityError, match="real start"):
        upgraded.execute("UPDATE TranscriptChunkRun SET ended_at=2000")
    upgraded.execute("UPDATE TranscriptChunkRun SET state='running',started_at=1000")
    upgraded.execute("UPDATE TranscriptChunkRun SET state='done',ended_at=2000")
    with pytest.raises(sqlite3.IntegrityError):
        upgraded.execute("UPDATE TranscriptChunkRun SET ended_at=999")


@pytest.mark.parametrize("statement", [
    "UPDATE TranscriptSegment SET text='changed'", "DELETE FROM TranscriptSegment",
    "INSERT OR REPLACE INTO TranscriptSegment SELECT * FROM TranscriptSegment",
    "UPDATE Transcript SET sha256='changed'", "UPDATE MediaAsset SET path='changed'",
    "UPDATE Event SET payload_json='{}'", "DELETE FROM Event",
    "INSERT OR REPLACE INTO Event SELECT * FROM Event",
])
def test_immutability_survives_id_backfill(upgraded, statement):
    """CANON-001/EVENT-001: ID backfill leaves every raw-evidence guard intact."""
    before = {table: [tuple(row) for row in upgraded.execute(f"SELECT * FROM {table}")]
              for table in ("TranscriptSegment", "Transcript", "MediaAsset", "Event")}
    upgraded.execute("PRAGMA recursive_triggers=OFF")
    with pytest.raises(sqlite3.IntegrityError):
        upgraded.execute(statement)
    for table, rows in before.items():
        assert [tuple(row) for row in upgraded.execute(f"SELECT * FROM {table}")] == rows


def test_fts_backfill_and_material_change_delete_are_synchronized(upgraded):
    """B-2/SEARCH-001: existing and future text is available to trigram search."""
    assert upgraded.execute("SELECT count(*) FROM SearchIndex WHERE SearchIndex MATCH '구조역학'").fetchone()[0] == 2
    insert(upgraded, "TranscriptSegment", transcript_id="transcript", seq=1, start_ms=1000,
           text="새로운 합성구문", chunk_index=0)
    assert upgraded.execute("SELECT count(*) FROM SearchIndex WHERE SearchIndex MATCH '합성구문'").fetchone()[0] == 1
    upgraded.execute("UPDATE MaterialPage SET text='변경된 새로운자료'")
    assert upgraded.execute("SELECT count(*) FROM SearchIndex WHERE SearchIndex MATCH '새로운자료'").fetchone()[0] == 1
    upgraded.execute("DELETE FROM MaterialPage")
    assert upgraded.execute("SELECT count(*) FROM SearchIndex WHERE source_kind='material'").fetchone()[0] == 0
    ids = upgraded.execute("SELECT id FROM SearchIndex").fetchall()
    assert all(str(ULID.from_str(row[0])) == row[0] for row in ids)


@pytest.mark.parametrize("operation", ["update_destination", "insert_reuse_id", "insert_id_and_destination"])
def test_fts_material_replace_has_no_stale_id_or_destination_entries(upgraded, operation):
    """B-2/SEARCH-001: REPLACE stays synchronized even with recursive triggers off."""
    upgraded.execute("PRAGMA recursive_triggers=OFF")
    insert(upgraded, "MaterialPage", material_id="material", page_no=2, text="두번째합성", terms="[]")
    page_id = upgraded.execute("SELECT id FROM MaterialPage WHERE page_no=2").fetchone()[0]
    if operation == "update_destination":
        upgraded.execute("UPDATE OR REPLACE MaterialPage SET page_no=1,text='옮겨진합성' WHERE page_no=2")
    else:
        destination = 3 if operation == "insert_reuse_id" else 1
        upgraded.execute(
            "INSERT OR REPLACE INTO MaterialPage(id,material_id,page_no,text,terms) "
            "VALUES (?, 'material', ?, '옮겨진합성', '[]')", (page_id, destination),
        )
    expected = [tuple(row) for row in upgraded.execute(
        "SELECT id,material_id,page_no,text FROM MaterialPage ORDER BY page_no"
    )]
    indexed = [tuple(row) for row in upgraded.execute(
        "SELECT id,source_id,position,text FROM SearchIndex WHERE source_kind='material' ORDER BY position"
    )]
    assert indexed == expected
    assert upgraded.execute("SELECT count(*) FROM SearchIndex WHERE SearchIndex MATCH '옮겨진합성'").fetchone()[0] == 1


def test_new_segment_id_cannot_be_reused_to_replace_another_raw_position(upgraded):
    """B-002/CANON-001: the added ID must not create a new REPLACE bypass."""
    upgraded.execute("PRAGMA recursive_triggers=OFF")
    before = [tuple(row) for row in upgraded.execute("SELECT * FROM TranscriptSegment")]
    identity = upgraded.execute("SELECT id FROM TranscriptSegment").fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        upgraded.execute(
            "INSERT OR REPLACE INTO TranscriptSegment(id,transcript_id,seq,start_ms,text,chunk_index) "
            "VALUES (?, 'transcript', 9, 9000, 'replacement', 0)", (identity,),
        )
    assert [tuple(row) for row in upgraded.execute("SELECT * FROM TranscriptSegment")] == before
