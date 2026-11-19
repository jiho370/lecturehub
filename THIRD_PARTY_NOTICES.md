# Third-party notices

Lecture Hub is MIT-licensed. It uses these components at run time (all permissive unless noted):

| Component | License | Use |
|---|---|---|
| typer, PyYAML, python-ulid, keyring | MIT / BSD | command line, settings, ids, credential store |
| numpy, PyAudioWPatch | BSD / MIT | recording |
| httpx, websockets, google-cloud-speech | BSD / Apache-2.0 | speech services |
| pypdfium2 (PDFium) | Apache-2.0 / BSD-3-Clause | reading PDF text and page images |
| Pillow | HPND | page images |
| python-pptx, olefile | MIT / BSD | slides, HWP text |
| FastAPI, uvicorn, Jinja2, pywebview | MIT / BSD | window and local pages |
| Pretendard font (src/lecturehub/ui/web/fonts) | SIL OFL 1.1 | UI font; license text included in that folder |

Development only (never shipped): PyMuPDF (AGPL-3.0) builds synthetic PDFs for tests.

| pythonnet, clr-loader, bottle, proxy-tools (with pywebview) | MIT | the window's bridge to WebView2 |
| Microsoft WebView2 loader (with pywebview) | BSD-3-Clause | starts the WebView2 Runtime already on the PC |
| grpcio, protobuf, google-auth and friends (with google-cloud-speech) | Apache-2.0 / BSD-3-Clause | speech services |
| certifi | MPL-2.0 | the list of trusted web certificates, unchanged |

The installed app (the setup file / zip) also carries:

- **FFmpeg** (`bin\ffmpeg.exe`, `bin\ffprobe.exe` and their DLLs): an unchanged **LGPL-2.1-or-later shared build**
  (no GPL parts). Lecture Hub runs them as separate programs. The exact build, its SHA-256, the licence text and
  where its source code is are in `bin\FFMPEG_NOTICE.txt` and the files next to it. You may replace these files
  with another FFmpeg build.
- **The exact list of every Python package inside that build**, with version and licence, and each package's own
  licence files: the `licenses` folder (`licenses\THIRD_PARTY_PACKAGES.txt`), written by the build itself from what
  really went in.
- The Python runtime (PSF licence) and the PyInstaller bootloader (GPL-2.0 with an exception that allows
  distributing applications built with it under any licence).

Outside programs the app can use when they are on your PC but never bundles: Codex CLI, Claude Code, Aside,
Google Cloud CLI. Running from source also uses the ffmpeg / ffprobe you installed yourself. Each has its own terms.
