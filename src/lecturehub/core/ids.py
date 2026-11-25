"""Stable, sortable identifiers used by the domain model."""


def new_ulid() -> str:
    """Return a fresh canonical 26-character ULID suitable for a TEXT ID."""
    from ulid import ULID  # on first use: python-ulid loads importlib.metadata (and with it email), which the
    # read-only AI app link process (D-072) never needs

    return str(ULID())
