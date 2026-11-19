"""MAI playground, agent-assisted (§8.5, §8.6, §8.6a): prepare → (Claude in Chrome uploads) → import.

- ``prepare``: consent (mai_playground × course, asked by this command, never by the agent) →
  30-min 40 kbps chunks → ``manifest.json`` for the mai-transcribe skill. State ``waiting_agent``.
- The skill uploads each chunk ``runs`` times (MAI-N-001: default 2) and saves each result as
  ``mai_<tag>_<NN>_r<run>.txt`` in the Downloads folder (or the job's ``inbox``).
- ``import_results``: copies each result file unchanged into the job (``raw``), parses it chunk-local,
  adds the measured chunk offset, runs QA and writes one output per complete run
  (``<stem> (MAI).txt``, ``<stem> (MAI 2).txt`` …). The app decides completion from files + QA,
  never from the agent's word (AGENT-003).
"""
from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path

from lecturehub.core.timeutil import add_chunk_offset

from .consent import CONFIRMED, ConsentStore, authority_of, ensure_consent, named
from .job import Deps, Job, _now_ms, clock_text, prepare
from .qa import QAIssue, check_chunk
from .store import app_dir, ensure_inside, read_json, write_json

PROVIDER = "mai_playground"
EARLY_STOP_RATIO = 0.85   # a run with < 85% of the best run's text for the same chunk: early stop suspected (9/9)


def downloads_dir() -> Path:
    return Path(os.environ.get("USERPROFILE") or Path.home()) / "Downloads"


def tag_of(job: Job) -> str:
    return job.id[-6:].lower()


def result_name(job: Job, index: int, run: int) -> str:
    return f"mai_{tag_of(job)}_{index + 1:02d}_r{run}.txt"


def create(audio: Path, root: Path, preset: dict, *, course: str, runs: int, deps: Deps,
           confirm: Callable[[str], bool]) -> Job:
    """Ask consent first; nothing is encoded or prepared for upload before a yes."""
    store = ConsentStore(ensure_inside(app_dir(root) / "consent.json", root))
    if not ensure_consent(store, PROVIDER, course, confirm, _now_ms(deps)):
        raise PermissionError("동의하지 않아 MAI 조각을 만들지 않았습니다.")
    courses = store.entries().get(PROVIDER, {})
    entry = courses.get(course) if named(course) else None
    if entry:
        authority = authority_of(course, entry)
    elif named(course) and store.granted(PROVIDER, course):
        authority = CONFIRMED  # the user's standing consent for every named course (D-062)
    else:
        authority = "once_for_this_recording"
    job = Job.create(audio, root, PROVIDER, preset, course=course, now_ms=_now_ms(deps),
                     consent_once=not named(course))
    job.state["consent"] = {"provider": PROVIDER, "course": course, "authority": authority,
                            **({"scope": "all_named_courses"} if named(course) and not entry else {})}
    job.state["runs"] = runs
    job.event("consent_checked", _now_ms(deps), **job.state["consent"])
    prepare(job, deps)
    limit = int(job.state["preset"].get("max_file_bytes") or 0)
    too_big = [c for c in job.state["chunks"] if limit and (c["bytes"] or 0) > limit]
    if too_big:  # the playground refuses > 10 MB (§3-3); never hand the agent a file that will fail
        job.state["state"] = "failed"
        job.save()
        raise RuntimeError(f"조각 {too_big[0]['index'] + 1}이 {too_big[0]['bytes']} B로 한도 {limit} B를 넘습니다.")
    for entry_ in job.state["chunks"]:
        entry_.update(state="waiting_agent", results={})
    job.state["state"] = "waiting_agent"
    job.save()
    write_manifest(job)
    return job


def write_manifest(job: Job) -> Path:
    chunks = [{"index": c["index"], "path": c["path"], "offset_ms": c["start_ms"], "end_ms": c["end_ms"],
               "offset_text": clock_text(c["start_ms"]), "bytes": c["bytes"],
               "expected_results": [result_name(job, c["index"], run) for run in range(1, job.state["runs"] + 1)],
               "done_runs": sorted(int(r) for r in c.get("results", {}))}
              for c in job.state["chunks"]]
    manifest = {
        "job_id": job.id, "provider": PROVIDER, "url": "https://playground.microsoft.ai",
        "course": job.state["course"], "consent": job.state.get("consent"), "runs": job.state["runs"],
        "mode": "verbatim", "download_dir": str(downloads_dir()), "inbox": str(job.dir / "inbox"),
        "chunks": chunks,
        "import_command": f"lecturehub mai import {job.id}",
    }
    path = ensure_inside(job.dir / "manifest.json", job.root)
    write_json(path, manifest)
    return path


