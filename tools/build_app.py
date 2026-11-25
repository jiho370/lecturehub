"""Build the installable Windows app from the PUBLIC edition (2026-11-13; release review 2026-11-16).

    python tools/build_app.py [--out FOLDER] [--installer] [--ffmpeg pinned|path|none] [--cache FOLDER]

1. exports the public edition into FOLDER/public (tools/export_public.py; personal-data scan must be clean). In the
   public repository, which is that export and does not carry the export tool, it builds from the repository itself. The scan
   also looks for the terms in LECTUREHUB_PRIVATE_TERMS (a local text file OUTSIDE the repository, one term per line)
   and the course names in the configured semester's 시간표.csv; it warns when it has neither,
2. runs PyInstaller (packaging/lecturehub.spec) on that export: FOLDER/dist/LectureHub with LectureHub.exe and
   LectureHubWorker.exe and the windowed login launcher LectureHubStartup.exe, all stamped with the app version,
3. refuses the result when it lacks the public-edition marker or carries a library that must not ship (AGPL),
4. puts ffmpeg/ffprobe into LectureHub/bin: by default the pinned LGPL shared build (tools/fetch_tools.py: fixed
   address and SHA-256, checked for the encoders and filters the app uses). ``--ffmpeg path`` copies the ffmpeg on
   this PC instead (a local try-out only: such a build is marked NOT FOR RELEASE and no installer is made),
5. writes the licence list of every Python package that really is in the build (LectureHub/licenses),
6. zips the folder: FOLDER/LectureHub-<version>-win64.zip, and with ``--installer`` compiles
   packaging/installer.iss with Inno Setup: FOLDER/LectureHub-<version>-win64-setup.exe,
7. writes FOLDER/SHA256SUMS.txt.
Nothing is written into the repository. Needs PyInstaller in the running Python.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ENV_TERMS = "LECTUREHUB_PRIVATE_TERMS"
ENV_ISCC = "LECTUREHUB_ISCC"
# AGPL/GPL Python libraries the MIT app must not carry (2026-11-16: PyMuPDF had slipped in through the trial semester)
FORBIDDEN_IN_BUILD = ("pymupdf*", "fitz*", "PyMuPDF*")
NOT_FOR_RELEASE = "NOT_FOR_RELEASE.txt"


def private_term_sources() -> list[Path]:
    """Files whose terms the public scan must not find: the private list in ``LECTUREHUB_PRIVATE_TERMS`` (one term
    per line, kept outside the repository: names, course names, anything personal) and the configured semester's
    시간표.csv. Never a file inside the repository. Prints a warning when there is none."""
    found: list[Path] = []
    value = os.environ.get(ENV_TERMS)
    if value:
        path = Path(value).expanduser().resolve()
        if path == REPO or REPO in path.parents:
            raise SystemExit(f"{ENV_TERMS} must point outside the repository (private terms never go into it)")
        if path.is_file():
            found.append(path)
        else:
            print(f"WARNING: {ENV_TERMS} is set but the file does not exist: {path}", file=sys.stderr)
    try:
        sys.path.insert(0, str(REPO / "src"))
        from lecturehub.core import settings, timetable

        root = settings.data_root()
        table = (root / timetable.FILE) if root else None
        if table and table.is_file() and REPO not in table.resolve().parents:
            found.append(table)
        kept = (root / "_lecturehub" / "private_terms.txt") if root else None  # the owner's standing list
        if kept and kept.is_file() and REPO not in kept.resolve().parents and kept.resolve() not in found:
            found.append(kept)
    except Exception as exc:  # noqa: BLE001 - a missing timetable only weakens the scan; the warning says so
        print(f"WARNING: could not read the configured timetable ({exc.__class__.__name__})", file=sys.stderr)
    if not found:
        print("WARNING: no private terms for the public scan (no timetable of the configured semester and no "
              f"{ENV_TERMS} file). Course names and personal names will NOT be checked. Set {ENV_TERMS} to a "
              "local text file outside the repository, one term per line.", file=sys.stderr)
    return found


def app_version(source: Path) -> str:
    """The one place the version lives: ``__version__`` in src/lecturehub/__init__.py."""
    text = (source / "lecturehub" / "__init__.py").read_text(encoding="utf-8")
    found = re.search(r'^__version__\s*=\s*"(\d+\.\d+\.\d+)"\s*$', text, re.MULTILINE)
    if not found:
        raise SystemExit("__version__ = \"X.Y.Z\" not found in lecturehub/__init__.py")
    return found[1]


def copy_ffmpeg_from_path(target: Path) -> str:
    """Local try-out only: whatever ffmpeg is on this PC (often a GPL build). Returns its version line."""
    target.mkdir(parents=True, exist_ok=True)
    for name in ("ffmpeg", "ffprobe"):
        where = shutil.which(name)
        if not where:
            raise SystemExit(f"{name} not found on PATH")
        exe = Path(where).resolve()
        if exe.suffix.lower() != ".exe":  # WinGet links resolve to the real exe; anything else: keep the given one
            exe = Path(where)
        shutil.copy2(exe, target / f"{name}.exe")
    lines = subprocess.run([str(target / "ffmpeg.exe"), "-version"], capture_output=True, text=True,
                           check=False).stdout.splitlines()
    return lines[0] if lines else "unknown"


def copy_pinned_ffmpeg(target: Path, cache: Path | None, archive: Path | None) -> None:
    sys.path.insert(0, str(REPO / "tools"))
    import fetch_tools  # noqa: E402

    folder = fetch_tools.ffmpeg_folder(cache or fetch_tools.default_cache(), archive=archive)
    target.mkdir(parents=True, exist_ok=True)
    for item in folder.iterdir():
        if item.name == "ok":
            continue
        name = item.name if item.suffix.lower() in (".exe", ".dll") else f"FFMPEG_{item.name}"
        shutil.copy2(item, target / name)
    (target / "FFMPEG_NOTICE.txt").write_text(fetch_tools.notice(), encoding="utf-8")


def bundled_modules(work: Path) -> set[str]:
    """Top-level names of every Python module PyInstaller put into the build (from its own table of contents)."""
    names: set[str] = set()
    for toc in work.rglob("*.toc"):
        try:
            data = ast.literal_eval(toc.read_text(encoding="utf-8"))
        except (OSError, ValueError, SyntaxError):
            continue
        stack = [data]
        while stack:
            item = stack.pop()
            if isinstance(item, (list, tuple)):
                if len(item) == 3 and all(isinstance(part, str) for part in item) and item[2] in ("PYMODULE", "EXTENSION"):
                    names.add(item[0].split(".")[0])
                else:
                    stack.extend(item)
    return names


def write_licenses(app: Path, work: Path) -> int:
    """LectureHub/licenses: one line per third-party Python package in the build (name, version, licence, home page)
    and each package's own licence files. Returns the number of packages."""
    from importlib import metadata

    modules = bundled_modules(work)
    owners = metadata.packages_distributions()
    wanted = sorted({dist for module in modules for dist in owners.get(module, [])} - {"lecturehub"}, key=str.casefold)
    out = app / "licenses"
    out.mkdir(exist_ok=True)
    lines = ["Third-party Python packages inside this build of Lecture Hub", "",
             "name | version | licence | home page", "-----|---------|---------|----------"]
    for name in wanted:
        dist = metadata.distribution(name)
        meta = dist.metadata
        licence = meta.get("License-Expression") or (meta.get("License") or "").splitlines()[0:1] or ""
        if isinstance(licence, list):
            licence = licence[0] if licence else ""
        if not licence or len(licence) > 80:
            classifiers = [c.split("::")[-1].strip() for c in meta.get_all("Classifier") or [] if c.startswith("License ::")]
            licence = "; ".join(classifiers) or "see the licence file"
        home = meta.get("Home-page") or next((u.split(",", 1)[1].strip() for u in meta.get_all("Project-URL") or []
                                              if "," in u), "")
        lines.append(f"{name} | {dist.version} | {licence} | {home}")
        for file in dist.files or []:
            lower = file.name.lower()
            if lower.startswith(("license", "licence", "copying", "notice", "authors")) and "dist-info" in str(file):
                source = Path(dist.locate_file(file))
                if source.is_file():
                    (out / name).mkdir(exist_ok=True)
                    shutil.copy2(source, out / name / file.name)
    (out / "THIRD_PARTY_PACKAGES.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(wanted)


def find_iscc() -> Path | None:
    candidates = [os.environ.get(ENV_ISCC), shutil.which("ISCC")]
    local = os.environ.get("LOCALAPPDATA")
    for base in (Path(local) / "Programs" if local else None, Path(os.environ.get("ProgramFiles(x86)") or ""),
                 Path(os.environ.get("ProgramFiles") or "")):
        if base:
            candidates += [str(base / f"Inno Setup {n}" / "ISCC.exe") for n in (7, 6)]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    return None


def build_installer(app: Path, out: Path, version: str, public: Path) -> Path:
    iscc = find_iscc()
    if iscc is None:
        raise SystemExit(f"Inno Setup (ISCC.exe) not found. Install it (tools/fetch_tools.py innosetup) or set {ENV_ISCC}.")
    icon = public / "src" / "lecturehub" / "ui" / "assets" / "app.ico"
    command = [str(iscc), f"/DAppVersion={version}", f"/DSourceDir={app}", f"/DOutputDir={out}", "/Qp"]
    if icon.is_file():
        command.append(f"/DIconFile={icon}")
    subprocess.run([*command, str(public / "packaging" / "installer.iss")], check=True)
    setup = out / f"LectureHub-{version}-win64-setup.exe"
    if not setup.is_file():
        raise SystemExit("Inno Setup finished but the setup file is missing")
    return setup


def sha256sums(out: Path, files: list[Path]) -> Path:
    lines = []
    for file in files:
        digest = hashlib.sha256()
        with file.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        lines.append(f"{digest.hexdigest()}  {file.name}")
    target = out / "SHA256SUMS.txt"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--ffmpeg", choices=("pinned", "path", "none"), default="pinned")
    parser.add_argument("--ffmpeg-archive", type=Path, default=None, help="an already downloaded copy of the pinned ffmpeg zip")
    parser.add_argument("--cache", type=Path, default=None, help="where pinned downloads are kept")
    parser.add_argument("--installer", action="store_true", help="also build the Inno Setup installer")
    parser.add_argument("--no-ffmpeg", action="store_true", help="same as --ffmpeg none")
    args = parser.parse_args(argv)
    ffmpeg = "none" if args.no_ffmpeg else args.ffmpeg
    out = (args.out or Path(tempfile.gettempdir()) / f"lecturehub-build-{datetime.now():%Y%m%d_%H%M%S}").resolve()
    if REPO in out.parents or out == REPO:
        raise SystemExit("--out must be outside the repository")
    public = out / "public"
    if (REPO / "tools" / "export_public.py").is_file():  # the development repository: export the public edition
        sys.path.insert(0, str(REPO / "tools"))
        import export_public  # noqa: E402

        terms = []
        for source in private_term_sources():
            terms += ["--terms-from", str(source)]
        if export_public.main([str(public), *terms]) != 0:
            raise SystemExit("public export failed (see the scan above)")
    else:
        # The public repository IS the public edition (it was made by that export, which is not part of it): build
        # from its own files. The marker is checked here and again in the finished build below.
        if not (REPO / "src" / "lecturehub" / "core" / "edition.json").is_file():
            raise SystemExit("neither tools/export_public.py nor the public edition marker is here: nothing to build from")
        public = REPO
        print("building from this repository (it is the public edition)")
    version = app_version(public / "src")
    env = {**os.environ, "LECTUREHUB_BUILD_SRC": str(public / "src"), "LECTUREHUB_BUILD_VERSION": version}
    subprocess.run([sys.executable, "-m", "PyInstaller", str(public / "packaging" / "lecturehub.spec"), "--noconfirm",
                    "--distpath", str(out / "dist"), "--workpath", str(out / "work")], env=env, check=True)
    app = out / "dist" / "LectureHub"
    if not (app / "_internal" / "lecturehub" / "core" / "edition.json").is_file():
        raise SystemExit("the build does not carry the public edition marker; refusing to package it")
    forbidden = [name for name in FORBIDDEN_IN_BUILD if any((app / "_internal").glob(name))]
    if forbidden:
        raise SystemExit(f"the build contains libraries that must not ship (license): {forbidden}")
    releasable = True
    if ffmpeg == "pinned":
        copy_pinned_ffmpeg(app / "bin", args.cache, args.ffmpeg_archive)
    elif ffmpeg == "path":
        line = copy_ffmpeg_from_path(app / "bin")
        releasable = False
        (app / NOT_FOR_RELEASE).write_text(
            "This build carries the ffmpeg that was on the build PC, not the pinned LGPL build.\n"
            f"ffmpeg: {line}\nIt is for trying things on the developer's PC only. Do not give it to anyone.\n", encoding="utf-8")
    else:
        releasable = False
        (app / NOT_FOR_RELEASE).write_text("Built without ffmpeg: recording cannot be finished. Not for release.\n",
                                           encoding="utf-8")
    shutil.copy2(public / "LICENSE", app / "LICENSE.txt")
    if (public / "THIRD_PARTY_NOTICES.md").is_file():
        shutil.copy2(public / "THIRD_PARTY_NOTICES.md", app / "THIRD_PARTY_NOTICES.md")
    packages = write_licenses(app, out / "work")
    archive = Path(shutil.make_archive(str(out / f"LectureHub-{version}-win64"), "zip", out / "dist", "LectureHub"))
    made = [archive]
    if args.installer:
        if not releasable:
            raise SystemExit("--installer needs the pinned ffmpeg (a try-out build is never packaged as an installer)")
        made.append(build_installer(app, out, version, public))
    sums = sha256sums(out, made)
    size = sum(f.stat().st_size for f in app.rglob("*") if f.is_file()) / 1e6
    print(f"version: {version}{'' if releasable else '  (NOT FOR RELEASE)'}\napp folder: {app} ({size:.0f} MB, "
          f"{packages} third-party packages listed)")
    for file in made:
        print(f"{file.name}: {file.stat().st_size / 1e6:.0f} MB")
    print(f"hashes: {sums}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
