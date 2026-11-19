"""Shrink a finished recording session (user decision 2026-11-09, P-053).

After finalize a session holds the same PCM three times: the 1-minute segments, the merged master WAV
(an exact concatenation of the segments) and the compressed master (MP3). This module

1. sends the master WAV to the Recycle Bin once the compressed master was verified (it adds nothing);
2. keeps the segments' PCM losslessly as one FLAC (``segments.flac``): encode, decode it back and compare
   the sha256 of the PCM and the sample count, write ``compact.json`` (rate, per-segment sample counts,
   sha256), and only then send the segment WAVs to the Recycle Bin. ``restore_segments`` rebuilds the
   segment WAVs byte for byte from the FLAC and compact.json.

The segments are left alone while a live transcription job still follows the session (``live.closed``
false): it cuts its upload parts from the segment WAVs. Failures never touch the WAVs.

A drive without a Recycle Bin (USB stick, network drive; owner's decision 2026-11-19): the WAVs that would go
to the bin are deleted for good instead, one by one, under the same conditions (the MP3 master verified, and
with ``keep_lossless`` the FLAC verified too). compact.json and the result name them (``deleted_permanently``)
and the result says ``permanent: True``; a file that cannot be deleted stays and is named (``not_deleted``).
The app's other Recycle Bin users (discard, relink, cleanups) do not do this: on such a drive they keep the files
and report ``NoRecycleBin``. When the drive has a bin and recycling fails, nothing is deleted here either.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from .heartbeat import SessionLock, write_json
from .paths import ensure_writable
from .recovery import read_json, session_sample_rate
from .segments import HEADER_SIZE, inspect_segment, pcm_header, segment_paths

COMPACT = "compact.json"
FLAC = "segments.flac"
BELOW_NORMAL = 0x00004000
CREATE_NO_WINDOW = 0x08000000
BLOCK = 1024 * 1024


def _flags() -> int:
    return (BELOW_NORMAL | CREATE_NO_WINDOW) if os.name == "nt" else 0


def _ffmpeg(ffmpeg: str | None) -> str:
    found = ffmpeg or shutil.which("ffmpeg")
    if not found:
        raise RuntimeError("ffmpeg가 PATH에 없습니다.")
    return found


def _trash(paths: list[Path]) -> None:
    from lecturehub.core.recycle_bin import to_recycle_bin

    to_recycle_bin(paths)


def _without_bin(paths: list[Path]) -> bool:
    """True when every one of these files is on a drive that has no Recycle Bin (core/recycle_bin.py)."""
    from lecturehub.core import recycle_bin

    return bool(paths) and not any(recycle_bin.has_recycle_bin(p) for p in paths)


def _delete_for_good(paths: list[Path]) -> tuple[list[Path], dict[str, str]]:
    """Plain unlink, file by file: (deleted, {name: why it stayed}). Only for this module's own redundant WAVs
    on a drive without a Recycle Bin, after the checks the Recycle Bin path makes."""
    deleted, kept = [], {}
    for path in paths:
        try:
            path.unlink()
        except OSError as exc:
            kept[path.name] = str(exc)
        else:
            deleted.append(path)
    return deleted, kept


def _not_deleted_reason(kept: dict[str, str]) -> str:
    first = next(iter(kept))
    more = f" 외 {len(kept) - 1}개" if len(kept) > 1 else ""
    return f"휴지통이 없는 드라이브에서 WAV 파일을 지우지 못해 남겨 두었습니다: {first}{more}"


def _flac_verified(session_dir: Path) -> bool:
    """segments.flac is in place and compact.json holds the checksum it was verified against."""
    return (Path(session_dir) / FLAC).is_file() and bool(read_json(Path(session_dir) / COMPACT).get("pcm_sha256"))


def verified_report(session_dir: Path) -> dict | None:
    """The newest finalize report that verified a compressed (non-WAV) master that still exists."""
    best = None
    for path in Path(session_dir).glob("finalize_*.json"):
        try:
            report = read_json(path)
        except ValueError:
            continue
        master = Path(report.get("master") or "")
        if (report.get("verified") and master.suffix.lower() not in ("", ".wav") and master.is_file()
                and (best is None or path.stat().st_mtime > best[0])):
            best = (path.stat().st_mtime, report)
    return best[1] if best else None


def _app_dir(session_dir: Path) -> Path | None:
    for parent in Path(session_dir).resolve().parents:
        if parent.name == "_lecturehub":
            return parent
    return None


def open_live_jobs(session_dir: Path, *, ignore: str | None = None) -> list[str]:
    """Live transcription jobs that still read this session's segment WAVs (``live.closed`` false)."""
    app = _app_dir(session_dir)
    if app is None:
        return []
    target = Path(session_dir).resolve()
    found = []
    for state_path in (app / "jobs").glob("*/state.json"):
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        live = state.get("live") if isinstance(state, dict) else None
        if not isinstance(live, dict) or live.get("closed") or state.get("job_id") == ignore:
            continue
        try:
            if Path(live.get("session_dir") or "").resolve() == target:
                found.append(state.get("job_id") or state_path.parent.name)
        except OSError:
            continue
    return found


