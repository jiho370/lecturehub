"""S4-0: page text of indexed materials into MaterialPage (search, note evidence, number checks).

- PDF: text per page with pypdfium2.
- PPTX/PPSX/PPT: the PowerPoint export already cached by S3 when there is one, so page N is
  the same page Chrome opens at ``#page=N``; otherwise the slide XML with python-pptx,
  skipping hidden slides the way PowerPoint's PDF export does. PowerPoint is never started here.
- HWP (5.x) / HWPX / DOCX (D-078): the document's own text read in-process (doctext.py, a bounded OLE reader in
  cfb.py for .hwp). These have no fixed pages, so the text is cut into pieces of about units.CHUNK_CHARS
  characters (a new piece at each section) and the piece number is stored as page_no; it is shown as 구간, not 쪽
  (units.page_unit). Material.page_count is left as it was for them (a piece count is not a page count).
  Password-encrypted, distribution-protected or DRM-protected HWP files are not read (a clear reason).
- Binary .ppt without an export, .doc and other formats: no text (reported, nothing stored).
Originals are only read. A material whose file changed since indexing (sha256) is skipped;
re-indexing makes it a new Material. Pages with no text layer (scans) are stored as empty
text so page numbers stay complete; OCR is a later step.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from pathlib import Path

from lecturehub.core.events import append

from .opener import SLIDES, cached_pdf
from .units import CHUNK_CHARS, is_chunked

# 2 (D-078): .hwp/.hwpx/.docx are read now; a newer version makes the background helper try every file again
# that an older one wrote down as giving no text.
EXTRACTOR_VERSION = 2
WORD_FILES = {".hwp", ".hwpx", ".docx"}
# Bounds for lecture material (review 2026-11-24, H2): a bigger file, or a slide file that would unpack into more
# than this, is not read and is written down as skipped; a longer document keeps its first MAX_PAGES pages.
MAX_FILE_BYTES = 200 * 1024 * 1024
MAX_UNZIPPED_BYTES = 1024 * 1024 * 1024  # .pptx/.ppsx/.hwpx/.docx: total size of the zip's entries once unpacked
MAX_ZIP_ENTRIES = 20_000
MAX_PAGES = 2_000
# ``busy()`` (a look at the recordings folder, a few ms) is asked before each file and every few pages, so a
# recording the app did not stop the helper for (started from the CLI, after a device drop) stops it within
# a few pages; a single step that cannot be interrupted (hashing a file, opening a PDF, python-pptx loading
# a deck) still runs to its end.
PAGES_BETWEEN_CHECKS = 5


class TooLarge(ValueError):
    """The file is past one of the bounds above; ``str()`` says which."""


class NotReadable(ValueError):
    """The file is given up with a reason of its own (an encrypted or protected document, not the format it
    claims, damaged, no text at all); ``str()`` is the reason as shown."""


class Interrupted(Exception):
    """A recording started while a long file was being read (``busy()`` is asked between pages, too)."""


# What the run in progress asks between pages: set by ``extract`` for the length of one run. A module value and
# not a parameter so that ``page_texts(path, root)`` keeps its signature.
_run: dict = {"busy": None, "capped": False}


def _between_pages() -> None:
    busy = _run["busy"]
    session = busy() if busy else None
    if session:
        raise Interrupted(session)


@dataclass
class ExtractReport:
    extracted: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)

    @property
    def pages(self) -> int:
        return sum(item["pages"] for item in self.extracted)

    @property
    def empty_pages(self) -> int:
        return sum(item["empty_pages"] for item in self.extracted)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def clean(text: str) -> str:
    """Trim each line, drop blank runs; keep line breaks (slides are line-oriented)."""
    lines = [re.sub(r"[ \t ]+", " ", line).strip() for line in text.replace("\r", "\n").split("\n")]
    return "\n".join(line for line in lines if line)


def pdf_pages(path: Path) -> list[str]:
    from .pdfdoc import PdfDoc

    with PdfDoc(path) as document:
        count = document.page_count
        _run["capped"] = count > MAX_PAGES
        texts = []
        for index in range(min(count, MAX_PAGES)):
            if index and index % PAGES_BETWEEN_CHECKS == 0:  # the file itself was asked about just before
                _between_pages()
            texts.append(clean(document.text(index)))
        return texts


def zip_guard(path: Path, what: str = "슬라이드 파일") -> None:
    """A .pptx/.hwpx/.docx is a zip: check what it would unpack into before anything loads it (zip-bomb guard)."""
    import zipfile

    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
    except zipfile.BadZipFile as exc:
        raise NotReadable(f"{what} — 열 수 없음: {exc}") from None
    if len(entries) > MAX_ZIP_ENTRIES or sum(entry.file_size for entry in entries) > MAX_UNZIPPED_BYTES:
        raise TooLarge(f"{what} — 풀면 너무 큼(읽지 않음)")


def slide_pages(path: Path) -> list[str]:
    """Visible slides in order (PowerPoint's PDF export leaves hidden slides out)."""
    zip_guard(path)
    from pptx import Presentation

    pages = []
    for slide in Presentation(str(path)).slides:
        if slide._element.get("show") == "0":
            continue
        if len(pages) >= MAX_PAGES:
            _run["capped"] = True
            break
        if pages and len(pages) % PAGES_BETWEEN_CHECKS == 0:
            _between_pages()
        parts = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                parts.append(shape.text_frame.text)
            elif getattr(shape, "has_table", False) and shape.has_table:
                parts.extend(" | ".join(cell.text for cell in row.cells) for row in shape.table.rows)
        pages.append(clean("\n".join(parts)))
    return pages


def chunks(sections: list[list[str]], size: int = CHUNK_CHARS) -> list[str]:
    """Lines of a document without pages into pieces of at most ``size`` characters, cut between lines (a longer
    line is cut inside), each section starting a new piece; blank lines dropped as in ``clean``."""
    pieces: list[str] = []
    for lines in sections:
        current: list[str] = []
        length = 0
        for raw in lines:
            for line in clean(raw).split("\n"):
                if not line:
                    continue
                while len(line) > size:
                    if current:
                        pieces.append("\n".join(current))
                        current, length = [], 0
                    pieces.append(line[:size])
                    line = line[size:]
                if current and length + 1 + len(line) > size:
                    pieces.append("\n".join(current))
                    current, length = [], 0
                length += len(line) + (1 if current else 0)
                current.append(line)
        if current:
            pieces.append("\n".join(current))
    return pieces


def word_format(path: Path) -> str:
    """"hwp", "hwpx" or "docx" by the file's suffix, except that a Hancom file is read by what it is: an HWP 5
    file saved under a .hwpx name (an OLE file; seen among real course files) is read as .hwp and the other way
    round. An OLE .docx stays docx (that is an encrypted Word file and is given up as such)."""
    kind = path.suffix.lower().lstrip(".")
    if kind in ("hwp", "hwpx"):
        with path.open("rb") as stream:
            head = stream.read(8)
        if head == bytes.fromhex("D0CF11E0A1B11AE1"):
            return "hwp"
        if head[:4] == b"PK\x03\x04":
            return "hwpx"
    return kind


def word_pages(path: Path, kind: str | None = None) -> list[str]:
    """Pieces of a .hwp/.hwpx/.docx (see the module text); the busy check runs between sections."""
    from . import doctext

    reader = {"hwp": doctext.hwp_sections, "hwpx": doctext.hwpx_sections,
              "docx": doctext.docx_sections}[kind or word_format(path)]
    pieces = chunks(reader(path, _between_pages))
    if not pieces:
        raise NotReadable("글이 없는 문서(그림만 있거나 빈 문서)")
    if len(pieces) > MAX_PAGES:
        _run["capped"] = True
        pieces = pieces[:MAX_PAGES]
    return pieces


def page_texts(path: Path, root: Path) -> tuple[list[str], str] | None:
    """(texts, source) or None when the format has no extractable text."""
    suffix = path.suffix.lower()
    if suffix in WORD_FILES:
        kind = word_format(path)
        return word_pages(path, kind), kind
    if suffix == ".pdf":
        return pdf_pages(path), "pdf"
    if suffix in SLIDES:
        exported = cached_pdf(path, root)
        if exported is not None:
            return pdf_pages(exported), "powerpoint_pdf"
        if suffix == ".ppt":
            return None  # binary .ppt needs PowerPoint; the S3 prewarm makes the export
        return slide_pages(path), "pptx_xml"
    return None


def extract(connection: sqlite3.Connection, root: Path, *, busy: Callable[[], str | None] = lambda: None,
            only_missing: bool = True, skip: Collection[str] = (),
            before: Callable[[str], None] | None = None, after: Callable[[dict], None] | None = None) -> ExtractReport:
    """Fill MaterialPage for materials that have none yet. Commits per material.

    ``busy()`` is asked before each file and between pages of a long one; a recording in progress stops the run
    (AGENTS rule 7) and what was done so far stays committed: each material's pages are written in one
    transaction (``with connection`` below), so a run stopped or killed in the middle of a file loses that file
    only, never a half-written one. ``skip``: material ids not to look at (the background run's record of files
    already tried that gave no text, materials/background.py). ``before(material_id)`` is called before a file
    is opened and ``after(item)`` with its report item as soon as it is done (the background run writes both
    down at once, review H1).
    """
    report = ExtractReport()
    rows = connection.execute(
        "SELECT m.id, m.path, m.sha256, (SELECT count(*) FROM MaterialPage p WHERE p.material_id = m.id) "
        "FROM Material m ORDER BY m.rowid").fetchall()
    _run["busy"] = busy
    try:
        for material_id, raw_path, sha256, existing in rows:
            if (only_missing and existing) or material_id in skip:
                continue
            session = busy()
            if session:
                report.skipped.append({"material_id": material_id, "path": raw_path,
                                       "reason": f"녹음 중({session}) — 녹음이 끝난 뒤 다시 실행"})
                break
            if before is not None:
                before(material_id)
            item = _one(connection, root, material_id, raw_path, sha256, existing, report)
            if after is not None:
                after(item)
            if item.get("interrupted"):
                break
    finally:
        _run["busy"] = None
    return report


def _one(connection: sqlite3.Connection, root: Path, material_id: str, raw_path: str, sha256: str, existing: int,
         report: ExtractReport) -> dict:
    def skipped(reason: str, **extra) -> dict:
        item = {"material_id": material_id, "path": raw_path, "reason": reason, **extra}
        report.skipped.append(item)
        return item

    path = Path(raw_path)
    if not path.is_file():
        return skipped("파일 없음")
    try:
        size = path.stat().st_size
    except OSError as exc:
        return skipped(f"읽기 실패: {exc}")
    if size > MAX_FILE_BYTES:
        return skipped(f"파일이 너무 큼({size // (1024 * 1024)} MB, {MAX_FILE_BYTES // (1024 * 1024)} MB까지 읽음)")
    if _sha256(path) != sha256:
        return skipped("색인 뒤 파일이 바뀜(다시 색인하면 새 자료로 등록)")
    _run["capped"] = False
    try:
        result = page_texts(path, root)
    except Interrupted as stop:
        return skipped(f"녹음 중({stop}) — 녹음이 끝난 뒤 다시 실행", interrupted=True)
    except (TooLarge, NotReadable) as exc:
        return skipped(str(exc))
    except Exception as exc:  # noqa: BLE001 - one broken file must not stop the rest
        return skipped(f"읽기 실패: {exc}")
    if result is None:
        return skipped("이 형식은 본문을 읽지 않음(ppt·doc 등은 기본 프로그램으로 보기)")
    texts, source = result
    capped = bool(_run["capped"])  # only the first MAX_PAGES pages were read: said in the event and the report
    empty = sum(1 for text in texts if not text)
    with connection:  # one transaction per material (see extract)
        if existing:
            connection.execute("DELETE FROM MaterialPage WHERE material_id = ?", (material_id,))
        connection.executemany(
            "INSERT INTO MaterialPage (material_id, page_no, text, image_path, terms) VALUES (?, ?, ?, NULL, '[]')",
            [(material_id, number, text) for number, text in enumerate(texts, 1)])
        # after a capped read the real count is larger, and a piece count of a .hwp/.docx is not a page count:
        # the indexed one stays
        if not capped and not is_chunked(source):
            connection.execute("UPDATE Material SET page_count = ? WHERE id = ?", (len(texts), material_id))
        append(connection, "MATERIAL_TEXT_EXTRACTED", "Material", material_id,
               {"pages": len(texts), "empty_pages": empty, "source": source,
                "extractor_version": EXTRACTOR_VERSION} | ({"capped_at": MAX_PAGES} if capped else {})
               | ({"page_unit": "구간", "chunk_chars": CHUNK_CHARS} if is_chunked(source) else {}))
    item = {"material_id": material_id, "path": raw_path, "pages": len(texts), "empty_pages": empty, "source": source}
    if capped:
        item["capped_at"] = MAX_PAGES
    report.extracted.append(item)
    return item
