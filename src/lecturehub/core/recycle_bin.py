"""Windows Recycle Bin: move items there (undo possible) and put them back.

No database here: the recorder uses it too (D-050), after a recording is compacted (P-053).
Moved from ``index/wav_cleanup.py``, which re-exports these names.

Nothing here ever deletes for good. On a drive that has no Recycle Bin (USB stick, network drive)
``to_recycle_bin`` raises ``NoRecycleBin`` before it touches anything, so every file is still where it was;
what that means for the user's action is each caller's business (keep the files and say so).
"""
from __future__ import annotations

import os
from pathlib import Path

FO_DELETE = 3
FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 0x4, 0x10, 0x40, 0x400
FOF_WANTNUKEWARNING = 0x4000  # overrides NOCONFIRMATION when an item would be deleted outright
RECYCLE_FLAGS = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI | FOF_WANTNUKEWARNING


# Seen 2026-11-12: for some folders the bin record shows up ~0.1 s after SHFileOperationW returns,
# so one look right away missed 6 of 44 session folders. Look again for up to ~3 s before calling it
# a permanent delete; an item that never shows up still raises.
BIN_CHECK_TRIES, BIN_CHECK_STEP_S = 30, 0.1


def _bin_key(path) -> str:
    return os.path.normpath(str(path)).rstrip("\\/").casefold()


def _in_recycle_bin(paths: list[Path], entries=None, *, tries: int = BIN_CHECK_TRIES,
                    sleep=None) -> list[Path]:
    """The paths that are NOT found in their drive's Recycle Bin (by original path, case-insensitive).

    Re-reads the bin up to ``tries`` times (``BIN_CHECK_STEP_S`` apart) while something is missing,
    since Windows can write the record a moment after the move. ``sleep`` is a test seam."""
    import time

    entries = entries or recycle_bin_entries
    sleep = sleep or time.sleep
    missing = list(paths)
    for attempt in range(max(1, tries)):
        if attempt:
            sleep(BIN_CHECK_STEP_S)
        by_drive = {}
        still = []
        for path in missing:
            anchor = path.anchor or Path.home().anchor
            if anchor not in by_drive:
                try:
                    by_drive[anchor] = {_bin_key(e["original"]) for e in entries(Path(anchor))}
                except OSError:
                    by_drive[anchor] = set()
            if _bin_key(path) not in by_drive[anchor]:
                still.append(path)
        missing = still
        if not missing:
            break
    return missing


class NoRecycleBin(OSError):
    """The drive has no Recycle Bin: nothing was moved and nothing was deleted.

    An ``OSError``, so a caller that already keeps its files on ``OSError`` needs no change. A caller whose own
    work does not depend on the bin (moving a link, index/relink.py) catches it, goes on and reports the files
    it left. ``drives`` are the drive roots without a bin (``"E:\\\\"``)."""

    def __init__(self, drives: list[str]):
        self.drives = list(drives)
        super().__init__(f"{', '.join(self.drives)} 드라이브에는 휴지통이 없어 옮기지 않았습니다(파일은 그대로 있습니다).")


def has_recycle_bin(path: Path, drive_type=None) -> bool:
    """False for removable (USB) and network drives, which Windows gives no Recycle Bin; True when it cannot be
    told, so a normal disk is never refused. ``drive_type(anchor)`` is a test seam for GetDriveTypeW."""
    if os.name != "nt" and drive_type is None:
        return True
    if drive_type is None:
        import ctypes

        drive_type = ctypes.windll.kernel32.GetDriveTypeW
    try:
        kind = drive_type(Path(path).anchor or str(path))
    except (OSError, ValueError):
        return True
    return kind not in (2, 4)  # DRIVE_REMOVABLE, DRIVE_REMOTE


