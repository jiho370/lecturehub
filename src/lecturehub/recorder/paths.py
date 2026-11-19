"""Recorder output boundary for the configured semester library."""
from pathlib import Path

from lecturehub.core import settings

SEMESTER_ROOT: Path | None = None  # tests may set this; otherwise the configured data folder is used


def protected_root() -> Path | None:
    return SEMESTER_ROOT if SEMESTER_ROOT is not None else settings.data_root()


def ensure_writable(path: Path) -> Path:
    """Refuse writes inside the library except under ``_lecturehub``."""
    target = Path(path).expanduser().resolve()
    configured = protected_root()
    if configured is None:
        return target
    root = configured.resolve()
    if target.is_relative_to(root) and not target.is_relative_to(root / "_lecturehub"):
        raise ValueError("실제 학기 폴더에는 _lecturehub 안에만 쓸 수 있습니다.")
    return target
