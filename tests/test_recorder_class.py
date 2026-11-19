"""One-click class recording: device-name selection, settings, timetable, safe stop."""

import codecs
import json
import sys
import threading
import time
from datetime import datetime

import numpy as np
import pytest
from typer.testing import CliRunner

from lecturehub.recorder import devices
from lecturehub.recorder.capture import FakeSource
from lecturehub.recorder.classconfig import ConfigError, current_course, load_config
from lecturehub.recorder.console import make_close_handler
from lecturehub.recorder.devices import Device, DeviceError
from lecturehub.recorder.levels import MixMonitor
from lecturehub.recorder.segments import pcm_header
from lecturehub.recorder.session import RecordingSession

MIC_ARRAY = "마이크 배열(2- 디지털 마이크용 인텔® 스마트 사운드 기술)"
LISTED = [
    Device(9, MIC_ARRAY, 48000, 2, False, False),
    Device(10, "스피커(2- Realtek(R) Audio) [Loopback]", 48000, 2, True, True),
    Device(12, "헤드셋 마이크(AirPods Hands-Free)", 16000, 1, False, True),
    Device(13, "USB 마이크 A", 48000, 1, False, False),
    Device(14, "USB 마이크 B", 48000, 1, False, False),
]


def test_device_name_substring_survives_reindexing():
    """Partial, case-insensitive match finds the array even when AirPods is the default input."""
    assert devices._choose_device(LISTED, "mic", None, "마이크 배열").index == 9
    moved = [Device(12 if d.index == 9 else d.index + 20, d.name, d.sample_rate, d.channels,
                    d.is_loopback, d.is_default) for d in LISTED]
    assert devices._choose_device(moved, "mic", None, "마이크 배열").index == 12
    assert devices._choose_device(LISTED, "loopback", None, "realtek").index == 10


def test_device_name_never_guesses():
    with pytest.raises(DeviceError, match="여러 장치"):
        devices._choose_device(LISTED, "mic", None, "USB 마이크")
    with pytest.raises(DeviceError, match="장치가 없습니다"):
        devices._choose_device(LISTED, "mic", None, "없는 장치")
    with pytest.raises(DeviceError, match="장치가 없습니다"):  # a loopback is not a mic
        devices._choose_device(LISTED, "mic", None, "Realtek")
    with pytest.raises(ValueError):
        devices._choose_device(LISTED, "mic", 9, "마이크 배열")


def _timetable(path, rows):
    text = "폴더,과목,요일,시작,종료\r\n" + "".join(f"{r}\r\n" for r in rows)
    path.write_bytes(codecs.BOM_UTF8 + text.encode("utf-8"))
    return path


def test_current_course_from_timetable(tmp_path):
    table = _timetable(tmp_path / "시간 표 (읽기전용).csv", [
        "구조역학,구조역학 및 실험,화,10:00,11:50",
        "토질역학,수자원설계,화,14:00,17:50",
        "환경공,환경조절공학 및 실험,목,13:00,14:50",
        "환경공,환경조절공학 및 실험,목,15:00,16:50",
        "기초어학,기초어학 1,수,14:00,15:15",
        "도시계,도시계획론,수,15:30,16:45",
        "갑,합성 과목 갑,금,09:00,10:30",
        "을,합성 과목 을,금,10:00,11:00",
    ])
    tuesday = datetime(2026, 9, 29, 10, 5)
    assert current_course(table, tuesday) == "구조역학"
    assert current_course(table, tuesday.replace(hour=9, minute=46)) == "구조역학"  # 15 min lead
    assert current_course(table, tuesday.replace(hour=9, minute=44)) == ""
    assert current_course(table, tuesday.replace(hour=12, minute=0)) == ""
    assert current_course(table, datetime(2026, 10, 1, 14, 55)) == "환경공"  # same folder twice
    assert current_course(table, datetime(2026, 9, 30, 15, 14)) == "기초어학"
    assert current_course(table, datetime(2026, 9, 30, 15, 15)) == ""  # 기초어학 end = 도시계 lead start
    assert current_course(table, datetime(2026, 9, 30, 15, 16)) == "도시계"
    assert current_course(table, datetime(2026, 10, 2, 10, 15)) == ""  # overlap → blank, not guessed
    assert current_course(table, datetime(2026, 10, 4, 10, 5)) == ""  # Sunday
    assert table.read_bytes().startswith(codecs.BOM_UTF8)  # read only


