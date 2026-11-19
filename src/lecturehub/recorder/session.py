"""Bounded callback queue; one worker owns PCM conversion, WAVs and heartbeat."""
from __future__ import annotations

import errno
import os
import queue
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from .capture import AudioSource
from .heartbeat import SessionLock, now_ms, write_json
from .levels import MixMonitor, QuietTracker, measure_dbfs, mono_pcm
from .paths import ensure_writable
from .segments import SegmentWriter


class RecordingSession:
    def __init__(self, source: AudioSource, out: Path, *, mode: str = "mic", course: str = "",
                 course_source: str = "unknown",
                 queue_blocks: int = 2048, segment_seconds: float = 60,
                 heartbeat_seconds: float = 5, quiet_dbfs: float = -50):
        if mode not in ("mic", "loopback") or queue_blocks < 1 or heartbeat_seconds <= 0:
            raise ValueError("잘못된 녹음 설정입니다.")
        self.source, self.mode, self.course = source, mode, course
        # user_pick | timetable_auto | unknown. Only user_pick counts as the user's choice (D-052).
        self.course_source = course_source
        self.out = ensure_writable(out)
        self.queue = queue.Queue(maxsize=queue_blocks)
        self.segment_seconds, self.heartbeat_seconds = segment_seconds, heartbeat_seconds
        self.quiet = QuietTracker(quiet_dbfs)
        self.mix = MixMonitor()
        self.session_dir = None
        self.writer = None
        self.error = None
        self.peak = self.rms = float("-inf")
        self.quiet_warning = False
        self.dropped_samples = self.status_count = self.queue_high_water = 0
        self._done = threading.Event()
        self._accepting = False
        self._thread = None
        self._lock = None
        self._stopped = False
        # Two clocks. ``_last_pcm_at`` is the writer's: when a block was last written to disk; only the level
        # meter reads it. ``_arrival`` is the device's: (monotonic, wall-clock ms) of the last block the device
        # delivered, stamped in the audio callback. Stall detection reads the second one, so a slow disk is
        # never taken for a dead microphone.
        self._last_pcm_at = time.monotonic()
        self._arrival = (time.monotonic(), now_ms())
        self._heard_samples = 0  # taken from the device into the queue so far (counted in the callback)
        # Pause (user request 2026-09-29): the device stays open, incoming audio is simply not written.
        # Each pause is logged with wall-clock times and the audio position where it happened.
        self._paused = False
        self.pauses: list[dict] = []
        # Stall recovery (release review 2026-11-16): each stretch in which the device delivered nothing, where
        # in the audio it is and how long it lasted (last sound until sound again, or until the recording ended:
        # ``open``). The audio simply continues after a gap, as with a pause.
        self.gaps: list[dict] = []

    @property
    def paused(self) -> bool:
        return self._paused

    def set_paused(self, paused: bool) -> bool:
        """Pause or resume writing; returns True when the state changed. pauses.json keeps the log."""
        if paused == self._paused or self._stopped or self.writer is None:
            return False
        if paused:
            audio_ms = self.writer.total_samples * 1000 // self.source.sample_rate
            self.pauses.append({"paused_at": now_ms(), "resumed_at": None, "audio_ms": audio_ms})
        else:
            self.pauses[-1]["resumed_at"] = now_ms()
            self._last_pcm_at = time.monotonic()  # the meter does not show the pause as "no sound for N s"
        self._paused = paused
        write_json(self.session_dir / "pauses.json", {"pauses": self.pauses})
        return True

    def idle_seconds(self) -> float:
        """How long the device has delivered nothing. Also counted while paused: a healthy device keeps
        delivering during a pause (the blocks are just not written), a dead one must still be noticed."""
        return max(0.0, time.monotonic() - self._arrival[0])

    def last_sound_ms(self) -> int:
        """Wall clock (ms) of the last block the device delivered; the start of the recording before the first."""
        return self._arrival[1]

    def arrived_since(self, moment: float) -> bool:
        """Whether the device delivered a block after ``moment`` (a ``time.monotonic()`` value taken while
        recording). The clock ticks coarsely on Windows, so a block in the same tick as ``moment`` is not
        counted; the next one is."""
        return self._arrival[0] > moment

    def heard_ms(self) -> int:
        """Audio position (ms) just after everything taken from the device so far, whether the writer has
        stored it yet or not. Blocks that were paused away or dropped are not part of the audio."""
        return self._heard_samples * 1000 // self.source.sample_rate

    def swap_source(self, new: AudioSource) -> None:
        """Carry on with a newly opened device after the old one died or stalled (sleep/resume, unplugged and
        plugged back). The sample rate must be the same: the segments of one session share it. Raises when the new
        device cannot start; the session then still holds the old (stopped) source and may try again.
        No clock is touched here: an opened device is not sound, only a delivered block is."""
        if new.sample_rate != self.source.sample_rate:
            raise ValueError("다시 연 장치의 샘플레이트가 달라 같은 녹음에 이어 쓸 수 없습니다.")
        old = self.source
        stopper = threading.Thread(target=_stop_quietly, args=(old,), name="recorder-old-source", daemon=True)
        stopper.start()
        stopper.join(3)  # a dead driver may hang in stop(); the recording does not wait for it
        self.source = new
        try:
            new.start(self._callback)
        except BaseException:
            self.source = old
            raise

    def note_gap(self, lost_ms: int, reason: str, still_open: bool = False, audio_ms: int | None = None) -> dict:
        """Remember an interruption (gaps.json, also in stop.json): ``at`` is when it ended, ``audio_ms`` where
        in the audio it is (given by the caller, else what is written so far), ``lost_ms`` how long it lasted.
        ``open`` is True when the sound had not come back by the time the recording ended (``still_open``).
        Any gap makes the stop report partial."""
        if audio_ms is None:
            audio_ms = self.writer.total_samples * 1000 // self.source.sample_rate if self.writer else 0
        gap = {"at": now_ms(), "audio_ms": int(audio_ms), "lost_ms": max(0, int(lost_ms)), "reason": reason,
               "open": bool(still_open)}
        self.gaps.append(gap)
        try:
            write_json(self.session_dir / "gaps.json", {"gaps": self.gaps})
        except (OSError, ValueError):
            pass  # the sidecar could not be written; the stop report carries the same list
        return gap

    def level_snapshot(self) -> tuple[float, float, bool]:
        """Do not leave a healthy-looking meter on screen when PCM stops arriving."""
        if self._paused:  # nothing is being written on purpose: no level, no "no sound" warning
            return float("-inf"), float("-inf"), False
        idle_seconds = time.monotonic() - self._last_pcm_at
        if idle_seconds >= 1:
            return float("-inf"), float("-inf"), self.quiet_warning or idle_seconds >= 30
        return self.peak, self.rms, self.quiet_warning

    def _callback(self, data: bytes, frames: int, status: int) -> None:
        # No disk IO, numpy, printing, waiting or blocking put in the audio callback.
        if frames > 0:  # the arrival clock: one attribute store per delivered block, paused or not
            self._arrival = (time.monotonic(), now_ms())
        if not self._accepting or self._paused:
            return
        if status:
            self.status_count += 1
        if frames <= 0 or len(data) != frames * self.source.channels * 2 or len(data) > 1024 * 1024:
            self.dropped_samples += max(0, frames)
            return
        try:
            self.queue.put_nowait(data)
            self._heard_samples += frames
            self.queue_high_water = max(self.queue_high_water, self.queue.qsize())
        except queue.Full:
            self.dropped_samples += frames

    def _heartbeat(self):
        self.writer.flush()
        write_json(self.session_dir / "heartbeat.json", {
            "at": now_ms(), "segment_number": self.writer.segment_number,
            "total_samples": self.writer.total_samples, "pid": os.getpid(),
            "dropped_samples": self.dropped_samples, "capture_status_count": self.status_count,
            "stereo_mix_loss_db": self.mix.loss_db,
        })

    def _write_loop(self):
        next_heartbeat = time.monotonic() + self.heartbeat_seconds
        try:
            while not self._done.is_set() or not self.queue.empty():
                try:
                    data = self.queue.get(timeout=min(0.1, self.heartbeat_seconds))
                except queue.Empty:
                    data = None
                if data is not None:
                    self.mix.update(data, self.source.channels)
                    mono = mono_pcm(data, self.source.channels)
                    self.writer.write(mono)
                    self._last_pcm_at = time.monotonic()
                    self.peak, self.rms = measure_dbfs(mono)
                    self.quiet_warning = self.quiet.update(
                        self.rms, len(mono) // 2, self.source.sample_rate)
                if time.monotonic() >= next_heartbeat:
                    self._heartbeat()
                    next_heartbeat = time.monotonic() + self.heartbeat_seconds
        except Exception as exc:  # noqa: BLE001 - publish worker failure to supervisor
            self.error = exc
            self._accepting = False
        finally:
            try:
                self.writer.close()
            except Exception as exc:  # noqa: BLE001 - retain close failure in stop report
                self.error = self.error or exc

    def start(self) -> Path:
        self.out.mkdir(parents=True, exist_ok=True)
        started = datetime.now(UTC)
        name = started.astimezone().strftime("%Y%m%d_%H%M%S") + "_" + self.mode
        self.session_dir = self.out / name
        # Never combine two sessions started in the same second.
        self.session_dir.mkdir(exist_ok=False)
        self._lock = SessionLock(self.session_dir)
        for attempt in range(40):  # D-050: a "is anyone recording?" probe (compact, transcribe) may hold the
            try:                   # brand-new lock for an instant; this folder is ours alone, so wait it out
                self._lock.__enter__()
                break
            except RuntimeError:
                if attempt == 39:
                    raise
                time.sleep(0.025)
        try:
            self.writer = SegmentWriter(self.session_dir, self.source.sample_rate, self.segment_seconds)
            write_json(self.session_dir / "session.json", {
                "started_at": int(started.timestamp() * 1000),
                "started_local": started.astimezone().isoformat(timespec="milliseconds"),
                "started_utc": started.isoformat(timespec="milliseconds"),
                "device_name": self.source.device_name, "mode": self.mode,
                "sample_rate": self.source.sample_rate, "input_channels": self.source.channels,
                "channels": 1, "wav_format": "pcm_s16le_mono", "course": self.course, "course_source": self.course_source,
                "quiet_threshold_dbfs": self.quiet.threshold_dbfs,
                "quiet_threshold_status": "proposal_D-009", "pid": os.getpid(),
            })
            self._heartbeat()
            self._thread = threading.Thread(target=self._write_loop, name="recorder-writer", daemon=True)
            self._thread.start()
            self._accepting = True
            self._last_pcm_at = time.monotonic()
            self.source.start(self._callback)
            # The arrival clock starts once the device is open: however long opening took is not silence.
            self._arrival = (time.monotonic(), now_ms())
        except BaseException:
            self.stop("unknown", "unknown")
            raise
        return self.session_dir

    def stop(self, cause: str = "user_stop", certainty: str = "confirmed", trigger: str | None = None) -> dict:
        if self._stopped:
            return self.stop_report
        if self._paused and self.pauses:  # stopped while paused: the pause ends with the recording
            self.pauses[-1]["resumed_at"] = now_ms()
            self.pauses[-1]["ended_by_stop"] = True
            self._paused = False
        stop_error = None
        try:
            self.source.stop()
        except Exception as exc:  # noqa: BLE001 - cleanup must still drain and release lock
            stop_error = exc
        self._accepting = False
        self._done.set()
        if self._thread and self._thread.ident is not None:
            self._thread.join()
        elif self.writer:
            try:
                self.writer.close()
            except Exception as exc:  # noqa: BLE001 - preserve failed-start cleanup error
                self.error = self.error or exc
        error = self.error or stop_error or self.source.error
        if error and cause == "source_lost" and not self.error:
            error = None  # the device went away and did not come back: a known ending, not an unknown failure
        if error:
            cause, certainty = "unknown", "unknown"
            if isinstance(self.error, OSError):
                cause = "disk_full" if error.errno == errno.ENOSPC else "write_error"
                certainty = "confirmed"
        total_samples = self.writer.total_samples if self.writer else 0
        no_pcm = total_samples == 0
        report = {
            "cause": cause, "certainty": certainty, "trigger": trigger, "at": now_ms(),
            "total_ms": total_samples * 1000 // self.source.sample_rate,
            "total_samples": total_samples, "dropped_samples": self.dropped_samples,
            "capture_status_count": self.status_count, "queue_high_water": self.queue_high_water,
            "stereo_mix_loss_db": self.mix.loss_db,
            "pauses": self.pauses,
            "gaps": self.gaps,
            "paused_ms": sum((p["resumed_at"] or p["paused_at"]) - p["paused_at"] for p in self.pauses),
            # source_lost: the recording ended because the sound never came back, so its tail is missing
            "partial": bool(error or no_pcm or cause in ("unknown", "source_lost") or self.dropped_samples
                            or self.status_count or self.gaps),
            "error": str(error) if error else ("캡처된 PCM 샘플이 없습니다." if no_pcm else None),
        }
        try:
            write_json(self.session_dir / "stop.json", report)
        finally:
            if self._lock:
                self._lock.__exit__()
            self._stopped = True
            self.stop_report = report
        return report


def _stop_quietly(source) -> None:
    try:
        source.stop()
    except Exception:  # noqa: BLE001, S110 - the device is already gone; nothing to save here
        pass
