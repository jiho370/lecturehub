"""Release review 2026-11-16: what a NEW user's PC needs that the owner's PC already had.

A new install has no library yet, runs as a windowed exe (helpers must go through the worker exe), and is
updated by replacing the app (an older library must upgrade by itself, with a backup).
"""
from __future__ import annotations

import sqlite3
import sys
import tomllib
from datetime import date

import pytest

from lecturehub.core import agent_workspace, db, edition, proc, settings, single_instance
from lecturehub.recorder import paths
from lecturehub.ui import setup
from lecturehub.ui.library import Library

COURSES = [{"name": "합성 과목", "slots": [{"day": "월", "start": "09:00", "end": "10:15"}]}]


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("LECTUREHUB_SETTINGS", str(tmp_path / "appdata" / "settings.json"))
    monkeypatch.setenv("LECTUREHUB_RECORDER_CONFIG", str(tmp_path / "appdata" / "recorder.toml"))
    monkeypatch.delenv("LECTUREHUB_ROOT", raising=False)
    monkeypatch.delenv("LECTUREHUB_EDITION", raising=False)
    monkeypatch.setattr(paths, "SEMESTER_ROOT", None)


# ---------- the library exists after setup
def test_setup_builds_the_library_so_the_window_can_list_it(tmp_path):
    result = setup.apply(str(tmp_path / "학기 모음"), "26-2", COURSES)
    root = tmp_path / "학기 모음" / "26-2"
    assert result["ok"] and (root / "_lecturehub" / "lecturehub.sqlite").is_file()
    shown = Library(root).sidebar(date(2026, 11, 16))
    assert shown["ok"], shown
    assert [c["folder"] for c in shown["courses"]] == ["합성 과목"]


def test_window_builds_a_missing_library_on_first_use(tmp_path):
    root = tmp_path / "26-2"
    (root / "합성 과목").mkdir(parents=True)
    library = Library(root)
    assert not library.path.exists()
    assert library.sidebar(date(2026, 11, 16))["ok"] and library.path.is_file()


def test_missing_semester_folder_is_a_plain_message_not_a_command(tmp_path):
    shown = Library(tmp_path / "없는 폴더").sidebar(date(2026, 11, 16))
    assert not shown["ok"] and "설정" in shown["error"] and "lecturehub index" not in shown["error"]


# ---------- an older library upgrades by itself in an installed build, with a backup
def _older_library(path, monkeypatch, upto: int):
    real = db._packaged_migrations
    with monkeypatch.context() as patch:
        patch.setattr(db, "_packaged_migrations", lambda: {v: f for v, f in real().items() if v <= upto})
        db.init_db(path)


def test_upgrade_backs_up_then_applies_pending_migrations(tmp_path, monkeypatch):
    path = tmp_path / "_lecturehub" / "lecturehub.sqlite"
    newest = db.known_schema_version()
    _older_library(path, monkeypatch, newest - 1)
    assert db.upgrade(path) == [newest]
    backups = list((path.parent / "backup").glob("*.sqlite"))
    assert backups, "an upgrade must leave a recovery point"
    with sqlite3.connect(backups[0]) as old:
        assert max(v for (v,) in old.execute("SELECT version FROM schema_version")) == newest - 1
    assert db.upgrade(path) == []  # current: nothing to do, no further backup
    assert len(list((path.parent / "backup").glob("*.sqlite"))) == len(backups)


def test_window_upgrades_only_in_an_installed_or_public_build(tmp_path, monkeypatch):
    root = tmp_path / "26-2"
    root.mkdir()
    newest = db.known_schema_version()
    _older_library(root / "_lecturehub" / "lecturehub.sqlite", monkeypatch, newest - 1)

    def version():
        with sqlite3.connect(root / "_lecturehub" / "lecturehub.sqlite") as connection:
            return max(v for (v,) in connection.execute("SELECT version FROM schema_version"))

    monkeypatch.setenv("LECTUREHUB_EDITION", "full")  # said here: this file is also run in the public tree
    Library(root)._ensure()  # the owner's source checkout: upgrades stay a deliberate, hand-run step
    assert version() == newest - 1
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    Library(root)._ensure()
    assert version() == newest


# ---------- the installed app is a windowed exe
def test_helpers_are_told_the_worker_exe_not_the_window_exe(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "LectureHub.exe"))
    assert proc.console_python() == str(tmp_path / "LectureHubWorker.exe")
    env = agent_workspace.launch_env(tmp_path)
    assert env["LECTUREHUB_PY"] == str(tmp_path / "LectureHubWorker.exe")


def test_installed_public_build_stays_public(tmp_path, monkeypatch):
    marker = tmp_path / "edition.json"
    marker.write_text('{"edition": "public"}', encoding="utf-8")
    monkeypatch.setattr(edition, "MARKER", marker)
    monkeypatch.setenv("LECTUREHUB_EDITION", "full")
    assert edition.name() == "full"  # a source checkout: tests may override
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert edition.name() == "public" and not edition.has("assignment")


def test_live_transcription_is_not_switched_off_in_the_public_edition(tmp_path, monkeypatch):
    """"captions" in the public edition hides only the separate 자막 button. The recording screen's live
    transcription starts through Backend.captions_start, so that call must not look at the edition."""
    import inspect

    from lecturehub.ui.backend import Backend

    assert "edition.has" not in inspect.getsource(Backend.captions_start)