def _config(tmp_path, out_dir, timetable=None, device_name="마이크 배열", mode="mic"):
    lines = ["[recorder]", f"out_dir = '{out_dir}'", f'mode = "{mode}"', "quiet_dbfs = -50"]
    if device_name:
        lines.append(f'device_name = "{device_name}"')
    if timetable:
        lines.append(f"timetable = '{timetable}'")
    path = tmp_path / "설정 (한글)" / "recorder.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(codecs.BOM_UTF8 + "\r\n".join(lines).encode("utf-8"))
    return path


def test_load_config_korean_paths_and_errors(tmp_path, monkeypatch):
    out = tmp_path / "대학" / "26-2" / "_lecturehub" / "recordings"
    config = _config(tmp_path, out, timetable=tmp_path / "시간표.csv")
    loaded = load_config(config)
    assert loaded.out_dir == out and loaded.device_name == "마이크 배열" and loaded.mode == "mic"
    assert loaded.timetable == tmp_path / "시간표.csv"
    monkeypatch.setenv("LECTUREHUB_RECORDER_CONFIG", str(config))
    assert load_config().path == config
    with pytest.raises(ConfigError, match="없습니다"):
        load_config(tmp_path / "없음.toml")
    with pytest.raises(ConfigError, match="mode"):
        load_config(_config(tmp_path, out, mode="stereo"))


def test_close_handler_waits_for_stop_json_then_lets_windows_exit():
    requested = []
    stopped = threading.Event()
    handler = make_close_handler(requested.append, stopped, grace_seconds=5)
    assert handler(0) is False and handler(1) is False and not requested  # Ctrl+C/Break → Python
    threading.Timer(0.2, stopped.set).start()
    begin = time.monotonic()
    assert handler(2) is True
    assert requested == ["console_close"]
    assert 0.15 <= time.monotonic() - begin < 2
    bounded = make_close_handler(lambda trigger: None, threading.Event(), grace_seconds=0.1)
    begin = time.monotonic()
    assert bounded(6) is True and time.monotonic() - begin < 1  # never hangs past the grace


def test_console_close_writes_stop_json_before_handler_returns(tmp_path, monkeypatch):
    """Closing the window must not lose stop.json (Windows kills the process on return)."""
    from lecturehub.recorder import cli as recorder_cli
    from lecturehub.recorder import console

    guards = []

    class ObservableGuard(console.CloseGuard):
        def __enter__(self):
            guards.append(self)
            return self  # Do not install a real handler in the test process.

        def __exit__(self, *args):
            self.stopped.set()

    monkeypatch.setattr(console, "CloseGuard", ObservableGuard)
    source = FakeSource(sample_rate=8000, duration_seconds=None, realtime=True)
    session = RecordingSession(source, tmp_path)
    seen = {}

    def close_window():
        while not guards or session.session_dir is None:
            time.sleep(0.01)
        time.sleep(0.2)
        seen["returned"] = guards[0]._body(2)
        seen["stop_json_at_return"] = (session.session_dir / "stop.json").exists()

    closer = threading.Thread(target=close_window)
    closer.start()
    report = recorder_cli._run_session(source, session, duration=10)
    closer.join(5)
    assert seen == {"returned": True, "stop_json_at_return": True}
    assert (report["cause"], report["certainty"], report["trigger"]) == (
        "user_stop", "estimated", "console_close")
    assert report["total_samples"] > 0 and not report["partial"]


def test_queue_absorbs_long_disk_stalls(tmp_path):
    session = RecordingSession(FakeSource(), tmp_path)
    assert session.queue.maxsize * 1024 / 48000 >= 30  # seconds of 1024-frame blocks at 48 kHz


