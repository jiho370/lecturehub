"""Open lecture materials in the user's own programs (D-056) and render page previews.

- PDF: the default PDF program at a page. Chrome/Edge take ``file:///…pdf#page=N``.
  Other programs open the file; the page may not be applied (reported back).
- PPTX/PPSX/PPT: converted to PDF with PowerPoint when installed, cached under
  _lecturehub\\cache\\pdf, then opened like a PDF. Without PowerPoint: default program.
- HWP and anything else: the default program.
Originals in the course folders are only read.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

from .units import CHUNK_CHARS, is_chunked

SLIDES = {".pptx", ".ppsx", ".ppt"}
CREATE_NO_WINDOW = 0x08000000


def _cache(root: Path, kind: str) -> Path:
    folder = Path(root) / "_lecturehub" / "cache" / kind
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _key(path: Path) -> str:
    stat = path.stat()
    return hashlib.sha256(f"{path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}".encode()).hexdigest()[:20]


def default_pdf_program() -> tuple[str | None, str | None]:
    """(ProgId, executable) of the user's default .pdf handler."""
    if os.name != "nt":
        return None, None
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\.pdf\UserChoice") as key:
            prog_id = winreg.QueryValueEx(key, "ProgId")[0]
    except OSError:
        return None, None
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf"{prog_id}\shell\open\command") as key:
            command = winreg.QueryValueEx(key, "")[0]
    except OSError:
        return prog_id, None
    exe = command.split('"')[1] if command.startswith('"') else command.split()[0]
    return prog_id, exe


def program_label() -> str:
    """What the open button names: the user's default PDF program."""
    prog_id, _ = default_pdf_program()
    for key, label in (("Chrome", "Chrome"), ("MSEdge", "Edge"), ("Acro", "Acrobat"), ("Hwp", "한글")):
        if prog_id and key.lower() in prog_id.lower():
            return label
    return "PDF 프로그램"


def powerpoint_available() -> bool:
    if os.name != "nt":
        return False
    import winreg

    try:
        winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"PowerPoint.Application").Close()
        return True
    except OSError:
        return False


def cached_pdf(path: Path, root: Path) -> Path | None:
    """The PowerPoint export already made for these slides, if any (no conversion)."""
    out = Path(root) / "_lecturehub" / "cache" / "pdf" / f"{_key(Path(path))}.pdf"
    return out if out.is_file() else None


CONVERT_COMMAND = (
    "$running = [bool](Get-Process POWERPNT -ErrorAction SilentlyContinue); "
    "$app = New-Object -ComObject PowerPoint.Application; $doc = $null; "
    "try { $doc = $app.Presentations.Open($env:LH_SRC, $true, $false, $false); "
    "$doc.SaveAs($env:LH_OUT, 32) } "
    "finally { if ($doc) { $doc.Close() }; if (-not $running) { $app.Quit() } }")


def convert_command(source: Path, out: Path) -> tuple[str, dict]:
    """PowerShell for one export, and the environment that carries the paths.

    New-Object attaches to a PowerPoint the user already has open, so only the presentation we opened
    is closed, and PowerPoint quits only if we started it. The paths never enter the command text
    (security review 2026-11-05): PowerShell also ends a '...' literal at the curly quotes
    U+2018–U+201B, so a file named ``a’); calc; (’.pptx`` would otherwise run code. They are read
    from ``$env:LH_SRC`` / ``$env:LH_OUT`` instead."""
    return CONVERT_COMMAND, {**os.environ, "LH_SRC": str(source), "LH_OUT": str(out)}


def slides_to_pdf(path: Path, root: Path, timeout: float = 180) -> Path:
    """PowerPoint export (ppSaveAsPDF = 32), cached by path/size/mtime; the original is only read."""
    out = _cache(root, "pdf") / f"{_key(path)}.pdf"
    if out.is_file():
        return out
    command, env = convert_command(path, out)
    result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
                            capture_output=True, text=True, timeout=timeout, check=False, env=env,
                            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0)
    if result.returncode or not out.is_file():
        raise RuntimeError("PowerPoint로 PDF 변환에 실패했습니다: " + (result.stderr or result.stdout).strip()[-200:])
    return out


def open_material(path: str | Path, page: int, root: Path, allow_convert: bool = True) -> dict:
    """Open with the user's program; say what actually happened (page applied or not).

    ``allow_convert=False`` (while recording): slides use an existing export or open as they are.
    """
    path = Path(path)
    if not path.is_file():
        return {"ok": False, "error": f"파일이 없습니다: {path.name}"}
    from lecturehub.core import safe_open

    reason = safe_open.refusal(path)  # every os.startfile below opens this path or its PDF export
    if reason:
        return {"ok": False, "error": reason}
    suffix = path.suffix.lower()
    if suffix in SLIDES and not allow_convert:
        cached = cached_pdf(path, root)
        if cached is None:
            os.startfile(str(path))
            return {"ok": True, "program": "default", "page_applied": False,
                    "note": "녹음 중이라 PDF로 바꾸지 않고 원본을 열었습니다"}
        path, suffix = cached, ".pdf"
    elif suffix in SLIDES and powerpoint_available():
        try:
            path, suffix = slides_to_pdf(path, root), ".pdf"
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            os.startfile(str(path))
            return {"ok": True, "program": "default", "page_applied": False, "note": str(exc)}
    if suffix == ".pdf":
        prog_id, exe = default_pdf_program()
        if exe and prog_id and ("Chrome" in prog_id or "MSEdge" in prog_id) and Path(exe).is_file():
            url = Path(path).resolve().as_uri() + f"#page={max(1, int(page))}"
            subprocess.Popen([exe, url], creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0)
            return {"ok": True, "program": prog_id, "page_applied": True, "opened": str(path)}
        os.startfile(str(path))
        return {"ok": True, "program": prog_id or "default", "page_applied": False, "opened": str(path)}
    os.startfile(str(path))
    result = {"ok": True, "program": "default", "page_applied": False, "opened": str(path)}
    if is_chunked(path.name):  # D-078: a 구간 is a piece of the text, not a page the program could go to
        result["note"] = ("한글·워드 문서는 그 자리로 바로 가지 못해 처음부터 열었습니다"
                          f"(구간 {max(1, int(page))}, 구간은 약 {CHUNK_CHARS:,}자 단위)")
    return result


def render_page(path: str | Path, page: int, root: Path, width: int = 1280, allow_convert: bool = True) -> Path | None:
    """PNG preview of one page (PDF, or slides converted with PowerPoint). None when not possible.

    ``allow_convert=False`` (while recording) never starts PowerPoint; only an existing export is used.
    """
    path = Path(path)
    if path.suffix.lower() in SLIDES:
        cached = cached_pdf(path, root)
        if cached is None and (not allow_convert or not powerpoint_available()):
            return None
        path = cached or slides_to_pdf(path, root)
    if path.suffix.lower() != ".pdf" or not path.is_file():
        return None
    out = _cache(root, "pages") / f"{_key(path)}_{int(page)}_{width}.png"
    if out.is_file():
        return out
    from .pdfdoc import PdfDoc

    with PdfDoc(path) as document:
        index = max(0, min(int(page), document.page_count) - 1)
        document.render_png(index, width, out)
    return out
