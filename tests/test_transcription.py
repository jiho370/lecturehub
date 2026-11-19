"""Track C (C-1): chunking, Gemini client, quota, QA, consent and the one-click job.

API calls use httpx.MockTransport or a fake client; no real audio leaves the machine.
"""

import codecs
import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from lecturehub.core.timeutil import add_chunk_offset
from lecturehub.parsers import parse_file
from lecturehub.transcription import audio
from lecturehub.transcription.chunking import estimated_bytes, plan_chunks
from lecturehub.transcription.gemini import (
    DailyQuotaExhausted,
    GeminiClient,
    PermanentError,
    RateLimited,
    TranscribeResult,
    TransientError,
    classify,
    paragraphs,
    parse_result,
)
from lecturehub.transcription.job import Deps, Job, clock_text, run
from lecturehub.transcription.presets import load_preset
from lecturehub.transcription.qa import check_chunk
from lecturehub.transcription.quota import PACIFIC, QuotaLedger, QuotaWait

MIN = 60_000


# ---------------------------------------------------------------- chunk plans
def test_SPLIT_001_68_minutes_mai_preset():
    """SPLIT-001: 68 min → MAI preset: chunks ≤ 30 min and ≤ 10 MB, offsets cover the whole length."""
    preset = load_preset("mai")
    total = 68 * MIN + 1234
    plan = plan_chunks(total, preset["chunk_max_duration_s"] * 1000, 30_000)
    assert len(plan.chunks) == 3
    assert all(c.duration_ms <= 30 * MIN for c in plan.chunks)
    assert all(estimated_bytes(c.duration_ms, 40_000) <= preset["max_file_bytes"] for c in plan.chunks)
    assert plan.chunks[0].start_ms == 0 and plan.chunks[-1].end_ms == total
    assert all(a.end_ms == b.start_ms for a, b in zip(plan.chunks, plan.chunks[1:], strict=False))
    assert sum(c.duration_ms for c in plan.chunks) == total


def test_split_prefers_longest_silence_but_never_exceeds_max():
    # 40 min into 20-min chunks leaves no slack: the nominal cut is the only legal one.
    tight = plan_chunks(40 * MIN, 20 * MIN, 30_000, lambda lo, hi: [(19 * MIN + 40_000, 19 * MIN + 41_000)])
    assert tight.boundaries[0].method == "nominal" and [c.duration_ms for c in tight.chunks] == [20 * MIN] * 2
    # 50 min → 3 chunks, nominal cut at 16:40; the longest silence inside ±30 s wins.
    silences = [(16 * MIN + 20_000, 16 * MIN + 22_000), (16 * MIN + 55_000, 17 * MIN), (18 * MIN, 18 * MIN + 30_000)]
    plan = plan_chunks(50 * MIN, 20 * MIN, 30_000,
                       lambda lo, hi: [s for s in silences if s[1] > lo and s[0] < hi])
    assert plan.boundaries[0].method == "silence" and plan.chunks[1].start_ms == 16 * MIN + 57_500
    assert max(c.duration_ms for c in plan.chunks) <= 20 * MIN
    assert sum(c.duration_ms for c in plan.chunks) == 50 * MIN


