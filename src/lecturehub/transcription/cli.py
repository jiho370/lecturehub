"""Track C commands: lecturehub transcribe run|resume|status|plan."""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer

from lecturehub.core.settings import root_option_callback
transcribe_app = typer.Typer(no_args_is_help=True, help="전사(Google 원클릭, MAI 조각 준비)")
RootOption = Annotated[Path, typer.Option("--root", callback=root_option_callback, help="학기 루트(비우면 설정의 학기 폴더, 쓰기는 _lecturehub 안에만)")]


def register(app: typer.Typer) -> None:
    app.add_typer(transcribe_app, name="transcribe")
    from lecturehub.core import edition

    if edition.has("mai"):
        app.add_typer(mai_app, name="mai")
    app.add_typer(captions_app, name="captions")
    if edition.has("qa"):
        app.add_typer(qa_app, name="qa")


mai_app = typer.Typer(no_args_is_help=True, help="MAI playground: prepare → 업로드(run: Aside, 또는 Claude in Chrome 스킬) → import")
captions_app = typer.Typer(no_args_is_help=True, help="실시간 자막: Gemini 3.5 Transcribe Live(기본) · 확정 줄 Chirp 3(선택)")
qa_app = typer.Typer(no_args_is_help=True, help="발표 질의응답 도우미: 자료 + 실시간 자막 → Claude 답 카드")


def _course_for(audio: Path, course: str | None) -> str:
    if course is not None:
        return course
    if audio.parent.parent.name == "녹음본":
        return audio.parent.name
    session = audio.parent / "session.json"
    if session.is_file():
        try:
            return str(json.loads(session.read_text(encoding="utf-8")).get("course") or "")
        except (OSError, ValueError):
            return ""
    return ""


SYNTHETIC_MARKER = "synthetic_root"


def is_synthetic_root(root: Path) -> bool:
    """Only a library that declares itself synthetic (_lecturehub/synthetic_root) may use mock transcription."""
    return (Path(root) / "_lecturehub" / SYNTHETIC_MARKER).is_file()


REMOTE_DURING_RECORDING = {"whisper_groq"}  # D-063 (user 2026-09-29); Google's own path is live.py (D-059)
ALIASES_REMOTE_DURING_RECORDING = {"whisper": "whisper_groq", "groq": "whisper_groq"}


def _lower_priority() -> None:
    """This process and its ffmpeg children run below normal (children inherit BELOW_NORMAL on Windows)."""
    if os.name != "nt":
        return
    import ctypes

    kernel32 = ctypes.windll.kernel32
    kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), 0x00004000)


def _recording_in_progress(root: Path) -> str | None:
    """AGENTS §2.7: no heavy work while a recorder session holds its lock."""
    from lecturehub.recorder.heartbeat import SessionLock

    recordings = root / "_lecturehub" / "recordings"
    if not recordings.is_dir():
        return None
    for folder in recordings.iterdir():
        if (folder / ".recording.lock").exists() and not (folder / "stop.json").exists():
            try:
                with SessionLock(folder):
                    pass
            except RuntimeError:
                return folder.name
    return None


FAKE_ENV = {"gemini_transcribe": "LECTUREHUB_FAKE_GEMINI", "whisper_groq": "LECTUREHUB_FAKE_GROQ"}


def _mock(provider: str) -> bool:
    """Verification transport for this provider (synthetic libraries only; nothing leaves the machine)."""
    return bool(os.environ.get(FAKE_ENV.get(provider, "")))


def _confirm(text: str) -> bool:
    return typer.confirm(text, default=False)


def _deps(provider: str = "gemini_transcribe", preset: dict | None = None):
    from .job import Deps

    if provider == "whisper_groq":
        if _mock(provider):
            from .fake import FakeGroqClient

            return Deps(client_factory=FakeGroqClient, confirm=_confirm, log=typer.echo)
        from .groq import GroqWhisperClient
        from .groq import load_api_key as groq_key

        model = (preset or {}).get("model") or "whisper-large-v3"
        language = (preset or {}).get("language") or "ko"
        return Deps(client_factory=lambda: GroqWhisperClient(groq_key(), model=model, language=language),
                    confirm=_confirm, log=typer.echo)
    from .gemini import GeminiClient, load_api_key

    if _mock(provider):
        from .fake import FakeGeminiClient

        return Deps(client_factory=FakeGeminiClient, confirm=_confirm, log=typer.echo)
    language = (preset or {}).get("language") or "ko"
    return Deps(client_factory=lambda: GeminiClient(load_api_key(), language=language), confirm=_confirm,
                log=typer.echo)


