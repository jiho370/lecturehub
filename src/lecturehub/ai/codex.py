"""AI-002: Codex one-shot JSON with an empty, read-only workspace."""
from __future__ import annotations

import copy
import json
import os
import shutil
import tempfile
from pathlib import Path

from lecturehub.core import tools

from . import process

# Model-side tools a one-shot JSON answer never needs. Each name is a Codex CLI 0.162 feature flag; `codex features list
# --disable <name>` shows it turning off (an unknown name is an error, so a renamed flag fails loudly, not silently).
# `shell_tool` is the shell/exec tool: without it a prompt-injection line in a transcript cannot make Codex run commands
# or read other files. The read-only sandbox, the empty temp cwd and --ignore-user-config/--ignore-rules stay as layers.
DISABLED_FEATURES = ("shell_tool", "unified_exec", "view_image", "browser_use", "in_app_browser", "computer_use",
                     "image_generation", "multi_agent", "apps", "plugins", "code_mode_host")


def strict_schema(schema: dict) -> dict:
    """Close objects and require every key. Optional values become nullable; original schema stays intact."""
    value = copy.deepcopy(schema)
    if value.get("type") == "object":
        required = value.get("required", [])
        props = {key: strict_schema(child) for key, child in value.get("properties", {}).items()}
        value["properties"] = {key: child if key in required else {"anyOf": [child, {"type": "null"}]} for key, child in props.items()}
        value.update(required=list(props), additionalProperties=False)
    elif value.get("type") == "array":
        value["items"] = strict_schema(value["items"])
    return value


def without_nulls(value):
    if isinstance(value, dict):
        return {key: without_nulls(child) for key, child in value.items() if child is not None}
    if isinstance(value, list):
        return [without_nulls(child) for child in value]
    return value


class CodexEngine:
    name = "codex"

    def executable(self) -> str | None:
        found = tools.find("codex")
        if os.name != "nt":
            return found
        if found and Path(found).suffix.lower() == ".cmd":
            return found
        sibling = Path(found).with_suffix(".cmd") if found else None
        return str(sibling) if sibling and sibling.is_file() else shutil.which("codex.cmd")

    def available(self) -> bool:
        return self.executable() is not None

    def run_json(self, prompt: str, schema: dict, *, timeout: float = 900, stop=None) -> dict:
        exe = self.executable()
        if not exe:
            raise FileNotFoundError("Codex(codex.cmd)를 찾지 못했습니다. 설정에서 설치 안내를 확인하세요.")
        with tempfile.TemporaryDirectory(prefix="lecturehub-ai-") as temp:
            folder = Path(temp)
            workspace = folder / "workspace"
            workspace.mkdir()
            spec, output = folder / "schema.json", folder / "output.json"
            spec.write_text(json.dumps(strict_schema(schema), ensure_ascii=False), encoding="utf-8")
            args = [exe, "exec", "--skip-git-repo-check", "--ephemeral", "--ignore-user-config", "--ignore-rules",
                    "--sandbox", "read-only", "-C", str(workspace)]
            for feature in DISABLED_FEATURES:
                args += ["--disable", feature]
            args += ["--output-schema", str(spec), "-o", str(output), "-c", 'model_reasoning_effort="low"', "--json", "-"]
            events = process.execute(args, prompt, timeout=timeout, cwd=workspace, stop=stop)
            try:
                value = json.loads(output.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError) as exc:
                raise RuntimeError("Codex가 올바른 JSON 결과를 남기지 않았습니다.") from exc
            if not isinstance(value, dict):
                raise RuntimeError("Codex 결과는 JSON 객체여야 합니다.")  # noqa: TRY004 - invalid provider output is an execution failure
            usage = None
            for line in events.splitlines():
                try:
                    event = json.loads(line)
                    if event.get("type") == "turn.completed":
                        usage = event.get("usage")
                except (ValueError, AttributeError):
                    continue
            return {**without_nulls(value), "model": "codex_cli", "usage": usage, "cost_usd": None}

    def run_text(self, prompt: str, *, timeout: float = 900, stop=None) -> str:
        return self.run_json(prompt, {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
                             timeout=timeout, stop=stop)["text"]

    def open_console(self, workspace: Path, root: Path, lecture_id: str, *, launcher=None, writable=(), pack=None) -> None:
        process.open_console(self.name, self.executable(), workspace, root, lecture_id, launcher=launcher,
                             writable=writable, pack=pack)

    def session(self):
        return None  # exec provides a final answer, not a warm streaming conversation