def _record(session_dir: Path, **fields) -> dict:
    path = Path(session_dir) / COMPACT
    data = read_json(path)
    data.update(fields)
    write_json(ensure_writable(path), data)
    return data


def recycle_master_wav(session_dir: Path, *, trash=_trash) -> dict:
    """Recycle ``master_*.wav`` once a compressed master is verified (the segments hold the same PCM).

    On a drive without a Recycle Bin it is deleted for good instead (state ``deleted``, or ``partial`` when a
    file could not be deleted), but with ``keep_lossless`` on only once the FLAC is verified; before that the
    drive's refusal (``NoRecycleBin``) is raised, as any other failure to recycle is."""
    session_dir = Path(session_dir)
    if verified_report(session_dir) is None:
        return {"state": "skipped", "reason": "압축 마스터 검증 기록 없음", "bytes": 0}
    wavs = sorted(session_dir.glob("master_*.wav"))
    if not wavs:
        return {"state": "none", "bytes": 0}
    if not segment_paths(session_dir) and not (session_dir / FLAC).is_file():
        return {"state": "skipped", "reason": "세그먼트도 FLAC도 없어 병합 WAV를 남김", "bytes": 0}
    size = sum(p.stat().st_size for p in wavs)
    from lecturehub.core import settings

    if _without_bin(wavs) and (not settings.keep_lossless() or _flac_verified(session_dir)):
        sizes = {p.name: p.stat().st_size for p in wavs}
        deleted, kept = _delete_for_good(wavs)
        names = [p.name for p in deleted]
        _record_deleted(session_dir, names, kept)
        return {"state": "partial" if kept else "deleted", "files": names, "deleted_permanently": names,
                "not_deleted": sorted(kept), "permanent": True, "bytes": sum(sizes[n] for n in names),
                **({"reason": _not_deleted_reason(kept)} if kept else {})}
    trash(wavs)  # with keep_lossless and no verified FLAC yet, a drive without a bin refuses here (NoRecycleBin)
    previous = read_json(session_dir / COMPACT).get("masters_recycled") or []
    _record(session_dir, masters_recycled=previous + [p.name for p in wavs])
    return {"state": "recycled", "files": [p.name for p in wavs], "bytes": size}


def _record_deleted(session_dir: Path, names: list[str], kept: dict[str, str], **fields) -> dict:
    """compact.json after a permanent delete: what went for good (added to earlier runs) and what stayed."""
    previous = read_json(Path(session_dir) / COMPACT).get("deleted_permanently") or []
    return _record(session_dir, deleted_permanently=previous + names, not_deleted=dict(kept) or None,
                   delete_error=_not_deleted_reason(kept) if kept else None, **fields)


