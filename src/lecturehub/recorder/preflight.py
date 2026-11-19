"""Two things that make a recording silently worthless on someone else's PC (release review 2026-11-16).

- **Windows microphone privacy**: with "let desktop apps access your microphone" off, the device still opens and
  delivers pure silence. The sound check then read "almost no input — normal in a quiet room". ``microphone_access``
  reads the same switches Windows Settings shows, so the app can say what is wrong and where to turn it on.
- **Disk space**: uncompressed audio is written while recording (about 5.8 MB a minute at 48 kHz mono). At the
  end the pieces are merged into one WAV of the same size and the MP3 is made from it, all while the pieces are
  still there, so a recording that ends with its MP3 needs a little over twice what was written. A full disk
  used to be noticed only after the fact. ``disk_space`` says before the start how many hours of finished
  recording fit, and the recorder warns while it runs low (``service._disk_state``).

Neither ever refuses a recording (D-050): the registry can be read wrongly on some editions of Windows and a
drive can report odd numbers; a wrong "no" here would be worse than a warning.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

SETTINGS_URI = "ms-settings:privacy-microphone"
_KEY = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"
COMMON_RATE = 48000                         # the usual device rate; the real one is known only once it is open
MP3_BYTES_PER_SECOND = 96_000 // 8          # the master made at the end (finalize.py: mono speech at 96 kbps)
WARN_BELOW_HOURS = 3.0                      # say so before the start (hours of finished recording)
LOW_BELOW_BYTES = 500 * 1024 * 1024         # while recording: warn when less than this is spare (service._disk_state);
#                                             at 48 kHz that is room to record and finish about 43 more minutes


def finish_bytes(recorded_ms: int, sample_rate: int) -> int:
    """What finishing a recording of this length still needs once its pieces are written: the merged WAV
    (16-bit mono, the same bytes again) and the MP3. Making the pieces smaller afterwards is not counted."""
    return max(0, int(recorded_ms)) * (sample_rate * 2 + MP3_BYTES_PER_SECOND) // 1000


def finished_bytes_per_hour(sample_rate: int = COMMON_RATE) -> int:
    """Disk room one hour of recording needs from start to MP3: the pieces plus ``finish_bytes``."""
    return sample_rate * 2 * 3600 + finish_bytes(3_600_000, sample_rate)


def microphone_access(read=None) -> dict:
    """``{"allowed": True|False|None, "blocked_by": "device"|"user"|"desktop_apps"|None}``.

    ``None`` means it could not be told (not Windows, or the keys are not there). ``read(hive, key)`` returns the
    ``Value`` string ("Allow"/"Deny") or ``None``; it is a test seam.
    """
    if os.name != "nt" and read is None:
        return {"allowed": None, "blocked_by": None}
    read = read or _read
    checks = (("device", "HKLM", _KEY), ("user", "HKCU", _KEY), ("desktop_apps", "HKCU", _KEY + r"\NonPackaged"))
    seen = False
    for name, hive, key in checks:
        value = read(hive, key)
        if value is None:
            continue
        seen = True
        if str(value).strip().lower() == "deny":
            return {"allowed": False, "blocked_by": name}
    return {"allowed": True if seen else None, "blocked_by": None}


def _read(hive: str, key: str) -> str | None:
    import winreg

    root = winreg.HKEY_LOCAL_MACHINE if hive == "HKLM" else winreg.HKEY_CURRENT_USER
    try:
        with winreg.OpenKey(root, key) as handle:
            value, _kind = winreg.QueryValueEx(handle, "Value")
    except OSError:
        return None
    return str(value) if value else None


BLOCKED_TEXT = {
    "device": "이 PC에서 마이크 접근이 꺼져 있습니다(Windows 설정 > 개인 정보 및 보안 > 마이크 > 마이크 액세스).",
    "user": "Windows 설정에서 앱의 마이크 접근이 꺼져 있습니다(개인 정보 및 보안 > 마이크 > 앱에서 마이크에 액세스하도록 허용).",
    "desktop_apps": "Windows 설정에서 데스크톱 앱의 마이크 접근이 꺼져 있습니다(개인 정보 및 보안 > 마이크 > 데스크톱 앱이 마이크에 액세스하도록 허용).",
}


def disk_space(folder: Path, usage=shutil.disk_usage) -> dict:
    """Free space where recordings go: ``{"free_bytes", "hours", "low"}`` (``None`` values when it cannot be read).

    ``hours`` is how long a recording may be and still end with its MP3 (``finished_bytes_per_hour`` at the
    usual 48 kHz; one recording at a time). ``low`` is ``hours < WARN_BELOW_HOURS``.
    """
    probe = Path(folder)
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        free = int(usage(probe).free)
    except OSError:
        return {"free_bytes": None, "hours": None, "low": False}
    hours = free / finished_bytes_per_hour()
    return {"free_bytes": free, "hours": round(hours, 1), "low": hours < WARN_BELOW_HOURS}


def disk_warning(folder: Path, usage=shutil.disk_usage) -> str | None:
    """The sentence shown before a recording starts when little room is left, else ``None``."""
    space = disk_space(folder, usage)
    if not space["low"]:
        return None
    gigabytes = space["free_bytes"] / 2**30
    return (f"녹음을 저장할 디스크에 {gigabytes:.1f} GB만 남았습니다. 끝까지 저장할 수 있는 녹음은 약 "
            f"{space['hours']:.1f}시간입니다. 공간이 다 차면 녹음이 멈춥니다. "
            "필요 없는 파일을 지우거나 다른 드라이브의 학기 폴더를 쓰세요.")
