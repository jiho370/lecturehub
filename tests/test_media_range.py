"""D-057: a recording that ran past the class is cut with one click; the original stays. Synthetic data only."""
import shutil
import subprocess
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from lecturehub.core import db
from lecturehub.core.ids import new_ulid
from lecturehub.index import media_range

KST = timezone(timedelta(hours=9))
MIN = 60_000
START = int(datetime(2026, 9, 29, 10, 5, 4, tzinfo=KST).timestamp() * 1000)


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "학기 (합성)"
    (root / "_lecturehub" / "recordings").mkdir(parents=True)
    master = root / "_lecturehub" / "recordings" / "master_합성.m4a"
    master.write_bytes(b"synthetic master audio")
    path = root / "_lecturehub" / "lecturehub.sqlite"
    db.init_db(path)
    with closing(db.connect(path)) as connection:
        connection.execute("INSERT INTO Course VALUES ('su','SU','구조역학','su','26-2')")
        connection.execute("INSERT INTO Schedule (course_id,weekday,start,end,id) VALUES ('su',1,?,?,?)",
                           (10 * 3_600_000, 11 * 3_600_000 + 50 * MIN, new_ulid()))
        connection.execute("INSERT INTO Lecture VALUES ('l','su','2026-09-29',1,NULL,NULL)")
        connection.execute(
            "INSERT INTO MediaAsset VALUES ('m','l',?, 'mh', ?, 'aac', 48000, 1, 9, 'app_mic', 'laptop', 1, NULL, NULL, "
            "?, 0, '{}', 'metadata', 'medium')", (str(master), 101 * MIN, START))
        connection.execute("INSERT INTO Transcript (id,lecture_id,media_id,engine,role,role_decided_by,status,source_file,"
                           "sha256) VALUES ('t','l','m','gemini_transcribe','timeline','policy','done','t.txt','t')")
        texts = [(minute * MIN, " ".join(f"합성{minute}낱말{i}" for i in range(30))) for minute in range(65)]
        texts += [(67 * MIN, "아"), (83 * MIN, "음. " * 1200)]
        for seq, (start, text) in enumerate(texts):
            connection.execute("INSERT INTO TranscriptSegment (transcript_id,seq,start_ms,text,chunk_index) "
                               "VALUES ('t',?,?,?,0)", (seq, start, text))
        connection.commit()
        yield root, connection, master


def fake_cutter(source, target, start_ms, end_ms):
    target.write_bytes(source.read_bytes() + f"[{start_ms}-{end_ms}]".encode())


def test_D057_suggests_the_end_from_speech_ignoring_repetition(library):
    _root, connection, _master = library
    s = media_range.suggest(connection, "m")
    assert s["speech_end_ms"] == 65 * MIN and s["end_ms"] == 67 * MIN  # "음. 음." at 83 min is not speech
    assert s["class_end_ms"] == (104 * 60 + 56) * 1000  # 11:50 timetable end − 10:05:04 recording start
    assert s["ask"] is True and "말소리" in s["reason"]


def test_D057_cut_keeps_the_original_and_records_a_derived_clip(library):
    root, connection, master = library
    before = master.read_bytes()
    out = media_range.cut(connection, root, "m", 0, 67 * MIN, cutter=fake_cutter)
    connection.commit()
    assert master.read_bytes() == before  # the original is never touched
    row = connection.execute("SELECT is_master, derived_from, duration_ms, recorded_start, path FROM MediaAsset "
                             "WHERE id = ?", (out["derived_id"],)).fetchone()
    assert tuple(row[:4]) == (0, "m", 67 * MIN, START) and Path(row[4]).parent.name == "derived"
    decided = media_range.decided_range(connection, "m")
    assert decided == {"start_ms": 0, "end_ms": 67 * MIN, "media_id_to_use": out["derived_id"], "cut": True}
    assert media_range.suggest(connection, "m")["ask"] is False
    assert media_range.in_range(connection, "t") == (0, 67 * MIN)
    assert connection.execute("SELECT count(*) FROM TranscriptSegment WHERE transcript_id='t'").fetchone()[0] == 67


