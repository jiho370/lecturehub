"""Shared-mode WASAPI and deterministic synthetic PCM capture sources.

The callback contract is deliberately small: enqueue the supplied int16 PCM
bytes using a bounded, nonblocking queue. Downmixing, metering and file writes
belong to the consumer thread, never to the native audio callback.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from . import devices

AudioCallback = Callable[[bytes, int, int], None]


class SourceError(RuntimeError):
    """Capture could not start or ended with an error."""


class SourceInterrupted(SourceError):
    """Synthetic source interruption for recovery/error-path tests."""


@runtime_checkable
class AudioSource(Protocol):
    """Raw little-endian, interleaved signed 16-bit PCM producer."""

    sample_rate: int
    channels: int
    device_name: str

    @property
    def active(self) -> bool: ...

    @property
    def error(self) -> Exception | None: ...

    def start(self, callback: AudioCallback) -> None: ...

    def stop(self) -> None: ...


class WasapiSource:
    """WASAPI input at its native rate, using PortAudio's shared-mode default.

    No host-specific stream info or exclusive-mode flag is supplied. In
    PyAudioWPatch's PortAudio WASAPI backend, absent stream info selects
    AUDCLNT_SHAREMODE_SHARED. Native channel count is preserved for the worker.
    """

    recoverable = True  # a real device can come back (sleep/resume, replug): recorder/service.py Recovery

    def __init__(self, mode: str, device_id: int | None = None, *, device_name: str | None = None,
                 frames_per_buffer: int = 1024):
        if not isinstance(frames_per_buffer, int) or frames_per_buffer <= 0:
            raise ValueError("frames_per_buffer must be a positive integer")
        self.device = devices.select_device(mode, device_id, device_name)
        self.sample_rate = self.device.sample_rate
        self.channels = self.device.channels
        self.device_name = self.device.name
        self.mode = mode
        self.frames_per_buffer = frames_per_buffer
        self._audio: Any = None
        self._stream: Any = None
        self._error: Exception | None = None

    @property
    def error(self) -> Exception | None:
        return self._error

    @property
    def active(self) -> bool:
        if self._stream is None or self._error is not None:
            return False
        try:
            return bool(self._stream.is_active())
        except (OSError, ValueError) as exc:
            self._error = exc
            return False

    def start(self, callback: AudioCallback) -> None:
        if self._audio is not None:
            raise SourceError("Source has already started; stop it before restarting.")
        self._error = None
        backend = devices._load_pyaudio()
        self._audio = backend.PyAudio()

        def receive(data: bytes, frame_count: int, time_info: Any, status_flags: int) -> tuple[None, int]:
            try:
                callback(data, frame_count, status_flags)
            except Exception as exc:  # noqa: BLE001 - publish callback errors to the supervising thread
                # Do not let PortAudio print/raise on its callback thread.
                self._error = exc
                return None, backend.paAbort
            return None, backend.paContinue

        try:
            # Detect device re-indexing between discovery and start instead of
            # accidentally recording a different microphone or speaker.
            current = devices._choose_device(
                devices._enumerate_devices(self._audio, backend, self.mode), self.mode, self.device.index
            )
            if current.name != self.device_name or (current.sample_rate, current.channels) != (
                self.sample_rate,
                self.channels,
            ):
                raise SourceError("Audio devices changed; list devices and start recording again.")
            self._stream = self._audio.open(
                format=backend.paInt16,
                channels=self.channels,
                rate=self.sample_rate,
                input=True,
                input_device_index=self.device.index,
                frames_per_buffer=self.frames_per_buffer,
                stream_callback=receive,
                start=False,
            )
            self._stream.start_stream()
        except Exception as exc:
            self._error = exc
            self.stop()
            raise SourceError(f"Could not start shared-mode WASAPI capture: {exc}") from exc

    def stop(self) -> None:
        """Stop callbacks and release both stream and device manager, idempotently."""
        stream, audio = self._stream, self._audio
        self._stream = self._audio = None
        if stream is not None:
            try:
                stream.stop_stream()
            except (OSError, ValueError) as exc:
                self._error = self._error or exc
            finally:
                try:
                    stream.close()
                except (OSError, ValueError) as exc:
                    self._error = self._error or exc
        if audio is not None:
            try:
                audio.terminate()
            except (OSError, ValueError) as exc:
                self._error = self._error or exc


class FakeSource:
    """Generate synthetic sine/silence without opening any real audio device.

    ``rms_dbfs`` is the sine's RMS level relative to 32768, not its peak.
    Interruption ends exactly at ``floor(interrupt_after_seconds * rate)``;
    normal finite duration also ends at an exact frame boundary. ``None`` for
    duration runs until stopped. Every channel carries the same test signal.
    """

    def __init__(
        self,
        *,
        sample_rate: int = 48000,
        channels: int = 1,
        duration_seconds: float | None = 1.0,
        rms_dbfs: float = -20.0,
        silence: bool = False,
        frequency_hz: float = 440.0,
        interrupt_after_seconds: float | None = None,
        frames_per_buffer: int = 1024,
        realtime: bool = False,
    ):
        if sample_rate <= 0 or channels <= 0 or frames_per_buffer <= 0:
            raise ValueError("sample_rate, channels and frames_per_buffer must be positive")
        if any(not isinstance(value, int) for value in (sample_rate, channels, frames_per_buffer)):
            raise ValueError("sample_rate, channels and frames_per_buffer must be integers")
        for name, value in (
            ("duration_seconds", duration_seconds),
            ("interrupt_after_seconds", interrupt_after_seconds),
        ):
            if value is not None and (not math.isfinite(value) or value < 0):
                raise ValueError(f"{name} must be finite and nonnegative, or None")
        if not math.isfinite(rms_dbfs) or rms_dbfs > -3.010299956639812:
            raise ValueError("A non-clipping sine RMS level must be finite and at most -3.0103 dBFS")
        if not math.isfinite(frequency_hz) or not 0 < frequency_hz < sample_rate / 2:
            raise ValueError("frequency_hz must be positive and below the Nyquist frequency")
        self.sample_rate = sample_rate
        self.channels = channels
        self.device_name = "FakeSource (synthetic silence)" if silence else "FakeSource (synthetic sine)"
        self.duration_seconds = duration_seconds
        self.rms_dbfs = rms_dbfs
        self.silence = silence
        self.frequency_hz = frequency_hz
        self.interrupt_after_seconds = interrupt_after_seconds
        self.frames_per_buffer = frames_per_buffer
        self.realtime = realtime
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._error: Exception | None = None

    @property
    def active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def error(self) -> Exception | None:
        return self._error

    def start(self, callback: AudioCallback) -> None:
        if self._thread is not None and self._thread.is_alive():
            raise SourceError("Source has already started.")
        # Import before starting the thread so missing optional dependencies
        # fail synchronously, before a recording session appears to be running.
        import numpy as np

        self._stop.clear()
        self._error = None

        def generate() -> None:
            total = None if self.duration_seconds is None else int(self.duration_seconds * self.sample_rate)
            interrupted_at = (
                None
                if self.interrupt_after_seconds is None
                else int(self.interrupt_after_seconds * self.sample_rate)
            )
            offset = 0
            started = time.monotonic()
            amplitude = 32768 * math.sqrt(2) * 10 ** (self.rms_dbfs / 20)
            try:
                while not self._stop.is_set():
                    if total is not None and offset >= total:
                        break
                    if interrupted_at is not None and offset >= interrupted_at:
                        raise SourceInterrupted(f"Synthetic capture interrupted after {offset} frames")
                    count = self.frames_per_buffer
                    for boundary in (total, interrupted_at):
                        if boundary is not None:
                            count = min(count, boundary - offset)
                    if self.realtime:
                        wait = started + (offset + count) / self.sample_rate - time.monotonic()
                        if wait > 0 and self._stop.wait(wait):
                            break
                    if self.silence:
                        data = bytes(count * self.channels * 2)
                    else:
                        phase = np.arange(offset, offset + count, dtype=np.float64)
                        mono = np.rint(
                            amplitude * np.sin(phase * (2 * math.pi * self.frequency_hz / self.sample_rate))
                        )
                        mono = np.clip(mono, -32768, 32767).astype("<i2")
                        data = np.repeat(mono, self.channels).tobytes()
                    callback(data, count, 0)
                    offset += count
            except Exception as exc:  # noqa: BLE001 - preserve worker errors for the supervising thread
                self._error = exc

        self._thread = threading.Thread(target=generate, name="lecturehub-fake-audio", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                self._error = self._error or SourceError("Audio callback did not stop; it must never block.")
