"""Plain text out of course documents, for the assignment helper (user request 2026-09-29).

Reads PDF (pypdfium2), HTML notes, HWP 5 (OLE, needs ``olefile``), HWPX / DOCX / XLSX / PPTX (zip + XML, stdlib)
and plain text. Nothing is written next to the source: callers save the text inside ``_lecturehub``.
``extract`` never raises for a readable file; an unsupported or broken one comes back with ``text=""``
and a Korean ``note`` saying why, so a pack can show what could not be read.
"""
from __future__ import annotations

import html
import re
import struct
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

from lecturehub.core.textio import read_text_any

HWPTAG_PARA_TEXT = 67  # HWPTAG_BEGIN (0x10) + 51
# HWP 5 control characters that take 8 WCHARs (the code, 6 WCHARs of data, the code again)
_HWP_WIDE = {1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23}


@dataclass
class Extracted:
    text: str
    note: str = ""  # why the text is empty or partial ("" when fine)
    pages: int | None = None


def extract(path: Path) -> Extracted:
    path = Path(path)
    suffix = path.suffix.lower()
    try:
        if suffix == ".pdf":
            return _pdf(path)
        if suffix in (".html", ".htm"):
            return Extracted(html_text(read_text_any(path)))
        if suffix in (".txt", ".md", ".csv"):
            return Extracted(read_text_any(path))
        if suffix == ".hwp":
            return _hwp(path)
        if suffix == ".hwpx":
            return Extracted(_zip_xml(path, lambda n: n.startswith("Contents/section") and n.endswith(".xml"),
                                      text_tag="t", para_tag="p"))
        if suffix == ".docx":
            return Extracted(_zip_xml(path, lambda n: n == "word/document.xml", text_tag="t", para_tag="p"))
        if suffix == ".pptx":
            return Extracted(_zip_xml(path, lambda n: re.fullmatch(r"ppt/slides/slide\d+\.xml", n) is not None,
                                      text_tag="t", para_tag="p", numbered="슬라이드"))
        if suffix == ".xlsx":
            return Extracted(_xlsx(path))
    except (OSError, ValueError, zipfile.BadZipFile, ElementTree.ParseError, struct.error, zlib.error) as exc:
        return Extracted("", f"읽지 못함({type(exc).__name__}: {exc})")
    return Extracted("", f"글자를 뽑지 않는 형식({suffix or '확장자 없음'})")


