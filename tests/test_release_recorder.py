"""Release review 2026-11-16: recordings that used to become worthless without a word.

- the device stops giving sound (sleep/resume, unplugged and plugged back): the engine opens it again and carries on;
- Windows keeps apps from the microphone: the sound check says so instead of "normal in a quiet room";
- the disk is nearly full: said before the start and while recording.
None of these ever refuses or stops a healthy recording (D-050).

Most stall tests run the real engine loop (``service.record``) on simulated time with a device that delivers a
block only when the test says so: nothing in them waits for a real second to pass or depends on how busy the PC is.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from lecturehub.recorder import preflight, service
from lecturehub.recorder import session as session_module
from lecturehub.recorder.capture import FakeSource
from lecturehub.recorder.session import RecordingSession

T0 = 1000.0                 # where the simulated monotonic clock starts
WALL0 = 1_800_000_000_000   # and the simulated wall clock (ms)
MIB = 2**20


class Device(FakeSource):
    """A synthetic source on real time that behaves like a real device: it may come back (``recoverable``)."""

    recoverable = True

    def __init__(self, **kwargs):
        super().__init__(sample_rate=16000, duration_seconds=None, realtime=True, **kwargs)
        self.device_name = "합성 마이크"


class Hand:
    """A synthetic device driven by hand: it delivers a block only when the test says so."""

    sample_rate, channels = 16000, 1

    def __init__(self, name="합성 마이크", *, recoverable=True, silent=False, on_open=None):
        self.device_name, self.recoverable, self.silent, self.on_open = name, recoverable, silent, on_open
        self.error = None
        self.active = False
        self.callback = None

    def start(self, callback):
        if self.on_open:
            self.on_open()
        self.callback, self.active = callback, True

    def stop(self):
        self.active = False

    def deliver(self, seconds=1.0) -> int:
        """Hand one block of this length to the recorder; 0 when the device is silent, dead or closed."""
        if self.silent or self.error or not self.active:
            return 0
        frames = int(self.sample_rate * seconds)
        self.callback(bytes(frames * 2), frames, 0)
        return frames


class Clock:
    """Simulated time for the engine and the session: it moves only when a test moves it."""

    def __init__(self):
        self.t, self.wall = T0, WALL0

    def monotonic(self) -> float:
        return self.t

    def now_ms(self) -> int:
        return self.wall

    def advance(self, seconds: float) -> None:
        self.t += seconds
        self.wall += round(seconds * 1000)


@pytest.fixture
def clock(monkeypatch):
    simulated = Clock()
    fake_time = SimpleNamespace(monotonic=simulated.monotonic, sleep=lambda seconds: time.sleep(0.001))
    for module in (service, session_module):
        monkeypatch.setattr(module, "time", fake_time)
        monkeypatch.setattr(module, "now_ms", simulated.now_ms)
    return simulated


def _wait(condition, seconds=5.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.02)
    return False


def _until(condition, what="조건"):
    """Wait for something another thread does (never a margin: the limit only keeps a broken test from hanging)."""
    assert _wait(condition, 30.0), f"기다리던 일이 일어나지 않음: {what}"


def _events():
    seen = []
    return seen, lambda kind, **payload: seen.append({"type": kind, **payload})


class Run:
    """What a scenario sees of one engine run, and what the test reads afterwards."""

    def __init__(self, clock):
        self.clock = clock
        self.seen: list[dict] = []
        self.attempts: list[float] = []  # simulated seconds since the start at which a device was asked for
        self.opened: list[Hand] = []
        self.session = self.recovery = self.report = self.code = None

    @property
    def source(self):
        return self.session.source

    @property
    def folder(self) -> Path:
        return self.session.session_dir

    def second(self, sound=True, seconds=1.0, stored=True) -> None:
        """Time passes; a device that is delivering hands over that much sound, and (unless the recording is
        paused or ``stored`` is False) the writer has stored it before the engine looks at the source."""
        self.clock.advance(seconds)
        if not sound:
            return
        before, expect = self.session.writer.total_samples, stored and not self.session.paused
        frames = self.source.deliver(seconds)
        if frames and expect:
            _until(lambda: self.session.writer.total_samples >= before + frames, "받은 소리가 파일에 쓰임")

    def control(self, name: str, present=True) -> None:
        path = self.folder / name
        if present:
            path.write_text("", encoding="utf-8")
        else:
            path.unlink()

    def kinds(self, *names) -> list[str]:
        return [e["type"] for e in self.seen if e["type"] in names]

    def one(self, kind: str) -> dict:
        found = [e for e in self.seen if e["type"] == kind]
        assert len(found) == 1, f"{kind}: {len(found)}번"
        return found[0]

    def statuses(self) -> list[dict]:
        return [e for e in self.seen if e["type"] == "status"]

    def between(self, first: str, second: str) -> list[dict]:
        """The status events after the one ``first`` event and before the one ``second`` event."""
        a, b = self.seen.index(self.one(first)), self.seen.index(self.one(second))
        return [e for e in self.seen[a:b] if e["type"] == "status"]

    def gaps_file(self):
        path = self.folder / "gaps.json"
        return json.loads(path.read_text(encoding="utf-8"))["gaps"] if path.exists() else None


def _engine(tmp_path, monkeypatch, clock, scenario, devices=None, argv=()) -> Run:
    """Run ``service.record`` with hand-driven devices on simulated time.

    ``scenario(run)`` is a generator. The code up to each ``yield`` runs at the top of one pass of the engine
    loop, just before the engine looks at the source. When it returns, the stop file is written (a user stop).
    ``devices(n)`` makes a device or raises; n counts the earlier requests (0 is the device the recording
    starts with).
    """
    run = Run(clock)
    devices = devices or (lambda n: Hand())

    def make(args):
        run.attempts.append(clock.t - T0)
        device = devices(len(run.attempts) - 1)
        run.opened.append(device)
        return device

    class Scripted(service.Recovery):
        def __init__(self, session, args, emit):
            super().__init__(session, args, emit)
            run.session, run.recovery = session, self
            self.script, self.passes = scenario(run), 0

        def reason(self):
            self.passes += 1
            if self.script is not None:
                try:
                    if self.passes > 600:  # a scenario that never ends would hang the test run
                        raise StopIteration
                    next(self.script)
                except StopIteration:
                    self.script = None
                    run.control(service.STOP_FILE)
            return super().reason()

    monkeypatch.setattr(service, "_make_source", make)
    monkeypatch.setattr(service, "Recovery", Scripted)
    args = service.parse_args(["--out", str(tmp_path / "recordings"), "--no-finalize", *argv])
    run.code = service.record(args, lambda kind, **payload: run.seen.append({"type": kind, **payload}))
    run.report = next((e["report"] for e in run.seen if e["type"] in ("stopped", "discarded")), None)
    return run


def _stop_json(run: Run) -> dict:
    return json.loads((run.folder / "stop.json").read_text(encoding="utf-8"))


# ---------- the session: one recording across devices, and the arrival clock
def test_session_continues_in_the_same_recording_after_the_device_is_opened_again(tmp_path):
    first = Device(interrupt_after_seconds=0.3)
    session = RecordingSession(first, tmp_path / "recordings", course="합성 과목", course_source="user_pick")
    folder = session.start()
    assert _wait(lambda: first.error is not None)
    before = session.writer.total_samples
    session.swap_source(Device())
    assert _wait(lambda: session.writer.total_samples > before + 1600)
    gap = session.note_gap(1234, "source_lost")
    report = session.stop()
    assert report["cause"] == "user_stop" and report["error"] is None
    assert report["gaps"] == [gap] and gap["lost_ms"] == 1234 and gap["audio_ms"] >= before * 1000 // 16000
    assert gap["open"] is False
    assert report["partial"] is True  # a recording with a hole says so
    assert json.loads((folder / "gaps.json").read_text(encoding="utf-8"))["gaps"] == [gap]


def test_a_device_with_another_sample_rate_is_not_mixed_into_the_recording(tmp_path):
    session = RecordingSession(Device(), tmp_path / "recordings")
    session.start()
    other = FakeSource(sample_rate=48000, duration_seconds=None, realtime=True)
    with pytest.raises(ValueError, match="샘플레이트"):
        session.swap_source(other)
    assert session.source is not other
    session.stop()


def test_idle_time_is_measured_where_the_device_delivers(tmp_path, clock):
    """Contract 1: the clock starts when the device is open, every delivered block resets it (paused or not),
    and nothing else does: not a pause, not a resume, not opening another device."""
    device = Hand(on_open=lambda: clock.advance(7))  # a device that takes 7 s to open
    session = RecordingSession(device, tmp_path / "recordings")
    session.start()
    assert session.idle_seconds() == 0.0  # the time it took to open is not silence
    assert session.last_sound_ms() == clock.wall
    clock.advance(3)
    assert session.idle_seconds() == 3.0
    device.deliver(0.1)
    heard = clock.wall
    assert session.idle_seconds() == 0.0 and session.last_sound_ms() == heard

    session.set_paused(True)
    clock.advance(4)
    assert session.idle_seconds() == 4.0  # a pause does not hide a device that delivers nothing
    mark = clock.t
    assert session.arrived_since(mark) is False
    clock.advance(1)
    device.deliver(0.1)  # a healthy device keeps delivering during a pause; the block is just not written
    heard = clock.wall
    assert session.idle_seconds() == 0.0 and session.arrived_since(mark) is True
    clock.advance(2)
    session.set_paused(False)
    assert session.idle_seconds() == 2.0  # resuming is not sound

    device.callback(b"", 0, 0)  # a callback without frames is not a delivered block
    assert session.idle_seconds() == 2.0
    clock.advance(4)
    session.swap_source(Hand())
    assert session.idle_seconds() == 6.0  # an opened device is not sound either
    assert session.last_sound_ms() == heard
    assert session.stop()["total_ms"] == 100  # only the block that arrived outside the pause was written


# ---------- the engine loop on simulated time: what is and is not a stall
def test_a_stuck_disk_writer_is_not_a_dead_microphone(tmp_path, monkeypatch, clock):
    """The device delivers every second while the disk writer is stuck for 12 s: nothing is reopened."""
    gate = threading.Event()
    real_write = session_module.SegmentWriter.write

    def stuck(self, data):
        gate.wait(60)
        return real_write(self, data)

    monkeypatch.setattr(session_module.SegmentWriter, "write", stuck)

    def scenario(run):
        for _ in range(12):
            run.second(stored=False)
            yield
        gate.set()
        _until(lambda: run.session.writer.total_samples == 12 * 16000, "밀린 소리가 모두 쓰임")
        run.second()
        yield

    try:
        run = _engine(tmp_path, monkeypatch, clock, scenario)
    finally:
        gate.set()
    assert run.kinds("stalled", "recovered") == [] and len(run.opened) == 1
    assert run.report["cause"] == "user_stop" and run.report["total_ms"] == 13_000
    assert run.report["partial"] is False and run.report["gaps"] == [] and run.report["dropped_samples"] == 0


def test_a_slow_device_open_is_not_counted_as_silence(tmp_path, monkeypatch, clock):
    def scenario(run):
        yield  # the engine looks at the source before the first block has arrived
        for _ in range(3):
            run.second()
            yield

    run = _engine(tmp_path, monkeypatch, clock, scenario, devices=lambda n: Hand(on_open=lambda: clock.advance(8)))
    assert run.kinds("stalled", "recovered") == [] and len(run.opened) == 1
    assert run.report["partial"] is False and run.report["total_ms"] == 3000


def test_a_synthetic_source_that_is_only_silent_is_not_ended(tmp_path, monkeypatch, clock):
    """Only a real device is reopened; a synthetic one (the app's test engine) that is silent for a while has
    not ended, and the recording goes on as it did before stall recovery existed."""
    def scenario(run):
        run.second()
        yield
        for _ in range(10):
            run.second(sound=False)
            yield
        run.second()
        yield

    run = _engine(tmp_path, monkeypatch, clock, scenario, devices=lambda n: Hand(recoverable=False))
    assert run.kinds("stalled", "recovered") == [] and len(run.opened) == 1
    assert run.report["cause"] == "user_stop" and run.report["total_ms"] == 2000


def test_a_synthetic_source_that_ends_still_just_ends(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "_make_source", lambda args: FakeSource(sample_rate=16000, duration_seconds=0.3))
    seen, emit = _events()
    args = service.parse_args(["--out", str(tmp_path / "recordings"), "--no-finalize"])
    assert service.record(args, emit) == 0
    assert "stalled" not in [e["type"] for e in seen]


# ---------- one stall, one recovery
def test_a_lost_device_is_reopened_and_the_recording_goes_on(tmp_path, monkeypatch, clock):
    """Unplugged and back: one stalled, one recovered, the same session, the gap from the last sound on."""
    def scenario(run):
        for _ in range(3):
            run.second()
            yield
        run.clock.advance(1)
        run.source.error = RuntimeError("합성: 장치가 빠짐")
        yield  # t=4: noticed; the reopen fails, the device is not back yet
        run.clock.advance(3)
        yield  # t=7: the next attempt opens it; no sound has arrived yet
        run.second()
        yield  # t=8: the first block of the new device
        run.second()
        yield

    def devices(n):
        if n == 1:
            raise RuntimeError("합성: 장치 없음")
        return Hand("합성 마이크 %d" % n)

    run = _engine(tmp_path, monkeypatch, clock, scenario, devices)
    assert run.kinds("stalled", "recovered") == ["stalled", "recovered"]
    assert run.one("stalled")["reason"] == "source_lost" and run.attempts == [0.0, 4.0, 7.0]
    waiting = run.between("stalled", "recovered")
    assert waiting and all(s["stalled"] is True and s["recoveries"] == 0 for s in waiting)
    recovered = run.one("recovered")
    assert recovered["device"] == "합성 마이크 2" and recovered["recoveries"] == 1
    gap = {"at": WALL0 + 8000, "audio_ms": 3000, "lost_ms": 5000, "reason": "source_lost", "open": False}
    assert {k: recovered[k] for k in gap} == gap  # last sound at t=3, sound again at t=8
    assert run.report["gaps"] == [gap] and run.gaps_file() == [gap]
    assert run.report["cause"] == "user_stop" and run.report["partial"] is True and run.report["total_ms"] == 5000
    last = run.statuses()[-1]
    assert last["stalled"] is False and last["recoveries"] == 1


def test_an_opened_but_silent_microphone_is_still_a_stall(tmp_path, monkeypatch, clock):
    """A device that opens and delivers nothing is not a recovery: the stall stays open, the device is opened
    again after STALL_SECONDS, and the gap runs from the last sound to the first sound of the device that works."""
    def scenario(run):
        for _ in range(3):
            run.second()
            yield
        run.source.silent = True  # the stream stays open and stops delivering
        for _ in range(10):       # t=4..13: noticed at t=8 (5 s without a block), devices 1 and 2 opened
            run.second()
            yield
        run.second()              # t=14: device 2 delivers
        yield
        for _ in range(2):
            run.second()
            yield

    run = _engine(tmp_path, monkeypatch, clock, scenario, devices=lambda n: Hand("합성 마이크 %d" % n, silent=(n == 1)))
    assert run.kinds("stalled", "recovered") == ["stalled", "recovered"]
    assert run.one("stalled")["reason"] == "no_data"
    assert run.attempts == [0.0, 8.0, 13.0]  # opened at once, then again only after 5 s without a block
    waiting = run.between("stalled", "recovered")
    assert len(waiting) >= 5 and all(s["stalled"] is True and s["recoveries"] == 0 for s in waiting)
    gap = {"at": WALL0 + 14_000, "audio_ms": 3000, "lost_ms": 11_000, "reason": "no_data", "open": False}
    assert run.report["gaps"] == [gap] and run.gaps_file() == [gap]
    assert run.one("recovered")["device"] == "합성 마이크 2"
    assert run.report["partial"] is True and run.report["total_ms"] == 6000
    last = run.statuses()[-1]
    assert last["stalled"] is False and last["recoveries"] == 1


def test_pc_sound_is_recovered_as_soon_as_it_is_open_again(tmp_path, monkeypatch, clock):
    """PC sound is silent by design while nothing plays: silence is never a stall there, and after a lost
    device a successful reopen ends the stall at once, without waiting for sound."""
    def scenario(run):
        for _ in range(3):
            run.second()
            yield
        for _ in range(6):  # a minute in which nothing plays
            run.second(sound=False, seconds=10)
            yield
        run.clock.advance(1)
        run.source.active = False  # t=64: the output device went away
        yield
        for _ in range(2):  # still nothing playing
            run.second(sound=False, seconds=10)
            yield

    run = _engine(tmp_path, monkeypatch, clock, scenario, argv=["--mode", "loopback"])
    assert run.kinds("stalled", "recovered") == ["stalled", "recovered"]
    assert run.between("stalled", "recovered") == [] and run.attempts == [0.0, 64.0]
    gap = {"at": WALL0 + 64_000, "audio_ms": 3000, "lost_ms": 61_000, "reason": "source_lost", "open": False}
    assert run.report["gaps"] == [gap]
    last = run.statuses()[-1]
    assert last["stalled"] is False and last["recoveries"] == 1


def test_a_healthy_microphone_is_left_alone_during_a_pause_and_a_dead_one_is_noticed(tmp_path, monkeypatch, clock):
    def scenario(run):
        for _ in range(4):
            run.second()
            yield
        run.control(service.PAUSE_FILE)
        yield                     # the engine pauses at the end of this pass
        for _ in range(10):       # t=5..14: paused, the device keeps delivering
            run.second()
            yield
        run.source.silent = True  # and then it dies, still paused
        for _ in range(5):        # t=15..19: noticed at t=19, device 1 opened
            run.second()
            yield
        run.second()              # t=20: device 1 delivers, still paused
        yield
        run.control(service.PAUSE_FILE, present=False)
        yield
        for _ in range(2):
            run.second()
            yield

    run = _engine(tmp_path, monkeypatch, clock, scenario, devices=lambda n: Hand("합성 마이크 %d" % n))
    assert run.kinds("paused", "resumed", "stalled", "recovered") == ["paused", "stalled", "recovered", "resumed"]
    assert run.attempts == [0.0, 19.0]
    assert all(s["paused"] is True and s["stalled"] is True for s in run.between("stalled", "recovered"))
    gap = {"at": WALL0 + 20_000, "audio_ms": 4000, "lost_ms": 6000, "reason": "no_data", "open": False}
    assert run.report["gaps"] == [gap]
    assert run.report["total_ms"] == 6000 and len(run.report["pauses"]) == 1


def test_pausing_and_resuming_do_not_end_an_open_stall(tmp_path, monkeypatch, clock):
    """The microphone is open but silent and cannot be opened again. Pausing must not read as 'sound came
    back'. When the old stream delivers again by itself, that is the one recovery."""
    def scenario(run):
        for _ in range(3):
            run.second()
            yield
        run.source.silent = True
        for _ in range(6):        # t=4..9: noticed at t=8, every reopen fails
            run.second()
            yield
        run.control(service.PAUSE_FILE)
        for _ in range(4):        # t=10..13: paused inside the stall
            run.second()
            yield
        run.control(service.PAUSE_FILE, present=False)
        for _ in range(2):        # t=14..15: resumed, still silent
            run.second()
            yield
        run.source.silent = False
        run.second()              # t=16: the same stream delivers again
        yield
        run.second()
        yield

    def devices(n):
        if n:
            raise RuntimeError("합성: 장치 없음")
        return Hand()

    run = _engine(tmp_path, monkeypatch, clock, scenario, devices)
    assert run.kinds("paused", "resumed", "stalled", "recovered") == ["stalled", "paused", "resumed", "recovered"]
    waiting = run.between("stalled", "recovered")
    assert len(waiting) >= 8 and all(s["stalled"] is True for s in waiting)
    assert any(s["paused"] for s in waiting) and len(run.opened) == 1
    assert [b - a for a, b in zip(run.attempts[1:], run.attempts[2:])] == [3.0, 3.0]  # every RETRY_EVERY_S
    gap = {"at": WALL0 + 16_000, "audio_ms": 3000, "lost_ms": 13_000, "reason": "no_data", "open": False}
    assert run.report["gaps"] == [gap]  # one gap, from the last sound to the first sound again
    assert run.report["total_ms"] == 5000 and run.statuses()[-1]["recoveries"] == 1


# ---------- the limit
def test_a_microphone_that_never_delivers_again_ends_as_source_lost_after_the_limit(tmp_path, monkeypatch, clock):
    """Every reopen 'succeeds' and stays silent. The limit still runs out, once, and the missing tail is on record."""
    monkeypatch.setattr(service, "RECOVER_LIMIT_S", 20.0)

    def scenario(run):
        for _ in range(3):
            run.second()
            yield
        run.source.silent = True
        for _ in range(200):
            run.second()
            yield

    run = _engine(tmp_path, monkeypatch, clock, scenario, devices=lambda n: Hand(silent=bool(n)))
    assert run.kinds("stalled", "recovered") == ["stalled"]
    assert run.attempts == [0.0, 8.0, 13.0, 18.0, 23.0, 28.0]  # noticed at t=8; 21 s of trying ends it at t=29
    assert all(s["stalled"] is True for s in run.statuses()[8:])
    report = _stop_json(run)
    assert report == run.report
    assert (report["cause"], report["certainty"], report["trigger"]) == ("source_lost", "confirmed", "no_data")
    assert report["partial"] is True and report["error"] is None and report["total_ms"] == 3000
    gap = {"at": WALL0 + 29_000, "audio_ms": 3000, "lost_ms": 26_000, "reason": "no_data", "open": True}
    assert report["gaps"] == [gap] and run.gaps_file() == [gap]


def test_time_asleep_is_not_time_spent_trying(tmp_path, monkeypatch, clock):
    """The stall is noticed, then the PC sleeps for 15 minutes (longer than the 10-minute limit). After waking
    the device is tried, comes back, and the gap says how long really nothing was recorded."""
    def scenario(run):
        for _ in range(3):
            run.second()
            yield
        run.clock.advance(1)
        run.source.active = False
        yield                     # t=4: noticed, the first reopen fails
        run.clock.advance(1)
        yield                     # t=5
        run.clock.advance(900)    # the PC slept
        yield                     # t=905: awake; this pass must try, not give up
        run.second()
        yield                     # t=906: sound again
        run.second()
        yield

    def devices(n):
        if n == 1:
            raise RuntimeError("합성: 장치 없음")
        return Hand()

    run = _engine(tmp_path, monkeypatch, clock, scenario, devices)
    assert service.RECOVER_LIMIT_S == 600.0
    assert run.kinds("stalled", "recovered") == ["stalled", "recovered"]
    assert run.attempts == [0.0, 4.0, 905.0]
    assert run.report["cause"] == "user_stop"
    gap = {"at": WALL0 + 906_000, "audio_ms": 3000, "lost_ms": 903_000, "reason": "source_lost", "open": False}
    assert run.report["gaps"] == [gap]
    assert run.recovery.tried_s <= 2.0  # only the seconds the engine was running count towards the limit


def test_waking_with_the_limit_almost_used_up_still_gets_one_attempt(tmp_path, monkeypatch, clock):
    monkeypatch.setattr(service, "RECOVER_LIMIT_S", 10.0)

    def scenario(run):
        for _ in range(3):
            run.second()
            yield
        run.clock.advance(1)
        run.source.active = False
        yield                     # t=4: noticed
        for _ in range(9):        # t=5..13: nine seconds of trying, the device stays away
            run.clock.advance(1)
            yield
        run.clock.advance(900)    # asleep
        for _ in range(50):       # t=913 and on
            yield
            run.clock.advance(1)

    def devices(n):
        if n:
            raise RuntimeError("합성: 장치 없음")
        return Hand()

    run = _engine(tmp_path, monkeypatch, clock, scenario, devices)
    assert 913.0 in run.attempts  # tried on waking although the retry was not due
    assert run.report["cause"] == "source_lost" and run.kinds("stalled", "recovered") == ["stalled"]
    assert run.attempts[-1] <= 915.0  # and the two seconds that were left of the limit then run out
    (gap,) = run.report["gaps"]
    assert gap["open"] is True and gap["lost_ms"] == round((run.clock.t - T0 - 3) * 1000) >= 900_000


def test_a_driver_that_hangs_on_every_open_is_not_mistaken_for_a_sleeping_pc(tmp_path, monkeypatch, clock):
    """Each attempt blocks for 40 s and fails. That is longer than the hole that means "the PC slept", but it is
    time spent trying: the limit must still run out. (Without counting the attempt itself every pass looked like
    a wake-up, nothing was counted, and the recording stayed stalled for ever.)"""
    monkeypatch.setattr(service, "RECOVER_LIMIT_S", 100.0)

    def scenario(run):
        for _ in range(3):
            run.second()
            yield
        run.clock.advance(1)
        run.source.active = False
        for _ in range(400):
            yield
            run.clock.advance(1)

    def devices(n):
        if n:
            clock.advance(40)  # the driver hangs, then says no
            raise RuntimeError("합성: 장치가 응답하지 않음")
        return Hand()

    run = _engine(tmp_path, monkeypatch, clock, scenario, devices)
    assert run.report["cause"] == "source_lost" and run.kinds("stalled", "recovered") == ["stalled"]
    # the first device, then four hung attempts: each counts 30 s at most (a longer one may hide a sleep), and
    # with the seconds between them that is over the limit of 100 s
    assert len(run.attempts) == 5
    assert run.recovery is not None and run.report["gaps"][0]["open"] is True


@pytest.mark.parametrize("comes_back", [False, True])
def test_a_pc_that_falls_asleep_inside_an_attempt_still_gets_its_tries_after_waking(tmp_path, monkeypatch, clock,
                                                                                     comes_back):
    """The stall is noticed and the first reopen is in flight when the PC goes to sleep for 15 minutes (longer
    than the limit). On waking that attempt fails, or opens a device that has not delivered yet. Either way the
    sleep is not time spent trying: the engine tries again and records when the device is there."""
    def scenario(run):
        for _ in range(3):
            run.second()
            yield
        run.clock.advance(1)
        run.source.active = False
        yield                      # t=4: noticed; the attempt started here sleeps for 900 s inside make()
        for _ in range(8):         # awake: a few seconds pass, the engine keeps trying
            run.clock.advance(1)
            yield
        run.second()               # the device that is open now delivers
        yield
        run.second()
        yield

    def devices(n):
        if n == 1:
            clock.advance(900)     # asleep inside the attempt
            if not comes_back:
                raise RuntimeError("합성: 깨어난 직후에는 장치가 아직 없음")
        return Hand()

    run = _engine(tmp_path, monkeypatch, clock, scenario, devices)
    assert service.RECOVER_LIMIT_S == 600.0
    assert run.kinds("stalled", "recovered") == ["stalled", "recovered"]
    assert run.report["cause"] == "user_stop"
    assert len(run.attempts) >= (2 if comes_back else 3)  # after waking there was a device to ask
    (gap,) = run.report["gaps"]
    assert gap["open"] is False and gap["lost_ms"] >= 900_000 and gap["reason"] == "source_lost"
    assert run.recovery.tried_s < 60.0  # the 900 s asleep count as one capped attempt, not as 15 minutes


# ---------- ending inside a stall
@pytest.mark.parametrize("ending", ["user_stop", "duration", "discard"])
def test_ending_inside_a_stall_puts_the_missing_tail_on_record(tmp_path, monkeypatch, clock, ending):
    """The stream went away without an error (a plain device removal) and is not back when the recording ends."""
    def scenario(run):
        for _ in range(3):
            run.second()
            yield
        run.clock.advance(1)
        run.source.active = False
        yield                     # t=4: noticed
        for _ in range(2):        # t=5, 6
            run.clock.advance(1)
            yield
        if ending == "discard":
            run.control(service.DISCARD_FILE)
        elif ending == "duration":
            for _ in range(5):  # the engine reads the clock at the top of a pass: the next one sees the six seconds
                yield

    def devices(n):
        if n:
            raise RuntimeError("합성: 장치 없음")
        return Hand()

    argv = ["--duration", "6"] if ending == "duration" else []
    run = _engine(tmp_path, monkeypatch, clock, scenario, devices, argv)
    assert run.kinds("stalled", "recovered") == ["stalled"]
    if ending == "discard":
        assert run.kinds("stopped", "discarded") == ["discarded"]
        assert run.report["cause"] == "user_discard" and run.report["gaps"] == [] and run.gaps_file() is None
        return
    report = _stop_json(run)
    assert report == run.report and run.kinds("stopped", "discarded") == ["stopped"]
    assert report["cause"] == ("scheduled_stop" if ending == "duration" else "user_stop")
    gap = {"at": WALL0 + 6000, "audio_ms": 3000, "lost_ms": 3000, "reason": "source_lost", "open": True}
    assert report["gaps"] == [gap] and run.gaps_file() == [gap]
    assert report["partial"] is True and report["total_ms"] == 3000


def test_a_gap_that_cannot_be_noted_never_keeps_the_recording_from_being_closed(tmp_path, monkeypatch, clock):
    """D-050: bookkeeping about the stall must not stand between the recording and its stop.json. A recording
    that ended because the device never came back is partial even then."""
    monkeypatch.setattr(service, "RECOVER_LIMIT_S", 5.0)

    def broken(self, *args, **kwargs):
        raise RuntimeError("합성: 기록 실패")

    monkeypatch.setattr(RecordingSession, "note_gap", broken)

    def scenario(run):
        for _ in range(3):
            run.second()
            yield
        run.clock.advance(1)
        run.source.active = False
        for _ in range(50):
            yield
            run.clock.advance(1)

    def devices(n):
        if n:
            raise RuntimeError("합성: 장치 없음")
        return Hand()

    run = _engine(tmp_path, monkeypatch, clock, scenario, devices)
    assert run.code == 0 and run.kinds("stopped") == ["stopped"]
    report = _stop_json(run)
    assert report["cause"] == "source_lost" and report["partial"] is True and report["total_ms"] == 3000


# ---------- the same loop with real threads and the real clock
def test_engine_run_with_real_threads_recovers_and_saves_one_recording(tmp_path, monkeypatch):
    """The first device dies after 0.3 s, the second is opened and delivers; the test stops the recording once
    it has seen the recording grow after the recovery. The duration is only a net under a broken run."""
    made = []

    def make(args):
        made.append(1)
        return Device(interrupt_after_seconds=0.3) if len(made) == 1 else Device()

    monkeypatch.setattr(service, "_make_source", make)
    monkeypatch.setattr(service, "RETRY_EVERY_S", 0.1)
    seen, state = [], {}

    def emit(kind, **payload):
        seen.append({"type": kind, **payload})
        if kind == "recovered":
            state["audio_ms"] = payload["audio_ms"]
        elif (kind == "status" and "audio_ms" in state and "stop" not in state
              and payload["elapsed_ms"] > state["audio_ms"]):
            state["stop"] = True
            (Path(payload["session_dir"]) / service.STOP_FILE).write_text("", encoding="utf-8")

    args = service.parse_args(["--out", str(tmp_path / "recordings"), "--duration", "120", "--no-finalize"])
    assert service.record(args, emit) == 0
    kinds = [e["type"] for e in seen if e["type"] in ("stalled", "recovered")]
    assert kinds == ["stalled", "recovered"] and len(made) == 2
    report = next(e for e in seen if e["type"] == "stopped")["report"]
    assert report["cause"] == "user_stop" and report["partial"] is True
    (gap,) = report["gaps"]
    assert gap["open"] is False and gap["reason"] == "source_lost" and report["total_ms"] > gap["audio_ms"]
    last = [e for e in seen if e["type"] == "status"][-1]
    assert last["stalled"] is False and last["recoveries"] == 1


# ---------- Windows microphone privacy
def _registry(values):
    return lambda hive, key: values.get((hive, key.rsplit("\\", 1)[-1] if key.endswith("NonPackaged") else "microphone"))


def test_microphone_access_reads_the_three_switches():
    allow = {("HKLM", "microphone"): "Allow", ("HKCU", "microphone"): "Allow", ("HKCU", "NonPackaged"): "Allow"}
    assert preflight.microphone_access(_registry(allow)) == {"allowed": True, "blocked_by": None}
    for blocked, key in (("device", ("HKLM", "microphone")), ("user", ("HKCU", "microphone")),
                         ("desktop_apps", ("HKCU", "NonPackaged"))):
        assert preflight.microphone_access(_registry({**allow, key: "Deny"})) == {"allowed": False, "blocked_by": blocked}
        assert "Windows" in preflight.BLOCKED_TEXT[blocked] or "PC" in preflight.BLOCKED_TEXT[blocked]
    assert preflight.microphone_access(_registry({})) == {"allowed": None, "blocked_by": None}  # cannot tell: say nothing


def test_sound_check_names_the_privacy_switch_instead_of_calling_silence_normal(tmp_path, monkeypatch):
    from lecturehub.ui.backend import Backend

    fake = SimpleNamespace(_state={"probe": {}}, _lock=__import__("threading").RLock(), _probe_mode="mic", fake_engine=False)
    fake._mic_blocked = lambda: Backend._mic_blocked(fake)
    monkeypatch.setattr(preflight, "microphone_access", lambda read=None: {"allowed": False, "blocked_by": "desktop_apps"})
    Backend._on_probe(fake, {"type": "probe_done", "verdict": "silent", "peak": -90.0})
    assert "데스크톱 앱" in fake._state["probe"]["mic_blocked"]
    monkeypatch.setattr(preflight, "microphone_access", lambda read=None: {"allowed": True, "blocked_by": None})
    Backend._on_probe(fake, {"type": "probe_done", "verdict": "silent", "peak": -90.0})
    assert fake._state["probe"]["mic_blocked"] is None  # a quiet room is still just a quiet room
    Backend._on_probe(fake, {"type": "probe_done", "verdict": "ok", "peak": -30.0})
    assert fake._state["probe"]["mic_blocked"] is None


# ---------- disk space
def test_disk_space_counts_what_a_finished_recording_needs(tmp_path):
    """An hour of recording ends with its MP3 only if there is room for the pieces written while recording, the
    merged WAV made from them (the same bytes again) and the MP3: about 734 MB at 48 kHz, not 346 MB."""
    hour = 48000 * 2 * 3600 * 2 + 96_000 // 8 * 3600

    def usage(free):
        return lambda path: SimpleNamespace(free=free)

    folder = tmp_path / "없는 폴더" / "recordings"
    # room for exactly two hours: said as two hours, and that is below the three-hour notice line
    assert preflight.disk_space(folder, usage(2 * hour)) == {"free_bytes": 2 * hour, "hours": 2.0, "low": True}
    assert preflight.disk_space(folder, usage(3 * hour))["low"] is False
    assert preflight.disk_space(folder, usage(3 * hour - 50 * MIB))["low"] is True
    plenty = preflight.disk_space(folder, usage(200 * 2**30))
    assert plenty["low"] is False and 290 < plenty["hours"] < 295
    assert preflight.disk_warning(tmp_path, usage(200 * 2**30)) is None

    # the review's case: 1.2 GB free was announced as 3.7 hours, with no notice; a two-hour class cannot be finished
    tight = preflight.disk_space(folder, usage(1_200_000_000))
    assert tight["hours"] == 1.6 and tight["low"] is True
    text = preflight.disk_warning(tmp_path, usage(1_200_000_000))
    assert "1.1 GB" in text and "1.6시간" in text

    def broken(path):
        raise OSError("드라이브 없음")

    assert preflight.disk_space(tmp_path, broken) == {"free_bytes": None, "hours": None, "low": False}
    assert preflight.disk_warning(tmp_path, broken) is None  # a drive that cannot be asked never blocks or nags


def test_status_carries_the_disk_figures_of_this_recording(tmp_path, monkeypatch, clock):
    """Through the engine loop: the status event itself says disk_low / disk_minutes. Both are about a recording
    that ends with its MP3, at this recording's sample rate (16 kHz here): what is free, less what finishing the
    part recorded so far needs, and how many more minutes can be recorded and finished out of the rest."""
    monkeypatch.setattr(service, "DISK_CHECK_EVERY_S", 1.0)  # every pass below is ten seconds long
    low_line = 500 * MIB
    assert preflight.LOW_BELOW_BYTES == low_line
    piece, mp3 = 16000 * 2, 96_000 // 8      # bytes per recorded second: the piece on disk, the MP3
    finish = piece + mp3                     # what a second already recorded still needs: the merged copy, the MP3
    minute = (piece + finish) * 60           # what one more minute needs from start to MP3
    disk = {"free": None, "error": None}
    shots = []

    def space(folder):
        if disk["error"]:
            raise disk["error"]
        return {"free_bytes": disk["free"], "hours": None, "low": False}

    monkeypatch.setattr(preflight, "disk_space", space)

    def scenario(run):
        for free, error in ((200 * 2**30, None),                    # plenty
                            (100 * MIB, None),                      # nearly full
                            (low_line + 30 * finish - 1, None),     # 30 s recorded: one byte under the line
                            (low_line + 40 * finish, None),         # 40 s recorded: exactly on it
                            (50 * finish - 1, None),                # 50 s recorded: even that cannot be finished
                            (None, None),                           # the drive gives no answer
                            (0, OSError("합성: 드라이브 오류"))):      # the drive cannot be asked at all
            disk["free"], disk["error"] = free, error
            run.second(seconds=10)
            yield
            shots.append(run.statuses()[-1])

    run = _engine(tmp_path, monkeypatch, clock, scenario)
    assert [s["elapsed_ms"] for s in shots] == [10_000, 20_000, 30_000, 40_000, 50_000, 60_000, 70_000]
    figures = [(s.get("disk_low"), s.get("disk_minutes")) for s in shots]
    assert figures == [
        (False, (200 * 2**30 - 10 * finish) // minute),
        (True, 22),   # 100 MiB at 16 kHz: 22 more minutes that can be finished (the 48 kHz, pieces-only figure was 18)
        (True, (low_line - 1) // minute),
        (False, low_line // minute),
        (True, 0),
        (None, None),
        (None, None),
    ]
    assert run.report["cause"] == "user_stop" and run.report["total_ms"] == 70_000  # a disk that cannot be asked stops nothing
