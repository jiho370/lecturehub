"""Recorder engine process for the desktop app.

The app starts this module as its own process, so a crashed or closed window
never stops a recording. Protocol:

- stdout: one JSON object per line (``started``, ``status``, ``bookmark``,
  ``stopped``, ``finalizing``, ``finalized``, ``compacted`` (P-053), ``finalize_error``, ``error``,
  ``probe``, ``probe_done``). Write failures (the app went away) are ignored.
- commands: files in the session folder, so a restarted app can still reach a
  running engine. ``control.stop`` requests a stop; each line appended to
  ``control.bookmarks`` is one ★ bookmark; while ``control.pause`` exists the
  recording is paused (audio not written; ``paused``/``resumed`` events, pauses.json).
- ``live.json`` in the session folder mirrors the latest status every second.

stop.json is written on every exit path that Windows allows (see console.py).
"""
from __future__ import annotations

import argparse
import json
import math
import queue
import signal
import sys
import time
from pathlib import Path

from .console import CloseGuard
from .heartbeat import now_ms, write_json
from .power import KeepAwake

STOP_FILE = "control.stop"
BOOKMARK_FILE = "control.bookmarks"
PAUSE_FILE = "control.pause"
DISCARD_FILE = "control.discard"  # user: throw this recording away now (stop, no finalize; the app recycles it)
BOOKMARKS = "bookmarks.jsonl"
LIVE = "live.json"
PROBE_OK_DBFS = -45.0      # peak at or above: clearly hearing sound
PROBE_SILENT_DBFS = -85.0  # peak at or below: (almost) digital silence

_CAUSES = {
    "app_button": ("user_stop", "confirmed"),
    "app_stop_file": ("user_stop", "confirmed"),
    "app_discard": ("user_discard", "confirmed"),
    "ctrl_c": ("user_stop", "confirmed"),
    "ctrl_break": ("user_stop", "confirmed"),
    "console_close": ("user_stop", "estimated"),
    "logoff": ("system_shutdown", "confirmed"),
    "system_shutdown": ("system_shutdown", "confirmed"),
}


class Emitter:
    """JSON lines to stdout; a vanished reader must never stop the recording."""

    def __init__(self, stream=None):
        self.stream = stream if stream is not None else sys.stdout
        self.alive = self.stream is not None

    def __call__(self, kind: str, **payload) -> None:
        if not self.alive:
            return
        try:
            self.stream.write(json.dumps({"type": kind, "at": now_ms(), **payload}, ensure_ascii=False,
                                         allow_nan=False) + "\n")
            self.stream.flush()
        except (OSError, ValueError):
            self.alive = False


def _finite(value: float) -> float | None:
    return round(value, 1) if math.isfinite(value) else None


def _make_source(args):
    if args.fake:
        from .capture import FakeSource

        return FakeSource(sample_rate=16000, channels=2, duration_seconds=None, realtime=True,
                          rms_dbfs=args.fake_dbfs)
    from .capture import WasapiSource

    return WasapiSource(args.mode, device_name=args.device_name or None)


def probe(args, emit: Emitter) -> int:
    """Open the input for a few seconds and report levels; writes no files."""
    from .levels import measure_dbfs, mono_pcm

    blocks: queue.Queue = queue.Queue(maxsize=4096)
    source = _make_source(args)

    def callback(data: bytes, frames: int, status: int) -> None:
        try:
            blocks.put_nowait(data)
        except queue.Full:
            pass

    emit("probe_started", device=source.device_name, mode=args.mode, seconds=args.probe)
    source.start(callback)
    end = time.monotonic() + args.probe
    peak_max, rms_values, window_peak, window_rms = -math.inf, [], -math.inf, []
    next_emit = time.monotonic() + 0.25
    try:
        while time.monotonic() < end:
            try:
                data = blocks.get(timeout=0.05)
            except queue.Empty:
                data = None
            if data:
                peak, rms = measure_dbfs(mono_pcm(data, source.channels))
                peak_max, window_peak = max(peak_max, peak), max(window_peak, peak)
                rms_values.append(rms)
                window_rms.append(rms)
            if time.monotonic() >= next_emit:
                emit("probe", peak=_finite(window_peak),
                     rms=_finite(max(window_rms) if window_rms else -math.inf),
                     remaining_ms=max(0, int((end - time.monotonic()) * 1000)))
                window_peak, window_rms = -math.inf, []
                next_emit += 0.25
    finally:
        source.stop()
    finite = [r for r in rms_values if math.isfinite(r)]
    verdict = ("no_data" if not rms_values else "ok" if peak_max >= PROBE_OK_DBFS
               else "silent" if peak_max <= PROBE_SILENT_DBFS else "quiet")
    emit("probe_done", verdict=verdict, peak=_finite(peak_max),
         rms=_finite(sum(finite) / len(finite)) if finite else None, device=source.device_name)
    return 0