def test_TIME_002_silence_boundary_offsets_are_exact_ms():
    """TIME-002: a non-integer-minute silence boundary is used as the exact offset."""
    silence = (19 * MIN + 50_250, 19 * MIN + 51_750)
    plan = plan_chunks(40 * MIN, 21 * MIN, 30_000, lambda lo, hi: [silence])
    cut = plan.chunks[1].start_ms
    assert cut == 19 * MIN + 51_000 and plan.boundaries[0].actual_ms == cut
    assert add_chunk_offset(5_123, cut) == 19 * MIN + 56_123
    assert clock_text(add_chunk_offset(5_123, cut)) == "19:56"  # floor, never rounded


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")
def test_TIME_002_real_ffmpeg_silence_detection_and_cut(tmp_path):
    source = tmp_path / "합성 (무음 7.25~8.75s).wav"
    subprocess.run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=f=440:d=7.25", "-f", "lavfi",
        "-i", "anullsrc=r=44100:cl=mono:d=1.5", "-f", "lavfi", "-i", "sine=f=440:d=6",
        "-filter_complex", "[0][1][2]concat=n=3:v=0:a=1", "-ar", "44100", "-ac", "1", str(source),
    ], check=True)
    total = audio.duration_ms(source)
    assert abs(total - 14_750) <= 30
    found = audio.detect_silences(source, 5_000, 11_000)
    assert len(found) == 1 and abs(found[0][0] - 7_250) <= 60 and abs(found[0][1] - 8_750) <= 60
    plan = plan_chunks(total, 9_000, 3_000, lambda lo, hi: audio.detect_silences(source, lo, hi))
    assert plan.boundaries[0].method == "silence"
    assert 7_250 <= plan.chunks[1].start_ms <= 8_750
    chunk = audio.make_chunk(source, tmp_path / "out" / "chunk.mp3", plan.chunks[1].start_ms,
                             plan.chunks[1].end_ms, "gemini_transcribe")
    assert abs(audio.duration_ms(chunk) - plan.chunks[1].duration_ms) <= 80
    assert source.stat().st_size > 0  # source untouched


# ---------------------------------------------------------------- Gemini client
def _interaction(text, words):
    """Real response shape (2026-09-28 dry run): steps[].content is a list of parts."""
    return {"object": "interaction", "status": "completed", "steps": [
        {"type": "model_output", "content": [{"type": "text", "text": text, "annotations": words}]}]}


def test_gemini_client_flow_matches_transcribe_ps1(tmp_path):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        url = str(request.url)
        assert request.headers.get("x-goog-api-key", "SECRET") == "SECRET" or "upload-session" in url
        if url.endswith("/upload/v1beta/files"):
            assert request.headers["x-goog-upload-command"] == "start"
            return httpx.Response(200, headers={"X-Goog-Upload-URL": "https://up.example/upload-session"})
        if "upload-session" in url:
            assert request.headers["x-goog-upload-command"] == "upload, finalize"
            return httpx.Response(200, json={"file": {"name": "files/abc", "uri": "https://f/abc",
                                                      "state": "PROCESSING"}})
        if url.endswith("/v1beta/files/abc") and request.method == "GET":
            return httpx.Response(200, json={"name": "files/abc", "uri": "https://f/abc", "state": "ACTIVE"})
        if url.endswith("/v1beta/interactions"):
            body = json.loads(request.content)
            assert body["model"] == "gemini-3.5-transcribe"
            config = body["generation_config"]["transcription_config"]
            assert config == {"language_codes": ["ko-KR"],
                              "mode": {"type": "verbatim", "timestamp_granularities": ["word"]}}
            assert body["input"][0]["mime_type"] == "audio/mpeg"
            text = "첫 문장입니다. 둘째 문장입니다."
            second = len("첫 문장입니다. ".encode())
            return httpx.Response(200, json=_interaction(text, [
                {"type": "word_info", "text": "첫", "start_offset": "0.5s", "start_index": 0},
                {"type": "word_info", "text": "문장입니다.", "start_offset": "61.2s", "start_index": 4},
                {"type": "word_info", "text": "둘째", "start_offset": "62.0s", "start_index": second},
            ]))
        if request.method == "DELETE":
            return httpx.Response(200, json={})
        raise AssertionError(url)

    chunk = tmp_path / "chunk.mp3"
    chunk.write_bytes(b"ID3fake")
    client = GeminiClient("SECRET", http=httpx.Client(transport=httpx.MockTransport(handler)),
                          sleep=lambda s: None)
    result = client.transcribe(chunk)
    assert [r.method for r in seen] == ["POST", "POST", "GET", "POST", "DELETE"]
    assert paragraphs(result) == [(500, "첫 문장입니다."), (62_000, "둘째 문장입니다.")]


