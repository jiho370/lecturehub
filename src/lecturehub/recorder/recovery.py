"""Conservative recovery of recorder-owned, unfinished WAV segments."""

from __future__ import annotations

import json
import os
import struct
from pathlib import Path

from .heartbeat import SessionLock, write_json
from .paths import ensure_writable
from .segments import inspect_segment, segment_paths


def read_json(path: Path, *, required: bool = False) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        if not required:
            return {}
        raise ValueError(f"필수 세션 파일이 없습니다: {path.name}") from None
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"UTF-8 JSON 파일을 읽을 수 없습니다: {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"JSON 객체가 필요합니다: {path.name}")  # noqa: TRY004 - invalid file content
    return value


def session_sample_rate(session_dir: Path) -> int:
    metadata = read_json(session_dir / "session.json", required=True)
    rate = metadata.get("sample_rate")
    if isinstance(rate, bool) or not isinstance(rate, int) or not 1 <= rate <= 768000:
        raise ValueError("session.json에 유효한 정수 sample_rate가 필요합니다.")
    return rate


def validate_recorded_total(metadata: dict, total_samples: int, sample_rate: int,
                            source: str, *, minimum: bool = False) -> None:
    """Detect missing/truncated tails even when remaining filenames are contiguous.

    Recorder totals are exact sample counts. Legacy millisecond-only sidecars
    used floor, so compare in that same unit; a sidecar without either count
    cannot establish completeness. A heartbeat is only a durable lower bound.
    """
    key = "total_samples" if "total_samples" in metadata else "total_ms"
    if key not in metadata:
        return
    expected = metadata[key]
    if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
        raise ValueError(f"{source}의 {key}는 0 이상 정수여야 합니다.")
    actual = total_samples if key == "total_samples" else total_samples * 1000 // sample_rate
    mismatch = actual < expected if minimum else actual != expected
    if mismatch:
        bound = "최소 " if minimum else ""
        raise ValueError(
            f"세그먼트 합과 {source} 기록이 일치하지 않습니다: {key}={actual}, {bound}기록값={expected}. "
            "누락·손상된 세그먼트를 확인하세요."
        )


def recover_session(session_dir: Path) -> dict:
    """Repair only the final unfinished segment; preserve all PCM bytes.

    A recovery report does not pretend that a normal stop took place. In
    particular stop.json is never manufactured, replaced, or modified here.
    """
    session_dir = ensure_writable(Path(session_dir))
    if not session_dir.is_dir():
        raise ValueError(f"세션 폴더가 없습니다: {session_dir}")
    with SessionLock(session_dir):
        rate = session_sample_rate(session_dir)
        paths = segment_paths(session_dir)
        stop_exists = (session_dir / "stop.json").exists()
        stop = read_json(session_dir / "stop.json")
        heartbeat = read_json(session_dir / "heartbeat.json")
        infos = [inspect_segment(path, rate, allow_stale_lengths=(not stop_exists and i == len(paths) - 1))
                 for i, path in enumerate(paths)]
        total_samples = sum(info.samples for info in infos)
        if stop_exists:
            validate_recorded_total(stop, total_samples, rate, "stop.json")
        else:
            previous = read_json(session_dir / "recovery.json")
            if previous.get("recovered"):
                validate_recorded_total(previous, total_samples, rate, "recovery.json")
            validate_recorded_total(heartbeat, total_samples, rate, "heartbeat.json", minimum=True)
        repaired = False
        if infos and not stop_exists and not infos[-1].header_matches:
            last = infos[-1]
            with ensure_writable(last.path).open("r+b", buffering=0) as stream:
                stream.seek(4)
                stream.write(struct.pack("<I", 36 + last.data_bytes))
                stream.seek(40)
                stream.write(struct.pack("<I", last.data_bytes))
                os.fsync(stream.fileno())
            repaired = True
        cause = stop.get("cause", "unknown") if stop_exists else ("process_killed" if heartbeat else "unknown")
        certainty = stop.get("certainty", "confirmed") if stop_exists else (
            "estimated" if heartbeat else "unknown")
        if cause == "unknown":
            certainty = "unknown"
        report = {
            "session_dir": str(session_dir), "interrupted": not stop_exists,
            "cause": cause, "certainty": certainty,
            "input_partial": bool(not stop_exists or stop.get("partial") or stop.get("error")
                                  or stop.get("dropped_samples") or stop.get("capture_status_count")),
            "recovered": bool(infos) and not stop_exists,
            "header_repaired": repaired, "segments": [info.as_dict() for info in infos],
            "total_samples": total_samples, "total_ms": total_samples * 1000 // rate,
            "sample_rate": rate,
            "evidence": {"stop_json_present": stop_exists, "heartbeat": heartbeat},
            "trailing_bytes_preserved": sum(info.trailing_bytes for info in infos),
        }
        write_json(ensure_writable(session_dir / "recovery.json"), report)
        return report
