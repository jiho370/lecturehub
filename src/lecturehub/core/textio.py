"""Strict, BOM-aware text input with explicit legacy-encoding failures."""

import codecs
from os import PathLike
from pathlib import Path


class EncodingSuspectError(ValueError):
    """Text cannot be decoded safely using UTF-8 or its declared Unicode BOM."""


def read_text_any(path: str | PathLike[str]) -> str:
    """Read a file once, honoring Unicode BOMs and normalizing newlines to LF.

    The source file is only read and is never converted in place.
    """
    source = Path(path)
    return decode_text(source.read_bytes(), source=source)


def decode_text(data: bytes, *, source: str | PathLike[str] = "<bytes>") -> str:
    """Decode a captured byte snapshot and normalize CRLF/CR newlines to LF.

    UTF-8 is the default. An explicit UTF-8, UTF-16 or UTF-32 BOM selects that
    encoding; no legacy encoding is silently guessed. A cp949-decodable input
    that fails UTF-8 produces a clear error instead of replacement characters.
    ``source`` supplies a file label for diagnostics without reading that path.
    """
    if data.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        encoding = "utf-32"
    elif data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        encoding = "utf-16"
    else:
        encoding = "utf-8-sig"
    try:
        text = data.decode(encoding)
    except UnicodeDecodeError as error:
        suspected = ""
        if encoding == "utf-8-sig" and not data.startswith(codecs.BOM_UTF8):
            try:
                data.decode("cp949")
            except UnicodeDecodeError:
                pass
            else:
                suspected = " cp949 인코딩으로 의심됩니다."
        raise EncodingSuspectError(
            f"{source}: {encoding} 텍스트로 읽을 수 없습니다.{suspected} "
            "원본을 유지하고 UTF-8 사본으로 변환한 뒤 다시 시도하세요."
        ) from error
    return text.replace("\r\n", "\n").replace("\r", "\n")
