"""Schema contract and persistence invariants from the canonical plan, section 6."""

import sqlite3
from contextlib import closing

import pytest

from lecturehub.core import db

# These are the documented fields, including the two explicit v2 references.
SCHEMA_FIELDS = {
    "Course": "id code folder_name abbr term",
    "Schedule": "course_id weekday start end",
    "ScheduleException": "course_id date kind note",
    "Lecture": "id course_id date session_index title lecture_type",
    "MediaAsset": "id lecture_id path sha256 duration_ms codec sample_rate channels bytes origin "
    "device_type is_master derived_from preset_id recorded_start partial loudness_profile decided_by confidence_class",
    "RecordingSession": "id lecture_id media_id mode device_name started_at ended_at heartbeat_path segments gaps",
    "StopReport": "session_id cause certainty at_ms recovered",
    "RecordingAnomaly": "session_id kind from_ms to_ms certainty",
    "IngestItem": "id source_path suggestion state",
    "MoveLog": "from_path to_path at estimated_start estimated_end",
    "ChunkPlan": "id media_id preset_id chunks",
    "Transcript": "id lecture_id media_id engine engine_version role role_decided_by chunk_plan_id "
    "status source_file sha256",
    "TranscriptChunkRun": "transcript_id chunk_index attempt state error raw_output_path started_at ended_at",
    "TranscriptSegment": "transcript_id seq start_ms end_ms speaker text chunk_index",
    "TranscriptQA": "transcript_id chunk_index kind detail severity",
    "CanonicalTranscript": "id lecture_id version parent_version created_at created_by inputs material_ids "
    "stage_reached status",
    "CanonicalSegment": "canonical_id seq start_ms end_ms time_precision text confidence_class flags source_spans",
    "Correction": "id lecture_id anchor before after kind status decided_by created_at",
    "Material": "id course_id path sha256 kind title etl_file_id etl_uploaded_at etl_module page_count",
    "MaterialPage": "material_id page_no text image_path terms",
    "MaterialMatch": "lecture_id material_id page_from page_to confidence_class decided_by",
    "Syllabus": "course_id week topic planned_date",
    "StudyArtifact": "id lecture_ids path kind version parent_version build_script_path mode inputs created_by qa_status",
    "ProcessingJob": "id kind executor lecture_id state attempts manifest_path log_path next_eligible_at",
    "ProviderConsent": "provider course_id granted_at note",
    "QuotaLedger": "provider date requests audio_seconds tokens",
    "Bookmark": "timestamp optional_label",
    "AlignmentAnchor": "lecture_id start_time_ms end_time_ms transcript_segment_id material_id page_or_slide "
    "study_artifact_anchor confidence provenance",
    "SlideViewEvent": "session_id material_id page_no at_ms",
    "ExamCue": "id lecture_id at_ms kind quote context_before context_after material_page engines_agree "
    "source confidence_class decided_by",
    "EmphasisItem": "id course_id term_or_topic lectures mention_count minutes_spent example_solved score_components",
    "QuizSet": "id course_id scope created_by created_at generator_version",
    "QuizItem": "id quiz_set_id type question choices answer explanation sources quoted_evidence difficulty flags status",
    "QuizAttempt": "quiz_item_id at answer_given correct review_state",
    "PastExam": "id course_id path year term exam_kind professor ocr_used",
    "PastExamQuestion": "id past_exam_id number text points type topic_links",
    "TopicExamLikelihood": "id course_id topic exam prob_lo prob_hi level signals method_version n_past_exams "
    "confidence_class created_at",
    "ExamOutcome": "course_id exam topic appeared recorded_by at",
    "TopicLink": "id subject lecture_id material_id page at_ms confidence_class decided_by",
    "Evidence": "id subject_type subject_id field kind detail weight source created_at",
    "EngineMetricObservation": "id metric_kind metric_name engine engine_b engine_version comparison_set_id device_type "
    "course_id lecture_type value sample_size method window_start created_at",
    "EngineMetricAggregate": "engine metric_name scope scope_value value range_lo range_hi n method calculated_at",
    "Event": "id at type subject_type subject_id payload_json",
    "TranscriptInUse": "id lecture_id transcript_id previous_transcript_id decided_by reason created_at",
    "NoteBlock": "id artifact_id block_key seq kind text ext_confidence ext_source ext_ref ext_conflict authored_by "
    "warnings created_at",
    "NoteCitation": "id block_id kind transcript_id seg_seq material_id page_no quote",
    "CalendarEvent": "id course_id kind title date date_precision date_text start_ms end_ms note source source_ref "
    "status created_at updated_at",
}


