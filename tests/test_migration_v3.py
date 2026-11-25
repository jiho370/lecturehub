"""Synthetic preservation, backup and honesty tests for migration 0003."""

import sqlite3
import shutil
from contextlib import closing
from importlib import resources
from pathlib import Path

import pytest
from test_migration_v2 import insert, seed_legacy

from lecturehub.core import db
from lecturehub.core.ids import new_ulid


def migration_three_sql():
    return resources.files("lecturehub.core").joinpath("migrations/0003_optional_media.sql").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def version_two_template(tmp_path_factory):
    path = tmp_path_factory.mktemp("v2_template") / "synthetic.sqlite"
    with closing(db.connect(path)) as connection:
        directory = resources.files("lecturehub.core").joinpath("migrations")
        connection.execute("BEGIN")  # fixture setup only; the migration under test still runs separately
        connection.execute("CREATE TABLE schema_version(version INTEGER PRIMARY KEY)")
        for version, filename in [(1, "0001_init.sql"), (2, "0002_stable_references_search.sql")]:
            for statement in db._statements(directory.joinpath(filename).read_text(encoding="utf-8")):
                connection.execute(statement)
            connection.execute("INSERT INTO schema_version(version) VALUES (?)", (version,))
        connection.commit()
        seed_legacy(connection)
        insert(connection, "TranscriptChunkRun", transcript_id="transcript", chunk_index=0, attempt=1,
               state="done", raw_output_path="원시 합성.txt", started_at=1000, ended_at=2000)
        insert(connection, "TranscriptQA", transcript_id="transcript", kind="coverage", detail="합성 검사",
               severity="low")
        connection.execute("UPDATE AlignmentAnchor SET transcript_id='transcript', seq=0")
        connection.commit()
    return path


@pytest.fixture
def version_two(tmp_path, version_two_template):
    path = tmp_path / "합성 v2 (보존).sqlite"
    shutil.copyfile(version_two_template, path)
    with closing(db.connect(path)) as connection:
        yield connection


def install_candidate(tmp_path, monkeypatch, suffix=""):
    directory = tmp_path / "candidate_package" / "migrations"
    directory.mkdir(parents=True)
    original = resources.files("lecturehub.core").joinpath("migrations")
    candidate = migration_three_sql()
    for filename in ("0001_init.sql", "0002_stable_references_search.sql"):
        (directory / filename).write_text(original.joinpath(filename).read_text(encoding="utf-8"), encoding="utf-8")
    (directory / "0003_optional_media.sql").write_text(candidate + suffix, encoding="utf-8")
    monkeypatch.setattr(db.resources, "files", lambda _package: directory.parent)


def all_rows(connection):
    result = {}
    for table, in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'"):
        columns = ",".join(f'"{row[1]}"' for row in connection.execute(f'PRAGMA table_info("{table}")'))
        query = f'SELECT {columns} FROM "{table}"'
        if table == "schema_version":
            query += " WHERE version < 3"
        result[table] = sorted([tuple(row) for row in connection.execute(query)], key=repr)
    return result


def transcript_values(**overrides):
    values = {"id": new_ulid(), "lecture_id": "lecture", "media_id": None, "engine": "synthetic",
              "role": "timeline", "role_decided_by": "user", "status": "done",
              "source_file": "음원 없는 합성.txt", "sha256": new_ulid()}
    values.update(overrides)
    return values


def test_v3_preserves_all_legacy_rows_ids_references_and_fts(version_two, tmp_path, monkeypatch):
    """CANON-001/EVENT-001: a rebuild changes the constraint, never source data."""
    before = all_rows(version_two)
    old_guards = {row[0] for row in version_two.execute(
        "SELECT name FROM sqlite_schema WHERE type='trigger' AND tbl_name='Transcript'"
    )}
    install_candidate(tmp_path, monkeypatch)
    assert db.migrate(version_two) == [3]
    assert all_rows(version_two) == before
    assert db.migrate(version_two) == []
    assert version_two.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert version_two.execute("PRAGMA legacy_alter_table").fetchone()[0] == 0
    assert version_two.execute("PRAGMA foreign_key_check").fetchall() == []
    assert version_two.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert {row[0] for row in version_two.execute(
        "SELECT name FROM sqlite_schema WHERE type='trigger' AND tbl_name='Transcript'"
    )} == old_guards
    assert version_two.execute("SELECT no_media FROM Transcript").fetchone()[0] == 0
    assert version_two.execute("SELECT count(*) FROM SearchIndex WHERE SearchIndex MATCH '구조역학'").fetchone()[0] == 2
    assert "media_id" in {row[3] for row in version_two.execute("PRAGMA foreign_key_list(Transcript)")}


