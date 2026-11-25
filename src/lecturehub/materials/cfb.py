"""A small, bounded, read-only reader for OLE compound files (MS-CFB), as much as reading an HWP 5 document needs
(D-078). No new dependency.

The whole file is given as bytes (the caller bounds its size). Every sector chain is checked: a sector number past
the end of the file, a chain that loops or runs longer than the file, a directory tree that loops, and a stream
whose chain ends before its stated size all raise ``CfbError`` instead of reading on. A stream larger than
``max_stream_bytes`` raises ``CfbTooLarge`` before anything of it is read.
"""
from __future__ import annotations

import struct
import sys
from array import array

SIGNATURE = bytes.fromhex("D0CF11E0A1B11AE1")
MAXREGSECT = 0xFFFFFFFA
ENDOFCHAIN = 0xFFFFFFFE
FREESECT = 0xFFFFFFFF
NOSTREAM = 0xFFFFFFFF
MINI_SECTOR = 64
STORAGE, STREAM, ROOT = 1, 2, 5


class CfbError(ValueError):
    """Not a compound file, or a damaged one."""


class CfbTooLarge(CfbError):
    """A stream is larger than the caller allows."""


def _uint32s(data: bytes) -> array:
    values = array("I")
    if values.itemsize != 4:  # pragma: no cover - every platform we ship on has a 4-byte unsigned int
        values = array("L")
    values.frombytes(data[:len(data) // 4 * 4])
    if sys.byteorder != "little":  # pragma: no cover
        values.byteswap()
    return values


class CompoundFile:
    def __init__(self, data: bytes, *, max_stream_bytes: int = 64 * 1024 * 1024):
        self.data, self.max_stream_bytes = data, max_stream_bytes
        if len(data) < 512 or data[:8] != SIGNATURE:
            raise CfbError("복합 문서(OLE) 형식이 아님")
        try:
            self._read_header()
            self._read_fat()
            self._read_directory()
        except struct.error:
            raise CfbError("복합 문서가 잘려 있음") from None

    # --- structure -------------------------------------------------------------------------------------------
    def _read_header(self) -> None:
        _minor, major, order, sector_shift, mini_shift = struct.unpack_from("<HHHHH", self.data, 0x18)
        if order != 0xFFFE or (major, sector_shift) not in ((3, 9), (4, 12)) or mini_shift != 6:
            raise CfbError("복합 문서 머리글이 맞지 않음")
        self.major = major
        self.sector = 1 << sector_shift
        (_dir_sectors, self.fat_sectors, self.dir_start, _transaction, self.cutoff, self.minifat_start,
         self.minifat_sectors, self.difat_start, self.difat_sectors) = struct.unpack_from("<9I", self.data, 0x28)
        # sectors that start inside the file (the last one may be short; a read past the data is caught below)
        self.count = max(0, (len(self.data) - 1) // self.sector)
        if not 1 <= self.fat_sectors <= self.count:
            raise CfbError("복합 문서의 FAT 크기가 맞지 않음")

    def _sector_bytes(self, number: int) -> bytes:
        if number >= self.count:
            raise CfbError("복합 문서의 섹터 번호가 파일 밖을 가리킴")
        start = (number + 1) * self.sector
        chunk = self.data[start:start + self.sector]
        if len(chunk) < self.sector:
            chunk += bytes(self.sector - len(chunk))  # a short last sector: the rest is unused
        return chunk

    def _read_fat(self) -> None:
        difat = list(struct.unpack_from("<109I", self.data, 0x4C))
        per = self.sector // 4
        following, seen = self.difat_start, set()
        while following not in (ENDOFCHAIN, FREESECT) and len(difat) < self.fat_sectors:
            if following in seen or len(seen) > self.count:
                raise CfbError("복합 문서의 DIFAT 사슬이 되돌아옴")
            seen.add(following)
            values = struct.unpack_from(f"<{per}I", self._sector_bytes(following))
            difat.extend(values[:-1])
            following = values[-1]
        fat_ids = [number for number in difat[:self.fat_sectors] if number <= MAXREGSECT]
        if len(fat_ids) < self.fat_sectors:
            raise CfbError("복합 문서의 FAT 목록이 모자람")
        self.fat = _uint32s(b"".join(self._sector_bytes(number) for number in fat_ids))

    def _chain(self, start: int, table: array, limit: int):
        """Sector numbers from ``start`` along ``table``; at most ``limit`` steps and never twice the same."""
        seen: set[int] = set()
        current = start
        while current != ENDOFCHAIN:
            if current > MAXREGSECT or current >= len(table) or current in seen or len(seen) >= limit:
                raise CfbError("복합 문서의 섹터 사슬이 망가짐")
            seen.add(current)
            yield current
            current = table[current]

    def _big_stream(self, start: int, size: int | None) -> bytes:
        """Bytes of a chain in the main FAT; ``size`` None = the whole chain (the directory, the mini FAT)."""
        out = bytearray()
        if start == ENDOFCHAIN or (size == 0):
            return b""
        for number in self._chain(start, self.fat, self.count):
            out += self._sector_bytes(number)
            if size is not None and len(out) >= size:
                return bytes(out[:size])
            if len(out) > len(self.data):  # pragma: no cover - the chain limit already bounds this
                raise CfbError("복합 문서의 섹터 사슬이 너무 김")
        if size is not None:
            raise CfbError("복합 문서의 스트림이 잘려 있음")
        return bytes(out)

    def _read_directory(self) -> None:
        raw = self._big_stream(self.dir_start, None)
        entries = []
        for offset in range(0, len(raw) - 127, 128):
            name_bytes, = struct.unpack_from("<64s", raw, offset)
            name_length, kind = struct.unpack_from("<HB", raw, offset + 0x40)
            left, right, child = struct.unpack_from("<III", raw, offset + 0x44)
            start, = struct.unpack_from("<I", raw, offset + 0x74)
            size, = struct.unpack_from("<Q", raw, offset + 0x78)
            if self.major == 3:
                size &= 0xFFFFFFFF  # version 3: the high half may hold garbage (MS-CFB 2.6.3)
            length = min(max(name_length - 2, 0), 62) // 2 * 2
            name = name_bytes[:length].decode("utf-16-le", "replace")
            entries.append((name, kind, left, right, child, start, size))
        if not entries or entries[0][1] != ROOT:
            raise CfbError("복합 문서의 루트 항목이 없음")
        self.entries = entries
        root = entries[0]
        self.ministream = b""
        self.minifat = array("I")
        if root[6]:
            if root[6] > len(self.data):
                raise CfbError("복합 문서의 작은 스트림 영역이 파일보다 큼")
            self.ministream = self._big_stream(root[5], root[6])
        if self.minifat_sectors and self.minifat_start != ENDOFCHAIN:
            self.minifat = _uint32s(self._big_stream(self.minifat_start, None))
        self.streams: dict[str, tuple] = {}
        stack, visited = [(root[4], "")], set()
        while stack:
            index, prefix = stack.pop()
            if index == NOSTREAM:
                continue
            if index >= len(entries) or index in visited:
                raise CfbError("복합 문서의 디렉터리가 망가짐")
            visited.add(index)
            name, kind, left, right, child, _start, _size = entries[index]
            stack += [(left, prefix), (right, prefix)]
            if kind == STORAGE:
                stack.append((child, f"{prefix}{name}/"))
            elif kind == STREAM:
                self.streams[f"{prefix}{name}"] = entries[index]

    # --- reading ---------------------------------------------------------------------------------------------
    def names(self) -> list[str]:
        """Stream paths such as ``BodyText/Section0``."""
        return list(self.streams)

    def read(self, path: str) -> bytes:
        entry = self.streams.get(path)
        if entry is None:
            raise KeyError(path)
        start, size = entry[5], entry[6]
        if size > self.max_stream_bytes:
            raise CfbTooLarge(f"스트림이 너무 큼({path})")
        if size == 0:
            return b""
        if size >= self.cutoff:
            return self._big_stream(start, size)
        out = bytearray()
        limit = (len(self.ministream) + MINI_SECTOR - 1) // MINI_SECTOR
        for number in self._chain(start, self.minifat, limit):
            offset = number * MINI_SECTOR
            if offset >= len(self.ministream):
                raise CfbError("복합 문서의 작은 섹터가 영역 밖을 가리킴")
            out += self.ministream[offset:offset + MINI_SECTOR]
            if len(out) >= size:
                return bytes(out[:size])
        raise CfbError("복합 문서의 스트림이 잘려 있음")
