"""Bounded WAV segments whose PCM survives an interrupted header update."""

from __future__ import annotations

import math
import os
import re
import struct
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Self

from .paths import ensure_writable

HEADER_SIZE = 44
_HEADER = struct.Struct("<4sI4s4sIHHIIHH4sI")
_SEGMENT_NAME = re.compile(r"segment_(\d{6,})\.wav\Z")


def pcm_header(sample_rate: int, data_bytes: int) -> bytes:
    """Return the recorder's fixed, little-endian, mono PCM16 header."""
    if data_bytes < 0 or data_bytes > 0xFFFFFFFF - 36 or data_bytes % 2:
        raise ValueError("WAV data length must be aligned PCM16 within the RIFF size limit")
    return _HEADER.pack(
        b"RIFF", 36 + data_bytes, b"WAVE", b"fmt ", 16, 1, 1,
        sample_rate, sample_rate * 2, 2, 16, b"data", data_bytes,
    )


def segment_paths(session_dir: Path) -> list[Path]:
    """Only recorder-owned segments, in numeric order; masters are never inputs."""
    found = []
    for path in Path(session_dir).glob("segment_*.wav"):
        match = _SEGMENT_NAME.fullmatch(path.name)
        if match:
            found.append((int(match.group(1)), path))
    found.sort()
    if found and [number for number, _ in found] != list(range(1, len(found) + 1)):
        raise ValueError("세그먼트 번호에 누락 또는 중복이 있습니다. 원본을 보존하고 확인하세요.")
    return [path for _, path in found]


@dataclass(frozen=True)
class SegmentInfo:
    path: Path
    sample_rate: int
    samples: int
    data_bytes: int
    trailing_bytes: int
    header_matches: bool

    @property
    def duration_ms(self) -> int:
        return self.samples * 1000 // self.sample_rate

    def as_dict(self) -> dict:
        return {
            "path": str(self.path), "sample_rate": self.sample_rate,
            "samples": self.samples, "total_ms": self.duration_ms,
            "trailing_bytes": self.trailing_bytes,
        }


def inspect_segment(path: Path, sample_rate: int, *, allow_stale_lengths: bool = False) -> SegmentInfo:
    """Validate the format before interpreting actual PCM length.

    Only the two RIFF length fields can be repaired safely: the writer writes
    the entire format header durably before any PCM, and only patches lengths.
    Never guess a data offset for a truncated or foreign-format header.
    """
    path = Path(path)
    with path.open("rb") as stream:
        header = stream.read(HEADER_SIZE)
    if len(header) != HEADER_SIZE:
        raise ValueError(f"불완전한 WAV 헤더: {path.name} (44바이트 미만)")
    expected = pcm_header(sample_rate, 0)
    if header[:4] != expected[:4] or header[8:40] != expected[8:40]:
        raise ValueError(f"지원하지 않거나 손상된 WAV 형식: {path.name} (모노 PCM16 필요)")
    actual_bytes = path.stat().st_size - HEADER_SIZE
    aligned_bytes = actual_bytes - actual_bytes % 2
    canonical = pcm_header(sample_rate, aligned_bytes)
    header_matches = header == canonical
    if not allow_stale_lengths and (not header_matches or actual_bytes % 2):
        raise ValueError(f"WAV 길이 불일치: {path.name}. 먼저 recorder recover를 실행하세요.")
    return SegmentInfo(path, sample_rate, aligned_bytes // 2, aligned_bytes, actual_bytes % 2, header_matches)


class SegmentWriter:
    """Write exact sample boundaries, with no empty segment after rotation.

    Calls are thread-safe, so a heartbeat may flush while the disk worker writes.
    Unbuffered PCM is persisted with fsync on rotation, flush, and close. At most
    the current segment's lengths may be stale after a process termination.
    """

    def __init__(self, session_dir: Path, sample_rate: int, segment_seconds: float = 60):
        if (isinstance(sample_rate, bool) or not isinstance(sample_rate, int)
                or not 1 <= sample_rate <= 768000):
            raise ValueError("sample_rate must be an integer from 1 to 768000")
        if not math.isfinite(segment_seconds) or segment_seconds <= 0:
            raise ValueError("segment_seconds must be finite and positive")
        frames = sample_rate * segment_seconds
        if isinstance(frames, float) and not frames.is_integer():
            raise ValueError("segment_seconds must describe a whole number of samples")
        self._segment_bytes = int(frames) * 2
        if self._segment_bytes < 2 or self._segment_bytes > 0xFFFFFFFF - 36:
            raise ValueError("segment size exceeds PCM RIFF limits")
        self.session_dir = ensure_writable(Path(session_dir))
        self.session_dir.mkdir(parents=True, exist_ok=True)
        if any(self.session_dir.glob("segment_*.wav")):
            raise FileExistsError("세션 폴더에 이미 세그먼트가 있습니다. 새 세션 폴더를 사용하세요.")
        self.sample_rate = sample_rate
        self._lock = threading.RLock()
        self._stream: BinaryIO | None = None
        self._paths: list[Path] = []
        self._current_bytes = 0
        self._total_bytes = 0
        self._closed = False

    @property
    def total_samples(self) -> int:
        with self._lock:
            return self._total_bytes // 2

    @property
    def segment_number(self) -> int:
        with self._lock:
            return len(self._paths)

    @property
    def segment_paths(self) -> tuple[Path, ...]:
        with self._lock:
            return tuple(self._paths)

    def _open_segment(self) -> None:
        path = ensure_writable(self.session_dir / f"segment_{len(self._paths) + 1:06d}.wav")
        stream = path.open("xb+", buffering=0)
        self._stream = stream
        self._paths.append(path)
        self._current_bytes = 0
        stream.write(pcm_header(self.sample_rate, 0))
        os.fsync(stream.fileno())

    def write(self, data: bytes) -> None:
        if len(data) % 2:
            raise ValueError("PCM16 input must contain complete two-byte samples")
        with self._lock:
            if self._closed:
                raise ValueError("SegmentWriter is closed")
            remaining = memoryview(data)
            while remaining:
                if self._stream is None:
                    self._open_segment()
                count = min(len(remaining), self._segment_bytes - self._current_bytes)
                written = self._stream.write(remaining[:count])
                if not written:
                    raise OSError("WAV write made no progress")
                self._current_bytes += written
                self._total_bytes += written
                remaining = remaining[written:]
                if self._current_bytes == self._segment_bytes:
                    self._finish_segment()

    def flush(self) -> None:
        with self._lock:
            if self._stream is None:
                return
            stream = self._stream
            # Persist PCM before publishing its length. The header's structural
            # fields are immutable, even when a process dies during this patch.
            stream.flush()
            os.fsync(stream.fileno())
            position = stream.tell()
            stream.seek(4)
            stream.write(struct.pack("<I", 36 + self._current_bytes))
            stream.seek(40)
            stream.write(struct.pack("<I", self._current_bytes))
            stream.seek(position)
            stream.flush()
            os.fsync(stream.fileno())

    def _finish_segment(self) -> None:
        try:
            self.flush()
        finally:
            if self._stream is not None:
                self._stream.close()
                self._stream = None

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                try:
                    self._finish_segment()
                finally:
                    self._closed = True

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
