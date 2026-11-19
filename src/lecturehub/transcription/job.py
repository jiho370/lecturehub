"""Google one-click transcription job (§8.4): consent → plan → chunks → API → QA → merge.

State lives in <root>/_lecturehub/jobs/<job_id>/state.json (DB link is C-2).
Chunk states follow §6.3: queued → running → done / rate_limited → retrying →
… / qa_warning / failed. A used-up daily quota is waiting_for_quota, never a
failure; resuming continues with the remaining chunks only.
"""
from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol

from lecturehub.core.ids import new_ulid
from lecturehub.core.timeutil import add_chunk_offset

from . import audio
from .chunking import Chunk, plan_chunks
from .consent import ConsentStore, ensure_consent
from .gemini import (
    DailyQuotaExhausted,
    PermanentError,
    RateLimited,
    TranscribeResult,
    TransientError,
    paragraphs,
)
from .providers import provider as provider_info
from .qa import check_chunk
from .quota import QuotaLedger, QuotaWait
from .store import app_dir, ensure_inside, read_json, write_json

EMPTY_NOTE = "[이 구간은 전사 결과 없음 — 소리가 작거나 말이 없음]"
DRY_RUN_MS = 60_000
MAX_RATE_LIMIT_WAITS = 10  # transcribe.ps1 retried per-minute 429 up to 10 times
LONG_WAIT_S = 900          # a longer provider wait (Groq hourly/daily audio limits) parks the job instead


class Client(Protocol):
    def transcribe(self, path: Path) -> TranscribeResult: ...


@dataclass
class Deps:
    client_factory: Callable[[], Client]
    confirm: Callable[[str], bool]
    clock: Callable[[], datetime] = field(default=lambda: datetime.now().astimezone())
    sleep: Callable[[float], None] = time.sleep
    log: Callable[[str], None] = lambda message: None
    # Silence finder/encoder are injectable so tests can avoid long real audio.
    duration_ms: Callable[[Path], int] = audio.duration_ms
    detect_silences: Callable[[Path, int, int], list] = audio.detect_silences
    make_chunk: Callable[[Path, Path, int, int, str], Path] = audio.make_chunk


def _now_ms(deps: Deps) -> int:
    return int(deps.clock().timestamp() * 1000)


def clock_text(ms: int) -> str:
    """transcribe.ps1 Format-Clock: floor seconds; MM:SS, or HH:MM:SS from one hour."""
    seconds = ms // 1000
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class Job:
    def __init__(self, directory: Path, root: Path):
        self.dir, self.root = Path(directory), Path(root)
        self.state: dict = read_json(self.dir / "state.json") or {}

    @property
    def id(self) -> str:
        return self.state["job_id"]

    def save(self) -> None:
        write_json(ensure_inside(self.dir / "state.json", self.root), self.state)

    @classmethod
    def create(cls, audio_path: Path, root: Path, provider: str, preset: dict, *, course: str,
               dry_run_1min: bool = False, now_ms: int, consent_once: bool = False, mock: bool = False) -> Job:
        audio_path = Path(audio_path).resolve()
        if not audio_path.is_file():
            raise FileNotFoundError(f"오디오 파일이 없습니다: {audio_path}")
        job_id = new_ulid()
        folder = provider_info(provider).job_folder
        job = cls(ensure_inside(app_dir(root) / folder / job_id, root), root)
        job.dir.mkdir(parents=True, exist_ok=False)
        job.state = {
            "job_id": job_id, "provider": provider, "state": "queued", "created_at": now_ms,
            "audio": str(audio_path), "audio_sha256": _sha256(audio_path), "course": course,
            "dry_run_1min": dry_run_1min, "consent_once": consent_once, "mock": mock, "preset": preset, "plan": None,
            "chunks": [],
            "next_eligible_at": None, "next_eligible_estimated": None, "output": None,
            "metrics": {"requests": 0, "auto_retries": 0, "qa_reruns": 0, "rate_limit_waits": 0,
                        "manual_interventions": 0, "chunk_seconds": []},
            "events": [],
        }
        job.save()
        return job

    def event(self, kind: str, at_ms: int, **detail) -> None:
        self.state["events"].append({"at": at_ms, "type": kind, **detail})


