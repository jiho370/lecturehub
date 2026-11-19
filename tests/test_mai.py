"""Track C: MAI agent-assisted flow (§8.5, §8.6, MAI-N-001, AGENT-003) and the K1 one-line download format.

Synthetic text only; nothing is uploaded anywhere.
"""
import codecs
import json
from datetime import datetime
from pathlib import Path

import pytest

from lecturehub.core import edition
from lecturehub.parsers import detect_engine, parse_file
from lecturehub.parsers.mai import parse as parse_mai
from lecturehub.transcription import mai
from lecturehub.transcription.job import Deps, Job
from lecturehub.transcription.presets import load_preset

MIN = 60_000


# ---------------------------------------------------------------- K1 format
def test_K1_mai_one_line_download_is_time_speaker_body():
    text = "00:00 [S1] 합성 강의를 시작합니다.\n이어지는 줄입니다.\n12:34 [S2] 질문 있습니다.\n10:30 에 다시 모입니다.\n"
    segments = parse_mai(text)
    assert [(s.start_ms, s.speaker) for s in segments] == [(0, "S1"), (754_000, "S2")]
    assert segments[0].text == "합성 강의를 시작합니다.\n이어지는 줄입니다."
    assert segments[1].text == "질문 있습니다.\n10:30 에 다시 모입니다."  # K1: a bare time + words stays speech
    assert detect_engine(text.encode("utf-8")) == "mai"


def test_K1_classic_mai_layout_still_parses_the_same():
    text = "﻿00:00 \r\n[S1] 합성 첫 문단.\r\n\r\n08:10 \r\n[S1] 합성 둘째 문단.\r\n"
    data = text.encode("utf-8")
    assert detect_engine(data) == "mai"
    assert [(s.start_ms, s.speaker, s.text) for s in parse_mai(text)] == [
        (0, "S1", "합성 첫 문단."), (490_000, "S1", "합성 둘째 문단.")]


# ---------------------------------------------------------------- prepare / import
class Clock:
    def __call__(self):
        return datetime(2026, 9, 29, 21, 0).astimezone()


def deps(total_ms=75 * MIN):
    def fake_chunk(source, out, start, end, provider):
        assert provider == "mai_playground"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"ID3" + f"{start}-{end}".encode())
        return out

    return Deps(client_factory=lambda: None, confirm=lambda text: True, clock=Clock(),
                duration_ms=lambda path: total_ms, detect_silences=lambda path, lo, hi: [], make_chunk=fake_chunk)


@pytest.fixture
def semester(tmp_path):
    root = tmp_path / "대학" / "26-2 (합성)"
    (root / "녹음본" / "도시계").mkdir(parents=True)
    source = root / "녹음본" / "도시계" / "26.9.28 도시계.m4a"
    source.write_bytes(b"synthetic audio bytes")
    return root, source


def make_job(root, source, runs=2, confirm=lambda text: True):
    return mai.create(source, root, load_preset("mai"), course="도시계", runs=runs, deps=deps(), confirm=confirm)


def result_text(minutes, words=200):
    """A synthetic MAI result: one block per `minutes` entry, K1 one-line form."""
    return "\n".join(f"{m:02d}:{s:02d} [S1] " + " ".join(["합성 문장입니다."] * words) for m, s in minutes)


def test_mai_prepare_asks_consent_first_and_makes_nothing_without_it(semester):
    root, source = semester
    asked = []
    with pytest.raises(PermissionError):
        make_job(root, source, confirm=lambda text: asked.append(text) or False)
    assert "MAI playground" in asked[0] and "도시계" in asked[0]
    assert not (root / "_lecturehub" / "jobs_mai").exists()
    consent = json.loads((root / "_lecturehub" / "consent.json").read_text(encoding="utf-8")) \
        if (root / "_lecturehub" / "consent.json").exists() else {}
    assert "mai_playground" not in consent


