"""Tests for the first-run setup pieces (settings resolver, write guard, setup.apply, root callback)."""
import json

import pytest

from lecturehub.core import settings, tools
from lecturehub.recorder import paths
from lecturehub.ui import setup


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("LECTUREHUB_SETTINGS", str(tmp_path / "appdata" / "settings.json"))
    monkeypatch.setenv("LECTUREHUB_RECORDER_CONFIG", str(tmp_path / "appdata" / "recorder.toml"))
    monkeypatch.delenv("LECTUREHUB_ROOT", raising=False)
    monkeypatch.setattr(paths, "SEMESTER_ROOT", None)


def test_root_is_none_until_configured_and_env_wins(tmp_path, monkeypatch):
    assert settings.data_root() is None
    settings.save({"data_root": str(tmp_path / "a")})
    assert settings.data_root() == tmp_path / "a"
    monkeypatch.setenv("LECTUREHUB_ROOT", str(tmp_path / "b"))
    assert settings.data_root() == tmp_path / "b"


def test_save_merges_and_none_removes(tmp_path):
    settings.save({"x": 1, "y": 2})
    settings.save({"x": None, "z": "한글"})
    assert settings.load() == {"y": 2, "z": "한글"}
    assert json.loads(settings.settings_path().read_text(encoding="utf-8"))["z"] == "한글"


def test_unreadable_settings_file_is_empty(tmp_path):
    settings.settings_path().parent.mkdir(parents=True)
    settings.settings_path().write_text("{not json", encoding="utf-8")
    assert settings.load() == {}


def test_require_root_explicit_or_error(tmp_path):
    assert settings.require_root(tmp_path) == tmp_path
    with pytest.raises(settings.NeedsSetup):
        settings.require_root()


def test_write_guard_follows_configured_root(tmp_path):
    root = tmp_path / "학기 (한글)"
    assert paths.ensure_writable(root / "녹음본" / "x")  # nothing configured: no guard
    settings.save({"data_root": str(root)})
    with pytest.raises(ValueError):
        paths.ensure_writable(root / "녹음본" / "x")
    assert paths.ensure_writable(root / "_lecturehub" / "recordings").is_relative_to(root.resolve())
    assert paths.ensure_writable(tmp_path / "elsewhere" / "f")


def test_setup_apply_writes_folder_timetable_and_config(tmp_path):
    courses = [{"name": "회계 원리", "slots": [{"day": "월", "start": "9:00", "end": "10:15"}]}, {"name": "구조역학", "slots": []}]
    result = setup.apply(str(tmp_path / "내 학기"), "26-2", courses)
    root = tmp_path / "내 학기" / "26-2"
    assert result["ok"] and result["root"] == str(root)
    assert (root / "회계 원리").is_dir() and (root / "_lecturehub" / "recordings").is_dir()
    csv_text = (root / "시간표.csv").read_text(encoding="utf-8-sig")
    assert csv_text.splitlines()[0] == "폴더,과목,요일,시작,종료"
    assert "회계 원리,회계 원리,월,09:00,10:15" in csv_text
    from lecturehub.recorder.classconfig import load_config

    config = load_config()
    assert config.out_dir == root / "_lecturehub" / "recordings" and config.timetable == root / "시간표.csv"
    assert settings.data_root() == root


def test_setup_apply_never_overwrites_existing_timetable_or_config(tmp_path):
    root = tmp_path / "p" / "26-2"
    root.mkdir(parents=True)
    (root / "시간표.csv").write_text("keep", encoding="utf-8")
    config = tmp_path / "appdata" / "recorder.toml"
    config.parent.mkdir(parents=True)
    config.write_text("keep", encoding="utf-8")
    setup.apply(str(tmp_path / "p"), "26-2", [{"name": "A", "slots": [{"day": "화", "start": "10:00", "end": "11:00"}]}])
    assert (root / "시간표.csv").read_text(encoding="utf-8") == "keep" and config.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize("courses", [
    [{"name": "", "slots": []}],
    [{"name": "_hidden", "slots": []}],
    [{"name": "A", "slots": []}, {"name": "A", "slots": []}],
    [{"name": "A", "slots": [{"day": "x", "start": "09:00", "end": "10:00"}]}],
    [{"name": "A", "slots": [{"day": "월", "start": "11:00", "end": "10:00"}]}],
    [{"name": "A", "slots": [{"day": "월", "start": "9am", "end": "10:00"}]}],
])
def test_setup_rejects_bad_courses(tmp_path, courses):
    with pytest.raises(setup.SetupError):
        setup.apply(str(tmp_path), "26-2", courses)
    assert settings.data_root() is None