def test_429_and_error_classification_never_leaks_key():
    minute = httpx.Response(429, text='{"error":{"message":"Quota exceeded per minute. Please retry in 12.5s."}}')
    with pytest.raises(RateLimited) as caught:
        classify(minute)
    assert caught.value.retry_after_s == 12.5
    delay = httpx.Response(429, text='quota metric ...PerMinute... "retryDelay": "37s"')
    with pytest.raises(RateLimited) as caught:
        classify(delay)
    assert caught.value.retry_after_s == 37
    with pytest.raises(DailyQuotaExhausted):
        classify(httpx.Response(429, text="Quota exceeded for requests per day"))
    with pytest.raises(TransientError):
        classify(httpx.Response(503, text="unavailable"))
    with pytest.raises(PermanentError) as caught:
        classify(httpx.Response(400, text="API key not valid"))
    assert "SECRET" not in str(caught.value)


def test_parse_result_accepts_list_and_dict_content():
    words = [{"type": "word_info", "text": "합성", "start_offset": "0.100s", "start_index": 0}]
    as_list = parse_result(_interaction("합성 결과입니다.", words))
    as_dict = parse_result({"steps": [{"content": {"text": "합성 결과입니다.", "annotations": words}}]})
    assert as_list.text == as_dict.text == "합성 결과입니다." and as_list.words == words


def test_paragraphs_without_words_and_empty():
    assert paragraphs(parse_result(_interaction("단어 시각 없는 합성 결과입니다.", []))) == [
        (0, "단어 시각 없는 합성 결과입니다.")]
    assert paragraphs(parse_result({"steps": []})) == []


# ---------------------------------------------------------------- QA
def _paras(starts_s, repeat_tail=False):
    body = [(int(s * 1000), " ".join(f"합성 문단 {i}번의 {j}번째 문장은 충분히 깁니다." for j in range(8)))
            for i, s in enumerate(starts_s)]
    if repeat_tail:
        body += [(int(starts_s[-1] * 1000) + 1000, "같은 문장이 되풀이됩니다. 또 같은 말입니다.\n" * 5)]
    return body


def test_TQA_001_003_and_empty_chunk():
    healthy = _paras(range(0, 1200, 60))
    assert check_chunk(healthy, 20 * MIN) == []
    assert [i.check for i in check_chunk(_paras(range(0, 900, 60)), 20 * MIN)] == ["coverage"]  # TQA-001
    broken = _paras([47, 1010, 2020, 30, 60])  # 00:47 / 16:50 / 33:40 …
    assert "monotonic" in [i.check for i in check_chunk(broken, 20 * MIN)]  # TQA-002
    assert "tail_repetition" in [i.check for i in check_chunk(_paras(range(0, 1200, 60), True), 20 * MIN)]
    assert [i.check for i in check_chunk([(0, "짧음")], 20 * MIN)] == ["empty_chunk"]


# ---------------------------------------------------------------- job
def result_at(starts_s, tag="합성"):
    sentences = [f"{tag} 강의의 {i}번째 합성 문단은 전사 결과처럼 보이도록 충분히 길게 이어지는 설명을 담고 있습니다. "
                 f"같은 문단 안에서 {i}번 예제를 풀어 보는 흐름도 함께 적어 둡니다. "
                 f"마지막으로 {i}번 내용을 다시 정리하면서 다음 주제로 넘어가는 말을 덧붙입니다." for i in range(len(starts_s))]
    text = " ".join(sentences)
    words, index = [], 0
    for sentence, start in zip(sentences, starts_s, strict=True):
        words.append({"type": "word_info", "text": sentence.split()[-1], "start_offset": f"{start}s",
                      "start_index": index})
        index += len((sentence + " ").encode("utf-8"))
    for w, sentence in zip(words, sentences, strict=True):  # first word of each sentence carries its offset
        w["text"] = sentence  # ends with '.' → a sentence end for the 60 s rule
    return TranscribeResult(text, words, {"steps": [{"content": {"text": text}}], "tag": tag})


GOOD = list(range(0, 1000, 70))  # fits a 16:40 chunk (50 min / 3)


