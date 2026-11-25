"""What the user pastes into an AI app to connect it (D-072). The app shows these; it never edits another app's files.

The command is ``core.proc.console_python()``: python.exe from source, LectureHubWorker.exe in the installed build,
and in the Store build the worker's execution alias (the exe inside the package folder cannot be started by path
from another app).
"""
from __future__ import annotations

import json

SERVER_NAME = "lecturehub"
ARGS = ["-m", "lecturehub.mcp"]  # mcp/__main__.py: no typer, no other CLI, no network library ("mcp serve" = alias)


def command() -> str:
    from lecturehub.core import proc

    return proc.console_python()


def _toml_string(text: str) -> str:
    # a TOML basic string: JSON escaping is a valid subset for what a Windows path can hold
    return json.dumps(text, ensure_ascii=False)


ORDER = ("codex", "claude_desktop", "claude_code")  # ChatGPT desktop · Codex first (D-073), then the Claude apps


def snippets(program: str | None = None) -> dict:
    """{"command", "args", "codex", "claude_desktop", "claude_code"}: the three ways of connecting, ready to paste,
    in the order the app and ``lecturehub mcp config`` show them (``ORDER``)."""
    program = program or command()
    desktop = json.dumps({"mcpServers": {SERVER_NAME: {"command": program, "args": ARGS}}}, ensure_ascii=False, indent=2)
    codex = "\n".join([f"[mcp_servers.{SERVER_NAME}]", f"command = {_toml_string(program)}",
                       "args = [" + ", ".join(_toml_string(a) for a in ARGS) + "]"])
    # user scope: the link works in every folder Claude Code is opened in, not only the current project
    claude_code = f'claude mcp add --scope user {SERVER_NAME} -- "{program}" {" ".join(ARGS)}'
    return {"command": program, "args": list(ARGS), "codex": codex, "claude_desktop": desktop,
            "claude_code": claude_code}