STALL_SECONDS = 5.0        # a microphone delivers sound every few ms; this long without any is a dead stream
RETRY_EVERY_S = 3.0
RECOVER_LIMIT_S = 600.0    # keep trying this long (a laptop that slept, a headset plugged back), then save and stop
SLEPT_JUMP_S = 30.0        # the loop steps every 50 ms and an attempt normally takes a few seconds: a longer hole
#                            between two steps means the engine did not run (the PC slept)
DISK_CHECK_EVERY_S = 20.0


class Recovery:
    """Open the device again when the sound stopped arriving (release review 2026-11-16).

    A PC that slept and woke, or a headset pulled out and put back, used to leave the recording either ended or
    running without sound. Now one stall gives one ``stalled`` event, the device is opened again in the same
    session, and when the sound is back there is one ``recovered`` event and one entry in gaps.json.

    - A stall begins when the source reports an error or is no longer active (``source_lost``) or, for the
      microphone only, when the device has delivered no block for STALL_SECONDS (``no_data``). That is measured
      where the device delivers, not where the disk writes, and it is measured during a pause too. A PC-sound
      (loopback) stream is silent by design while nothing plays, so silence alone is never a stall there.
    - Attempts are at least RETRY_EVERY_S apart. For the microphone an opened device is not yet a recovery: the
      stall stays open until a block has arrived after it was noticed or after the last reopen, and a device
      that opened but delivers nothing for STALL_SECONDS is opened again. Pausing or resuming does not end a
      stall. For PC sound a successful reopen ends the stall at once, because there may be nothing to hear.
    - A stall ends well in ``finish``: ``lost_ms`` is wall-clock time from the last block the device delivered
      before the stall to that moment, so the seconds it took to notice and any time the PC slept are in it.
    - ``step`` answers ``"give_up"`` after RECOVER_LIMIT_S of trying. A hole of more than SLEPT_JUMP_S between
      two steps (the engine did not run: the PC slept) is not counted as trying, and the step after such a hole
      always makes an attempt first.
    - When the recording ends inside a stall, ``end_open`` writes it as a gap with ``"open": true``.

    The engine loop uses this only for real devices (``source.recoverable``). While the device delivers blocks
    and reports no error, ``reason`` only reads; nothing is reopened or written.

    ``clock`` times the attempts and the limit. Whether a block has arrived is asked of the session, which
    stamps arrivals with ``time.monotonic()`` itself.
    """

    def __init__(self, session, args, emit: Emitter, make=None, clock=None):
        self.session, self.args, self.emit = session, args, emit
        self.clock = clock or time.monotonic
        self.make = make or _make_source
        self.since: float | None = None        # ``clock`` time at which the open stall was noticed
        self.since_ms = 0                      # wall clock of the last sound before it
        self.audio_ms = 0                      # where in the audio it is: everything heard before it
        self.why = ""                          # the reason it began with
        self.reopened_at: float | None = None  # ``clock`` time of the last successful reopen in this stall
        self.tried_s = 0.0                     # time spent trying in this stall (what the limit counts)
        self.next_try = 0.0
        self.count = 0
        self.last_error = ""
        self._seen = 0.0                       # ``clock`` time of the previous step
        self._mark = 0.0                       # time.monotonic(): a block after this ends a microphone stall

    @property
    def stalled(self) -> bool:
        return self.since is not None

    def reason(self) -> str | None:
        """What is wrong with the source now, or ``None``: it delivers (microphone) or is open (PC sound)."""
        source = self.session.source
        if source.error or not source.active:
            return "source_lost"
        if self.args.mode != "mic":
            return None
        if self.since is not None:  # an open stall ends with a block, not with a pause, a resume or an open device
            return None if self.session.arrived_since(self._mark) else "no_data"
        return "no_data" if self.session.idle_seconds() >= STALL_SECONDS else None

    def step(self, reason: str) -> str:
        """Called on every loop pass while ``reason()`` names a problem.

        ``"waiting"``: nothing to do yet, or an attempt failed. ``"reopened"``: the microphone opened; the stall
        stays open until a block arrives. ``"recovered"``: PC sound reopened and the stall finished.
        ``"give_up"``: the limit is used up.
        """
        now = self.clock()
        woke = False
        if self.since is None:
            self.since, self.since_ms, self.why = now, self.session.last_sound_ms(), reason
            self.audio_ms = self.session.heard_ms()
            self.tried_s, self.next_try, self.reopened_at = 0.0, now, None
            self._mark = time.monotonic()
            self.emit("stalled", reason=reason)
        elif now - self._seen > SLEPT_JUMP_S:
            woke = True  # the engine did not run: not time spent trying, and the device gets a try right now
        else:
            self.tried_s += now - self._seen
        self._seen = now
        if self.tried_s > RECOVER_LIMIT_S:
            return "give_up"
        if not woke:
            if now < self.next_try:
                return "waiting"
            if reason == "no_data" and self.reopened_at is not None and now - self.reopened_at < STALL_SECONDS:
                return "waiting"  # it opened a moment ago: let it deliver before opening it yet again
        self.next_try = now + RETRY_EVERY_S
        try:
            self.session.swap_source(self.make(self.args))
        except Exception as exc:  # noqa: BLE001 - the device may not be back yet: try again
            self.last_error = str(exc)
            self._attempt_took(now)
            return "waiting"
        self._attempt_took(now)
        self.reopened_at = self.clock()
        self._mark = time.monotonic()
        if self.args.mode == "mic":
            return "reopened"
        self.finish()
        return "recovered"

    def _attempt_took(self, began: float) -> None:
        """An attempt is time spent trying: count it and move ``_seen`` past it, so a device whose opening hangs
        is not mistaken for a PC that slept (which would never use up the limit). But at most SLEPT_JUMP_S per
        attempt: the PC can fall asleep while an attempt is in flight, and that sleep must not use up the limit
        either (the next step then tries again, because the retry time has passed)."""
        after = self.clock()
        self.tried_s += min(max(0.0, after - began), SLEPT_JUMP_S)
        self._seen = after

    def finish(self) -> dict:
        """The one place a stall ends well: note the gap, count the recovery, say ``recovered``."""
        gap = self.session.note_gap(now_ms() - self.since_ms, self.why, audio_ms=self.audio_ms)
        self.count += 1
        self.since = self.reopened_at = None
        self.emit("recovered", device=self.session.source.device_name, recoveries=self.count, **gap)
        return gap

    def end_open(self) -> None:
        """The recording ends while a stall is open: put the missing tail on record as a gap marked ``open``
        (it makes the stop report partial). Never raises: stop.json is written next and must not be held up."""
        if self.since is None:
            return
        try:
            self.session.note_gap(now_ms() - self.since_ms, self.why, still_open=True, audio_ms=self.audio_ms)
        except Exception:  # noqa: BLE001, S110 - D-050: the recording still has to be closed and reported
            pass
        self.since = self.reopened_at = None


