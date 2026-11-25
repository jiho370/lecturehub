"""Entry point of the installed app (LectureHub.exe, windowed)."""
import os
import sys
from pathlib import Path

if getattr(sys, "frozen", False):  # the bundled ffmpeg/ffprobe (bin\) come first; child jobs inherit PATH
    os.environ["PATH"] = str(Path(sys.executable).parent / "bin") + os.pathsep + os.environ.get("PATH", "")
else:  # the development Run entry must open this checkout, independent of login's working directory
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lecturehub.ui.launch import run  # noqa: E402 - logging, WebView2 check, start-up failure message, --smoke

if __name__ == "__main__":
    raise SystemExit(run())