def _permanent_result(out: dict, session_dir: Path, paths: list[Path], *, flac_bytes: int, done: dict,
                      failed_state: str, **record) -> dict:
    """Delete ``paths`` for good (drive without a bin) and say so in compact.json and in the result."""
    sizes = {p.name: p.stat().st_size for p in paths}
    deleted, kept = _delete_for_good(paths)
    names = [p.name for p in deleted]
    freed = sum(sizes[n] for n in names)
    _record_deleted(session_dir, names, kept, recycled=[], recycle_error=None, **record)
    common = {"deleted_permanently": names, "recycled": [], "permanent": True, "flac_bytes": flac_bytes}
    if kept:
        return {**out, "state": failed_state, "reason": _not_deleted_reason(kept), "not_deleted": sorted(kept),
                "bytes_freed": freed, **common}  # the WAV bytes that went
    return {**out, **done, "bytes_freed": sum(sizes.values()) - flac_bytes, **common}


def _segment_pcm(infos):
    for info in infos:
        with info.path.open("rb") as source:
            source.seek(HEADER_SIZE)
            remaining = info.data_bytes
            while remaining:
                chunk = source.read(min(BLOCK, remaining))
                if not chunk:
                    raise RuntimeError(f"세그먼트가 짧아졌습니다: {info.path.name}")
                remaining -= len(chunk)
                yield chunk


class RecordingStarted(Exception):
    """Another session started recording: stop at once, the WAVs stay (D-050, D-059)."""


def recording_elsewhere(session_dir: Path) -> str | None:
    """Another session next to this one that is recording now (its lock held, no stop.json), else None."""
    from .heartbeat import SessionLock as Lock

    me = Path(session_dir).resolve()
    try:
        folders = list(me.parent.iterdir())
    except OSError:
        return None
    for folder in folders:
        if folder.resolve() == me or not (folder / ".recording.lock").exists() or (folder / "stop.json").exists():
            continue
        try:
            with Lock(folder):
                pass
        except RuntimeError:
            return folder.name
        except OSError:
            continue
    return None


def _encode(ffmpeg: str, infos, rate: int, out: Path, busy=None) -> tuple[str, int]:
    """PCM of the segments → FLAC; returns (sha256, bytes) of the PCM that went in.
    ``busy()`` is asked every 32 MB: a recording that starts meanwhile stops the encode."""
    digest, count = hashlib.sha256(), 0
    process = subprocess.Popen(
        [ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-y", "-f", "s16le", "-ar", str(rate), "-ac", "1",
         "-i", "pipe:0", "-c:a", "flac", "-compression_level", "5", "-threads", "1", "-f", "flac", str(out)],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=_flags())
    try:
        for n, chunk in enumerate(_segment_pcm(infos)):
            if busy and n % 32 == 31 and busy():
                raise RecordingStarted
            digest.update(chunk)
            count += len(chunk)
            process.stdin.write(chunk)
    except BaseException:
        process.kill()
        process.wait()
        raise
    _, error = process.communicate()
    if process.returncode:
        raise RuntimeError(f"FLAC 인코딩 실패: {error.decode('utf-8', 'replace')[-500:].strip()}")
    return digest.hexdigest(), count