APPROVED_FIELDS = {
    "Bookmark": "lecture_id media_id offset_ms",
    "AlignmentAnchor": "start_ms end_ms transcript_id seq page artifact_id artifact_anchor",
    "QuizItem": "external_blocks",
}


@pytest.fixture
def connection(tmp_path):
    with closing(db.connect(tmp_path / "lecturehub.sqlite")) as connection:
        assert db.migrate(connection) == [1, 2, 3, 4, 5, 6, 7, 8, 9]
        yield connection


def seed_transcript(connection):
    connection.execute("INSERT INTO Course VALUES ('course', 'TEST', '합성 과목', 'su', '26-2')")
    connection.execute("INSERT INTO Lecture VALUES ('lecture', 'course', '2026-09-28', 1, NULL, NULL)")
    connection.execute(
        "INSERT INTO MediaAsset VALUES "
        "('media', 'lecture', '합성 녹음.wav', 'media-hash', 5000, 'pcm', 16000, 1, 160000, "
        "'win_recorder', 'laptop', 1, NULL, NULL, 123456, 0, '[]', 'user', 'exact')"
    )
    connection.execute(
        "INSERT INTO Transcript VALUES "
        "('transcript', 'lecture', 'media', 'synthetic', NULL, 'timeline', 'user', NULL, "
        "'done', '합성 전사.txt', 'transcript-hash')"
    )
    connection.execute(
        "INSERT INTO TranscriptSegment (transcript_id, seq, start_ms, end_ms, speaker, text, chunk_index) VALUES ('transcript', 0, 1000, NULL, NULL, '합성 문장입니다.', 0)"
    )
    connection.commit()


def test_schema_contains_all_and_only_documented_entities_and_fields(connection):
    """DB-SCHEMA: every section 6 entity exists without invented fields."""
    tables = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'SearchIndex%'")
    }
    assert tables == SCHEMA_FIELDS.keys() | {"schema_version"}
    for table, expected in SCHEMA_FIELDS.items():
        columns = list(connection.execute(f'PRAGMA table_info("{table}")'))
        approved = APPROVED_FIELDS.get(table, "").split()
        expected_fields = expected.split() + approved
        if "id" not in expected_fields:
            expected_fields.append("id")
        assert [row[1] for row in columns] == expected_fields, table
        assert all(row[2] == "TEXT" for row in columns if row[1] == "id"), table
    assert list(connection.execute("PRAGMA foreign_key_check")) == []
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def index_columns(connection, table):
    return [
        tuple(column[2] for column in connection.execute(f'PRAGMA index_info("{index[1]}")'))
        for index in connection.execute(f'PRAGMA index_list("{table}")')
    ]


def test_all_foreign_keys_and_temporal_columns_are_indexed(connection):
    """DB-INDEX: real SQLite metadata covers child keys and time-window queries."""
    clock_names = {
        "start", "end", "date", "planned_date", "recorded_start", "started_at", "ended_at",
        "at", "at_ms", "from_ms", "to_ms", "estimated_start", "estimated_end", "start_ms",
        "end_ms", "created_at", "etl_uploaded_at", "next_eligible_at", "granted_at", "timestamp",
        "start_time_ms", "end_time_ms", "window_start", "calculated_at",
    }
    for table in SCHEMA_FIELDS:
        indexes = index_columns(connection, table)
        foreign_keys = {}
        for key in connection.execute(f'PRAGMA foreign_key_list("{table}")'):
            foreign_keys.setdefault(key[0], []).append((key[1], key[3]))
        for parts in foreign_keys.values():
            fields = tuple(field for _, field in sorted(parts))
            assert any(index[:len(fields)] == fields for index in indexes), (table, fields)
        for column in connection.execute(f'PRAGMA table_info("{table}")'):
            if column[1] in clock_names:
                assert any(index[0] == column[1] for index in indexes), (table, column[1])
                expected_type = "TEXT" if column[1] in {"date", "planned_date"} else "INTEGER"
                assert column[2] == expected_type, (table, column[1])


