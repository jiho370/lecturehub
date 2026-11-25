"""Read-only source indexing, library search and status commands."""

import json
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import asdict, is_dataclass
from datetime import date
from pathlib import Path
from typing import Annotated

import typer

from lecturehub.core import db
from lecturehub.core.paths import app_dir
from lecturehub.core.timeutil import format_timestamp
from lecturehub.materials.units import is_chunked


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _date(value: str | None) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter("날짜는 YYYY-MM-DD 형식이어야 합니다.") from exc


@contextmanager
def _library(root: Path):
    owned = _owned_dir(root)
    path = (owned / "lecturehub.sqlite").resolve()
    if not path.is_relative_to(owned):
        raise ValueError("DB 경로가 _lecturehub 밖을 가리킵니다.")
    if db.auto_upgrade_allowed():  # installed build / public edition: its users have no command line to do this
        if not path.is_file():
            from .ingest import index_root

            index_root(root)
        else:
            db.upgrade(path)
    if not path.is_file():
        raise ValueError("색인된 DB가 없습니다. 먼저 lecturehub index <학기 루트>를 실행하세요.")
    with closing(db.connect(path)) as connection:  # refuses a newer DB (D-051)
        current = db.check_schema_version(connection)
        if current < db.known_schema_version():
            raise ValueError(f"DB 스키마 {current}은 이 앱({db.known_schema_version()})보다 오래됐습니다. "
                             "lecturehub db init --root <학기 루트>를 실행하세요.")
        yield connection


def _owned_dir(root: Path) -> Path:
    root = root.resolve()
    owned = app_dir(root)
    if owned.resolve() != owned:
        raise ValueError("_lecturehub가 다른 폴더를 가리키는 링크입니다.")
    return owned


def _html_path(root: Path) -> Path:
    owned = _owned_dir(root)
    destination = (owned / "status.html").resolve()
    if destination.parent != owned:
        raise ValueError("HTML 출력 경로가 학기 루트의 _lecturehub 밖을 가리킵니다.")
    return destination


def _fail(exc: Exception) -> None:
    typer.echo(f"오류: {exc}", err=True)
    raise typer.Exit(code=1) from exc


RootOption = Annotated[
    Path,
    typer.Option("--root", envvar="LECTUREHUB_ROOT", exists=True, file_okay=False,
                 help="학기 루트. 생략하면 LECTUREHUB_ROOT 또는 현재 폴더"),
]


def _recording_busy(root: Path) -> str | None:
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


