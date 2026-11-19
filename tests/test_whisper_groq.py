"""Track C: Whisper through Groq (D-061) — client, QA, job folder, registration as engine 'whisper'.

HTTP uses httpx.MockTransport and jobs use a fake client; no audio leaves the machine.
"""
import codecs
import json
import shutil
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from lecturehub.parsers import parse_file
from lecturehub.transcription.gemini import DailyQuotaExhausted, PermanentError, RateLimited, TransientError
from lecturehub.transcription.groq import GroqWhisperClient, classify, extra_qa, parse_result
from lecturehub.transcription.job import Deps, Job, run
from lecturehub.transcription.presets import load_preset
from lecturehub.transcription.providers import find_job_dir, job_roots

KEY = "gsk_SYNTHETIC_TEST_KEY"
MIN = 60_000


def verbose(segments):
    return {"task": "transcribe", "language": "korean", "text": "".join(s["text"] for s in segments),
            "segments": segments, "x_groq": {"id": "req_synthetic"}}


def seg(start, text, *, no_speech=0.01, logprob=-0.2, compression=1.3):
    return {"id": 0, "start": start, "end": start + 5, "text": text, "no_speech_prob": no_speech,
            "avg_logprob": logprob, "compression_ratio": compression}


def test_groq_request_shape_and_key_never_leaks(tmp_path):
    chunk = tmp_path / "조각 (합성) 01.mp3"
    chunk.write_bytes(b"ID3 synthetic")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["authorization"]
        seen["url"] = str(request.url)
        seen["body"] = request.content
        return httpx.Response(200, json=verbose([seg(0.0, " 합성 첫 구간입니다."), seg(65.2, " 합성 둘째 구간입니다.")]))

    client = GroqWhisperClient(KEY, http=httpx.Client(transport=httpx.MockTransport(handler)))
    result = client.transcribe(chunk)
    assert seen["url"].endswith("/openai/v1/audio/transcriptions") and seen["auth"] == f"Bearer {KEY}"
    for field in (b'name="model"', b"whisper-large-v3", b'name="language"', b"ko", b"verbose_json",
                  b"timestamp_granularities[]", b"segment"):
        assert field in seen["body"]
    assert result.text == "합성 첫 구간입니다. 합성 둘째 구간입니다."
    assert [w["start_offset"] for w in result.words] == ["0.000s", "65.200s"]

    def unauthorized(request):
        return httpx.Response(401, json={"error": {"message": "Invalid API Key"}})

    bad = GroqWhisperClient(KEY, http=httpx.Client(transport=httpx.MockTransport(unauthorized)))
    with pytest.raises(PermanentError) as error:
        bad.transcribe(chunk)
    assert KEY not in str(error.value)


@pytest.mark.parametrize(("status", "headers", "body", "expected", "wait"), [
    (429, {"retry-after": "12"}, "Rate limit reached ... requests per minute (RPM)", RateLimited, 12.0),
    (429, {}, "Rate limit reached on audio seconds per hour (ASH). Please try again in 7m32.5s.", RateLimited, 452.5),
    (429, {}, "Rate limit reached on audio seconds per day (ASD). Please try again in 1h2m3s.", RateLimited, 3723.0),
    (429, {}, "Rate limit reached for requests per day (RPD)", DailyQuotaExhausted, None),
    (503, {}, "unavailable", TransientError, None),
    (413, {}, "file too large", PermanentError, None),
])
def test_groq_429_and_errors_are_classified(status, headers, body, expected, wait):
    response = httpx.Response(status, headers=headers, text=body)
    with pytest.raises(expected) as error:
        classify(response)
    if wait is not None:
        assert error.value.retry_after_s == pytest.approx(wait)


def test_whisper_no_speech_segments_leave_the_text_but_stay_in_raw():
    raw = verbose([seg(0.0, " 합성 강의 첫 문장입니다."),
                   seg(30.0, " 시청해 주셔서 감사합니다.", no_speech=0.9, logprob=-1.4),   # silence: dropped
                   seg(60.0, " 시청해 주셔서 감사합니다."),                                # kept but flagged
                   seg(90.0, " 네 네 네 네 네 네 네 네", compression=3.1)])
    result = parse_result(raw)
    assert result.text.count("시청해 주셔서") == 1 and len(result.words) == 3
    assert len(raw["segments"]) == 4  # the raw response is never edited
    checks = {issue.check for issue in extra_qa(raw)}
    assert checks == {"no_speech_dropped", "hallucination", "tail_repetition"}


# ---------------------------------------------------------------- job
class FakeWhisper:
    def __init__(self, script=None):
        self.script, self.calls = script or {}, []

    def transcribe(self, path):
        name = Path(path).stem
        self.calls.append(name)
        queue = self.script.get(name)
        if queue:
            item = queue.pop(0)
            if isinstance(item, Exception):
                raise item
        return parse_result(verbose([seg(s, f" {name} 합성 Whisper 구간 {s}초 설명이 이어집니다.")
                                     for s in range(0, 1480, 20)]))