class FakeClient:
    def __init__(self, script=None, default=None):
        self.script = script or {}
        self.default = default or (lambda name: result_at(GOOD, name))
        self.calls = []

    def transcribe(self, path):
        name = Path(path).stem
        self.calls.append(name)
        queue = self.script.get(name)
        item = queue.pop(0) if queue else self.default(name)
        if isinstance(item, Exception):
            raise item
        return item


KST = timezone(timedelta(hours=9))  # the app's day is Korean time, whatever TZ the test machine runs in


class Clock:
    def __init__(self, at=datetime(2026, 9, 29, 13, 0)):
        # A naive test time means KST (TZ-001: tests pass under TZ=UTC too).
        self.at = at.replace(tzinfo=KST) if at.tzinfo is None else at

    def __call__(self):
        return self.at


def make_deps(client, *, confirm=lambda text: True, clock=None, sleeps=None, total_ms=50 * MIN):
    def fake_chunk(source, out, start, end, provider):
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"ID3" + f"{start}-{end}".encode())
        return out

    return Deps(client_factory=lambda: client, confirm=confirm, clock=clock or Clock(),
                sleep=(sleeps.append if sleeps is not None else (lambda s: None)),
                duration_ms=lambda path: total_ms, detect_silences=lambda path, lo, hi: [],
                make_chunk=fake_chunk)


@pytest.fixture
def semester(tmp_path):
    root = tmp_path / "대학" / "26-2 (합성)"
    (root / "녹음본" / "구조역학").mkdir(parents=True)
    source = root / "녹음본" / "구조역학" / "26.9.29 구조역학.m4a"
    source.write_bytes(b"synthetic audio bytes")
    return root, source


def new_job(root, source, preset=None, **kwargs):
    preset = preset or dict(load_preset("gemini"), chunk_max_duration_s=1200)  # these tests plan 20-minute chunks
    return Job.create(source, root, "gemini_transcribe", preset, course="구조역학", now_ms=0, **kwargs)


def test_AUTO_STT_001_one_click_to_compatible_transcript(semester):
    """AUTO-STT-001: preset → chunks → API → merge, with no user file handling."""
    root, source = semester
    before = sorted(p.name for p in root.iterdir())
    original = source.read_bytes()
    client = FakeClient()
    state = run(new_job(root, source), make_deps(client))
    assert state["state"] == "done"
    assert client.calls == ["chunk_01", "chunk_02", "chunk_03"]
    out = Path(state["output"])
    assert out.parent == root / "_lecturehub" / "out" and out.name == "26.9.29 구조역학.txt"
    data = out.read_bytes()
    assert data.startswith(codecs.BOM_UTF8) and b"\r\n" in data and b"\n" not in data.replace(b"\r\n", b"")
    parsed = parse_file(out)
    assert parsed.engine == "gemini_transcribe"
    starts = [s.start_ms for s in parsed.segments]
    assert starts == sorted(starts) and starts[0] == 0
    boundary = state["chunks"][1]["start_ms"]
    assert boundary in starts  # chunk 2's first paragraph sits exactly at its measured offset
    assert sorted(p.name for p in root.iterdir()) == sorted({*before, "_lecturehub"})
    assert source.read_bytes() == original
    assert all(Path(p).exists() for c in state["chunks"] for p in c["raw_outputs"])


def test_AUTO_STT_002_only_the_failed_chunk_is_retried(semester):
    """AUTO-STT-002: one chunk keeps failing → only it is retried; resume re-sends only it."""
    root, source = semester
    flaky = [TransientError("503"), TransientError("503"), TransientError("503")]
    client = FakeClient({"chunk_02": flaky})
    sleeps = []
    job = new_job(root, source)
    state = run(job, make_deps(client, sleeps=sleeps))
    assert state["state"] == "failed"
    assert client.calls == ["chunk_01", "chunk_02", "chunk_02", "chunk_02", "chunk_03"]
    assert [c["state"] for c in state["chunks"]] == ["done", "failed", "done"]
    assert sleeps == [15, 30]  # two automatic retries with backoff, then failed
    healthy = FakeClient()
    state = run(Job(job.dir, root), make_deps(healthy))
    assert healthy.calls == ["chunk_02"] and state["state"] == "done"