def register(app: typer.Typer) -> None:
    from . import clova_cli, lecture_cli, organize_cli, quiz_cli, schedule_cli

    schedule_cli.register(app)
    lecture_cli.register(app)
    organize_cli.register(app)
    quiz_cli.register(app)
    clova_cli.register(app)

    @app.command("index")
    def index_command(
        root: Annotated[Path, typer.Argument(exists=True, file_okay=False, readable=True)],
        ffprobe: Annotated[Path | None, typer.Option("--ffprobe", exists=True, dir_okay=False,
                                                   help="ffprobe 실행 파일 경로")] = None,
        as_json: Annotated[bool, typer.Option("--json", help="파일 내용 없이 색인 결과 JSON 출력")] = False,
        force: Annotated[bool, typer.Option("--force", help="녹음 중 확인 무시")] = False,
    ) -> None:
        """원본을 읽고 _lecturehub DB에만 색인한다."""
        from .ingest import index_root

        if not force:  # AGENTS §2.7: a full scan (hashing, ffprobe) waits until the recording is over
            busy = _recording_busy(root)
            if busy:
                typer.echo(f"녹음 중인 세션이 있습니다({busy}). 녹음이 끝난 뒤 실행하세요(--force로 무시).", err=True)
                raise typer.Exit(1)
        try:
            report = index_root(root, ffprobe=str(ffprobe) if ffprobe else None)
        except (OSError, ValueError, sqlite3.Error) as exc:
            _fail(exc)
        if as_json:
            typer.echo(_json(report.as_dict()))
            return
        typer.echo(f"색인 완료: {root.resolve()}")
        for kind, counts in report.stats.items():
            typer.echo(f"{kind}: " + ", ".join(f"{key}={value}" for key, value in counts.items()))
        for label, rate in (("녹음", report.recording_match_rate),
                            ("전사", report.transcript_match_rate)):
            display = "대상 없음" if rate is None else f"{rate:.1%}"
            typer.echo(f"{label} → Lecture 매칭률: {display}")
        for failure in report.failures:
            data = asdict(failure) if is_dataclass(failure) else failure
            typer.echo(f"확인 필요 [{data['kind']}] {data['filename']}: {data['reason']}")
        for warning in report.warnings:
            typer.echo(f"주의: {warning}")

    @app.command("search")
    def search_command(
        query: Annotated[str, typer.Argument(help="검색할 단어나 구절")],
        root: RootOption = Path("."),
        limit: Annotated[int, typer.Option("--limit", min=1, max=500)] = 50,
        as_json: Annotated[bool, typer.Option("--json")] = False,
    ) -> None:
        """전사·자료 쪽 검색. 짧은 질의는 LIKE로 찾는다."""
        from .search import search

        try:
            with _library(root) as connection:
                results = search(connection, query, limit=limit)
        except (OSError, ValueError, sqlite3.Error) as exc:
            _fail(exc)
        if as_json:
            typer.echo(_json([asdict(result) for result in results]))
            return
        if not results:
            typer.echo("검색 결과가 없습니다.")
        for result in results:
            position = format_timestamp(result.start_ms) if result.start_ms is not None else "시각 없음"
            if result.page_no is not None:
                position = f"구간 {result.page_no}" if is_chunked(result.path) else f"p.{result.page_no}"
            typer.echo(f"{result.course} · {result.date or '-'} · {position} · {result.engine or result.source_kind}")
            if result.no_media:
                typer.echo("  연결된 원음 없음 (no_media)")
            if result.context_before:
                typer.echo(f"  앞: {result.context_before}")
            typer.echo(f"  {result.text}")
            if result.context_after:
                typer.echo(f"  뒤: {result.context_after}")

    @app.command("status")
    def status_command(
        root: RootOption = Path("."),
        as_of: Annotated[str | None, typer.Option("--as-of", help="기준 날짜 YYYY-MM-DD")] = None,
        start_date: Annotated[str | None, typer.Option("--from", help="누락 확인 시작 날짜 YYYY-MM-DD")] = None,
        html: Annotated[bool, typer.Option("--html", help="_lecturehub/status.html도 생성")] = False,
        backlog_only: Annotated[bool, typer.Option("--backlog", help="Google 전사 백로그만 출력")] = False,
        as_json: Annotated[bool, typer.Option("--json")] = False,
    ) -> None:
        """강의별 현황, 녹음 미확인 일정과 Google 전사 백로그를 표시한다."""
        from .status import library_status, render_status_html, render_status_text

        target_date, first_date = _date(as_of), _date(start_date)
        try:
            with _library(root) as connection:
                report = library_status(connection, as_of=target_date, start_date=first_date)
            if html:
                destination = _html_path(root)
                destination.write_text(render_status_html(report), encoding="utf-8")
                typer.echo(f"HTML: {destination}", err=True)
        except (OSError, ValueError, sqlite3.Error) as exc:
            _fail(exc)
        if as_json:
            payload = report.google_backlog if backlog_only else report
            typer.echo(_json([asdict(row) for row in payload] if isinstance(payload, list) else asdict(payload)))
        elif backlog_only:
            typer.echo(f"Google 전사 백로그: {len(report.google_backlog)}개 강의")
            for row in report.google_backlog:
                typer.echo(f"{row.course} · {row.date} · {row.title or row.session_index}")
        else:
            typer.echo(render_status_text(report))
