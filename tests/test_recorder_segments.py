"""Synthetic PCM only: STOP-002, STOP-006, exact rotation and master safety."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import time
import wave
from pathlib import Path

import pytest

from lecturehub.recorder import finalize as finalizer
from lecturehub.recorder.finalize import finalize_session
from lecturehub.recorder.heartbeat import SessionLock
from lecturehub.recorder.recovery import recover_session
from lecturehub.recorder.segments import SegmentWriter, inspect_segment, segment_paths


@pytest.fixture
def session(tmp_path):
    folder = tmp_path / "구조역학 녹음 (합성)"
    folder.mkdir()
    (folder / "session.json").write_text(json.dumps({"sample_rate": 8000}), encoding="utf-8")
    return folder


def _stopped(session: Path) -> None:
    (session / "stop.json").write_text(
        json.dumps({"cause": "user_stop", "certainty": "confirmed"}), encoding="utf-8",
    )


def _hashes(session: Path) -> dict:
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in segment_paths(session)}


def test_segment_rotation_exact_minutes_and_sample_sum(session):
    """STOP-006: PCM order and total samples survive crossing many boundaries."""
    count = 8000 * 125 + 7
    payload = b"\x13\x27" * count
    with SegmentWriter(session, 8000) as writer:
        for offset in range(0, len(payload), 3334):
            writer.write(payload[offset:offset + 3334])
        assert writer.total_samples == count
        assert writer.segment_number == 3
    lengths = []
    data = bytearray()
    for path in writer.segment_paths:
        with wave.open(str(path), "rb") as stream:
            assert stream.getparams()[:3] == (1, 2, 8000)
            lengths.append(stream.getnframes())
            data.extend(stream.readframes(stream.getnframes()))
    assert lengths == [480000, 480000, 40007]
    assert data == payload


def test_rotation_does_not_create_empty_tail_and_refuses_overwrite(session):
    with SegmentWriter(session, 8000, segment_seconds=0.25) as writer:
        writer.write(b"\0\0" * 4000)
    assert len(writer.segment_paths) == 2
    with pytest.raises(FileExistsError):
        SegmentWriter(session, 8000)


def test_flush_makes_header_readable_before_close(session):
    with SegmentWriter(session, 8000) as writer:
        writer.write(b"\0\0" * 800)
        writer.flush()
        info = inspect_segment(writer.segment_paths[0], 8000)
        assert info.samples == 800
        writer.write(b"\0\0" * 400)
    assert inspect_segment(writer.segment_paths[0], 8000).samples == 1200


def test_bad_pcm_and_writer_arguments_fail_without_partial_write(session):
    for rate, seconds in [(0, 1), (True, 1), (8000, 0), (8000, float("nan")), (8000, 0.00001)]:
        with pytest.raises(ValueError):
            SegmentWriter(session, rate, seconds)
    writer = SegmentWriter(session, 8000)
    with pytest.raises(ValueError):
        writer.write(b"\0")
    writer.close()
    assert writer.total_samples == 0
    assert writer.segment_paths == ()
    with pytest.raises(ValueError):
        writer.write(b"\0\0")


def test_STOP_006_recover_actual_data_length_and_preserve_samples(session):
    payload = b"\x01\x02" * 12345
    with SegmentWriter(session, 8000) as writer:
        writer.write(payload)
    path = writer.segment_paths[-1]
    with path.open("r+b") as stream:
        stream.seek(4)
        stream.write(b"\xff" * 4)
        stream.seek(40)
        stream.write(b"\0" * 4)
    (session / "heartbeat.json").write_text('{"total_samples":10000}', encoding="utf-8")
    result = recover_session(session)
    assert result["total_samples"] == 12345
    assert result["cause"] == "process_killed"
    assert result["certainty"] == "estimated"
    assert result["header_repaired"] is True
    assert result["recovered"] is True
    assert not (session / "stop.json").exists()
    with wave.open(str(path), "rb") as stream:
        assert stream.getnframes() == 12345
        assert stream.readframes(12345) == payload
    assert recover_session(session)["header_repaired"] is False


def test_STOP_002_real_process_kill_recovers_unflushed_header(session):
    code = """
