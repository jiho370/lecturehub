"""Text of word processor files for MaterialPage (D-078): HWP 5 (.hwp), OWPML (.hwpx) and Word (.docx).

Each reader returns the document's lines grouped by section (구역), in document order, table cells included; the
caller (extract.py) cuts them into numbered pieces (units.CHUNK_CHARS). Nothing here starts another program or
needs a new dependency:
- .hwpx and .docx are zips of XML: the zip-bomb guard of extract.py first (unpacked size, entry count), then each
  XML part read up to ``MAX_XML_BYTES`` and parsed with the standard library; a part that declares a DOCTYPE or
  an entity is not parsed.
- .hwp is an OLE compound file read by the bounded reader in cfb.py. The FileHeader flags say whether the body
  is compressed (raw deflate per BodyText/Section stream) and whether the document is password-encrypted,
  distribution-protected (배포용) or DRM-protected: such a file is not read and gives a clear reason. The
  inflated body is bounded by ``MAX_BODY_BYTES``. Text comes from HWPTAG_PARA_TEXT records (UTF-16LE); inline and
  extended controls (8 code units each) are skipped, a tab and a line break are kept.
Every failure is a ``NotReadable`` (or ``TooLarge``) with a Korean reason, so the background helper writes the
file down as given up and goes on.
"""
from __future__ import annotations

import re
import struct
import zipfile
import zlib
from collections.abc import Callable
from pathlib import Path
from xml.etree import ElementTree

from .cfb import CfbError, CfbTooLarge, CompoundFile
from .extract import MAX_FILE_BYTES, NotReadable, TooLarge, zip_guard

MAX_XML_BYTES = 128 * 1024 * 1024  # one XML part of a .hwpx/.docx, unpacked
MAX_BODY_BYTES = 256 * 1024 * 1024  # all BodyText sections of a .hwp, inflated
OLE_SIGNATURE = bytes.fromhex("D0CF11E0A1B11AE1")

Sections = list[list[str]]


def _nothing() -> None:
    return None


# --- XML (.hwpx, .docx) ----------------------------------------------------------------------------------------
_CONTAINERS = {"subList", "txbxContent"}  # paragraphs inside a paragraph (cell lists, text boxes, footnotes)
_BREAKS = {"lineBreak", "br", "cr"}
_SPACES = {"fwSpace", "nbSpace"}
_SKIP = {"delText", "instrText", "Fallback"}  # deleted text, field codes, the duplicate of a text box


def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _parse(data: bytes, what: str):
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise NotReadable(f"{what} 안에 허용하지 않는 XML 선언이 있음(읽지 않음)")
    try:
        return ElementTree.fromstring(data)
    except ElementTree.ParseError as exc:
        raise NotReadable(f"{what}의 XML을 읽을 수 없음: {exc}") from None


def _inline(element, buffer: list[str], deferred: list, in_text: bool = False) -> None:
    """Text of one paragraph; tables and nested paragraph lists are kept for after the paragraph's own line."""
    for child in element:
        name = _local(child.tag)
        if name == "tbl" or name in _CONTAINERS:
            deferred.append(child)
        elif name in _SKIP:
            pass
        elif name == "t":
            buffer.append(child.text or "")
            _inline(child, buffer, deferred, True)
        elif name == "tab":
            buffer.append("\t")
        elif name in _BREAKS:
            buffer.append("\n")
        elif name in _SPACES:
            buffer.append(" ")
        elif name == "noBreakHyphen":
            buffer.append("-")
        else:
            _inline(child, buffer, deferred, in_text)
        if in_text and child.tail:  # inside a text run (hwpx hp:t) the text after a <hp:tab/> belongs to it
            buffer.append(child.tail)


def _lines(element, out: list[str]) -> None:
    """Paragraphs and tables under ``element`` in document order. A table row is one line, cells joined by ' | '
    (as slide tables are in extract.slide_pages)."""
    for child in element:
        name = _local(child.tag)
        if name == "p":
            buffer: list[str] = []
            deferred: list = []
            _inline(child, buffer, deferred)
            out.append("".join(buffer))
            for inner in deferred:
                if _local(inner.tag) == "tbl":
                    _table(inner, out)
                else:
                    _lines(inner, out)
        elif name == "tbl":
            _table(child, out)
        elif name in _SKIP:
            continue
        else:
            _lines(child, out)


