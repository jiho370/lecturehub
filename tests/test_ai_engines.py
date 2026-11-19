"""AI-001/002: no real AI calls, strict schemas, bounded CLI, Korean paths and packaged skills."""
import copy
import json
import os
import subprocess
from pathlib import Path

import pytest

from lecturehub import ai
from lecturehub.ai import process
from lecturehub.ai.claude import ClaudeEngine
from lecturehub.ai.codex import CodexEngine, strict_schema, without_nulls
from lecturehub.core import agent_workspace, tools
from lecturehub.study.note_job import SCHEMA


def test_AI_001_selection_priority_and_off(monkeypatch):
    assert ai.selected() == "codex"
    ai.set_choice("claude")
    assert isinstance(ai.get_engine(), ClaudeEngine)
    ai.set_choice("codex", "notes")
    assert isinstance(ai.get_engine(), CodexEngine)
    monkeypatch.setenv("LECTUREHUB_AI_ENGINE", "off")
    with pytest.raises(RuntimeError):
        ai.get_engine()
    assert ai.status()["env_override"]
    monkeypatch.delenv("LECTUREHUB_AI_ENGINE")
    ai.set_choice("inherit", "notes")
    assert ai.selected() == "claude"
    with pytest.raises(ValueError):
        ai.set_choice("other")


def test_AI_002_strict_schema_keeps_optional_fields_nullable_and_does_not_mutate():
    before = copy.deepcopy(SCHEMA)
    spec = strict_schema(SCHEMA)
    def objects(schema):
        if schema.get("type") == "object":
            assert schema["additionalProperties"] is False
            assert set(schema["required"]) == set(schema["properties"])
            for value in schema["properties"].values(): objects(value)
        if schema.get("type") == "array": objects(schema["items"])
        for value in schema.get("anyOf", []): objects(value)
    objects(spec)
    assert spec["properties"]["blocks"]["items"]["properties"]["cites"]["anyOf"][1] == {"type": "null"}
    assert SCHEMA == before
    assert without_nulls({"cites": [{"t": None, "s": None, "m": "slide", "p": 2}], "ext": None}) == {"cites": [{"m": "slide", "p": 2}]}


def test_AI_002_codex_cmd_empty_readonly_workspace_and_final_output(monkeypatch):
    seen = {}
    monkeypatch.setattr(CodexEngine, "executable", lambda self: "codex.cmd")
    def execute(args, prompt, **kw):
        seen.update(args=args, prompt=prompt, kw=kw)
        cwd = Path(args[args.index("-C") + 1])
        assert list(cwd.iterdir()) == []
        schema = Path(args[args.index("--output-schema") + 1])
        output = Path(args[args.index("-o") + 1])
        assert schema.parent == output.parent and schema.parent != cwd
        assert json.loads(schema.read_text(encoding="utf-8"))["additionalProperties"] is False
        output.write_text(json.dumps({"blocks": [{"key": "b1", "kind": "heading", "text": "합성", "cites": None, "ext": None}]}), encoding="utf-8")
        return json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 2}})
    monkeypatch.setattr(process, "execute", execute)
    result = CodexEngine().run_json("합성 입력만", SCHEMA)
    args = seen["args"]
    assert args[:2] == ["codex.cmd", "exec"] and args[-1] == "-"
    assert args[args.index("--sandbox") + 1] == "read-only"
    assert "--ephemeral" in args and "--ignore-user-config" in args and "--skip-git-repo-check" in args
    assert "--ignore-rules" in args
    disabled = {args[i + 1] for i, a in enumerate(args) if a == "--disable"}
    assert {"shell_tool", "unified_exec", "view_image", "browser_use", "computer_use"} <= disabled  # no shell for a JSON answer
    assert "--add-dir" not in args and "--dangerously-bypass-approvals-and-sandbox" not in args
    assert args[args.index("-c") + 1] == 'model_reasoning_effort="low"'
    assert "합성 입력만" not in args and seen["prompt"] == "합성 입력만"
    assert result["usage"]["output_tokens"] == 2 and "cites" not in result["blocks"][0]
    assert not Path(args[args.index("-C") + 1]).exists()
    assert CodexEngine().session() is None


@pytest.mark.parametrize("content", ["not JSON", "[]", None])
def test_AI_002_codex_bad_or_missing_output_never_succeeds(monkeypatch, content):
    monkeypatch.setattr(CodexEngine, "executable", lambda self: "codex.cmd")
    def execute(args, *a, **kw):
        if content is not None: Path(args[args.index("-o") + 1]).write_text(content, encoding="utf-8")
        return ""
    monkeypatch.setattr(process, "execute", execute)
    with pytest.raises(RuntimeError): CodexEngine().run_json("synthetic", SCHEMA)


