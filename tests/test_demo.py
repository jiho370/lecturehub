"""D-085 example semester: invented source files and real extraction / evidence validation."""
import shutil
from contextlib import closing
from datetime import datetime, timedelta, timezone

import pytest

from lecturehub.core import db
from lecturehub.index import demo

pytestmark = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg required")


def test_trial_semester_has_a_recording_transcript_slides_and_a_checked_note(tmp_path):
    dest = tmp_path / "예시 학기"
    result = demo.make(dest, now=datetime(2026, 11, 13, 14, 0).astimezone())
    assert result["state"] == "made" and result["rejected"] == 0
    note = open(result["note"], encoding="utf-8").read()
    assert "유출계수는 항상 2" not in note and "16.68 m³/s" in note and '<li id="fn' in note
    with closing(db.connect(dest / "_lecturehub" / "lecturehub.sqlite")) as c:
        from lecturehub.index import lectures

        assert f"수문학 {lectures.label(c, result['lecture_id'])} 정리본" in note
        assert c.execute("SELECT count(*) FROM MediaAsset WHERE origin='app_mic'").fetchone()[0] >= 60
        assert c.execute("SELECT count(*) FROM Lecture WHERE id NOT IN (SELECT lecture_id FROM MediaAsset)").fetchone()[0] == 3
        assert c.execute("SELECT min(duration_ms) FROM MediaAsset").fetchone()[0] == 3000000
        assert c.execute("SELECT count(*) FROM Transcript").fetchone()[0] == 3
        assert c.execute("SELECT count(*) FROM Material").fetchone()[0] == 4
        assert c.execute("SELECT count(*) FROM StudyArtifact WHERE kind='note'").fetchone()[0] == 2
    assert demo.make(dest)["state"] == "ready"  # second run reuses it


def test_trial_writes_a_recorder_config_and_leaving_it_replaces_that_config(tmp_path, monkeypatch):
    from lecturehub.core import settings
    from lecturehub.ui import setup
    from lecturehub.ui.library import Library

    monkeypatch.setenv("LECTUREHUB_SETTINGS", str(tmp_path / "app" / "settings.json"))
    monkeypatch.setenv("LECTUREHUB_RECORDER_CONFIG", str(tmp_path / "app" / "recorder.toml"))
    monkeypatch.delenv("LECTUREHUB_ROOT", raising=False)
    made = Library.make_trial()
    assert made["ok"] and settings.get("trial")
    config = (tmp_path / "app" / "recorder.toml").read_text(encoding="utf-8")
    assert "recordings" in config and str(made["root"]).replace("\\", "/") in config.replace("\\\\", "/").replace("\\", "/")
    real = tmp_path / "real"
    real.mkdir()
    done = setup.apply(str(real), "26-2", [{"name": "토질역학", "slots": [{"day": "월", "start": "09:00", "end": "10:15"}]}])
    assert done["ok"] and not settings.get("trial")
    config = (tmp_path / "app" / "recorder.toml").read_text(encoding="utf-8")
    assert "예시" not in config and str(real / "26-2").replace("\\", "/") in config.replace("\\\\", "/").replace("\\", "/")


def test_trial_110_showcase_has_quotes_submissions_week_and_real_undo(tmp_path):
    """D-077, D-079, WEEK-001: synthetic examples feed the real read models."""
    from lecturehub.index import etl_submissions
    from lecturehub.mcp import changes
    from lecturehub.ui.library import Library

    now = datetime(2026, 11, 26, 10, 0).astimezone()
    dest = tmp_path / "한글 공백 (예시)"
    demo.make(dest, now=now)
    lib = Library(dest)
    week = lib.week_view(now)
    classes = [c for d in week["days"] for c in d["classes"]]
    assert {"휴강", "보강"} <= {c["kind"] for c in classes}
    assert len({c["course"] for c in classes}) == 3
    today = lib.today_briefing(now)
    assert today["ok"]
    calendar = lib.calendar(now.year, now.month, now.date())
    assert len(calendar["submitted"]) == 2
    assert len(calendar["candidates"]) >= 2
    assert {c["source"] for c in calendar["candidates"]} == {"transcript", "etl"}
    assert all(c["evidence"]["quote"] for c in calendar["candidates"])
    items = etl_submissions.items(dest)
    assert sum(etl_submissions.state(i["submission"]) == "submitted" for i in items) == 2
    assert datetime.fromisoformat(items[0]["due"]) - now.replace(tzinfo=None) == timedelta(hours=13, minutes=59)
    rows = changes.recent(dest)
    assert len(rows) == 4 and all(r["undoable"] for r in rows)
    assert changes.undo(dest, rows[0]["id"])["ok"]
    assert changes.recent(dest)[0]["undone"]
    assert demo.make(dest, now=now)["state"] == "ready"
    assert len(changes.recent(dest)) == 4
    # D-085: relative dates resolve from the dated lecture, including "그 주 금요일".
    quiz = next(c for c in calendar["candidates"] if c["kind"] == "quiz")
    moved = next(c for c in calendar["candidates"] if c["kind"] == "closure")
    assert quiz["date"] == "2026-12-02" and moved["date"] == "2026-12-04"
    assert moved["evidence"]["at_ms"] == 2060000
    assert any(c.get("room") == "301호" for c in classes if c["kind"] == "보강")
    assert {r["tool"] for r in rows} == {"add_calendar_event", "save_note", "add_schedule_change", "request_notice_rescan"}
    with closing(db.connect(dest / "_lecturehub" / "lecturehub.sqlite")) as c:
        assert {r[0] for r in c.execute("SELECT folder_name FROM Course")} == set(demo.COURSES)
        assert c.execute("SELECT count(*) FROM Lecture").fetchone()[0] >= 60
        assert c.execute("SELECT count(*) FROM Lecture l JOIN Course c ON l.course_id=c.id "
                         "WHERE c.folder_name='토질역학' AND l.date='2026-11-26'").fetchone()[0] == 0
        assert all(20 <= r[0] <= 40 for r in c.execute("SELECT count(*) FROM TranscriptSegment GROUP BY transcript_id"))
        assert c.execute("SELECT count(*) FROM MaterialPage WHERE length(text)>0").fetchone()[0] >= 10
        text = " ".join(r[0] for r in c.execute("SELECT text FROM TranscriptSegment UNION ALL SELECT text FROM MaterialPage"))
        assert not any(word in text for word in ("체험", "합성", "샘플", "테스트", "교수님 성함"))
        assert all(word in text for word in ("유출계수", "도달시간", "단위도", "투수계수", "퀴즈"))