class Clock:
    def __call__(self):
        return datetime(2026, 9, 29, 20, 0).astimezone()


def deps(client, sleeps=None, total_ms=75 * MIN):
    def fake_chunk(source, out, start, end, provider):
        assert provider == "whisper_groq"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"ID3" + f"{start}-{end}".encode())
        return out

    return Deps(client_factory=lambda: client, confirm=lambda text: True, clock=Clock(),
                sleep=(sleeps.append if sleeps is not None else (lambda s: None)),
                duration_ms=lambda path: total_ms, detect_silences=lambda path, lo, hi: [], make_chunk=fake_chunk)


@pytest.fixture
def semester(tmp_path):
    root = tmp_path / "대학" / "26-2 (합성)"
    (root / "녹음본" / "도시계").mkdir(parents=True)
    source = root / "녹음본" / "도시계" / "26.9.28 도시계.m4a"
    source.write_bytes(b"synthetic audio bytes")
    return root, source


def whisper_job(root, source):
    return Job.create(source, root, "whisper_groq", load_preset("whisper"), course="도시계", now_ms=0)


def test_whisper_job_lives_apart_from_google_jobs_and_writes_a_whisper_output(semester):
    """A Whisper job never shows up in _lecturehub\\jobs (the app's Google 'already transcribing' check)."""
    root, source = semester
    original = source.read_bytes()
    client = FakeWhisper()
    job = whisper_job(root, source)
    state = run(job, deps(client))
    assert state["state"] == "done", state
    assert job.dir.parent == root / "_lecturehub" / "jobs_whisper"
    assert not (root / "_lecturehub" / "jobs").exists()
    assert client.calls == ["chunk_01", "chunk_02", "chunk_03"]  # 75 min → three ≤30-min chunks
    out = Path(state["output"])
    assert out.name == "26.9.28 도시계 (Whisper).txt"
    data = out.read_bytes()
    assert data.startswith(codecs.BOM_UTF8) and b"\r\n" in data
    parsed = parse_file(out)
    starts = [s.start_ms for s in parsed.segments]
    assert starts == sorted(starts) and state["chunks"][1]["start_ms"] in starts
    assert source.read_bytes() == original
    assert find_job_dir(root, job.id) == job.dir
    assert job_roots(root)[0] == root / "_lecturehub" / "jobs"


def test_whisper_long_provider_wait_parks_the_job_until_the_named_time(semester):
    """Groq hourly/daily audio limits name a long wait: the job waits for it (not a failure, no sleeping)."""
    root, source = semester
    client = FakeWhisper({"chunk_02": [RateLimited("ASD", 3723.0)]})
    sleeps = []
    state = run(whisper_job(root, source), deps(client, sleeps))
    assert state["state"] == "waiting_for_quota" and state["next_eligible_basis"] == "provider_retry_after"
    assert state["next_eligible_estimated"] is True
    assert int(Clock()().timestamp() * 1000) + 3_723_000 < state["next_eligible_at"]
    assert sleeps == [] and [c["state"] for c in state["chunks"]] == ["done", "queued", "queued"]


def test_whisper_short_wait_retries_in_place(semester):
    root, source = semester
    client = FakeWhisper({"chunk_01": [RateLimited("RPM", 12)]})
    sleeps = []
    state = run(whisper_job(root, source), deps(client, sleeps))
    assert state["state"] == "done" and sleeps == [17]


def test_whisper_needs_its_own_consent(semester):
    """CONSENT-001 for Groq: a Google consent for the course is not a Groq consent."""
    from lecturehub.transcription.consent import ConsentStore

    root, source = semester
    ConsentStore(root / "_lecturehub" / "consent.json").grant("gemini_transcribe", "도시계", 0, "google")
    client = FakeWhisper()
    asked = []
    job = whisper_job(root, source)
    d = deps(client)
    d.confirm = lambda text: asked.append(text) or False
    state = run(job, d)
    assert state["state"] == "waiting_user" and client.calls == []
    assert "Groq" in asked[0] and "도시계" in asked[0]


def test_cli_refuses_to_send_the_same_recording_to_whisper_twice(semester, monkeypatch):
    from typer.testing import CliRunner

    from lecturehub import cli
    from lecturehub.transcription import cli as tcli

    cli._register_all()
    root, source = semester
    job = whisper_job(root, source)
    job.state["state"] = "done"
    job.save()
    monkeypatch.setattr(tcli, "_deps", lambda *a, **k: pytest.fail("must not start a second job"))
    result = CliRunner().invoke(cli.app, ["transcribe", "run", str(source), "--provider", "whisper",
                                          "--course", "도시계", "--root", str(root)])
    assert result.exit_code == 1 and "이미 같은 엔진의 작업" in result.output


