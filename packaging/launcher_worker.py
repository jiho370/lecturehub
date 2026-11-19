"""Console helper of the installed app (LectureHubWorker.exe).

The app starts its background jobs (recorder, transcription, captions) as ``python -m <module> args``. In the
installed build there is no python.exe, so the app runs this program with the same arguments instead.
"""
import os
import runpy
import sys
from pathlib import Path

if getattr(sys, "frozen", False):
    os.environ["PATH"] = str(Path(sys.executable).parent / "bin") + os.pathsep + os.environ.get("PATH", "")


def main() -> int:
    args = sys.argv[1:]
    if len(args) >= 2 and args[0] == "-m":
        module, rest = args[1], args[2:]
    else:
        module, rest = "lecturehub", args
    sys.argv = [module, *rest]
    runpy.run_module(module, run_name="__main__", alter_sys=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
