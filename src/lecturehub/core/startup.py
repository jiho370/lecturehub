"""D-084: opt-in Windows login launch. No DB, registry access only on request.

Store packages use their manifest startupTask and Windows Settings, never the
virtualized Run key. Pass a registry module double in tests; no test needs HKCU.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from . import package, settings

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "LectureHub"
STARTUP_PAGE = "ms-settings:startupapps"
STAY_KEY = "keep_running_for_reminders"


def close_choice() -> bool | None:
    choice = settings.get(STAY_KEY)
    return choice if isinstance(choice, bool) else None


def set_close_choice(on: bool) -> None:
    if not isinstance(on, bool):
        raise ValueError("알림 유지 여부를 골라 주세요.")
    settings.save({STAY_KEY: on})


def reminders_on() -> bool:
    from .reminder_settings import preferences

    prefs = preferences()
    return prefs["deadlines"] or prefs["briefing"]


def command() -> str:
    """Absolute launcher path, quoted with Windows rules (also Korean/space/parentheses)."""
    executable = Path(sys.executable).resolve()
    if getattr(sys, "frozen", False):
        args = [str(executable.with_name("LectureHub.exe"))]
    else:
        windowed = executable.with_name("pythonw.exe")
        args = [str(windowed if windowed.is_file() else executable),
                str(Path(__file__).resolve().parents[3] / "packaging" / "launcher_app.py")]
    return subprocess.list2cmdline([*args, "--background"])


def _registry(registry=None):
    if registry is not None:
        return registry
    import winreg

    return winreg


def status(*, registry=None) -> dict:
    if package.packaged():
        return {"ok": True, "store": True, "enabled": None, "possible": True}
    if sys.platform != "win32" and registry is None:
        return {"ok": True, "store": False, "enabled": False, "possible": False}
    try:
        reg = _registry(registry)
        try:
            with reg.OpenKey(reg.HKEY_CURRENT_USER, RUN_KEY, 0, reg.KEY_READ) as key:
                value, _kind = reg.QueryValueEx(key, RUN_NAME)
        except FileNotFoundError:
            value = ""
        return {"ok": True, "store": False, "enabled": bool(value), "possible": True}
    except OSError:
        return {"ok": False, "store": False, "enabled": None, "possible": False,
                "error": "Windows 시작 설정을 읽지 못했습니다."}


def set_enabled(on: bool, *, registry=None) -> dict:
    if package.packaged():
        return {**status(), "ok": False, "error": "Windows 시작 앱 설정에서 Lecture Hub를 켜거나 끄세요."}
    if not isinstance(on, bool):
        return {"ok": False, "error": "시작 여부를 골라 주세요."}
    if sys.platform != "win32" and registry is None:
        return status()
    try:
        reg = _registry(registry)
        if on:
            with reg.CreateKeyEx(reg.HKEY_CURRENT_USER, RUN_KEY, 0, reg.KEY_SET_VALUE) as key:
                reg.SetValueEx(key, RUN_NAME, 0, reg.REG_SZ, command())
        else:
            try:
                with reg.OpenKey(reg.HKEY_CURRENT_USER, RUN_KEY, 0, reg.KEY_SET_VALUE) as key:
                    reg.DeleteValue(key, RUN_NAME)
            except FileNotFoundError:
                pass
        return status(registry=reg)  # read back; never a cached settings.json flag
    except OSError:
        return {**status(registry=registry), "ok": False, "error": "Windows 시작 설정을 바꾸지 못했습니다."}


def open_settings(*, opener=None) -> dict:
    try:
        (opener or os.startfile)(STARTUP_PAGE)
        return {"ok": True}
    except OSError:
        return {"ok": False, "error": "Windows 설정 › 앱 › 시작 프로그램을 열어 주세요."}
