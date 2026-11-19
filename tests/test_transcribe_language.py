"""Track C: speech language of a recording (user report 2026-10-19 — a German YouTube recording was sent as Korean
and came back as two lines). HTTP uses httpx.MockTransport; no audio leaves the machine."""
import json
import shutil
import subprocess

import httpx
import pytest

from lecturehub.transcription import language as lang
from lecturehub.transcription.gemini import GeminiClient
from lecturehub.transcription.groq import GroqWhisperClient
from lecturehub.transcription.qa import check_chunk

MIN = 60_000


def test_TLANG_001_explicit_then_course_default_then_korean(tmp_path):
    root = tmp_path / "합성 학기"
    (root / "_lecturehub").mkdir(parents=True)
    assert lang.resolve(root, "기타", None, "gemini_transcribe") == "ko"
    (root / "_lecturehub" / lang.MAP_FILE).write_text(json.dumps({"기타": "de", "기초어학": "bogus"}), encoding="utf-8")
    assert lang.resolve(root, "기타", None, "gemini_transcribe") == "de"
    assert lang.resolve(root, "기초어학", None, "gemini_transcribe") == "ko"  # a bad value is ignored, not trusted
    assert lang.resolve(root, "기타", "EN", "whisper_groq") == "en"
    assert lang.resolve(root, "기타", "auto", "whisper_groq") == "auto"
    with pytest.raises(ValueError):
        lang.resolve(root, "기타", "auto", "gemini_transcribe")  # Gemini needs a language code
    with pytest.raises(ValueError):
        lang.resolve(root, "기타", "klingon", "whisper_groq")


def test_TLANG_002_gemini_sends_the_chosen_language_code():
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"steps": []})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    file = {"uri": "https://f/abc", "mime_type": "audio/mpeg"}
    GeminiClient("SECRET", http=http, language="de").interact(file)
    GeminiClient("SECRET", http=http).interact(file)
    codes = [b["generation_config"]["transcription_config"]["language_codes"] for b in bodies]
    assert codes == [["de-DE"], ["ko-KR"]]


def test_TLANG_003_whisper_sends_language_or_lets_whisper_detect(tmp_path):
    forms = []

    def handler(request: httpx.Request) -> httpx.Response:
        forms.append(request.content.decode("utf-8", "replace"))
        return httpx.Response(200, json={"text": "", "segments": []})

    audio = tmp_path / "chunk.mp3"
    audio.write_bytes(b"ID3fake")
    http = httpx.Client(transport=httpx.MockTransport(handler))
    GroqWhisperClient("gsk_SYNTHETIC", http=http, language="de").transcribe(audio)
    GroqWhisperClient("gsk_SYNTHETIC", http=http, language="auto").transcribe(audio)
    assert 'name="language"' in forms[0] and "\r\nde\r\n" in forms[0]
    assert 'name="language"' not in forms[1]


def test_TLANG_004_sparse_text_in_a_short_chunk_is_flagged():
    sparse = [(1000, "Wir haben uns an Feinden Wir haben uns an Feinden"), (132_000, "Tausende von")]
    issues = check_chunk(sparse, 151_978)
    assert [i.check for i in issues] == ["density"] and "--language" in issues[0].detail
    normal = [(i * 10_000, "오늘은 토지이용계획의 목적과 실현수단을 이야기합니다.") for i in range(15)]
    assert check_chunk(normal, 151_978) == []
    assert check_chunk([(0, "강의가 끝나고 정리하는 짧은 끝부분 한 문장입니다(합성).")], 40_000) == []  # under a minute: not judged


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")
def test_TLANG_005_cli_language_is_kept_in_the_job(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from lecturehub import cli

    cli._register_all()
    root = tmp_path / "합성 학기"
    (root / "_lecturehub").mkdir(parents=True)
    (root / "_lecturehub" / "synthetic_root").write_text("synthetic", encoding="utf-8")
    source = root / "기초어학 영상 (합성).mp3"
    subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=70",
                    "-ac", "1", str(source)], check=True)
    monkeypatch.setenv("LECTUREHUB_FAKE_GROQ", "1")
    result = CliRunner().invoke(cli.app, ["transcribe", "run", str(source), "--provider", "whisper", "--course", "",
                                          "--language", "de", "--root", str(root)], input="y\n")
    assert result.exit_code == 0, result.output
    assert "언어 de" in result.output
    state = json.loads(next((root / "_lecturehub" / "jobs_whisper").glob("*/state.json")).read_text(encoding="utf-8"))
    assert state["preset"]["language"] == "de"
    bad = CliRunner().invoke(cli.app, ["transcribe", "run", str(source), "--course", "", "--language", "auto",
                                       "--again", "--root", str(root)], input="y\n")
    assert bad.exit_code == 1 and "auto는 Whisper만" in bad.output