def prepare(job: Job, deps: Deps) -> None:
    """Measure, plan silence-aware chunks and encode provider-derived audio (idempotent)."""
    if job.state["plan"] is not None:
        return
    preset, provider = job.state["preset"], job.state["provider"]
    source = Path(job.state["audio"])
    total = deps.duration_ms(source)
    if job.state["dry_run_1min"]:
        plan = plan_chunks(min(total, DRY_RUN_MS), DRY_RUN_MS, 0)
    else:
        max_ms = int(preset["chunk_max_duration_s"]) * 1000
        window_ms = int(preset.get("split", {}).get("window_s", 30)) * 1000
        plan = plan_chunks(total, max_ms, window_ms, lambda lo, hi: deps.detect_silences(source, lo, hi))
    chunks = []
    for chunk in plan.chunks:
        out = ensure_inside(job.dir / "chunks" / f"chunk_{chunk.index + 1:02d}.mp3", job.root)
        if not out.exists():
            deps.make_chunk(source, out, chunk.start_ms, chunk.end_ms, provider)
        chunks.append({
            "index": chunk.index, "start_ms": chunk.start_ms, "end_ms": chunk.end_ms,
            "path": str(out), "bytes": out.stat().st_size if out.exists() else None,
            "state": "queued", "requests": 0, "transient_retries": 0, "qa_reruns": 0,
            "qa": [], "raw_outputs": [], "parsed_path": None, "error": None,
        })
    job.state.update(plan=plan.as_dict(), chunks=chunks, source_duration_ms=total)
    job.save()


def _run_chunk(job: Job, entry: dict, client: Client, ledger: QuotaLedger, deps: Deps) -> None:
    preset = job.state["preset"]
    retry = preset.get("auto_retry", {})
    transient_left = int(retry.get("transient_errors", 2))
    monotonic_left = int(retry.get("qa_monotonic_fail", 1))
    rate_waits = 0
    chunk = Chunk(entry["index"], entry["start_ms"], entry["end_ms"])
    label = f"조각 {chunk.index + 1}/{len(job.state['chunks'])}"
    while True:
        try:
            ledger.check()
        except QuotaWait:
            entry["state"] = "queued"
            raise
        entry["state"] = "running"
        job.save()
        began = time.monotonic()
        try:
            result = client.transcribe(Path(entry["path"]))
        except RateLimited as exc:
            if exc.retry_after_s is not None and exc.retry_after_s > LONG_WAIT_S:
                entry["state"] = "queued"
                job.event("rate_limited", _now_ms(deps), chunk=chunk.index, wait_s=exc.retry_after_s, parked=True)
                raise QuotaWait(_now_ms(deps) + int((exc.retry_after_s + 5) * 1000), "provider_retry_after") from None
            rate_waits += 1
            job.state["metrics"]["rate_limit_waits"] += 1
            if rate_waits > MAX_RATE_LIMIT_WAITS:
                entry.update(state="failed", error=f"분당 한도 대기 {MAX_RATE_LIMIT_WAITS}회 초과")
                return
            wait = exc.retry_after_s + 5 if exc.retry_after_s is not None else min(65 * 2 ** (rate_waits - 1), 600)
            entry["state"] = "rate_limited"
            job.event("rate_limited", _now_ms(deps), chunk=chunk.index, wait_s=wait)
            job.save()
            deps.log(f"  {label}: 분당 한도 — {wait:.0f}초 대기 후 재시도")
            deps.sleep(wait)
            entry["state"] = "retrying"
            continue
        except DailyQuotaExhausted:
            ledger.mark_exhausted()
            entry["state"] = "queued"
            raise QuotaWait(ledger.next_eligible_at()) from None
        except TransientError as exc:
            if transient_left <= 0:
                entry.update(state="failed", error=str(exc))
                job.event("chunk_failed", _now_ms(deps), chunk=chunk.index, error=str(exc))
                return
            transient_left -= 1
            entry["transient_retries"] += 1
            job.state["metrics"]["auto_retries"] += 1
            entry["state"] = "retrying"
            job.save()
            wait = 15 * entry["transient_retries"]
            deps.log(f"  {label}: 일시 오류 — {wait}초 후 재시도 ({exc})")
            deps.sleep(wait)
            continue
        ledger.record()
        entry.setdefault("sent_at", []).append(_now_ms(deps))  # for per-engine usage (hour/day audio limits)
        entry["requests"] += 1
        job.state["metrics"]["requests"] += 1
        job.state["metrics"]["chunk_seconds"].append(round(time.monotonic() - began, 1))
        raw_path = ensure_inside(job.dir / "raw" / f"chunk_{chunk.index + 1:02d}_try{entry['requests']}.json",
                                 job.root)
        write_json(raw_path, result.raw)  # every raw output is kept, including rejected ones
        entry["raw_outputs"].append(str(raw_path))
        local = paragraphs(result)
        issues = _qa(job, local, chunk, result.raw)
        if any(issue.check == "monotonic" for issue in issues) and monotonic_left > 0:
            monotonic_left -= 1
            entry["qa_reruns"] += 1
            job.state["metrics"]["qa_reruns"] += 1
            job.state["metrics"]["auto_retries"] += 1
            entry["state"] = "retrying"
            job.event("qa_rerun", _now_ms(deps), chunk=chunk.index, check="monotonic")
            deps.log(f"  {label}: 타임스탬프 붕괴 — 자동 1회 재실행")
            continue
        _accept(job, entry, chunk, local, issues)
        for issue in issues:
            deps.log(f"  {label}: QA 경고 {issue.check} — {issue.detail}")
        return