def test_transient_error_recovers_within_two_retries(semester):
    root, source = semester
    client = FakeClient({"chunk_01": [TransientError("timeout"), TransientError("503")]})
    state = run(new_job(root, source), make_deps(client))
    assert state["state"] == "done" and state["metrics"]["auto_retries"] == 2


def test_QUOTA_001_AUTO_STT_003_waits_for_quota_without_losing_work(semester):
    """QUOTA-001/AUTO-STT-003: the 26th request waits; resuming the next day finishes the job."""
    root, source = semester
    clock = Clock()
    ledger = QuotaLedger(root / "_lecturehub" / "quota.json", "gemini_transcribe", 25, clock)
    ledger.record(24)
    client = FakeClient()
    job = new_job(root, source)
    state = run(job, make_deps(client, clock=clock))
    assert state["state"] == "waiting_for_quota" and client.calls == ["chunk_01"]
    assert [c["state"] for c in state["chunks"]] == ["done", "queued", "queued"]
    # D-054: the provider day ends at midnight Pacific time (13:00 KST 9/29 = 21:00 PDT 9/28).
    assert state["next_eligible_basis"] == "provider_pt_midnight" and state["next_eligible_estimated"] is False
    pacific_midnight = datetime(2026, 9, 29, 0, 0, tzinfo=PACIFIC)
    assert state["next_eligible_at"] == int(pacific_midnight.timestamp() * 1000)
    assert run(Job(job.dir, root), make_deps(client, clock=clock))["state"] == "waiting_for_quota"
    assert client.calls == ["chunk_01"]  # still no request beyond the limit
    clock.at += timedelta(days=1)
    state = run(Job(job.dir, root), make_deps(client, clock=clock))
    assert state["state"] == "done" and client.calls == ["chunk_01", "chunk_02", "chunk_03"]
    assert state["chunks"][0]["requests"] == 1  # finished work was not re-sent


def test_provider_daily_429_marks_ledger_and_waits(semester):
    root, source = semester
    client = FakeClient({"chunk_02": [DailyQuotaExhausted("per day")]})
    clock = Clock()
    state = run(new_job(root, source), make_deps(client, clock=clock))
    assert state["state"] == "waiting_for_quota"
    with pytest.raises(QuotaWait):
        QuotaLedger(root / "_lecturehub" / "quota.json", "gemini_transcribe", 25, clock).check()


def test_per_minute_429_waits_retry_delay_and_is_not_charged(semester):
    root, source = semester
    client = FakeClient({"chunk_01": [RateLimited("per minute", 12.0)]})
    sleeps = []
    clock = Clock()
    state = run(new_job(root, source), make_deps(client, sleeps=sleeps, clock=clock))
    assert state["state"] == "done" and sleeps == [17.0]
    assert QuotaLedger(root / "_lecturehub" / "quota.json", "gemini_transcribe", 25, clock).used() == 3


def test_AUTO_STT_004_monotonic_failure_reruns_once_then_warns(semester):
    """AUTO-STT-004: timestamp collapse → one automatic rerun, then qa_warning."""
    root, source = semester
    collapsed = result_at([47, 1010, 2020, 30, 60, 90, 120], "붕괴")
    client = FakeClient({"chunk_01": [collapsed, result_at(GOOD)]})
    state = run(new_job(root, source), make_deps(client))
    assert state["state"] == "done" and state["chunks"][0]["qa_reruns"] == 1
    assert len(state["chunks"][0]["raw_outputs"]) == 2  # the rejected output is kept too
    root2 = root.parent / "두번째"
    (root2 / "녹음본" / "구조역학").mkdir(parents=True)
    source2 = root2 / "녹음본" / "구조역학" / source.name
    source2.write_bytes(b"x")
    always = FakeClient({"chunk_01": [collapsed, collapsed]})
    state = run(new_job(root2, source2), make_deps(always))
    assert always.calls.count("chunk_01") == 2
    assert state["chunks"][0]["state"] == "qa_warning" and state["state"] == "qa_warning"
    assert Path(state["output"]).exists()


