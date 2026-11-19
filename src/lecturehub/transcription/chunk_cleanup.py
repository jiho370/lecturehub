"""Delete the upload chunks of finished transcription jobs (user decision 2026-11-09, disk review).

``chunks\\chunk_NN.mp3`` are cut copies of the master recording made only for upload. Once a job is
finished nothing reads them again: resume returns early for a finished job, ``reparse`` rebuilds from
``raw``, the output is merged from ``parsed``, usage/app_ingest read only ``state.json``, and sending a
recording again (``--again``, MAI [실행] again) makes a new job that cuts its own chunks from the master.
So only those MP3 files (and the then-empty ``chunks`` folder) go; ``state.json``, ``manifest.json``,
``raw``, ``parsed``, ``inbox`` and the output stay. The prune is recorded in state.json
(``chunks_pruned_at``, ``chunks_pruned_bytes``).

"Finished" (and therefore safe) is decided from state.json alone, see :func:`finished`.
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from .store import read_json, write_json

MAI = "mai_playground"
DONE = ("done", "qa_warning")
CHUNK_NAME = re.compile(r"chunk_\d+\.mp3", re.IGNORECASE)


def finished(state: dict) -> tuple[bool, str]:
    """(True, "") when no flow will read this job's chunk files again, else (False, why not).

    - Google/Whisper (job.run, reparse) and live jobs: job state done/qa_warning with an output, every
      chunk done/qa_warning (a failed or queued chunk is re-sent by ``resume`` from its chunk file).
      A live job also needs its master attached (``audio``) — before that, ``resume`` may still add parts.
    - MAI: done/qa_warning and every chunk has a result for every run. A ``--partial`` finish keeps the
      chunks, because the missing runs can still be uploaded later (``mai run`` / the skill).
    """
    if not state:
        return False, "state.json 없음"
    if state.get("state") not in DONE:
        return False, f"끝나지 않은 작업({state.get('state')})"
    chunks = state.get("chunks") or []
    if not chunks:
        return False, "조각 기록 없음"
    if state.get("provider") == MAI:
        runs = int(state.get("runs") or 0)
        if runs < 1 or not state.get("outputs"):
            return False, "MAI 결과 파일 없음"
        missing = [c["index"] + 1 for c in chunks
                   if any(str(run) not in (c.get("results") or {}) for run in range(1, runs + 1))]
        if missing:
            return False, f"MAI 실행 결과가 다 모이지 않음(조각 {missing[:5]})"
        return True, ""
    if not state.get("output"):
        return False, "결과 파일 기록 없음"
    if any(c.get("state") not in DONE or not c.get("parsed_path") for c in chunks):
        return False, "끝나지 않은 조각이 있음"
    if state.get("live") and not state.get("audio"):
        return False, "녹음 중 전사: 원본(마스터) 연결 전"
    return True, ""


def chunk_files(job_dir: Path) -> list[Path]:
    """Only regular ``chunk_NN.mp3`` files directly inside ``<job>\\chunks`` (no links, nothing else)."""
    folder = Path(job_dir) / "chunks"
    if not folder.is_dir() or folder.is_symlink():
        return []
    base = folder.resolve()
    return sorted(p for p in folder.iterdir()
                  if CHUNK_NAME.fullmatch(p.name) and p.is_file() and not p.is_symlink() and p.resolve().parent == base)


def prune_state(job_dir: Path, state: dict, *, dry_run: bool = False, now_ms: int | None = None) -> dict:
    """Delete the chunk MP3s of a finished job and note it in ``state`` (the caller saves state.json)."""
    job_dir = Path(job_dir)
    ok, why = finished(state)
    if not ok:
        return {"job_id": state.get("job_id") or job_dir.name, "pruned": False, "reason": why, "files": 0, "bytes": 0}
    files = chunk_files(job_dir)
    sizes = {path: path.stat().st_size for path in files}
    result = {"job_id": state.get("job_id") or job_dir.name, "pruned": not dry_run and bool(files), "reason": "",
              "files": len(files), "bytes": sum(sizes.values())}
    if dry_run:
        result["paths"] = [str(path) for path in files]
        return result
    freed = deleted = 0
    errors = []
    for path in files:
        try:
            path.unlink()
            freed, deleted = freed + sizes[path], deleted + 1
        except OSError as exc:  # e.g. a player still has it open: try again next time
            errors.append(f"{path.name}: {exc}")
    folder = job_dir / "chunks"
    try:
        if folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()
    except OSError:
        pass
    result.update(files=deleted, bytes=freed, pruned=deleted > 0)
    if deleted:
        state["chunks_pruned_at"] = now_ms if now_ms is not None else int(time.time() * 1000)
        state["chunks_pruned_bytes"] = int(state.get("chunks_pruned_bytes") or 0) + freed
    if errors:
        state["chunks_prune_error"] = "; ".join(errors)[:500]
        result["errors"] = errors
    else:
        state.pop("chunks_prune_error", None)
    return result


def prune_chunks(job_dir: Path, *, dry_run: bool = False) -> dict:
    """Prune one job folder from disk: reads state.json, deletes, writes state.json back.

    Returns ``{"job_id", "pruned", "reason", "files", "bytes"}`` (bytes freed; for a dry run, bytes that would be).
    """
    job_dir = Path(job_dir)
    state = read_json(job_dir / "state.json") or {}
    result = prune_state(job_dir, state, dry_run=dry_run)
    if not dry_run and ("chunks_pruned_at" in state or result.get("errors")):
        write_json(job_dir / "state.json", state)
    return result


def prune_after_finish(job) -> dict | None:
    """Called where a job finishes. Never breaks the job: an error is only noted in state.json."""
    try:
        result = prune_state(job.dir, job.state)
    except Exception as exc:  # noqa: BLE001 - cleanup must never fail a finished transcription
        job.state["chunks_prune_error"] = f"{type(exc).__name__}: {exc}"[:500]
        result = None
    try:
        job.save()
    except Exception:  # noqa: BLE001, S110 - the job itself was already saved as finished
        pass
    return result


def prune_root(root: Path, *, dry_run: bool = False) -> dict:
    """Every job folder (jobs, jobs_whisper, jobs_mai): prune the finished ones, skip the rest."""
    from .providers import job_roots

    summary = {"dry_run": dry_run, "jobs": 0, "files": 0, "bytes": 0, "skipped": 0, "errors": 0, "items": []}
    for base in job_roots(root):
        if not base.is_dir():
            continue
        for job_dir in sorted(p for p in base.iterdir() if p.is_dir() and (p / "state.json").is_file()):
            if not chunk_files(job_dir):
                continue  # nothing to free (already pruned, or chunks never made)
            try:
                result = prune_chunks(job_dir, dry_run=dry_run)
            except (OSError, ValueError) as exc:
                result = {"job_id": job_dir.name, "pruned": False, "reason": f"오류: {exc}", "files": 0, "bytes": 0}
                summary["errors"] += 1
            result["folder"] = base.name
            if result["reason"]:
                summary["skipped"] += 1
            else:
                summary["jobs"] += 1
                summary["files"] += result["files"]
                summary["bytes"] += result["bytes"]
                summary["errors"] += bool(result.get("errors"))
            summary["items"].append(result)
    return summary
