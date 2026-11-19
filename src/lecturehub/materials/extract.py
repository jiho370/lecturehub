"""S4-0: page text of indexed materials into MaterialPage (search, note evidence, number checks).

- PDF: text per page with pypdfium2.
- PPTX/PPSX/PPT: the PowerPoint export already cached by S3 when there is one, so page N is
  the same page Chrome opens at ``#page=N``; otherwise the slide XML with python-pptx,
  skipping hidden slides the way PowerPoint's PDF export does. PowerPoint is never started here.
- HWP and other formats: no text (reported, nothing stored).
Originals are only read. A material whose file changed since indexing (sha256) is skipped;
re-indexing makes it a new Material. Pages with no text layer (scans) are stored as empty
text so page numbers stay complete; OCR is a later step.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from lecturehub.core.events import append

from .opener import SLIDES, cached_pdf

EXTRACTOR_VERSION = 1


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
        return [clean(text) for text in document.texts()]


def slide_pages(path: Path) -> list[str]:
    """Visible slides in order (PowerPoint's PDF export leaves hidden slides out)."""
    from pptx import Presentation

    pages = []
    for slide in Presentation(str(path)).slides:
        if slide._element.get("show") == "0":
            continue
        parts = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                parts.append(shape.text_frame.text)
            elif getattr(shape, "has_table", False) and shape.has_table:
                parts.extend(" | ".join(cell.text for cell in row.cells) for row in shape.table.rows)
        pages.append(clean("\n".join(parts)))
    return pages


def page_texts(path: Path, root: Path) -> tuple[list[str], str] | None:
    """(texts, source) or None when the format has no extractable text."""
    suffix = path.suffix.lower()
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
            only_missing: bool = True) -> ExtractReport:
    """Fill MaterialPage for materials that have none yet. Commits per material.

    ``busy()`` is asked before each file; a recording in progress stops the run
    (AGENTS rule 7) and what was done so far stays committed.
    """
    report = ExtractReport()
    rows = connection.execute(
        "SELECT m.id, m.path, m.sha256, (SELECT count(*) FROM MaterialPage p WHERE p.material_id = m.id) "
        "FROM Material m ORDER BY m.rowid").fetchall()
    for material_id, raw_path, sha256, existing in rows:
        path = Path(raw_path)
        if only_missing and existing:
            continue
        session = busy()
        if session:
            report.skipped.append({"material_id": material_id, "path": raw_path,
                                   "reason": f"녹음 중({session}) — 녹음이 끝난 뒤 다시 실행"})
            break
        if not path.is_file():
            report.skipped.append({"material_id": material_id, "path": raw_path, "reason": "파일 없음"})
            continue
        if _sha256(path) != sha256:
            report.skipped.append({"material_id": material_id, "path": raw_path,
                                   "reason": "색인 뒤 파일이 바뀜(다시 색인하면 새 자료로 등록)"})
            continue
        try:
            result = page_texts(path, root)
        except Exception as exc:  # noqa: BLE001 - one broken file must not stop the rest
            report.skipped.append({"material_id": material_id, "path": raw_path, "reason": f"읽기 실패: {exc}"})
            continue
        if result is None:
            report.skipped.append({"material_id": material_id, "path": raw_path,
                                   "reason": "이 형식은 본문을 읽지 않음(한글·ppt는 기본 프로그램으로 보기)"})
            continue
        texts, source = result
        with connection:
            if existing:
                connection.execute("DELETE FROM MaterialPage WHERE material_id = ?", (material_id,))
            connection.executemany(
                "INSERT INTO MaterialPage (material_id, page_no, text, image_path, terms) VALUES (?, ?, ?, NULL, '[]')",
                [(material_id, number, text) for number, text in enumerate(texts, 1)])
            connection.execute("UPDATE Material SET page_count = ? WHERE id = ?", (len(texts), material_id))
            empty = sum(1 for text in texts if not text)
            append(connection, "MATERIAL_TEXT_EXTRACTED", "Material", material_id,
                   {"pages": len(texts), "empty_pages": empty, "source": source,
                    "extractor_version": EXTRACTOR_VERSION})
        report.extracted.append({"material_id": material_id, "path": raw_path, "pages": len(texts),
                                 "empty_pages": empty, "source": source})
    return report
