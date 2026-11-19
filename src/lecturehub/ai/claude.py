"""AI-001: legacy Claude invocation, kept as an explicit compatibility choice."""
from __future__ import annotations

import json
from pathlib import Path

from lecturehub.core import tools

from . import process


class ClaudeEngine:
    name = "claude"

    def available(self) -> bool:
        return tools.find("claude") is not None

    def run_json(self, prompt: str, schema: dict, *, timeout: float = 900, stop=None) -> dict:
        exe = tools.find("claude")
        if not exe:
            raise FileNotFoundError("Claude Code 명령을 찾지 못했습니다.")
        args = [exe, "-p", "--output-format", "json", "--json-schema", json.dumps(schema), "--tools", "",
                "--strict-mcp-config", "--no-session-persistence"]
        try:
            envelope = json.loads(process.execute(args, prompt, timeout=timeout, stop=stop))
            if envelope.get("is_error"):
                raise ValueError("provider error")
            value = envelope.get("structured_output")
            if value is None:
                value = json.loads(envelope.get("result") or "{}")
            if not isinstance(value, dict):
                raise ValueError("not an object")  # noqa: TRY004 - invalid provider output is an execution failure
            return {**value, "model": ",".join(envelope.get("modelUsage", {}) or []), "cost_usd": envelope.get("total_cost_usd")}
        except (ValueError, AttributeError) as exc:
            raise RuntimeError("Claude 응답을 읽지 못했습니다. 로그인·사용 한도를 확인하세요.") from exc

    def run_text(self, prompt: str, *, timeout: float = 900, stop=None) -> str:
        return self.run_json(prompt, {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
                             timeout=timeout, stop=stop)["text"]

    def open_console(self, workspace: Path, root: Path, lecture_id: str, *, launcher=None, writable=(), pack=None) -> None:
        process.open_console(self.name, tools.find("claude"), workspace, root, lecture_id, launcher=launcher,
                             writable=writable, pack=pack)

    def session(self):
        return None