def test_mix_monitor_flags_phase_cancellation():
    rng = np.random.default_rng(1)
    a = rng.normal(0, 3000, 48000)
    b = rng.normal(0, 3000, 48000)

    def loss(left, right):
        monitor = MixMonitor()
        monitor.update(np.stack([left, right], axis=1).astype("<i2").tobytes(), 2)
        return monitor.loss_db

    assert loss(a, a) == pytest.approx(0, abs=0.05)
    assert loss(a, b) == pytest.approx(-3, abs=0.2)
    assert loss(a, -a) <= -60
    assert loss(a / 3000, a / 3000) is None  # below -60 dBFS: no evidence either way
    mono = MixMonitor()
    mono.update(a.astype("<i2").tobytes(), 1)
    assert mono.loss_db is None


@pytest.fixture(autouse=True)
def quiet_console(monkeypatch):
    """Keep tests from retitling or changing the mode of the developer's console."""
    import contextlib

    from lecturehub.recorder import console
    monkeypatch.setattr(console, "set_title", lambda text: None)
    monkeypatch.setattr(console, "quick_edit_disabled", contextlib.nullcontext)


def _fake_factory(calls):
    def factory(mode, device_id=None, *, device_name=None, **_):
        calls.append(device_name)
        if device_name == "없는 장치":
            raise DeviceError("'없는 장치'와 일치하는 WASAPI mic 장치가 없습니다.")
        return FakeSource(sample_rate=16000, channels=2, duration_seconds=None, realtime=True,
                          rms_dbfs=-30)
    return factory


def test_class_command_end_to_end(tmp_path, monkeypatch):
    """Config → course from timetable → record → stop.json → finalize; earlier crash recovered."""
    from lecturehub import cli
    from lecturehub.recorder import capture
    cli._register_all()
    out = tmp_path / "대학 (26-2)" / "_lecturehub" / "recordings"
    everyday = [f"합성과목,합성 과목,{day},00:00,23:59" for day in "월화수목금토일"]
    config = _config(tmp_path, out, timetable=_timetable(tmp_path / "시간표.csv", everyday))
    crashed = out / "20260928_220000_mic"
    crashed.mkdir(parents=True)
    (crashed / "session.json").write_text(json.dumps({"sample_rate": 16000}), encoding="utf-8")
    (crashed / "segment_000001.wav").write_bytes(pcm_header(16000, 0) + b"\1\0" * 1600)
    (crashed / "heartbeat.json").write_text(json.dumps({"total_samples": 0}), encoding="utf-8")
    calls = []
    monkeypatch.setattr(capture, "WasapiSource", _fake_factory(calls))

    result = CliRunner().invoke(cli.app, ["recorder", "class", "--config", str(config), "--duration", "0.6"])
    assert result.exit_code == 0, result.output
    assert calls == ["마이크 배열"]
    assert "합성과목" in result.output and "Ctrl+C" in result.output and "병합 완료" in result.output
    assert json.loads((crashed / "recovery.json").read_text(encoding="utf-8"))["cause"] == "process_killed"
    session_dir = next(p for p in out.iterdir() if p != crashed)
    assert json.loads((session_dir / "session.json").read_text(encoding="utf-8"))["course"] == "합성과목"
    stop = json.loads((session_dir / "stop.json").read_text(encoding="utf-8"))
    assert (stop["cause"], stop["trigger"]) == ("scheduled_stop", "duration")
    assert stop["stereo_mix_loss_db"] == pytest.approx(0, abs=0.1)
    assert list(session_dir.glob("finalize_*.json")) and list(session_dir.glob("segment_*.wav"))