def test_CONSENT_001_no_upload_before_consent(semester):
    """CONSENT-001: until the notice is accepted, zero uploads happen."""
    root, source = semester
    factory_calls = []
    client = FakeClient()
    deps = make_deps(client, confirm=lambda text: False)
    deps.client_factory = lambda: factory_calls.append(1) or client
    notices = []
    deps.confirm = lambda text: notices.append(text) or False
    job = new_job(root, source)
    state = run(job, deps)
    assert state["state"] == "waiting_user" and factory_calls == [] and client.calls == []
    assert "Google" in notices[0] and "구조역학" in notices[0]
    assert not (root / "_lecturehub" / "consent.json").exists() or "구조역학" not in (
        root / "_lecturehub" / "consent.json").read_text(encoding="utf-8")
    deps.confirm = lambda text: True
    assert run(Job(job.dir, root), deps)["state"] == "done"
    asked = []
    deps.confirm = lambda text: asked.append(text) or True
    run(new_job(root, source), deps)
    assert asked == []  # once per provider × course


def test_dry_run_is_one_minute_single_chunk(semester):
    root, source = semester
    client = FakeClient(default=lambda name: result_at([0, 30]))
    state = run(new_job(root, source, dry_run_1min=True), make_deps(client))
    assert [(c["start_ms"], c["end_ms"]) for c in state["chunks"]] == [(0, 60_000)]
    assert Path(state["output"]).name == "26.9.29 구조역학 (dryrun-1min).txt"


def test_writes_are_confined_to_lecturehub(semester):
    from lecturehub.transcription.store import ensure_inside
    root, _ = semester
    with pytest.raises(ValueError):
        ensure_inside(root / "전사본" / "구조역학" / "x.txt", root)
    assert ensure_inside(root / "_lecturehub" / "out" / "x.txt", root)


def test_transcribe_refuses_while_recording(semester, monkeypatch):
    from typer.testing import CliRunner

    from lecturehub import cli
    from lecturehub.recorder.heartbeat import SessionLock
    cli._register_all()
    root, source = semester
    live = root / "_lecturehub" / "recordings" / "20260929_100500_mic"
    live.mkdir(parents=True)
    with SessionLock(live):
        result = CliRunner().invoke(cli.app, ["transcribe", "run", str(source), "--root", str(root)])
    assert result.exit_code == 1 and "녹음 중" in result.output


def test_reparse_rebuilds_from_raw_without_api_calls(semester):
    """Tonight's incident: a parser bug emptied a good response; reparse fixes it with no request."""
    root, source = semester
    good = result_at(GOOD)
    real_raw = _interaction(good.text, good.words)
    lost = TranscribeResult("", [], real_raw)  # what the list-shape bug produced
    client = FakeClient({"chunk_01": [lost], "chunk_02": [lost], "chunk_03": [lost]})
    job = new_job(root, source)
    state = run(job, make_deps(client))
    assert state["state"] == "qa_warning" and {c["qa"][0]["check"] for c in state["chunks"]} == {"empty_chunk"}
    from lecturehub.transcription.job import reparse
    calls = len(client.calls)
    state = reparse(Job(job.dir, root), make_deps(client))
    assert len(client.calls) == calls and state["state"] == "done"
    assert Path(state["output"]).name == "26.9.29 구조역학 (2).txt"
    assert parse_file(Path(state["output"])).engine == "gemini_transcribe"


def test_D052_blank_course_consent_is_never_stored_or_honoured(tmp_path):
    """R2/D-052: a yes for a recording with no course applies to that job only."""
    import json as _json

    from lecturehub.transcription.consent import ConsentStore, ensure_consent

    path = tmp_path / "consent.json"
    path.write_text(_json.dumps({"gemini_transcribe": {"": {"granted_by": "user_app_dialog"}}}), encoding="utf-8")
    store = ConsentStore(path)
    assert not store.granted("gemini_transcribe", "")  # the legacy blank entry grants nothing
    with pytest.raises(ValueError):
        store.grant("gemini_transcribe", "", 0, "notice")
    asked = []
    assert ensure_consent(store, "gemini_transcribe", "", lambda text: asked.append(text) or True, 0)
    assert ensure_consent(store, "gemini_transcribe", "", lambda text: asked.append(text) or True, 0)
    assert len(asked) == 2 and "이번 녹음에만" in asked[0]  # asked every time


