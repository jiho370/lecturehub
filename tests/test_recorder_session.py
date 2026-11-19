"""A-1 recorder integration tests use only synthetic samples."""
import json
import math
import os
import signal
import subprocess
import sys
import time
import wave

import numpy as np
import pytest
from typer.testing import CliRunner

from lecturehub.recorder.capture import FakeSource
from lecturehub.recorder.heartbeat import SessionLock
from lecturehub.recorder.levels import QuietTracker, measure_dbfs, mono_pcm
from lecturehub.recorder.session import RecordingSession


def wait_source(source, timeout=5):
    end = time.monotonic() + timeout
    while source.active and time.monotonic() < end:
        time.sleep(0.01)
    assert not source.active


def test_STOP_001_normal_user_stop_and_metadata(tmp_path):
    """STOP-001: explicit stop is confirmed and all accepted samples drain."""
    source = FakeSource(sample_rate=8000, duration_seconds=0.3, realtime=True)
    session = RecordingSession(source, tmp_path / "한글 공백 (세션)", course="구조역학")
    folder = session.start()
    wait_source(source)
    result = session.stop()
    assert result["cause"] == "user_stop"
    assert result["certainty"] == "confirmed"
    assert result["total_samples"] == 2400
    assert result["total_ms"] == 300
    assert not result["partial"]
    assert json.loads((folder / "stop.json").read_text(encoding="utf-8")) == result
    metadata = json.loads((folder / "session.json").read_text(encoding="utf-8"))
    assert metadata["course"] == "구조역학"
    assert metadata["started_local"] and metadata["started_utc"]
    assert isinstance(metadata["started_at"], int)
    with wave.open(str(next(folder.glob("segment_*.wav"))), "rb") as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getnframes()) == (1, 2, 2400)


def test_STOP_001_cli_ctrl_c_flushes(tmp_path, monkeypatch):
    """STOP-001: the CLI SIGINT handler maps Ctrl+C to user_stop."""
    from lecturehub import cli
    from lecturehub.recorder import capture
    cli._register_all()

    class InterruptSource(FakeSource):
        @property
        def active(self):
            time.sleep(0.05)  # Capture at least one real synthetic block before Ctrl+C.
            signal.raise_signal(signal.SIGINT)
            return True

    monkeypatch.setattr(capture, "WasapiSource", lambda *args, **kwargs: InterruptSource(duration_seconds=None,
                                                                             realtime=True))
    result = CliRunner().invoke(cli.app, ["recorder", "record", "--out", str(tmp_path)])
    assert result.exit_code == 0, result.output
    stop = json.loads(next(tmp_path.rglob("stop.json")).read_text(encoding="utf-8"))
    assert stop["cause"] == "user_stop" and stop["certainty"] == "confirmed"