def test_class_command_falls_back_to_default_device_loudly(tmp_path, monkeypatch):
    from lecturehub import cli
    from lecturehub.recorder import capture
    cli._register_all()
    config = _config(tmp_path, tmp_path / "rec", device_name="없는 장치")
    calls = []
    monkeypatch.setattr(capture, "WasapiSource", _fake_factory(calls))
    result = CliRunner().invoke(cli.app, ["recorder", "class", "--config", str(config),
                                          "--duration", "0.3", "--no-finalize", "--course", "구조역학"])
    assert result.exit_code == 0, result.output
    assert calls == ["없는 장치", None]
    assert "기본 장치로 녹음합니다" in result.output


def test_meter_line_fits_console_width_with_hangul():
    from lecturehub.recorder.cli import _fit
    line = _fit("0:00:01 peak -20.0 | 소리 매우 작음", 40)
    assert len(line) == 40 - 6  # six Hangul characters occupy two columns each
    cut = _fit("가" * 50, 11)
    assert cut == "가" * 5 + " "


CHILD = """
import sys
from pathlib import Path
from lecturehub.recorder.capture import FakeSource
from lecturehub.recorder.cli import _run_session
from lecturehub.recorder.session import RecordingSession
source = FakeSource(sample_rate=8000, duration_seconds=None, realtime=True)
_run_session(source, RecordingSession(source, Path(sys.argv[1])), duration=20)
"""
SEND_CTRL_C = """
import ctypes, sys
k = ctypes.windll.kernel32
k.FreeConsole()
assert k.AttachConsole(int(sys.argv[1]))
k.SetConsoleCtrlHandler(None, True)
assert k.GenerateConsoleCtrlEvent(0, 0)
"""


@pytest.mark.skipif(sys.platform != "win32", reason="Windows console semantics")
def test_STOP_001_real_console_ctrl_c_even_when_started_ignoring_it(tmp_path):
    """STOP-001: conhost/CREATE_NEW_PROCESS_GROUP start a process with Ctrl+C ignored.

    A real console Ctrl+C must still end in user_stop with stop.json.
    """
    import subprocess

    out = tmp_path / "콘솔 (Ctrl+C)"
    out.mkdir()
    child = subprocess.Popen(
        [sys.executable, "-c", CHILD, str(out)],
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW)
    try:
        deadline = time.monotonic() + 15
        while not list(out.glob("*/session.json")) and time.monotonic() < deadline:
            time.sleep(0.05)
        session_dir = next(out.glob("*/session.json")).parent
        pid = json.loads((session_dir / "session.json").read_text(encoding="utf-8"))["pid"]
        time.sleep(0.5)
        subprocess.run([sys.executable, "-c", SEND_CTRL_C, str(pid)], check=True, timeout=10,
                       creationflags=subprocess.DETACHED_PROCESS)
        child.wait(timeout=10)
    finally:
        if child.poll() is None:
            child.kill()
    stop = json.loads((session_dir / "stop.json").read_text(encoding="utf-8"))
    assert (stop["cause"], stop["certainty"], stop["trigger"]) == ("user_stop", "confirmed", "ctrl_c")



def test_review_S2_2b_console_class_marks_a_timetable_prefill(tmp_path, monkeypatch):
    """The console shortcut fills the course from the timetable: that is timetable_auto, not a user pick."""
    from lecturehub import cli
    from lecturehub.recorder import capture
    cli._register_all()
    everyday = [f"합성과목,합성 과목,{day},00:00,23:59" for day in "월화수목금토일"]
    config = _config(tmp_path, tmp_path / "rec", timetable=_timetable(tmp_path / "시간표.csv", everyday))
    monkeypatch.setattr(capture, "WasapiSource", _fake_factory([]))
    for extra, expected in (([], "timetable_auto"), (["--course", "구조역학"], "user_pick")):
        result = CliRunner().invoke(cli.app, ["recorder", "class", "--config", str(config), "--duration", "0.3",
                                              "--no-finalize", *extra])
        assert result.exit_code == 0, result.output
        newest = max((tmp_path / "rec").iterdir())
        assert json.loads((newest / "session.json").read_text(encoding="utf-8"))["course_source"] == expected
        import time as _time
        _time.sleep(1.1)  # session folders are named per second
