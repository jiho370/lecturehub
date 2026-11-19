"""TT-001: timetable editing, term dates, course order, and schedule sync (synthetic courses only)."""
from contextlib import closing
from datetime import date, datetime

import pytest

from lecturehub.core import timetable
from lecturehub.core.db import connect, migrate
from lecturehub.core.paths import database_path
from lecturehub.index.ingest import sync_schedule
from lecturehub.index.schedule import daily_schedule

COURSES = [
    {"name": "미적분", "slots": [{"day": "월", "start": "9:00", "end": "10:15"}, {"day": "수", "start": "09:00", "end": "10:15"}]},
    {"name": "물리학", "slots": [{"day": "화", "start": "13:00", "end": "14:15"}]},
]


def test_clean_rejects_overlap_and_bad_times():
    with pytest.raises(timetable.TimetableError, match="겹칩니다"):
        timetable.clean(COURSES + [{"name": "화학", "slots": [{"day": "월", "start": "10:00", "end": "11:00"}]}])
    with pytest.raises(timetable.TimetableError):
        timetable.clean([{"name": "화학", "slots": [{"day": "월", "start": "11:00", "end": "10:00"}]}])
    adjacent = timetable.clean(COURSES + [{"name": "화학", "slots": [{"day": "월", "start": "10:15", "end": "11:00"}]}])
    assert adjacent[0]["slots"][0]["start"] == "09:00"


def test_write_backs_up_keeps_folder_on_rename_and_reads_back(tmp_path):
    first = timetable.write(tmp_path, COURSES, now=datetime(2026, 9, 1, 9, 0))
    assert first["backup"] == "" and set(first["new_folders"]) == {"미적분", "물리학"}
    renamed = [{"name": "미적분학 1", "folder": "미적분", "slots": COURSES[0]["slots"]}]
    second = timetable.write(tmp_path, renamed, now=datetime(2026, 9, 2, 9, 0))
    assert second["new_folders"] == [] and second["backup"].endswith("시간표_20260902_090000.csv")
    assert (tmp_path / "물리학").is_dir()  # removing a course never removes its folder
    assert timetable.read(tmp_path) == [{"name": "미적분학 1", "folder": "미적분", "slots": [
        {"day": "월", "start": "09:00", "end": "10:15"}, {"day": "수", "start": "09:00", "end": "10:15"}]}]
    with pytest.raises(timetable.TimetableError, match="수업 시간"):
        timetable.write(tmp_path, [{"name": "세미나", "slots": []}])


def test_term_dates(tmp_path):
    assert timetable.in_term(tmp_path, date(2030, 1, 1))  # no term: every day counts
    timetable.save_term(tmp_path, "2026-09-01", "2026-12-18")
    assert timetable.load_term(tmp_path) == {"start": "2026-09-01", "end": "2026-12-18"}
    assert timetable.in_term(tmp_path, date(2026, 9, 1)) and not timetable.in_term(tmp_path, date(2026, 12, 19))
    for bad in [("2026-12-18", "2026-09-01"), ("2026-09-01", "2027-09-01"), ("9월 1일", "2026-12-18")]:
        with pytest.raises(timetable.TimetableError):
            timetable.save_term(tmp_path, *bad)
    timetable.save_term(tmp_path, "", "")
    assert timetable.load_term(tmp_path) == {}


def test_order_today_first_then_week():
    tuesday = datetime(2026, 9, 29, 8, 0)  # a Tuesday
    names = ["미적분", "물리학", "세미나"]
    slots = {"미적분": [("월", "09:00", "10:15"), ("수", "09:00", "10:15")], "물리학": [("화", "13:00", "14:15")], "세미나": []}
    assert sorted(names, key=lambda n: timetable.order_key(n, slots[n], tuesday)) == ["물리학", "미적분", "세미나"]


def test_term_bounds_and_sync_schedule(tmp_path):
    root = tmp_path / "26-2"
    timetable.write(root, COURSES)
    (root / "_lecturehub").mkdir(exist_ok=True)
    with closing(connect(database_path(root))) as connection:
        migrate(connection)
        sync_schedule(root, connection)
        connection.commit()
        monday = date(2026, 9, 7)
        assert [c.course for c in daily_schedule(connection, monday)] == ["미적분"]
        timetable.save_term(root, "2026-09-08", "2026-12-18")
        assert daily_schedule(connection, monday) == []  # before 개강: no regular class
        timetable.save_term(root, "", "")
        moved = [{"name": "미적분", "slots": [{"day": "목", "start": "15:00", "end": "16:15"}]}, COURSES[1]]
        timetable.write(root, moved)
        result = sync_schedule(root, connection)
        connection.commit()
        assert result["removed"] == 2 and result["failures"] == []
        assert daily_schedule(connection, monday) == []
        assert [c.course for c in daily_schedule(connection, date(2026, 9, 10))] == ["미적분"]
