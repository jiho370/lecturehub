"""D-059 live transcription: parts are sent as 20-minute pieces fill; the recorder always wins."""
import json
import wave
from pathlib import Path

import pytest

from lecturehub.transcription import live
from lecturehub.transcription.gemini import parse_result
from lecturehub.transcription.job import Deps, Job
from lecturehub.transcription.presets import load_preset

RATE = 8000


def segment(folder: Path, n: int, seconds: float = 60) -> None:
    with wave.open(str(folder / f"segment_{n:06d}.wav"), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(b"\0\0" * int(RATE * seconds))


class Client:
    def __init__(self):
        self.calls = []

    def transcribe(self, path):
        self.calls.append(Path(path).name)
        raw = {"steps": [{"type": "model_output", "content": [{"type": "text", "text": "합성 문단입니다. 충분히 긴 합성 문장입니다.",
                                                               "annotations": [{"type": "word_info", "text": "합성",
                                                                                "start_offset": "1.000s",
                                                                                "start_index": 0}]}]}]}
        return parse_result(raw)


def stub_encoder(segments, out, provider):
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"ID3" + str(len(segments)).encode())
    return out


@pytest.fixture
def session(tmp_path):
    root = tmp_path / "학기(합성)"
    folder = root / "_lecturehub" / "recordings" / "20260929_100000_mic"
    folder.mkdir(parents=True)
    (folder / "session.json").write_text(json.dumps({"sample_rate": RATE}), encoding="utf-8")
    return root, folder


def test_parts_are_built_as_twenty_segments_complete_and_the_rest_at_stop(session):
    root, folder = session
    for n in range(1, 46):  # 45 one-minute segments; the newest one is still being written
        segment(folder, n)
    job = live.create(root, folder, "구조역학", dict(load_preset("gemini"), chunk_max_duration_s=1200), now_ms=0)
    assert live.add_ready_parts(job, final=False, encoder=stub_encoder) == 2
    assert [(c["start_ms"], c["end_ms"]) for c in job.state["chunks"]] == [(0, 1_200_000), (1_200_000, 2_400_000)]
    assert live.add_ready_parts(job, final=False, encoder=stub_encoder) == 0  # nothing new yet
    assert live.add_ready_parts(job, final=True, encoder=stub_encoder) == 1  # the last 5 minutes at stop
    assert job.state["chunks"][-1]["end_ms"] == 2_700_000 and job.state["live"]["segments_used"] == 45


def test_live_job_waits_for_recorder_pressure_then_completes_with_the_master(session):
    root, folder = session
    for n in range(1, 22):
        segment(folder, n, seconds=1)
    (folder / "stop.json").write_text("{}", encoding="utf-8")
    master = folder / "master_x.mp3"
    master.write_bytes(b"ID3 synthetic master")
    (folder / "finalize_x.json").write_text(json.dumps({"master": str(master)}), encoding="utf-8")
    job = live.create(root, folder, "구조역학", dict(load_preset("gemini"), chunk_max_duration_s=1200), now_ms=0)
    client, sleeps = Client(), []
    deps = Deps(client_factory=lambda: client, confirm=lambda text: True, sleep=sleeps.append)
    state = live.run_live(job, deps, encoder=stub_encoder, pressure=lambda s, b: None)
    assert state["state"] in ("done", "qa_warning") and state["audio"] == str(master) and state["audio_sha256"]
    assert client.calls == ["chunk_01.mp3", "chunk_02.mp3"]  # 20 segments, then the last one
    reloaded = Job(job.dir, root)
    assert reloaded.state["output"] and reloaded.state["live"]["closed"]


def test_pressure_holds_sending_while_recording(session):
    root, folder = session
    for n in range(1, 22):
        segment(folder, n, seconds=1)
    job = live.create(root, folder, "구조역학", dict(load_preset("gemini"), chunk_max_duration_s=1200), now_ms=0)
    client, sleeps = Client(), []
    calls = {"n": 0}

    def pressure(session_dir, baseline):
        calls["n"] += 1
        if calls["n"] == 1:
            return "녹음 대기열 300"
        (folder / "stop.json").write_text("{}", encoding="utf-8")  # the class ends
        master = folder / "master.mp3"
        master.write_bytes(b"ID3")
        (folder / "finalize_1.json").write_text(json.dumps({"master": str(master)}), encoding="utf-8")
        return None

    deps = Deps(client_factory=lambda: client, confirm=lambda text: True, sleep=sleeps.append)
    state = live.run_live(job, deps, encoder=stub_encoder, pressure=pressure)
    assert live.PRESSURE_WAIT_S in sleeps and state["live"]["pressure_waits"] == 1
    assert any(e["type"] == "live_pressure_wait" for e in state["events"])
    assert state["state"] in ("done", "qa_warning")


