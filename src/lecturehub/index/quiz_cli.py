"""CLI for practice question banks (index/exam_prep.py)."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Annotated

import typer

from .cli import RootOption, _fail, _library


def register(app: typer.Typer) -> None:
    quiz_app = typer.Typer(no_args_is_help=True, help="연습 문제 묶음(시험 대비 화면)")

    @quiz_app.command("check")
    def check(file: Path) -> None:
        """문제 파일(JSON)의 형식만 확인한다. DB는 건드리지 않는다. JSON 한 줄."""
        from . import exam_prep

        try:
            bank = exam_prep.read_bank(file)
        except (OSError, ValueError) as exc:
            _fail(exc)
        kinds: dict[str, int] = {}
        for item in bank["items"]:
            kinds[item["type"]] = kinds.get(item["type"], 0) + 1
        typer.echo(json.dumps({"course": bank["course"], "bank": bank["bank"], "items": len(bank["items"]), "types": kinds},
                              ensure_ascii=False))

    @quiz_app.command("import")
    def import_command(
            file: Annotated[Path | None, typer.Argument(help="문제 파일. 생략하면 _lecturehub/quiz 아래의 새 파일 전부")] = None,
            root: RootOption = Path(".")) -> None:
        """문제 파일을 읽어 들인다(같은 파일은 한 번만, 같은 묶음의 새 파일은 풀이 기록을 유지). JSON 한 줄."""
        from . import exam_prep

        try:
            with _library(root) as connection, connection:
                result = exam_prep.import_bank(connection, root, file) if file else exam_prep.import_new(connection, root)
        except (OSError, ValueError, sqlite3.Error) as exc:
            _fail(exc)
        typer.echo(json.dumps(result, ensure_ascii=False))

    @quiz_app.command("cues")
    def cues(file: Path, root: RootOption = Path(".")) -> None:
        """수업 중 발언 파일(*.cues.json)을 읽어 들인다. 전사에서 찾지 못한 발언은 넣지 않고 알려 준다. JSON 한 줄."""
        from . import exam_prep

        try:
            with _library(root) as connection, connection:
                result = exam_prep.import_cues(connection, root, file)
        except (OSError, ValueError, sqlite3.Error) as exc:
            _fail(exc)
        typer.echo(json.dumps(result, ensure_ascii=False))

    @quiz_app.command("stats")
    def stats(root: RootOption = Path(".")) -> None:
        """과목별 문제 수와 풀이 상태. JSON 한 줄."""
        from . import exam_prep

        try:
            with _library(root) as connection:
                found = {folder: exam_prep.stats(connection, course_id) for course_id, folder in
                         connection.execute("SELECT id, folder_name FROM Course ORDER BY rowid").fetchall()}
        except (OSError, ValueError, sqlite3.Error) as exc:
            _fail(exc)
        typer.echo(json.dumps({k: {x: v[x] for x in ("total", "new", "wrong", "reviewing", "mastered")}
                               for k, v in found.items() if v["total"]}, ensure_ascii=False))

    app.add_typer(quiz_app, name="quiz")
