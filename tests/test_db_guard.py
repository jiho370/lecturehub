"""S0 safety: newer databases are refused before any write (D-051), capture never
depends on the database (D-050), raw tables resist every REPLACE/upsert path."""
import ast
import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from test_db import seed_transcript
from typer.testing import CliRunner

from lecturehub.core import db

SRC = Path(__file__).resolve().parents[1] / "src" / "lecturehub"


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def newer_db(tmp_path):
    root = tmp_path / "대학" / "26-2 (합성)"
    path = root / "_lecturehub" / "lecturehub.sqlite"
    db.init_db(path)
    with closing(sqlite3.connect(path)) as raw:
        raw.create_function("new_ulid", 0, lambda: "01NEWERAPPWROTETHISROW0000")  # as a newer app would
        raw.execute("INSERT INTO schema_version(version) VALUES (?)", (db.known_schema_version() + 1,))
        raw.commit()
        raw.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return root, path


def _untouched(path: Path):
    return (_digest(path), path.stat().st_mtime_ns, sorted(p.name for p in path.parent.iterdir()))


def test_D051_newer_database_is_refused_before_any_write(newer_db):
    _root, path = newer_db
    before = _untouched(path)
    assert not path.with_name(path.name + "-wal").exists() and not path.with_name(path.name + "-shm").exists()
    with pytest.raises(db.SchemaTooNewError, match="이 앱보다 새 버전"):
        db.connect(path)
    with pytest.raises(db.SchemaTooNewError):
        db.init_db(path)
    uri = path.resolve().as_uri() + "?mode=ro&immutable=1"
    with closing(sqlite3.connect(uri, uri=True)) as raw, pytest.raises(db.SchemaTooNewError):
        db.migrate(raw)
    assert _untouched(path) == before  # same bytes, same mtime, no -wal/-shm created


def test_D051_version_only_in_the_wal_is_still_caught(tmp_path):
    path = tmp_path / "lecturehub.sqlite"
    db.init_db(path)
    keeper = sqlite3.connect(path)  # holds the WAL open so the newer version is not checkpointed
    keeper.create_function("new_ulid", 0, lambda: "01NEWERAPPWROTETHISROW0000")
    keeper.execute("PRAGMA wal_autocheckpoint = 0")
    keeper.execute("INSERT INTO schema_version(version) VALUES (?)", (db.known_schema_version() + 1,))
    keeper.commit()
    try:
        with pytest.raises(db.SchemaTooNewError):
            db.connect(path)
    finally:
        keeper.close()


def test_D051_cli_commands_stop_with_a_clear_message(newer_db):
    from lecturehub import cli

    cli._register_all()
    root, path = newer_db
    before = _digest(path)
    runner = CliRunner()
    for args in (["db", "init", "--root", str(root)], ["status", "--root", str(root)],
                 ["search", "합성", "--root", str(root)]):
        result = runner.invoke(cli.app, args)
        assert result.exit_code != 0, args
        assert "이 앱보다 새 버전" in result.output, (args, result.output)
    assert _digest(path) == before


def test_D051_same_or_older_database_still_opens(tmp_path):
    path = tmp_path / "lecturehub.sqlite"
    db.init_db(path)
    with closing(db.connect(path)) as connection:
        assert db.check_schema_version(connection) == db.known_schema_version()


