"""EVENT-001 integration checks against the migrated SQLite database."""

import json
import sqlite3
import time
from pathlib import Path

import pytest

from lecturehub.core.db import connect, init_db
from lecturehub.core.events import EventStore, append
from lecturehub.core.ids import new_ulid


@pytest.fixture
def connection(tmp_path: Path):
    path = tmp_path / "events.sqlite"
    init_db(path)
    database = connect(path)
    try:
        yield database
    finally:
        database.close()


def test_event_001_each_transition_appends_timestamped_json(connection: sqlite3.Connection) -> None:
    """EVENT-001: Separate state transitions retain separate event evidence."""
    subject_id = new_ulid()
    before_ms = time.time_ns() // 1_000_000
    first_id = EventStore(connection).append(
        "TRANSCRIPTION_QUEUED", "Transcript", subject_id, {"state": "queued", "설명": "합성"}
    )
    second_id = append(connection, "CHUNK_DONE", "Transcript", subject_id, {"state": "done", "chunk": 0})
    after_ms = time.time_ns() // 1_000_000
    rows = connection.execute(
        "SELECT id, at, type, subject_type, subject_id, payload_json FROM Event ORDER BY at, id"
    ).fetchall()
    assert len(rows) == 2
    assert {row[0] for row in rows} == {first_id, second_id}
    assert {row[2] for row in rows} == {"TRANSCRIPTION_QUEUED", "CHUNK_DONE"}
    assert all(before_ms <= row[1] <= after_ms and isinstance(row[1], int) for row in rows)
    assert all(row[3] == "Transcript" and row[4] == subject_id for row in rows)
    payloads = {row[2]: json.loads(row[5]) for row in rows}
    assert payloads["TRANSCRIPTION_QUEUED"] == {"state": "queued", "설명": "합성"}
    assert payloads["CHUNK_DONE"] == {"state": "done", "chunk": 0}


def test_event_001_update_and_delete_are_blocked(connection: sqlite3.Connection) -> None:
    """EVENT-001: The database itself protects events from update and deletion."""
    event_id = append(connection, "CHUNK_DONE", "Transcript", new_ulid(), {"chunk": 0})
    connection.commit()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("UPDATE Event SET at = at + 1 WHERE id = ?", (event_id,))
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("DELETE FROM Event WHERE id = ?", (event_id,))
    assert connection.execute("SELECT count(*) FROM Event").fetchone()[0] == 1


def test_event_append_participates_in_callers_transaction(connection: sqlite3.Connection) -> None:
    """A failed state transition can roll back its event in the same transaction."""
    append(connection, "TRANSCRIPTION_QUEUED", "Transcript", new_ulid(), {"state": "queued"})
    connection.rollback()
    assert connection.execute("SELECT count(*) FROM Event").fetchone()[0] == 0


def test_invalid_json_payload_does_not_append(connection: sqlite3.Connection) -> None:
    with pytest.raises(ValueError):
        append(connection, "CHUNK_DONE", "Transcript", new_ulid(), {"elapsed": float("nan")})
    assert connection.execute("SELECT count(*) FROM Event").fetchone()[0] == 0
