"""Discover supported existing files without modifying the semester tree."""

from dataclasses import dataclass
from pathlib import Path

from lecturehub.core import settings
from lecturehub.parsers.dedupe import sha256_file

AUDIO_EXTENSIONS = {".m4a", ".mp3", ".wav", ".flac", ".aac", ".ogg", ".wma"}
# .docx since D-078 (its text is read like .hwp/.hwpx: materials/doctext.py)
MATERIAL_EXTENSIONS = {".pdf", ".ppt", ".pptx", ".pps", ".ppsx", ".hwp", ".hwpx", ".docx"}
NOTE_FOLDERS = {"정리본"}  # compared without spaces; older names come from settings (legacy_note_folders)


def _walk(folder: Path):
    """Files under a folder in name order, skipping folders whose name starts with _ or . (work files)."""
    for path in sorted(folder.iterdir()):
        if path.is_dir():
            if not path.name.startswith(("_", ".")) and not path.is_symlink() and not path.is_junction():
                yield from _walk(path)
        else:
            yield path


@dataclass(frozen=True)
class SourceFile:
    path: Path
    kind: str
    folder: str


def scan_sources(root: Path, folders: list[str], *, explicit_transcripts: list[Path] | None = None) -> list[SourceFile]:
    """Ignore app output, chunks, backups and links outside the supplied root."""
    root = root.resolve()
    sources: list[SourceFile] = []

    def add(path: Path, kind: str, folder: str) -> None:
        linked = any(part.is_symlink() or part.is_junction() for part in (path, *path.parents) if part != root)
        if path.is_file() and path.resolve().is_relative_to(root) and not linked:
            sources.append(SourceFile(path, kind, folder))

    for container, kind, suffixes in (
        ("녹음본", "media", AUDIO_EXTENSIONS), ("전사본", "transcript", {".txt"}),
    ):
        parent = root / container
        if not parent.is_dir():
            continue
        for folder in sorted(parent.iterdir()):
            if not folder.is_dir() or (folder.name.startswith("_") and folder.name != "_미분류"):
                continue
            for path in sorted(folder.iterdir()):
                if path.suffix.lower() in suffixes:
                    add(path, kind, folder.name)
    for folder in folders:
        parent = root / folder
        if not parent.is_dir():
            continue
        for path in sorted(parent.iterdir()):
            if path.is_dir():
                # sorted course folders (index/organize.py): slides and handouts at any depth; notes only
                # in the notes folder, so an html inside an assignment or a manual is not taken for one
                notes = path.name.replace(" ", "") in NOTE_FOLDERS | settings.legacy_note_folders()
                if not path.name.startswith(("_", ".")):
                    for child in _walk(path):
                        if child.suffix.lower() in MATERIAL_EXTENSIONS:
                            add(child, "material", folder)
                        elif notes and child.suffix.lower() in {".html", ".htm"}:
                            add(child, "note", folder)
            elif path.suffix.lower() in MATERIAL_EXTENSIONS:
                add(path, "material", folder)
            elif path.suffix.lower() in {".html", ".htm"}:
                add(path, "note", folder)
    for relative in explicit_transcripts or []:
        if len(relative.parts) == 2 and relative.parts[0] in folders and relative.suffix.lower() == ".txt":
            add(root / relative, "transcript", relative.parts[0])
    return list(dict.fromkeys(sources))


def source_inventory(root: Path, sources: list[SourceFile]) -> dict[str, str]:
    """Capture content hashes for the declared inputs, never for generated DB files."""
    root = root.resolve()
    return {str(source.path.relative_to(root)): sha256_file(source.path) for source in sources}


def verify_sources(root: str | Path, hashes: dict[str, str]) -> list[str]:
    """Return changed/missing input filenames; safe to call after an index run."""
    root = Path(root).resolve()
    changed: list[str] = []
    for relative, expected in hashes.items():
        path = root / relative
        try:
            valid = path.resolve().is_relative_to(root) and sha256_file(path) == expected
        except OSError:
            valid = False
        if not valid:
            changed.append(path.name)
    return changed