def _qa(job: Job, local: list[tuple[int, str]], chunk: Chunk, raw: dict) -> list:
    preset = job.state["preset"]
    issues = check_chunk(local, chunk.duration_ms, tail_lines=int(preset.get("tail_repetition_lines", 40)))
    extra = provider_info(job.state["provider"]).extra_qa(raw) if raw else []
    have = {issue.check for issue in issues}
    return issues + [issue for issue in extra if issue.check not in have]


def _accept(job: Job, entry: dict, chunk: Chunk, local: list[tuple[int, str]], issues: list) -> None:
    parsed = [{"start_ms": add_chunk_offset(ms, chunk.start_ms), "text": text} for ms, text in local]
    parsed_path = ensure_inside(job.dir / "parsed" / f"chunk_{chunk.index + 1:02d}.json", job.root)
    write_json(parsed_path, parsed)
    entry.update(parsed_path=str(parsed_path), qa=[issue.as_dict() for issue in issues],
                 state="qa_warning" if issues else "done", error=None)


def merged_text(job: Job) -> str:
    blocks = []
    for entry in job.state["chunks"]:
        parsed = read_json(Path(entry["parsed_path"]), []) if entry["parsed_path"] else []
        if not parsed:
            blocks.append(f"{clock_text(entry['start_ms'])} \n{EMPTY_NOTE}\n")
            continue
        blocks.append("\n".join(f"{clock_text(p['start_ms'])} \n{p['text']}\n" for p in parsed))
    return "\n\n".join(blocks)


def write_output(job: Job) -> Path:
    """Transcribe-compatible txt (UTF-8 BOM + CRLF) in _lecturehub/out only — never 전사본."""
    suffix = provider_info(job.state["provider"]).output_suffix
    stem = Path(job.state["audio"]).stem + suffix + (" (dryrun-1min)" if job.state["dry_run_1min"] else "")
    out_dir = ensure_inside(app_dir(job.root) / "out", job.root)
    out_dir.mkdir(parents=True, exist_ok=True)
    path, n = out_dir / f"{stem}.txt", 2
    while path.exists():
        path, n = out_dir / f"{stem} ({n}).txt", n + 1
    data = merged_text(job).replace("\r\n", "\n").replace("\n", "\r\n")
    with path.open("xb") as stream:
        stream.write(b"\xef\xbb\xbf" + data.encode("utf-8"))
    return path


def _finish(job: Job, deps: Deps) -> dict:
    state = job.state
    state["output"] = str(write_output(job))
    warned = [c for c in state["chunks"] if c["state"] == "qa_warning"]
    state.update(state="qa_warning" if warned else "done", next_eligible_at=None, next_eligible_estimated=None)
    state["metrics"].update(chunks_total=len(state["chunks"]), chunks_ok=len(state["chunks"]),
                            qa_warning_rate=round(len(warned) / len(state["chunks"]), 3))
    job.event("completed", _now_ms(deps), output=state["output"])
    job.save()
    # Upload chunks are never read again once every chunk is done (2026-11-09); covers run, reparse, live.
    from .chunk_cleanup import prune_after_finish

    prune_after_finish(job)
    return state