def test_mai_prepare_writes_a_manifest_for_the_skill(semester):
    root, source = semester
    job = make_job(root, source)
    manifest = json.loads((job.dir / "manifest.json").read_text(encoding="utf-8"))
    assert job.dir.parent == root / "_lecturehub" / "jobs_mai" and job.state["state"] == "waiting_agent"
    assert manifest["consent"] == {"provider": "mai_playground", "course": "도시계", "authority": "confirmed_by_user"}
    assert [c["offset_ms"] for c in manifest["chunks"]] == [0, 25 * MIN, 50 * MIN]
    tag = job.id[-6:].lower()
    assert manifest["chunks"][1]["expected_results"] == [f"mai_{tag}_02_r1.txt", f"mai_{tag}_02_r2.txt"]
    assert all(Path(c["path"]).is_file() for c in manifest["chunks"])


def test_MAI_N_001_two_runs_become_two_outputs_with_offsets_and_raw_kept(semester, tmp_path):
    """MAI-N-001: each run is its own transcript; raw files are kept byte for byte (AGENT-003: files decide)."""
    root, source = semester
    job = make_job(root, source)
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    blocks = {0: [(0, 0), (9, 30)], 1: [(0, 5), (12, 0)], 2: [(0, 0), (16, 40)]}
    for index, minutes in blocks.items():
        for run in (1, 2):
            (downloads / mai.result_name(job, index, run)).write_text(result_text(minutes), encoding="utf-8")
    first = mai.import_results(job, deps(), [downloads])
    assert first["imported"] == 6 and first["state"] == "done"
    outputs = [Path(p) for p in job.state["outputs"]]
    assert [p.name for p in outputs] == ["26.9.28 도시계 (MAI).txt", "26.9.28 도시계 (MAI 2).txt"]
    parsed = parse_file(outputs[0])
    assert parsed.engine == "mai"
    assert [s.start_ms for s in parsed.segments] == [0, 9 * MIN + 30_000, 25 * MIN + 5_000, 37 * MIN,
                                                     50 * MIN, 66 * MIN + 40_000]
    assert outputs[0].read_bytes().startswith(codecs.BOM_UTF8)
    raw = job.dir / "raw" / "chunk_02_run1.txt"
    assert raw.read_bytes() == (downloads / mai.result_name(job, 1, 1)).read_bytes()
    again = mai.import_results(job, deps(), [downloads])  # idempotent: nothing new, nothing rewritten
    assert again["imported"] == 0 and job.state["outputs"] == [str(p) for p in outputs]


def test_mai_early_stop_is_flagged_by_comparing_runs(semester, tmp_path):
    """9/9: a run ended at 23 of 27 minutes. With two runs, the short one is flagged (coverage)."""
    root, source = semester
    job = make_job(root, source)
    inbox = job.dir / "inbox"
    inbox.mkdir()
    for index in range(3):
        (inbox / mai.result_name(job, index, 1)).write_text(result_text([(0, 0), (10, 0)]), encoding="utf-8")
        short = [(0, 0)] if index == 1 else [(0, 0), (10, 0)]
        (inbox / mai.result_name(job, index, 2)).write_text(result_text(short), encoding="utf-8")
    state = mai.import_results(job, deps())
    assert state["state"] == "qa_warning"
    chunk = job.state["chunks"][1]
    assert "coverage" in [q["check"] for q in chunk["results"]["2"]["qa"]] and chunk["results"]["1"]["qa"] == []


def test_mai_partial_runs_finish_only_on_request(semester, tmp_path):
    root, source = semester
    job = make_job(root, source)
    inbox = job.dir / "inbox"
    inbox.mkdir()
    for index in range(3):
        (inbox / mai.result_name(job, index, 1)).write_text(result_text([(0, 0)]), encoding="utf-8")
    assert mai.import_results(job, deps())["state"] == "waiting_agent"
    with pytest.raises(ValueError):
        mai.finish(job)
    state = mai.finish(job, allow_partial=True)
    assert state["runs_completed"] == [1] and len(state["outputs"]) == 1
    reopened = Job(job.dir, root)
    assert reopened.state["output"] == state["outputs"][0]


