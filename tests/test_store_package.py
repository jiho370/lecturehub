"""Microsoft Store (MSIX) package: identity checks, manifest, tiles, and what changes when the app runs packaged
(user decision 2026-11-19: publish on the Store, which signs the package)."""
import importlib.util
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from lecturehub.core import package, update

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("build_msix", ROOT / "tools/build_msix.py")
msix = importlib.util.module_from_spec(spec)
spec.loader.exec_module(msix)
NS = {"m": "http://schemas.microsoft.com/appx/manifest/foundation/windows10",
      "uap": "http://schemas.microsoft.com/appx/manifest/uap/windows10",
      "uap3": "http://schemas.microsoft.com/appx/manifest/uap/windows10/3",
      "desktop": "http://schemas.microsoft.com/appx/manifest/desktop/windows10",
      "rescap": "http://schemas.microsoft.com/appx/manifest/foundation/windows10/restrictedcapabilities"}


@pytest.fixture(autouse=True)
def _fresh_package_cache():
    package.full_name.cache_clear()
    package.family_name.cache_clear()
    yield
    package.full_name.cache_clear()
    package.family_name.cache_clear()


def test_store_version_has_four_parts_and_a_zero_revision():
    assert msix.store_version("1.0.0") == "1.0.0.0"
    assert msix.store_version("2.13.4") == "2.13.4.0"
    for bad in ("1.0", "1.0.0.1", "v1.0.0", "1.x.0"):
        with pytest.raises(ValueError):
            msix.store_version(bad)


def test_version_comes_from_the_repository_not_the_compiled_build():
    from lecturehub import __version__

    assert msix.app_version() == __version__


def test_identity_must_look_like_partner_center_values():
    msix.check_identity("12345Synthetic.LectureHub", "CN=00000000-0000-0000-0000-000000000000")
    with pytest.raises(ValueError):
        msix.check_identity("bad name with spaces", "CN=x")
    with pytest.raises(ValueError):
        msix.check_identity("Synthetic.App", "O=not a CN")


def test_manifest_is_valid_xml_with_the_identity_alias_and_capabilities():
    text = msix.manifest_text((ROOT / msix.MANIFEST).read_text(encoding="utf-8"), name="Synthetic.LectureHub",
                              publisher="CN=SYNTHETIC-PUBLISHER", publisher_display_name='Synthetic & "Co"',
                              version="1.2.3")
    tree = ET.fromstring(text.encode("utf-8"))
    identity = tree.find("m:Identity", NS)
    assert identity.get("Name") == "Synthetic.LectureHub" and identity.get("Version") == "1.2.3.0"
    assert identity.get("Publisher") == "CN=SYNTHETIC-PUBLISHER"
    assert tree.find("m:Properties/m:PublisherDisplayName", NS).text == 'Synthetic & "Co"'
    apps = tree.findall("m:Applications/m:Application", NS)
    # one Application only: a second one hidden from Start (AppListEntry="none") is a headless app the Store refuses
    assert [a.get("Executable") for a in apps] == ["LectureHub.exe"]
    assert apps[0].get("EntryPoint") == "Windows.FullTrustApplication"
    assert "AppListEntry" not in text
    aliases = [e.get("Alias") for e in tree.iter(f"{{{NS['desktop']}}}ExecutionAlias")]
    assert aliases == ["LectureHubWorker.exe"]
    # makeappx: "windows.appExecutionAlias must only be declared once" per Application
    alias_exes = [e.get("Executable") for e in apps[0].iter(f"{{{NS['uap3']}}}Extension")
                  if e.get("Category") == "windows.appExecutionAlias"]
    assert alias_exes == ["LectureHubWorker.exe"]
    # STARTUP-007 (D-084): opt-in, same Application, no unsupported Parameters on Windows 10 1809.
    startups = [e for e in apps[0].iter(f"{{{NS['desktop']}}}Extension")
                if e.get("Category") == "windows.startupTask"]
    assert len(startups) == 1 and startups[0].get("Executable") == "LectureHubStartup.exe"
    assert startups[0].get("EntryPoint") == "Windows.FullTrustApplication"
    task = startups[0].find("desktop:StartupTask", NS)
    assert task.attrib == {"TaskId": "LectureHubStartup", "Enabled": "false", "DisplayName": "Lecture Hub"}
    assert tree.find("m:Capabilities/rescap:Capability", NS).get("Name") == "runFullTrust"
    assert tree.find("m:Capabilities/m:DeviceCapability", NS).get("Name") == "microphone"
    assert "${" not in text