def _find(name: str, folders: list[Path]) -> Path | None:
    for folder in folders:
        candidate = folder / name
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate
    return None


def _decode(data: bytes) -> str:
    from lecturehub.core.textio import decode_text

    return decode_text(data)


def local_paragraphs(text: str) -> list[tuple[int, str, str | None]]:
    """(chunk-local ms, text, speaker) from a MAI result in any known shape."""
    from lecturehub.parsers import TranscriptParseError
    from lecturehub.parsers.mai import parse

    try:
        return [(s.start_ms, s.text, s.speaker) for s in parse(text) if s.text.strip()]
    except TranscriptParseError:
        return []


def import_results(job: Job, deps: Deps, folders: list[Path] | None = None) -> dict:
    """Pick up result files that exist; safe to run again (a stored result is never replaced)."""
    folders = folders or [job.dir / "inbox", downloads_dir()]
    found = 0
    for entry in job.state["chunks"]:
        results = entry.setdefault("results", {})
        for run in range(1, job.state["runs"] + 1):
            if str(run) in results:
                continue
            source = _find(result_name(job, entry["index"], run), folders)
            if source is None:
                continue
            data = source.read_bytes()
            raw = ensure_inside(job.dir / "raw" / f"chunk_{entry['index'] + 1:02d}_run{run}.txt", job.root)
            raw.parent.mkdir(parents=True, exist_ok=True)
            with raw.open("xb") as stream:  # the raw result is kept byte for byte (§6.1)
                stream.write(data)
            local = local_paragraphs(_decode(data))
            issues = check_chunk([(ms, body) for ms, body, _ in local], entry["end_ms"] - entry["start_ms"],
                                 tail_lines=int(job.state["preset"].get("tail_repetition_lines", 40)))
            # MAI blocks are 8-16 minutes long (§3-3): a late last timestamp is not an early stop by itself.
            issues = [i for i in issues if i.check != "coverage"]
            parsed = [{"start_ms": add_chunk_offset(ms, entry["start_ms"]), "text": body, "speaker": speaker}
                      for ms, body, speaker in local]
            parsed_path = ensure_inside(job.dir / "parsed" / f"chunk_{entry['index'] + 1:02d}_run{run}.json",
                                        job.root)
            write_json(parsed_path, parsed)
            results[str(run)] = {"raw": str(raw), "source": str(source), "parsed": str(parsed_path),
                                 "chars": sum(len(body) for _, body, _ in local),
                                 "qa": [i.as_dict() for i in issues], "imported_at": _now_ms(deps)}
            job.event("mai_result_imported", _now_ms(deps), chunk=entry["index"], run=run, source=source.name)
            found += 1
        _compare_runs(entry)
    _compare_other_engines(job)
    _update_state(job)
    job.save()
    write_manifest(job)
    return {"imported": found, "state": job.state["state"], "outputs": job.state.get("outputs") or []}


def _compare_runs(entry: dict) -> None:
    """Early stop shows up as a run with clearly less text than another run of the same chunk."""
    results = entry.get("results", {})
    if len(results) < 2:
        return
    best = max(r["chars"] for r in results.values())
    for result in results.values():
        result["qa"] = [q for q in result["qa"] if q["check"] != "coverage"]
        if best and result["chars"] < best * EARLY_STOP_RATIO:
            result["qa"].append(QAIssue("coverage", f"같은 조각의 다른 실행보다 글자가 {result['chars']}/{best} "
                                                    "— 조기 종료 의심").as_dict())


def _hangul(text: str) -> int:
    return sum(1 for ch in text if "가" <= ch <= "힣")


def _other_engine_counts(job: Job) -> dict[str, list[int]]:
    """Hangul syllables per MAI chunk window in finished Google/Whisper outputs of the same recording."""
    from lecturehub.parsers import parse_file

    from .providers import PROVIDERS

    counts: dict[str, list[int]] = {}
    for info in PROVIDERS.values():
        if info.name == PROVIDER:
            continue
        base = app_dir(job.root) / info.job_folder
        for state_path in base.glob("*/state.json") if base.is_dir() else []:
            state = read_json(state_path) or {}
            same = state.get("audio_sha256") == job.state.get("audio_sha256") or state.get("audio") == job.state["audio"]
            if not same or state.get("state") not in ("done", "qa_warning") or not state.get("output") \
                    or state.get("mock") or state.get("dry_run_1min") or not Path(state["output"]).is_file():
                continue
            segments = parse_file(Path(state["output"])).segments
            counts[info.engine] = [sum(_hangul(s.text) for s in segments if c["start_ms"] <= s.start_ms < c["end_ms"])
                                   for c in job.state["chunks"]]
    return counts


