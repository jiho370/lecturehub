"""Live transcription (D-059, user decision 2026-09-29): transcribe a recording while class goes on.

The recorder writes one-minute WAV segments. Whenever 20 of them (the provider's chunk length) are
complete, that part is encoded and sent — the same request, quota ledger, retries and QA as a normal
job (job._run_chunk), so the result is an ordinary job. When the recording stops, the last partial
part is sent, the finished master is attached (audio + sha256) and the job completes; the app then
registers it like any other job. If the quota runs out, the job waits and resumes later as usual.

Recording always wins (D-050): this process and its ffmpeg run at below-normal priority with one
thread, and sending pauses while the recorder shows pressure (queue filling, dropped samples,
capture warnings). Local heavy work (PowerPoint, Whisper, bulk indexing) stays forbidden while
recording (AGENTS rule 7); only this remote transcription is allowed.
"""
from __future__ import annotations

import os
import subprocess
import time
import wave
from collections.abc import Callable
from pathlib import Path

from lecturehub.core.ids import new_ulid

from .audio import PROFILES, _tool
from .job import Job, _finish, _run_chunk, _sha256
from .quota import QuotaLedger, QuotaWait
from .store import app_dir, ensure_inside, read_json

BELOW_NORMAL = 0x00004000  # Windows BELOW_NORMAL_PRIORITY_CLASS
CREATE_NO_WINDOW = 0x08000000
QUEUE_PRESSURE = 256       # blocks waiting in the recorder queue (of 2048) → hold off
PRESSURE_WAIT_S = 60


def _segments(session_dir: Path) -> list[Path]:
    return sorted(Path(session_dir).glob("segment_*.wav"))


def _frames(path: Path) -> int:
    with wave.open(str(path), "rb") as stream:
        return stream.getnframes()


def create(root: Path, session_dir: Path, course: str, preset: dict, *, now_ms: int, mock: bool = False) -> Job:
    """A job that fills its chunks from a running session (audio is set when the master exists)."""
    job_id = new_ulid()
    job = Job(ensure_inside(app_dir(root) / "jobs" / job_id, root), root)
    job.dir.mkdir(parents=True, exist_ok=False)
    job.state = {
        "job_id": job_id, "provider": preset["provider"], "state": "running", "created_at": now_ms,
        "audio": None, "audio_sha256": None, "course": course, "dry_run_1min": False, "consent_once": False,
        "mock": mock, "preset": preset, "plan": {"live": True},
        "live": {"session_dir": str(session_dir), "segments_used": 0, "closed": False, "pressure_waits": 0},
        "chunks": [], "next_eligible_at": None, "next_eligible_estimated": None, "output": None,
        "metrics": {"requests": 0, "auto_retries": 0, "qa_reruns": 0, "rate_limit_waits": 0,
                    "manual_interventions": 0, "chunk_seconds": []},
        "events": [],
    }
    job.save()
    return job