def reparse(job: Job, deps: Deps) -> dict:
    """Rebuild parsed chunks and the output from saved raw responses — no API call.

    For parser/QA fixes in the improvement loop (§8.4a) without spending quota.
    Only chunks that already have a raw output are rebuilt; the last try is used.
    """
    for entry in job.state["chunks"]:
        if not entry["raw_outputs"]:
            continue
        chunk = Chunk(entry["index"], entry["start_ms"], entry["end_ms"])
        raw = read_json(Path(entry["raw_outputs"][-1]))
        local = paragraphs(provider_info(job.state["provider"]).parse_raw(raw))
        issues = _qa(job, local, chunk, raw)
        _accept(job, entry, chunk, local, issues)
    job.event("reparsed", _now_ms(deps))
    if any(c["state"] not in ("done", "qa_warning") for c in job.state["chunks"]):
        job.save()
        return job.state
    return _finish(job, deps)


def run(job: Job, deps: Deps) -> dict:
    """Advance the job as far as possible; safe to call again to resume."""
    state = job.state
    if state["state"] in ("done", "qa_warning") and state.get("output"):
        return state
    if state["state"] == "cancelled":  # cancelled by the user (e.g. a duplicate): never sent again
        return state
    provider, course = state["provider"], state["course"]
    consent = ConsentStore(ensure_inside(app_dir(job.root) / "consent.json", job.root))
    if state.get("consent_once") and not state.get("consent_once_used"):
        # The app already showed the notice and got a yes for this recording only (blank course).
        state["consent_once_used"] = True
        job.event("consent_once", _now_ms(deps), course=course)
    elif not state.get("consent_once") and not ensure_consent(consent, provider, course, deps.confirm,
                                                               _now_ms(deps)):
        state["state"] = "waiting_user"
        job.event("consent_declined", _now_ms(deps), course=course)
        job.save()
        return state
    state["state"] = "running"
    job.save()
    prepare(job, deps)
    quota = state["preset"].get("quota", {})
    ledger = QuotaLedger(ensure_inside(app_dir(job.root) / "quota.json", job.root), provider,  # D-054
                         int(quota.get("requests_per_day", 25)),
                         quota.get("personal_requests_per_day"), deps.clock)
    client: Client | None = None
    for entry in state["chunks"]:
        if entry["state"] in ("done", "qa_warning"):
            continue
        if entry["state"] == "failed":
            entry.update(state="queued", error=None)  # resume retries only the failed chunk
        client = client or deps.client_factory()
        try:
            _run_chunk(job, entry, client, ledger, deps)
        except QuotaWait as wait:
            remaining = sum(1 for c in state["chunks"] if c["state"] not in ("done", "qa_warning"))
            state.update(state="waiting_for_quota", next_eligible_at=wait.next_eligible_at,
                         next_eligible_estimated=wait.estimated, next_eligible_basis=wait.basis)
            job.event("waiting_for_quota", _now_ms(deps), remaining_chunks=remaining)
            job.save()
            return state
        except PermanentError as exc:
            entry.update(state="failed", error=str(exc))
            state["state"] = "failed"
            job.event("permanent_error", _now_ms(deps), error=str(exc))
            job.save()
            return state
        job.save()
    if any(c["state"] == "failed" for c in state["chunks"]):
        state["state"] = "failed"
        job.save()
        return state
    return _finish(job, deps)


def level1(state: dict) -> str:
    """Screen wording (§6.3 Level 1)."""
    done = sum(1 for c in state.get("chunks", []) if c["state"] in ("done", "qa_warning"))
    total = len(state.get("chunks", []))
    return {
        "queued": "준비됨", "running": f"처리 중({done}/{total})", "waiting_for_quota": "한도 대기",
        "waiting_user": "확인 필요(동의)", "qa_warning": "검토 필요", "done": "완료", "failed": "실패",
    }.get(state.get("state"), state.get("state", "?"))
