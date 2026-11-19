"""One-click class recording settings and timetable lookup.

The settings file keeps Korean paths out of shortcuts and batch files. Lookup
order: explicit path, LECTUREHUB_RECORDER_CONFIG, %APPDATA%\\lecturehub\\recorder.toml.
"""
from __future__ import annotations

import csv
import io
import os
import tomllib
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path

from lecturehub.core.textio import read_text_any

ENV_VAR = "LECTUREHUB_RECORDER_CONFIG"
_WEEKDAYS = "월화수목금토일"


class ConfigError(ValueError):
    """The class-recording settings are missing or invalid."""


@dataclass(frozen=True)
class ClassConfig:
    path: Path
    out_dir: Path
    device_name: str | None
    mode: str = "mic"
    quiet_dbfs: float = -50.0
    timetable: Path | None = None
    lead_minutes: int = 15
    live_transcription: bool = True  # D-059: transcribe consented classes while recording (off: false)


def default_config_path() -> Path:
    if os.environ.get(ENV_VAR):
        return Path(os.environ[ENV_VAR])
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(base) / "lecturehub" / "recorder.toml"


def load_config(path: Path | None = None) -> ClassConfig:
    path = Path(path) if path else default_config_path()
    if not path.is_file():
        raise ConfigError(f"녹음 설정 파일이 없습니다: {path}")
    try:
        data = tomllib.loads(read_text_any(path))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"녹음 설정 파일 형식 오류: {path}: {exc}") from exc
    section = data.get("recorder")
    if not isinstance(section, dict):
        raise ConfigError(f"[recorder] 절이 필요합니다: {path}")
    out_dir = section.get("out_dir")
    if not isinstance(out_dir, str) or not out_dir.strip():
        raise ConfigError("recorder.out_dir(녹음 저장 위치)가 필요합니다.")
    mode = section.get("mode", "mic")
    if mode not in ("mic", "loopback"):
        raise ConfigError("recorder.mode는 mic 또는 loopback이어야 합니다.")
    device_name = section.get("device_name") or None
    timetable = section.get("timetable") or None
    try:
        quiet = float(section.get("quiet_dbfs", -50.0))
        lead = int(section.get("course_lead_minutes", 15))
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"숫자 설정 오류: {exc}") from exc
    return ClassConfig(
        path=path, out_dir=Path(out_dir), device_name=device_name, mode=mode, quiet_dbfs=quiet,
        timetable=Path(timetable) if timetable else None, lead_minutes=lead,
        live_transcription=bool(section.get("live_transcription", True)),
    )


def _clock(text: str) -> time:
    hour, minute = text.strip().split(":")
    return time(int(hour), int(minute))


def current_course(timetable: Path, now: datetime, lead_minutes: int = 15) -> str:
    """Return the course folder name when exactly one class matches ``now``.

    A class matches from ``lead_minutes`` before its start until its end.
    No match, several matches, or an unreadable row yields "" (left blank
    rather than guessed). The timetable is only read.
    """
    rows = csv.DictReader(io.StringIO(read_text_any(timetable)))
    day = _WEEKDAYS[now.weekday()]
    found: set[str] = set()
    for row in rows:
        try:
            if (row.get("요일") or "").strip() != day:
                continue
            start = datetime.combine(now.date(), _clock(row["시작"])) - timedelta(minutes=lead_minutes)
            end = datetime.combine(now.date(), _clock(row["종료"]))
        except (KeyError, ValueError, AttributeError):
            continue
        if start <= now.replace(tzinfo=None) <= end:
            name = (row.get("폴더") or row.get("과목") or "").strip()
            if name:
                found.add(name)
    return found.pop() if len(found) == 1 else ""
