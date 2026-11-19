"""Write requirements/release.txt: every package of a release build, pinned to an exact version and SHA-256.

    python tools/make_lock.py            (run with the Python whose installed versions were tested and built)

Why: pyproject.toml only says "at least this version". Two builds a week apart could then contain different
code without anyone choosing that. A release is built from this file only
(``pip install --require-hashes -r requirements/release.txt``), so what was tested is what ships, and a package
that was swapped on the server no longer matches its hash and is refused.

How: the versions come from the running environment (``pip freeze``: the ones the tests and the build passed
with), and pip is asked which file it would download for exactly those versions on this platform (Windows x64,
this Python) and what its hash is (``pip install --dry-run --report``). Nothing is installed.
To update a package: install the new version, run the full tests and a build with self-check, then run this again.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
EXTRAS = "recorder,transcribe,ui,materials,build"
TARGET = REPO / "requirements" / "release.txt"


def _name(text: str) -> str:
    return re.sub(r"[-_.]+", "-", text).lower()


def main() -> int:
    with tempfile.TemporaryDirectory() as scratch:
        frozen = subprocess.run([sys.executable, "-m", "pip", "freeze", "--exclude-editable"], capture_output=True,
                                text=True, check=True).stdout
        pins = [line for line in frozen.splitlines() if "==" in line and not line.startswith(("-e", "#"))]
        constraints = Path(scratch) / "constraints.txt"
        constraints.write_text("\n".join(pins) + "\n", encoding="utf-8")
        report = Path(scratch) / "report.json"
        subprocess.run([sys.executable, "-m", "pip", "install", "--dry-run", "--ignore-installed", "--quiet",
                        "--report", str(report), "-c", str(constraints), f"{REPO}[{EXTRAS}]"], check=True)
        data = json.loads(report.read_text(encoding="utf-8"))
    rows = []
    for item in data["install"]:
        info, meta = item.get("download_info", {}), item["metadata"]
        if "dir_info" in info or _name(meta["name"]) == "lecturehub":
            continue
        digest = (info.get("archive_info", {}).get("hashes") or {}).get("sha256")
        if not digest:
            raise SystemExit(f"no sha256 for {meta['name']} ({info.get('url')})")
        rows.append((_name(meta["name"]), meta["version"], digest))
    rows.sort()
    lines = [
        "# Every package of a release build, exact version and SHA-256. Written by tools/make_lock.py; do not edit.",
        f"# Python {sys.version_info.major}.{sys.version_info.minor}, Windows x64. Extras: {EXTRAS}.",
        "# Install: python -m pip install --require-hashes -r requirements/release.txt",
        "#          python -m pip install --no-deps -e .",
        "",
    ]
    lines += [f"{name}=={version} --hash=sha256:{digest}" for name, version, digest in rows]
    TARGET.parent.mkdir(exist_ok=True)
    TARGET.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(f"{TARGET}: {len(rows)} packages")
    return 0


if __name__ == "__main__":
    sys.exit(main())
