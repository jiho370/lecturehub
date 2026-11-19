"""Track C: live captions (CAP-001~008, user request 2026-11-06). Synthetic tones and text only; nothing is sent."""
import json
import threading

import numpy as np
import pytest
from typer.testing import CliRunner

from lecturehub.core import edition
from lecturehub.transcription import captions

RATE = captions.RATE


def tone(seconds, amp=8000, freq=220):
    t = np.arange(int(seconds * RATE)) / RATE
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.int16)


def silence(seconds):
    return np.zeros(int(seconds * RATE), dtype=np.int16)


def pieces(audio):
    data = audio.tobytes()
    return [data[i:i + captions.PIECE * 2] for i in range(0, len(data), captions.PIECE * 2)]


def test_CAP_001_cuts_at_a_pause_between_15_and_25_seconds():
    cuts = []
    seg = captions.Segmenter(lambda audio, a, b: cuts.append((a, b, len(audio) // 2)))
    audio = np.concatenate([tone(10), silence(0.5), tone(7), silence(0.6), tone(12)])  # pauses at 10 s and 17.5 s
    for p in pieces(audio):
        seg.add(p)
    seg.flush()
    assert 15 <= cuts[0][1] <= 18.5  # the 17.5 s pause, not the 10 s one (too early)
    assert sum(n for _, _, n in cuts) == len(audio)  # nothing lost or doubled
    assert [round(a, 1) for a, _, _ in cuts[1:]] == [round(b, 1) for _, b, _ in cuts[:-1]]


def test_CAP_001_no_pause_cuts_at_25_seconds_at_the_quietest_moment():
    cuts = []
    seg = captions.Segmenter(lambda audio, a, b: cuts.append((a, b)))
    loud = tone(40)
    loud[int(19 * RATE):int(19.3 * RATE)] //= 3  # a softer moment, not a pause
    for p in pieces(loud):
        seg.add(p)
    assert cuts and 18.5 <= cuts[0][1] <= 20 and cuts[0][1] <= captions.MAX_S


def test_CAP_002_any_device_format_becomes_16k_mono():
    t = np.arange(48000) / 48000
    left = (8000 * np.sin(2 * np.pi * 440 * t)).astype(np.int16)
    stereo = np.stack([left, left], axis=1).reshape(-1).tobytes()
    out = captions.to_16k_mono(stereo, 48000, 2)
    assert abs(len(out) - RATE) <= 2 and out.dtype == np.int16
    spectrum = np.abs(np.fft.rfft(out.astype(np.float32)))
    assert abs(np.argmax(spectrum) * RATE / len(out) - 440) < 5


class FakeQuick:
    def __init__(self, key, on_event, on_error):
        self.on_event, self.fed = on_event, 0
        self.thread = threading.Thread(target=lambda: None)

    def start(self):
        self.thread.start()

    def feed(self, piece):
        if piece is None:
            self.on_event("final", "빠른 줄 끝")
            return
        self.fed += 1
        if self.fed == 5:
            self.on_event("interim", "합성 중간")


def test_CAP_003_run_writes_lines_files_and_live_json(tmp_path):
    audio = np.concatenate([tone(18), silence(0.6), tone(10)])
    pcm = tmp_path / "합성.pcm"
    pcm.write_bytes(audio.tobytes())
    sent = []

    def recognize(chunk):
        sent.append(len(chunk) // 2)
        return f"합성 문장 {len(sent)}"

    result = captions.run(tmp_path, "합성 과목", source=lambda out, stop: captions.file_source(pcm, out, stop, pace=0),
                          recognize=recognize, gemini_key="k", quick_factory=FakeQuick)
    assert result["ok"] and result["lines"] == 2 and sum(sent) == len(audio)
    text = (tmp_path / "_lecturehub" / "captions").glob("* 합성 과목.txt")
    body = next(text).read_text(encoding="utf-8-sig")
    assert "[00:00] 합성 문장 1" in body and "합성 문장 2" in body
    quick = open(result["quick_file"], encoding="utf-8-sig").read()
    assert "빠른 줄 끝" in quick
    live = json.loads((tmp_path / "_lecturehub" / "captions" / "live.json").read_text(encoding="utf-8"))
    assert live["state"] == "stopped" and live["line_count"] == 2 and live["interim"] == ""
    assert captions.status(tmp_path)["state"] == "stopped"


def test_CAP_003_stop_file_ends_the_run_and_the_last_piece_is_still_sent(tmp_path):
    sent = []

    def endless(out, stop):
        while not stop.is_set():
            out(tone(0.1).tobytes())
            if len(sent) == 0 and stop is not None:
                captions.request_stop(tmp_path) if not (tmp_path / "_lecturehub" / "captions" / "stop").exists() else None

    result = captions.run(tmp_path, "합성 과목", source=endless, recognize=lambda c: sent.append(c) or "끝 조각",
                          gemini_key=None)
    assert result["lines"] == 1 and len(sent) == 1
    assert not (tmp_path / "_lecturehub" / "captions" / "stop").exists()


def test_CAP_004_a_piece_that_lost_words_keeps_the_quick_line(tmp_path):
    board = captions.Board(tmp_path, "합성 과목", {"confirmed": "chirp_3@us", "quick": "live"})
    board.quick_lines = [(12.0, "사채를 액면금액보다 낮은 가격에 발행하면 할인발행이라고 합니다"), (40.0, "다른 조각")]
    board.confirmed(0.0, 16.0, "할인")
    board.confirmed(16.0, 33.0, "다음 시간에는 충당부채를 다룹니다")
    assert [l.get("from") for l in board.lines] == [None, "quick", None]
    body = board.text_path.read_text(encoding="utf-8-sig")
    assert "[00:00] (빠른 줄) 사채를 액면금액보다" in body and "다른 조각" not in body


def test_CAP_005_live_api_messages():
    setup = captions.gemini_setup()["setup"]
    assert setup["model"] == "models/gemini-3.5-transcribe-live"
    assert setup["inputAudioTranscription"] == {"languageCodes": ["ko-KR"], "mode": "VERBATIM"}
    assert captions.gemini_audio(b"\x00\x01")["realtimeInput"]["audio"]["mimeType"] == "audio/pcm;rate=16000"
    assert captions.gemini_event({"serverContent": {"interimInputTranscription": {"text": "오늘은"}}}) == ("interim", "오늘은")
    assert captions.gemini_event({"serverContent": {"inputTranscription": {"text": "오늘은 사채"}}}) == ("final", "오늘은 사채")
    assert captions.gemini_event({"voiceActivity": {}}) is None


def test_CAP_006_cli_never_starts_without_consent(tmp_path, monkeypatch):
    from lecturehub import cli

    cli._register_all()
    monkeypatch.setattr(captions, "chirp_recognizer", lambda project, phrases=(): pytest.fail("must not reach the engine"))
    result = CliRunner().invoke(cli.app, ["captions", "start", "--course", "구조역학", "--root", str(tmp_path)], input="n\n")
    assert result.exit_code == 1 and "동의하지 않아" in result.output
    assert not (tmp_path / "_lecturehub" / "captions").exists()


def test_CAP_006_cli_runs_a_synthetic_file_in_a_synthetic_root(tmp_path, monkeypatch):
    from lecturehub import cli

    cli._register_all()
    (tmp_path / "_lecturehub").mkdir()
    (tmp_path / "_lecturehub" / "synthetic_root").write_text("합성", encoding="utf-8")
    pcm = tmp_path / "합성.pcm"
    pcm.write_bytes(tone(3).tobytes())
    if edition.has("qa"):
        from lecturehub.transcription import qa_packs

        (tmp_path / "합성 실시간" / "발표").mkdir(parents=True)  # a pack of this course with terms (P-051)
        made = qa_packs.create(tmp_path, "합성 발표", tmp_path / "합성 실시간" / "발표")
        meta = json.loads((tmp_path / "_lecturehub" / "qa" / "packs" / made["id"] / "pack.json").read_text(encoding="utf-8"))
        meta["terms"] = ["합성용어", "사수위"]
        (tmp_path / "_lecturehub" / "qa" / "packs" / made["id"] / "pack.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    given = []
    monkeypatch.setattr(captions, "chirp_recognizer",
                        lambda project, phrases=(): given.append(list(phrases)) or (lambda chunk: "합성 확정 줄"))
    monkeypatch.setattr(captions, "default_project", lambda: "synthetic-project")
    result = CliRunner().invoke(cli.app, ["captions", "start", "--course", "합성 실시간", "--file", str(pcm), "--pace", "0",
                                          "--no-quick", "--confirmed", "--root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "합성 확정 줄" in result.output
    expected = ["합성용어", "사수위"] if edition.has("qa") else []
    assert given == [expected]
    assert json.loads((tmp_path / "_lecturehub" / "captions" / "live.json").read_text(encoding="utf-8"))["hints"] == len(expected)
    consent = json.loads((tmp_path / "_lecturehub" / "consent.json").read_text(encoding="utf-8"))
    assert consent["google_live"]["합성 실시간"]["authority"] == "synthetic_test"


class FinalsQuick(FakeQuick):
    def feed(self, piece):
        if piece is None:
            self.on_event("final", "마지막 문장")
            return
        self.fed += 1
        if self.fed in (10, 30):
            self.on_event("interim", "합성 중")
            self.on_event("final", f"합성 문장 {self.fed}")


def test_CAP_008_gemini_live_alone_is_the_default_and_its_lines_are_the_captions(tmp_path, monkeypatch):
    """User 2026-11-06: "기본을 … 트랜스크라이브 라이브를 기본으로" — Chirp 3 only when asked for."""
    pcm = tmp_path / "합성.pcm"
    pcm.write_bytes(tone(4).tobytes())
    result = captions.run(tmp_path, "합성 과목", source=lambda out, stop: captions.file_source(pcm, out, stop, pace=0),
                          recognize=None, gemini_key="k", quick_factory=FinalsQuick)
    assert result["lines"] == 3 and result["quick_file"] == ""
    files = sorted(p.name for p in (tmp_path / "_lecturehub" / "captions").glob("*.txt"))
    assert len(files) == 1 and "(빠른)" not in files[0]  # one file, no copy
    body = open(result["file"], encoding="utf-8-sig").read()
    assert body.count("합성 문장") == 2 and "마지막 문장" in body
    live = json.loads((tmp_path / "_lecturehub" / "captions" / "live.json").read_text(encoding="utf-8"))
    assert [l["text"] for l in live["lines"]] == ["합성 문장 10", "합성 문장 30", "마지막 문장"]
    assert live["engines"]["confirmed"] is None and live["quick_file"] == ""


def test_CAP_008_cli_does_not_reach_chirp_unless_asked(tmp_path, monkeypatch):
    from lecturehub import cli
    from lecturehub.transcription import gemini

    cli._register_all()
    (tmp_path / "_lecturehub").mkdir()
    (tmp_path / "_lecturehub" / "synthetic_root").write_text("합성", encoding="utf-8")
    pcm = tmp_path / "합성.pcm"
    pcm.write_bytes(tone(2).tobytes())
    monkeypatch.setattr(captions, "chirp_recognizer", lambda project, phrases=(): pytest.fail("Chirp is opt-in"))
    monkeypatch.setattr(gemini, "load_api_key", lambda: "k")
    seen = {}
    monkeypatch.setattr(captions, "run", lambda root, course, **kw: seen.update(kw) or {"ok": True})
    result = CliRunner().invoke(cli.app, ["captions", "start", "--course", "합성 실시간", "--file", str(pcm), "--pace", "0",
                                          "--root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert seen["recognize"] is None and seen["gemini_key"] == "k"


class CountingQuick(FakeQuick):
    made = []

    def __init__(self, key, on_event, on_error):
        super().__init__(key, on_event, on_error)
        CountingQuick.made.append(self)


def test_CAP_009_pause_sends_nothing_and_resume_goes_on_in_the_same_file(tmp_path):
    """P-050 (user 2026-11-07: 일시중단, 이어하기): no audio reaches Chirp or Gemini while paused; marks in the file."""
    import time

    CountingQuick.made = []
    sent = []

    def source(out, stop):
        for _ in range(20):
            out(tone(0.1).tobytes())
        captions.request_pause(tmp_path, True)
        time.sleep(0.6)
        for _ in range(30):  # paused: dropped
            out(tone(0.1).tobytes())
        assert captions.status(tmp_path)["state"] == "paused"
        captions.request_pause(tmp_path, False)
        time.sleep(0.6)
        for _ in range(20):
            out(tone(0.1).tobytes())

    result = captions.run(tmp_path, "합성 과목", source=source, recognize=lambda c: sent.append(len(c) // 2) or "합성 조각",
                          gemini_key="k", quick_factory=CountingQuick)
    assert sum(sent) == 40 * int(0.1 * RATE)  # 2 s before + 2 s after, none of the paused 3 s
    assert len(sent) == 2  # the piece in progress was finished at the pause
    assert len(CountingQuick.made) == 2 and sum(q.fed for q in CountingQuick.made) == 40  # Gemini closed, then opened again
    body = open(result["file"], encoding="utf-8-sig").read()
    assert "# 일시중단 00:0" in body and "# 이어서 00:0" in body and body.count("합성 조각") == 2
    assert len(list((tmp_path / "_lecturehub" / "captions").glob("* 합성 과목.txt"))) == 1  # same file
    if edition.has("qa"):
        from lecturehub.transcription import qa_assist

        assert "일시중단" not in qa_assist.class_text_of({"file": result["file"]})  # the helper skips the marks


def test_CAP_010_a_switch_file_still_held_by_windows_is_taken_at_the_next_check(tmp_path, monkeypatch):
    """[일시중단]/[Gemini] flags: an unlink that fails (WinError 32) must not end the captions; the flag waits."""
    flag = tmp_path / "pause.on"
    flag.write_text("1", encoding="utf-8")
    real, held = type(tmp_path).unlink, {"n": 0}

    def unlink(self, missing_ok=False):
        if self == flag and held["n"] < 1:
            held["n"] += 1
            raise PermissionError(32, "다른 프로세스가 파일을 사용 중")
        return real(self, missing_ok=missing_ok)

    monkeypatch.setattr(type(tmp_path), "unlink", unlink)
    assert captions._take_flag(flag) is False and flag.exists()   # held: left for the next check
    assert captions._take_flag(flag) is True and not flag.exists()
    assert captions._take_flag(flag) is False                     # nothing to take
    monkeypatch.undo()
    captions._set_flag(tmp_path, "pause.off", "pause.on")
    assert (tmp_path / "pause.off").read_text(encoding="utf-8") == "1" and not list(tmp_path.glob("*.tmp"))


def test_CAP_011_chirp_gets_the_course_words_as_phrase_hints():
    """P-051 (L-025): pack terms go to Chirp 3 as an inline phrase set; without terms the settings stay as before."""
    pytest.importorskip("google.cloud.speech_v2")
    plain = captions.chirp_config()
    assert not plain.adaptation.phrase_sets and plain.model == captions.CHIRP_MODEL
    hinted = captions.chirp_config(["유효저수량", "사수위"])
    phrases = hinted.adaptation.phrase_sets[0].inline_phrase_set.phrases
    assert [p.value for p in phrases] == ["유효저수량", "사수위"] and phrases[0].boost == captions.HINT_BOOST
    assert hinted.language_codes == ["ko-KR"]


def test_CAP_gemini_live_sends_the_key_in_a_header_not_the_url(monkeypatch):
    """Security review 2026-11-13 (L6): a key in the URL ends up in proxy logs and error text."""
    client = pytest.importorskip("websockets.sync.client")
    seen, errors = {}, []

    class FakeSocket:
        def __enter__(self): return self
        def __exit__(self, *exc): return False
        def send(self, text): seen.setdefault("sent", []).append(text)
        def recv(self, timeout=None): return json.dumps({"setupComplete": {}})
        def __iter__(self): return iter(())

    def connect(url, **kw):
        seen.update(url=url, **kw)
        return FakeSocket()

    monkeypatch.setattr(client, "connect", connect)
    live = captions.GeminiLive("synthetic-not-a-key", lambda *a: None, errors.append)
    live.feed(None)
    live._run()
    assert "synthetic-not-a-key" not in seen["url"] and "key=" not in seen["url"]
    assert seen["additional_headers"] == {"x-goog-api-key": "synthetic-not-a-key"} and errors == []