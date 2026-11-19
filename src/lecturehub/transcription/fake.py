"""Mock Gemini transport for verification (LECTUREHUB_FAKE_GEMINI=1). Sends nothing anywhere.

Produces synthetic paragraphs every 60 s of the chunk, in the real response
shape (steps[].content is a list), so the whole pipeline runs offline.
"""
from __future__ import annotations

from pathlib import Path

from .audio import duration_ms
from .gemini import TranscribeResult, parse_result


class FakeGeminiClient:
    def transcribe(self, path: Path) -> TranscribeResult:
        total = max(1000, duration_ms(Path(path)))
        texts, words, index = [], [], 0
        for n, start in enumerate(range(0, total, 60_000)):
            sentence = f"합성 모의 전사 {n + 1}번 문단입니다({Path(path).stem[-6:]}). 실제 음성은 외부로 보내지 않았습니다."
            words.append({"type": "word_info", "text": sentence, "start_offset": f"{start / 1000:.3f}s",
                          "start_index": index})
            texts.append(sentence)
            index += len((sentence + " ").encode("utf-8"))
        raw = {"object": "interaction", "status": "completed", "mock": True, "steps": [
            {"type": "model_output", "content": [{"type": "text", "text": " ".join(texts), "annotations": words}]}]}
        return parse_result(raw)


class FakeGroqClient:
    """Mock Groq Whisper transport (LECTUREHUB_FAKE_GROQ=1): verbose_json segments every 30 s, sends nothing."""

    def transcribe(self, path: Path) -> TranscribeResult:
        from .groq import parse_result as parse_groq

        total = max(1000, duration_ms(Path(path)))
        segments = [{"id": n, "start": start / 1000, "end": min(total, start + 30_000) / 1000,
                     "text": f" 합성 모의 Whisper {n + 1}번 구간입니다. 실제 음성은 보내지 않았습니다.",
                     "avg_logprob": -0.2, "no_speech_prob": 0.01, "compression_ratio": 1.3}
                    for n, start in enumerate(range(0, total, 30_000))]
        raw = {"task": "transcribe", "language": "korean", "duration": total / 1000, "mock": True,
               "text": "".join(s["text"] for s in segments), "segments": segments}
        return parse_groq(raw)