import sys, time
from pathlib import Path
from lecturehub.recorder.segments import SegmentWriter
from lecturehub.recorder.heartbeat import SessionLock
p = Path(sys.argv[1])
with SessionLock(p):
    writer = SegmentWriter(p, 8000)
    writer.write(b'\\x34\\x12' * 16003)
    (p / 'ready').write_text('yes')
    time.sleep(30)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(session)], cwd=Path(__file__).resolve().parents[1],
    )

    def kill_tree():
        # Windows venv python.exe can be a redirector. Terminate its child
        # interpreter too, so the process holding SessionLock is actually gone.
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True, check=True, cwd=Path(__file__).resolve().parents[1],
            )
        else:
            process.kill()
        process.wait(timeout=10)

    try:
        deadline = time.monotonic() + 10
        while not (session / "ready").exists():
            assert process.poll() is None, "child exited before writing"
            if time.monotonic() > deadline:
                pytest.fail("child did not create synthetic capture")
            time.sleep(0.02)
        with pytest.raises(RuntimeError):
            recover_session(session)
        kill_tree()
        result = recover_session(session)
        assert result["total_samples"] == 16003
        assert result["certainty"] == "unknown"
        assert result["cause"] == "unknown"
        assert result["header_repaired"]
    finally:
        if process.poll() is None:
            kill_tree()
        process.wait(timeout=10)


def test_partial_last_sample_is_preserved_and_not_counted(session, monkeypatch):
    with SegmentWriter(session, 8000) as writer:
        writer.write(b"\x01\x02" * 8000)
    path = writer.segment_paths[0]
    with path.open("ab") as stream:
        stream.write(b"\x7f")
    original = path.read_bytes()
    result = recover_session(session)
    assert result["trailing_bytes_preserved"] == 1
    assert result["total_samples"] == 8000
    assert path.read_bytes() == original
    monkeypatch.setattr(finalizer.shutil, "which", lambda _: None)
    report = finalize_session(session)
    with wave.open(report["master"], "rb") as stream:
        assert stream.getnframes() == 8000
    assert path.read_bytes() == original


@pytest.mark.parametrize("corruption", ["short_header", "stereo", "wrong_rate", "missing_segment"])
def test_recovery_refuses_ambiguous_format_without_modifying_it(session, corruption):
    with SegmentWriter(session, 8000, segment_seconds=1) as writer:
        writer.write(b"\0\0" * 16001)
    path = writer.segment_paths[-1]
    data = bytearray(path.read_bytes())
    if corruption == "short_header":
        path.write_bytes(data[:30])
    elif corruption == "stereo":
        data[22:24] = struct.pack("<H", 2)
        path.write_bytes(data)
    elif corruption == "wrong_rate":
        data[24:28] = struct.pack("<I", 44100)
        path.write_bytes(data)
    else:
        writer.segment_paths[1].unlink()
    before = _hashes(session) if corruption != "missing_segment" else path.read_bytes()
    with pytest.raises(ValueError):
        recover_session(session)
    after = _hashes(session) if corruption != "missing_segment" else path.read_bytes()
    assert after == before
    assert not (session / "recovery.json").exists()


def test_recovery_does_not_touch_closed_session(session):
    with SegmentWriter(session, 8000) as writer:
        writer.write(b"\0\0" * 3000)
    _stopped(session)
    before = _hashes(session)
    stop_before = (session / "stop.json").read_bytes()
    result = recover_session(session)
    assert not result["interrupted"]
    assert not result["header_repaired"]
    assert result["cause"] == "user_stop"
    assert result["certainty"] == "confirmed"
    assert _hashes(session) == before
    assert (session / "stop.json").read_bytes() == stop_before


