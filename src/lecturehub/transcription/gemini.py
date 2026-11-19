"""Gemini Transcribe client, ported from transcribe.ps1 (see port notes).

Flow: resumable upload → poll file ACTIVE → POST /v1beta/interactions
(verbatim, ko-KR, word timestamps) → delete the uploaded file.
The API key is never logged, echoed or written to disk.
"""
from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path


class _LazyHttpx:
    """httpx on first use: importing it pulls in click/rich (~1 s cold), which delayed the app's first screen."""

    def __getattr__(self, name: str):
        import httpx as module

        return getattr(module, name)


httpx = _LazyHttpx()

API = "https://generativelanguage.googleapis.com"
DEFAULT_MODEL = "gemini-3.5-transcribe"
KEYRING_SERVICE, KEYRING_NAME = "lecturehub", "gemini"
_RETRY_IN = re.compile(r"retry in ([\d.]+)\s*s", re.IGNORECASE)
_RETRY_DELAY = re.compile(r'"retryDelay"\s*:\s*"([\d.]+)s"')


class GeminiError(RuntimeError):
    """Base class; messages never contain the API key."""


class RateLimited(GeminiError):
    """Per-minute limit: wait ``retry_after_s`` then try again."""

    def __init__(self, message: str, retry_after_s: float | None):
        super().__init__(message)
        self.retry_after_s = retry_after_s


class DailyQuotaExhausted(GeminiError):
    """Daily request limit reached: the job waits for quota (not a failure)."""


class TransientError(GeminiError):
    """5xx / network error: retry a limited number of times."""


class PermanentError(GeminiError):
    """4xx other than 429 (bad key, bad request): retrying will not help."""


@dataclass(frozen=True)
class TranscribeResult:
    text: str
    words: list[dict]
    raw: dict


def load_api_key() -> str:
    """keyring(lecturehub/gemini) → GEMINI_API_KEY (process, then Windows user scope)."""
    try:
        import keyring

        key = keyring.get_password(KEYRING_SERVICE, KEYRING_NAME)
        if key:
            return key
    except Exception:  # noqa: BLE001, S110 - a broken keyring backend must not block the env fallback
        pass
    key = os.environ.get("GEMINI_API_KEY")
    if not key and os.name == "nt":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as env:
                key = winreg.QueryValueEx(env, "GEMINI_API_KEY")[0]
        except OSError:
            key = None
    if not key:
        raise PermanentError("Gemini API 키가 없습니다. keyring(lecturehub/gemini) 또는 GEMINI_API_KEY를 설정하세요.")
    return key


def _mime(path: Path) -> str:
    suffix = path.suffix.lower()
    return {".m4a": "audio/m4a", ".wav": "audio/wav"}.get(suffix, "audio/mpeg")


def classify(response: httpx.Response) -> None:
    """Raise the matching error for a failed response (ps1 Invoke-Api rules)."""
    if response.is_success:
        return
    status, detail = response.status_code, response.text[:2000]
    if status == 429:
        if "per minute" in detail.lower() or "perminute" in detail.lower():
            match = _RETRY_IN.search(detail) or _RETRY_DELAY.search(detail)
            header = response.headers.get("retry-after")
            wait = float(match.group(1)) if match else (float(header) if header and header.isdigit() else None)
            raise RateLimited(f"분당 한도(429): {detail[:300]}", wait)
        raise DailyQuotaExhausted(f"일일 한도(429): {detail[:300]}")
    if status >= 500:
        raise TransientError(f"서버 오류 {status}: {detail[:300]}")
    raise PermanentError(f"HTTP {status}: {detail[:300]}")