def test_setup_rejects_relative_folder_and_bad_term(tmp_path):
    with pytest.raises(setup.SetupError):
        setup.apply("relative/dir", "26-2", [])
    with pytest.raises(setup.SetupError):
        setup.apply(str(tmp_path), "../x", [])


def test_course_folder_name_strips_path_characters():
    assert setup.folder_name('회계/원리: "1"') == "회계 원리 1"


def test_suggested_term():
    from datetime import date

    assert setup.suggested_term(date(2026, 11, 10)) == "26-2"
    assert setup.suggested_term(date(2027, 1, 20)) == "26-2"
    assert setup.suggested_term(date(2026, 4, 1)) == "26-1"


def test_tools_status_shape_and_override(tmp_path, monkeypatch):
    fake = tmp_path / "ffmpeg.exe"
    fake.write_text("x")
    monkeypatch.setenv("LECTUREHUB_FFMPEG_EXE", str(fake))
    status = tools.status()
    assert status["ffmpeg"]["found"] and status["ffmpeg"]["path"] == str(fake)
    assert set(status) >= {"ffmpeg", "ffprobe", "codex", "claude"}
    assert all("hint" in row and "needed_for" in row for row in status.values())


def test_backend_without_root_is_inert_and_asks_for_setup(tmp_path):
    from lecturehub.ui.backend import Backend

    backend = Backend(config_path=tmp_path / "missing.toml", fake_engine=True)
    info = backend.init_info()
    assert info["needs_setup"] and info["config_error"]
    assert backend.root == settings.parking_root()
    assert not (backend.root / "_lecturehub").exists()  # constructing wrote nothing


def test_cli_root_callback_fills_configured_folder_or_fails(tmp_path):
    import typer

    with pytest.raises(typer.BadParameter):
        settings.root_option_callback(None)
    settings.save({"data_root": str(tmp_path)})
    assert settings.root_option_callback(None) == tmp_path


def test_gsr_companions_are_parsed_not_saved_and_kept_out_of_the_code(tmp_path):
    from lecturehub.core import settings

    rows = settings.parse_companions("홍 길동 20201234\n김철수,20211111\n\n")
    assert rows == [["홍 길동", "20201234"], ["김철수", "20211111"]]
    assert "gsr" not in settings.load()
    for bad in ("이름만", "이름 abc", "이름 123", "\n".join(f"a{i} 2020000{i}" for i in range(9))):
        with pytest.raises(ValueError):
            settings.parse_companions(bad)
    from pathlib import Path

    core = Path(settings.__file__).parents[1] / "booking" / "assets" / "gsr_core.js"
    assert "companions: []" in core.read_text(encoding="utf-8")


def test_browser_run_hands_the_typed_companions_to_the_adapter(monkeypatch):
    from lecturehub.booking import browser
    seen = {}

    class Fake:
        @staticmethod
        def run(action, stop=None):
            seen.update(action)
            return {"ok": True}

    monkeypatch.setattr(browser, "aside_cli", Fake)
    crew = [["가나", "20200001"], ["다라", "20200002"]]
    # user_confirmed: the public edition books only what the user confirmed (booking/browser.py); the full one ignores it
    result = browser.run({"action": "book", "companions": crew, "user_confirmed": True}, which="aside")
    assert result["ok"] and seen["companions"] == crew  # rooms: full build only (test_booking)
