"""The folder organizer works for any student's course folders (release prep): English names, configurable old notes folder."""
import pytest

from lecturehub.core import settings
from lecturehub.index import organize


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("LECTUREHUB_SETTINGS", str(tmp_path / "appdata" / "settings.json"))
    monkeypatch.delenv("LECTUREHUB_ROOT", raising=False)
    settings.set_folder_layout("detailed")  # these cases cover the six-category classifier (기본 folds three of them)


@pytest.mark.parametrize("name,expected", [
    ("Homework 3.pdf", "과제"), ("hw2_solutions.pdf", "과제"), ("problem_set_1.pdf", "과제"),
    ("Lab 4 handout.pdf", "실습자료"), ("tutorial-02.pptx", "실습자료"),
    ("Textbook ch1.pdf", "교재"), ("Reference manual.pdf", "참고자료"),
    ("lecture_notes_week3.html", "정리본"), ("Week 3 slides.pdf", "강의자료"),
    ("syllabus.pdf", None), ("강의계획서.pdf", None), ("notes.xyz", None),
])
def test_guess_knows_english_and_korean_names(name, expected):
    assert organize.guess(name) == expected


def test_old_notes_folder_is_only_read_as_notes_when_configured(tmp_path):
    course = tmp_path / "과목"
    (course / "수업정리html").mkdir(parents=True)
    note = course / "수업정리html" / "a.html"
    note.write_text("x", encoding="utf-8")
    assert organize.category_of(course, note) is None  # a new install has no such convention
    settings.save({"legacy_note_folders": ["수업 정리 html"]})
    assert organize.category_of(course, note) == "정리본"
    assert organize.folder_for(course, "정리본") == course / "수업정리html"


def test_settings_legacy_folders_ignore_junk():
    settings.save({"legacy_note_folders": "not a list"})
    assert settings.legacy_note_folders() == frozenset()


def test_course_abbr_has_no_built_in_course_map():
    from lecturehub.index.ingest import _course_abbr

    assert _course_abbr("과목A") == "과목A"            # non-ASCII folder keeps the old fallback (the folder name)
    assert _course_abbr("Linear Algebra") == "linear-algebra"
    settings.save({"course_abbreviations": {"과목A": "ca", "": "x", "빈칸": " "}})
    assert settings.course_abbreviations() == {"과목A": "ca"}
    assert _course_abbr("과목A") == "ca"
    settings.save({"course_abbreviations": ["not", "a", "dict"]})
    assert settings.course_abbreviations() == {}