def _table(table, out: list[str]) -> None:
    for row in _rows(table):
        cells = []
        for cell in (c for c in row if _local(c.tag) == "tc"):
            inside: list[str] = []
            _lines(cell, inside)
            cells.append(" ".join(" ".join(line.split()) for line in inside if line.strip()))
        out.append(" | ".join(cells))


def _rows(element):
    """Rows of this table in order (also inside a wrapper such as w:sdt), not those of a table nested in a cell."""
    for child in element:
        name = _local(child.tag)
        if name == "tr":
            yield child
        elif name != "tbl":
            yield from _rows(child)


def _zip_part(archive: zipfile.ZipFile, name: str, what: str) -> bytes:
    info = archive.getinfo(name)
    if info.file_size > MAX_XML_BYTES:
        raise TooLarge(f"{what}의 본문이 너무 큼(읽지 않음)")
    with archive.open(info) as stream:
        data = stream.read(MAX_XML_BYTES + 1)
    if len(data) > MAX_XML_BYTES:
        raise TooLarge(f"{what}의 본문이 너무 큼(읽지 않음)")
    return data


def _open_zip(path: Path, what: str) -> zipfile.ZipFile:
    with path.open("rb") as stream:
        if stream.read(8) == OLE_SIGNATURE:
            raise NotReadable(f"암호가 걸렸거나 옛 형식인 {what}(읽지 않음)")
    zip_guard(path, what)
    try:
        return zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:  # pragma: no cover - zip_guard opened it a moment ago
        raise NotReadable(f"{what} — 열 수 없음: {exc}") from None


def hwpx_sections(path: Path, between: Callable[[], None] = _nothing) -> Sections:
    what = "한글 문서(hwpx)"
    with _open_zip(path, what) as archive:
        names = archive.namelist()
        lowered = {name.lower(): name for name in names}
        manifest = lowered.get("meta-inf/manifest.xml")
        if manifest and b"encryption-data" in _zip_part(archive, manifest, what):
            raise NotReadable("암호가 걸린 한글 문서(읽지 않음)")
        numbered = []
        for name in names:
            found = re.fullmatch(r"contents/section(\d+)\.xml", name, re.IGNORECASE)
            if found:
                numbered.append((int(found[1]), name))
        if not numbered:
            raise NotReadable("본문 구역이 없는 한글 문서(hwpx)")
        sections = []
        for index, (_number, name) in enumerate(sorted(numbered)):
            if index:
                between()
            lines: list[str] = []
            _lines([_parse(_zip_part(archive, name, what), what)], lines)
            sections.append(lines)
        return sections


def docx_sections(path: Path, between: Callable[[], None] = _nothing) -> Sections:
    what = "워드 문서"
    with _open_zip(path, what) as archive:
        lowered = {name.lower(): name for name in archive.namelist()}
        body = lowered.get("word/document.xml")
        if body is None:
            raise NotReadable("본문이 없는 워드 문서(word/document.xml 없음)")
        lines: list[str] = []
        _lines([_parse(_zip_part(archive, body, what), what)], lines)
        return [lines]


# --- HWP 5 (.hwp) ----------------------------------------------------------------------------------------------
HWPTAG_PARA_TEXT = 0x10 + 51  # 67
FLAG_COMPRESSED, FLAG_PASSWORD, FLAG_DISTRIBUTION = 0x1, 0x2, 0x4
FLAG_DRM, FLAG_CERT_ENCRYPTED, FLAG_CERT_DRM = 0x10, 0x100, 0x400
# Control characters of HWP 5 text (HWP 5.0 file format, 표 6): 'char' controls take one code unit, 'inline'
# and 'extended' ones take eight (the code, six units of data, the code again).
_WIDE_CONTROLS = frozenset({1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23})
_CHAR_TEXT = {9: "\t", 10: "\n", 24: "-", 30: " ", 31: " "}