def test_init_db_korean_spaces_parentheses_wal_and_idempotence(tmp_path):
    """DB-INIT/INGEST-005: initialize and reopen a Korean filesystem path."""
    path = tmp_path / "가상 학기 (26-2)" / "_lecturehub" / "lecturehub.sqlite"
    assert db.init_db(path) is None
    db.init_db(path)
    with closing(db.connect(path)) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert db.migrate(connection) == []
        assert [row[0] for row in connection.execute("SELECT version FROM schema_version")] == [1, 2, 3, 4, 5, 6, 7, 8, 9]
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            connection.execute("INSERT INTO Lecture VALUES ('bad', 'missing', '2026-09-28', 1, NULL, NULL)")


def test_integer_time_and_json_shape_are_enforced(connection):
    """TIME-001/DB-JSON: fractional milliseconds and invalid list/object storage fail."""
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("INSERT INTO Bookmark (timestamp, optional_label) VALUES (1000.5, NULL)")
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("INSERT INTO IngestItem VALUES ('bad', '합성.wav', '[]', 'pending')")
    connection.execute("INSERT INTO Bookmark (timestamp, optional_label) VALUES (1000, NULL)")
    assert connection.execute("SELECT timestamp, typeof(timestamp) FROM Bookmark").fetchone()[:] == (1000, "integer")


@pytest.mark.parametrize("sql", [
    "UPDATE Event SET type = 'CHANGED' WHERE id = 'event'",
    "DELETE FROM Event WHERE id = 'event'",
    "INSERT OR REPLACE INTO Event VALUES ('event', 2, 'CHANGED', 'Lecture', 'lecture', '{}')",
    "REPLACE INTO Event VALUES ('event', 2, 'CHANGED', 'Lecture', 'lecture', '{}')",
])
def test_event_is_append_only_even_for_replace(connection, sql):
    """EVENT-001: immutable events cannot be overwritten through SQLite REPLACE."""
    connection.execute("INSERT INTO Event VALUES ('event', 1, 'ORIGINAL', 'Lecture', 'lecture', '{}')")
    connection.commit()
    connection.execute("PRAGMA recursive_triggers = OFF")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute(sql)
    assert connection.execute("SELECT at, type FROM Event").fetchone()[:] == (1, "ORIGINAL")


