"""Locate app-owned paths while preserving the original semester layout."""

from os import PathLike
from pathlib import Path


def app_dir(root: str | PathLike[str]) -> Path:
    """Return ``<semester root>/_lecturehub`` without creating directories."""
    return Path(root) / "_lecturehub"


def database_path(root: str | PathLike[str]) -> Path:
    """Return the library database path without modifying the filesystem."""
    return app_dir(root) / "lecturehub.sqlite"
