"""A-1 source tests: REC-AB-001 setup, -47 dBFS and simulated interruption."""

import math
import time
from types import SimpleNamespace

import numpy as np
import pytest

from lecturehub.recorder import devices
from lecturehub.recorder.capture import FakeSource, SourceError, SourceInterrupted, WasapiSource


def collect(source):
    chunks = []
    source.start(lambda data, count, status: chunks.append((data, count, status)))
    deadline = time.monotonic() + 3
    while source.active and time.monotonic() < deadline:
        time.sleep(0.001)
    assert not source.active, "Synthetic source failed to finish"
    source.stop()
    return chunks


def test_fake_minus_47_dbfs_and_interleaved_channels():
    """A-1/E5: -47 dB sine RMS is within 0.5 dB, including int16 quantization."""
    source = FakeSource(sample_rate=48000, channels=2, duration_seconds=1, rms_dbfs=-47)
    chunks = collect(source)
    assert source.error is None
    assert sum(count for _, count, _ in chunks) == 48000
    assert all(len(data) == count * 2 * 2 and status == 0 for data, count, status in chunks)
    samples = np.frombuffer(b"".join(data for data, _, _ in chunks), dtype="<i2").reshape(-1, 2)
    np.testing.assert_array_equal(samples[:, 0], samples[:, 1])
    rms = np.sqrt(np.mean(np.square(samples[:, 0].astype(np.float64)))) / 32768
    assert 20 * math.log10(rms) == pytest.approx(-47, abs=0.5)


def test_fake_silence_and_partial_final_buffer():
    source = FakeSource(sample_rate=8000, channels=3, duration_seconds=0.137, silence=True)
    chunks = collect(source)
    assert [count for _, count, _ in chunks] == [1024, 72]
    assert not any(b"".join(data for data, _, _ in chunks))
    assert source.error is None


def test_fake_interruption_has_exact_frame_boundary_and_error():
    """STOP-003 fixture: interruption is observable, not silently normal EOF."""
    source = FakeSource(sample_rate=8000, duration_seconds=1, interrupt_after_seconds=0.2)
    chunks = collect(source)
    assert sum(count for _, count, _ in chunks) == 1600
    assert isinstance(source.error, SourceInterrupted)


def test_fake_stop_interrupts_realtime_wait_and_can_restart():
    """STOP-001: stop does not wait for a long native-size synthetic block."""
    source = FakeSource(duration_seconds=None, realtime=True, frames_per_buffer=480000)
    callbacks = []
    source.start(lambda *args: callbacks.append(args))
    with pytest.raises(SourceError, match="already started"):
        source.start(lambda *args: None)
    started = time.monotonic()
    source.stop()
    source.stop()
    assert time.monotonic() - started < 1
    assert not source.active
    assert source.error is None
    assert callbacks == []
    source.duration_seconds = 0
    assert collect(source) == []


def test_fake_callback_error_is_observable():
    source = FakeSource()

    def broken_callback(*args):
        raise RuntimeError("queue consumer failed")

    source.start(broken_callback)
    deadline = time.monotonic() + 3
    while source.active and time.monotonic() < deadline:
        time.sleep(0.001)
    assert not source.active
    assert str(source.error) == "queue consumer failed"
    source.stop()


@pytest.fixture
def audio_backend(monkeypatch):
    def info(index, name, host=2, inputs=2, loopback=False, rate=44100):
        return {
            "index": index,
            "name": name,
            "hostApi": host,
            "maxInputChannels": inputs,
            "isLoopbackDevice": loopback,
            "defaultSampleRate": rate,
        }

    records = [
        info(0, "MME microphone", host=0),
        info(1, "마이크 (내장)", rate=48000),
        info(2, "Default speakers", inputs=0),
        info(3, "Default speakers [Loopback]", loopback=True),
        info(4, "USB microphone", inputs=1),
        info(5, "Other speakers [Loopback]", loopback=True),
    ]
    state = SimpleNamespace(
        managers=[], records=records, default_input=1, default_loopback=3, open_error=None, start_error=None
    )

    class Stream:
        def __init__(self):
            self.running = False
            self.closed = False

        def start_stream(self):
            if state.start_error:
                raise state.start_error
            self.running = True

        def is_active(self):
            return self.running

        def stop_stream(self):
            self.running = False

        def close(self):
            self.closed = True

    class Audio:
        def __init__(self):
            state.managers.append(self)
            self.terminated = False
            self.kwargs = None
            self.stream = None

        def get_host_api_info_by_type(self, host):
            assert host == 13
            return {"index": 2, "defaultInputDevice": state.default_input, "defaultOutputDevice": 2}

        def get_default_wasapi_loopback(self):
            if state.default_loopback is None:
                raise LookupError("No default output")
            return records[state.default_loopback]

        def get_device_count(self):
            return len(records)

        def get_device_info_by_index(self, index):
            return records[index]

        def terminate(self):
            self.terminated = True

        def open(self, **kwargs):
            self.kwargs = kwargs
            if state.open_error:
                raise state.open_error
            self.stream = Stream()
            return self.stream

    backend = SimpleNamespace(PyAudio=Audio, paWASAPI=13, paInt16=8, paContinue=0, paAbort=2)
    monkeypatch.setattr(devices, "_load_pyaudio", lambda: backend)
    return state