def test_D052_synthetic_consent_never_counts_for_a_real_course(tmp_path):
    from lecturehub.transcription.consent import SYNTHETIC, ConsentStore

    store = ConsentStore(tmp_path / "consent.json")
    store.grant("gemini_transcribe", "합성 드라이런", 0, "notice", by="agent", authority=SYNTHETIC)
    assert not store.granted("gemini_transcribe", "합성 드라이런")
    assert store.granted("gemini_transcribe", "합성 드라이런", allow_synthetic=True)


def test_D052_audit_annotates_legacy_entries_without_deleting(tmp_path):
    import json as _json

    from lecturehub.transcription.consent import ConsentStore

    path = tmp_path / "consent.json"
    legacy = {"gemini_transcribe": {"합성 드라이런": {"granted_by": "user", "granted_at": 1, "notice": "n"},
                                    "": {"granted_by": "user_app_dialog", "granted_at": 2, "notice": "n"},
                                    "구조역학": {"granted_by": "user_app_dialog", "granted_at": 3, "notice": "n"}}}
    path.write_text(_json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
    store = ConsentStore(path)
    assert store.granted("gemini_transcribe", "합성 드라이런")  # before the audit (legacy judgement)
    changes = store.audit(10, apply=True)
    assert {(c["course"], c["authority"]) for c in changes} == {("합성 드라이런", "synthetic_test"), ("", "invalid")}
    after = _json.loads(path.read_text(encoding="utf-8"))["gemini_transcribe"]
    assert set(after) == {"합성 드라이런", "", "구조역학"} and after["합성 드라이런"]["granted_at"] == 1
    assert not store.granted("gemini_transcribe", "합성 드라이런") and store.granted("gemini_transcribe", "구조역학")


def test_consent_once_job_uploads_without_storing_consent(semester):
    root, source = semester
    asked = []
    client = FakeClient()
    job = Job.create(source, root, "gemini_transcribe", dict(load_preset("gemini"), chunk_max_duration_s=1200), course="", now_ms=0,
                     consent_once=True)
    state = run(job, make_deps(client, confirm=lambda text: asked.append(text) or False))
    assert state["state"] == "done" and asked == [] and client.calls
    assert any(e["type"] == "consent_once" for e in state["events"])
    consent = root / "_lecturehub" / "consent.json"
    assert not consent.exists() or '""' not in consent.read_text(encoding="utf-8")



def test_D054_provider_day_is_pacific_and_personal_budget_is_separate(tmp_path):
    clock = Clock(datetime(2026, 9, 29, 15, 30))  # 15:30 KST = 23:30 PDT (9/28)
    ledger = QuotaLedger(tmp_path / "quota.json", "gemini_transcribe", 25, 3, clock)
    ledger.record(3)
    with pytest.raises(QuotaWait) as caught:
        ledger.check()  # the personal budget (3/day, local) is used up first
    assert caught.value.basis == "personal_local_midnight" and caught.value.estimated
    clock.at += timedelta(hours=1)  # 16:30 KST = 00:30 PDT: new provider day, same local day
    assert ledger.used() == 0 and ledger.used_personal() == 3
    with pytest.raises(QuotaWait):
        ledger.check()
    ledger.mark_exhausted()
    clock.at += timedelta(hours=8)  # next local day (00:30 KST 9/30), same Pacific day (08:30 PDT 9/29)
    with pytest.raises(QuotaWait) as caught:
        ledger.check()
    assert caught.value.basis == "provider_pt_midnight"


def test_D054_v1_ledger_counts_as_personal(tmp_path):
    import json as _json

    path = tmp_path / "quota.json"
    path.write_text(_json.dumps({"gemini_transcribe": {"2026-09-29": 2}}), encoding="utf-8")
    ledger = QuotaLedger(path, "gemini_transcribe", 25, clock=Clock(datetime(2026, 9, 29, 12, 0)))
    assert ledger.used_personal() == 2 and ledger.used() == 0
    ledger.record()
    assert _json.loads(path.read_text(encoding="utf-8"))["version"] == 2


def test_D054_summary_shows_what_is_left_without_writing(tmp_path):
    from lecturehub.transcription.quota import summary

    path = tmp_path / "quota.json"
    clock = Clock(datetime(2026, 9, 29, 15, 30))
    QuotaLedger(path, "gemini_transcribe", 25, 10, clock).record(4)
    before = path.read_bytes()
    info = summary(path, "gemini_transcribe", 25, 10, clock)
    assert (info["limit"], info["used"], info["used_personal"], info["remaining"]) == (25, 4, 4, 6)
    assert info["provider_reset_at"] < info["personal_reset_at"]  # Pacific midnight = 16:00 KST, before local midnight
    assert path.read_bytes() == before
    assert summary(tmp_path / "none.json", "gemini_transcribe", 25, clock=clock)["remaining"] == 25


def test_L002_short_token_loop_is_caught():
    """2026-09-29 chunk 5: "음. 음. …" 4,732 chars passed QA; short sentences are skipped by the sentence check."""
    looped = _paras(range(0, 1080, 60)) + [(1_080_000, "음. " * 1500)]
    issues = check_chunk(looped, 20 * MIN)
    assert [i.check for i in issues] == ["tail_repetition"] and "'음.'" in issues[0].detail
    normal = _paras(range(0, 1200, 60)) + [(1_190_000, "네. 네. 네. 좋아요.")]  # a few repeats are normal speech
    assert check_chunk(normal, 20 * MIN) == []


def test_cancelled_job_is_never_sent_again(semester):
    """9/29: a duplicate job was resumed by the app. `transcribe cancel` makes a job permanently idle."""
    from typer.testing import CliRunner

    from lecturehub import cli

    root, source = semester
    clock = Clock()
    QuotaLedger(root / "_lecturehub" / "quota.json", "gemini_transcribe", 25, clock).record(24)
    client = FakeClient()
    job = new_job(root, source)
    assert run(job, make_deps(client, clock=clock))["state"] == "waiting_for_quota"
    cli._register_all()
    result = CliRunner().invoke(cli.app, ["transcribe", "cancel", job.dir.name, "--reason", "중복(합성)",
                                          "--root", str(root)])
    assert result.exit_code == 0, result.output
    clock.at += timedelta(days=1)
    state = run(Job(job.dir, root), make_deps(client, clock=clock))
    assert state["state"] == "cancelled" and client.calls == ["chunk_01"]  # nothing more was sent
    assert [c["state"] for c in state["chunks"]] == ["done", "queued", "queued"]  # finished work kept


def test_resume_refuses_google_job_while_recording(semester, monkeypatch):
    """Audit #1 (11/5): `transcribe resume` lacked the recording check that `run` has (AGENTS §2.7)."""
    from typer.testing import CliRunner

    from lecturehub import cli
    from lecturehub.recorder.heartbeat import SessionLock

    root, source = semester
    clock = Clock()
    QuotaLedger(root / "_lecturehub" / "quota.json", "gemini_transcribe", 25, clock).record(24)
    client = FakeClient()
    job = new_job(root, source)
    assert run(job, make_deps(client, clock=clock))["state"] == "waiting_for_quota"
    cli._register_all()
    live = root / "_lecturehub" / "recordings" / "20261105_100500_mic"
    live.mkdir(parents=True)
    with SessionLock(live):
        result = CliRunner().invoke(cli.app, ["transcribe", "resume", job.dir.name, "--root", str(root)])
    assert result.exit_code == 1 and "녹음 중" in result.output
    assert client.calls == ["chunk_01"]  # nothing more was sent
