"""Stable, sortable identifiers used by the domain model."""

from ulid import ULID


def new_ulid() -> str:
    """Return a fresh canonical 26-character ULID suitable for a TEXT ID."""
    return str(ULID())
