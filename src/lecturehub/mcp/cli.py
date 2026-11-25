"""AI app link CLI (D-072).

    lecturehub mcp serve    the read-only MCP server on stdin/stdout; an alias of ``-m lecturehub.mcp``, which is
                            what the pasted config runs (lean start, mcp/__main__.py)
    lecturehub mcp config   what to paste into ChatGPT desktop/Codex, Claude desktop and Claude Code (in that order)
"""
from __future__ import annotations

import typer


def register(app: typer.Typer) -> None:
    mcp = typer.Typer(no_args_is_help=True, help="AI 앱 연결(MCP) — 허용한 과목의 수업 자료를 AI 앱이 읽기 전용으로 봄")

    @mcp.command("serve")
    def serve_command() -> None:
        """AI 앱이 실행하는 읽기 전용 서버(표준 입출력). 직접 실행할 일은 없습니다."""
        from .server import serve

        raise typer.Exit(serve())

    @mcp.command("config")
    def config_command() -> None:
        """AI 앱에 붙여 넣을 설정 문구를 보여 줍니다(다른 앱의 설정 파일은 고치지 않음)."""
        from lecturehub.core import settings

        from .config import ORDER, snippets

        found = snippets()
        state = "켜짐" if settings.ai_link()["enabled"] else "꺼짐 — 앱의 설정 → AI → 'AI 앱 연결'에서 켜고 과목을 고르세요"
        typer.echo(f"AI 앱 연결: {state}\n")
        titles = {"codex": "ChatGPT 데스크톱 · Codex (config.toml):",
                  "claude_desktop": "Claude 데스크톱 (claude_desktop_config.json):",
                  "claude_code": "Claude Code (명령 한 줄):"}
        typer.echo("\n\n".join(f"{titles[key]}\n{found[key]}" for key in ORDER))
        typer.echo("\n확인한 것은 Codex 명령줄 도구와 Claude Code입니다. ChatGPT·Claude 데스크톱 앱은 직접 해 보지 않았습니다.")

    app.add_typer(mcp, name="mcp")
