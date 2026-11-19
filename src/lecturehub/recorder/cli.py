"""A-1 recorder commands, registered without modifying the core CLI."""
from __future__ import annotations

import json
import shutil
import signal
import time
from enum import Enum
from pathlib import Path
from typing import Annotated

import typer

recorder_app = typer.Typer(no_args_is_help=True, help="WASAPI 공유 모드 녹음·복구")


class Mode(str, Enum):
    mic = "mic"
    loopback = "loopback"


def register(app: typer.Typer) -> None:
    app.add_typer(recorder_app, name="recorder")


@recorder_app.command()
def devices():
    """WASAPI 입력과 loopback의 현재 장치 ID를 표시한다."""
    from .devices import list_devices
    try:
        found = list_devices()
    except (OSError, RuntimeError, ValueError) as exc:
        typer.echo(f"장치 조회 실패: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo("ID  mode      native Hz  channels  default  device")
    for device in found:
        mode = "loopback" if device.is_loopback else "mic"
        typer.echo(f"{device.index:<3} {mode:<9} {device.sample_rate:<10} {device.channels:<9} "
                   f"{'*' if device.is_default else '-':<8} {device.name}")


# How each stop request maps to StopReport (cause, certainty). A closed console
# window is almost always the user, but nothing proves who closed it.
_STOP_CAUSES = {
    "ctrl_c": ("user_stop", "confirmed"),
    "ctrl_break": ("user_stop", "confirmed"),
    "console_close": ("user_stop", "estimated"),
    "logoff": ("system_shutdown", "confirmed"),
    "system_shutdown": ("system_shutdown", "confirmed"),
}


def _fit(line: str, columns: int) -> str:
    """Pad/cut to the console width so a carriage return redraws one line (Hangul: 2 columns)."""
    import unicodedata

    width, kept = 0, []
    for char in line:
        step = 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
        if width + step > columns:
            break
        kept.append(char)
        width += step
    return "".join(kept) + " " * (columns - width)


def _format_level(value: float) -> str:
    return "  -inf" if value == float("-inf") else f"{value:6.1f}"


def _run_session(source, session, *, duration: float | None, title: str | None = None) -> dict:
    """Record until Ctrl+C/Ctrl+Break, console close, source end or ``duration``.

    stop.json is written on every path that leaves this function; a console
    close waits (bounded) for it before Windows ends the process.
    """
    from .console import CloseGuard, set_title
    from .power import KeepAwake

    requested: list[str] = []

    def request_stop(trigger: str) -> None:
        if not requested:
            requested.append(trigger)

    handlers = {signal.SIGINT: lambda signum, frame: request_stop("ctrl_c")}
    if hasattr(signal, "SIGBREAK"):
        handlers[signal.SIGBREAK] = lambda signum, frame: request_stop("ctrl_break")
    previous = {signum: signal.signal(signum, handler) for signum, handler in handlers.items()}
    started = False
    try:
        with CloseGuard(request_stop) as guard, KeepAwake() as awake:  # K6: no sleep while recording
            session.start()
            started = True
            typer.echo("잠자기 방지: " + ("켜짐" if awake.active else "켜지 못함(전원 설정 확인)")
                       + " · 노트북 덮개를 닫으면 녹음이 멈출 수 있습니다")
            begin = time.monotonic()
            next_meter = begin
            cause, certainty, trigger = "unknown", "unknown", None
            while True:
                now = time.monotonic()
                if requested:
                    trigger = requested[0]
                    cause, certainty = _STOP_CAUSES[trigger]
                    break
                if session.error or source.error or not source.active:
                    break
                if duration is not None and now - begin >= duration:
                    cause, certainty, trigger = "scheduled_stop", "confirmed", "duration"
                    break
                if now >= next_meter:
                    peak, rms, quiet_warning = session.level_snapshot()
                    elapsed = int(now - begin)
                    clock = f"{elapsed // 3600}:{elapsed // 60 % 60:02d}:{elapsed % 60:02d}"
                    warning = " | 소리 매우 작음/오디오 데이터 없음 (30초 이상)" if quiet_warning else ""
                    if session.dropped_samples or session.status_count:
                        warning += (f" | 캡처 누락/상태 경고: {session.dropped_samples} samples/"
                                    f"{session.status_count}")
                    columns = max(40, shutil.get_terminal_size((100, 25)).columns - 1)
                    line = f"{clock} peak {_format_level(peak)} / RMS {_format_level(rms)} dBFS{warning}"
                    typer.echo("\r" + _fit(line, columns), nl=False)
                    if title:
                        set_title(f"{title} · {clock}")
                    next_meter = now + 0.5
                time.sleep(0.05)
            report = session.stop(cause, certainty, trigger)
            started = False
            guard.mark_stopped()
            return report
    finally:
        if started:
            session.stop("unknown", "unknown")
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _check_permission(out: Path, course: str) -> None:
    from lecturehub.core.recording_permission import for_output

    permission = for_output(out, course)
    if not permission["allowed"]:
        raise PermissionError(permission["reason"])
    if permission.get("warning"):
        typer.echo(permission["warning"], err=True)


@recorder_app.command()
def record(
    mode: Annotated[Mode, typer.Option(help="mic 또는 loopback")] = Mode.mic,
    device: Annotated[int | None, typer.Option(help="devices에 표시된 ID")] = None,
    device_name: Annotated[str | None, typer.Option(
        help='장치 이름 일부(대소문자 무시). 예: "마이크 배열". ID가 바뀌어도 된다')] = None,
    out: Annotated[Path, typer.Option(help="세션 폴더를 만들 위치")] = Path("_lecturehub/recordings"),
    course: Annotated[str, typer.Option(help="과목명")] = "",
    quiet_dbfs: Annotated[float, typer.Option(min=-120, max=0, help="D-009 임시 RMS dBFS 기준")] = -50,
    duration: Annotated[float | None, typer.Option(min=0.1, help="시험용 자동 종료(초)")] = None,
):
    """녹음한다. Ctrl+C로 안전하게 종료하고 WAV 세그먼트를 남긴다."""
    from .capture import WasapiSource
    from .session import RecordingSession

    if device is not None and device_name is not None:
        typer.echo("--device와 --device-name 중 하나만 쓰세요.", err=True)
        raise typer.Exit(2)
    try:
        _check_permission(out, course)
        source = WasapiSource(mode.value, device, device_name=device_name)
        session = RecordingSession(source, out, mode=mode.value, course=course, quiet_dbfs=quiet_dbfs,
                                   course_source="user_pick" if course else "unknown")
        typer.echo(f"장치: {source.device_name}\n공유 모드 · {source.sample_rate} Hz · Ctrl+C로 종료")
        report = _run_session(source, session, duration=duration)
        typer.echo(f"\n세션: {session.session_dir}\n" + json.dumps(report, ensure_ascii=False))
    except (OSError, RuntimeError, ValueError) as exc:
        typer.echo(f"\n녹음 오류: {exc}", err=True)
        raise typer.Exit(1) from exc
    if report["partial"] or report["cause"] == "unknown":
        raise typer.Exit(1)


@recorder_app.command("class")
def class_(
    config: Annotated[Path | None, typer.Option(
        help=r"설정 파일(기본: %APPDATA%\lecturehub\recorder.toml)")] = None,
    course: Annotated[str | None, typer.Option(help="과목명(없으면 시간표로 추정, 애매하면 빈칸)")] = None,
    duration: Annotated[float | None, typer.Option(min=0.1, help="시험용 자동 종료(초)")] = None,
    no_finalize: Annotated[bool, typer.Option(help="끝난 뒤 병합(finalize)을 건너뛴다")] = False,
    pause: Annotated[bool, typer.Option(help="끝난 뒤 Enter를 기다린다(바로가기용)")] = False,
):
    """수업 원클릭 녹음: 설정 파일의 장치·저장 위치로 녹음하고, 끝나면 병합한다."""
    from datetime import datetime

    from .capture import WasapiSource
    from .classconfig import current_course, load_config
    from .console import quick_edit_disabled, set_title
    from .devices import DeviceError
    from .session import RecordingSession

    exit_code = 0
    with quick_edit_disabled():
        try:
            settings = load_config(config)
            course_source = "user_pick"  # --course given explicitly
            if course is None:
                course, course_source = "", "timetable_auto"
                if settings.timetable is not None:
                    try:
                        course = current_course(settings.timetable, datetime.now().astimezone(),
                                                settings.lead_minutes)
                    except (OSError, ValueError) as exc:
                        typer.echo(f"시간표를 읽지 못해 과목을 비워 둡니다: {exc}", err=True)
            _check_permission(settings.out_dir, course)
            _recover_interrupted(settings.out_dir)
            try:
                source = WasapiSource(settings.mode, device_name=settings.device_name)
            except DeviceError as exc:
                if settings.device_name is None:
                    raise
                # Recording something beats recording nothing; say so loudly.
                typer.echo(f"!! 설정한 장치를 찾지 못했습니다: {exc}\n!! 기본 장치로 녹음합니다.", err=True)
                source = WasapiSource(settings.mode)
            session = RecordingSession(source, settings.out_dir, mode=settings.mode, course=course,
                                       course_source=course_source if course else "unknown",
                                       quiet_dbfs=settings.quiet_dbfs)
            label = course or "(과목 미지정)"
            title = f"수업 녹음 · {label}"
            set_title(title)
            typer.echo("=" * 64)
            typer.echo(f" 과목   : {label}")
            typer.echo(f" 장치   : {source.device_name}  ({source.sample_rate} Hz, 공유 모드)")
            typer.echo(f" 저장   : {settings.out_dir}")
            typer.echo(" 끝낼 때: Ctrl+C   (창을 닫아도 저장은 되지만 Ctrl+C를 권장)")
            typer.echo("=" * 64)
            report = _run_session(source, session, duration=duration, title=title)
            set_title(f"{title} · 종료")
            typer.echo(f"\n\n녹음 종료: {report['cause']} ({report['certainty']}), "
                       f"{report['total_ms'] // 1000}초\n세션: {session.session_dir}")
            if report["partial"]:
                exit_code = 1
                typer.echo(f"!! 불완전 녹음으로 표시됨: {report.get('error') or '누락/상태 경고'}", err=True)
            if not no_finalize and report["total_samples"]:
                from .finalize import finalize_session

                typer.echo("병합 중(세그먼트는 보존)...")
                result = finalize_session(session.session_dir)
                typer.echo(f"병합 완료: {result['master']}\n"
                           f"  길이 {result['master_total_ms'] // 1000}초, 세그먼트 합과 차이 "
                           f"{result['duration_difference_ms']} ms")
                from .compact import after_finalize

                shrink = after_finalize(session.session_dir, background=False)
                typer.echo(f"WAV 정리: {shrink.get('state')} {shrink.get('reason') or shrink.get('error') or ''}")
        except (OSError, RuntimeError, ValueError) as exc:
            typer.echo(f"\n녹음 오류: {exc}", err=True)
            exit_code = 1
    if pause:
        try:
            input("\nEnter를 누르면 창이 닫힙니다.")
        except (EOFError, KeyboardInterrupt):
            pass
    if exit_code:
        raise typer.Exit(exit_code)


def _recover_interrupted(out_dir: Path) -> None:
    """Repair headers of earlier sessions that ended without stop.json.

    This only rewrites two length fields, so it is safe before class. Merging
    is heavy work and is left for after class.
    """
    from .recovery import recover_session

    if not out_dir.is_dir():
        return
    for folder in sorted(out_dir.iterdir()):
        if (not folder.is_dir() or not (folder / "session.json").exists()
                or (folder / "stop.json").exists() or (folder / "recovery.json").exists()):
            continue
        try:
            report = recover_session(folder)
        except (OSError, RuntimeError, ValueError) as exc:
            typer.echo(f"!! 이전 세션 복구 실패(원본 보존): {folder.name}: {exc}", err=True)
            continue
        typer.echo(f"이전 비정상 종료 세션을 복구했습니다: {folder.name} "
                   f"({report['total_ms'] // 1000}초, 원인 추정 {report['cause']}). "
                   "수업 후 recorder finalize로 병합하세요.")


@recorder_app.command()
def recover(session_folder: Path):
    """비정상 종료 세션의 마지막 WAV를 복구하고 추정 원인을 보고한다."""
    from .recovery import recover_session
    try:
        typer.echo(json.dumps(recover_session(session_folder), ensure_ascii=False, indent=2, default=str))
    except (OSError, RuntimeError, ValueError) as exc:
        typer.echo(f"복구 실패: {exc}", err=True)
        raise typer.Exit(1) from exc


@recorder_app.command()
def compact(
    session_folder: Annotated[Path | None, typer.Argument(help="세션 폴더(생략하면 --all)")] = None,
    all_: Annotated[bool, typer.Option("--all", help="--root의 _lecturehub\\recordings 전체")] = False,
    root: Annotated[Path, typer.Option(help="학기 폴더")] = Path("."),
    dry_run: Annotated[bool, typer.Option("--dry-run", help="바꾸지 않고 할 일만 보기")] = False,
):
    """끝난 녹음의 병합 WAV를 휴지통으로, 1분 조각 WAV를 검증한 FLAC 하나로 바꾼다(P-053). JSON 한 줄."""
    from lecturehub.transcription.cli import _recording_in_progress

    from .compact import compact_session

    if session_folder is None and not all_:
        typer.echo("세션 폴더나 --all을 주세요.", err=True)
        raise typer.Exit(2)
    if not dry_run and _recording_in_progress(root):
        typer.echo("녹음 중에는 하지 않습니다. 녹음이 끝난 뒤 다시 실행하세요.", err=True)
        raise typer.Exit(2)
    folders = [session_folder] if session_folder else sorted(
        p for p in (root / "_lecturehub" / "recordings").glob("*") if (p / "session.json").is_file())
    results = []
    for folder in folders:
        if not dry_run and _recording_in_progress(root):
            results.append({"session": folder.name, "state": "stopped", "reason": "녹음이 시작되어 멈춤"})
            break
        try:
            results.append(compact_session(folder, dry_run=dry_run))
        except (OSError, RuntimeError, ValueError) as exc:
            results.append({"session": folder.name, "state": "error", "error": str(exc)[-300:]})
    total = sum(r.get("bytes_freed", 0) for r in results)
    typer.echo(json.dumps({"sessions": results, "bytes_freed": total}, ensure_ascii=False))


@recorder_app.command("restore-wav")
def restore_wav(session_folder: Path):
    """compact로 FLAC이 된 세션의 1분 조각 WAV를 FLAC에서 그대로 되살린다."""
    from .compact import restore_segments
    try:
        typer.echo(json.dumps(restore_segments(session_folder), ensure_ascii=False))
    except (OSError, RuntimeError, ValueError) as exc:
        typer.echo(f"되살리기 실패: {exc}", err=True)
        raise typer.Exit(1) from exc


@recorder_app.command()
def finalize(session_folder: Path):
    """세그먼트를 보존하면서 병합하고 길이를 확인한다."""
    from .finalize import finalize_session
    try:
        typer.echo(json.dumps(finalize_session(session_folder), ensure_ascii=False, indent=2, default=str))
    except (OSError, RuntimeError, ValueError) as exc:
        typer.echo(f"병합 실패: {exc}", err=True)
        raise typer.Exit(1) from exc