def test_AI_001_claude_keeps_legacy_no_tool_flags(monkeypatch):
    monkeypatch.setattr(tools, "find", lambda name: "claude.exe")
    seen = {}
    def execute(args, prompt, **kw):
        seen["args"] = args
        return json.dumps({"structured_output": {"blocks": []}, "modelUsage": {"fake": {}}, "total_cost_usd": 0})
    monkeypatch.setattr(process, "execute", execute)
    assert ClaudeEngine().run_json("synthetic", SCHEMA)["model"] == "fake"
    args = seen["args"]
    assert args[args.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in args and "--no-session-persistence" in args
    assert json.loads(args[args.index("--json-schema") + 1]) == SCHEMA


def test_AI_002_process_uses_stdin_low_priority_and_redacts_provider_errors(monkeypatch):
    seen = {}
    class Fake:
        returncode = 1
        def __init__(self, args, **kw): seen.update(args=args, kw=kw)
        def communicate(self, text=None, **kw): seen["input"] = text; return "private provider output", "private error"
        def poll(self): return self.returncode
    monkeypatch.setattr(process.subprocess, "Popen", Fake)
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-not-a-key")
    with pytest.raises(RuntimeError) as error: process.execute(["codex.cmd"], "합성 입력", timeout=5)
    assert "private" not in str(error.value)
    assert seen["input"] == "합성 입력" and "OPENAI_API_KEY" not in seen["kw"]["env"]
    if os.name == "nt": assert seen["kw"]["creationflags"] & 0x4000


def test_AI_002_console_quotes_korean_space_parentheses_and_copies_skills(tmp_path, monkeypatch):
    workspace = agent_workspace.ensure(tmp_path / "도우미 (합성)")
    for name in agent_workspace.skill_names():
        original = (agent_workspace.SKILLS_SRC / name / "SKILL.md").read_bytes()
        for base in (".agents/skills", ".claude/skills"):
            assert (workspace / base / name / "SKILL.md").read_bytes() == original
    assert (workspace / ".git").is_dir()
    exe = tmp_path / "가짜 도구 (합성)" / "codex.cmd"
    exe.parent.mkdir()
    exe.write_text("@echo off\nchcp 65001 >nul\necho %*\n", encoding="utf-8")
    monkeypatch.setattr(CodexEngine, "executable", lambda self: str(exe))
    seen = []
    root = tmp_path / "학기 (합성 공백)"
    root.mkdir()
    notes = root / "합성 과목" / "정리본"
    cues = root / "_lecturehub" / "quiz" / "합성 과목"
    CodexEngine().open_console(workspace, root, "lecture_01", launcher=lambda cmd, cwd: seen.append(cmd),
                               writable=[notes, cues])
    cmd = seen[0]
    assert "workspace-write" in cmd and "$lecture-note lecture_01" in cmd and '"-a" "on-request"' in cmd
    assert f'"--add-dir" "{notes.resolve()}"' in cmd and f'"--add-dir" "{cues.resolve()}"' in cmd
    assert f'"{root}"' not in cmd and f'"{root.resolve()}"' not in cmd   # the semester folder is never writable
    if os.name == "nt":
        result = subprocess.run(cmd.replace(" /k ", " /c ", 1), capture_output=True, encoding="utf-8", check=True)
        assert "$lecture-note lecture_01" in result.stdout and str(notes.resolve()) in result.stdout
    with pytest.raises(ValueError): CodexEngine().open_console(workspace, root, "x&whoami", launcher=lambda *a: None)
    for bad in (root, tmp_path, root / "_lecturehub", root / "_lecturehub" / "quiz", root / "_lecturehub" / "recordings"):
        with pytest.raises(ValueError):
            CodexEngine().open_console(workspace, root, "lecture_01", launcher=lambda *a: None, writable=[bad])


@pytest.mark.skipif(os.name != "nt", reason="Windows cmd argument round trip")
def test_AI_002_batch_exec_keeps_stdin_and_config_quotes(tmp_path):
    import sys
    helper = tmp_path / "합성 (도구).py"
    helper.write_text("import json,sys; print(json.dumps({'args':sys.argv[1:], 'input':sys.stdin.read()},ensure_ascii=False))", encoding="utf-8")
    cmd = tmp_path / "codex.cmd"
    cmd.write_text(f'@echo off\nchcp 65001 >nul\n"{sys.executable}" "{helper}" %*\n', encoding="utf-8")
    args = [str(cmd), "exec", "-C", str(tmp_path / "학기 (가상 경로)"), "-c", 'model_reasoning_effort="low"', "--json", "-"]
    result = json.loads(process.execute(args, "합성 입력만", timeout=10))
    assert result == {"args": args[1:], "input": "합성 입력만"}


@pytest.mark.parametrize("cancel", [True, False])
def test_AI_002_cancel_or_timeout_terminates_process_tree(monkeypatch, cancel):
    from threading import Event
    seen = []
    class Fake:
        pid, returncode = 4321, None
        def __init__(self, *a, **kw): pass
        def poll(self): return self.returncode
        def kill(self): self.returncode = -1; seen.append("killed")
        def communicate(self, *a, **kw):
            if self.returncode is None: raise subprocess.TimeoutExpired("fake", 1)
            return "", ""
    monkeypatch.setattr(process.subprocess, "Popen", Fake)
    monkeypatch.setattr(process.subprocess, "run", lambda args, **kw: seen.append(args))
    stop = Event()
    if cancel: stop.set()
    with pytest.raises(RuntimeError): process.execute(["synthetic"], "synthetic", timeout=0, stop=stop)
    assert "killed" in seen
    if os.name == "nt": assert seen[0] == ["taskkill.exe", "/PID", "4321", "/T", "/F"]
