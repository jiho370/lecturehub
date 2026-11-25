"""Recording must never inspect the builder's accounts, credentials or semester."""
import os
import sys
from pathlib import Path
from unittest.mock import patch


def isolate():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    work = Path(__file__).resolve().parents[2] / "work"
    for name in ("LECTUREHUB_SETTINGS", "LECTUREHUB_RECORDER_CONFIG"):
        value = os.environ.get(name)
        if not value or not Path(value).resolve().is_relative_to(work):
            raise SystemExit(f"{name} must explicitly point inside this worktree's work/")
    scratch = Path(os.environ["LECTUREHUB_SETTINGS"]).parent
    for name in ("APPDATA", "LOCALAPPDATA", "USERPROFILE", "HOME", "CODEX_HOME", "CLAUDE_CONFIG_DIR"):
        target = scratch / name.lower()
        target.mkdir(parents=True, exist_ok=True)
        os.environ[name] = str(target)
    os.environ["LECTUREHUB_EDITION"] = "public"
    os.environ["PYTHON_KEYRING_BACKEND"] = "keyring.backends.null.Keyring"

    def audit(event, args):
        if event == "socket.connect":
            raise RuntimeError("network forbidden while recording the trial")
        if event == "open" and isinstance(args[0], (str, bytes)):
            name = os.fsdecode(args[0]).replace("\\", "/").split("/")[-1].lower()
            if name in {"auth.json", "credentials.json", "tokens.json", "token.json", ".credentials.json"}:
                raise RuntimeError("credential reads forbidden while recording the trial")
    sys.addaudithook(audit)
    patch("lecturehub.core.connections._stored", return_value=None).start()
    patch("lecturehub.core.connections._from_env", return_value=None).start()
    patch("lecturehub.index.etl_sync.load_token", return_value=None).start()
    # Presence is a scenario state. Never inspect a credential or invoke a logged-in CLI.
    patch("lecturehub.core.tools.codex_signed_in", return_value=True).start()
    patch("lecturehub.ui.backend.Backend.engine_status", return_value={"google": "ok", "whisper": "ok", "mai": "off"}).start()
    patch("lecturehub.core.startup.status", return_value={"ok": True, "store": False,
                                                        "enabled": False, "possible": True}).start()
    return scratch
