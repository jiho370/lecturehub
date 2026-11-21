"""Package a finished build folder (``dist\\LectureHub`` from tools/build_app.py) as an MSIX for the Microsoft Store.

    python tools/build_msix.py --app <build>\\dist\\LectureHub --out <folder> \\
        --name <Package/Identity/Name> --publisher "CN=..." --publisher-display-name "<name>"

The three identity values come from Partner Center (the app's "Product identity" page) and must match exactly. The
Store signs the package after certification, so the result is uploaded unsigned. To try it on a machine before
that, sign a copy with a test certificate whose subject equals ``--publisher`` (the build workflow does this on its
throw-away runner; never on the owner's PC, where it would mean trusting a self-made certificate).

Needs makeappx.exe from the Windows SDK (present on GitHub's Windows runners) and Pillow (for the tile images).
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from string import Template

REPO = Path(__file__).resolve().parents[1]
MANIFEST = "packaging/AppxManifest.xml"
TILES = {"StoreLogo.png": (50, 50), "Square44x44Logo.png": (44, 44), "Square150x150Logo.png": (150, 150),
         "Wide310x150Logo.png": (310, 150)}
# Test identity for builds that are not uploaded (CI self-check). A real upload passes Partner Center's values.
TEST_NAME, TEST_PUBLISHER, TEST_PUBLISHER_DISPLAY = "LectureHub.Test", "CN=LectureHubTest", "Lecture Hub (test)"


def store_version(version: str) -> str:
    """1.2.3 → 1.2.3.0 (the Store needs four parts and reserves the last one, which must be 0)."""
    parts = version.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise ValueError(f"version must be major.minor.patch, got {version!r}")
    return ".".join(parts + ["0"])


def check_identity(name: str, publisher: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9.-]{3,50}", name):
        raise ValueError("package name: 3-50 letters, digits, dots or hyphens (copy it from Partner Center)")
    if not publisher.startswith("CN="):
        raise ValueError('publisher must look like "CN=..." (copy it from Partner Center)')


def manifest_text(source: str, *, name: str, publisher: str, publisher_display_name: str, version: str,
                  display_name: str = "Lecture Hub") -> str:
    from xml.sax.saxutils import escape

    values = {"name": name, "publisher": publisher, "publisher_display_name": publisher_display_name,
              "version": store_version(version), "display_name": display_name}
    return Template(source).substitute({k: escape(v, {'"': "&quot;"}) for k, v in values.items()})


def make_tiles(icon_png: Path, assets: Path) -> list[Path]:
    """Tile images from the app's own icon, centred on a transparent canvas."""
    from PIL import Image

    assets.mkdir(parents=True, exist_ok=True)
    source = Image.open(icon_png).convert("RGBA")
    made = []
    for filename, (width, height) in TILES.items():
        side = int(min(width, height) * (0.9 if width == height else 0.8))
        canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        tile = source.resize((side, side), Image.LANCZOS)
        canvas.paste(tile, ((width - side) // 2, (height - side) // 2), tile)
        target = assets / filename
        canvas.save(target)
        made.append(target)
    return made


def find_makeappx() -> Path | None:
    found = shutil.which("makeappx")
    if found:
        return Path(found)
    kits = Path(os.environ.get("ProgramFiles(x86)") or r"C:\Program Files (x86)") / "Windows Kits" / "10" / "bin"
    candidates = sorted(kits.glob("10.*/x64/makeappx.exe"), reverse=True) if kits.is_dir() else []
    return candidates[0] if candidates else None


def app_icon(*folders: Path) -> Path:
    """The app icon (the same in every edition): ``app.png`` in the first folder that has it."""
    for folder in folders:
        if (folder / "app.png").is_file():
            return folder / "app.png"
    raise SystemExit("app icon not found")


def app_version(source: Path = REPO / "src") -> str:
    """The version in this repository's ``__init__.py`` (the build carries only compiled modules, and
    tools/build_app.py stamps the same value into the exe)."""
    text = (source / "lecturehub" / "__init__.py").read_text(encoding="utf-8")
    found = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if not found:
        raise SystemExit("version not found")
    return found.group(1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--app", type=Path, required=True, help="the build folder with LectureHub.exe")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--name", default=os.environ.get("LECTUREHUB_MSIX_NAME") or TEST_NAME)
    parser.add_argument("--publisher", default=os.environ.get("LECTUREHUB_MSIX_PUBLISHER") or TEST_PUBLISHER)
    parser.add_argument("--publisher-display-name",
                        default=os.environ.get("LECTUREHUB_MSIX_PUBLISHER_DISPLAY") or TEST_PUBLISHER_DISPLAY)
    parser.add_argument("--version", default=None, help="major.minor.patch (default: this repository's version)")
    args = parser.parse_args(argv)
    app, out = args.app.resolve(), args.out.resolve()
    if not (app / "LectureHub.exe").is_file() or not (app / "LectureHubWorker.exe").is_file():
        raise SystemExit(f"{app} is not a build folder (LectureHub.exe and LectureHubWorker.exe expected)")
    if not (app / "_internal" / "lecturehub" / "core" / "edition.json").is_file():
        raise SystemExit("only the public edition goes to the Store (edition marker missing)")
    if (app / "NOT_FOR_RELEASE.txt").exists():
        raise SystemExit("this build is marked NOT FOR RELEASE (no pinned ffmpeg); build it with the pinned ffmpeg")
    check_identity(args.name, args.publisher)
    makeappx = find_makeappx()
    if makeappx is None:
        raise SystemExit("makeappx.exe not found (Windows SDK). It is on GitHub's Windows runners.")
    version = args.version or app_version()
    stage = out / "msix-stage"
    if stage.exists():
        shutil.rmtree(stage)
    shutil.copytree(app, stage)
    manifest = manifest_text((REPO / MANIFEST).read_text(encoding="utf-8"), name=args.name, publisher=args.publisher,
                             publisher_display_name=args.publisher_display_name, version=version)
    (stage / "AppxManifest.xml").write_text(manifest, encoding="utf-8")
    make_tiles(app_icon(app / "_internal" / "lecturehub" / "ui" / "assets", REPO / "src" / "lecturehub" / "ui" / "assets"),
               stage / "Assets")
    target = out / f"LectureHub-{version}-x64.msix"
    target.unlink(missing_ok=True)
    subprocess.run([str(makeappx), "pack", "/d", str(stage), "/p", str(target), "/o"], check=True)
    shutil.rmtree(stage)
    test = " (TEST identity: for the self-check only, not for upload)" if args.name == TEST_NAME else ""
    print(f"msix: {target} ({target.stat().st_size / 1e6:.0f} MB){test}\nidentity: {args.name} / {args.publisher}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