# ---------- nothing AGPL is needed at run time (PyMuPDF is for tests and tools only)
def test_app_code_never_imports_pymupdf():
    import re
    from pathlib import Path

    import lecturehub

    pattern = re.compile(r"^\s*(import|from)\s+(pymupdf|fitz)\b", re.MULTILINE)
    hits = [str(p) for p in Path(lecturehub.__file__).parent.rglob("*.py") if pattern.search(p.read_text(encoding="utf-8"))]
    assert hits == []


def test_trial_slides_are_written_without_pymupdf_and_can_be_read_back(tmp_path):
    from lecturehub.materials import pdfdoc

    out = tmp_path / "자료" / "합성 슬라이드.pdf"
    pdfdoc.write_slides(out, [("합성 제목", ["첫 줄", "h_f = f · (L/D)"]), ("둘째 쪽", [])])
    with pdfdoc.PdfDoc(out) as pdf:
        assert pdf.page_count == 2
        if pdfdoc.KOREAN_FONT.is_file():
            assert "합성 제목" in pdf.text(0) and "첫 줄" in pdf.text(0) and "둘째 쪽" in pdf.text(1)


# ---------- a new user who typed no class times still gets a library
def test_library_builds_without_a_timetable_or_with_the_header_only(tmp_path):
    for n, table in enumerate((None, "폴더,과목,요일,시작,종료\n")):
        root = tmp_path / f"학기{n}"
        (root / "합성 과목").mkdir(parents=True)
        if table:
            (root / "시간표.csv").write_text(table, encoding="utf-8-sig")
        shown = Library(root).sidebar(date(2026, 11, 16))
        assert shown["ok"], shown


# ---------- a course without a class time is offered as a '수업 외' topic instead of vanishing
def test_course_without_class_time_becomes_an_outside_class_topic(tmp_path):
    from lecturehub.core import extra_courses

    result = setup.apply(str(tmp_path / "p"), "26-2", COURSES + [{"name": "온라인 합성 강의", "slots": []}])
    root = tmp_path / "p" / "26-2"
    assert result["courses"] == ["합성 과목"] and result["extra"] == ["온라인 합성 강의"] and result["skipped"] == []
    assert extra_courses.names(root) == ["온라인 합성 강의"] and not (root / "온라인 합성 강의").exists()
    assert "온라인 합성 강의" not in (root / "시간표.csv").read_text(encoding="utf-8-sig")


# ---------- recorder.toml follows the semester folder
def test_second_setup_points_the_recorder_at_the_new_folder_and_keeps_other_choices(tmp_path):
    setup.apply(str(tmp_path / "a"), "26-2", COURSES)
    config = tmp_path / "appdata" / "recorder.toml"
    config.write_text(config.read_text(encoding="utf-8") + 'device_name = "합성 마이크"\n', encoding="utf-8")
    setup.apply(str(tmp_path / "b"), "27-1", COURSES)
    section = tomllib.loads(config.read_text(encoding="utf-8"))["recorder"]
    new_root = tmp_path / "b" / "27-1"
    assert section["out_dir"] == str(new_root / "_lecturehub" / "recordings")
    assert section["timetable"] == str(new_root / "시간표.csv") and section["device_name"] == "합성 마이크"
    assert "a" in config.with_name("recorder.toml.bak").read_text(encoding="utf-8")


def test_recorder_config_survives_a_quote_in_the_folder_name(tmp_path):
    out_dir = tmp_path / "O'Brien 수업" / "_lecturehub" / "recordings"
    section = tomllib.loads(setup.recorder_toml(out_dir, tmp_path / "O'Brien 수업" / "시간표.csv"))["recorder"]
    assert section["out_dir"] == str(out_dir)


# ---------- one window per settings file
def test_second_window_on_the_same_settings_is_refused(tmp_path):
    name = single_instance.lock_name(tmp_path / "settings.json") + ".test"
    assert single_instance.acquire(name) is True
    if sys.platform == "win32":
        assert single_instance.acquire(name) is False
    other = single_instance.lock_name(tmp_path / "other" / "settings.json")
    assert other != single_instance.lock_name(tmp_path / "settings.json")
    assert single_instance.lock_name(settings.settings_path()).startswith("LectureHub.App.")


# ---------- found by running the installed app by hand (2026-11-16)
def test_first_run_does_not_run_semester_jobs_before_a_folder_is_chosen(tmp_path, monkeypatch):
    from lecturehub.ui.backend import Backend

    calls = []
    fake = type("B", (), {"needs_setup": True, "_cli": lambda self, *a, **k: calls.append(a)})()
    assert Backend.resume_jobs(fake) == [] and calls == []


def test_command_line_error_box_is_reduced_to_its_words(tmp_path, monkeypatch):
    import subprocess
    from types import SimpleNamespace

    from lecturehub.ui import backend as module

    box = ("┌─ Error ─────────────────────┐\n│ Invalid value for '--root': 폴더가 없습니다.   │\n"
           "└─────────────────────────────┘\n")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=2, stdout="", stderr=box))
    fake = SimpleNamespace(python="python", root=tmp_path)
    with pytest.raises(RuntimeError, match="폴더가 없습니다") as caught:
        module.Backend._cli(fake, "lecture", "archive-copy")
    assert "─" not in str(caught.value) and "│" not in str(caught.value)


def test_page_hides_what_the_edition_lacks_before_the_first_run_wizard():
    from pathlib import Path

    import lecturehub

    web = Path(lecturehub.__file__).parent / "ui" / "web"
    script = (web / "app.js").read_text(encoding="utf-8")
    assert script.index("applyLabs();  // before the first-run wizard") < script.index("if (ui.info.needs_setup) { await runSetup(); return; }")
    assert 'id="maiHint"' in (web / "index.html").read_text(encoding="utf-8") and '["maiHint", "mai"]' in script
