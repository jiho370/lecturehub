"""CLI for lecture materials (S4-0: page text)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import typer

from lecturehub.index.cli import RootOption, _fail, _library


def register(app: typer.Typer) -> None:
    materials_app = typer.Typer(no_args_is_help=True, help="강의자료(쪽 본문)")

    @materials_app.command("extract")
    def extract_command(
        root: RootOption = Path("."),
        force: bool = typer.Option(False, help="이미 뽑은 자료도 다시 뽑는다"),
    ) -> None:
        """색인된 자료의 쪽별 본문을 MaterialPage에 넣는다(원본은 읽기만, 녹음 중에는 멈춤)."""
        from lecturehub.transcription.cli import _recording_in_progress

        from .extract import extract

        try:
            with _library(root) as connection:
                report = extract(connection, Path(root), busy=lambda: _recording_in_progress(Path(root)),
                                 only_missing=not force)
        except (OSError, ValueError, sqlite3.Error) as exc:
            _fail(exc)
        typer.echo(f"자료 {len(report.extracted)}개 · 쪽 {report.pages}개(본문 없는 쪽 {report.empty_pages}) · "
                   f"건너뜀 {len(report.skipped)}개")
        for item in report.skipped:
            typer.echo(f"  건너뜀: {Path(item['path']).name} — {item['reason']}")
        if any(item["reason"].startswith("녹음 중") for item in report.skipped):
            raise typer.Exit(2)

    app.add_typer(materials_app, name="materials")