@pytest.mark.skipif(not shutil.which("ffprobe"), reason="ffmpeg required")
def test_cli_mock_groq_runs_offline_in_a_synthetic_library(tmp_path, monkeypatch):
    import subprocess

    from typer.testing import CliRunner

    from lecturehub import cli

    cli._register_all()
    root = tmp_path / "합성 학기"
    (root / "_lecturehub").mkdir(parents=True)
    (root / "_lecturehub" / "synthetic_root").write_text("synthetic", encoding="utf-8")
    source = root / "녹음 (합성).mp3"
    subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=70",
                    "-ac", "1", str(source)], check=True)
    monkeypatch.setenv("LECTUREHUB_FAKE_GROQ", "1")
    result = CliRunner().invoke(cli.app, ["transcribe", "run", str(source), "--provider", "whisper",
                                          "--course", "", "--root", str(root)], input="y\n")
    assert result.exit_code == 0, result.output
    out = next((root / "_lecturehub" / "out").glob("*(Whisper).txt"))
    assert "합성 모의 Whisper" in out.read_text(encoding="utf-8-sig")
    state = json.loads(next((root / "_lecturehub" / "jobs_whisper").glob("*/state.json")).read_text(encoding="utf-8"))
    assert state["mock"] is True


# ---------------------------------------------------------------- registration
@pytest.mark.skipif(not shutil.which("ffprobe"), reason="ffprobe required")
def test_whisper_output_registers_as_reference_and_never_outranks_google(tmp_path):
    from contextlib import closing

    from test_app_ingest import _job_output, make_session

    from lecturehub.core import db
    from lecturehub.index import app_ingest, lectures

    with closing(db.connect(tmp_path / "lecturehub.sqlite")) as connection:
        db.migrate(connection)
        connection.execute("INSERT INTO Course VALUES ('su','SU','구조역학','su','26-2')")
        connection.execute("INSERT INTO Schedule (course_id,weekday,start,end,id) VALUES ('su',1,?,?,'s1')",
                           (10 * 3_600_000, 12 * 3_600_000))
        linked = app_ingest.link_recording(connection, make_session(tmp_path, "구조역학", "2026-09-29T10:05:00+09:00"))
        google = {"job_id": "g", "state": "done", "audio": linked["master"], "provider": "gemini_transcribe",
                  "output": str(_job_output(tmp_path, "26.9.29 구조역학.txt"))}
        whisper_out = tmp_path / "out" / "26.9.29 구조역학 (Whisper).txt"
        whisper_out.write_bytes(codecs.BOM_UTF8 + "00:00 \r\n합성 Whisper 문단입니다.\r\n".encode())
        whisper = {"job_id": "w", "state": "qa_warning", "audio": "C:/elsewhere/copy.mp3",
                   "audio_sha256": connection.execute("SELECT sha256 FROM MediaAsset").fetchone()[0],
                   "provider": "whisper_groq", "output": str(whisper_out), "preset": {"model": "whisper-large-v3"},
                   "chunks": [{"qa": [{"check": "hallucination", "detail": "합성"}]}]}
        w = app_ingest.register_job_transcript(connection, whisper)  # found by content: the 녹음본 copy
        assert w["state"] == "registered" and w["engine"] == "whisper"
        g = app_ingest.register_job_transcript(connection, google)
        connection.commit()
        row = connection.execute("SELECT engine, role, engine_version FROM Transcript WHERE id=?",
                                 (w["transcript_id"],)).fetchone()
        assert tuple(row) == ("whisper", "reference", "whisper-large-v3")
        assert lectures.in_use(connection, linked["lecture_id"])["transcript_id"] == g["transcript_id"]
        labels = {t["label"] for t in lectures.lecture_detail(connection, linked["lecture_id"])["transcripts"]}
        assert labels == {"Google", "Whisper"}


def test_D063_whisper_may_start_during_a_recording_but_google_may_not(semester, monkeypatch):
    from typer.testing import CliRunner

    from lecturehub import cli
    from lecturehub.recorder.heartbeat import SessionLock
    from lecturehub.transcription import cli as tcli

    cli._register_all()
    root, source = semester
    started, lowered = [], []
    monkeypatch.setattr(tcli, "_lower_priority", lambda: lowered.append(True))

    def fake_run(job, deps):
        started.append(job.state["provider"])
        return {**job.state, "state": "done", "chunks": [], "output": None}

    monkeypatch.setattr("lecturehub.transcription.job.run", fake_run)
    live = root / "_lecturehub" / "recordings" / "20260929_140000_mic"
    live.mkdir(parents=True)
    with SessionLock(live):
        google = CliRunner().invoke(cli.app, ["transcribe", "run", str(source), "--root", str(root)])
        whisper = CliRunner().invoke(cli.app, ["transcribe", "run", str(source), "--provider", "whisper",
                                               "--course", "도시계", "--root", str(root)])
    assert google.exit_code == 1 and "녹음 중" in google.output
    assert whisper.exit_code == 0, whisper.output
    assert started == ["whisper_groq"] and lowered == [True]