def _append_bookmark(session, emit: Emitter, count: int) -> dict:
    at_ms = session.writer.total_samples * 1000 // session.source.sample_rate
    record = {"n": count, "at_ms": at_ms, "label": "★", "created_at": now_ms()}
    with (session.session_dir / BOOKMARKS).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    emit("bookmark", **record)
    return record


def record(args, emit: Emitter) -> int:
    from .session import RecordingSession

    requested: list[str] = []

    def request_stop(trigger: str) -> None:
        if not requested:
            requested.append(trigger)

    handlers = {signal.SIGINT: lambda s, f: request_stop("ctrl_c")}
    if hasattr(signal, "SIGBREAK"):
        handlers[signal.SIGBREAK] = lambda s, f: request_stop("ctrl_break")
    for signum, handler in handlers.items():
        signal.signal(signum, handler)
    try:
        from lecturehub.core.recording_permission import for_output

        permission = for_output(Path(args.out), args.course)
        if not permission["allowed"]:
            raise PermissionError(permission["reason"])
        if permission.get("warning"):
            emit("warning", message=permission["warning"])
        source = _make_source(args)
        session = RecordingSession(source, Path(args.out), mode=args.mode, course=args.course,
                                   course_source=args.course_source,
                                   quiet_dbfs=args.quiet_dbfs)
    except Exception as exc:  # noqa: BLE001 - report every start failure to the app
        emit("error", stage="open", message=str(exc))
        return 1
    started = False
    bookmarks = 0
    try:
        with CloseGuard(request_stop) as guard, KeepAwake() as awake:  # K6: no sleep while recording
            session.start()
            started = True
            folder = session.session_dir
            emit("started", session_dir=str(folder), device=source.device_name, mode=args.mode,
                 course=args.course, sample_rate=source.sample_rate, pid=_pid(), keep_awake=awake.active)
            begin = time.monotonic()
            next_status = next_live = begin
            next_disk = begin
            disk: dict = {}
            recovery = Recovery(session, args, emit)
            cause, certainty, trigger = "unknown", "unknown", None
            while True:
                now = time.monotonic()
                if (folder / DISCARD_FILE).exists():
                    request_stop("app_discard")
                if (folder / STOP_FILE).exists():
                    request_stop("app_stop_file")
                if requested:
                    trigger = requested[0]
                    cause, certainty = _CAUSES[trigger]
                    break
                if session.error:
                    break
                broken = recovery.reason()
                if broken:
                    if not getattr(session.source, "recoverable", False):
                        if broken == "source_lost":
                            break  # a synthetic source that ended; as before
                    elif recovery.step(broken) == "give_up":
                        cause, certainty, trigger = "source_lost", "confirmed", broken
                        break
                elif recovery.stalled:  # the sound is back: after a reopen, or by itself before one succeeded
                    recovery.finish()
                if now >= next_disk:
                    disk = _disk_state(folder, session)
                    next_disk = now + DISK_CHECK_EVERY_S
                if args.duration is not None and now - begin >= args.duration:
                    cause, certainty, trigger = "scheduled_stop", "confirmed", "duration"
                    break
                want_pause = (folder / PAUSE_FILE).exists()
                if want_pause != session.paused and session.set_paused(want_pause):
                    emit("paused" if want_pause else "resumed", **session.pauses[-1])
                wanted = _bookmark_requests(folder)
                while bookmarks < wanted:
                    bookmarks += 1
                    _append_bookmark(session, emit, bookmarks)
                if now >= next_status:
                    status = {**_status(session, bookmarks, args), **disk, "stalled": recovery.stalled,
                              "recoveries": recovery.count}
                    emit("status", **status)
                    if now >= next_live:
                        write_json(folder / LIVE, {"at": now_ms(), "pid": _pid(), "state": "recording", **status})
                        next_live = now + 1.0
                    next_status = now + 0.25
                time.sleep(0.05)
            if cause != "user_discard":  # a thrown-away recording needs no account of what is missing
                recovery.end_open()
            report = session.stop(cause, certainty, trigger)
            started = False
            guard.mark_stopped()
    finally:
        if started:
            session.stop("unknown", "unknown")
    if report.get("cause") == "user_discard":  # no master, no link, no transcription: the app recycles the folder
        write_json(folder / LIVE, {"at": now_ms(), "pid": _pid(), "state": "discarded", "report": report})
        emit("discarded", report=report, session_dir=str(folder))
        return 0
    write_json(folder / LIVE, {"at": now_ms(), "pid": _pid(), "state": "stopped", "report": report})
    emit("stopped", report=report, session_dir=str(folder), bookmarks=bookmarks)
    if not args.no_finalize and report["total_samples"]:
        emit("finalizing", session_dir=str(folder))
        try:
            from .finalize import finalize_session

            result = finalize_session(folder)
            emit("finalized", session_dir=str(folder), master=result["master"],
                 total_ms=result["total_ms"], duration_difference_ms=result["duration_difference_ms"])
            write_json(folder / LIVE, {"at": now_ms(), "state": "finalized", "report": report,
                                       "master": result["master"]})
        except Exception as exc:  # noqa: BLE001 - segments stay; the app shows the error
            emit("finalize_error", session_dir=str(folder), message=str(exc))
        else:
            from .compact import after_finalize  # P-053: master WAV → Recycle Bin, segments → FLAC

            emit("compacted", session_dir=str(folder), result=after_finalize(folder))
    return 0