def test_D057_lecture_view_search_and_note_input_leave_out_the_tail(library):
    from lecturehub.study import note_job
    from lecturehub.ui.library import Library

    root, connection, _master = library
    media_range.cut(connection, root, "m", 0, 67 * MIN, cutter=fake_cutter)
    connection.commit()
    lecture = Library(root).lecture("l")["lecture"]
    assert lecture["hidden_segments"] == 2 and all(s["start_ms"] < 67 * MIN for s in lecture["segments"])
    assert lecture["range"]["decided"]["cut"] is True
    assert [r for r in Library(root).search("음. 음.")["results"] if r["kind"] == "transcript"] == []
    data = note_job.build_input(connection, "l")
    assert max(s["start_ms"] for s in data["segments"]) < 67 * MIN


def test_D057_keep_whole_and_revert_are_recorded_decisions(library):
    root, connection, _master = library
    media_range.keep_whole(connection, "m")
    connection.commit()
    assert media_range.decided_range(connection, "m")["cut"] is False
    assert media_range.suggest(connection, "m")["ask"] is False and media_range.in_range(connection, "t") is None
    media_range.cut(connection, root, "m", 0, 67 * MIN, cutter=fake_cutter)
    media_range.revert(connection, "m")
    connection.commit()
    assert media_range.decided_range(connection, "m") is None and media_range.suggest(connection, "m")["ask"] is True
    kinds = [r[0] for r in connection.execute("SELECT kind FROM Evidence WHERE subject_id='m' ORDER BY rowid")]
    assert kinds == ["lecture_range_full", "lecture_range", "lecture_range_revoked"]


def test_D057_rejects_ranges_outside_the_recording(library):
    root, connection, _master = library
    for start, end in ((0, 102 * MIN), (5 * MIN, 5 * MIN + 30_000), (-1, 10 * MIN)):
        with pytest.raises(ValueError):
            media_range.cut(connection, root, "m", start, end, cutter=fake_cutter)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_D057_real_ffmpeg_copy_cut(tmp_path):
    source, target = tmp_path / "합성 원본.m4a", tmp_path / "잘림.m4a"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=150", "-c:a", "aac",
                    str(source)], check=True)
    media_range._ffmpeg_cut(source, target, 0, 90_000)
    from lecturehub.transcription.audio import duration_ms

    assert abs(duration_ms(target) - 90_000) < 1_500 and duration_ms(source) > 149_000


def test_D057_plan_for_manual_cut_uses_auto_stop_point_and_pauses(library):
    """User 2026-09-30: [자르기] on any recording; a recording stopped by itself after a long silence gets its
    end suggested from where the sound stopped, before any transcript exists; pauses map audio time to the clock."""
    root, connection, _master = library
    session = root / "_lecturehub" / "recordings" / "20260929_100504_mic"
    session.mkdir()
    master = session / "master_합성2.m4a"
    master.write_bytes(b"synthetic master audio 2")
    connection.execute(
        "INSERT INTO MediaAsset VALUES ('n','l',?, 'nh', ?, 'aac', 48000, 1, 9, 'app_mic', 'laptop', 1, NULL, NULL, "
        "?, 0, '{}', 'metadata', 'medium')", (str(master), 101 * MIN, START))
    (master.parent / "auto_stop.json").write_text('{"reason": "합성", "quiet_since_ms": 4800000}', encoding="utf-8")
    (master.parent / "pauses.json").write_text(
        '{"pauses": [{"paused_at": 1000, "resumed_at": 601000, "audio_ms": 1200000}, '
        '{"paused_at": 5, "resumed_at": null, "audio_ms": 9}]}', encoding="utf-8")
    plan = media_range.plan(connection, "n")
    assert plan["end_ms"] == 4800000 + 2 * MIN and "자동 정지" in plan["reason"] and plan["start_ms"] == 0
    assert plan["pauses"] == [{"at_ms": 1200000, "paused_ms": 600000}]  # the open pause is left out
    assert media_range.suggest(connection, "n")["ask"] is True  # 101 min − 82 min ≥ 15 min tail
    media_range.cut(connection, root, "n", 5 * MIN, 60 * MIN, cutter=fake_cutter)
    again = media_range.plan(connection, "n")
    assert (again["start_ms"], again["end_ms"], again["reason"]) == (5 * MIN, 60 * MIN, None)  # the decided range