def test_STOP_002_subprocess_kill_and_recover(tmp_path):
    """STOP-002/STOP-006: kill real writer process, repair without claiming certainty."""
    from lecturehub.recorder.recovery import recover_session
    code = '''
import sys, time
from pathlib import Path
from lecturehub.recorder.capture import FakeSource
from lecturehub.recorder.session import RecordingSession
s=RecordingSession(FakeSource(sample_rate=8000,duration_seconds=None,realtime=True),
                   Path(sys.argv[1]),heartbeat_seconds=0.1,segment_seconds=0.25)
p=s.start()
print(p,flush=True)
while True: time.sleep(0.1)
'''
    process = subprocess.Popen([sys.executable, "-c", code, str(tmp_path)], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, encoding="utf-8",
                               env={**os.environ, "PYTHONIOENCODING": "utf-8"})

    def kill_tree():
        # Windows venv python.exe may redirect to a child interpreter. Killing
        # only the wrapper is not a recording-process termination.
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           capture_output=True, check=True)
        else:
            process.kill()
        process.wait(timeout=5)
    try:
        folder = None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            beats = list(tmp_path.rglob("heartbeat.json"))
            if beats:
                try:
                    beat = json.loads(beats[0].read_text(encoding="utf-8"))
                    if beat["total_samples"] >= 5000:
                        folder = beats[0].parent
                        break
                except (OSError, ValueError):
                    pass
            if process.poll() is not None:
                pytest.fail(process.communicate()[1])
            time.sleep(0.02)
        assert folder is not None
        with pytest.raises(RuntimeError):
            recover_session(folder)
        kill_tree()
    finally:
        if process.poll() is None:
            kill_tree()
    assert not (folder / "stop.json").exists()
    before = sum((path.stat().st_size - 44) // 2 for path in folder.glob("segment_*.wav"))
    report = recover_session(folder)
    assert report["total_samples"] == before
    assert report["cause"] in ("process_killed", "unknown")
    assert report["certainty"] in ("estimated", "unknown")
    after = 0
    for path in folder.glob("segment_*.wav"):
        with wave.open(str(path), "rb") as wav:
            after += wav.getnframes()
    assert before == after


def test_levels_minus_47_rms_and_silence():
    samples = np.arange(48000)
    signal_pcm = np.rint(np.sqrt(2) * 32768 * 10 ** (-47 / 20) *
                        np.sin(2 * np.pi * 1000 * samples / 48000)).astype("<i2").tobytes()
    peak, rms = measure_dbfs(signal_pcm)
    assert abs(rms - (-47)) <= 0.5
    assert abs(peak - (-47 + 20 * math.log10(math.sqrt(2)))) <= 0.5
    assert measure_dbfs(b"\0" * 400) == (-math.inf, -math.inf)
    stereo = np.array([[32767, 32767], [-32768, -32768]], dtype="<i2")
    assert np.frombuffer(mono_pcm(stereo.tobytes(), 2), dtype="<i2").tolist() == [32767, -32768]


def test_quiet_warning_requires_30_continuous_seconds():
    tracker = QuietTracker()
    for _ in range(29):
        assert not tracker.update(-60, 48000, 48000)
    assert tracker.update(-60, 48000, 48000)
    assert not tracker.update(-40, 48000, 48000)
    assert not tracker.update(-60, 48000, 48000)


def test_queue_bounded_and_drop_count_explicit(tmp_path):
    source = FakeSource()
    session = RecordingSession(source, tmp_path, queue_blocks=2)
    session._accepting = True
    chunk = b"\0" * 2048
    for _ in range(1000):
        session._callback(chunk, 1024, 0)
    assert session.queue.qsize() == 2
    assert session.queue_high_water == 2
    assert session.dropped_samples == 998 * 1024


def test_heartbeat_updates_while_recording(tmp_path):
    source = FakeSource(sample_rate=8000, duration_seconds=None, realtime=True)
    session = RecordingSession(source, tmp_path, heartbeat_seconds=0.05)
    folder = session.start()
    try:
        heartbeat = {}
        deadline = time.monotonic() + 30  # an upper bound only: on a busy PC the first block can take over 0.3 s
        while time.monotonic() < deadline:
            try:
                heartbeat = json.loads((folder / "heartbeat.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):  # being replaced right now
                heartbeat = {}
            if heartbeat.get("total_samples", 0) > 0:
                break
            time.sleep(0.05)
        assert heartbeat.get("total_samples", 0) > 0
        with pytest.raises(RuntimeError), SessionLock(folder):
            pass
    finally:
        session.stop()
    with SessionLock(folder):
        pass


def test_source_interruption_is_not_user_stop(tmp_path):
    source = FakeSource(duration_seconds=1, interrupt_after_seconds=0.1, realtime=True)
    session = RecordingSession(source, tmp_path)
    session.start()
    wait_source(source)
    report = session.stop()
    assert report["cause"] == "unknown"
    assert report["certainty"] == "unknown"
    assert report["partial"] and report["error"]


def test_protected_semester_output_rejected(tmp_path, monkeypatch):
    from lecturehub.recorder import paths
    root = tmp_path / "대학" / "26-2"
    monkeypatch.setattr(paths, "SEMESTER_ROOT", root)
    with pytest.raises(ValueError):
        paths.ensure_writable(root / "녹음본" / "changed")
    assert paths.ensure_writable(root / "_lecturehub" / "recordings").is_relative_to(root)


def test_writer_failure_is_reported(tmp_path, monkeypatch):
    """A storage failure never becomes a successful user_stop."""
    import errno

    from lecturehub.recorder.segments import SegmentWriter

    def fail_write(self, data):
        raise OSError(errno.ENOSPC, "synthetic disk full")

    monkeypatch.setattr(SegmentWriter, "write", fail_write)
    source = FakeSource(duration_seconds=0.1, realtime=True)
    session = RecordingSession(source, tmp_path)
    session.start()
    wait_source(source)
    report = session.stop()
    assert report["cause"] == "disk_full"
    assert report["partial"]


def test_source_oserror_not_misreported_as_disk_failure(tmp_path):
    class BadSource(FakeSource):
        @property
        def error(self):
            return OSError("synthetic device failure")

    source = BadSource(duration_seconds=0.1, realtime=True)
    session = RecordingSession(source, tmp_path)
    session.start()
    wait_source(source)
    report = session.stop()
    assert report["cause"] == "unknown" and report["certainty"] == "unknown"


def test_failed_start_closes_writer_and_releases_lock(tmp_path, monkeypatch):
    from lecturehub.recorder import session as module
    original = module.write_json

    def fail_metadata(path, payload):
        if path.name == "session.json":
            raise OSError("synthetic startup failure")
        original(path, payload)

    monkeypatch.setattr(module, "write_json", fail_metadata)
    session = RecordingSession(FakeSource(), tmp_path)
    with pytest.raises(OSError, match="startup"):
        session.start()
    with pytest.raises(ValueError, match="closed"):
        session.writer.write(b"\0\0")
    with SessionLock(session.session_dir):
        pass


def test_atomic_heartbeat_retries_transient_windows_reader(tmp_path, monkeypatch):
    from lecturehub.recorder import heartbeat
    replace = heartbeat.os.replace
    calls = []

    def busy_once(source, dest):
        calls.append(1)
        if len(calls) == 1:
            raise PermissionError("synthetic Windows sharing violation")
        return replace(source, dest)

    monkeypatch.setattr(heartbeat.os, "replace", busy_once)
    path = tmp_path / "heartbeat.json"
    heartbeat.write_json(path, {"total_samples": 123})
    assert len(calls) == 2
    assert json.loads(path.read_text()) == {"total_samples": 123}


def test_no_pcm_is_partial_and_stale_meter_warns(tmp_path):
    class EmptySource:
        sample_rate = 48000
        channels = 1
        device_name = "synthetic stalled device"
        error = None
        active = True

        def start(self, callback):
            pass

        def stop(self):
            self.active = False

    session = RecordingSession(EmptySource(), tmp_path)
    session.start()
    session.peak, session.rms = -10, -20
    session._last_pcm_at = time.monotonic() - 31
    assert session.level_snapshot() == (-math.inf, -math.inf, True)
    report = session.stop("scheduled_stop", "confirmed")
    assert report["total_samples"] == 0
    assert report["partial"] and "PCM" in report["error"]


def test_pause_skips_audio_and_logs_each_pause(tmp_path):
    """User request 2026-09-29: pause keeps the device open, writes nothing, and logs where it happened."""
    source = FakeSource(sample_rate=8000, duration_seconds=1.0, realtime=True)
    session = RecordingSession(source, tmp_path / "일시정지(합성)")
    folder = session.start()
    time.sleep(0.2)
    assert session.set_paused(True) and not session.set_paused(True)  # idempotent
    assert session.level_snapshot() == (float("-inf"), float("-inf"), False)  # no "no sound" warning while paused
    time.sleep(0.4)
    assert session.set_paused(False)
    wait_source(source)
    result = session.stop()
    assert len(result["pauses"]) == 1 and result["pauses"][0]["resumed_at"] >= result["pauses"][0]["paused_at"]
    assert 300 <= result["paused_ms"] <= 900
    # What was kept is the 1.0 s minus what the pause skipped (wall time of the pause on a real-time source); a
    # slow runner stretches the pause, so compare with the measured pause instead of a fixed window.
    assert 0 < result["total_samples"] < 8000
    assert abs(result["total_samples"] - (8000 - result["paused_ms"] * 8)) <= 1600
    assert not result["partial"] and result["dropped_samples"] == 0  # a pause is not a capture loss
    log = json.loads((folder / "pauses.json").read_text(encoding="utf-8"))["pauses"]
    assert log[0]["audio_ms"] == result["pauses"][0]["audio_ms"]


def test_stop_while_paused_closes_the_pause(tmp_path):
    source = FakeSource(sample_rate=8000, duration_seconds=None, realtime=True)
    session = RecordingSession(source, tmp_path / "일시정지 중 종료(합성)")
    session.start()
    time.sleep(0.1)
    session.set_paused(True)
    time.sleep(0.1)
    result = session.stop()
    assert result["pauses"][0]["ended_by_stop"] and result["pauses"][0]["resumed_at"] is not None
    assert not session.set_paused(False)  # nothing to resume after stop


def test_D050_start_waits_out_a_probe_holding_the_new_lock_for_an_instant(tmp_path, monkeypatch):
    """A busy check (compact, transcribe) briefly takes another session's lock; a recording starting at that
    instant must still start (it retries for about a second) instead of failing."""
    real, held = SessionLock.__enter__, {"n": 0}

    def enter(self):
        if held["n"] < 3:
            held["n"] += 1
            raise RuntimeError("사용 중인 세션입니다.")
        return real(self)

    monkeypatch.setattr(SessionLock, "__enter__", enter)
    source = FakeSource(sample_rate=8000, duration_seconds=0.2, realtime=True)
    session = RecordingSession(source, tmp_path / "rec")
    folder = session.start()
    wait_source(source)
    result = session.stop()
    assert held["n"] == 3 and result["total_samples"] == 1600 and (folder / "stop.json").exists()
