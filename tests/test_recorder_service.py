"""Recorder engine process used by the desktop app (JSON lines out, control files in)."""
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from lecturehub.recorder import service


def _spawn(*args):
    process = subprocess.Popen([sys.executable, "-m", "lecturehub.recorder.service", "--fake", *args],
                               stdout=subprocess.PIPE, stdin=subprocess.DEVNULL)
    events: list[dict] = []

    def read():
        try:
            for line in process.stdout:
                events.append(json.loads(line))
        except (ValueError, OSError):
            pass  # the test closed the pipe on purpose (the app went away)

    thread = threading.Thread(target=read, daemon=True)
    thread.start()
    return process, events, thread


def _wait(events, kind, timeout=20):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        found = [e for e in events if e["type"] == kind]
        if found:
            return found[0]
        time.sleep(0.05)
    raise TimeoutError(kind)


def test_STOP_001_engine_protocol_bookmark_stop_finalize(tmp_path):
    """STOP-001: the app's stop request ends in user_stop + stop.json, then a verified master."""
    process, events, thread = _spawn("--course", "구조역학", "--out", str(tmp_path / "녹음 (합성)"))
    started = _wait(events, "started")
    folder = Path(started["session_dir"])
    assert started["course"] == "구조역학" and started["mode"] == "mic"
    assert any(e["type"] == "warning" and "허락" in e["message"] for e in events)
    time.sleep(0.8)
    (folder / service.BOOKMARK_FILE).write_text("1\n", encoding="utf-8")
    bookmark = _wait(events, "bookmark")
    time.sleep(0.5)
    (folder / service.STOP_FILE).write_text("stop", encoding="utf-8")
    assert process.wait(30) == 0
    thread.join(5)
    kinds = [e["type"] for e in events if e["type"] not in ("status", "warning")]
    assert kinds == ["started", "bookmark", "stopped", "finalizing", "finalized", "compacted"]
    assert events[-1]["result"] == {"state": "off"}  # P-053 compaction is off in tests (conftest)
    events = events[:-1]
    statuses = [e for e in events if e["type"] == "status"]
    assert statuses and all(isinstance(s["elapsed_ms"], int) for s in statuses)
    stop = json.loads((folder / "stop.json").read_text(encoding="utf-8"))
    assert (stop["cause"], stop["certainty"], stop["trigger"]) == ("user_stop", "confirmed", "app_stop_file")
    mark = json.loads((folder / service.BOOKMARKS).read_text(encoding="utf-8").strip())
    assert mark["n"] == 1 and 0 < mark["at_ms"] <= stop["total_ms"] and bookmark["at_ms"] == mark["at_ms"]
    assert json.loads((folder / service.LIVE).read_text(encoding="utf-8"))["state"] == "finalized"
    assert events[-1]["duration_difference_ms"] == 0


def test_engine_keeps_recording_when_the_app_goes_away(tmp_path):
    """A crashed window closes its end of the pipe; the engine must keep recording."""
    process, events, _thread = _spawn("--out", str(tmp_path), "--no-finalize")
    folder = Path(_wait(events, "started")["session_dir"])
    process.stdout.close()  # the reader (the app) is gone
    time.sleep(1.5)
    first = json.loads((folder / service.LIVE).read_text(encoding="utf-8"))["elapsed_ms"]
    time.sleep(1.5)
    second = json.loads((folder / service.LIVE).read_text(encoding="utf-8"))["elapsed_ms"]
    assert process.poll() is None and second > first
    (folder / service.STOP_FILE).write_text("stop", encoding="utf-8")
    assert process.wait(30) == 0
    stop = json.loads((folder / "stop.json").read_text(encoding="utf-8"))
    assert stop["cause"] == "user_stop" and not stop["partial"] and stop["total_ms"] >= second


def test_emitter_swallows_a_broken_pipe():
    class Broken:
        def write(self, text):
            raise OSError(22, "Invalid argument")

        def flush(self):
            pass

    emit = service.Emitter(Broken())
    emit("status", elapsed_ms=1)
    emit("status", elapsed_ms=2)  # no exception, and no further attempts
    assert emit.alive is False


@pytest.mark.parametrize(("level", "verdict"), [(-30.0, "ok"), (-110.0, "silent"), (-70.0, "quiet")])
def test_probe_reports_levels_without_writing_files(tmp_path, level, verdict):
    result = subprocess.run([sys.executable, "-m", "lecturehub.recorder.service", "--fake", "--fake-dbfs",
                             str(level), "--probe", "1"], capture_output=True, cwd=tmp_path, timeout=30, check=True)
    events = [json.loads(line) for line in result.stdout.decode("utf-8").splitlines()]
    assert events[0]["type"] == "probe_started" and events[-1]["type"] == "probe_done"
    assert any(e["type"] == "probe" for e in events)
    assert events[-1]["verdict"] == verdict
    assert list(tmp_path.iterdir()) == []


def test_discard_while_recording_stops_without_a_master(tmp_path):
    """User 2026-09-29: a mistaken recording is thrown away while it runs — no finalize, no master."""
    process, events, thread = _spawn("--course", "구조역학", "--out", str(tmp_path / "버리기 (합성)"))
    folder = Path(_wait(events, "started")["session_dir"])
    time.sleep(0.6)
    (folder / service.DISCARD_FILE).write_text("discard", encoding="utf-8")
    assert process.wait(30) == 0
    thread.join(5)
    kinds = [e["type"] for e in events if e["type"] not in ("status", "warning")]
    assert kinds == ["started", "discarded"]
    stop = json.loads((folder / "stop.json").read_text(encoding="utf-8"))
    assert (stop["cause"], stop["trigger"]) == ("user_discard", "app_discard")
    assert json.loads((folder / service.LIVE).read_text(encoding="utf-8"))["state"] == "discarded"
    assert not list(folder.glob("master_*")) and not list(folder.glob("finalize_*"))