def test_finalize_wav_fallback_and_repeat_preserve_all_originals(session, monkeypatch):
    with SegmentWriter(session, 8000, segment_seconds=1) as writer:
        writer.write(b"\x11\x22" * 12345)
    _stopped(session)
    originals = _hashes(session)
    sentinel = session / "master_previous.wav"
    sentinel.write_bytes(b"do not overwrite")
    monkeypatch.setattr(finalizer.shutil, "which", lambda _: None)
    first = finalize_session(session)
    second = finalize_session(session)
    assert first["master"] != second["master"]
    assert first["verified"]
    assert first["format"] == "wav"
    assert first["total_ms"] == 1543
    with wave.open(first["master"], "rb") as stream:
        assert stream.getnframes() == 12345
        assert stream.readframes(12345) == b"\x11\x22" * 12345
    assert _hashes(session) == originals
    assert sentinel.read_bytes() == b"do not overwrite"
    assert len(list(session.glob("finalize_*.json"))) == 2


def test_finalize_requires_recovery_of_interrupted_session(session):
    with SegmentWriter(session, 8000) as writer:
        writer.write(b"\0\0" * 100)
    with pytest.raises(ValueError, match="recover"):
        finalize_session(session)
    assert not list(session.glob("master_*.wav"))


def test_finalize_refuses_active_session_even_when_stop_file_exists(session):
    _stopped(session)
    with SessionLock(session), pytest.raises(RuntimeError):
        finalize_session(session)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg is optional; fallback covered separately")
def test_finalize_real_aac_and_duration_verification(session):
    (session / "session.json").write_text('{"sample_rate":48000}', encoding="utf-8")
    with SegmentWriter(session, 48000, segment_seconds=1) as writer:
        writer.write(b"\x01\x00" * 117777)
    _stopped(session)
    originals = _hashes(session)
    result = finalize_session(session, master_format="aac")
    assert result["format"] == "aac_m4a"
    assert Path(result["master"]).is_file()
    assert Path(result["wav_master"]).is_file()
    assert abs(result["duration_difference_ms"]) <= 100
    assert _hashes(session) == originals


def test_finalize_default_is_mp3_96k_mono_and_verified(session):
    """User decision 2026-09-29: MP3 masters above the 64 kbps upload profile."""
    (session / "session.json").write_text('{"sample_rate":48000}', encoding="utf-8")
    with SegmentWriter(session, 48000, segment_seconds=1) as writer:
        writer.write(b"\x01\x00" * 117777)
    _stopped(session)
    result = finalize_session(session)
    assert result["format"] == "mp3" and result["master"].endswith(".mp3") and result["bitrate"] == "96k"
    assert result["bitrate_policy"] == "user_default_mp3_96k" and result["fallback"] is None
    assert abs(result["duration_difference_ms"]) <= 100 and Path(result["wav_master"]).is_file()


def test_failed_aac_keeps_wav_and_segments(session, monkeypatch):
    with SegmentWriter(session, 8000) as writer:
        writer.write(b"\0\0" * 100)
    _stopped(session)
    originals = _hashes(session)
    monkeypatch.setattr(finalizer.shutil, "which", lambda _: "ffmpeg")

    def fail(_):
        raise RuntimeError("simulated codec failure")

    monkeypatch.setattr(finalizer, "_run", fail)
    with pytest.raises(RuntimeError, match="codec failure"):
        finalize_session(session)
    assert len(list(session.glob("master_*.wav"))) == 1
    assert _hashes(session) == originals
    assert not list(session.glob("finalize_*.json"))


def test_aac_duration_mismatch_is_not_reported_as_verified(session, monkeypatch):
    with SegmentWriter(session, 8000) as writer:
        writer.write(b"\0\0" * 8000)
    _stopped(session)
    originals = _hashes(session)
    monkeypatch.setattr(finalizer.shutil, "which", lambda _: "ffmpeg")
    monkeypatch.setattr(finalizer, "_run", lambda _: None)
    monkeypatch.setattr(finalizer, "_encoded_duration_ms", lambda *_: 1101)
    with pytest.raises(RuntimeError, match="길이 검증 실패"):
        finalize_session(session)
    assert _hashes(session) == originals
    assert len(list(session.glob("master_*.wav"))) == 1
    assert not list(session.glob("finalize_*.json"))