def _open_job(root: Path, job_id: str):
    from .job import Job
    from .providers import find_job_dir

    folder = find_job_dir(root, job_id)
    job = Job(folder, root) if folder else None
    if job is None or not job.state:
        typer.echo(f"작업이 없습니다: {job_id}", err=True)
        raise typer.Exit(1)
    return job


ACTIVE_OR_DONE = ("queued", "running", "retrying", "waiting_for_quota", "waiting_agent", "done", "qa_warning")


def existing_job(root: Path, provider: str, audio: Path) -> dict | None:
    """A job of the same provider for the same recording (path or content) that is not failed or cancelled."""
    from .job import _sha256
    from .providers import provider as provider_info
    from .store import app_dir, read_json

    base = app_dir(root) / provider_info(provider).job_folder
    digest = None
    for state_path in sorted(base.glob("*/state.json"), reverse=True) if base.is_dir() else []:
        state = read_json(state_path) or {}
        if state.get("provider") != provider or state.get("dry_run_1min") or state.get("state") not in ACTIVE_OR_DONE:
            continue
        if state.get("audio") and Path(state["audio"]) == Path(audio):
            return state
        if state.get("audio_sha256"):
            digest = digest or _sha256(Path(audio))
            if state["audio_sha256"] == digest:
                return state
    return None


def _report(state: dict) -> None:
    from .job import level1

    typer.echo(f"작업 {state['job_id']}: {level1(state)} [{state['state']}]")
    for chunk in state.get("chunks", []):
        qa = ", ".join(issue["check"] for issue in chunk["qa"]) or "-"
        typer.echo(f"  조각 {chunk['index'] + 1}: {chunk['start_ms'] // 1000}s~{chunk['end_ms'] // 1000}s "
                   f"{chunk['state']} 요청 {chunk['requests']} QA {qa}" + (f" 오류 {chunk['error']}" if chunk["error"] else ""))
    if state.get("next_eligible_at"):
        at = datetime.fromtimestamp(state["next_eligible_at"] / 1000).astimezone()
        basis = {"provider_pt_midnight": "Gemini 일일 한도, 태평양 시간 자정 기준(D-054)",
                 "personal_local_midnight": "개인 일일 예산, 자정 기준"}.get(state.get("next_eligible_basis"), "추정")
        typer.echo(f"  다음 가능 시점: {at:%Y-%m-%d %H:%M} ({basis})")
        typer.echo(f"  이어서 하기: lecturehub transcribe resume {state['job_id']}")
    if state.get("output"):
        typer.echo(f"  결과: {state['output']}")


@transcribe_app.command()
def run(
    audio: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="녹음 파일(읽기만 한다)")],
    provider: Annotated[str, typer.Option(help="gemini | whisper(Groq)")] = "gemini",
    course: Annotated[str | None, typer.Option(help="과목(동의 단위). 없으면 폴더로 추정")] = None,
    dry_run_1min: Annotated[bool, typer.Option("--dry-run-1min", help="앞 1분만 한 조각으로 시험")] = False,
    root: RootOption = None,
    force: Annotated[bool, typer.Option(help="녹음 중이어도 실행")] = False,
    consent_once: Annotated[bool, typer.Option("--consent-once", hidden=True,
                                               help="앱이 이 녹음에 한해 동의를 받았음(저장 안 함)")] = False,
    again: Annotated[bool, typer.Option("--again", help="같은 엔진으로 이미 전사한 녹음을 다시 보냄")] = False,
    language: Annotated[str | None, typer.Option("--language", help="음성 언어: ko de en ja zh fr es (Whisper는 auto도). "
                                                 "기본: 과목 기본값(_lecturehub\transcribe_languages.json) → ko")] = None,
):
    """원클릭 전사(Google·Whisper): 동의 → 조각 → API → QA → 병합 → _lecturehub\\out\\*.txt"""
    from .gemini import GeminiError
    from .job import Job
    from .job import run as run_job
    from .presets import load_preset

    busy = _recording_in_progress(root)
    remote_ok = ALIASES_REMOTE_DURING_RECORDING.get(provider, provider) in REMOTE_DURING_RECORDING
    if busy and not force and not remote_ok:
        typer.echo(f"녹음 중인 세션이 있습니다({busy}). 녹음이 끝난 뒤 실행하세요(--force로 무시).", err=True)
        raise typer.Exit(1)
    if busy:  # D-063: the remote engine may run during class, gently (below normal, one ffmpeg thread)
        _lower_priority()
    try:
        preset = load_preset(provider)
        if preset.get("mode") != "automatic":
            raise ValueError(f"{preset['provider']}는 자동 전사 제공자가 아닙니다. 'transcribe plan'을 쓰세요.")
        mock = _mock(preset["provider"])
        if mock and not is_synthetic_root(root):
            raise ValueError("모의 전사(LECTUREHUB_FAKE_*)는 합성 표지(_lecturehub\\synthetic_root)가 있는 "
                             "폴더에서만 쓸 수 있습니다. 실제 학기 폴더에서는 쓰지 않습니다.")
        previous = None if (again or dry_run_1min) else existing_job(root, preset["provider"], audio.resolve())
        if previous:  # never send the same recording twice to one engine (ad60211)
            raise ValueError(f"이 녹음은 이미 같은 엔진의 작업이 있습니다({previous['job_id']}, {previous['state']}). "
                             f"이어서 하려면 'transcribe resume {previous['job_id']}', 다시 보내려면 --again")
        from .language import resolve as resolve_language

        course_name = _course_for(audio.resolve(), course)
        preset = {**preset, "language": resolve_language(root, course_name, language, preset["provider"])}
        deps = _deps(preset["provider"], preset)
        if busy:
            from . import audio as audio_tools

            deps.make_chunk = lambda s, o, a, b, p: audio_tools.make_chunk(s, o, a, b, p, threads=1)
        job = Job.create(audio, root, preset["provider"], preset, course=course_name,
                         dry_run_1min=dry_run_1min, now_ms=int(deps.clock().timestamp() * 1000),
                         consent_once=consent_once, mock=mock)
        typer.echo(f"작업 {job.id} 시작: {audio.name} (언어 {preset['language']})")
        state = run_job(job, deps)
    except (OSError, ValueError, RuntimeError, GeminiError) as exc:
        typer.echo(f"전사 오류: {exc}", err=True)
        raise typer.Exit(1) from exc
    _report(state)
    if state["state"] in ("failed", "waiting_user"):
        raise typer.Exit(1)


