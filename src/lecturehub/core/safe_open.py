"""One allow-list for every "open with the default program" in the app (security review 2026-11-13).

``os.startfile`` runs whatever Windows associates with a file, so a planted ``.exe``, ``.py``, ``.chm``, ``.xll``
or a macro document (``.docm``/``.xlsm``) in a course folder would run with one click. Only folders and the
file types a student opens (documents, slides, sheets, images, audio/video, text, archives as files) pass.
Keep this module free of sqlite3 / index imports (D-050: the UI modules import it).
"""
from __future__ import annotations

import os
from pathlib import Path

ALLOWED_SUFFIXES = frozenset((
    # documents
    ".pdf", ".doc", ".docx", ".hwp", ".hwpx", ".odt", ".rtf", ".epub", ".html", ".htm",
    # slides and sheets (no macro-enabled variants: .pptm .ppsm .potm .xlsm .xlsb .xltm .xlam .docm .dotm)
    ".ppt", ".pptx", ".pps", ".ppsx", ".odp", ".xls", ".xlsx", ".ods", ".csv", ".tsv",
    # images
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".heic", ".svg",
    # audio and video
    ".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma", ".webm", ".mp4", ".m4v", ".mkv", ".mov",
    ".avi", ".wmv",
    # text
    ".txt", ".md", ".log", ".srt", ".vtt", ".json",
    # archives (opened as files in the user's archive program, never extracted or run here)
    ".zip", ".7z", ".rar", ".alz", ".egg", ".tar", ".gz",
))


def refusal(path: str | Path) -> str | None:
    """``None`` when ``path`` may be opened; otherwise the reason shown to the user."""
    target = Path(path)
    if target.is_dir():
        return None
    suffix = target.suffix.lower()
    if target.is_file() and suffix in ALLOWED_SUFFIXES and not target.name.endswith((" ", ".")):
        return None
    shown = suffix or "확장자 없음"
    return (f"이 형식({shown})은 앱에서 열지 않습니다(프로그램·스크립트·매크로 문서일 수 있음). "
            "필요하면 [폴더 열기]로 탐색기에서 직접 확인하세요.")


def startfile(path: str | Path, opener=None) -> None:
    """``os.startfile`` behind the allow-list. Raises :class:`PermissionError` with the reason."""
    reason = refusal(path)
    if reason:
        raise PermissionError(reason)
    (opener or os.startfile)(str(path))