def _session_recording(library, media_id="n", minutes=101):
    """A second recording of lecture 'l' whose master sits in a recorder session folder; it has no transcript."""
    root, connection, _master = library
    session = root / "_lecturehub" / "recordings" / "20260929_100504_mic"
    session.mkdir()
    master = session / "master_합성2.m4a"
    master.write_bytes(b"synthetic master audio 2")
    connection.execute(
        "INSERT INTO MediaAsset VALUES (?,'l',?, 'nh', ?, 'aac', 48000, 1, 9, 'app_mic', 'laptop', 1, NULL, NULL, "
        "?, 0, '{}', 'metadata', 'medium')", (media_id, str(master), minutes * MIN, START))
    return session


def _write(path, payload):
    import json

    path.write_text(json.dumps(payload), encoding="utf-8")


def test_a_gap_shifts_the_clock_exactly_as_a_pause_does(library, monkeypatch):
    """Release review 2026-11-16: the device was lost for 8 minutes at audio minute 20 (gaps.json as the
    recorder writes it) and the user paused for 5 minutes at audio minute 50. Both are wall-clock time that is
    not in the audio: the [자르기] label needs both, and the timetable end lies 13 minutes earlier in the audio."""
    from lecturehub.recorder.session import RecordingSession

    _root, connection, _master = library
    session = _session_recording(library)
    recorder = RecordingSession.__new__(RecordingSession)  # only what note_gap uses
    recorder.gaps, recorder.session_dir, recorder.writer = [], session, None
    monkeypatch.setattr("lecturehub.recorder.session.now_ms", lambda: START + 28 * MIN)  # when the sound came back
    gap = recorder.note_gap(8 * MIN, "source_lost", audio_ms=20 * MIN)
    assert gap["open"] is False and (session / "gaps.json").is_file()
    _write(session / "pauses.json", {"pauses": [
        {"paused_at": START + 58 * MIN, "resumed_at": START + 63 * MIN, "audio_ms": 50 * MIN}]})
    holes = [{"at_ms": 20 * MIN, "paused_ms": 8 * MIN}, {"at_ms": 50 * MIN, "paused_ms": 5 * MIN}]
    plan = media_range.plan(connection, "n")
    assert plan["pauses"] == holes

    def label_shift(ms):  # app.js trimRecording: the clock label of an audio position adds this
        return sum(x["paused_ms"] for x in plan["pauses"] if x["at_ms"] <= ms)

    assert (label_shift(10 * MIN), label_shift(30 * MIN), label_shift(60 * MIN)) == (0, 8 * MIN, 13 * MIN)

    s = media_range.suggest(connection, "n")
    assert "시간표" in s["reason"] and s["pauses"] == holes
    assert s["class_end_ms"] == (104 * 60 + 56) * 1000  # the timetable end on the wall clock: unchanged
    assert s["end_ms"] == (104 * 60 + 56) * 1000 - 13 * MIN + 2 * MIN  # where that moment is in the audio, + 2 min
    assert plan["end_ms"] == s["end_ms"]