@pytest.mark.parametrize("table,sql", [
    ("Transcript", "UPDATE Transcript SET source_file = 'other.txt'"),
    ("Transcript", "UPDATE Transcript SET sha256 = 'changed-hash'"),
    ("Transcript", "DELETE FROM Transcript"),
    ("Transcript", ("INSERT OR REPLACE INTO Transcript SELECT id,lecture_id,media_id,engine,engine_version,"
                    "role,role_decided_by,chunk_plan_id,status,source_file,sha256 FROM Transcript")),
    ("TranscriptSegment", "UPDATE TranscriptSegment SET text = 'changed'"),
    ("TranscriptSegment", "DELETE FROM TranscriptSegment"),
    ("TranscriptSegment", "INSERT OR REPLACE INTO TranscriptSegment SELECT * FROM TranscriptSegment"),
    ("MediaAsset", "UPDATE MediaAsset SET path = 'changed.wav'"),
    ("MediaAsset", "DELETE FROM MediaAsset"),
    ("MediaAsset", "INSERT OR REPLACE INTO MediaAsset SELECT * FROM MediaAsset"),
])
def test_raw_evidence_is_immutable(connection, table, sql):
    """CANON-001: source metadata and raw transcript rows survive rejected edits."""
    seed_transcript(connection)
    before = [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]
    connection.execute("PRAGMA recursive_triggers = OFF")
    with pytest.raises(sqlite3.IntegrityError, match="immutable|duplicate"):
        connection.execute(sql)
    assert [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")] == before


@pytest.mark.parametrize("table", ["Event", "Transcript", "TranscriptSegment", "MediaAsset"])
def test_hidden_rowid_cannot_bypass_immutable_tables(table):
    """EVENT-001/CANON-001: plain SQLite clients cannot replace evidence by rowid."""
    with closing(sqlite3.connect(":memory:")) as connection:
        db.migrate(connection)
        assert connection.execute("PRAGMA recursive_triggers").fetchone()[0] == 0
        seed_transcript(connection)
        connection.execute("INSERT INTO Event VALUES ('event', 1, 'ORIGINAL', 'Lecture', 'lecture', '{}')")
        before = connection.execute(f"SELECT * FROM {table}").fetchall()
        with pytest.raises(sqlite3.OperationalError, match="no column named rowid"):
            connection.execute(f"INSERT OR REPLACE INTO {table}(rowid) VALUES (1)")
        assert connection.execute(f"SELECT * FROM {table}").fetchall() == before


def test_metadata_can_change_without_modifying_raw_sources(connection):
    """CANON-001: role selection, ingest association and analysis remain possible."""
    seed_transcript(connection)
    connection.execute("UPDATE Transcript SET role = 'content', role_decided_by = 'user', status = 'qa_warning'")
    connection.execute("UPDATE MediaAsset SET lecture_id = NULL")
    connection.execute(
        "UPDATE MediaAsset SET lecture_id = 'lecture', loudness_profile = '[{\"rms\":-30}]', "
        "partial = 1, decided_by = 'user', confidence_class = 'exact'"
    )
    assert connection.execute("SELECT role, sha256 FROM Transcript").fetchone()[:] == ("content", "transcript-hash")
    assert connection.execute("SELECT lecture_id, sha256, partial FROM MediaAsset").fetchone()[:] == (
        "lecture", "media-hash", 1,
    )
    assert connection.execute("SELECT text FROM TranscriptSegment").fetchone()[0] == "합성 문장입니다."


def test_derived_media_cannot_replace_a_master_on_update(connection):
    """CANON-001: UPDATE OR REPLACE of a derived row cannot delete a master."""
    seed_transcript(connection)
    connection.execute(
        "INSERT INTO MediaAsset SELECT 'derived', lecture_id, 'derived.wav', 'derived-hash', "
        "duration_ms, codec, sample_rate, channels, bytes, origin, device_type, 0, 'media', "
        "preset_id, recorded_start, partial, loudness_profile, decided_by, confidence_class FROM MediaAsset"
    )
    connection.execute("PRAGMA recursive_triggers = OFF")
    with pytest.raises(sqlite3.IntegrityError, match="duplicate"):
        connection.execute("UPDATE OR REPLACE MediaAsset SET sha256 = 'media-hash' WHERE id = 'derived'")
    assert connection.execute("SELECT count(*) FROM MediaAsset").fetchone()[0] == 2


def test_chunk_run_records_waiting_and_running_without_fabricated_times(connection):
    """DB-STATE: queued/running/done timestamps follow actual lifecycle events."""
    seed_transcript(connection)
    connection.execute(
        "INSERT INTO TranscriptChunkRun (transcript_id, chunk_index, attempt, state, error, raw_output_path, started_at, ended_at) VALUES ('transcript', 0, 1, 'queued', NULL, 'raw.txt', NULL, NULL)"
    )
    connection.execute("UPDATE TranscriptChunkRun SET state = 'running', started_at = 1000")
    assert connection.execute("SELECT ended_at FROM TranscriptChunkRun").fetchone()[0] is None
    connection.execute("UPDATE TranscriptChunkRun SET state = 'done', ended_at = 1500")
    assert connection.execute("SELECT started_at, ended_at FROM TranscriptChunkRun").fetchone()[:] == (1000, 1500)


def test_slide_view_does_not_require_prior_material_text_extraction(connection):
    """SLIDE-001: a page turn is stored before MaterialPage extraction finishes."""
    seed_transcript(connection)
    connection.execute(
        "INSERT INTO RecordingSession VALUES "
        "('session', 'lecture', 'media', 'mic', 'synthetic', 1000, NULL, 'heartbeat.json', '[]', '[]')"
    )
    connection.execute(
        "INSERT INTO Material VALUES "
        "('material', 'course', 'synthetic.pdf', 'material-hash', 'pdf', '합성 자료', NULL, NULL, NULL, 10)"
    )
    connection.execute("INSERT INTO SlideViewEvent (session_id, material_id, page_no, at_ms) VALUES ('session', 'material', 2, 1000)")
    assert connection.execute("SELECT count(*) FROM MaterialPage").fetchone()[0] == 0
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        connection.execute("INSERT INTO SlideViewEvent (session_id, material_id, page_no, at_ms) VALUES ('session', 'material', 0, 1500)")


def test_duplicate_hash_cannot_create_another_registration(connection):
    """DEDUP-001: a different Transcript ID cannot bypass content-hash uniqueness."""
    seed_transcript(connection)
    with pytest.raises(sqlite3.IntegrityError, match="sha256"):
        connection.execute(
            "INSERT INTO Transcript SELECT 'duplicate', lecture_id, media_id, engine, engine_version, role, "
            "role_decided_by, chunk_plan_id, status, source_file, sha256 FROM Transcript"
        )
    assert connection.execute("SELECT count(*) FROM Transcript").fetchone()[0] == 1


def test_derived_transcript_preserves_raw_evidence(connection):
    """CANON-001: generating and editing a derived transcript leaves the raw rows intact."""
    seed_transcript(connection)
    connection.execute(
        "INSERT INTO CanonicalTranscript VALUES "
        "('canonical', 'lecture', 1, NULL, 1000, 'pipeline', "
        "'[{\"transcript_id\":\"transcript\",\"role\":\"timeline\"}]', '[]', 'C0', 'done')"
    )
    connection.execute(
        "INSERT INTO CanonicalSegment (canonical_id, seq, start_ms, end_ms, time_precision, text, confidence_class, flags, source_spans) VALUES "
        "('canonical', 0, 1000, 2000, 'single_source', '파생 문장', 'low', '[\"single_source\"]', '[]')"
    )
    connection.execute("UPDATE CanonicalSegment SET text = '수정된 파생 문장'")
    assert connection.execute("SELECT sha256 FROM Transcript").fetchone()[0] == "transcript-hash"
    assert connection.execute("SELECT text FROM TranscriptSegment").fetchone()[0] == "합성 문장입니다."


def fake_migrations(tmp_path, monkeypatch, scripts):
    directory = tmp_path / "fake_package" / "migrations"
    directory.mkdir(parents=True)
    for filename, script in scripts.items():
        (directory / filename).write_text(script, encoding="utf-8")
    monkeypatch.setattr(db.resources, "files", lambda _package: directory.parent)


def test_migrations_apply_in_numeric_order_and_handle_trigger_statements(tmp_path, monkeypatch):
    """DB-MIGRATE: ordered packaged files, SQL literals, triggers and re-entry."""
    fake_migrations(tmp_path, monkeypatch, {
        "0002_second.sql": "INSERT INTO example VALUES (2); INSERT INTO example VALUES (3);",
        "0001_first.sql": "CREATE TABLE example (n INTEGER); CREATE TABLE audit (message TEXT);\n"
        "CREATE TRIGGER example_insert AFTER INSERT ON example BEGIN\n"
        "INSERT INTO audit VALUES ('one;two'); INSERT INTO audit VALUES ('three'); END;",
    })
    with closing(db.connect(":memory:")) as connection:
        assert db.migrate(connection) == [1, 2]
        assert db.migrate(connection) == []
        assert [row[0] for row in connection.execute("SELECT n FROM example")] == [2, 3]
        assert connection.execute("SELECT count(*) FROM audit").fetchone()[0] == 4


def test_failed_migration_rolls_back_schema_data_and_version(tmp_path, monkeypatch):
    """DB-MIGRATE: failing later SQL never leaves a partially upgraded database."""
    fake_migrations(tmp_path, monkeypatch, {
        "0001_first.sql": "CREATE TABLE example (n INTEGER); INSERT INTO example VALUES (1);",
        "0002_broken.sql": "CREATE TABLE partial (n INTEGER); INSERT INTO nonexistent VALUES (2);",
    })
    with closing(db.connect(":memory:")) as connection:
        with pytest.raises(sqlite3.OperationalError, match="nonexistent"):
            db.migrate(connection)
        assert connection.in_transaction is False
        assert list(connection.execute("SELECT name FROM sqlite_schema WHERE type = 'table'")) == []
        (tmp_path / "fake_package" / "migrations" / "0002_broken.sql").write_text(
            "INSERT INTO example VALUES (2);", encoding="utf-8"
        )
        assert db.migrate(connection) == [1, 2]


def test_migration_does_not_commit_callers_transaction(connection):
    """DB-MIGRATE: migrations never implicitly commit unrelated work."""
    connection.execute("INSERT INTO Bookmark (timestamp, optional_label) VALUES (123, NULL)")
    with pytest.raises(ValueError, match="active transaction"):
        db.migrate(connection)
    assert connection.in_transaction
    connection.rollback()
    assert connection.execute("SELECT count(*) FROM Bookmark").fetchone()[0] == 0
