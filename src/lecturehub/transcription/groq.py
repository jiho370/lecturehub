"""Whisper through the Groq API (D-061): the third, reference engine without laptop load.

One request per chunk: POST /openai/v1/audio/transcriptions (multipart, verbose_json,
segment timestamps, language ko, temperature 0). The API key is never logged,
echoed or written to disk. Errors reuse the Gemini error classes so the job
runner treats every remote engine the same way (429 → wait, 5xx → retry, 4xx → stop).

Whisper makes things up over silence ("시청해 주셔서 감사합니다"). The raw response
is kept as is; the derived text drops only segments Whisper itself marks as
no speech (the reference decoder's rule: no_speech_prob > 0.6 and avg_logprob < -1),
and flags the rest for QA (``extra_qa``) instead of deleting it.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from .gemini import (
    DailyQuotaExhausted,
    PermanentError,
    RateLimited,
    TranscribeResult,
    TransientError,
    httpx,  # imported on first use (see gemini._LazyHttpx)
)
from .qa import QAIssue

API = "https://api.groq.com/openai/v1/audio/transcriptions"
DEFAULT_MODEL = "whisper-large-v3"
KEYRING_SERVICE, KEYRING_NAME = "lecturehub", "groq"
NO_SPEECH_PROB, LOW_LOGPROB = 0.6, -1.0
HIGH_COMPRESSION = 2.4           # the reference decoder's repetition threshold
# Phrases Whisper is known to invent for Korean silence (subtitle credits of its training data).
HALLUCINATION_PHRASES = ("시청해 주셔서 감사합니다", "시청해주셔서 감사합니다", "구독과 좋아요", "MBC 뉴스",
                         "자막 제공", "다음 영상에서 만나요")
_TRY_AGAIN = re.compile(r"try again in (?:(\d+)h)?(?:(\d+)m)?(?:([\d.]+)s)?", re.IGNORECASE)


def load_api_key() -> str:
    """keyring(lecturehub/groq) → GROQ_API_KEY (process, then Windows user scope)."""
    try:
        import keyring

        key = keyring.get_password(KEYRING_SERVICE, KEYRING_NAME)
        if key:
            return key
    except Exception:  # noqa: BLE001, S110 - a broken keyring backend must not block the env fallback
        pass
    key = os.environ.get("GROQ_API_KEY")
    if not key and os.name == "nt":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as env:
                key = winreg.QueryValueEx(env, "GROQ_API_KEY")[0]
        except OSError:
            key = None
    if not key:
        raise PermanentError("Groq API 키가 없습니다. 사용자 환경변수 GROQ_API_KEY 또는 keyring(lecturehub/groq)을 "
                             "설정하세요.")
    return key


def _wait_seconds(response: httpx.Response, detail: str) -> float | None:
    header = response.headers.get("retry-after")
    try:
        if header:
            return float(header)
    except ValueError:
        pass
    match = _TRY_AGAIN.search(detail)
    if match and any(match.groups()):
        hours, minutes, seconds = (float(g) if g else 0.0 for g in match.groups())
        return hours * 3600 + minutes * 60 + seconds
    return None


def classify(response: httpx.Response) -> None:
    if response.is_success:
        return
    status, detail = response.status_code, response.text[:2000]
    if status == 429:
        wait = _wait_seconds(response, detail)
        # Groq limits are rolling windows (per minute, per hour, per day of audio seconds): the wait it
        # names is the truth. Without one, a daily limit waits for the next day like Gemini (D-054).
        if wait is None and re.search(r"per day|\(ASD\)|\(RPD\)", detail, re.IGNORECASE):
            raise DailyQuotaExhausted(f"일일 한도(429): {detail[:300]}")
        raise RateLimited(f"한도(429): {detail[:300]}", wait)
    if status >= 500:
        raise TransientError(f"서버 오류 {status}: {detail[:300]}")
    raise PermanentError(f"HTTP {status}: {detail[:300]}")


class GroqWhisperClient:
    def __init__(self, api_key: str, *, model: str = DEFAULT_MODEL, language: str = "ko",
                 http: httpx.Client | None = None):
        self._key = api_key
        self.model, self.language = model, language
        self.http = http or httpx.Client(timeout=httpx.Timeout(600.0, connect=30.0))

    def transcribe(self, path: Path) -> TranscribeResult:
        path = Path(path)
        data = {"model": self.model, "response_format": "verbose_json",
                "temperature": "0", "timestamp_granularities[]": "segment"}
        if self.language and self.language != "auto":  # auto: Whisper detects the language itself
            data["language"] = self.language
        try:
            with path.open("rb") as stream:
                response = self.http.post(API, headers={"Authorization": f"Bearer {self._key}"}, data=data,
                                          files={"file": (path.name, stream, "audio/mpeg")})
        except httpx.TransportError as exc:
            raise TransientError(f"네트워크 오류: {type(exc).__name__}") from exc
        classify(response)
        raw = response.json()
        raw.setdefault("_request", {"model": self.model, "language": self.language})
        return parse_result(raw)


def _is_no_speech(segment: dict) -> bool:
    return (float(segment.get("no_speech_prob") or 0) > NO_SPEECH_PROB
            and float(segment.get("avg_logprob") or 0) < LOW_LOGPROB)


def parse_result(raw: dict) -> TranscribeResult:
    """Segments → the word-info shape ``gemini.paragraphs`` understands (start_offset, start_index bytes)."""
    segments = raw.get("segments")
    if not isinstance(segments, list):
        text = str(raw.get("text") or "").strip()
        return TranscribeResult(text, [], raw)
    parts: list[str] = []
    words: list[dict] = []
    index = 0
    for segment in segments:
        body = str(segment.get("text") or "").strip()
        if not body or _is_no_speech(segment):
            continue
        words.append({"type": "word_info", "text": body, "start_offset": f"{float(segment.get('start') or 0):.3f}s",
                      "start_index": index})
        parts.append(body)
        index += len((body + " ").encode("utf-8"))
    return TranscribeResult(" ".join(parts), words, raw)


def extra_qa(raw: dict) -> list[QAIssue]:
    """Whisper-specific warnings from the raw response (nothing is removed here)."""
    segments = raw.get("segments") if isinstance(raw.get("segments"), list) else []
    issues: list[QAIssue] = []
    dropped = sum(1 for s in segments if str(s.get("text") or "").strip() and _is_no_speech(s))
    if dropped >= 10 or (segments and dropped / len(segments) >= 0.2):  # a few are normal pauses, not a warning
        issues.append(QAIssue("no_speech_dropped", f"말소리 없음으로 판정된 구간 {dropped}개를 본문에서 뺌(원시 응답에는 남음)"))
    phrases = [s for s in segments if not _is_no_speech(s)
               and any(p in str(s.get("text") or "") for p in HALLUCINATION_PHRASES)]
    if phrases:
        issues.append(QAIssue("hallucination", f"무음 환각으로 흔한 문구 {len(phrases)}곳(예: "
                                               f"'{str(phrases[0].get('text')).strip()[:20]}')"))
    repeating = [s for s in segments if float(s.get("compression_ratio") or 0) > HIGH_COMPRESSION]
    if repeating:
        issues.append(QAIssue("tail_repetition", f"압축률 {HIGH_COMPRESSION} 초과 구간 {len(repeating)}개 — 반복 환각 의심"))
    return issues