def _para_text(data: bytes) -> str:
    """One PARA_TEXT record: text with the control characters taken out (tab, line break, hyphen kept)."""
    out: list[str] = []
    count = len(data) // 2
    run_start = index = 0
    while index < count:
        code = data[2 * index] | (data[2 * index + 1] << 8)
        if code >= 32:
            index += 1
            continue
        if run_start < index:
            out.append(data[2 * run_start:2 * index].decode("utf-16-le", "replace"))
        out.append(_CHAR_TEXT.get(code, ""))
        index += 8 if code in _WIDE_CONTROLS else 1
        run_start = index
    if run_start < count:
        out.append(data[2 * run_start:2 * count].decode("utf-16-le", "replace"))
    return "".join(out)


def _records(body: bytes) -> list[str]:
    """Paragraph texts of one BodyText section, in order (table cells, footnotes and headers are paragraphs too)."""
    lines, position, end = [], 0, len(body)
    while position + 4 <= end:
        header, = struct.unpack_from("<I", body, position)
        position += 4
        tag, size = header & 0x3FF, header >> 20
        if size == 0xFFF:
            if position + 4 > end:
                raise NotReadable("한글 문서 본문 레코드가 잘려 있음(읽지 않음)")
            size, = struct.unpack_from("<I", body, position)
            position += 4
        if position + size > end:
            raise NotReadable("한글 문서 본문 레코드가 잘려 있음(읽지 않음)")
        if tag == HWPTAG_PARA_TEXT:
            lines.append(_para_text(body[position:position + size]))
        position += size
    return lines


def _inflate(raw: bytes, budget: int) -> bytes:
    inflater = zlib.decompressobj(-15)
    try:
        body = inflater.decompress(raw, budget + 1)
    except zlib.error as exc:
        raise NotReadable(f"한글 문서 본문의 압축을 풀 수 없음: {exc}") from None
    if len(body) > budget:
        raise TooLarge("한글 문서 본문을 풀면 너무 큼(읽지 않음)")
    if not inflater.eof:
        raise NotReadable("한글 문서 본문의 압축 자료가 잘려 있음(읽지 않음)")
    return body


def hwp_sections(path: Path, between: Callable[[], None] = _nothing) -> Sections:
    data = path.read_bytes()
    if data.startswith(b"HWP Document File"):  # HWP 3.x: its own binary format, not a compound file
        raise NotReadable("한글 97 이전(HWP 3) 형식(읽지 않음, 한글에서 새 형식으로 저장하면 읽음)")
    try:
        document = CompoundFile(data, max_stream_bytes=MAX_FILE_BYTES)
    except CfbError:
        raise NotReadable("한글 파일 형식이 아님(읽지 않음)") from None
    names = document.names()
    if "FileHeader" not in names:
        raise NotReadable("한글 파일 형식이 아님(FileHeader 없음, 읽지 않음)")
    try:
        header = document.read("FileHeader")
    except CfbError as exc:
        raise NotReadable(f"한글 문서 머리글을 읽을 수 없음: {exc}") from None
    if len(header) < 40 or not header.startswith(b"HWP Document File"):
        raise NotReadable("한글 파일 형식이 아님(머리글이 다름, 읽지 않음)")
    flags, = struct.unpack_from("<I", header, 36)
    if flags & FLAG_PASSWORD:
        raise NotReadable("암호가 걸린 한글 문서(읽지 않음)")
    if flags & FLAG_DISTRIBUTION:
        raise NotReadable("배포용(복사·인쇄 제한) 한글 문서라 본문이 암호화됨(읽지 않음)")
    if flags & (FLAG_DRM | FLAG_CERT_ENCRYPTED | FLAG_CERT_DRM):
        raise NotReadable("보안(DRM·인증서 암호화)이 걸린 한글 문서(읽지 않음)")
    numbered = []
    for name in names:
        found = re.fullmatch(r"BodyText/Section(\d+)", name)
        if found:
            numbered.append((int(found[1]), name))
    if not numbered:
        raise NotReadable("본문 구역이 없는 한글 문서(읽지 않음)")
    left = MAX_BODY_BYTES
    sections = []
    for index, (_number, name) in enumerate(sorted(numbered)):
        if index:
            between()
        try:
            raw = document.read(name)
        except CfbTooLarge:
            raise TooLarge("한글 문서 본문이 너무 큼(읽지 않음)") from None
        except CfbError as exc:
            raise NotReadable(f"한글 문서 본문을 읽을 수 없음: {exc}") from None
        body = _inflate(raw, left) if flags & FLAG_COMPRESSED else raw
        left -= len(body)
        sections.append(_records(body))
    return sections
