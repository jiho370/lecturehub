"""Shared test settings."""
import json
import os

import pytest

# P-053: after finalize the recorder compacts the session and sends WAVs to the Windows Recycle Bin.
# Tests that record and finalize (also in child processes) must never put files into the real bin;
# tests of compaction itself switch this off and pass a fake bin.
os.environ["LECTUREHUB_NO_AUTO_COMPACT"] = "1"


@pytest.fixture(autouse=True)
def _isolated_user_settings(tmp_path_factory, monkeypatch):
    """Every test reads its own settings file, never the real %APPDATA%\\LectureHub\\settings.json.

    It carries the conventions the existing fixtures use: a notes folder of an older semester (수업정리html) and
    the detailed folder layout with a copy of each recording under 녹음본. The layout is written here because its
    default differs by edition (basic, no copies, in the public one) and this suite also runs in the public tree;
    a test about the default removes the key.
    Child processes inherit the variable.
    """
    path = tmp_path_factory.mktemp("settings") / "settings.json"
    path.write_text(json.dumps({"legacy_note_folders": ["수업정리html"],
                                "folders": {"style": "detailed", "archive_copies": True}}, ensure_ascii=False),
                    encoding="utf-8")
    monkeypatch.setenv("LECTUREHUB_SETTINGS", str(path))
    monkeypatch.setenv("LECTUREHUB_AGENT_WORKSPACE", str(path.parent / "agent-workspace"))
    monkeypatch.setenv("LECTUREHUB_LOG_DIR", str(path.parent / "logs"))  # never the real %LOCALAPPDATA% log (core/applog.py)
    monkeypatch.setenv("LECTUREHUB_UPDATE_REPO", "")  # no test asks GitHub for a newer version (core/update.py)
    # [Codex에 연결] / [Claude 데스크톱에 연결] (D-076): never the user's real config.toml or Claude settings file
    monkeypatch.setenv("CODEX_HOME", str(path.parent / "codex-home"))
    monkeypatch.setenv("LECTUREHUB_CLAUDE_DESKTOP_CONFIG", str(path.parent / "claude" / "claude_desktop_config.json"))
    monkeypatch.delenv("LECTUREHUB_ROOT", raising=False)
    monkeypatch.delenv("LECTUREHUB_AI_ENGINE", raising=False)


@pytest.fixture(autouse=True)
def _no_real_confirmation_dialog(monkeypatch):
    """D-080: no test ever shows the AI app link's native confirmation dialog. Unanswered here means "the dialog
    could not be shown" (nothing changes); a test that needs an answer replaces ``approve.dialog`` itself. Child
    processes do not get this: their tests set the kinds to automatic in settings instead."""
    from lecturehub.mcp import approve

    monkeypatch.setattr(approve, "dialog", lambda *args, **kwargs: approve.FAILED)
    monkeypatch.setattr(approve, "notice", lambda *args, **kwargs: None)  # D-083 M1: the OK-only notice, never real
    monkeypatch.setattr(approve, "_last_notice", [float("-inf")])


@pytest.fixture(autouse=True)
def _isolated_credentials(monkeypatch):
    """No test reads or writes the user's real credential store or inherited API keys."""
    import keyring

    values = {}
    monkeypatch.setattr(keyring, "get_password", lambda service, name: values.get((service, name)))
    monkeypatch.setattr(keyring, "set_password", lambda service, name, value: values.__setitem__((service, name), value))

    def delete(service, name):
        if (service, name) not in values:
            raise keyring.errors.PasswordDeleteError()
        del values[(service, name)]

    monkeypatch.setattr(keyring, "delete_password", delete)
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GROQ_API_KEY", "LECTUREHUB_ETL_TOKEN", "OPENAI_API_KEY", "CODEX_API_KEY"):
        monkeypatch.delenv(name, raising=False)