@pytest.mark.parametrize("count_key,count,expected", [
    ("total_samples", 16001, 16001), ("total_ms", 17001, 2125),
])
@pytest.mark.parametrize("operation", [recover_session, finalize_session])
def test_closed_session_missing_final_segment_rejected_before_mutation(
        session, count_key, count, expected, operation):
    """A contiguous prefix is not the complete audio promised by stop.json."""
    with SegmentWriter(session, 8000, segment_seconds=1) as writer:
        writer.write(b"\x01\x02" * count)
    (session / "stop.json").write_text(
        json.dumps({"cause": "user_stop", "certainty": "confirmed", count_key: expected}),
        encoding="utf-8",
    )
    writer.segment_paths[-1].unlink()
    originals = _hashes(session)
    stop_before = (session / "stop.json").read_bytes()
    with pytest.raises(ValueError, match="stop.json"):
        operation(session)
    assert not list(session.glob("master_*"))
    assert not (session / "recovery.json").exists()
    assert _hashes(session) == originals
    assert (session / "stop.json").read_bytes() == stop_before


@pytest.mark.parametrize("operation", [recover_session, finalize_session])
def test_recovered_session_missing_tail_cannot_replace_recorded_total(session, operation):
    with SegmentWriter(session, 8000, segment_seconds=1) as writer:
        writer.write(b"\x01\x02" * 17001)
    recovered = recover_session(session)
    assert recovered["total_samples"] == 17001
    before = (session / "recovery.json").read_bytes()
    writer.segment_paths[-1].unlink()
    originals = _hashes(session)
    with pytest.raises(ValueError, match="recovery.json"):
        operation(session)
    assert not list(session.glob("master_*"))
    assert (session / "recovery.json").read_bytes() == before
    assert _hashes(session) == originals


def test_recovery_heartbeat_is_a_minimum_not_an_exact_total(session):
    """STOP-002/006: data after the last heartbeat is valid and must survive."""
    with SegmentWriter(session, 8000, segment_seconds=1) as writer:
        writer.write(b"\x01\x02" * 17001)
    (session / "heartbeat.json").write_text('{"total_samples":17000}', encoding="utf-8")
    writer.segment_paths[-1].unlink()
    originals = _hashes(session)
    with pytest.raises(ValueError, match="heartbeat.json"):
        recover_session(session)
    assert _hashes(session) == originals
    assert not (session / "recovery.json").exists()
    (session / "heartbeat.json").write_text('{"total_samples":15000}', encoding="utf-8")
    assert recover_session(session)["total_samples"] == 16000


@pytest.mark.parametrize("partial", [False, True])
def test_finalize_verification_preserves_input_capture_health(session, monkeypatch, partial):
    with SegmentWriter(session, 8000) as writer:
        writer.write(b"\x01\x02" * 8011)
    (session / "stop.json").write_text(
        json.dumps({"cause": "user_stop", "certainty": "confirmed", "total_samples": 8011,
                    "partial": partial, "dropped_samples": 512 if partial else 0}), encoding="utf-8",
    )
    monkeypatch.setattr(finalizer.shutil, "which", lambda _: None)
    report = finalize_session(session)
    assert report["verified"]
    assert report["verification_scope"] == "output_format_and_duration"
    assert report["input_partial"] is partial
    assert report["input_cause"] == "user_stop"
    assert report["input_certainty"] == "confirmed"


def test_legacy_millisecond_total_compares_floor(session, monkeypatch):
    with SegmentWriter(session, 8000) as writer:
        writer.write(b"\x01\x02" * 8011)
    (session / "stop.json").write_text('{"cause":"user_stop","total_ms":1001}', encoding="utf-8")
    monkeypatch.setattr(finalizer.shutil, "which", lambda _: None)
    assert finalize_session(session)["total_samples"] == 8011
