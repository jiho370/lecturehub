"""Read PDFs through pypdfium2 (BSD/Apache) — the only place the app touches a PDF library.

PyMuPDF (AGPL) used to do this; keeping the surface this small makes the library replaceable.
Page numbers here are 0-based like the library; callers that show 1-based pages convert themselves.
"""
from __future__ import annotations

import re
from pathlib import Path

_PDF_DATE = re.compile(r"D:\d{4}")
KOREAN_FONT = Path(r"C:\Windows\Fonts\malgun.ttf")  # on every Windows 10/11; used from the user's own PC, not shipped


def write_slides(path: Path, slides: list[tuple[str, list[str]]], *, font: Path | None = None,
                 width: float = 960, height: float = 540) -> None:
    """Write a simple slide PDF with real text (searchable): one ``(title, [lines])`` per page.

    The trial semester's sample slides are made with this. It replaced PyMuPDF there (2026-11-16: an AGPL library
    was being bundled into the MIT app for these three pages). Without the Korean font the built-in Helvetica is
    used and Korean letters do not show; the page count and layout stay the same.
    """
    import ctypes

    import pypdfium2 as pdfium
    import pypdfium2.raw as raw

    font = KOREAN_FONT if font is None else Path(font)
    doc = pdfium.PdfDocument.new()
    try:
        handle = None
        if font.is_file():
            data = font.read_bytes()
            buffer = (ctypes.c_uint8 * len(data)).from_buffer_copy(data)
            handle = raw.FPDFText_LoadFont(doc.raw, buffer, len(data), raw.FPDF_FONT_TRUETYPE, True)
        if not handle:
            handle = raw.FPDFText_LoadStandardFont(doc.raw, b"Helvetica")
        if not handle:
            raise OSError("PDF 글꼴을 불러오지 못했습니다.")

        def put(page, text: str, x: float, y: float, size: float) -> None:
            item = raw.FPDFPageObj_CreateTextObj(doc.raw, handle, size)
            encoded = (text + "\x00").encode("utf-16-le")
            wide = ctypes.cast(ctypes.create_string_buffer(encoded, len(encoded)), ctypes.POINTER(ctypes.c_ushort))
            if not raw.FPDFText_SetText(item, wide):
                raise OSError("PDF에 글을 넣지 못했습니다.")
            raw.FPDFPageObj_Transform(item, 1, 0, 0, 1, x, y)
            raw.FPDFPage_InsertObject(page.raw, item)

        for title, lines in slides:
            page = doc.new_page(width, height)
            try:
                put(page, title, 60, height - 90, 34)
                for n, line in enumerate(lines):
                    put(page, "• " + line, 80, height - (180 + n * 60), 24)
                if not raw.FPDFPage_GenerateContent(page.raw):
                    raise OSError("PDF 쪽을 만들지 못했습니다.")
            finally:
                page.close()
        raw.FPDFFont_Close(handle)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        doc.save(str(path))
    finally:
        doc.close()


class PdfDoc:
    """``with PdfDoc(path) as pdf``: ``page_count``, ``text(i)``, ``size(i)``, ``render_png(i, width, out)``, ``created``."""

    def __init__(self, path):
        import pypdfium2 as pdfium

        self._pdfium = pdfium
        self._doc = pdfium.PdfDocument(str(path))

    def __enter__(self) -> PdfDoc:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._doc.close()

    @property
    def page_count(self) -> int:
        return len(self._doc)

    def text(self, index: int) -> str:
        page = self._doc[index]
        try:
            textpage = page.get_textpage()
            try:
                return textpage.get_text_range().replace("\r\n", "\n").replace("\r", "\n")
            finally:
                textpage.close()
        finally:
            page.close()

    def texts(self) -> list[str]:
        return [self.text(i) for i in range(self.page_count)]

    def size(self, index: int) -> tuple[float, float]:
        page = self._doc[index]
        try:
            return page.get_size()
        finally:
            page.close()

    def render_png(self, index: int, width: int, out: Path) -> None:
        """Draw one page ``width`` pixels wide (aspect kept) into ``out`` as PNG."""
        page = self._doc[index]
        try:
            page_width, _ = page.get_size()
            image = page.render(scale=width / page_width).to_pil().convert("RGB")
        finally:
            page.close()
        image.save(out, format="PNG")

    @property
    def created(self) -> str:
        """The document's CreationDate string ('D:YYYYMMDDHHmmSS…') or ''."""
        try:
            value = self._doc.get_metadata_dict().get("CreationDate") or ""
        except Exception:  # noqa: BLE001 - unreadable metadata means unknown
            return ""
        return value if _PDF_DATE.match(value) else ""