@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="the example recordings need ffmpeg")
@pytest.mark.parametrize("now", [
    datetime(2026, 11, 23, 0, 30),   # Monday night: no class of this week is over yet
    datetime(2026, 11, 24, 12, 30),  # Tuesday after 토질역학, before Wednesday's 수문학
    datetime(2026, 11, 25, 9, 30),   # Wednesday, in the middle of 수문학
    datetime(2026, 11, 27, 9, 0),    # Friday just before class
    datetime(2026, 11, 29, 23, 30),  # Sunday night
])
def test_example_semester_builds_on_any_day_of_the_week(tmp_path, now):
    """Early in a week the newest 수문학 lecture moved back a week onto the second one's date and the build failed
    with a duplicate recording (found by building at 128 clocks, 2026-11-26)."""
    for zone in (timezone.utc, timezone(timedelta(hours=9))):
        dest = tmp_path / zone.tzname(None).replace(":", "") / "예시 학기"
        made = demo.make(dest, now=now.replace(tzinfo=zone))
        assert made["state"] == "made" and made["lecture_id"]
        with closing(db.connect(dest / "_lecturehub" / "lecturehub.sqlite")) as connection:
            assert connection.execute("SELECT count(*) FROM StudyArtifact WHERE kind='note'").fetchone()[0] >= 2
            rows = connection.execute("SELECT sha256, count(*) FROM MediaAsset GROUP BY sha256 HAVING count(*) > 1").fetchall()
            assert rows == []


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="the example recordings need ffmpeg")
def test_example_semester_does_not_look_its_note_up_by_path(tmp_path, monkeypatch):
    """The built app's self-check failed on the build machine only: its temp folder is spelled with an 8.3 short name,
    so the path the note job returned never equalled the stored one (2026-11-26). The note is found by its lecture."""
    from lecturehub.study import note_job

    real = note_job.run

    def other_spelling(*args, **kwargs):
        result = real(*args, **kwargs)
        return {**result, "html": "X:\\SHORT~1\\" + str(result.get("html", ""))[3:]}

    monkeypatch.setattr(note_job, "run", other_spelling)
    made = demo.make(tmp_path / "예시 학기", now=datetime(2026, 11, 26, 14, 0).astimezone())
    assert made["state"] == "made"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="the example recordings need ffmpeg")
def test_example_timetable_uses_three_hour_blocks(tmp_path):
    """Three-hour classes (09:00–11:50): 75-minute ones left the week grid looking thin (user, 2026-11-26)."""
    from lecturehub.ui.library import Library

    now = datetime(2026, 11, 26, 14, 0).astimezone()
    dest = tmp_path / "예시 학기"
    demo.make(dest, now=now)
    lib = Library(dest)
    classes = [c for d in lib.week_view(now)["days"] for c in d["classes"]]
    assert classes and all(c["end_ms"] - c["start_ms"] == 170 * 60 * 1000 for c in classes)
    # the month calendar is not empty outside this week: midterms behind, a presentation, a report and finals ahead
    december = lib.calendar(2026, 12, now.date())["events"]
    assert sum(e["kind"] == "exam" for e in december) == 3 and {"presentation", "report"} <= {e["kind"] for e in december}
    assert sum(e["kind"] == "exam" for e in lib.calendar(2026, 10, now.date())["events"]) == 3