def _compare_other_engines(job: Job) -> None:
    """With one MAI run (D-064), an early stop shows up against Google/Whisper for the same chunk window."""
    counts = _other_engine_counts(job)
    if not counts:
        return
    for i, entry in enumerate(job.state["chunks"]):
        ref_engine, ref = max(((name, values[i]) for name, values in counts.items()), key=lambda item: item[1])
        if ref < 200:  # too little speech in the window to judge
            continue
        for result in entry.get("results", {}).values():
            mine = _hangul(" ".join(p["text"] for p in read_json(Path(result["parsed"]), [])))
            result["qa"] = [q for q in result["qa"] if not q["detail"].startswith("다른 엔진")]
            if mine < ref * EARLY_STOP_RATIO:
                result["qa"].append(QAIssue("coverage", f"다른 엔진({ref_engine})보다 한글이 {mine}/{ref}자 "
                                                        "— 조기 종료 의심").as_dict())


def _complete_runs(job: Job) -> list[int]:
    return [run for run in range(1, job.state["runs"] + 1)
            if all(str(run) in c.get("results", {}) for c in job.state["chunks"])]


def _update_state(job: Job) -> None:
    for entry in job.state["chunks"]:
        done = len(entry.get("results", {}))
        entry["state"] = "waiting_agent" if done < job.state["runs"] else (
            "qa_warning" if any(r["qa"] for r in entry["results"].values()) else "done")
        entry["qa"] = [q for r in entry.get("results", {}).values() for q in r["qa"]]
    if len(_complete_runs(job)) == job.state["runs"] and job.state["state"] not in ("done", "qa_warning"):
        finish(job)


def finish(job: Job, *, allow_partial: bool = False) -> dict:
    """Write one output per complete run. With allow_partial, runs that never completed are dropped."""
    runs = _complete_runs(job)
    if not runs:
        raise ValueError("모든 조각의 결과가 모인 실행이 없습니다.")
    if len(runs) < job.state["runs"] and not allow_partial:
        raise ValueError(f"실행 {job.state['runs']}회 중 {len(runs)}회만 끝났습니다(--partial로 마무리).")
    outputs = list(job.state.get("outputs") or [])
    written = {Path(p).name for p in outputs}
    for run in runs:
        name = f"{Path(job.state['audio']).stem} (MAI{'' if run == 1 else f' {run}'}).txt"
        if name in written:
            continue
        outputs.append(str(_write_run(job, run, name)))
    warned = any(r["qa"] for c in job.state["chunks"] for key, r in c.get("results", {}).items() if int(key) in runs)
    job.state.update(outputs=outputs, output=outputs[0], state="qa_warning" if warned else "done",
                     runs_completed=runs)
    job.event("completed", int(time.time() * 1000), outputs=outputs)
    job.save()
    if len(runs) == job.state["runs"]:  # a --partial finish keeps the chunks: missing runs may still be uploaded
        from .chunk_cleanup import prune_after_finish

        prune_after_finish(job)
    return job.state


def _write_run(job: Job, run: int, name: str) -> Path:
    blocks = []
    for entry in job.state["chunks"]:
        parsed = read_json(Path(entry["results"][str(run)]["parsed"]), [])
        if not parsed:
            blocks.append(f"{clock_text(entry['start_ms'])} \n[이 구간은 전사 결과 없음]\n")
        for p in parsed:
            label = f"[{p['speaker']}] " if p.get("speaker") else ""
            blocks.append(f"{clock_text(p['start_ms'])} \n{label}{p['text']}\n")
    out_dir = ensure_inside(app_dir(job.root) / "out", job.root)
    out_dir.mkdir(parents=True, exist_ok=True)
    path, n = out_dir / name, 2
    while path.exists():
        path, n = out_dir / f"{Path(name).stem} ({n}).txt", n + 1
    data = "\n".join(blocks).replace("\r\n", "\n").replace("\n", "\r\n")
    with path.open("xb") as stream:
        stream.write(b"\xef\xbb\xbf" + data.encode("utf-8"))
    return path


def status_lines(job: Job) -> list[str]:
    lines = [(f"MAI 작업 {job.id}: {job.state['state']} (과목 {job.state['course'] or '미지정'}, "
              f"실행 {job.state['runs']}회)")]
    for entry in job.state["chunks"]:
        runs = sorted(entry.get("results", {}))
        qa = sorted({q["check"] for q in entry.get("qa", [])}) or ["-"]
        lines.append(f"  조각 {entry['index'] + 1}: {clock_text(entry['start_ms'])}~ 결과 {len(runs)}/{job.state['runs']} "
                     f"QA {', '.join(qa)}")
    for output in job.state.get("outputs") or []:
        lines.append(f"  결과: {output}")
    return lines