@transcribe_app.command()
def resume(job_id: str, root: RootOption = None,
           force: Annotated[bool, typer.Option("--force", help="녹음 중 확인 무시")] = False):
    """한도 대기·실패 조각을 이어서 처리한다(끝난 조각은 다시 보내지 않는다)."""
    from .gemini import GeminiError
    from .job import run as run_job

    job = _open_job(root, job_id)
    busy = _recording_in_progress(root)  # AGENTS §2.7, same rule as `run` (B-track guard, audit #1)
    remote_ok = job.state.get("provider") in REMOTE_DURING_RECORDING or bool(job.state.get("live"))
    if busy and not force and not remote_ok:
        typer.echo(f"녹음 중인 세션이 있습니다({busy}). 녹음이 끝난 뒤 실행하세요(--force로 무시).", err=True)
        raise typer.Exit(1)
    if busy:
        _lower_priority()
    job.state["metrics"]["manual_interventions"] += 1
    try:
        if job.state.get("live") and not job.state.get("audio") and job.state["state"] != "cancelled":
            # a live job (review L1/L2): continue from the parts already sent, then attach the master
            from .live import consent_still_granted, run_live

            deps = _deps(job.state["provider"], job.state.get("preset"))
            if consent_still_granted(job, int(deps.clock().timestamp() * 1000)):
                state = run_live(job, deps)
            else:
                typer.echo("이 과목의 전사 동의가 없어 녹음 중 전사를 이어 가지 않습니다.", err=True)
                state = job.state
        else:
            state = run_job(job, _deps(job.state["provider"], job.state.get("preset")))
    except (OSError, ValueError, RuntimeError, GeminiError) as exc:
        typer.echo(f"전사 오류: {exc}", err=True)
        raise typer.Exit(1) from exc
    _report(state)
    if state["state"] in ("failed", "waiting_user"):
        raise typer.Exit(1)