def _imports(files) -> set[str]:
    found = set()
    for file in files:
        package = file.parent.name
        for node in ast.walk(ast.parse(file.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                found.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:  # relative import inside lecturehub
                    base = f"lecturehub.{package}.{base}".rstrip(".")
                found.add(base)
    return found


# D-050 is about the capture path. The app's library view (ui/library.py) may use the DB,
# but the recorder, transcription and the app's recording backend/window must not depend on it.
CAPTURE_PATH = {
    "recorder": sorted((SRC / "recorder").glob("*.py")),
    "transcription": sorted((SRC / "transcription").glob("*.py")),
    "ui recording path": [SRC / "ui" / "backend.py", SRC / "ui" / "app.py", SRC / "ui" / "media_server.py"],
}


@pytest.mark.parametrize("area", CAPTURE_PATH)
def test_D050_capture_and_transcription_paths_never_open_the_database(area):
    """D-050: recording (and the app's recording path) must work whatever state the DB is in."""
    imported = _imports(CAPTURE_PATH[area])
    assert "sqlite3" not in imported
    assert not any(name.startswith(("lecturehub.core.db", "lecturehub.index")) for name in imported)


@pytest.mark.parametrize("damage", ["newer", "garbage"])
def test_D050_app_library_fails_softly_while_recording_still_works(tmp_path, newer_db, damage):
    """A too-new or broken DB shows an error in the library view; the window and recorder keep working."""
    from datetime import date

    from lecturehub.ui.library import Library

    root, path = newer_db
    if damage == "garbage":
        path.write_bytes(b"this is not a database")
    library = Library(root)
    result = library.sidebar(date(2026, 9, 29))
    assert result["ok"] is False and result["error"]
    assert library.lecture("x")["ok"] is False and library.search("합성")["ok"] is False
    (tmp_path / "rec").mkdir()
    test_D050_recording_works_next_to_a_broken_database(tmp_path / "rec")


def test_D050_recording_works_next_to_a_broken_database(tmp_path):
    from lecturehub.recorder.capture import FakeSource
    from lecturehub.recorder.session import RecordingSession

    lecturehub_dir = tmp_path / "_lecturehub"
    lecturehub_dir.mkdir()
    (lecturehub_dir / "lecturehub.sqlite").write_bytes(b"not a database at all")
    source = FakeSource(sample_rate=8000, duration_seconds=0.2)
    session = RecordingSession(source, lecturehub_dir / "recordings")
    session.start()
    while source.active:
        pass
    report = session.stop()
    assert report["cause"] == "user_stop" and report["total_samples"] == 1600


RAW_BYPASSES = [
    ("Transcript", ("INSERT INTO Transcript (id,lecture_id,media_id,engine,role,role_decided_by,status,sha256) "
                   "VALUES ('transcript','lecture','media','synthetic','timeline','user','done','x') "
                   "ON CONFLICT(id) DO UPDATE SET engine='CHANGED'")),
    ("Transcript", ("INSERT INTO Transcript (id,lecture_id,media_id,engine,role,role_decided_by,status,sha256) "
                   "VALUES ('other','lecture','media','synthetic','timeline','user','done','transcript-hash') "
                   "ON CONFLICT(sha256) DO UPDATE SET source_file='CHANGED'")),
    ("Transcript", "UPDATE OR REPLACE Transcript SET sha256='CHANGED' WHERE id='transcript'"),
    ("TranscriptSegment", ("INSERT INTO TranscriptSegment (transcript_id,seq,start_ms,text,chunk_index) "
                          "VALUES ('transcript',0,1000,'CHANGED',0) "
                          "ON CONFLICT(transcript_id,seq) DO UPDATE SET text='CHANGED'")),
    ("TranscriptSegment", "UPDATE OR REPLACE TranscriptSegment SET text='CHANGED' WHERE seq=0"),
    ("Event", ("INSERT INTO Event VALUES ('event',2,'CHANGED','Lecture','lecture','{}') "
              "ON CONFLICT(id) DO UPDATE SET type='CHANGED'")),
    ("Event", "UPDATE OR REPLACE Event SET type='CHANGED' WHERE id='event'"),
]


@pytest.mark.parametrize("recursive", [True, False])
@pytest.mark.parametrize(("table", "sql"), RAW_BYPASSES)
def test_raw_tables_resist_upsert_and_update_or_replace(tmp_path, table, sql, recursive):
    """CANON-001/EVENT-001: raw rows cannot change through upsert or UPDATE OR REPLACE,
    including from a plain SQLite client with recursive triggers off."""
    with closing(db.connect(tmp_path / "lecturehub.sqlite")) as connection:
        db.migrate(connection)
        seed_transcript(connection)
        connection.execute("INSERT INTO Event VALUES ('event',1,'created','Lecture','lecture','{}')")
        connection.commit()
        if not recursive:
            connection.execute("PRAGMA recursive_triggers = OFF")
        before = [tuple(r) for r in connection.execute(f"SELECT * FROM {table} ORDER BY 1")]
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute(sql)
        connection.rollback()
        assert [tuple(r) for r in connection.execute(f"SELECT * FROM {table} ORDER BY 1")] == before
