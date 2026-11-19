"""클로바노트 CLI(2026-11-05) — 주 1회 예약 작업이나 손으로 돌린다. 결과 JSON 한 줄.

    lecturehub clova sync [--limit N]   새 노트(녹음+전사)를 다운로드 폴더로 받는다. 여러 번 돌려도 안전(받은 것은 건너뜀)
    lecturehub clova status             마지막 확인 결과(로그인 필요 여부 포함)
"""
from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path
from typing import Annotated

import typer

from lecturehub.core.settings import root_option_callback

Root = Annotated[Path, typer.Option("--root", callback=root_option_callback, help="학기 폴더(비우면 설정의 학기 폴더)")]


def _echo(data) -> None:
    typer.echo(json.dumps(data, ensure_ascii=False))


def register(app: typer.Typer) -> None:
    clova = typer.Typer(no_args_is_help=True, help="클로바노트에서 새 노트 받기 — Aside 브라우저")

    @clova.command("sync")
    def sync_command(root: Root = None,
                     limit: Annotated[int, typer.Option("--limit", min=1, max=10, help="한 번에 받을 노트 수")] = 3) -> None:
        from lecturehub.core import db

        from . import clova_fetch

        with closing(db.connect(Path(root) / "_lecturehub" / "lecturehub.sqlite")) as connection:
            result = clova_fetch.sync(connection, Path(root), limit=limit)
        _echo({k: result.get(k) for k in ("ok", "login_required", "message", "staged", "duplicate", "failed",
                                          "seconds", "error") if result.get(k) not in (None, "")}
              | {"files": [n.get("audio") for n in result.get("notes", []) if n.get("state") == "staged"]})
        if not result.get("ok"):
            raise typer.Exit(1)

    @clova.command("status")
    def status_command(root: Root = None) -> None:
        from . import clova_fetch

        _echo(clova_fetch.load_status(Path(root)) or {"checked_at": None})

    app.add_typer(clova, name="clova")