@transcribe_app.command()
def live(
    session_dir: Annotated[Path, typer.Argument(exists=True, file_okay=False, help="녹음 중인 세션 폴더")],
    course: Annotated[str, typer.Option(help="과목(동의한 과목만)")] = "",
    root: RootOption = None,
):
    """녹음 중 전사(D-059): 20분 조각이 찰 때마다 보내고, 녹음이 끝나면 마저 보내 작업을 마친다."""
    from .consent import ConsentStore, named
    from .gemini import GeminiError
    from .live import create, run_live
    from .presets import load_preset
    from .store import app_dir

    if not named(course) or not ConsentStore(app_dir(root) / "consent.json").granted("gemini_transcribe", course):
        typer.echo("녹음 중 전사는 사용자가 동의한 과목만 합니다.", err=True)
        raise typer.Exit(1)
    try:
        from .language import resolve as resolve_language

        preset = load_preset("gemini")
        preset = {**preset, "language": resolve_language(root, course, None, preset["provider"])}
        mock = bool(os.environ.get("LECTUREHUB_FAKE_GEMINI"))
        if mock and not is_synthetic_root(root):
            raise ValueError("모의 전사는 합성 표지가 있는 폴더에서만 쓸 수 있습니다.")
        deps = _deps(preset["provider"], preset)
        job = create(root, session_dir.resolve(), course, preset, now_ms=int(deps.clock().timestamp() * 1000),
                     mock=mock)
        typer.echo(f"녹음 중 전사 {job.id} 시작: {session_dir.name}")
        state = run_live(job, deps)
    except (OSError, ValueError, RuntimeError, GeminiError) as exc:
        typer.echo(f"녹음 중 전사 오류: {exc}", err=True)
        raise typer.Exit(1) from exc
    _report(state)


@transcribe_app.command()
def cancel(job_id: str, reason: Annotated[str, typer.Option("--reason", help="취소 이유")] = "사용자 취소",
           root: RootOption = None):
    """작업을 취소한다. 끝난 조각과 원시 응답은 그대로 두고, 다시 보내지 않는다(이어하기·자동 재시작 제외)."""
    import time

    job = _open_job(root, job_id)
    if job.state["state"] in ("done", "qa_warning"):
        typer.echo("이미 끝난 작업입니다.", err=True)
        raise typer.Exit(1)
    for chunk in job.state.get("chunks", []):
        if chunk.get("state") == "running":
            chunk["state"] = "queued"  # the process was stopped mid-request; nothing will resume it
    job.state["state"] = "cancelled"
    job.event("cancelled", int(time.time() * 1000), reason=reason)
    job.save()
    typer.echo(f"취소함: {job_id}")


@transcribe_app.command()
def status(job_id: str, root: RootOption = None):
    """작업 상태를 보여 준다."""
    _report(_open_job(root, job_id).state)


@transcribe_app.command()
def plan(
    audio: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    provider: Annotated[str, typer.Option(help="mai")] = "mai",
    root: RootOption = None,
):
    """조각만 만든다(업로드 없음). MAI 수동/에이전트 업로드용 manifest.json을 남긴다."""
    from . import audio as audio_tools
    from .chunking import estimated_bytes, parse_bitrate, plan_chunks
    from .presets import load_preset
    from .store import app_dir, ensure_inside, write_json

    preset = load_preset(provider)
    name = preset["provider"]
    source = audio.resolve()
    try:
        total = audio_tools.duration_ms(source)
        max_ms = int(preset["chunk_max_duration_s"]) * 1000
        limit = int(preset.get("max_file_bytes") or 0)
        if limit:  # never plan a chunk whose CBR estimate already exceeds the size limit
            max_ms = min(max_ms, limit * 8000 // parse_bitrate(preset.get("bitrate", "64k")) - 1000)
        window_ms = int(preset.get("split", {}).get("window_s", 30)) * 1000
        chunk_plan = plan_chunks(total, max_ms, window_ms,
                                 lambda lo, hi: audio_tools.detect_silences(source, lo, hi))
        out_dir = ensure_inside(app_dir(root) / "mai" / f"{source.stem}_{datetime.now().astimezone():%Y%m%d_%H%M%S}", root)
        chunks = []
        for chunk in chunk_plan.chunks:
            out = audio_tools.make_chunk(source, out_dir / f"{source.stem} {chunk.index + 1:02d}.mp3",
                                         chunk.start_ms, chunk.end_ms, name)
            size = out.stat().st_size
            if limit and size > limit:
                raise RuntimeError(f"조각 {chunk.index + 1}이 {size} B로 한도 {limit} B를 넘습니다.")
            chunks.append({"index": chunk.index, "path": str(out), "offset_ms": chunk.start_ms,
                           "end_ms": chunk.end_ms, "bytes": size,
                           "estimated_bytes": estimated_bytes(chunk.duration_ms, parse_bitrate(preset["bitrate"])),
                           "expected_result": f"{source.stem} {chunk.index + 1:02d} (MAI).txt"})
        write_json(out_dir / "manifest.json", {
            "audio": str(source), "provider": name, "plan": chunk_plan.as_dict(), "chunks": chunks,
            "runs_per_chunk": preset.get("runs_per_chunk", {}),
        })
    except (OSError, ValueError, RuntimeError) as exc:
        typer.echo(f"조각 준비 오류: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"조각 {len(chunks)}개 → {out_dir}")
    for chunk in chunks:
        typer.echo(f"  {Path(chunk['path']).name}: {chunk['offset_ms'] // 1000}s~, {chunk['bytes']} B")


@transcribe_app.command()
def reparse(job_id: str, root: RootOption = None):
    """저장된 원시 응답으로 파싱·QA·병합을 다시 한다(API 호출 없음, 한도 소모 없음)."""
    from .job import Deps
    from .job import reparse as reparse_job

    job = _open_job(root, job_id)

    state = reparse_job(job, Deps(client_factory=lambda: None, confirm=lambda text: False))
    _report(state)


@transcribe_app.command("prune-chunks")
def prune_chunks(
    root: RootOption = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="지울 조각만 보여 주고 지우지 않음")] = False,
    show_skipped: Annotated[bool, typer.Option("--show-skipped", help="건너뛴 작업과 이유도 출력")] = False,
):
    """끝난 전사 작업의 업로드용 조각 MP3(chunks\\chunk_NN.mp3)를 지운다. 원본·state·원시/파싱 결과는 그대로. JSON 출력."""
    from .chunk_cleanup import prune_root

    busy = _recording_in_progress(root)
    if busy:
        typer.echo(f"녹음 중인 세션이 있습니다({busy}). 녹음이 끝난 뒤 실행하세요.", err=True)
        raise typer.Exit(1)
    summary = prune_root(root, dry_run=dry_run)
    if not show_skipped:
        summary["items"] = [item for item in summary["items"] if not item["reason"]]
    typer.echo(json.dumps(summary, ensure_ascii=False, indent=2))
    if summary["errors"]:
        raise typer.Exit(1)


