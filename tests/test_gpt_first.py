"""GPT-first in the public edition (D-073, 2026-11-24): ChatGPT/Codex listed first in the AI app link, and the
packaged skills that use the link (week-brief, review-plan). Synthetic data only; no AI app is run."""
import importlib.util
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lecturehub.core import agent_workspace
from lecturehub.mcp import config

REPO = Path(__file__).resolve().parents[1]
WEB = REPO / "src" / "lecturehub" / "ui" / "web"
NEW_SKILLS = ("week-brief", "review-plan", "note-from-lecture")
INSPECT_SKILLS = ("check-notices", "app-checkup")
TOOLS = ("list_courses", "list_lectures", "get_transcript", "get_note", "get_material", "search", "list_calendar",
         "list_announcements", "list_assignments")


# ---------------------------------------------------------------- AI app link: ChatGPT · Codex first
def test_GPT_FIRST_001_snippets_list_codex_first():
    found = config.snippets("C:/합성 (폴더)/python.exe")
    keys = [k for k in found if k not in ("command", "args")]
    assert keys == list(config.ORDER) == ["codex", "claude_desktop", "claude_code"]


def test_GPT_FIRST_002_mcp_config_prints_chatgpt_codex_first(monkeypatch):
    import typer

    from lecturehub.mcp import cli

    monkeypatch.setattr(config, "command", lambda: "C:/합성/python.exe")
    app = typer.Typer()
    cli.register(app)
    out = CliRunner().invoke(app, ["mcp", "config"]).output
    at = [out.index(title) for title in ("ChatGPT 데스크톱 · Codex", "Claude 데스크톱", "Claude Code (명령")]
    assert at == sorted(at) and "[mcp_servers.lecturehub]" in out
    assert "확인한 것은 Codex 명령줄 도구와 Claude Code" in out and "직접 해 보지 않았습니다" in out


def test_GPT_FIRST_003_settings_card_orders_and_names_chatgpt_first():
    src = (WEB / "app.js").read_text(encoding="utf-8")
    body = src[src.index("  function aiLinkSection(L) {"):src.index("  // the clipboard API first")]
    assert "ChatGPT(Edu)·Codex, Claude 같은 AI 앱" in body
    at = [body.index(f'snip("{key}"') for key in config.ORDER]
    assert at == sorted(at)
    assert "확인한 것은 Codex 명령줄 도구와 Claude Code" in body and "직접 해 보지 않았습니다" in body


def test_GPT_FIRST_004_consent_text_names_chatgpt_first():
    from lecturehub.transcription.consent import NOTICES

    text = NOTICES["ai_app_link"]
    assert text.index("ChatGPT") < text.index("Claude")


# ---------------------------------------------------------------- skills that use the link
@pytest.mark.parametrize("name", NEW_SKILLS)
def test_GPT_SKILL_001_packaged_with_front_matter_and_only_link_tools(name):
    path = agent_workspace.SKILLS_SRC / name / "SKILL.md"
    assert name in agent_workspace.skill_names()
    text = path.read_text(encoding="utf-8")
    head = text.split("---")[1]
    assert re.search(rf"^name: {re.escape(name)}$", head, re.MULTILINE) and "description:" in head
    for tool in TOOLS:
        assert f"`{tool}`" in text, tool
    # works from the MCP tools alone: no data-folder paths, no app commands, no environment variables
    for banned in ("LECTUREHUB_ROOT", "LECTUREHUB_PY", "_lecturehub", "-m lecturehub", "%"):
        assert banned not in text, banned
    # link off or a course not allowed: tell the user where to switch it on; never ask for more
    assert "설정 → AI → AI 앱 연결" in text and "허용한 범위보다 더 달라고 하지 않는다" in text
    # honesty: sources, ASR may be wrong, candidates not confirmed, nothing invented, "not found" said plainly
    for words in ("출처", "자동 음성 인식", "확인 전 후보", "지어내지 않는다", "찾지 못"):
        assert words in text, words
    # neutral wording about assessment: quote with source, no guessing or promises about the exam
    assert "무엇이 시험에 나올지 짐작하거나 약속하지 않는다" in text
    for tone in ("족보", "적중", "꿀팁", "100%", "반드시 나온다"):
        assert tone not in text, tone