def _disk_state(folder: Path, session) -> dict:
    """``{"disk_low": bool, "disk_minutes": n}`` for the status line; empty when the drive cannot be asked.

    Both figures are about a recording that ends with its MP3, at this recording's own sample rate. ``spare`` is
    the free space minus what finishing the part recorded so far needs (the merged copy and the MP3).
    ``disk_minutes``: how many more minutes can be recorded and still be finished out of ``spare`` (0 when even
    the part so far no longer fits). ``disk_low``: ``spare`` is under LOW_BELOW_BYTES. The audio itself can
    still be written for more than twice that long before the disk is full. A second engine on the same disk
    is not counted here.
    """
    from . import preflight

    try:
        free = preflight.disk_space(folder)["free_bytes"]
    except OSError:  # the folder could not even be looked at; the next check asks again
        return {}
    if free is None:
        return {}
    rate = session.source.sample_rate
    recorded_ms = session.writer.total_samples * 1000 // rate
    spare = free - preflight.finish_bytes(recorded_ms, rate)
    minutes = spare / (preflight.finished_bytes_per_hour(rate) / 60)
    return {"disk_low": spare < preflight.LOW_BELOW_BYTES, "disk_minutes": max(0, int(minutes))}


def _bookmark_requests(folder: Path) -> int:
    try:
        return sum(1 for line in (folder / BOOKMARK_FILE).read_text(encoding="utf-8").splitlines() if line.strip())
    except (FileNotFoundError, UnicodeDecodeError):
        return 0


