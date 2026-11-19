"""Atomic JSON files and the _lecturehub write boundary for track C."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path


def app_dir(root: Path) -> Path:
    return Path(root) / "_lecturehub"


def ensure_inside(path: Path, root: Path) -> Path:
    """Refuse any write outside <root>/_lecturehub (AGENTS §2.1)."""
    target, base = Path(path).resolve(), app_dir(root).resolve()
    if not target.is_relative_to(base):
        raise ValueError(f"_lecturehub 밖에는 쓰지 않습니다: {target}")
    return target


def write_json(path: Path, payload) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    for attempt in range(20):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.01)


def read_json(path: Path, default=None):
    """The file's JSON, or ``default`` when it is missing or broken.

    A broken file (these are written atomically, so: a damaged disk or a hand edit) used to raise here, and the
    callers on background threads then failed without a word. It now reads as "not there" — for consent.json that
    means "no consent given", so the app asks again rather than sending anything — and the broken file is kept as
    ``<name>.corrupt-<time>`` with a line in the app log."""
    path = Path(path)
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return default
    except ValueError:
        _keep_broken(path)
        return default


def _keep_broken(path: Path) -> None:
    from lecturehub.core import applog

    try:
        if not list(path.parent.glob(f"{path.name}.corrupt-*")):
            path.with_name(f"{path.name}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}").write_bytes(path.read_bytes())
        applog.LOG.error("읽을 수 없는 파일을 건너뜀: %s", path.name)
    except OSError:
        pass
