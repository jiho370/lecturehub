"""Transcript summary CLI; deliberately does not print transcript content."""

from pathlib import Path
from typing import Annotated

import typer

from lecturehub.core.textio import EncodingSuspectError
from lecturehub.core.timeutil import format_timestamp

from . import TranscriptParseError, parse_file


def register(app: typer.Typer) -> None:
    @app.command("parse")
    def parse_command(
        path: Annotated[Path, typer.Argument(exists=True, file_okay=True, dir_okay=False, readable=True)],
    ) -> None:
        """Print the detected engine, segment count and first/last start time."""
        try:
            result = parse_file(path)
        except (OSError, EncodingSuspectError, TranscriptParseError) as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(1) from exc
        first = format_timestamp(result.segments[0].start_ms) if result.segments else "-"
        last = format_timestamp(result.segments[-1].start_ms) if result.segments else "-"
        typer.echo(f"engine: {result.engine}")
        typer.echo(f"segments: {len(result.segments)}")
        typer.echo(f"first: {first}")
        typer.echo(f"last: {last}")
        if result.engine == "unknown":
            typer.echo("Warning: unknown transcript format; engine needs confirmation", err=True)

    @app.command("parse-check")
    def parse_check_command(
        root: Annotated[Path, typer.Option("--root", exists=True, file_okay=False, help="학기 루트")],
        record: Annotated[bool, typer.Option(help="결과를 Event(append-only)로 DB에 남긴다")] = False,
    ) -> None:
        """색인된 전사본을 현재 파서로 다시 읽어 저장된 세그먼트와 비교한다(원본·DB 세그먼트는 바꾸지 않음)."""
        from contextlib import closing

        from lecturehub.core import db
        from lecturehub.core.events import append
        from lecturehub.core.paths import database_path

        from .revalidate import revalidate

        try:
            with closing(db.connect(database_path(root))) as connection:
                report = revalidate(connection, root)
                if record:
                    append(connection, "parser_revalidated", "Parser", f"parser-v{report.parser_version}",
                           report.as_payload())
                    connection.commit()
        except (OSError, ValueError) as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(1) from exc
        typer.echo(f"parser v{report.parser_version}: checked {report.checked}, changed {len(report.changed)}, "
                   f"missing {len(report.missing)}, skipped {report.skipped}")
        if report.changed:
            typer.echo("changed transcripts (rebuild as a new version, keep the raw): " + ", ".join(report.changed))
            raise typer.Exit(2)
