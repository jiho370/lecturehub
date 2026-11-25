"""[Codex에 연결] / [Claude 데스크톱에 연결] / [ChatGPT(Codex)로 열기] (D-076). Synthetic files only.

The app writes only Lecture Hub's own entry into another app's settings file, after one native dialog whose token
is bound to the exact text; [연결 해제] takes out exactly what was added. Every other byte stays as it was.
"""
from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from lecturehub.mcp import connect

PROGRAM = r"C:\합성\LectureHub\python.exe"
OTHER = r"C:\예전\python.exe"


@pytest.fixture
def plain(monkeypatch):
    """A normal install: our fixed command, no env overrides."""
    monkeypatch.setattr(connect, "command", lambda: PROGRAM)
    monkeypatch.setattr(connect, "_env", lambda: {})
    return connect


def codex_file() -> Path:
    return connect.codex_path()


def claude_file() -> Path:
    return connect.claude_path()


def put(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


TOML_CASES = {
    "empty file": b"",
    "one key": b'model = "gpt-5"\n',
    "no final newline": b'model = "gpt-5"',
    "crlf": b'model = "gpt-5"\r\n\r\n[profiles.fast]\r\nmodel = "x"\r\n',
    "bom": b'\xef\xbb\xbf# \xec\x84\xa4\xec\xa0\x95\nmodel = "gpt-5"\n',
    "other servers": (b'# my settings\nmodel = "gpt-5"  # keep\n\n[mcp_servers.other]\ncommand = "node"\nargs = ["a.js"]\n\n'
                      b'[mcp_servers.other.env]\nTOKEN_NAME = "x"\n\n[projects."C:\\\\work"]\ntrust_level = "trusted"\n'),
    "trailing blank lines": b'model = "gpt-5"\n\n\n',
}


@pytest.mark.parametrize("name", list(TOML_CASES))
def test_codex_connect_then_disconnect_leaves_every_other_byte(plain, name):
    original = TOML_CASES[name]
    put(codex_file(), original)
    assert plain.status("codex")["state"] == "off"
    assert plain.connect("codex")["state"] == "connected"
    written = codex_file().read_bytes()
    data = tomllib.loads(written.decode("utf-8-sig"))
    assert data["mcp_servers"]["lecturehub"] == {"command": PROGRAM, "args": ["-m", "lecturehub.mcp"],
                                                 "default_tools_approval_mode": "approve", "tool_timeout_sec": 120}
    before = tomllib.loads(original.decode("utf-8-sig"))
    data["mcp_servers"].pop("lecturehub")
    if not data["mcp_servers"]:
        data.pop("mcp_servers")
    assert data == before  # nothing else changed in meaning
    assert written.startswith(original.rstrip(b"\r\n")) or not original.strip()  # the rest kept as it was, in front
    assert (b"\r\n" in written) == (b"\r\n" in original)  # the file's own line breaks
    assert plain.connect("codex")["state"] == "connected" and codex_file().read_bytes() == written  # again: same file
    assert plain.disconnect("codex")["state"] == "off"
    assert codex_file().read_bytes() == original  # byte for byte


def test_codex_block_is_replaced_in_place_when_the_command_moved(plain, monkeypatch):
    original = b'model = "gpt-5"\n'
    put(codex_file(), original)
    plain.connect("codex")
    put(codex_file(), codex_file().read_bytes() + b'\n[profiles.later]\nmodel = "y"\n')  # the user added more after it
    with_later = codex_file().read_bytes()
    monkeypatch.setattr(connect, "command", lambda: r"C:\새 위치\python.exe")
    assert connect.status("codex")["state"] == "elsewhere"
    assert connect.connect("codex")["state"] == "connected"
    moved = codex_file().read_bytes()
    assert moved.replace("새 위치".encode(), "합성\\\\LectureHub".encode()) == with_later  # only the command changed
    connect.disconnect("codex")
    assert codex_file().read_bytes() == original + b'\n[profiles.later]\nmodel = "y"\n'


def test_codex_env_overrides_go_into_a_subtable(monkeypatch):
    monkeypatch.setattr(connect, "command", lambda: PROGRAM)
    monkeypatch.setenv("LECTUREHUB_ROOT", r"C:\합성\학기")
    connect.connect("codex")
    entry = tomllib.loads(codex_file().read_text(encoding="utf-8"))["mcp_servers"]["lecturehub"]
    assert entry["env"]["LECTUREHUB_ROOT"] == r"C:\합성\학기" and "LECTUREHUB_SETTINGS" in entry["env"]
    assert connect.status("codex")["state"] == "connected"
    connect.connect("claude_desktop")
    assert json.loads(claude_file().read_text(encoding="utf-8"))["mcpServers"]["lecturehub"]["env"]["LECTUREHUB_ROOT"] == r"C:\합성\학기"
    monkeypatch.delenv("LECTUREHUB_ROOT")
    for app in ("codex", "claude_desktop"):  # the app now runs without that override
        assert connect.status(app)["state"] == "elsewhere" and connect.status(app)["why"] == "env"


def test_a_hand_typed_codex_table_is_replaced_where_it_is(plain):
    """Only command/args (what [연결] writes itself): replaced in place; the comment block that describes the next
    table stays with it (integration review)."""
    head = b'model = "gpt-5"\n\n'
    tail = b'\n# the other server: keep this note\n[mcp_servers.other]\ncommand = "node"\n'
    put(codex_file(), head + b'[mcp_servers.lecturehub]\ncommand = "C:\\\\old\\\\python.exe"\nargs = ["-m", "lecturehub.mcp"]\n' + tail)
    assert plain.status("codex")["state"] == "elsewhere" and "old" in plain.status("codex")["command"]
    assert plain.connect("codex")["state"] == "connected"
    text = codex_file().read_bytes()
    assert text.startswith(head) and text.endswith(tail) and b"old" not in text
    plain.disconnect("codex")
    assert codex_file().read_bytes() == head + tail


@pytest.mark.parametrize("extra", [b'startup_timeout_sec = 30\n', b'[mcp_servers.lecturehub.env]\nX = "1"\n',
                                   b'default_tools_approval_mode = "prompt"\n', b'tool_timeout_sec = 30\n'])
def test_a_hand_typed_table_with_keys_of_its_own_is_refused_and_shown(plain, extra):
    """Integration review: replacing it dropped startup_timeout_sec / env / the user's approval choice. Now nothing
    is written and the dialog shows the user's own text."""
    text = (b'[mcp_servers.lecturehub]\ncommand = "C:\\\\old\\\\python.exe"\nargs = ["-m", "lecturehub.mcp"]\n' + extra
            + b'\n[mcp_servers.other]\ncommand = "node"\n')
    put(codex_file(), text)
    with pytest.raises(connect.ConfigError) as refused:
        plain.preview("codex")
    assert refused.value.original and extra.decode().strip().splitlines()[-1] in refused.value.original
    assert "[mcp_servers.other]" not in refused.value.original and "지금 들어 있는 내용" in str(refused.value)
    with pytest.raises(connect.ConfigError):
        plain.connect("codex")
    assert codex_file().read_bytes() == text


def test_disconnect_refuses_when_lecturehub_subtables_sit_outside_the_markers(plain):
    """Integration review: [연결 해제] left [mcp_servers.lecturehub.*] behind = a server entry without a command."""
    plain.connect("codex")
    put(codex_file(), codex_file().read_bytes() + b'\n[mcp_servers.lecturehub.tools.search]\napproval_mode = "approve"\n')
    before = codex_file().read_bytes()
    with pytest.raises(connect.ConfigError, match="표시 밖"):
        plain.disconnect("codex")
    assert codex_file().read_bytes() == before


def test_the_settings_file_as_a_symlink_stays_a_link_and_the_target_is_written(plain, tmp_path):
    """Integration review: os.replace on the link replaced the link itself with a plain file."""
    real = tmp_path / "dotfiles" / "codex.toml"
    real.parent.mkdir()
    real.write_bytes(b'model = "gpt-5"\n')
    codex_file().parent.mkdir(parents=True, exist_ok=True)
    try:
        codex_file().symlink_to(real)
    except OSError:
        pytest.skip("symbolic links are not allowed for this Windows account")
    assert plain.connect("codex")["state"] == "connected"
    assert codex_file().is_symlink() and b"[mcp_servers.lecturehub]" in real.read_bytes()
    assert not list(real.parent.glob("*.lecturehub-part")) and not list(codex_file().parent.glob("*.lecturehub-part"))


def test_our_command_typed_by_hand_is_manual_and_stays_on_disconnect(plain):
    typed = ('[mcp_servers.lecturehub]\ncommand = ' + json.dumps(PROGRAM, ensure_ascii=False) + '\nargs = ["-m", "lecturehub.mcp"]\n').encode()
    put(codex_file(), typed)
    assert plain.status("codex")["state"] == "manual"
    assert plain.disconnect("codex")["state"] == "manual" and codex_file().read_bytes() == typed


@pytest.mark.parametrize("text", [
    b'[mcp_servers]\nlecturehub = { command = "x", args = [] }\n',  # an inline table
    b'mcp_servers.lecturehub.command = "x"\n',  # dotted keys
])
def test_codex_entry_in_another_shape_is_not_rewritten(plain, text):
    put(codex_file(), text)
    with pytest.raises(connect.ConfigError, match="다른 모양"):
        plain.connect("codex")
    assert codex_file().read_bytes() == text
    assert not codex_file().with_name("config.toml.lecturehub-bak").exists()


def test_invalid_toml_is_refused_untouched(plain):
    broken = b'model = "gpt-5\n[mcp_servers\n'
    put(codex_file(), broken)
    assert plain.status("codex")["state"] == "error"
    for act in (plain.connect, plain.disconnect):
        with pytest.raises(connect.ConfigError, match="올바른 TOML이 아니라"):
            act("codex")
    assert codex_file().read_bytes() == broken
    assert sorted(p.name for p in codex_file().parent.iterdir()) == ["config.toml"]  # no backup, no part file


def test_not_utf8_is_refused_untouched(plain):
    put(codex_file(), 'model = "가"\n'.encode("cp949"))
    with pytest.raises(connect.ConfigError, match="UTF-8"):
        plain.connect("codex")


def test_backup_is_made_once_and_never_for_a_new_file(plain, monkeypatch):
    plain.connect("codex")  # no file yet: nothing to keep
    backup = codex_file().with_name("config.toml" + connect.BACKUP_SUFFIX)
    assert not backup.exists()
    original = b'model = "gpt-5"\n'
    put(codex_file(), original)
    plain.connect("codex")
    assert backup.read_bytes() == original
    put(codex_file(), codex_file().read_bytes() + b'\n[profiles.p]\nmodel = "z"\n')
    monkeypatch.setattr(connect, "command", lambda: OTHER)
    connect.connect("codex")
    connect.disconnect("codex")
    assert backup.read_bytes() == original  # the very first state, kept


# ---------- Claude desktop (JSON)
JSON_CASES = {
    "empty object": b"{}",
    "other keys": b'{\n  "theme": "dark",\n  "globalShortcut": ""\n}\n',
    "empty servers": b'{"mcpServers": {}}',
    "other server": b'{\n  "mcpServers": {\n    "files": {"command": "npx", "args": ["-y", "x"]}\n  },\n  "theme": "dark"\n}\n',
    "bom crlf": b'\xef\xbb\xbf{\r\n  "theme": "\xea\xb0\x80"\r\n}\r\n',
    "escaped strings": b'{"note": "a \\"quoted\\" } brace", "mcpServers": {"s": {"command": "c:\\\\x"}}}',
}


@pytest.mark.parametrize("name", list(JSON_CASES))
def test_claude_connect_then_disconnect_leaves_every_other_byte(plain, name):
    original = JSON_CASES[name]
    put(claude_file(), original)
    assert plain.status("claude_desktop")["state"] == "off"
    assert plain.connect("claude_desktop")["state"] == "connected"
    written = claude_file().read_bytes()
    data = json.loads(written.decode("utf-8-sig"))
    assert data["mcpServers"]["lecturehub"] == {"command": PROGRAM, "args": ["-m", "lecturehub.mcp"]}
    before = json.loads(original.decode("utf-8-sig"))
    data["mcpServers"].pop("lecturehub")
    if not data["mcpServers"] and "mcpServers" not in before:
        data.pop("mcpServers")
    assert data == before
    assert plain.connect("claude_desktop")["state"] == "connected" and claude_file().read_bytes() == written
    assert plain.disconnect("claude_desktop")["state"] == "off"
    assert claude_file().read_bytes() == original


def test_claude_new_file_and_an_older_entry(plain):
    assert plain.connect("claude_desktop")["state"] == "connected"  # no file yet
    assert json.loads(claude_file().read_text(encoding="utf-8"))["mcpServers"]["lecturehub"]["command"] == PROGRAM
    plain.disconnect("claude_desktop")
    assert json.loads(claude_file().read_text(encoding="utf-8")) == {}
    older = b'{"mcpServers": {"lecturehub": {"command": "C:\\\\old\\\\python.exe", "args": ["-m", "lecturehub.mcp"]}, "x": {"command": "y"}}}'
    put(claude_file(), older)
    assert plain.status("claude_desktop")["state"] == "elsewhere"
    plain.connect("claude_desktop")
    data = json.loads(claude_file().read_text(encoding="utf-8"))
    assert data["mcpServers"]["lecturehub"]["command"] == PROGRAM and data["mcpServers"]["x"] == {"command": "y"}


@pytest.mark.parametrize("text", [b'{"theme": "dark",}', b"[1, 2]", b'{"mcpServers": []}', b"{"])
def test_claude_invalid_json_is_refused_untouched(plain, text):
    put(claude_file(), text)
    assert plain.status("claude_desktop")["state"] == "error"
    for act in (plain.connect, plain.disconnect):
        with pytest.raises(connect.ConfigError):
            act("claude_desktop")
    assert claude_file().read_bytes() == text
    assert sorted(p.name for p in claude_file().parent.iterdir()) == ["claude_desktop_config.json"]


def test_claude_backup_is_made_once(plain, monkeypatch):
    original = b'{"theme": "dark"}'
    put(claude_file(), original)
    plain.connect("claude_desktop")
    backup = claude_file().with_name("claude_desktop_config.json" + connect.BACKUP_SUFFIX)
    assert backup.read_bytes() == original
    monkeypatch.setattr(connect, "command", lambda: OTHER)
    connect.connect("claude_desktop")
    assert backup.read_bytes() == original


@pytest.fixture
def store_build(monkeypatch):
    from lecturehub.core import package

    monkeypatch.setenv("LECTUREHUB_FAKE_PACKAGE", "LectureHub_1.1.0.0_x64__synthetic")
    package.full_name.cache_clear()
    yield
    monkeypatch.delenv("LECTUREHUB_FAKE_PACKAGE")
    package.full_name.cache_clear()


def test_store_build_offers_paste_only_for_claude_desktop(plain, store_build):
    state = plain.status("claude_desktop")
    assert state["state"] == "unsupported" and "붙여 넣어" in state["message"]
    for act in (plain.connect, plain.preview):
        with pytest.raises(connect.ConfigError):
            act("claude_desktop")
    assert not claude_file().exists()
    assert plain.status("codex")["state"] == "off"  # Codex's file is under the user folder: not redirected


def test_unknown_app_is_refused(plain):
    for act in (plain.connect, plain.disconnect):
        with pytest.raises(connect.ConfigError):
            act("../settings")


# ---------- the app window: [연결] only through the native dialog's token, bound to the text it showed
class _Window:
    def __init__(self, answer):
        self.answer, self.asked = answer, []

    def create_confirmation_dialog(self, title, message):
        self.asked.append((title, message))
        return self.answer


class _Backend:
    def __init__(self):
        self.connected = []

    def ai_link_connect(self, app):
        self.connected.append(app)
        return {"ok": True, **connect.connect(app)}


class _Library:
    def __init__(self, root):
        self.root = root


def test_connect_needs_the_native_dialogs_token(plain, tmp_path, monkeypatch):
    from lecturehub.ui.app import Api

    backend = _Backend()
    api = Api(backend, _Library(tmp_path))
    assert api.ai_link_connect("codex")["needs_confirm"] and backend.connected == []  # straight from page JS
    assert api.ai_link_connect("codex", "made-up")["ok"] is False
    api._main = _Window(False)
    assert api.ai_link_connect_confirm("codex") == {"ok": True, "yes": False}
    api._main = _Window(True)
    q = api.ai_link_connect_confirm("codex")
    title, message = api._main.asked[-1]
    assert "Codex" in title and str(codex_file()) in message and "[mcp_servers.lecturehub]" not in message  # short
    assert f"실행할 프로그램: {PROGRAM}" in message  # the program that will be started, in plain form
    assert api.ai_link_connect("claude_desktop", q["token"])["ok"] is False  # another app's token
    q = api.ai_link_connect_confirm("codex")
    monkeypatch.setattr(connect, "command", lambda: OTHER)  # what would be written is not what was shown
    assert api.ai_link_connect("codex", q["token"])["ok"] is False and backend.connected == []
    monkeypatch.setattr(connect, "command", lambda: PROGRAM)
    q = api.ai_link_connect_confirm("codex")
    assert api.ai_link_connect("codex", q["token"])["state"] == "connected" and backend.connected == ["codex"]
    assert api.ai_link_connect("codex", q["token"])["ok"] is False  # one time only
    assert api.ai_link_connect_confirm("nonsense")["ok"] is False


# ---------- [ChatGPT(Codex)로 열기]: the server for that run only, the user's config.toml untouched
def test_codex_args_set_the_server_for_one_run(plain, tmp_path):
    args = connect.codex_args(r"C:\npm\codex.cmd", tmp_path, "01JSYNTHETIC")
    joined = " ".join(args)
    assert args[0] == r"C:\npm\codex.cmd"
    assert f"mcp_servers.lecturehub.command='{PROGRAM}'" in args
    assert "mcp_servers.lecturehub.args=['-m','lecturehub.mcp']" in args
    assert "mcp_servers.lecturehub.default_tools_approval_mode='approve'" in args
    assert "mcp_servers.lecturehub.tool_timeout_sec=120" in args  # D-080: room for Lecture Hub's 50 s dialog
    assert "--sandbox read-only" in joined and args[args.index("-C") + 1] == str(tmp_path)
    assert "01JSYNTHETIC" in args[-1]
    for bad in ("x y", "a&b", "../x"):
        with pytest.raises(ValueError):
            connect.codex_args("codex", tmp_path, bad)
    assert not codex_file().exists()


def test_open_codex_through_the_launcher_and_the_missing_codex_message(plain, monkeypatch):
    from lecturehub.ai.codex import CodexEngine

    seen = []
    monkeypatch.setattr(CodexEngine, "executable", lambda self: r"C:\npm\codex.cmd")
    connect.open_codex("01JSYNTHETIC", launcher=lambda command, cwd: seen.append((command, cwd)))
    command, cwd = seen[0]
    assert command.startswith('cmd.exe /d /s /k ""C:\\npm\\codex.cmd" "-c"') and "mcp_servers.lecturehub.command=" in command
    assert Path(cwd).is_dir() and not codex_file().exists()
    monkeypatch.setattr(CodexEngine, "executable", lambda self: None)
    with pytest.raises(FileNotFoundError, match="Codex\\(codex.cmd\\)를 찾지 못했습니다"):
        connect.open_codex(launcher=lambda *a: seen.append(a))
    assert len(seen) == 1


def test_open_codex_button_is_refused_while_the_link_is_off(plain, tmp_path, monkeypatch):
    from lecturehub.ai.codex import CodexEngine
    from lecturehub.core import settings
    from lecturehub.ui.app import Api

    monkeypatch.setattr(CodexEngine, "executable", lambda self: r"C:\npm\codex.cmd")
    api = Api(_Backend(), _Library(tmp_path))
    seen = []
    api._codex_launcher = lambda command, cwd: seen.append(command)
    answer = api.ai_link_open_codex()
    assert answer["ok"] is False and "꺼져 있습니다" in answer["error"] and seen == []
    settings.set_ai_link(True)
    assert api.ai_link_open_codex("01JSYNTHETIC") == {"ok": True} and len(seen) == 1
    monkeypatch.setattr(CodexEngine, "executable", lambda self: None)
    assert "찾지 못했습니다" in api.ai_link_open_codex()["error"]


# ---------- integration review
def test_the_connect_dialog_warns_about_writes_and_shows_a_refused_hand_table(plain, tmp_path):
    """Finding 3: the dialog said nothing about Codex running the write tools without asking. Finding 5: a hand-typed
    table that would lose keys is shown in a dialog, not replaced."""
    from lecturehub.ui.app import AI_WRITES_WARNING, CODEX_APPROVE_NOTE, Api

    api = Api(_Backend(), _Library(tmp_path))
    api._main = _Window(False)
    api.ai_link_connect_confirm("codex")
    message = api._main.asked[-1][1]
    assert AI_WRITES_WARNING in message and CODEX_APPROVE_NOTE in message
    assert "숨은 문장" in message and "되돌릴 수 있습니다" in message and ".lecturehub-bak" in message
    # D-080: Lecture Hub asks first per kind; Codex's own prompt is replaced by that dialog
    assert "처음에 Lecture Hub가 묻습니다" in message and "Codex의 자체 확인 대신" in message
    fixed = message.replace(str(codex_file()), "").split("실행할 프로그램:")[0] + message.split(".lecturehub-bak", 1)[1]
    assert len(fixed) < 320 and "넣을 내용" not in message  # user 2026-11-25: keep it short (paths not counted)
    api.ai_link_connect_confirm("claude_desktop")
    message = api._main.asked[-1][1]
    assert AI_WRITES_WARNING in message and CODEX_APPROVE_NOTE not in message
    put(codex_file(), b'[mcp_servers.lecturehub]\ncommand = "x"\nstartup_timeout_sec = 30\n')
    before = codex_file().read_bytes()
    out = api.ai_link_connect_confirm("codex")
    title, message = api._main.asked[-1]
    assert out["ok"] is False and title == "연결하지 않았습니다" and "startup_timeout_sec = 30" in message
    assert codex_file().read_bytes() == before


def test_open_codex_takes_the_api_keys_out_and_refuses_command_characters(plain, tmp_path, monkeypatch):
    """The console runs on the user's ChatGPT login (no API key), and a workspace path with cmd.exe symbols is
    refused before anything starts."""
    import os

    from lecturehub.ai.codex import CodexEngine

    monkeypatch.setattr(CodexEngine, "executable", lambda self: r"C:\npm\codex.cmd")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-synthetic-not-a-key")
    monkeypatch.setenv("CODEX_API_KEY", "synthetic-not-a-key")
    started = []
    connect.open_codex(popen=lambda command, **kw: started.append((command, kw)))
    command, kw = started[0]
    assert "OPENAI_API_KEY" not in kw["env"] and "CODEX_API_KEY" not in kw["env"]
    assert kw["env"]["LECTUREHUB_SETTINGS"] == os.environ["LECTUREHUB_SETTINGS"]
    assert os.environ["OPENAI_API_KEY"] == "sk-synthetic-not-a-key"  # only the console's copy loses it
    assert command.startswith('cmd.exe /d /s /k ""C:\\npm\\codex.cmd"') and Path(kw["cwd"]).is_dir()
    for bad in ("a&b", "x%PATH%y", "c^d"):
        monkeypatch.setenv("LECTUREHUB_AGENT_WORKSPACE", str(tmp_path / bad))
        with pytest.raises(ValueError, match="명령 기호"):
            connect.open_codex(popen=lambda *a, **k: started.append(a))
    assert len(started) == 1 and not codex_file().exists()
