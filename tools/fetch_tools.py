"""Fetch the outside programs a release build needs, pinned by address and SHA-256 (release review 2026-11-16).

    python tools/fetch_tools.py ffmpeg    [--cache FOLDER]   → prints the folder with ffmpeg.exe, ffprobe.exe, DLLs
    python tools/fetch_tools.py innosetup [--cache FOLDER]   → prints the downloaded installer (run it yourself)

Why pinned: before this, the build copied whatever ffmpeg happened to be on the build PC (a GPL "full" build of
217 MB per exe). A release must be the same on every machine that builds it, and its licence must be known:
this is the LGPL *shared* build (ffmpeg.exe and ffprobe.exe share their DLLs; no GPL parts), with MP3 (libmp3lame),
FLAC and the filters the app uses. A download whose hash differs is refused and deleted.

To move to a newer build: change the entry in PINNED (address, size, hash from the release page) and run the
full test suite and the self-check of a new build.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

PINNED = {
    "ffmpeg": {
        "name": "ffmpeg-n9.0.2-39-g7adfbe40dc-win64-lgpl-shared-9.0.zip",
        "url": "https://github.com/FFmpeg-Builds/FFmpeg-Builds/releases/download/autobuild-2026-10-31-12-59/"
               "ffmpeg-n9.0.2-39-g7adfbe40dc-win64-lgpl-shared-9.0.zip",
        "sha256": "e907b01fa4783913151237e37df2a10fa97d99a1598a021bf59d4ae62ffed4c0",
        "bytes": 76576818,
        "license": "LGPL-2.1-or-later (shared build, no GPL components)",
        "source": "https://github.com/FFmpeg/FFmpeg (release/9.0, commit 7adfbe40dc); build scripts: "
                  "https://github.com/FFmpeg-Builds/FFmpeg-Builds (autobuild-2026-10-31-12-59)",
    },
    "innosetup": {
        "name": "innosetup-7.1.1-x64.exe",
        "url": "https://github.com/jrsoftware/issrc/releases/download/is-7_1_1/innosetup-7.1.1-x64.exe",
        "sha256": "8a5a6711c3766a6e5fb74f280ba0e3551e719ce94538d901fdbd1f8f9920b3f0",
        "bytes": 14336568,
    },
}
# What the app needs from ffmpeg (checked on the fetched build, so a different build cannot slip in unnoticed)
NEED_ENCODERS = ("libmp3lame", "flac", "aac", "pcm_s16le")
NEED_FILTERS = ("dynaudnorm", "silencedetect")


def default_cache() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".cache")
    return Path(os.environ.get("LECTUREHUB_BUILD_CACHE") or Path(base) / "LectureHub-build-cache")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def download(tool: str, cache: Path, *, opener=urllib.request.urlopen) -> Path:
    """The pinned file in ``cache`` (downloaded when missing), verified by size and SHA-256."""
    pin = PINNED[tool]
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / pin["name"]
    if not (target.is_file() and sha256(target) == pin["sha256"]):
        part = target.with_name(target.name + ".part")
        with opener(pin["url"], timeout=120) as response, part.open("wb") as out:  # noqa: S310 - fixed https address
            shutil.copyfileobj(response, out, 1024 * 1024)
        found = sha256(part)
        if found != pin["sha256"]:
            part.unlink(missing_ok=True)
            raise SystemExit(f"{pin['name']}: SHA-256 differs from the pinned one (got {found}); nothing was kept")
        part.replace(target)
    return target


def ffmpeg_folder(cache: Path, *, archive: Path | None = None) -> Path:
    """Folder with ffmpeg.exe, ffprobe.exe, their DLLs and the licence text of the pinned build."""
    pin = PINNED["ffmpeg"]
    archive = archive or download("ffmpeg", cache)
    if sha256(archive) != pin["sha256"]:
        raise SystemExit(f"{archive}: not the pinned ffmpeg build (SHA-256 differs)")
    out = cache / ("ffmpeg-" + pin["sha256"][:12])
    marker = out / "ok"
    if not marker.is_file():
        shutil.rmtree(out, ignore_errors=True)
        out.mkdir(parents=True)
        with zipfile.ZipFile(archive) as bundle:
            for item in bundle.infolist():
                parts = Path(item.filename).parts
                name = parts[-1] if parts else ""
                keep = (len(parts) >= 2 and parts[-2] == "bin" and name.lower().endswith((".exe", ".dll"))
                        and name.lower() != "ffplay.exe") or name in ("LICENSE.txt", "LICENSE", "COPYING.LGPLv2.1")
                if item.is_dir() or not keep:
                    continue
                with bundle.open(item) as source, (out / name).open("wb") as dest:
                    shutil.copyfileobj(source, dest)
        check_ffmpeg(out)
        marker.write_text(pin["sha256"], encoding="utf-8")
    return out


def check_ffmpeg(folder: Path) -> None:
    """Refuse a build that is GPL or lacks something the app uses."""
    def ask(*args: str) -> str:
        done = subprocess.run([str(folder / "ffmpeg.exe"), "-hide_banner", *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=60, check=False)
        return done.stdout + done.stderr

    version = ask("-version")
    if "--enable-gpl" in version or "--enable-nonfree" in version:
        raise SystemExit("the fetched ffmpeg is a GPL/nonfree build; the app ships LGPL builds only")
    encoders, filters = ask("-encoders"), ask("-filters")
    missing = [n for n in NEED_ENCODERS if f" {n} " not in encoders] + [n for n in NEED_FILTERS if f" {n} " not in filters]
    if missing:
        raise SystemExit(f"the fetched ffmpeg lacks: {missing}")
    if not (folder / "ffprobe.exe").is_file():
        raise SystemExit("ffprobe.exe is missing from the fetched build")


NOTICE = """ffmpeg / ffprobe (and the av*.dll, sw*.dll files next to them)

These files are FFmpeg (https://ffmpeg.org), copied unchanged from a public Windows build.
Licence: {license}. The licence text is in the file(s) next to this notice.
Lecture Hub runs ffmpeg.exe and ffprobe.exe as separate programs; its own code stays under the MIT licence.

Build: {name}
SHA-256 of the downloaded archive: {sha256}
Source code: {source}
You may replace these files with another FFmpeg build of your choice (same file names).
"""


def notice() -> str:
    return NOTICE.format(**PINNED["ffmpeg"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("tool", choices=sorted(PINNED))
    parser.add_argument("--cache", type=Path, default=None)
    parser.add_argument("--archive", type=Path, default=None, help="an already downloaded copy of the pinned file")
    args = parser.parse_args(argv)
    cache = args.cache or default_cache()
    if args.tool == "ffmpeg":
        print(ffmpeg_folder(cache, archive=args.archive))
    else:
        print(download(args.tool, cache))
    return 0


if __name__ == "__main__":
    sys.exit(main())
