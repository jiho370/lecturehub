"""Database commands owned by track B."""

import json
import sqlite3
from pathlib import Path
from typing import Annotated

import typer

from lecturehub.core.db import KEEP_LATEST, init_db, make_backup, prune_backups, recording_session
from lecturehub.core.paths import app_dir, database_path


def register(app: typer.Typer) -> None:
    db_app = typer.Typer(no_args_is_help=True, help="학기별 데이터베이스 관리")

    @db_app.command("init")
    def initialize(
        root: Annotated[
            Path,
            typer.Option("--root", exists=True, file_okay=False, readable=True, help="학기 루트 폴더"),
        ],
    ) -> None:
        """학기 루트의 _lecturehub/lecturehub.sqlite를 초기화한다."""
        try:
            destination = database_path(root)
            init_db(destination)
        except (OSError, sqlite3.Error, ValueError) as exc:
            typer.echo(f"DB 초기화 실패: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        typer.echo(f"DB 준비 완료: {destination}")

    RootOption = Annotated[Path, typer.Option("--root", envvar="LECTUREHUB_ROOT", exists=True, file_okay=False,
                                              help="학기 루트. 생략하면 LECTUREHUB_ROOT 또는 현재 폴더")]

    @db_app.command("prune-backups")
    def prune_backups_command(
        root: RootOption = Path("."),
        keep: Annotated[int, typer.Option("--keep", min=1, help="마이그레이션 전 백업 말고 남길 최신 백업 수")] = KEEP_LATEST,
        dry_run: Annotated[bool, typer.Option("--dry-run", help="무엇을 압축할지 보여 주기만 함")] = False,
    ) -> None:
        """_lecturehub\\backup 정리: 마이그레이션 전 백업 + 최신 N개는 두고, 나머지는 old_backups_<날짜>.zip으로. JSON 출력."""
        backup_dir = app_dir(root) / "backup"
        busy = recording_session(app_dir(root))
        if busy:
            typer.echo(f"녹음 중인 세션이 있습니다({busy}). 녹음이 끝난 뒤 실행하세요.", err=True)
            raise typer.Exit(1)
        if not backup_dir.is_dir():
            typer.echo(json.dumps({"dry_run": dry_run, "backup_dir": str(backup_dir), "zipped": [], "files": 0,
                                   "bytes": 0}, ensure_ascii=False, indent=2))
            return
        try:
            result = prune_backups(backup_dir, keep, dry_run=dry_run)
        except (OSError, ValueError) as exc:
            typer.echo(f"백업 정리 실패: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
        if result["errors"]:
            raise typer.Exit(1)

    @db_app.command("backup")
    def backup_command(
        label: Annotated[str, typer.Option("--label", help="무슨 작업 전인지(영문, 예: notes1109)")],
        root: RootOption = Path("."),
    ) -> None:
        """실제 DB를 바꾸기 전 백업: backup\\lecturehub_before_<label>_<시각>.sqlite, 그다음 백업 폴더 정리."""
        try:
            destination = make_backup(database_path(root), label)
        except (OSError, sqlite3.Error, ValueError) as exc:
            typer.echo(f"백업 실패: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        typer.echo(f"백업: {destination}")

    app.add_typer(db_app, name="db")