def test_STARTUP_008_startup_launcher_is_bundled_and_selfchecked_in_the_installed_package():
    spec = (ROOT / "packaging/lecturehub.spec").read_text(encoding="utf-8")
    assert 'name="LectureHubStartup", console=False' in spec
    assert 'startup_exe = EXE(app_pyz, app.scripts' in spec
    assert "COLLECT(app_exe, startup_exe," in spec
    build = (ROOT / "tools/build_msix.py").read_text(encoding="utf-8")
    assert '"LectureHubStartup.exe"' in build
    workflow = (ROOT / ".github/workflows/build.yml").read_text(encoding="utf-8")
    assert r'\LectureHubStartup.exe" -Args "--smoke' in workflow
    assert "selfcheck-msix-startup.json" in workflow


def test_STARTUP_009_uninstaller_only_removes_the_current_installs_optin_run_value():
    from lecturehub.core.startup import RUN_KEY

    script = (ROOT / "packaging/installer.iss").read_text(encoding="utf-8")
    assert "AppMutex=LectureHub.AppRunning" in script and "CloseApplications=no" in script
    cleanup = script[script.index("procedure CurUninstallStepChanged"):]
    assert "CurUninstallStep = usUninstall" in cleanup and "CompareText(StartupCommand" in cleanup
    assert "{app}\\LectureHub.exe" in cleanup and "--background" in cleanup
    assert f"RegDeleteValue(HKCU, '{RUN_KEY}', 'LectureHub')" in cleanup


def test_tiles_have_the_sizes_the_manifest_names(tmp_path):
    from PIL import Image

    made = msix.make_tiles(msix.app_icon(ROOT / "src/lecturehub/ui/assets"), tmp_path / "Assets")
    sizes = {p.name: Image.open(p).size for p in made}
    assert sizes == msix.TILES
    manifest = (ROOT / msix.MANIFEST).read_text(encoding="utf-8")
    assert all(f"Assets\\{name}" in manifest for name in msix.TILES)


def test_not_packaged_by_default_and_real_path_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.delenv("LECTUREHUB_FAKE_PACKAGE", raising=False)
    monkeypatch.delenv("LECTUREHUB_FAKE_PACKAGE_FAMILY", raising=False)
    assert package.packaged() is False  # tests run from source, never from a package
    assert package.real_path(tmp_path / "x") == tmp_path / "x"


def test_real_path_finds_the_redirected_copy_only_when_it_exists(tmp_path, monkeypatch):
    local, roaming = tmp_path / "Local", tmp_path / "Roaming"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setenv("APPDATA", str(roaming))
    monkeypatch.setenv("LECTUREHUB_FAKE_PACKAGE", "Synthetic.LectureHub_1.0.0.0_x64__abc")
    monkeypatch.setenv("LECTUREHUB_FAKE_PACKAGE_FAMILY", "Synthetic.LectureHub_abc")
    cache = local / "Packages" / "Synthetic.LectureHub_abc" / "LocalCache"
    logs = local / "LectureHub" / "logs"
    assert package.real_path(logs) == logs  # nothing redirected yet
    (cache / "Local" / "LectureHub" / "logs").mkdir(parents=True)
    assert package.real_path(logs) == cache / "Local" / "LectureHub" / "logs"
    (cache / "Roaming" / "LectureHub").mkdir(parents=True)
    assert package.real_path(roaming / "LectureHub") == cache / "Roaming" / "LectureHub"
    elsewhere = tmp_path / "Documents" / "학기"
    assert package.real_path(elsewhere) == elsewhere


def test_helpers_are_called_through_the_package_alias_in_the_store_build(tmp_path, monkeypatch):
    import sys

    from lecturehub.core import proc

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "app" / "LectureHub.exe"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.delenv("LECTUREHUB_FAKE_PACKAGE", raising=False)
    assert proc.console_python() == str(tmp_path / "app" / "LectureHubWorker.exe")  # installer build
    monkeypatch.setenv("LECTUREHUB_FAKE_PACKAGE", "Synthetic.LectureHub_1.0.0.0_x64__abc")
    package.full_name.cache_clear()
    assert proc.console_python() == str(tmp_path / "app" / "LectureHubWorker.exe")  # alias missing: install folder
    alias = tmp_path / "Local" / "Microsoft" / "WindowsApps" / "LectureHubWorker.exe"
    alias.parent.mkdir(parents=True)
    alias.write_bytes(b"")
    assert proc.console_python() == str(alias)


def test_the_store_build_never_checks_github_for_updates(monkeypatch):
    import sys

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.delenv("LECTUREHUB_UPDATE_REPO", raising=False)
    monkeypatch.setattr(update.settings, "get", lambda key, default=None: default)
    asked = []

    def opener(*args, **kwargs):
        asked.append(args)
    monkeypatch.delenv("LECTUREHUB_FAKE_PACKAGE", raising=False)
    assert update.enabled() is True and update.status()["possible"] is True
    monkeypatch.setenv("LECTUREHUB_FAKE_PACKAGE", "Synthetic.LectureHub_1.0.0.0_x64__abc")
    package.full_name.cache_clear()
    assert update.enabled() is False and update.status()["possible"] is False
    update.check(force=True, opener=opener)
    assert asked == []
