"""Obtain measured audio metadata from ffprobe, with no fabricated fallbacks."""

import json
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path


class MediaProbeError(ValueError):
    """An audio input cannot be measured or ffprobe is unavailable."""


@dataclass(frozen=True)
class MediaMetadata:
    duration_ms: int
    codec: str
    sample_rate: int
    channels: int
    recorded_start: int | None = None


def probe_media(path: Path, ffprobe: str | None = None) -> MediaMetadata:
    executable = ffprobe or shutil.which("ffprobe")
    if not executable:
        raise MediaProbeError("ffprobe unavailable")
    command = [
        str(executable), "-v", "error", "-select_streams", "a:0", "-show_entries",
        ("stream=codec_name,sample_rate,channels:stream_tags=creation_time:"
         "format=duration:format_tags=creation_time"), "-of", "json", str(path),
    ]
    try:
        result = subprocess.run(
            command, capture_output=True, encoding="utf-8", errors="replace", timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MediaProbeError("ffprobe unavailable or timed out") from exc
    if result.returncode:
        raise MediaProbeError("ffprobe could not read audio")
    try:
        document = json.loads(result.stdout)
        stream = document["streams"][0]
        duration = Decimal(document["format"]["duration"])
        if not duration.is_finite() or duration < 0:
            raise ValueError
        codec, sample_rate, channels = stream["codec_name"], int(stream["sample_rate"]), int(stream["channels"])
        if not codec or sample_rate < 1 or channels < 1:
            raise ValueError
        recorded = None
        tags = document.get("format", {}).get("tags", {}) | stream.get("tags", {})
        if tags.get("creation_time"):
            try:
                timestamp = datetime.fromisoformat(tags["creation_time"])
                if timestamp.tzinfo is not None:
                    recorded = int(timestamp.timestamp() * 1000)
            except ValueError:
                pass
        return MediaMetadata(int(duration * 1000), codec, sample_rate, channels, recorded)
    except (KeyError, IndexError, TypeError, ValueError, InvalidOperation) as exc:
        raise MediaProbeError("ffprobe returned incomplete audio metadata") from exc