class GeminiClient:
    def __init__(self, api_key: str, *, model: str = DEFAULT_MODEL, http: httpx.Client | None = None,
                 sleep: Callable[[float], None] = time.sleep, poll_seconds: float = 2.0, language: str = "ko"):
        from .language import gemini_codes

        self._key = api_key
        self.model = model
        self.language_codes = gemini_codes(language)  # was always ko-KR (2026-10-19: German came back as 2 lines)
        self.http = http or httpx.Client(timeout=httpx.Timeout(900.0, connect=30.0))
        self.sleep = sleep
        self.poll_seconds = poll_seconds

    def _headers(self, extra: dict | None = None) -> dict:
        return {"x-goog-api-key": self._key, **(extra or {})}

    def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        try:
            response = self.http.request(method, url, **kwargs)
        except httpx.TransportError as exc:
            raise TransientError(f"네트워크 오류: {type(exc).__name__}") from exc
        classify(response)
        return response

    def upload(self, path: Path) -> dict:
        data = path.read_bytes()
        mime = _mime(path)
        start = self._request("POST", f"{API}/upload/v1beta/files", headers=self._headers({
            "X-Goog-Upload-Protocol": "resumable", "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": str(len(data)),
            "X-Goog-Upload-Header-Content-Type": mime, "Content-Type": "application/json",
        }), content=json.dumps({"file": {"display_name": "lecture"}}))
        upload_url = start.headers.get("x-goog-upload-url")
        if not upload_url:
            raise TransientError("업로드 URL을 받지 못했습니다.")
        done = self._request("POST", upload_url, headers={
            "X-Goog-Upload-Offset": "0", "X-Goog-Upload-Command": "upload, finalize"}, content=data)
        file = done.json()["file"]
        while file.get("state") == "PROCESSING":
            self.sleep(self.poll_seconds)
            file = self._request("GET", f"{API}/v1beta/{file['name']}", headers=self._headers()).json()
        if file.get("state") not in (None, "ACTIVE"):
            raise TransientError(f"업로드 파일 상태: {file.get('state')}")
        return {**file, "mime_type": mime}

    def delete(self, name: str) -> None:
        try:
            self.http.request("DELETE", f"{API}/v1beta/{name}", headers=self._headers())
        except httpx.HTTPError:
            pass  # best effort, as in transcribe.ps1

    def interact(self, file: dict) -> dict:
        body = {
            "model": self.model,
            "input": [{"type": "audio", "uri": file["uri"], "mime_type": file["mime_type"]}],
            "generation_config": {"transcription_config": {
                "language_codes": self.language_codes,
                "mode": {"type": "verbatim", "timestamp_granularities": ["word"]},
            }},
        }
        response = self._request("POST", f"{API}/v1beta/interactions", headers=self._headers(
            {"Content-Type": "application/json; charset=utf-8"}),
            content=json.dumps(body, ensure_ascii=False).encode("utf-8"))
        return json.loads(response.content.decode("utf-8"))

    def transcribe(self, path: Path) -> TranscribeResult:
        """One billable request (interactions). Upload/delete are not counted by the ledger."""
        file = self.upload(path)
        try:
            raw = self.interact(file)
        finally:
            if file.get("name"):
                self.delete(file["name"])
        return parse_result(raw)


def parse_result(raw: dict) -> TranscribeResult:
    # steps[].content is a *list* of parts in the real API (2026-09-28 dry run). PowerShell's
    # pipeline flattened it silently in transcribe.ps1; accept a single dict as well.
    parts = []
    for step in raw.get("steps", []):
        content = step.get("content")
        parts.extend(content if isinstance(content, list) else [content])
    content = next((part for part in parts if isinstance(part, dict) and part.get("text")), None)
    if content is None:
        return TranscribeResult("", [], raw)
    words = [a for a in content.get("annotations") or [] if a.get("type") == "word_info"]
    return TranscribeResult(str(content["text"]), words, raw)


def _offset_ms(word: dict) -> int:
    return int(Decimal(str(word["start_offset"]).rstrip("s")) * 1000)


def paragraphs(result: TranscribeResult) -> list[tuple[int, str]]:
    """≈1-minute paragraphs (chunk-local ms), cut at byte offsets to keep punctuation.

    Port of transcribe.ps1: break at ≥60 s after a sentence end, or at ≥90 s.
    Without word timings the whole text is one paragraph at 0.
    """
    text = result.text.strip()
    if not text:
        return []
    words = result.words
    if not words:
        return [(0, text)]
    data = result.text.encode("utf-8")
    breaks = [0]
    paragraph_start = _offset_ms(words[0])
    for i in range(1, len(words)):
        at = _offset_ms(words[i])
        sentence_end = bool(re.search(r"[.?!]$", str(words[i - 1].get("text", ""))))
        if (at - paragraph_start >= 60_000 and sentence_end) or at - paragraph_start >= 90_000:
            breaks.append(i)
            paragraph_start = at
    out: list[tuple[int, str]] = []
    for b, index in enumerate(breaks):
        start = 0 if b == 0 else int(words[index]["start_index"])
        end = int(words[breaks[b + 1]]["start_index"]) if b + 1 < len(breaks) else len(data)
        body = data[start:end].decode("utf-8", errors="replace").strip()
        if body:
            out.append((_offset_ms(words[index]), body))
    return out
