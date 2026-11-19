"""What each engine has used and has left, for the app (reads job files and the ledger only).

- Google: the request ledger (D-054), as before.
- Whisper (Groq free tier): audio seconds sent by this app in the last hour / 24 hours, from the
  jobs' chunk ``sent_at`` times, against 7,200 s/hour and 28,800 s/day. Groq counts the real limit;
  another program using the same key is invisible here, so the numbers are marked as this app's use.
- MAI playground: no published limit. Chunks imported today and rejections seen today; "하루 30조각
  무사" is an observation (§8.6a), not a limit.

Per recording: the latest job of each engine and whether it can be started now (and why not).
"""
from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

from .providers import PROVIDERS
from .store import app_dir, read_json

GROQ_HOUR_S, GROQ_DAY_S = 7_200, 28_800
MAI_OBSERVED_OK_PER_DAY = 30
LEVEL = {"queued": "대기", "running": "처리 중", "retrying": "재시도 중", "waiting_for_quota": "한도 대기",
         "waiting_user": "동의 필요", "waiting_agent": "Claude 업로드 대기", "waiting_master": "녹음 끝 대기",
         "qa_warning": "완료(검토 필요)", "done": "완료", "failed": "실패", "cancelled": "취소됨"}


def _jobs(root: Path, provider: str) -> list[dict]:
    base = app_dir(root) / PROVIDERS[provider].job_folder
    states = [read_json(p) or {} for p in sorted(base.glob("*/state.json"))] if base.is_dir() else []
    return [s for s in states if s.get("provider", "gemini_transcribe") == provider]


def whisper_usage(root: Path, now_ms: int) -> dict:
    hour = day = 0
    for state in _jobs(root, "whisper_groq"):
        if state.get("mock"):
            continue
        for chunk in state.get("chunks", []):
            seconds = (chunk["end_ms"] - chunk["start_ms"]) / 1000
            for at in chunk.get("sent_at", []):
                if now_ms - at < 3_600_000:
                    hour += seconds
                if now_ms - at < 86_400_000:
                    day += seconds
    return {"hour_used_s": int(hour), "hour_limit_s": GROQ_HOUR_S, "day_used_s": int(day), "day_limit_s": GROQ_DAY_S,
            "day_left_s": max(0, GROQ_DAY_S - int(day)), "hour_left_s": max(0, GROQ_HOUR_S - int(hour)),
            "basis": "이 앱이 보낸 양(최근 1시간·24시간). 실제 한도는 Groq가 센다"}


def _local_day(ms: int):
    return datetime.fromtimestamp(ms / 1000).astimezone().date()


def mai_usage(root: Path, now_ms: int) -> dict:
    today = _local_day(now_ms)
    chunks = rejected = 0
    for state in _jobs(root, "mai_playground"):
        for chunk in state.get("chunks", []):
            for result in chunk.get("results", {}).values():
                if _local_day(result.get("imported_at", 0)) == today:
                    chunks += 1
        for event in state.get("events", []):
            if event.get("type") == "provider_rejection" and _local_day(event["at"]) == today:
                rejected += 1
    return {"today_chunks": chunks, "today_rejections": rejected, "observed_ok_per_day": MAI_OBSERVED_OK_PER_DAY,
            "basis": "공개 한도 없음. 하루 30조각은 막히지 않았던 관찰값(§8.6a)"}


def job_summary(state: dict) -> dict:
    chunks = state.get("chunks", [])
    done = sum(1 for c in chunks if c.get("state") in ("done", "qa_warning"))
    out = {"job_id": state.get("job_id"), "state": state.get("state"), "label": LEVEL.get(state.get("state"), state.get("state")),
           "chunks_done": done, "chunks_total": len(chunks), "created_at": state.get("created_at"),
           "output": state.get("output"), "error": next((c.get("error") for c in chunks if c.get("error")), None),
           "language": (state.get("preset") or {}).get("language") or "ko"}
    if state.get("next_eligible_at"):
        out["next_eligible_at"] = state["next_eligible_at"]
    if state.get("provider") == "mai_playground":
        out["runs"] = state.get("runs")
        uploader = state.get("uploader") or {}
        if uploader.get("via") == "aside":  # the app uploads through Aside itself (2026-11-06), not a Claude window
            out["via"] = "aside"
            if state.get("state") == "waiting_agent":
                out["label"] = "Aside 업로드 중"
                out["chunks_done"] = max(done, len(chunks) - int(uploader.get("total") or 0) + int(uploader.get("done") or 0))
            if state.get("state") == "failed":
                out["error"] = out["error"] or uploader.get("error")
                out["login_required"] = bool(uploader.get("login_required"))
    return out


def jobs_for_recording(root: Path, audio: str, audio_sha256: str | None = None) -> dict[str, dict | None]:
    """Latest non-dry-run job per engine for this recording (by path, or content when given)."""
    result: dict[str, dict | None] = {}
    for provider in PROVIDERS:
        mine = [s for s in _jobs(root, provider) if not s.get("dry_run_1min") and (
            (s.get("audio") and Path(s["audio"]) == Path(audio))
            or (audio_sha256 and s.get("audio_sha256") == audio_sha256)
            or ((s.get("live") or {}).get("session_dir") and Path(s["live"]["session_dir"]) == Path(audio).parent))]
        mine.sort(key=lambda s: s.get("created_at") or 0)
        result[PROVIDERS[provider].engine] = job_summary(mine[-1]) if mine else None
    return result


def now_ms() -> int:
    return int(time.time() * 1000)