def test_devices_only_wasapi_capture_and_defaults(audio_backend):
    listed = devices.list_devices()
    assert [device.index for device in listed] == [1, 3, 4, 5]
    assert [device.index for device in listed if device.is_default] == [1, 3]
    assert devices.select_device("mic").index == 1
    assert devices.select_device("loopback").index == 3
    assert devices.select_device("mic", 4).channels == 1
    assert [device.index for device in devices.list_devices("loopback")] == [3, 5]
    assert all(manager.terminated for manager in audio_backend.managers)


def test_wrong_device_kind_rejected_and_missing_default_not_guessed(audio_backend):
    with pytest.raises(devices.DeviceError, match="쓸 수 있는 마이크 입력이 아닙니다"):
        devices.select_device("mic", 3)
    audio_backend.default_input = -1
    audio_backend.default_loopback = None
    for mode in ("mic", "loopback"):
        with pytest.raises(devices.DeviceError, match="기본 .+ 장치가 없습니다"):
            devices.select_device(mode)
    assert devices.select_device("loopback", 5).index == 5


def test_wasapi_shared_mode_native_rate_and_callback(audio_backend):
    """REC-AB-001 prerequisite: no exclusive stream info; use native format."""
    source = WasapiSource("mic")
    received = []
    source.start(lambda *args: received.append(args))
    manager = audio_backend.managers[-1]
    assert source.active
    assert manager.kwargs.keys() == {
        "format",
        "channels",
        "rate",
        "input",
        "input_device_index",
        "frames_per_buffer",
        "stream_callback",
        "start",
    }
    assert manager.kwargs["rate"] == 48000
    assert manager.kwargs["channels"] == 2
    assert manager.kwargs["format"] == 8
    assert manager.kwargs["input_device_index"] == 1
    assert manager.kwargs["input"] is True
    callback = manager.kwargs["stream_callback"]
    payload = b"\x01\x00\x02\x00"
    assert callback(payload, 1, {}, 7) == (None, 0)
    assert received == [(payload, 1, 7)]
    source.stop()
    source.stop()
    assert not source.active
    assert manager.stream.closed and manager.terminated


@pytest.mark.parametrize("failure", ["open_error", "start_error"])
def test_wasapi_start_failure_releases_resources(audio_backend, failure):
    setattr(audio_backend, failure, OSError("device busy"))
    source = WasapiSource("loopback")
    with pytest.raises(SourceError, match="device busy"):
        source.start(lambda *args: None)
    manager = audio_backend.managers[-1]
    assert manager.terminated
    assert manager.stream is None or manager.stream.closed
    assert not source.active
    assert isinstance(source.error, OSError)


def test_wasapi_device_change_fails_before_open(audio_backend):
    source = WasapiSource("mic")
    audio_backend.records[1]["name"] = "A different microphone"
    with pytest.raises(SourceError, match="devices changed"):
        source.start(lambda *args: None)
    assert audio_backend.managers[-1].kwargs is None
    assert audio_backend.managers[-1].terminated


def test_wasapi_callback_failure_aborts_stream(audio_backend):
    source = WasapiSource("mic")

    def broken_callback(*args):
        raise RuntimeError("queue failed")

    source.start(broken_callback)
    callback = audio_backend.managers[-1].kwargs["stream_callback"]
    assert callback(b"\0\0\0\0", 1, {}, 0) == (None, 2)
    assert not source.active
    assert str(source.error) == "queue failed"
    source.stop()
