"""D-084 login preferences. Only synthetic registry keys, no Windows API calls."""
from contextlib import nullcontext
from pathlib import Path
import subprocess
import sys

import pytest

from lecturehub.core import package, settings, startup


class Registry:
    HKEY_CURRENT_USER, KEY_READ, KEY_SET_VALUE, REG_SZ = range(4)

    def __init__(self):
        self.values = {"OtherApp": "leave alone"}
        self.writes = []

    def OpenKey(self, hive, path, reserved, access):
        assert hive == self.HKEY_CURRENT_USER and path == startup.RUN_KEY
        return nullcontext(self)

    CreateKeyEx = OpenKey

    def QueryValueEx(self, key, name):
        if name not in self.values:
            raise FileNotFoundError()
        return self.values[name], self.REG_SZ

    def SetValueEx(self, key, name, reserved, kind, value):
        self.writes.append((name, value))
        self.values[name] = value

    def DeleteValue(self, key, name):
        if name not in self.values:
            raise FileNotFoundError()
        self.writes.append((name, None))
        del self.values[name]


@pytest.fixture(autouse=True)
def ordinary(monkeypatch):
    monkeypatch.setattr(package, "packaged", lambda: False)
    monkeypatch.setattr(startup, "_registry", lambda registry=None: registry if registry is not None else pytest.fail("real registry"))


def test_STAY_001_close_choice_is_unset_until_chosen_and_survives_settings_roundtrip():
    assert startup.close_choice() is None and startup.reminders_on()
    for on in (True, False):
        startup.set_close_choice(on)
        assert startup.close_choice() is on and settings.load()[startup.STAY_KEY] is on
    settings.save({"reminders": {"deadlines": False, "briefing": False}})
    assert not startup.reminders_on()
    with pytest.raises(ValueError):
        startup.set_close_choice("yes")


def test_STARTUP_001_registry_default_optin_readback_remove_only_our_value(monkeypatch):
    registry = Registry()
    monkeypatch.setattr(startup, "command", lambda: '"C:\\합성 앱 (체험)\\LectureHub.exe" --background')
    assert startup.status(registry=registry)["enabled"] is False
    assert registry.writes == []
    assert startup.set_enabled(True, registry=registry)["enabled"] is True
    assert registry.values[startup.RUN_NAME].endswith(" --background")
    registry.values.pop(startup.RUN_NAME)  # an external change must be visible
    assert startup.status(registry=registry)["enabled"] is False
    startup.set_enabled(True, registry=registry)
    assert startup.set_enabled(False, registry=registry)["enabled"] is False
    assert startup.set_enabled(False, registry=registry)["ok"]  # already absent
    assert registry.values == {"OtherApp": "leave alone"}


def test_STARTUP_002_registry_denied_does_not_claim_success():
    class Denied(Registry):
        def OpenKey(self, *args):
            raise PermissionError()
        CreateKeyEx = OpenKey
    assert not startup.status(registry=Denied())["ok"]
    assert not startup.set_enabled(True, registry=Denied())["ok"]
    assert not startup.set_enabled(False, registry=Denied())["ok"]


def test_STARTUP_003_store_never_reads_or_writes_run_and_only_opens_fixed_windows_page(monkeypatch):
    monkeypatch.setattr(package, "packaged", lambda: True)
    assert startup.status() == {"ok": True, "store": True, "enabled": None, "possible": True}
    assert not startup.set_enabled(True)["ok"] and not startup.set_enabled(False)["ok"]
    opened = []
    assert startup.open_settings(opener=opened.append)["ok"]
    assert opened == ["ms-settings:startupapps"]


@pytest.mark.parametrize("frozen", [False, True])
def test_STARTUP_004_command_uses_own_absolute_launcher_and_handles_korean_paths(tmp_path, monkeypatch, frozen):
    folder = tmp_path / "합성 앱 (체험)"
    folder.mkdir()
    exe = folder / ("LectureHub.exe" if frozen else "python.exe")
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    if frozen:
        expected = [str(exe), "--background"]
    else:
        windowed = folder / "pythonw.exe"
        windowed.touch()
        expected = [str(windowed), str(Path(startup.__file__).resolve().parents[3] / "packaging/launcher_app.py"), "--background"]
    assert startup.command() == subprocess.list2cmdline(expected)