def html_text(source: str) -> str:
    source = re.sub(r"(?is)<(script|style|svg|head)[^>]*>.*?</\1>", " ", source)
    source = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h\d|tr|section|table|blockquote)>", "\n", source)
    text = html.unescape(re.sub(r"<[^>]+>", " ", source))
    text = re.sub(r"[ \t ]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def _pdf(path: Path, pages: list[int] | None = None) -> Extracted:
    from .pdfdoc import PdfDoc

    with PdfDoc(path) as document:
        wanted = pages or list(range(1, document.page_count + 1))
        parts = [f"[p.{n}]\n" + document.text(n - 1).strip()
                 for n in wanted if 1 <= n <= document.page_count]
        text = "\n\n".join(parts)
        note = "" if text.replace("[p.", "").strip(" \n0123456789]") else "글자 층이 없는 PDF(스캔본일 수 있음)"
        return Extracted(text, note, document.page_count)


def pdf_pages(path: Path, first: int, last: int) -> Extracted:
    """Slides of one lecture: pages first..last with [p.N] markers."""
    try:
        return _pdf(Path(path), list(range(first, last + 1)))
    except (OSError, ValueError, RuntimeError) as exc:
        return Extracted("", f"읽지 못함({type(exc).__name__})")


# ---------- HWP 5 (compound file)
def hwp_records(stream: bytes):
    """(tag, level, payload) records of one HWP 5 record stream (already inflated)."""
    offset = 0
    while offset + 4 <= len(stream):
        header = struct.unpack_from("<I", stream, offset)[0]
        offset += 4
        tag, level, size = header & 0x3FF, (header >> 10) & 0x3FF, header >> 20
        if size == 0xFFF:
            size = struct.unpack_from("<I", stream, offset)[0]
            offset += 4
        yield tag, level, stream[offset:offset + size]
        offset += size


def hwp_para_text(payload: bytes) -> str:
    chars = struct.unpack(f"<{len(payload) // 2}H", payload[: len(payload) // 2 * 2])
    kept, i = [], 0
    while i < len(chars):
        code = chars[i]
        if code in _HWP_WIDE:
            if code == 9:
                kept.append(9)
            i += 8
            continue
        if code in (10, 13):
            kept.append(10)
        elif code >= 32:
            kept.append(code)  # surrogate halves stay paired for the UTF-16 decode below
        i += 1
    return struct.pack(f"<{len(kept)}H", *kept).decode("utf-16-le", errors="replace").rstrip("\n")


def hwp_section_text(raw: bytes, compressed: bool) -> str:
    stream = zlib.decompress(raw, -15) if compressed else raw
    return "\n".join(t for tag, _level, payload in hwp_records(stream)
                     if tag == HWPTAG_PARA_TEXT and (t := hwp_para_text(payload)).strip())


def _hwp(path: Path) -> Extracted:
    try:
        import olefile
    except ImportError:
        return Extracted("", "HWP를 읽으려면 olefile이 필요합니다(pip install -e \".[materials]\")")
    if not olefile.isOleFile(str(path)):
        return Extracted("", "HWP 5 형식이 아님(HWP 3 이하 등)")
    with olefile.OleFileIO(str(path)) as ole:
        header = ole.openstream("FileHeader").read()
        flags = struct.unpack_from("<I", header, 36)[0]
        if flags & 0x2:
            return Extracted("", "암호가 걸린 HWP")
        if flags & 0x4:
            return Extracted("", "배포용 HWP(글자 추출 막힘) — 한글에서 열어 확인")
        sections = sorted((entry for entry in ole.listdir() if len(entry) == 2 and entry[0] == "BodyText"),
                          key=lambda entry: int(re.sub(r"\D", "", entry[1]) or 0))
        texts = [hwp_section_text(ole.openstream(entry).read(), bool(flags & 0x1)) for entry in sections]
    return Extracted("\n\n".join(t for t in texts if t))


# ---------- zip + XML formats
def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _zip_xml(path: Path, wanted, *, text_tag: str, para_tag: str, numbered: str | None = None) -> str:
    with zipfile.ZipFile(path) as archive:
        names = sorted((n for n in archive.namelist() if wanted(n)),
                       key=lambda n: [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", n)])
        parts = []
        for index, name in enumerate(names, 1):
            root = ElementTree.fromstring(archive.read(name))
            paragraphs = []
            for element in root.iter():
                if _local(element.tag) == para_tag:
                    line = "".join(t.text or "" for t in element.iter() if _local(t.tag) == text_tag)
                    if line.strip():
                        paragraphs.append(line)
            body = "\n".join(_dedupe_nested(paragraphs))
            parts.append(f"[{numbered} {index}]\n{body}" if numbered else body)
    return "\n\n".join(p for p in parts if p.strip())


def _dedupe_nested(lines: list[str]) -> list[str]:
    """Table cells hold their own paragraphs inside an outer one: drop an outer line that only repeats inner ones."""
    out: list[str] = []
    for line in lines:
        if out and line and out[-1] and line != out[-1] and line in out[-1]:
            continue
        out.append(line)
    return out


def _xlsx(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            for item in ElementTree.fromstring(archive.read("xl/sharedStrings.xml")):
                shared.append("".join(t.text or "" for t in item.iter() if _local(t.tag) == "t"))
        sheet_names = []
        if "xl/workbook.xml" in names:
            sheet_names = [s.get("name") for s in ElementTree.fromstring(archive.read("xl/workbook.xml")).iter()
                           if _local(s.tag) == "sheet"]
        sheets = sorted((n for n in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)),
                        key=lambda n: int(re.sub(r"\D", "", n)))
        parts = []
        for index, name in enumerate(sheets):
            rows = []
            for row in ElementTree.fromstring(archive.read(name)).iter():
                if _local(row.tag) != "row":
                    continue
                cells = []
                for cell in row:
                    if _local(cell.tag) != "c":
                        continue
                    value = next((v.text for v in cell if _local(v.tag) == "v"), None)
                    inline = "".join(t.text or "" for t in cell.iter() if _local(t.tag) == "t")
                    if cell.get("t") == "s" and value is not None:
                        value = shared[int(value)] if int(value) < len(shared) else ""
                    elif cell.get("t") == "inlineStr":
                        value = inline
                    formula = next((f.text for f in cell if _local(f.tag) == "f"), None)
                    shown = (value or "") + (f" (={formula})" if formula else "")
                    if shown:
                        cells.append(f"{cell.get('r')}: {shown}")
                if cells:
                    rows.append("\t".join(cells))
            title = sheet_names[index] if index < len(sheet_names) else f"시트 {index + 1}"
            parts.append(f"[시트 {title}]\n" + "\n".join(rows))
    return "\n\n".join(parts)