def encode_part(segments: list[Path], out: Path, provider: str) -> Path:
    """Concatenate WAV segments and encode with the provider profile — low priority, one thread."""
    out.parent.mkdir(parents=True, exist_ok=True)
    listing = out.with_suffix(".txt")
    listing.write_text("".join(f"file '{p.as_posix()}'\n" for p in segments), encoding="utf-8")
    result = subprocess.run(
        [_tool("ffmpeg"), "-hide_banner", "-nostdin", "-v", "error", "-n", "-threads", "1",
         "-f", "concat", "-safe", "0", "-i", str(listing), "-vn", *PROFILES[provider], str(out)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        creationflags=(BELOW_NORMAL | CREATE_NO_WINDOW) if os.name == "nt" else 0)
    if result.returncode:
        raise RuntimeError(f"조각 인코딩 실패: {result.stderr[-500:].strip()}")
    return out


def add_ready_parts(job: Job, *, final: bool, encoder: Callable = encode_part) -> int:
    """Append chunks for every complete part (preset chunk length) (and the remainder once the recording stopped)."""
    live = job.state["live"]
    session = Path(live["session_dir"])
    per_part = max(1, int(job.state["preset"]["chunk_max_duration_s"]) // 60)
    segments = _segments(session)
    complete = segments if final else segments[:-1]  # the newest segment is still being written
    added = 0
    while True:
        used = live["segments_used"]
        take = complete[used:used + per_part]
        if not take or (len(take) < per_part and not final):
            break
        rate = read_json(session / "session.json", {}).get("sample_rate") or 48000
        start_ms = sum(_frames(p) for p in segments[:used]) * 1000 // rate
        end_ms = start_ms + sum(_frames(p) for p in take) * 1000 // rate
        index = len(job.state["chunks"])
        out = ensure_inside(job.dir / "chunks" / f"chunk_{index + 1:02d}.mp3", job.root)
        if not out.exists():
            encoder(take, out, job.state["provider"])
        job.state["chunks"].append({
            "index": index, "start_ms": start_ms, "end_ms": end_ms, "path": str(out),
            "bytes": out.stat().st_size if out.exists() else None, "state": "queued", "requests": 0,
            "transient_retries": 0, "qa_reruns": 0, "qa": [], "raw_outputs": [], "parsed_path": None, "error": None,
        })
        live["segments_used"] = used + len(take)
        added += 1
        job.save()
    return added


def recorder_pressure(session_dir: Path, baseline: dict) -> str | None:
    """Reason to hold off, from the recorder's live status (queue, drops, capture warnings)."""
    status = read_json(Path(session_dir) / "live.json", {}) or {}
    if status.get("state") != "recording":
        return None
    queue = status.get("queue") or 0
    dropped, warnings = status.get("dropped_samples") or 0, status.get("capture_status_count") or 0
    reason = None
    if queue >= QUEUE_PRESSURE:
        reason = f"녹음 대기열 {queue}"
    elif dropped > baseline.get("dropped", 0) or warnings > baseline.get("warnings", 0):
        reason = f"녹음 누락 {dropped} · 경고 {warnings}"
    baseline.update(dropped=dropped, warnings=warnings)
    return reason


def consent_still_granted(job: Job, now_ms: int) -> bool:
    """Before a live job is resumed: the course's consent must still stand (security review 2026-11-05).

    ``job.run`` asks on every run; a live job was checked only when it started, so consent withdrawn
    while it was paused (``consent audit --apply``, consent.json) would not stop the remaining parts.
    Not granted → the job stops as ``waiting_user`` and nothing is sent."""
    from .consent import ConsentStore, named

    state = job.state
    store = ConsentStore(ensure_inside(app_dir(job.root) / "consent.json", job.root))
    if named(state.get("course") or "") and store.granted(state["provider"], state["course"]):
        return True
    state["state"] = "waiting_user"
    job.event("consent_missing", now_ms, course=state.get("course"), before="live_resume")
    job.save()
    return False


def run_live(job: Job, deps, *, poll_s: float = 20, master_wait_s: float = 900,
             encoder: Callable = encode_part, pressure: Callable = recorder_pressure) -> dict:
    """Follow the session until it stops and is finalized; send parts as they become ready."""
    state, live = job.state, job.state["live"]
    session = Path(live["session_dir"])
    quota = state["preset"].get("quota", {})
    ledger = QuotaLedger(ensure_inside(app_dir(job.root) / "quota.json", job.root), state["provider"],
                         int(quota.get("requests_per_day", 25)), quota.get("personal_requests_per_day"), deps.clock)
    client = None
    baseline: dict = {}
    waiting_quota = state.get("state") == "waiting_for_quota" and bool(state.get("next_eligible_at")) \
        and state["next_eligible_at"] > int(deps.clock().timestamp() * 1000)
    state["state"] = "running"
    while True:
        stopped = (session / "stop.json").exists()
        if stopped and (read_json(session / "stop.json", {}) or {}).get("cause") == "user_discard":
            state["state"] = "cancelled"  # the user threw the recording away while recording
            job.event("cancelled", int(deps.clock().timestamp() * 1000), reason="녹음 버림(녹음 중)")
            job.save()
            return state
        live["heartbeat_at"] = int(deps.clock().timestamp() * 1000)  # a restarted app sees this job is alive
        if not stopped and (reason := pressure(session, baseline)):  # also before encoding (review L3)
            live["pressure_waits"] += 1
            job.event("live_pressure_wait", live["heartbeat_at"], reason=reason, before="encode")
            job.save()
            deps.sleep(PRESSURE_WAIT_S)
            continue
        add_ready_parts(job, final=stopped, encoder=encoder)
        job.save()
        for entry in state["chunks"]:
            if entry["state"] in ("done", "qa_warning") or waiting_quota:
                continue
            if not stopped and (reason := pressure(session, baseline)):
                live["pressure_waits"] += 1
                job.event("live_pressure_wait", int(deps.clock().timestamp() * 1000), reason=reason)
                job.save()
                deps.sleep(PRESSURE_WAIT_S)
                break
            client = client or deps.client_factory()
            try:
                _run_chunk(job, entry, client, ledger, deps)
            except QuotaWait as wait:
                waiting_quota = True
                state.update(next_eligible_at=wait.next_eligible_at, next_eligible_estimated=wait.estimated,
                             next_eligible_basis=wait.basis)
            job.save()
        if stopped:
            break
        deps.sleep(poll_s)
    live["closed"] = True
    master = _wait_master(session, master_wait_s, deps)
    if master is None:  # finalize is slow or failed: keep every sent part; `transcribe resume` attaches it later
        state["state"] = "waiting_master"
        job.event("live_waiting_master", int(deps.clock().timestamp() * 1000))
        job.save()
        return state
    state.update(audio=str(master), audio_sha256=_sha256(master))
    job.save()  # live.closed is on disk before the segments are let go
    from lecturehub.recorder.compact import after_finalize  # P-053: every part is cut, the segments → FLAC

    job.event("live_session_compacted", int(deps.clock().timestamp() * 1000),
              **{k: v for k, v in after_finalize(session, ignore_job=state["job_id"], background=False).items()
                 if k in ("state", "reason", "bytes_freed", "error")})
    if waiting_quota:
        state["state"] = "waiting_for_quota"
        job.save()
        return state
    if any(c["state"] not in ("done", "qa_warning") for c in state["chunks"]):
        state["state"] = "failed"  # `transcribe resume` retries the failed parts
        job.save()
        return state
    return _finish(job, deps)


def _wait_master(session: Path, wait_s: float, deps) -> Path | None:
    deadline = time.monotonic() + wait_s
    while True:
        finals = sorted(session.glob("finalize_*.json"), key=lambda p: p.stat().st_mtime)
        if finals:
            master = Path((read_json(finals[-1], {}) or {}).get("master", ""))
            return master if master.is_file() else None
        if time.monotonic() > deadline:
            return None
        deps.sleep(5)
