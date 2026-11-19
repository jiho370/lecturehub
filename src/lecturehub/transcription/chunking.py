"""Silence-aware chunk plans with measured boundaries in integer ms (§8.3).

Boundaries are chosen near evenly spaced nominal points: the longest silence
within ±window of the nominal point, cut at its middle. Hard limits win over
the window: no chunk may exceed ``max_chunk_ms`` (SPLIT-001), so the search
range is clamped to what keeps every remaining chunk within the limit.
"""
from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass

Silence = tuple[int, int]  # absolute (start_ms, end_ms)
SilenceFinder = Callable[[int, int], Sequence[Silence]]


@dataclass(frozen=True)
class Chunk:
    index: int
    start_ms: int
    end_ms: int

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


@dataclass(frozen=True)
class Boundary:
    nominal_ms: int
    actual_ms: int
    method: str  # "silence" | "nominal"
    silence: Silence | None = None


@dataclass(frozen=True)
class ChunkPlan:
    duration_ms: int
    chunks: tuple[Chunk, ...]
    boundaries: tuple[Boundary, ...]

    def as_dict(self) -> dict:
        return {
            "duration_ms": self.duration_ms,
            "chunks": [asdict(chunk) for chunk in self.chunks],
            "boundaries": [asdict(boundary) for boundary in self.boundaries],
        }


def plan_chunks(duration_ms: int, max_chunk_ms: int, window_ms: int,
                find_silences: SilenceFinder | None = None) -> ChunkPlan:
    """Split ``duration_ms`` into the fewest chunks of at most ``max_chunk_ms``."""
    for name, value in (("duration_ms", duration_ms), ("max_chunk_ms", max_chunk_ms), ("window_ms", window_ms)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} must be a nonnegative integer")
    if duration_ms == 0 or max_chunk_ms == 0:
        raise ValueError("duration_ms and max_chunk_ms must be positive")
    count = math.ceil(duration_ms / max_chunk_ms)
    cuts: list[int] = [0]
    boundaries: list[Boundary] = []
    for k in range(1, count):
        previous = cuts[-1]
        nominal = duration_ms * k // count
        lowest = max(previous + 1, duration_ms - (count - k) * max_chunk_ms)
        highest = min(previous + max_chunk_ms, duration_ms - 1)
        lo, hi = max(lowest, nominal - window_ms), min(highest, nominal + window_ms)
        chosen: Silence | None = None
        if find_silences is not None and lo < hi:
            best = 0
            for start, end in find_silences(lo, hi):
                overlap = min(end, hi) - max(start, lo)
                if overlap > best:
                    best, chosen = overlap, (int(start), int(end))
        if chosen is not None:
            cut = (max(chosen[0], lo) + min(chosen[1], hi)) // 2
            boundaries.append(Boundary(nominal, cut, "silence", chosen))
        else:
            cut = min(max(nominal, lowest), highest)
            boundaries.append(Boundary(nominal, cut, "nominal"))
        cuts.append(cut)
    cuts.append(duration_ms)
    chunks = tuple(Chunk(i, cuts[i], cuts[i + 1]) for i in range(count))
    return ChunkPlan(duration_ms, chunks, tuple(boundaries))


def estimated_bytes(duration_ms: int, bitrate_bps: int) -> int:
    """CBR size estimate: bitrate × duration / 8 (§8.3); verify after encoding."""
    return bitrate_bps * duration_ms // 8000


def parse_bitrate(text: str | int) -> int:
    """'40k' → 40000."""
    if isinstance(text, int):
        return text
    value = str(text).strip().lower()
    return int(float(value[:-1]) * 1000) if value.endswith("k") else int(value)