def test_nullable_media_sets_truthful_generated_flag(version_two, tmp_path, monkeypatch):
    """No dummy MediaAsset is needed, and clients cannot forge no_media."""
    install_candidate(tmp_path, monkeypatch)
    db.migrate(version_two)
    values = transcript_values()
    insert(version_two, "Transcript", **values)
    insert(version_two, "TranscriptSegment", transcript_id=values["id"], seq=0, start_ms=0,
           text="음원없이 등록한 합성구문", chunk_index=0)
    assert version_two.execute("SELECT media_id,no_media FROM Transcript WHERE id=?", (values["id"],)).fetchone()[:] == (
        None, 1,
    )
    assert version_two.execute("SELECT count(*) FROM MediaAsset").fetchone()[0] == 1
    assert version_two.execute("SELECT count(*) FROM SearchIndex WHERE SearchIndex MATCH '합성구문'").fetchone()[0] == 1
    assert version_two.execute("PRAGMA foreign_key_check").fetchall() == []
    with pytest.raises(sqlite3.OperationalError, match="generated column"):
        insert(version_two, "Transcript", **transcript_values(media_id="media"), no_media=1)
    with pytest.raises(sqlite3.OperationalError, match="generated column"):
        version_two.execute("UPDATE Transcript SET no_media=0 WHERE id=?", (values["id"],))
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        insert(version_two, "Transcript", **transcript_values(media_id="missing"))


@pytest.mark.parametrize("statement", [
    "UPDATE Transcript SET media_id='media' WHERE media_id IS NULL",
    "UPDATE Transcript SET media_id=NULL WHERE media_id='media'",
    "UPDATE Transcript SET source_file='other.txt'",
    "UPDATE Transcript SET sha256='other-hash'",
    "DELETE FROM Transcript WHERE media_id IS NULL",
    ("INSERT OR REPLACE INTO Transcript SELECT id,lecture_id,media_id,engine,engine_version,role,role_decided_by,"
     "chunk_plan_id,status,source_file,sha256 FROM Transcript"),
    "UPDATE TranscriptSegment SET text='changed'",
    "UPDATE Event SET payload_json='{}'",
])
def test_no_media_never_opens_an_immutable_source_update_path(version_two, tmp_path, monkeypatch, statement):
    """CANON-001: even a later first media association remains unauthorized."""
    install_candidate(tmp_path, monkeypatch)
    db.migrate(version_two)
    insert(version_two, "Transcript", **transcript_values())
    before = all_rows(version_two)
    version_two.execute("PRAGMA recursive_triggers=OFF")
    with pytest.raises(sqlite3.IntegrityError):
        version_two.execute(statement)
    assert all_rows(version_two) == before


@pytest.mark.parametrize("foreign_keys,legacy_mode", [(0, 0), (1, 0), (1, 1)])
def test_rebuild_failure_restores_data_schema_and_connection_settings(
    version_two, tmp_path, monkeypatch, foreign_keys, legacy_mode,
):
    """A failing statement after DROP/RENAME cannot leave a partial migration."""
    version_two.execute(f"PRAGMA foreign_keys={foreign_keys}")
    version_two.execute(f"PRAGMA legacy_alter_table={legacy_mode}")
    before = all_rows(version_two)
    schema = [tuple(row) for row in version_two.execute("SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY name")]
    install_candidate(tmp_path, monkeypatch, "\nINSERT INTO nonexistent_v3_failure VALUES (1);\n")
    with pytest.raises(sqlite3.OperationalError, match="nonexistent_v3_failure"):
        db.migrate(version_two)
    assert all_rows(version_two) == before
    assert [tuple(row) for row in version_two.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY name"
    )] == schema
    assert version_two.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert version_two.execute("PRAGMA legacy_alter_table").fetchone()[0] == legacy_mode
    assert not version_two.in_transaction
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        version_two.execute("UPDATE Transcript SET source_file='changed'")


def test_rebuild_rejects_and_rolls_back_new_foreign_key_violations(version_two, tmp_path, monkeypatch):
    """The migration checks relations before COMMIT while FK enforcement is off."""
    before = all_rows(version_two)
    install_candidate(
        tmp_path, monkeypatch,
        "\nINSERT INTO Lecture(id,course_id,date,session_index) VALUES ('bad', 'absent-course', '2026-09-30', 1);\n",
    )
    with pytest.raises(sqlite3.IntegrityError, match="foreign key references"):
        db.migrate(version_two)
    assert all_rows(version_two) == before
    assert version_two.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_v3_allows_operational_metadata_and_automatic_ids(version_two, tmp_path, monkeypatch):
    """Role/status updates and auto-IDs survive the reconstructed Transcript table."""
    install_candidate(tmp_path, monkeypatch)
    db.migrate(version_two)
    values = transcript_values()
    values.pop("id")
    insert(version_two, "Transcript", **values)
    version_two.execute("UPDATE Transcript SET role='content',role_decided_by='user',status='qa_warning'")
    row = version_two.execute("SELECT id,no_media,role,status FROM Transcript WHERE media_id IS NULL").fetchone()
    assert len(row["id"]) == 26
    assert row["no_media"] == 1
    assert row["role"] == "content"
    assert row["status"] == "qa_warning"


