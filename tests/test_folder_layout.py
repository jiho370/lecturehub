"""Course folder layout (2026-11-13): 기본 · 자세히 · 이미 쓰는 폴더, chosen at first run (synthetic folders only)."""
import json
import os
from pathlib import Path

import pytest

from lecturehub.core import settings, timetable
from lecturehub.index import organize
from lecturehub.ui import setup


@pytest.fixture(autouse=True)
def _no_layout_chosen():
    """This file is about the layout nobody chose yet and about choosing one: take away the layout that the shared
    test settings carry for the other files' fixtures (tests/conftest.py)."""
    path = Path(os.environ["LECTUREHUB_SETTINGS"])
    data = json.loads(path.read_text(encoding="utf-8"))
    data.pop("folders", None)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_defaults_follow_the_edition(monkeypatch):
    monkeypatch.setenv("LECTUREHUB_EDITION", "public")
    assert settings.folder_layout() == {"style": "basic", "label": "기본", "categories": ["강의자료", "과제", "정리본"],
                                        "archive_copies": False}
    monkeypatch.setenv("LECTUREHUB_EDITION", "full")
    assert settings.folder_layout()["style"] == "detailed" and settings.folder_layout()["archive_copies"] is True
    assert settings.set_folder_layout("basic", archive_copies=True)["archive_copies"] is True
    with pytest.raises(ValueError):
        settings.set_folder_layout("fancy")


def test_guess_folds_unused_categories_into_lecture_material(monkeypatch):
    monkeypatch.setenv("LECTUREHUB_EDITION", "public")
    assert organize.guess("실습 3주차.xlsx") == "강의자료"
    assert organize.guess("과제2_제출.docx") == "과제"
    assert organize.guess("강의노트.html") == "정리본"
    settings.set_folder_layout("detailed")
    assert organize.guess("실습 3주차.xlsx") == "실습자료"


def test_archive_copy_is_off_in_the_basic_layout(monkeypatch):
    from lecturehub.index import archive_copy

    monkeypatch.setenv("LECTUREHUB_EDITION", "public")
    assert archive_copy.pending(None) == []  # returns before touching the database
    assert archive_copy.copy_media(None, None, "x")["state"] == "skipped"


def test_setup_with_term_dates_and_layouts(tmp_path, monkeypatch):
    monkeypatch.setattr(setup, "_recorder_config_path", lambda: tmp_path / "cfg" / "recorder.toml")
    rows = [{"name": "미적분", "slots": [{"day": "월", "start": "09:00", "end": "10:15"}]}]
    made = setup.apply(str(tmp_path), "26-2", rows, "2026-09-01", "2026-12-18", "basic")
    root = tmp_path / "26-2"
    assert made["root"] == str(root) and (root / "미적분").is_dir()
    assert not (root / "미적분" / "강의자료").exists()  # category folders appear on first use only
    assert timetable.load_term(root) == {"start": "2026-09-01", "end": "2026-12-18"}
    assert settings.folder_layout()["style"] == "basic"

    mine = tmp_path / "my semester"
    mine.mkdir()
    kept = setup.apply(str(mine), "26-2", rows, layout="existing")
    assert kept["root"] == str(mine) and not (mine / "미적분").exists() and (mine / "_lecturehub").is_dir()
    with pytest.raises(setup.SetupError):
        setup.apply(str(tmp_path / "missing"), "26-2", rows, layout="existing")


def test_layout_preview_lines():
    basic = setup.layout_preview("26-2", ["미적분"], "basic")["lines"]
    assert any("강의자료 · 과제 · 정리본" in line for line in basic) and not any("녹음본" in line for line in basic)
    assert any("녹음본" in line for line in setup.layout_preview("26-2", [], "detailed")["lines"])
