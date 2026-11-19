"""CLI 진입점.

각 트랙은 자기 패키지에 `cli.py`를 두고 `register(app)` 함수로 하위 명령을 붙인다.
이 파일은 트랙 B 소유다. 다른 트랙은 수정하지 않는다(아직 없는 모듈은 자동으로 건너뛴다).
"""

from __future__ import annotations

import importlib
import sys

import typer

app = typer.Typer(no_args_is_help=True, help="Lecture Hub")

# 트랙별 CLI 모듈. 모듈이 아직 없으면 건너뛴다.
_TRACK_CLI_MODULES = [
    "lecturehub.core.cli",          # B
    "lecturehub.parsers.cli",       # B
    "lecturehub.index.cli",         # B
    "lecturehub.recorder.cli",      # A
    "lecturehub.transcription.cli", # C
    "lecturehub.study.cli",         # D
    "lecturehub.materials.cli",     # E
    "lecturehub.canonical.cli",     # E
    "lecturehub.ui.cli",            # B
    "lecturehub.booking.cli",       # 그스룸 예약(P-019)
]
_REGISTERED_MODULES: set[str] = set()


def _register_all() -> None:
    for name in _TRACK_CLI_MODULES:
        if name in _REGISTERED_MODULES:
            continue
        try:
            module = importlib.import_module(name)
        except ModuleNotFoundError as exc:
            # 모듈(또는 그 상위 패키지)이 아직 없으면 건너뛴다. 다른 import 오류는 그대로 올린다.
            if exc.name and (name == exc.name or name.startswith(exc.name + ".")):
                continue
            raise
        module.register(app)
        _REGISTERED_MODULES.add(name)


@app.callback()
def _root() -> None:
    """Lecture Hub CLI. 하위 명령이 하나뿐이어도 그룹 구조를 유지하기 위한 콜백."""


@app.command()
def version() -> None:
    """버전을 출력한다."""
    from lecturehub import __version__

    typer.echo(__version__)


@app.command()
def selfcheck(
    window: bool = typer.Option(False, "--window", help="숨긴 창 하나로 화면이 뜨는지도 확인"),
    out: str = typer.Option("", "--out", help="결과 JSON을 쓸 파일"),
) -> None:
    """새 PC의 첫 실행에 필요한 것을 임시 폴더에서 점검한다(실제 설정·학기 폴더·네트워크는 건드리지 않음)."""
    import json
    from pathlib import Path

    from lecturehub.core import selfcheck as check

    result = check.run(window=window)
    if out:
        Path(out).write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    for row in result["steps"]:
        typer.echo(f"{'통과' if row['ok'] else '실패'}  {row['step']} — {row['detail']} ({row['seconds']}초)")
    raise typer.Exit(0 if result["ok"] else 1)


def main() -> None:
    # Windows 콘솔(cp949)에서 한글 출력 오류 방지
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    from lecturehub.core import applog

    applog.setup("worker")  # helpers started by the window have no console anyone reads
    try:
        _register_all()
        app()
    except Exception:
        applog.LOG.critical("명령이 오류로 끝남: %s", " ".join(sys.argv[1:3]), exc_info=True)
        raise


if __name__ == "__main__":
    main()