def test_cli_mai_prepare_refuses_a_second_job_for_the_same_recording(semester):
    from typer.testing import CliRunner

    from lecturehub import cli

    cli._register_all()
    root, source = semester
    job = make_job(root, source)
    result = CliRunner().invoke(cli.app, ["mai", "prepare", str(source), "--course", "도시계", "--root", str(root)])
    if edition.has("mai"):
        assert result.exit_code == 1 and job.id in result.output
    else:
        assert result.exit_code == 2 and "No such command" in result.output


def test_D064_one_run_early_stop_is_caught_against_other_engines(semester):
    """With one MAI run, a chunk with clearly less text than Whisper/Google in the same window is flagged."""
    from lecturehub.transcription.store import write_json

    root, source = semester
    job = make_job(root, source, runs=1)
    other = root / "_lecturehub" / "out" / "26.9.28 도시계 (Whisper).txt"
    other.parent.mkdir(parents=True, exist_ok=True)
    blocks = [f"{m:02d}:00 \r\n" + "가나다라마바사아자차 " * 90 + "\r\n" for m in range(0, 75, 5)]
    other.write_bytes(codecs.BOM_UTF8 + "\r\n".join(blocks).encode("utf-8"))
    write_json(root / "_lecturehub" / "jobs_whisper" / "w1" / "state.json",
               {"job_id": "w1", "provider": "whisper_groq", "state": "done", "audio": job.state["audio"],
                "audio_sha256": job.state["audio_sha256"], "output": str(other)})
    inbox = job.dir / "inbox"
    inbox.mkdir()
    for index in range(3):
        minutes = [(0, 0)] if index == 2 else [(0, 0), (10, 0)]
        (inbox / mai.result_name(job, index, 1)).write_text(result_text(minutes, words=400), encoding="utf-8")
    assert mai.import_results(job, deps())["state"] == "qa_warning"
    flagged = [i for i, c in enumerate(job.state["chunks"]) if any("다른 엔진(whisper)" in q["detail"] for q in c["qa"])]
    assert flagged == [2]


def test_D062_standing_consent_covers_named_courses_but_never_a_blank_one(tmp_path, monkeypatch):
    monkeypatch.setenv("LECTUREHUB_EDITION", "full")
    from lecturehub.transcription.consent import ConsentStore, ensure_consent

    store = ConsentStore(tmp_path / "consent.json")
    store.grant_all("mai_playground", 0, "user_chat", "D-062")
    assert store.granted("mai_playground", "토질역학") and store.granted("mai_playground", "AI 외부 강의")
    assert not store.granted("mai_playground", "") and not store.granted("whisper_groq", "토질역학")
    asked = []
    assert ensure_consent(store, "mai_playground", "구조역학", lambda text: asked.append(text) or False, 0)
    assert asked == []  # nothing asked for a named course
    assert not ensure_consent(store, "mai_playground", "", lambda text: asked.append(text) or False, 0)
    assert len(asked) == 1  # a blank course is still asked, per recording


def test_D062_cli_grant_all_only_for_a_user(tmp_path, monkeypatch):
    monkeypatch.setenv("LECTUREHUB_EDITION", "full")
    from typer.testing import CliRunner

    from lecturehub import cli

    cli._register_all()
    root = tmp_path / "합성 학기"
    bad = CliRunner().invoke(cli.app, ["transcribe", "consent", "grant-all", "--provider", "mai", "--by", "agent",
                                       "--reason", "x", "--root", str(root)])
    assert bad.exit_code == 1
    ok = CliRunner().invoke(cli.app, ["transcribe", "consent", "grant-all", "--provider", "mai", "--provider",
                                      "whisper", "--by", "user_chat", "--reason", "D-062", "--root", str(root)])
    assert ok.exit_code == 0, ok.output
    data = json.loads((root / "_lecturehub" / "consent.json").read_text(encoding="utf-8"))
    assert data["mai_playground"]["*"]["scope"] == "all_named_courses" and "*" in data["whisper_groq"]
