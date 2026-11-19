"""CLI for evidence-linked notes (S4). Note text is not printed; the app shows it."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import typer

from lecturehub.index.cli import RootOption, _fail, _library


def register(app: typer.Typer) -> None:
    note_app = typer.Typer(no_args_is_help=True, help="근거 있는 강의 노트(S4)")

    @note_app.command("make")
    def make(lecture_id: str, root: RootOption = Path("."),
             block: str = typer.Option(None, "--block", help="이 블록 하나만 다시 만든다(사용자가 고친 블록은 그대로)")) -> None:
        """이 회차의 사용본 전사와 연결된 자료 쪽으로 노트를 만든다(선택한 AI, 녹음 중에는 안 함)."""
        from lecturehub.transcription.cli import _recording_in_progress

        from .note_job import run, consent_info
        from lecturehub.transcription.consent import ConsentStore, ensure_consent
        import time

        try:
            with _library(root) as connection:
                info = consent_info(connection, Path(root), lecture_id)
                store = ConsentStore(Path(root) / "_lecturehub" / "consent.json")
                if not ensure_consent(store, info["provider"], info["course"], typer.confirm, time.time_ns() // 1_000_000):
                    raise PermissionError("동의하지 않아 보내지 않았습니다.")
                result = run(connection, Path(root), lecture_id, only_key=block, consent_provider=info["provider"],
                             busy=lambda: _recording_in_progress(Path(root)))
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            _fail(exc)
        typer.echo(json.dumps(result, ensure_ascii=False))
        if result["state"] != "done":
            raise typer.Exit(2)

    @note_app.command("import")
    def import_command(job: Path, root: RootOption = Path(".")) -> None:
        """손으로 돌린 결과(작업 폴더의 output.json: claude -p JSON 또는 {"blocks": [...]})를 노트로 저장한다."""
        from .note_job import _state, import_result

        try:
            raw = json.loads((job / "output.json").read_text(encoding="utf-8"))
            if "blocks" in raw:
                output = raw
            elif raw.get("structured_output"):
                output = raw["structured_output"]
            else:  # a plain `claude -p --output-format json` envelope
                output = json.loads(raw.get("result") or "{}")
            with _library(root) as connection, connection:
                result = import_result(connection, job, output)
            _state(job, state="done", **result)
        except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
            _fail(exc)
        typer.echo(json.dumps(result, ensure_ascii=False))

    app.add_typer(note_app, name="note")
