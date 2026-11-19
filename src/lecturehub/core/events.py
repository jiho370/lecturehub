"""Append-only event logging within the caller's database transaction."""

import json
import sqlite3
import time
from collections.abc import Mapping
from typing import Any

from lecturehub.core.ids import new_ulid


class EventStore:
    """Write events using an existing connection; callers control commit/rollback."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def append(
        self,
        type: str,
        subject_type: str,
        subject_id: str,
        payload: Mapping[str, Any],
    ) -> str:
        """Append an event and return its ULID, without committing other changes."""
        for name, value in (("type", type), ("subject_type", subject_type), ("subject_id", subject_id)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if not isinstance(payload, Mapping):
            raise TypeError("payload must be a mapping containing JSON-compatible values")
        payload_json = json.dumps(dict(payload), ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        event_id = new_ulid()
        self.connection.execute(
            "INSERT INTO Event (id, at, type, subject_type, subject_id, payload_json) VALUES (?, ?, ?, ?, ?, ?)",
            (event_id, time.time_ns() // 1_000_000, type, subject_type, subject_id, payload_json),
        )
        return event_id


def append(
    connection: sqlite3.Connection,
    type: str,
    subject_type: str,
    subject_id: str,
    payload: Mapping[str, Any],
) -> str:
    """Append one event through an explicit connection; return its ULID."""
    return EventStore(connection).append(type, subject_type, subject_id, payload)