consent_app = typer.Typer(no_args_is_help=True, help="외부 전송 동의(제공자 × 과목)")
transcribe_app.add_typer(consent_app, name="consent")


@consent_app.command("list")
def consent_list(root: RootOption = None):
    """저장된 동의와 권한(confirmed_by_user / synthetic_test / invalid)을 보여 준다."""
    from .consent import ConsentStore, authority_of

    store = ConsentStore(root / "_lecturehub" / "consent.json")
    for provider, courses in store.entries().items():
        for course, entry in courses.items():
            label = "(이름 있는 모든 과목)" if course == "*" else course or "(과목 미지정)"
            typer.echo(f"{provider} · {label} · {authority_of(course, entry)} · {entry.get('granted_by')}")


@consent_app.command("grant-all")
def consent_grant_all(
    provider: Annotated[list[str], typer.Option("--provider", help="gemini | whisper | mai (여러 번)")],
    by: Annotated[str, typer.Option("--by", help="누가 어떻게 동의했는지(예: user_chat)")],
    reason: Annotated[str, typer.Option("--reason", help="근거(결정 ID·사용자 말)")],
    root: RootOption = None,
):
    """사용자가 모든 과목에 대해 미리 동의했을 때만 쓴다(D-062). 과목 미지정 녹음은 계속 그 녹음마다 묻는다."""
    import time
    from lecturehub.core import edition

    if edition.name() == "public":
        typer.echo("공개판에서는 상시 동의를 사용하지 않습니다. 제공자·과목마다 확인하세요.", err=True)
        raise typer.Exit(1)

    from .consent import ConsentStore
    from .presets import ALIASES

    if not by.startswith("user"):
        typer.echo("동의 주체는 사용자여야 합니다(--by user_…).", err=True)
        raise typer.Exit(1)
    store = ConsentStore(root / "_lecturehub" / "consent.json")
    for name in provider:
        full = ALIASES.get(name, name)
        store.grant_all(full, int(time.time() * 1000), by, reason)
        typer.echo(f"{full} · (이름 있는 모든 과목) · confirmed_by_user · {by}")


@consent_app.command("audit")
def consent_audit(root: RootOption = None,
                  apply: Annotated[bool, typer.Option(help="표시를 실제로 덧붙인다(삭제 없음)")] = False):
    """실제 과목 동의로 취급하면 안 되는 옛 항목(과목 미지정, 합성 시험)을 찾아 표시한다."""
    import time

    from .consent import ConsentStore

    changes = ConsentStore(root / "_lecturehub" / "consent.json").audit(int(time.time() * 1000), apply=apply)
    for change in changes:
        typer.echo(f"{change['provider']} · {change['course'] or '(과목 미지정)'} → {change['authority']} "
                   f"({change['reason']})")
    typer.echo(("표시함" if apply else "표시할 항목") + f" {len(changes)}건")