def test_recorder_pressure_reads_queue_and_new_drops(tmp_path):
    baseline = {}
    (tmp_path / "live.json").write_text(json.dumps({"state": "recording", "queue": 3, "dropped_samples": 0}),
                                        encoding="utf-8")
    assert live.recorder_pressure(tmp_path, baseline) is None
    (tmp_path / "live.json").write_text(json.dumps({"state": "recording", "queue": 400, "dropped_samples": 0}),
                                        encoding="utf-8")
    assert "대기열" in live.recorder_pressure(tmp_path, baseline)
    (tmp_path / "live.json").write_text(json.dumps({"state": "recording", "queue": 1, "dropped_samples": 96}),
                                        encoding="utf-8")
    assert "누락" in live.recorder_pressure(tmp_path, baseline)
    assert live.recorder_pressure(tmp_path, baseline) is None  # the same drops are not new


def test_slow_finalize_keeps_sent_parts_and_resume_attaches_the_master(session):
    """Review L1: a long class may finalize after the wait; nothing already sent is thrown away."""
    root, folder = session
    for n in range(1, 4):
        segment(folder, n, seconds=1)
    (folder / "stop.json").write_text("{}", encoding="utf-8")
    job = live.create(root, folder, "구조역학", dict(load_preset("gemini"), chunk_max_duration_s=1200), now_ms=0)
    client = Client()
    deps = Deps(client_factory=lambda: client, confirm=lambda text: True, sleep=lambda s: None)
    state = live.run_live(job, deps, encoder=stub_encoder, pressure=lambda s, b: None, master_wait_s=0)
    assert state["state"] == "waiting_master" and client.calls == ["chunk_01.mp3"]
    master = folder / "master.mp3"
    master.write_bytes(b"ID3 late master")
    (folder / "finalize_late.json").write_text(json.dumps({"master": str(master)}), encoding="utf-8")
    state = live.run_live(Job(job.dir, root), deps, encoder=stub_encoder, pressure=lambda s, b: None)
    assert state["state"] in ("done", "qa_warning") and state["audio"] == str(master)
    assert client.calls == ["chunk_01.mp3"]  # the part was not sent again


def test_discarded_recording_cancels_live_transcription(session):
    root, folder = session
    for n in range(1, 4):
        segment(folder, n, seconds=1)
    (folder / "stop.json").write_text(json.dumps({"cause": "user_discard"}), encoding="utf-8")
    job = live.create(root, folder, "구조역학", dict(load_preset("gemini"), chunk_max_duration_s=1200), now_ms=0)
    client = Client()
    deps = Deps(client_factory=lambda: client, confirm=lambda text: True, sleep=lambda s: None)
    state = live.run_live(job, deps, encoder=stub_encoder, pressure=lambda s, b: None)
    assert state["state"] == "cancelled" and client.calls == []  # nothing more is sent


def test_SEC_resume_checks_consent_before_sending_again(session, monkeypatch):
    """Security review 2026-11-05 (L2): consent withdrawn while a live job paused → resume sends nothing."""
    from typer.testing import CliRunner

    from lecturehub.transcription import cli
    from lecturehub.transcription.consent import ConsentStore

    root, folder = session
    for n in range(1, 4):
        segment(folder, n, seconds=1)
    job = live.create(root, folder, "구조역학", dict(load_preset("gemini"), chunk_max_duration_s=1200), now_ms=0)
    sent = []
    monkeypatch.setattr(live, "run_live", lambda job, deps, **k: sent.append(job.id) or job.state)
    monkeypatch.setattr(cli, "_deps", lambda *a, **k: Deps(client_factory=Client, confirm=lambda t: True,
                                                           sleep=lambda s: None))
    app = __import__("typer").Typer()
    cli.register(app)
    args = ["transcribe", "resume", job.id, "--root", str(root)]

    result = CliRunner().invoke(app, args)  # no consent.json: never granted
    assert sent == [] and result.exit_code == 1
    reloaded = Job(job.dir, root)
    assert reloaded.state["state"] == "waiting_user"

    ConsentStore(root / "_lecturehub" / "consent.json").grant(job.state["provider"], "구조역학", 0, "google")
    result = CliRunner().invoke(app, args)
    assert sent == [job.id], result.output