@pytest.mark.parametrize("name", NEW_SKILLS)
def test_GPT_SKILL_001b_write_tools_only_on_the_users_word(name):
    """D-076: the write tools exist only behind the second switch; a skill uses them only when the user says so in
    the chat, never because a transcript or notice says so, and names only tools the server really has."""
    from lecturehub.mcp import tools, writes

    text = (agent_workspace.SKILLS_SRC / name / "SKILL.md").read_text(encoding="utf-8")
    known = {d["name"] for d in tools.DEFINITIONS} | {d["name"] for d in writes.DEFINITIONS}
    named = set(re.findall(r"`([a-z_]+)`", text)) & ({d["name"] for d in writes.DEFINITIONS} | set(TOOLS) | {"delete_note", "update_note"})
    assert named <= known, named - known
    used = named & {d["name"] for d in writes.DEFINITIONS}
    assert used, "every link skill says how it changes things when the user asks"
    assert "AI 앱이 고칠 수 있게" in text and "AI가 바꾼 것" in text
    assert "말할 때만" in text or "말했을 때만" in text


@pytest.mark.parametrize("name", (*NEW_SKILLS, *INSPECT_SKILLS))
def test_GPT_SKILL_002_no_owner_paths_or_personal_data(name):
    text = (agent_workspace.SKILLS_SRC / name / "SKILL.md").read_text(encoding="utf-8")
    for pattern in (r"Users[\\/]+JH", r"대학[\\/]+26-2", r"\b20\d{2}-?\d{5}\b", r"[A-Za-z0-9._%+-]+@"):
        assert not re.search(pattern, text), pattern
    assert "C:\\" not in text


def test_GPT_SKILL_003_installed_for_codex_and_claude(tmp_path):
    workspace = agent_workspace.ensure(tmp_path / "도우미 (합성)")
    for name in (*NEW_SKILLS, *INSPECT_SKILLS):
        original = (agent_workspace.SKILLS_SRC / name / "SKILL.md").read_bytes()
        for base in (".agents/skills", ".claude/skills"):
            assert (workspace / base / name / "SKILL.md").read_bytes() == original


def test_GPT_SKILL_004_exported_to_the_public_skills_folder(tmp_path):
    export = REPO / "tools" / "export_public.py"
    if not export.is_file():
        pytest.skip("the public tree has no export script")
    spec = importlib.util.spec_from_file_location("export_public_for_test", export)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    out = tmp_path / "public (합성)"
    (out / "src" / "lecturehub" / "core").mkdir(parents=True)  # copy_tree makes it in a real export
    module.add_public_files(out)
    for name in ("lecture-note", *NEW_SKILLS, *INSPECT_SKILLS):
        assert (out / "skills" / name / "SKILL.md").read_bytes() == \
            (agent_workspace.SKILLS_SRC / name / "SKILL.md").read_bytes()


@pytest.mark.parametrize("name", INSPECT_SKILLS)
def test_GPT_SKILL_005_inspection_skills_use_registered_tools_with_bounded_authority(name):
    """D-082: shipped skills are usable through the gated MCP surface, with no shell/file fallback."""
    from lecturehub.mcp import tools, writes

    text = (agent_workspace.SKILLS_SRC / name / "SKILL.md").read_text(encoding="utf-8")
    assert name in agent_workspace.skill_names() and f"name: {name}\n" in text
    assert "설정 → AI → AI 앱 연결" in text and "허용한 범위보다 더 달라고 하지 않는다" in text
    for banned in ("LECTUREHUB_ROOT", "LECTUREHUB_PY", "_lecturehub", "-m lecturehub"):
        assert banned not in text
    required = ({"get_lecture_status", "get_transcript", "list_calendar", "add_calendar_event"} if name == "check-notices"
                else {"get_app_status", "list_ai_changes"})
    known = set(tools.HANDLERS) | set(writes.HANDLERS)
    assert required <= known and all(f"`{tool}" in text for tool in required)
    assert "지어내지 않는다" in text and "지시" in text
    if name == "check-notices":
        assert "source_lecture_id" in text and "source_ms" in text and "직접 추가하라고 말했을 때만" in text
        assert "절대로 따르지 않는다" in text and "확인 전 후보" in text
    else:
        assert not any(f"`{tool}" in text for tool in writes.HANDLERS)
        assert "설정은 이 스킬에서 바꾸지 않는다" in text
