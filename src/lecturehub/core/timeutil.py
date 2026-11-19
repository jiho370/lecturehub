"""Parse and display transcript times without floating-point rounding."""

import re
from datetime import timedelta, timezone

# The timetable, lecture dates and "today" are Korean wall-clock time (KST has no daylight saving).
# Stored times stay epoch ms; this zone is only for turning them into a date/hour of the class day.
KST = timezone(timedelta(hours=9))

_TIMESTAMP = re.compile(
    r"(?:(?P<hours>[0-9]+):)?(?P<minutes>[0-9]+):(?P<seconds>[0-5][0-9])"
    r"(?:\.(?P<fraction>[0-9]{1,3}))?"
)


def _require_ms(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer number of milliseconds")
    if value < 0:
        raise ValueError(f"{name} must not be negative")


def parse_timestamp(text: str) -> int:
    """Parse MM:SS or H:MM:SS, optionally bracketed, into integer ms.

    Decimal seconds may have one to three digits. In a two-part timestamp,
    minutes may exceed 59 (for example ``90:00``). With an hour field, minutes
    must be below 60. Surrounding whitespace is ignored.
    """
    value = text.strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1].strip()
    match = _TIMESTAMP.fullmatch(value)
    if match is None:
        raise ValueError(f"Invalid timestamp: {text!r}; expected MM:SS or H:MM:SS")
    hours = int(match["hours"] or 0)
    minutes = int(match["minutes"])
    if match["hours"] is not None and minutes >= 60:
        raise ValueError(f"Invalid timestamp: {text!r}; minutes must be below 60 after hours")
    seconds = int(match["seconds"])
    milliseconds = int((match["fraction"] or "0").ljust(3, "0"))
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + milliseconds


def format_timestamp(ms: int) -> str:
    """Display integer ms as MM:SS or H:MM:SS, flooring fractional seconds."""
    _require_ms(ms, "ms")
    hours, remainder = divmod(ms // 1000, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def add_chunk_offset(ms: int, offset_ms: int) -> int:
    """Add a chunk's actual ``start_ms`` to its local timestamp, without rounding.

    Callers pass the measured boundary from ChunkPlan, never an estimated
    chunk index multiplied by a nominal duration.
    """
    _require_ms(ms, "ms")
    _require_ms(offset_ms, "offset_ms")
    return ms + offset_ms