def _status(session, bookmarks: int, args) -> dict:
    peak, rms, quiet = session.level_snapshot()
    samples = session.writer.total_samples if session.writer else 0
    return {
        "elapsed_ms": samples * 1000 // session.source.sample_rate, "peak": _finite(peak), "rms": _finite(rms),
        "quiet": bool(quiet), "paused": session.paused, "pauses": len(session.pauses),
        "queue": session.queue.qsize(), "queue_high_water": session.queue_high_water,
        "dropped_samples": session.dropped_samples,
        "capture_status_count": session.status_count, "bookmarks": bookmarks,
        "device": session.source.device_name, "course": args.course, "mode": args.mode,
        "session_dir": str(session.session_dir),
    }


def _pid() -> int:
    import os

    return os.getpid()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(prog="lecturehub.recorder.service")
    parser.add_argument("--mode", choices=["mic", "loopback"], default="mic")
    parser.add_argument("--device-name", default="")
    parser.add_argument("--course", default="")
    parser.add_argument("--course-source", choices=["user_pick", "timetable_auto", "unknown"], default="unknown")
    parser.add_argument("--out", default="")
    parser.add_argument("--quiet-dbfs", type=float, default=-50.0)
    parser.add_argument("--duration", type=float, default=None)
    parser.add_argument("--no-finalize", action="store_true")
    parser.add_argument("--probe", type=float, default=None, help="level check only, seconds")
    parser.add_argument("--fake", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--fake-dbfs", type=float, default=-30.0, help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args(argv)
    emit = Emitter()
    try:
        if args.probe is not None:
            return probe(args, emit)
        if not args.out:
            emit("error", stage="args", message="--out이 필요합니다.")
            return 2
        return record(args, emit)
    except Exception as exc:  # noqa: BLE001 - last-resort report; stop.json handled in record()
        emit("error", stage="engine", message=str(exc))
        return 1
    finally:
        if not emit.alive:
            # The app closed its end of the pipe. Without this, flushing stdout at
            # interpreter exit fails and turns a clean stop into exit code 120.
            import os

            sys.stdout = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115 - lives until exit


if __name__ == "__main__":
    raise SystemExit(main())