def _decode(ffmpeg: str, flac: Path, rate: int, sink=None) -> tuple[str, int]:
    """FLAC → PCM s16le mono; returns (sha256, bytes). ``sink(chunk)`` receives the PCM in order."""
    digest, count = hashlib.sha256(), 0
    process = subprocess.Popen(
        [ffmpeg, "-nostdin", "-v", "error", "-xerror", "-i", str(flac), "-map", "0:a:0", "-f", "s16le",
         "-acodec", "pcm_s16le", "-ac", "1", "-ar", str(rate), "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=_flags())
    try:
        while chunk := process.stdout.read(BLOCK):
            digest.update(chunk)
            count += len(chunk)
            if sink:
                sink(chunk)
    finally:
        process.stdout.close()
        code = process.wait()
    if code:
        raise RuntimeError("FLAC 디코딩 검증에 실패했습니다.")
    return digest.hexdigest(), count


def _wav_pcm_sha(path: Path) -> tuple[str, int]:
    import wave

    digest, count = hashlib.sha256(), 0
    with wave.open(str(path), "rb") as stream:
        while frames := stream.readframes(BLOCK // 2):
            digest.update(frames)
            count += len(frames)
    return digest.hexdigest(), count


def compact_session(session_dir: Path, *, busy=None, **kwargs) -> dict:
    """Master WAV → Recycle Bin; segment WAVs → one verified FLAC, then Recycle Bin. Returns what happened.

    Never while another session records (D-059: no heavy local work during a recording): checked before
    starting, during the encode and before each later step; the session is then marked ``pending`` in
    compact.json and ``after_finalize`` picks it up after the next recording that ends with nothing else
    recording (or ``recorder compact --all``)."""
    session_dir = ensure_writable(Path(session_dir))
    busy = busy or (lambda: recording_elsewhere(session_dir))
    try:
        result = _compact(session_dir, busy=busy, **kwargs)
    except RecordingStarted:
        result = {"session": session_dir.name, "bytes_freed": 0, "state": "deferred_recording",
                  "reason": "다른 녹음이 진행 중이라 미룸"}
    if result["state"] == "deferred_recording":
        _record(session_dir, pending="recording")
    elif result["state"] in ("compacted", "masters_only", "done") and read_json(session_dir / COMPACT).get("pending"):
        _record(session_dir, pending=None)
    return result


def _check(busy) -> None:
    if busy():
        raise RecordingStarted


def _compact(session_dir: Path, *, busy, ffmpeg: str | None = None, trash=_trash,
             ignore_job: str | None = None, dry_run: bool = False) -> dict:
    out = {"session": session_dir.name, "bytes_freed": 0}
    report = verified_report(session_dir)
    if report is None:
        return {**out, "state": "skipped", "reason": "압축 마스터 검증 기록 없음"}
    if not ((session_dir / "stop.json").exists() or read_json(session_dir / "recovery.json").get("recovered")):
        return {**out, "state": "skipped", "reason": "녹음이 끝나지 않은 세션"}
    segments = segment_paths(session_dir)
    masters = sorted(session_dir.glob("master_*.wav"))
    wav_bytes = sum(p.stat().st_size for p in segments + masters)
    if not segments and not masters:
        return {**out, "state": "done" if (session_dir / FLAC).is_file() else "none"}
    if jobs := open_live_jobs(session_dir, ignore=ignore_job):
        return {**out, "state": "deferred", "reason": f"실시간 전사 작업이 조각을 아직 읽는 중({jobs[0]})",
                "wav_bytes": wav_bytes}
    if dry_run:
        return {**out, "state": "would_compact", "segments": len(segments), "masters": len(masters),
                "wav_bytes": wav_bytes}
    _check(busy)
    if not segments:  # FLAC made earlier: only the master WAV is left
        if not (session_dir / FLAC).is_file():
            return {**out, "state": "skipped", "reason": "세그먼트도 FLAC도 없어 병합 WAV를 남김"}
        result = recycle_master_wav(session_dir, trash=trash)
        extra = {k: result[k] for k in ("permanent", "deleted_permanently", "not_deleted", "reason") if k in result}
        return {**out, "state": "partial" if result["state"] == "partial" else "masters_only",
                "bytes_freed": result["bytes"], **extra}
    from lecturehub.core import settings

    if not settings.keep_lossless():  # 2026-11-13: the verified MP3 master is the recording; no lossless copy
        _check(busy)
        names = [p.name for p in segments + masters]
        if _without_bin(segments + masters):  # owner's decision 2026-11-19: the verified MP3 is the recording
            return _permanent_result(out, session_dir, segments + masters, flac_bytes=0, failed_state="partial",
                                     done={"state": "compacted", "wav_bytes": wav_bytes, "segments": len(segments),
                                           "mp3_only": True},
                                     lossless=False)
        try:
            trash(segments + masters)
        except OSError as exc:
            _record(session_dir, recycle_error=str(exc))
            return {**out, "state": "skipped", "reason": str(exc)}
        _record(session_dir, lossless=False, recycled=names, recycle_error=None,
                masters_recycled=(read_json(session_dir / COMPACT).get("masters_recycled") or [])
                + [p.name for p in masters])
        return {**out, "state": "compacted", "bytes_freed": wav_bytes, "wav_bytes": wav_bytes, "flac_bytes": 0,
                "segments": len(segments), "mp3_only": True}
    tool = _ffmpeg(ffmpeg)
    with SessionLock(session_dir):
        rate = session_sample_rate(session_dir)
        infos = [inspect_segment(p, rate, allow_stale_lengths=(i == len(segments) - 1))
                 for i, p in enumerate(segments)]
        if not all(info.header_matches for info in infos):
            return {**out, "state": "skipped", "reason": "WAV 길이 불일치(복구 필요)"}
        samples = sum(info.samples for info in infos)
        if report.get("total_samples") not in (None, samples):
            return {**out, "state": "skipped", "reason": "세그먼트 합이 병합 기록과 다름"}
        flac = ensure_writable(session_dir / FLAC)
        started = time.monotonic()
        if flac.exists():  # an earlier run made and checked the FLAC but could not recycle: check again
            record = read_json(session_dir / COMPACT)
            sha = hashlib.sha256()
            for chunk in _segment_pcm(infos):
                sha.update(chunk)
            _check(busy)
            if (record.get("pcm_sha256") != sha.hexdigest() or record.get("samples") != samples
                    or _decode(tool, flac, rate)[0] != record["pcm_sha256"]):
                return {**out, "state": "skipped", "reason": f"{FLAC}가 남은 세그먼트와 맞지 않음(확인 필요)"}
            for master in masters:
                _check(busy)
                if _wav_pcm_sha(master)[0] != record["pcm_sha256"]:
                    raise RuntimeError(f"병합 WAV가 세그먼트와 다릅니다: {master.name}")
        else:
            part = ensure_writable(session_dir / (FLAC + ".part"))
            try:
                sha, size = _encode(tool, infos, rate, part, busy)
                _check(busy)
                back_sha, back_size = _decode(tool, part, rate)
                if (back_sha, back_size) != (sha, size):
                    raise RuntimeError("FLAC을 풀어 본 PCM이 원본 세그먼트와 다릅니다.")
                for master in masters:  # the merged WAV must be the same PCM, or something else is going on
                    _check(busy)
                    if _wav_pcm_sha(master) != (sha, size):
                        raise RuntimeError(f"병합 WAV가 세그먼트와 다릅니다: {master.name}")
                os.replace(part, flac)
            except BaseException:
                part.unlink(missing_ok=True)
                raise
            record = _record(session_dir, version=1, flac=FLAC, sample_rate=rate, samples=samples,
                             pcm_sha256=sha, flac_bytes=flac.stat().st_size, wav_bytes=wav_bytes,
                             segments=[{"name": i.path.name, "samples": i.samples} for i in infos],
                             compacted_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                             seconds=round(time.monotonic() - started, 1))
    names = [p.name for p in segments + masters]
    _check(busy)  # the FLAC is kept and checked again next time
    if _without_bin(segments + masters):  # the FLAC was verified above: delete the WAVs it replaces
        return _permanent_result(out, session_dir, segments + masters, flac_bytes=record["flac_bytes"],
                                 failed_state="flac_only",
                                 done={"state": "compacted", "wav_bytes": wav_bytes, "segments": len(segments)})
    try:
        trash(segments + masters)
    except OSError as exc:  # the FLAC is verified; the WAVs simply stay until the next run
        _record(session_dir, recycle_error=str(exc))
        return {**out, "state": "flac_only", "reason": str(exc), "flac_bytes": record["flac_bytes"]}
    _record(session_dir, recycled=names, recycle_error=None,
            masters_recycled=(record.get("masters_recycled") or []) + [p.name for p in masters])
    return {**out, "state": "compacted", "bytes_freed": wav_bytes - record["flac_bytes"],
            "wav_bytes": wav_bytes, "flac_bytes": record["flac_bytes"], "segments": len(segments)}


def _background_mode() -> None:
    """Low CPU and disk priority for the rest of this process (the app may start another recording later;
    compaction then stops at its next check, see ``compact_session``)."""
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x00100000)
    except (AttributeError, OSError):
        pass


def after_finalize(session_dir: Path, *, ignore_job: str | None = None, background: bool = True,
                   **kwargs) -> dict:
    """Hook after finalize or when a live job lets go of a session: never raises.

    ``background`` puts the calling process into Windows background mode (low CPU and I/O priority) for
    good — only for processes whose real work is over (the recorder after finalize, a closed live job)."""
    if os.environ.get("LECTUREHUB_NO_AUTO_COMPACT"):  # the test suite (tests/conftest.py)
        return {"state": "off"}
    try:
        if background:
            _background_mode()
        if recording_elsewhere(session_dir):  # e.g. D-065 [바꾸기] started the next course at once
            _record(session_dir, pending="recording")
            return {"state": "deferred_recording", "reason": "다른 녹음이 진행 중이라 미룸"}
        if open_live_jobs(session_dir, ignore=ignore_job):
            result = recycle_master_wav(session_dir, **{k: v for k, v in kwargs.items() if k == "trash"})
            return {"state": "deferred", "master": result}
        result = compact_session(session_dir, ignore_job=ignore_job, **kwargs)
        if result["state"] != "deferred_recording":
            result["pending"] = compact_pending(Path(session_dir).parent, skip=session_dir,
                                                **{k: v for k, v in kwargs.items() if k in ("trash", "ffmpeg")})
        return result
    except Exception as exc:  # noqa: BLE001 - shrinking is optional; the WAVs stay
        return {"state": "error", "error": str(exc)[-500:]}


def compact_pending(recordings: Path, *, skip: Path | None = None, **kwargs) -> list[dict]:
    """Sessions an earlier run put off because something was recording (compact.json ``pending``)."""
    done = []
    skip_path = Path(skip).resolve() if skip else None
    for folder in sorted(Path(recordings).iterdir()):
        try:
            if folder.resolve() == skip_path or not folder.is_dir() or not read_json(folder / COMPACT).get("pending"):
                continue
            result = compact_session(folder, **kwargs)
        except (OSError, RuntimeError, ValueError) as exc:  # one broken session does not stop the others
            result = {"session": folder.name, "state": "error", "error": str(exc)[-300:]}
        done.append(result)
        if result["state"] == "deferred_recording":
            break
    return done


def restore_segments(session_dir: Path, *, ffmpeg: str | None = None) -> dict:
    """Rebuild the segment WAVs from ``segments.flac`` + compact.json (byte-identical PCM and headers)."""
    session_dir = ensure_writable(Path(session_dir))
    record = read_json(session_dir / COMPACT)
    flac = session_dir / (record.get("flac") or FLAC)
    if not flac.is_file() or not record.get("segments"):
        raise ValueError("되돌릴 FLAC 기록이 없습니다(compact.json·segments.flac).")
    if any(session_dir.glob("segment_*.wav")):
        raise ValueError("세그먼트 WAV가 이미 있습니다. 덮어쓰지 않습니다.")
    rate = int(record["sample_rate"])
    plan = [(ensure_writable(session_dir / s["name"]), int(s["samples"]) * 2) for s in record["segments"]]
    state = {"index": -1, "left": 0, "stream": None}

    def advance():
        if state["stream"]:
            state["stream"].close()
        state["index"] += 1
        if state["index"] >= len(plan):
            state["stream"] = None
            return
        path, size = plan[state["index"]]
        state["stream"] = path.open("xb")
        state["stream"].write(pcm_header(rate, size))
        state["left"] = size

    def sink(chunk: bytes):
        view = memoryview(chunk)
        while view:
            while state["stream"] is not None and state["left"] == 0:
                advance()
            if state["stream"] is None:
                raise RuntimeError("FLAC이 기록보다 깁니다.")
            take = min(len(view), state["left"])
            state["stream"].write(view[:take])
            state["left"] -= take
            view = view[take:]

    with SessionLock(session_dir):
        advance()
        try:
            sha, size = _decode(_ffmpeg(ffmpeg), flac, rate, sink)
            while state["stream"] is not None and state["left"] == 0:
                advance()
            if state["stream"] is not None or sha != record["pcm_sha256"]:
                raise RuntimeError("FLAC에서 되살린 PCM이 기록과 다릅니다.")
        except BaseException:
            if state["stream"]:
                state["stream"].close()
            for path, _ in plan:
                path.unlink(missing_ok=True)
            raise
    _record(session_dir, restored_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    return {"state": "restored", "segments": len(plan), "bytes": size}
