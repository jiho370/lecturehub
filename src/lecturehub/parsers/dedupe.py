"""Content-addressed duplicate detection; source files are only read."""

import hashlib
import warnings
from collections.abc import Iterable
from pathlib import Path


class DuplicateTranscriptWarning(UserWarning):
    """Two input paths contain the same transcript bytes."""


def sha256_file(path: str | Path) -> str:
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def deduplicate_files(paths: Iterable[str | Path]) -> list[Path]:
    """Keep the first path for each byte hash and warn for every duplicate."""
    seen: dict[str, Path] = {}
    unique: list[Path] = []
    for item in paths:
        path = Path(item)
        digest = sha256_file(path)
        if digest in seen:
            warnings.warn(
                f"Duplicate transcript: {path.name}; keeping {seen[digest].name} (same sha256)",
                DuplicateTranscriptWarning,
                stacklevel=2,
            )
            continue
        seen[digest] = path
        unique.append(path)
    return unique