def to_recycle_bin(paths: list[Path], shell=None, entries=None, sleep=None) -> None:
    """Windows Recycle Bin (undo possible) via SHFileOperationW with FOF_ALLOWUNDO.

    Security review 2026-11-05: with NOCONFIRMATION alone, items that cannot be recycled (bigger
    than the bin, bin off, no bin on that volume, path too long) are deleted outright and 0 comes
    back. FOF_WANTNUKEWARNING makes Windows ask first, and afterwards every item must still be gone
    from its place *and* be in the Recycle Bin, or this raises instead of reporting success.
    A path on a drive without a bin raises ``NoRecycleBin`` first: then no item of the call was moved, also not
    the ones on other drives.
    ``shell``/``entries``/``sleep`` are test seams (SHFileOperationW, recycle_bin_entries, time.sleep)."""
    if os.name != "nt" and shell is None:
        raise OSError("휴지통 이동은 Windows에서만 지원합니다")
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                    ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]

    resolved = [Path(p).resolve() for p in paths]
    # Release review 2026-11-16: a USB stick or a network drive has no Recycle Bin. There Windows would ask
    # "delete permanently?" in a window — from a background helper nobody is looking at. Say no before asking:
    # nothing is deleted, the files stay, and the caller gets NoRecycleBin to report.
    if shell is None:
        binless = sorted({p.anchor for p in resolved if not has_recycle_bin(p)})
        if binless:
            raise NoRecycleBin(binless)
    listing = "\0".join(str(p) for p in resolved) + "\0\0"
    operation = SHFILEOPSTRUCTW(None, FO_DELETE, listing, None, RECYCLE_FLAGS, False, None, None)
    result = (shell or ctypes.windll.shell32.SHFileOperationW)(ctypes.byref(operation))
    if result or operation.fAnyOperationsAborted:
        raise OSError(f"휴지통으로 옮기지 못했습니다(코드 {result})")
    left = [p for p in resolved if p.exists()]
    if left:
        raise OSError(f"휴지통으로 옮기지 못한 항목이 있습니다: {left[0].name} 외 {len(left) - 1}개")
    gone = _in_recycle_bin(resolved, entries, sleep=sleep)
    if gone:
        raise OSError(f"휴지통에서 찾을 수 없습니다(영구 삭제되었을 수 있음): {gone[0].name} 외 {len(gone) - 1}개")


def recycle_bin_entries(drive_root: Path | None = None) -> list[dict]:
    """This user's Recycle Bin items on a drive: {"original", "deleted_ms", "item", "info"} (newest first).

    Reads the ``$I…`` records in ``<drive>\\$Recycle.Bin\\<SID>`` (version 1 and 2); folders the user
    cannot read (other accounts) are skipped."""
    import struct

    base = Path(drive_root or Path.home().anchor) / "$Recycle.Bin"
    out = []
    for sid in (p for p in base.iterdir() if p.is_dir()) if base.is_dir() else []:
        try:
            infos = [p for p in sid.iterdir() if p.name.startswith("$I")]
        except OSError:
            continue
        for info in infos:
            try:
                raw = info.read_bytes()
                version, _size, filetime = struct.unpack_from("<qqq", raw, 0)
                if version == 2:
                    length = struct.unpack_from("<i", raw, 24)[0]
                    original = raw[28:28 + length * 2].decode("utf-16-le").rstrip("\0")
                else:
                    original = raw[24:24 + 520].decode("utf-16-le").split("\0")[0]
            except (OSError, struct.error, UnicodeDecodeError):
                continue
            item = info.with_name("$R" + info.name[2:])
            if item.exists():
                out.append({"original": original, "deleted_ms": filetime // 10_000 - 11_644_473_600_000,
                            "item": item, "info": info})
    return sorted(out, key=lambda e: -e["deleted_ms"])


def from_recycle_bin(originals: list[Path], *, not_before_ms: int = 0, entries: list[dict] | None = None) -> list[Path]:
    """Put items back where they were (what Explorer's "복원" does): the newest Recycle Bin item for each
    original path, deleted at or after ``not_before_ms``. A path that exists again is left alone.
    Returns the paths restored."""
    import shutil

    entries = recycle_bin_entries() if entries is None else entries
    restored = []
    for original in originals:
        target = Path(original)
        if target.exists():
            continue
        found = next((e for e in entries if Path(e["original"]) == target and e["deleted_ms"] >= not_before_ms), None)
        if found is None:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(found["item"]), str(target))
        found["info"].unlink(missing_ok=True)
        restored.append(target)
    return restored
