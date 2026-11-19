"""ffmpeg/ffprobe helpers: duration, silences, provider-derived chunk audio.

Source audio is only read. Derived files go to the job folder under _lecturehub.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from decimal import Decimal
from pathlib import Path

# Derived-audio profiles per provider (§8.3, transcribe.ps1).
PROFILES = {
    # transcribe.ps1: dynaudnorm=f=150:g=15, mono, 64 kbps mp3. Quiet (-47 dB) parts came back empty without it.
    "gemini_transcribe": ["-af", "dynaudnorm=f=150:g=15", "-ac", "1", "-b:a", "64k"],
    # MAI playground: 40 kbps / 16 kHz / mono mp3, ≤ 10 MB per file.
    "mai_playground": ["-ac", "1", "-ar", "16000", "-b:a", "40k"],
    # Groq Whisper: 16 kHz is what Whisper hears anyway; 48 kbps keeps a 30-min chunk ≈ 11 MB (< 25 MB free tier).
    "whisper_groq": ["-ac", "1", "-ar", "16000", "-b:a", "48k"],
}
PROFILE_BITRATE = {"gemini_transcribe": 64000, "mai_playground": 40000, "whisper_groq": 48000}

_SILENCE_START = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SILENCE_END = re.compile(r"silence_end:\s*(-?[\d.]+)")


class AudioToolError(RuntimeError):
    """ffmpeg/ffprobe missing or failed."""


def _tool(name: str) -> str:
    found = shutil.which(name)
    if not found:
        raise AudioToolError(f"{name}를 찾을 수 없습니다. 앱을 다시 설치해 보세요. "
                             "(소스에서 실행할 때: winget install Gyan.FFmpeg)")
    return found


def _run(arguments: list[str]) -> subprocess.CompletedProcess:
    from lecturehub.core import proc

    result = subprocess.run(arguments, capture_output=True, text=True, encoding="utf-8",
                            errors="replace", check=False, creationflags=proc.NO_WINDOW)
    if result.returncode:
        raise AudioToolError(f"{Path(arguments[0]).stem} 실패: {result.stderr[-1500:].strip()}")
    return result


def _seconds_to_ms(text: str) -> int:
    return int(Decimal(text) * 1000)  # int() truncates toward zero: floor for positive times


def duration_ms(path: Path) -> int:
    result = _run([_tool("ffprobe"), "-v", "error", "-show_entries", "format=duration",
                   "-of", "default=noprint_wrappers=1:nokey=1", str(path)])
    value = result.stdout.strip()
    if not value or value == "N/A":
        raise AudioToolError(f"재생 길이를 읽지 못했습니다: {path.name}")
    return _seconds_to_ms(value)


def detect_silences(path: Path, start_ms: int, end_ms: int, *, noise_db: float = -35.0,
                    min_silence_s: float = 0.5) -> list[tuple[int, int]]:
    """Silences inside [start_ms, end_ms] as absolute ms, via ffmpeg silencedetect."""
    if end_ms <= start_ms:
        return []
    result = _run([
        _tool("ffmpeg"), "-hide_banner", "-nostdin", "-ss", f"{start_ms / 1000:.3f}",
        "-t", f"{(end_ms - start_ms) / 1000:.3f}", "-i", str(path),
        "-af", f"silencedetect=noise={noise_db}dB:d={min_silence_s}", "-f", "null", "-",
    ])
    silences: list[tuple[int, int]] = []
    opened: int | None = None
    for line in result.stderr.splitlines():
        if match := _SILENCE_START.search(line):
            opened = max(0, _seconds_to_ms(match.group(1)))
        elif (match := _SILENCE_END.search(line)) and opened is not None:
            silences.append((start_ms + opened, start_ms + _seconds_to_ms(match.group(1))))
            opened = None
    if opened is not None:  # silence runs to the end of the window
        silences.append((start_ms + opened, end_ms))
    return [(max(start_ms, s), min(end_ms, e)) for s, e in silences if e > s]


def make_chunk(source: Path, out: Path, start_ms: int, end_ms: int, provider: str, *,
               threads: int | None = None) -> Path:
    """Encode [start_ms, end_ms) of ``source`` with the provider profile; never overwrite.

    ``threads=1`` while a recording is running (D-059/D-063: remote engines may work during class)."""
    if provider not in PROFILES:
        raise ValueError(f"알 수 없는 제공자 프로필: {provider}")
    out.parent.mkdir(parents=True, exist_ok=True)
    _run([
        _tool("ffmpeg"), "-hide_banner", "-nostdin", "-v", "error", "-n",
        *(["-threads", str(threads)] if threads else []),
        "-ss", f"{start_ms / 1000:.3f}", "-t", f"{(end_ms - start_ms) / 1000:.3f}", "-i", str(source),
        "-vn", *PROFILES[provider], str(out),
    ])
    return out