# ---------------------------------------------------------------- MAI (§8.5, §8.6)
@mai_app.command("prepare")
def mai_prepare(
    audio: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="녹음 파일(읽기만 한다)")],
    course: Annotated[str | None, typer.Option(help="과목(동의 단위). 없으면 폴더로 추정")] = None,
    runs: Annotated[int | None, typer.Option(min=1, max=5, help="조각당 실행 횟수(기본: 프리셋, MAI-N-001)")] = None,
    root: RootOption = None,
    again: Annotated[bool, typer.Option("--again", help="같은 녹음의 MAI 작업이 있어도 새로 만듦")] = False,
    consent_once: Annotated[bool, typer.Option("--consent-once", hidden=True,
                                               help="앱이 이 녹음(과목 미지정)에 한해 사용자 동의를 받았음(저장 안 함)")] = False,
):
    """동의 확인 → 30분 조각 → manifest.json. 업로드는 mai-transcribe 스킬(Claude in Chrome)이 한다."""
    from .job import Deps
    from .mai import create
    from .presets import load_preset

    busy = _recording_in_progress(root)
    if busy:
        typer.echo(f"녹음 중인 세션이 있습니다({busy}). 녹음이 끝난 뒤 실행하세요.", err=True)
        raise typer.Exit(1)
    source = audio.resolve()
    previous = None if again else existing_job(root, "mai_playground", source)
    if previous:
        typer.echo(f"이 녹음은 이미 MAI 작업이 있습니다({previous['job_id']}, {previous['state']}). "
                   f"상태: lecturehub mai status {previous['job_id']}", err=True)
        raise typer.Exit(1)
    preset = load_preset("mai")
    try:
        job = create(source, root, preset, course=_course_for(source, course),
                     runs=runs or int(preset.get("runs_per_chunk", {}).get("default", 2)),
                     deps=Deps(client_factory=lambda: None, confirm=_confirm),
                     confirm=(lambda text: True) if consent_once else _confirm)
    except PermissionError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    except (OSError, ValueError, RuntimeError) as exc:
        typer.echo(f"조각 준비 오류: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"MAI 작업 {job.id}: 조각 {len(job.state['chunks'])}개 × {job.state['runs']}회")
    typer.echo(f"manifest: {job.dir / 'manifest.json'}")


@mai_app.command("import")
def mai_import(
    job_id: str,
    root: RootOption = None,
    source: Annotated[list[Path] | None, typer.Option("--from", help="결과 파일을 찾을 폴더(기본: inbox, 다운로드)")] = None,
):
    """다운로드된 mai_<tag>_<NN>_r<run>.txt를 가져와 오프셋·QA·병합한다(여러 번 해도 같음)."""
    from .job import Deps
    from .mai import import_results, status_lines

    job = _open_job(root, job_id)
    result = import_results(job, Deps(client_factory=lambda: None, confirm=_confirm), source or None)
    typer.echo(f"새로 가져온 결과 {result['imported']}개")
    for line in status_lines(job):
        typer.echo(line)


@mai_app.command("run")
def mai_run(job_id: str, root: RootOption = None):
    """Aside로 남은 조각을 MAI playground에 올리고 결과를 가져온다(Claude 없음, 2026-11-06). 여러 번 해도 같음."""
    import json

    from .job import Deps
    from .mai import status_lines
    from .mai_aside import run

    job = _open_job(root, job_id)
    try:
        result = run(job, Deps(client_factory=lambda: None, confirm=_confirm), say=typer.echo)
    except PermissionError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    for line in status_lines(job):
        typer.echo(line)
    typer.echo(json.dumps({k: v for k, v in result.items() if k != "outputs"}, ensure_ascii=False))
    if not result.get("ok"):
        raise typer.Exit(1)


@mai_app.command("finish")
def mai_finish(job_id: str, root: RootOption = None,
               partial: Annotated[bool, typer.Option("--partial", help="끝난 실행만으로 마무리")] = False):
    """모든 조각이 모인 실행을 전사본으로 마무리한다."""
    from .mai import finish, status_lines

    job = _open_job(root, job_id)
    try:
        finish(job, allow_partial=partial)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    for line in status_lines(job):
        typer.echo(line)


@mai_app.command("status")
def mai_status(job_id: str, root: RootOption = None):
    """조각·실행별 진행 상황."""
    from .mai import status_lines

    for line in status_lines(_open_job(root, job_id)):
        typer.echo(line)


@mai_app.command("window")
def mai_window():
    """MAI playground가 떠 있는 Chrome 창이 최소화돼 있으면 포커스를 뺏지 않고 다시 띄운다(다른 창 뒤에). JSON 한 줄."""
    import json

    from .mai_window import restore

    typer.echo(json.dumps(restore(), ensure_ascii=False))


@mai_app.command("reject")
def mai_reject(job_id: str, chunk: Annotated[int, typer.Option(help="조각 번호(1부터)")],
               message: Annotated[str, typer.Option(help="화면에 뜬 거절 문구")] = "service refused this request",
               root: RootOption = None):
    """스킬이 MAI 거절을 기록한다(§8.6a provider_rejection — 실제 한도를 계속 잰다)."""
    import time

    job = _open_job(root, job_id)
    job.event("provider_rejection", int(time.time() * 1000), chunk=chunk - 1, message=message[:200])
    job.save()
    typer.echo(f"기록함: 조각 {chunk} 거절")


# ---------------------------------------------------------------- live captions (2026-11-06)
@captions_app.command("start")
def captions_start(
    course: Annotated[str, typer.Option(help="과목(동의 단위). 비우면 이번에만 묻는다")] = "",
    source: Annotated[str, typer.Option(help="mic(마이크) 또는 pc(PC 소리)")] = "mic",
    file: Annotated[Path | None, typer.Option("--file", exists=True, dir_okay=False,
                                              help="장치 대신 이 녹음을 실시간처럼 흘림(점검용)")] = None,
    pace: Annotated[float, typer.Option(help="--file 재생 속도(1=실시간, 0=최대한 빨리)")] = 1.0,
    quick: Annotated[bool, typer.Option("--quick/--no-quick", help="빠른 줄(Gemini Live)")] = True,
    confirmed: Annotated[bool, typer.Option("--confirmed/--no-confirmed",
                                         help="확정 줄 Chirp 3도 켬(더 정확, 15~25초 늦음, 월 60분 넘으면 Cloud 크레딧)")] = False,
    project: Annotated[str | None, typer.Option(help="Google Cloud 프로젝트(기본: gcloud 설정)")] = None,
    device: Annotated[str | None, typer.Option(help="장치 이름 일부(기본: Windows 기본 장치)")] = None,
    root: RootOption = None,
    consent_once: Annotated[bool, typer.Option("--consent-once", hidden=True,
                                               help="앱이 과목 미지정에 한해 사용자 동의를 받았음(저장 안 함)")] = False,
    parent_pid: Annotated[int | None, typer.Option("--parent-pid", hidden=True,
                                                   help="이 프로세스가 끝나면 멈춤(앱이 넘김, P-053)")] = None,
):
    """말하는 것을 실시간으로 글로 적는다. 확정 줄은 _lecturehub\\captions\\<날짜 시각> <과목>.txt, 화면용 상태는 live.json."""
    import threading

    from lecturehub.core.parent_watch import watch

    from . import captions
    from .consent import SYNTHETIC, ConsentStore, ensure_consent, named
    from .store import app_dir, ensure_inside

    from lecturehub.core.recording_permission import check as recording_permission

    permission = recording_permission(root, course)
    if not permission["allowed"]:
        typer.echo(permission["reason"], err=True)
        raise typer.Exit(1)
    if permission["warning"]:
        typer.echo(permission["warning"], err=True)
    store = ConsentStore(ensure_inside(app_dir(root) / "consent.json", root))
    now = int(datetime.now().timestamp() * 1000)
    if file is not None and is_synthetic_root(root) and "합성" in course and not store.granted(
            captions.PROVIDER, course, allow_synthetic=True):
        store.grant(captions.PROVIDER, course, now, "합성 음성 점검(에이전트 대리 기록)", by="agent", authority=SYNTHETIC)
    if not store.granted(captions.PROVIDER, course, allow_synthetic=file is not None and is_synthetic_root(root)):
        confirm = (lambda text: True) if (consent_once and not named(course)) else _confirm
        if not ensure_consent(store, captions.PROVIDER, course, confirm, now):
            typer.echo("동의하지 않아 시작하지 않았습니다.", err=True)
            raise typer.Exit(1)
    recognize = key = None
    hints: list[str] = []
    if confirmed:
        project = project or captions.default_project()
        if not project:
            typer.echo("Google Cloud 프로젝트를 찾지 못했습니다(gcloud config set project …). 확정 줄 없이 진행", err=True)
        else:
            try:
                try:  # the course's presentation words (pack "terms", P-051) as Chirp phrase hints
                    from lecturehub.core import edition

                    hints = []
                    if edition.has("qa"):
                        from .qa_packs import terms_for

                        hints = terms_for(root, course)
                except Exception:  # noqa: BLE001 - a broken pack must not stop the captions
                    hints = []
                recognize = captions.chirp_recognizer(project, hints)
            except Exception as exc:  # noqa: BLE001 - missing library or login: say so, go on with the quick line
                typer.echo(f"확정 줄(Chirp 3)을 켜지 못했습니다: {str(exc)[:200]}", err=True)
    try:  # loaded even with --no-quick, so Gemini Live can be switched on later (Q&A helper)
        from .gemini import load_api_key

        key = load_api_key()
    except Exception as exc:  # noqa: BLE001
        if quick:
            typer.echo(f"빠른 줄(Gemini Live)을 켜지 못했습니다: {str(exc)[:200]}", err=True)
    if not recognize and not (key and quick):
        typer.echo("켤 수 있는 엔진이 없습니다.", err=True)
        raise typer.Exit(1)
    if file is not None:
        def feed(out, stop):
            captions.file_source(file, out, stop, pace=pace)
    else:
        mode = {"pc": "loopback", "loopback": "loopback"}.get(source, "mic")

        def feed(out, stop):
            captions.device_source(mode, out, stop, device_name=device)
    typer.echo(f"실시간 자막 시작 · Gemini Live {'켬' if key and quick else '끔'} · 확정 줄 Chirp 3 {'켬' if recognize else '끔'}"
               f"{f' (용어 힌트 {len(hints)}개)' if recognize and hints else ''} · 그만: Ctrl+C 또는 앱의 [그만]")
    stop = threading.Event()
    watch(parent_pid, stop, say=typer.echo)
    result = captions.run(root, course, source=feed, recognize=recognize, gemini_key=key, stop=stop,
                          say=typer.echo, quick_on=quick, hints=len(hints) if recognize else 0)
    typer.echo(json.dumps(result, ensure_ascii=False))


@qa_app.command("start")
def qa_start(
    course: Annotated[str, typer.Option(help="과목(동의 단위) — --pack이면 묶음의 과목")] = "",
    folder: Annotated[Path | None, typer.Option(exists=True, file_okay=False, help="발표 자료 폴더")] = None,
    pack: Annotated[str, typer.Option(help="발표 묶음 id(_lecturehub\\qa\\packs\\<id>, P-046)")] = "",
    model: Annotated[str, typer.Option(help="Claude 모델(sonnet·opus·haiku)")] = "sonnet",
    root: RootOption = None,
    parent_pid: Annotated[int | None, typer.Option("--parent-pid", hidden=True,
                                                   help="이 프로세스가 끝나면 멈춤(앱이 넘김, P-053)")] = None,
):
    """자료를 Claude 세션에 넣어 두고, [HELP](qa ask)마다 실시간 자막의 최근 말로 답 카드를 만든다."""
    import threading

    from lecturehub.core.parent_watch import watch

    from . import qa_assist, qa_packs
    from .consent import ConsentStore, ensure_consent
    from .store import app_dir, ensure_inside

    if pack:
        try:
            course = qa_packs.load(root, pack)["course"]
        except ValueError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1) from exc
    elif not (course and folder):
        typer.echo("--pack, 또는 --course와 --folder를 주세요", err=True)
        raise typer.Exit(2)
    store = ConsentStore(ensure_inside(app_dir(root) / "consent.json", root))
    if not store.granted(qa_assist.PROVIDER, course):
        if not ensure_consent(store, qa_assist.PROVIDER, course, _confirm, int(datetime.now().timestamp() * 1000)):
            typer.echo("동의하지 않아 시작하지 않았습니다.", err=True)
            raise typer.Exit(1)
    stop = threading.Event()
    watch(parent_pid, stop, say=typer.echo)
    result = qa_assist.run(root, course, folder, model=model, stop=stop, say=typer.echo, pack_id=pack or None)
    typer.echo(json.dumps(result, ensure_ascii=False))
    if not result.get("ok"):
        raise typer.Exit(1)


@qa_app.command("ask")
def qa_ask(root: RootOption = None):
    """방금까지 받아 적은 말로 답 카드를 하나 만든다."""
    from . import qa_assist

    qa_assist.request_answer(root)
    typer.echo("요청을 남겼습니다")


@qa_app.command("stop")
def qa_stop(root: RootOption = None):
    """도우미를 끝낸다."""
    from . import qa_assist

    qa_assist.request_stop(root)
    typer.echo("멈춤 요청을 남겼습니다")


@captions_app.command("stop")
def captions_stop(root: RootOption = None):
    """도는 실시간 자막을 끝낸다(마지막 조각까지 보내고 멈춤)."""
    from . import captions

    captions.request_stop(root)
    typer.echo("멈춤 요청을 남겼습니다")


@captions_app.command("status")
def captions_status(root: RootOption = None):
    """live.json(프로세스가 없으면 stopped). JSON 한 줄."""
    from . import captions

    typer.echo(json.dumps(captions.status(root), ensure_ascii=False))