def test_custom_objects_and_cascading_child_rows_survive(version_two, tmp_path, monkeypatch):
    """The 12-step rebuild preserves unknown local objects and direct children."""
    version_two.execute(
        "CREATE TABLE CustomTranscriptChild(id TEXT PRIMARY KEY, transcript_id TEXT REFERENCES Transcript(id) "
        "ON DELETE CASCADE, detail TEXT)"
    )
    version_two.execute("INSERT INTO CustomTranscriptChild VALUES ('child','transcript','합성 자식')")
    version_two.execute("CREATE INDEX custom_transcript_engine ON Transcript(engine)")
    version_two.execute(
        "CREATE TRIGGER custom_transcript_metadata AFTER UPDATE OF status ON Transcript BEGIN SELECT 1; END"
    )
    version_two.execute("CREATE VIEW custom_transcript_view AS SELECT id,sha256 FROM Transcript")
    version_two.commit()
    objects = [tuple(row) for row in version_two.execute(
        "SELECT type,name,sql FROM sqlite_schema WHERE type IN ('index','trigger','view') ORDER BY name"
    )]
    before = all_rows(version_two)
    install_candidate(tmp_path, monkeypatch)
    assert db.migrate(version_two) == [3]
    assert all_rows(version_two) == before
    assert [tuple(row) for row in version_two.execute(
        "SELECT type,name,sql FROM sqlite_schema WHERE type IN ('index','trigger','view') ORDER BY name"
    )] == objects


@pytest.mark.parametrize("fail_after_rebuild", [False, True])
def test_file_database_backup_preserves_pre_migration_wal_state(
    version_two, tmp_path, monkeypatch, fail_after_rebuild,
):
    """Backups survive success and rollback, including data still in the WAL."""
    before = all_rows(version_two)
    suffix = "\nINSERT INTO nonexistent_after_backup VALUES (1);\n" if fail_after_rebuild else ""
    install_candidate(tmp_path, monkeypatch, suffix)
    if fail_after_rebuild:
        with pytest.raises(sqlite3.OperationalError, match="nonexistent_after_backup"):
            db.migrate(version_two)
    else:
        assert db.migrate(version_two) == [3]
    backups = list((tmp_path / "backup").glob("*_before_0003_*.sqlite"))
    assert len(backups) == 1
    with closing(sqlite3.connect(backups[0])) as backup:
        assert all_rows(backup) == before
        assert backup.execute("SELECT max(version) FROM schema_version").fetchone()[0] == 2
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        media_column = next(row for row in backup.execute("PRAGMA table_info(Transcript)") if row[1] == "media_id")
        assert media_column[3] == 1
    if fail_after_rebuild:
        assert all_rows(version_two) == before
    else:
        assert db.migrate(version_two) == []
        assert list((tmp_path / "backup").glob("*_before_0003_*.sqlite")) == backups


def test_backup_directory_cannot_resolve_outside_database_parent(version_two, tmp_path, monkeypatch):
    """Reject a symlink/junction escape before writing a backup or changing DB."""
    before = all_rows(version_two)
    install_candidate(tmp_path, monkeypatch)
    original_resolve = Path.resolve
    forbidden = tmp_path / "outside" / "escaped-backup"

    def resolved_path(path, *args, **kwargs):
        if path == tmp_path / "backup":
            return forbidden
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolved_path)
    with pytest.raises(ValueError, match="escapes"):
        db.migrate(version_two)
    assert all_rows(version_two) == before
    assert not forbidden.exists()
    assert not (tmp_path / "backup").exists()
    assert version_two.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_in_memory_database_rebuild_does_not_create_a_backup(tmp_path, monkeypatch):
    """Memory-only tests have no original file to back up."""
    install_candidate(tmp_path, monkeypatch)
    with closing(db.connect(":memory:")) as connection:
        assert db.migrate(connection) == [1, 2, 3]
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert not (tmp_path / "backup").exists()


def test_populated_v1_upgrades_through_v2_and_v3_with_a_v1_backup(tmp_path):
    """A populated initial-schema database follows the complete upgrade chain."""
    directory = resources.files("lecturehub.core").joinpath("migrations")
    with closing(db.connect(tmp_path / "populated-v1.sqlite")) as connection:
        connection.execute("BEGIN")  # synthetic schema construction; the upgrade below is unchanged
        connection.execute("CREATE TABLE schema_version(version INTEGER PRIMARY KEY)")
        for statement in db._statements(directory.joinpath("0001_init.sql").read_text(encoding="utf-8")):
            connection.execute(statement)
        connection.execute("INSERT INTO schema_version(version) VALUES (1)")
        seed_legacy(connection)
        before = {}
        for table in ("Transcript", "TranscriptSegment", "Event"):
            columns = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
            before[table] = (columns, [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")])
        assert db.migrate(connection) == [2, 3, 4, 5, 6, 7, 8, 9]
        for table, (columns, rows) in before.items():
            assert [tuple(row) for row in connection.execute(f"SELECT {','.join(columns)} FROM {table}")] == rows
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("SELECT no_media FROM Transcript").fetchone()[0] == 0
    backups = list((tmp_path / "backup").glob("*.sqlite"))
    assert len(backups) == 1
    with closing(sqlite3.connect(backups[0])) as backup:
        assert backup.execute("SELECT max(version) FROM schema_version").fetchone()[0] == 1
