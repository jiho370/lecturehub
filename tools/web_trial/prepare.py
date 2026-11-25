"""Create a fresh isolated trial, then configure only its scratch settings."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

for stream in (sys.stdout, sys.stderr):
    stream.reconfigure(encoding="utf-8")
work = Path(__file__).resolve().parents[2] / "work"
work.mkdir(exist_ok=True)
scratch = Path(tempfile.mkdtemp(prefix="trial-110-", dir=work))
os.environ["LECTUREHUB_SETTINGS"] = str(scratch / "settings.json")
os.environ["LECTUREHUB_RECORDER_CONFIG"] = str(scratch / "recorder.toml")
os.environ.pop("LECTUREHUB_ROOT", None)
from safety import isolate  # noqa: E402

isolate()
subprocess.run([sys.executable, "-m", "lecturehub.index.demo"], check=True)
from lecturehub.core import settings  # noqa: E402
from lecturehub.ui.library import Library  # noqa: E402

assert Library.make_trial()["ok"]
settings.save({"labs": {"gsr": False}, "ai_link": {"enabled": True, "write": True, "auto": {"save_note": True}},
               "reminders": {"deadlines": True, "briefing": True, "muted_courses": ["관개배수공학"]}})
(work / "scratch-root.txt").write_text(str(scratch), encoding="utf-8")
print(json.dumps({"scratch": str(scratch)}, ensure_ascii=False))