def test_wall_clock_to_audio_position_around_pauses_and_gaps():
    holes = [{"at_ms": 20 * MIN, "paused_ms": 8 * MIN}, {"at_ms": 50 * MIN, "paused_ms": 5 * MIN}]
    position = media_range.audio_position
    assert position(10 * MIN, holes) == 10 * MIN        # before anything was missing
    assert position(20 * MIN, holes) == 20 * MIN        # the moment the device was lost
    assert position(24 * MIN, holes) == 20 * MIN        # inside the gap: nothing was recorded then
    assert position(28 * MIN, holes) == 20 * MIN        # the moment the sound came back
    assert position(40 * MIN, holes) == 32 * MIN
    assert position(60 * MIN, holes) == 50 * MIN        # inside the pause (wall 58..63)
    assert position(100 * MIN, holes) == 87 * MIN
    assert position(100 * MIN, []) == 100 * MIN


def test_a_class_that_ended_while_the_device_was_lost_is_cut_where_the_sound_stopped(library):
    _root, connection, _master = library
    session = _session_recording(library)
    # no gaps.json (a session folder that lost it): the list in stop.json is used
    _write(session / "stop.json", {"cause": "user_stop", "gaps": [
        {"at": START + 120 * MIN, "audio_ms": 90 * MIN, "lost_ms": 30 * MIN, "reason": "no_data", "open": False}]})
    assert media_range.pauses(connection, "n") == [{"at_ms": 90 * MIN, "paused_ms": 30 * MIN}]
    s = media_range.suggest(connection, "n")  # the class ended at wall 104:56, inside the gap (wall 90..120)
    assert s["end_ms"] == 90 * MIN + 2 * MIN


def test_a_device_lost_during_a_pause_is_not_counted_twice(library):
    """Paused from wall minute 10 to 20 at audio minute 10; the device died at wall minute 15 and delivered
    again at minute 25. The audio stood still from minute 10 to 25: fifteen minutes, not twenty."""
    _root, connection, _master = library
    session = _session_recording(library)
    _write(session / "pauses.json", {"pauses": [
        {"paused_at": START + 10 * MIN, "resumed_at": START + 20 * MIN, "audio_ms": 10 * MIN}]})
    _write(session / "gaps.json", {"gaps": [
        {"at": START + 25 * MIN, "audio_ms": 10 * MIN, "lost_ms": 10 * MIN, "reason": "no_data", "open": False},
        {"at": START + 70 * MIN, "audio_ms": 55 * MIN, "lost_ms": 2 * MIN, "reason": "source_lost", "open": False}]})
    assert media_range.pauses(connection, "n") == [{"at_ms": 10 * MIN, "paused_ms": 15 * MIN},
                                                   {"at_ms": 55 * MIN, "paused_ms": 2 * MIN}]


def test_unreadable_pause_and_gap_entries_are_left_out_not_fatal(library):
    _root, connection, _master = library
    session = _session_recording(library)
    _write(session / "pauses.json", ["not", "an", "object"])
    _write(session / "gaps.json", {"gaps": [
        "junk", {"at": "x", "audio_ms": 1, "lost_ms": 5}, {"at": 5, "audio_ms": None, "lost_ms": 3},
        {"at": START + 9 * MIN, "audio_ms": 7 * MIN, "lost_ms": 0, "reason": "no_data"},  # nothing lost: no shift
        {"at": START + 30 * MIN, "audio_ms": 20 * MIN, "lost_ms": 4 * MIN, "reason": "no_data"}]})
    plan = media_range.plan(connection, "n")
    assert plan["pauses"] == [{"at_ms": 20 * MIN, "paused_ms": 4 * MIN}]


@pytest.mark.parametrize("zone", ["UTC", "America/Los_Angeles", "Asia/Seoul"])
def test_TZ_001_class_end_is_kst_whatever_the_pc_zone(library, monkeypatch, zone):
    """TZ-001: the timetable is Korean time; the test machine's TZ must not move the class end."""
    import time as _time

    if not hasattr(_time, "tzset"):
        pytest.skip("time.tzset is POSIX only")
    monkeypatch.setenv("TZ", zone)
    _time.tzset()
    try:
        assert media_range.suggest(library[1], "m")["class_end_ms"] == (104 * 60 + 56) * 1000
    finally:
        monkeypatch.undo()
        _time.tzset()
