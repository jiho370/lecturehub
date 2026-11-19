"""CLI for sorting course folders by category (user request 2026-10-01, index/organize.py)."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Annotated

import typer

from lecturehub.core.textio import read_text_any
from .cli import RootOption, _fail, _library


def register(app: typer.Typer) -> None:
    organize_app = typer.Typer(no_args_is_help=True,
                               help="과목 폴더 정리: 강의자료·실습자료·과제·정리본·참고자료·교재")

    @organize_app.command("show")
    def show(root: RootOption = Path("."), as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
        """과목별로 분류된 것과 아직 분류 안 된 것(제안 포함)을 보여 준다. 아무것도 옮기지 않는다."""
        from . import organize

        try:
            with _library(root) as connection:
                courses = [organize.inventory(connection, root, name)
                           for name in organize.course_folders(connection, root.resolve())]
        except (OSError, ValueError, sqlite3.Error) as exc:
            _fail(exc)
        if as_json:
            typer.echo(json.dumps(courses, ensure_ascii=False, indent=2))
            return
        for course in courses:
            sorted_count = sum(len(group["items"]) for group in course["groups"])
            typer.echo(f"{course['course']}: 분류됨 {sorted_count} · 분류 안 됨 {len(course['loose'])}")
            for item in course["loose"]:
                typer.echo(f"  {item['name']}{'/' if item['folder'] else ''} → {item['suggest'] or '?'}")

    @organize_app.command("apply")
    def apply(plan: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="[{from, to}, …] JSON (학기 루트 기준 경로)")],
              root: RootOption = Path("."),
              dry_run: Annotated[bool, typer.Option("--dry-run", help="옮기지 않고 걸리는 것만 본다")] = False) -> None:
        """사용자가 확인한 이동 목록대로 옮긴다. 덮어쓰지 않고, MoveLog에 남겨 되돌릴 수 있다."""
        from . import organize

        try:
            moves = json.loads(read_text_any(plan))
            with _library(root) as connection:
                problems = organize.check(connection, root, moves)
                result = None if dry_run or problems else organize.apply(connection, root, moves)
        except (OSError, ValueError, sqlite3.Error) as exc:
            _fail(exc)
        for problem in problems:
            typer.echo(f"걸림: {problem}")
        if problems:
            raise typer.Exit(code=1)
        if result is None:
            typer.echo(f"옮길 수 있습니다: {len(moves)}건 (아직 옮기지 않음)")
            return
        typer.echo(f"옮김 {len(result['moved'])}건 · 경로를 따라 고친 기록 {result['rows']}건 · 묶음 {result['batch']}")
        if result.get("stopped"):
            typer.echo(f"중간에 멈춤: {result['stopped']}")
            raise typer.Exit(code=1)

    @organize_app.command("undo")
    def undo(root: RootOption = Path("."),
             batch: Annotated[int | None, typer.Option("--batch", help="되돌릴 묶음 번호(생략하면 마지막)")] = None) -> None:
        """옮긴 묶음을 제자리로 되돌린다."""
        from . import organize

        try:
            with _library(root) as connection:
                result = organize.undo(connection, root, batch)
        except (OSError, ValueError, sqlite3.Error) as exc:
            _fail(exc)
        typer.echo(f"되돌림 {len(result['moved'])}건" + (f" · 멈춤: {result['stopped']}" if result.get("stopped") else ""))

    @organize_app.command("relocate")
    def relocate(root: RootOption = Path(".")) -> None:
        """탐색기에서 직접 옮긴 파일을 같은 과목 폴더 안에서 찾아(내용 해시) 앱 기록의 경로를 고친다."""
        from . import organize

        try:
            with _library(root) as connection, connection:
                found = organize.relocate(connection, root)
        except (OSError, ValueError, sqlite3.Error) as exc:
            _fail(exc)
        for item in found:
            typer.echo(f"{item['from']} → {item['to']}")
        typer.echo(f"경로를 고친 파일 {len(found)}개")

    app.add_typer(organize_app, name="organize")
