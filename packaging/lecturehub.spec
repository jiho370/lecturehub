# PyInstaller spec: one folder with LectureHub.exe (window) and LectureHubWorker.exe (background jobs).
# Built by tools/build_app.py from the PUBLIC export (its src comes first on the path: SRC env var).
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

SRC = os.environ["LECTUREHUB_BUILD_SRC"]
HERE = Path(SPECPATH)
ICON = str(Path(SRC) / "lecturehub" / "ui" / "assets" / "app.ico")

# Walk the PUBLIC export itself: collect_submodules/collect_data_files("lecturehub") would ask the package installed
# in the building venv (the full, private tree in a dev checkout) instead.
PKG = Path(SRC) / "lecturehub"
SKIP = {"__pycache__"}


def _public_modules():
    for path in PKG.rglob("*.py"):
        if SKIP & set(path.parts):
            continue
        parts = path.relative_to(SRC).with_suffix("").parts
        yield ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _public_data():
    for path in PKG.rglob("*"):
        if path.is_file() and path.suffix not in {".py", ".pyc"} and not SKIP & set(path.parts):
            yield str(path), str(path.parent.relative_to(SRC))


hidden = sorted(_public_modules()) + collect_submodules("webview") + ["clr"]
datas = list(_public_data()) + collect_data_files("webview")

# pymupdf/fitz: AGPL, dev-only (test PDFs). Never in the MIT app, even when the build venv has it.
EXCLUDES = ["tkinter", "matplotlib", "IPython", "pytest", "pymupdf", "fitz",
            # Pillow's AVIF codec (7.9 MB): the app only draws PDF pages to PNG (materials/pdfdoc.py). Pillow
            # treats a missing _avif as "no AVIF support" (AvifImagePlugin catches the ImportError).
            "PIL._avif",
            # cryptography (9.9 MB): pywebview imports it only for webview.start(ssl=True) and requests only when
            # Python's ssl lacks SNI, both inside try/except ImportError. The app starts the window without ssl.
            "cryptography"]
# Live captions are off in the public edition (core/edition.py PUBLIC_OFF). Their optional Chirp line is the only
# user of Google Cloud Speech; captions.py imports it inside chirp_recognizer(), whose caller (transcription/cli.py
# captions start --confirmed) reports a missing library and goes on without it. That stack is about 12 MB in the
# download: grpcio, google-api-core, google-auth, protobuf, proto-plus,
# googleapis-common-protos, grpcio-status, opentelemetry-api, pyasn1. Kept when building a tree without the marker.
if (PKG / "core" / "edition.json").is_file():
    EXCLUDES += ["google", "grpc", "grpc_status", "proto", "opentelemetry", "pyasn1", "pyasn1_modules"]


def analysis(script):
    # noarchive stays off. Loose .pyc files shared by both exes would cut ~16 MB from the download (each exe carries
    # its own copy of the module archive), but measured 2026-11-21 on a PC with Defender: the first start after
    # unpacking took 9-14 s instead of ~4 s (3240 files to scan instead of 983) and unpacking took twice as long.
    # Later starts were the same. A slow first launch costs more than 16 MB of download (branch slim, commit 4007cc7).
    return Analysis([str(HERE / script)], pathex=[SRC], datas=datas, hiddenimports=hidden, excludes=EXCLUDES,
                    noarchive=False)


VERSION = os.environ.get("LECTUREHUB_BUILD_VERSION", "0.0.0")


def version_file(name, description):
    """The version shown in the exe's Properties > Details (and read by Windows and installers)."""
    from PyInstaller.utils.win32.versioninfo import (FixedFileInfo, StringFileInfo, StringStruct, StringTable,
                                                     VarFileInfo, VarStruct, VSVersionInfo)

    numbers = tuple(int(part) for part in VERSION.split(".")[:3]) + (0,)
    info = VSVersionInfo(
        ffi=FixedFileInfo(filevers=numbers, prodvers=numbers),
        kids=[StringFileInfo([StringTable("040904B0", [
            StringStruct("CompanyName", "Lecture Hub contributors"), StringStruct("FileDescription", description),
            StringStruct("FileVersion", VERSION), StringStruct("InternalName", name),
            StringStruct("OriginalFilename", name + ".exe"), StringStruct("ProductName", "Lecture Hub"),
            StringStruct("ProductVersion", VERSION), StringStruct("LegalCopyright", "MIT License")])]),
            VarFileInfo([VarStruct("Translation", [1033, 1200])])])
    path = Path(workpath) / f"version_{name}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(info), encoding="utf-8")
    return str(path)


app = analysis("launcher_app.py")
worker = analysis("launcher_worker.py")

app_exe = EXE(PYZ(app.pure), app.scripts, [], exclude_binaries=True, name="LectureHub", console=False, icon=ICON,
              version=version_file("LectureHub", "Lecture Hub"))
worker_exe = EXE(PYZ(worker.pure), worker.scripts, [], exclude_binaries=True, name="LectureHubWorker",
                 console=True, icon=ICON, version=version_file("LectureHubWorker", "Lecture Hub background helper"))

COLLECT(app_exe, app.binaries, app.datas, worker_exe, worker.binaries, worker.datas, name="LectureHub")
