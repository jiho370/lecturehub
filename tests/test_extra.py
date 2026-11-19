"""'수업 외' topics (no timetable) and moving a recording to another lecture. Synthetic data only."""
import json
import shutil
from contextlib import closing

import pytest

from lecturehub.core import db, extra_courses
from lecturehub.index import app_ingest, archive_copy, relink
from lecturehub.index.media import MediaMetadata


@pytest.fixture
def library(tmp_path, monkeypatch):
    root = tmp_path / "학기 (합성)"
    (root / "_lecturehub").mkdir(parents=True)
    monkeypatch.setattr("lecturehub.index.app_ingest.probe_media", lambda path, ffprobe=None: MediaMetadata(
        30 * 60_000, "mp3", 48000, 1))
    path = root / "_lecturehub" / "lecturehub.sqlite"
    db.init_db(path)
    with closing(db.connect(path)) as c:
        c.execute("INSERT INTO Course VALUES ('rw','RW','토질역학','rw','26-2')")
        c.execute("INSERT INTO Schedule (course_id,weekday,start,end,id) VALUES ('rw',1,?,?,'s1')",
                  (14 * 3_600_000, 17 * 3_600_000 + 50 * 60_000))
        c.commit()
        yield root, c


def session(root, name, course, source="user_pick", mode="loopback", started="2026-09-29T15:34:47+09:00"):
    folder = root / "_lecturehub" / "recordings" / name
    folder.mkdir(parents=True)
    (folder / "master_합성.mp3").write_bytes(f"synthetic {name}".encode())
    (folder / "session.json").write_text(json.dumps({"course": course, "course_source": source, "mode": mode,
                                                     "started_local": started, "started_at": 1790664887000}),
                                         encoding="utf-8")
    (folder / "stop.json").write_text(json.dumps({"cause": "user_stop"}), encoding="utf-8")
    (folder / "finalize_1.json").write_text(json.dumps({"master": str(folder / "master_합성.mp3")}), encoding="utf-8")
    return folder


def test_extra_names_are_validated_and_kept_outside_the_db(library):
    root, _c = library
    assert extra_courses.add(root, "  AI   외부 강의 ", ["토질역학"]) == ["AI 외부 강의"]
    assert extra_courses.add(root, "AI 외부 강의") == ["AI 외부 강의"]  # idempotent
    for bad in ("", "a/b", "토질역학", "수업 외", "x" * 41):
        with pytest.raises(ValueError):
            extra_courses.add(root, bad, ["토질역학"])
    assert (root / "_lecturehub" / "extra_courses.json").is_file()


def test_extra_recording_ignores_the_timetable_and_gets_its_own_lecture(library):
    root, c = library
    extra_courses.add(root, "AI 외부 강의")
    first = app_ingest.link_recording(c, session(root, "20260929_153447_loopback", "AI 외부 강의"))
    second = app_ingest.link_recording(c, session(root, "20260929_155427_loopback", "AI 외부 강의"))
    c.commit()
    assert first["state"] == second["state"] == "linked" and first["lecture_id"] != second["lecture_id"]
    assert (first["confidence"], first["decided_by"]) == ("high", "metadata")
    course = c.execute("SELECT c.folder_name FROM Lecture l JOIN Course c ON c.id=l.course_id WHERE l.id=?",
                       (first["lecture_id"],)).fetchone()[0]
    assert course == "AI 외부 강의"  # not 토질역학, although 토질역학 is on the timetable at 15:34
    from lecturehub.index.extra import extra_course_ids

    assert len(extra_course_ids(c)) == 1


def test_extra_copy_goes_under_the_outside_class_group(library):
    root, c = library
    extra_courses.add(root, "AI 외부 강의")
    linked = app_ingest.link_recording(c, session(root, "20260929_153447_loopback", "AI 외부 강의"))
    out = archive_copy.copy_media(c, root, linked["media_id"])
    assert out["state"] == "copied"
    assert out["path"].endswith(str(root / "녹음본" / "수업 외" / "AI 외부 강의" / "26.9.29 AI 외부 강의 (PC 소리).mp3")[-30:])


def test_move_a_wrongly_linked_recording_to_an_outside_topic(library):
    root, c = library
    linked = app_ingest.link_recording(c, session(root, "20260929_153447_loopback", "", source="unknown"))
    assert linked["course"] == "토질역학"  # the timetable guess
    wrong = archive_copy.copy_media(c, root, linked["media_id"])["path"]
    c.commit()
    extra_courses.add(root, "AI 외부 강의")
    bin_dir = root.parent / "bin"
    bin_dir.mkdir()
    moved = relink.move(c, root, linked["media_id"], "AI 외부 강의", "2026-09-29",
                        trash=lambda paths: [shutil.move(str(p), bin_dir / p.name) for p in paths])
    again = archive_copy.copy_media(c, root, linked["media_id"])
    c.commit()
    assert moved["moved"] and moved["recycled_copy"] == wrong and (bin_dir / "26.9.29 토질역학 (PC 소리).mp3").exists()
    assert moved["copy_left"] is None and moved["copy_note"] is None
    assert "수업 외" in again["path"] and again["state"] == "copied"
    kinds = [r[0] for r in c.execute("SELECT kind FROM Evidence WHERE subject_id=? ORDER BY rowid", (linked["media_id"],))]
    assert "user_relink" in kinds


def _wrongly_linked_with_copy(root, c):
    linked = app_ingest.link_recording(c, session(root, "20260929_153447_loopback", "", source="unknown"))
    wrong = archive_copy.copy_media(c, root, linked["media_id"])["path"]
    c.commit()
    extra_courses.add(root, "AI 외부 강의")
    return linked, wrong


def _relink_record(c, media_id):
    detail = c.execute("SELECT detail FROM Evidence WHERE subject_type='MediaAsset' AND subject_id=? "
                       "AND kind='user_relink'", (media_id,)).fetchone()
    return json.loads(detail[0]) if detail else None


def test_on_a_drive_without_a_recycle_bin_the_link_moves_and_the_old_copy_stays(library, monkeypatch):
    """Release review 2026-11-16: on a USB or network drive the whole move used to fail and roll back."""
    from pathlib import Path

    from lecturehub.core import recycle_bin

    root, c = library
    linked, wrong = _wrongly_linked_with_copy(root, c)
    monkeypatch.setattr(recycle_bin, "has_recycle_bin", lambda path, drive_type=None: False)
    with c:  # what Library.relink does; the real to_recycle_bin refuses before it touches anything
        moved = relink.move(c, root, linked["media_id"], "AI 외부 강의", "2026-09-29")
    assert moved["moved"] and moved["lecture_id"] != linked["lecture_id"]
    assert c.execute("SELECT lecture_id FROM MediaAsset WHERE id=?", (linked["media_id"],)).fetchone()[0] == moved["lecture_id"]
    assert Path(wrong).is_file()  # nothing was deleted
    assert moved["recycled_copy"] is None and moved["copy_left"] == wrong and "휴지통이 없어" in moved["copy_note"]
    record = _relink_record(c, linked["media_id"])
    assert record["recycled_copy"] is None and record["copy_left"] == wrong and "휴지통이 없어" in record["copy_note"]
    # the copy that stayed is still this recording's registered copy: no second one is made behind the user's back
    assert archive_copy.copy_media(c, root, linked["media_id"]) == {"state": "already", "path": wrong}


def test_a_copy_that_cannot_be_recycled_for_another_reason_does_not_undo_the_move(library):
    from pathlib import Path

    root, c = library
    linked, wrong = _wrongly_linked_with_copy(root, c)

    def in_use(paths):
        raise OSError("휴지통으로 옮기지 못했습니다(코드 32)")

    moved = relink.move(c, root, linked["media_id"], "AI 외부 강의", "2026-09-29", trash=in_use)
    assert moved["moved"] and Path(wrong).is_file()
    assert (moved["recycled_copy"], moved["copy_left"], moved["copy_note"]) == (None, wrong, "휴지통으로 옮기지 못했습니다(코드 32)")


def test_a_copy_the_user_changed_is_left_and_the_result_says_so(library):
    from pathlib import Path

    root, c = library
    linked, wrong = _wrongly_linked_with_copy(root, c)
    Path(wrong).write_bytes(b"edited by the user after the copy (synthetic)")
    asked = []
    moved = relink.move(c, root, linked["media_id"], "AI 외부 강의", "2026-09-29", trash=asked.append)
    assert moved["moved"] and asked == [] and Path(wrong).read_bytes().startswith(b"edited")
    assert moved["recycled_copy"] is None and moved["copy_left"] == wrong and "그대로 두었습니다" in moved["copy_note"]


def test_a_copy_that_vanished_while_recycling_is_reported_as_not_left(library):
    """Recycling failed and the file is no longer in its place: the result must not claim it is still there."""
    from pathlib import Path

    root, c = library
    linked, wrong = _wrongly_linked_with_copy(root, c)

    def gone(paths):
        for path in paths:
            path.rename(root.parent / "moved away (synthetic)")
        raise OSError("휴지통에서 찾을 수 없습니다(영구 삭제되었을 수 있음): 합성")

    moved = relink.move(c, root, linked["media_id"], "AI 외부 강의", "2026-09-29", trash=gone)
    assert moved["moved"] and not Path(wrong).exists()
    assert moved["recycled_copy"] is None and moved["copy_left"] is None and "찾을 수 없습니다" in moved["copy_note"]


def transcribed(root, c):
    linked = app_ingest.link_recording(c, session(root, "20260929_155427_loopback", "", source="unknown",
                                                  started="2026-09-29T15:54:27+09:00"))
    c.execute("INSERT INTO Transcript (id,lecture_id,media_id,engine,role,role_decided_by,status,source_file,sha256) "
              "VALUES ('t',?,?,'gemini_transcribe','timeline','policy','done','t','t')",
              (linked["lecture_id"], linked["media_id"]))
    c.execute("INSERT INTO TranscriptSegment (transcript_id,seq,start_ms,end_ms,text,chunk_index) "
              "VALUES ('t',0,0,1000,'합성 문장',0)")
    from lecturehub.index.lectures import refresh_policy

    refresh_policy(c, linked["lecture_id"])
    extra_courses.add(root, "AI 외부 강의")
    return linked


def test_a_transcribed_recording_asks_first_and_changes_nothing(library):
    root, c = library
    linked = transcribed(root, c)
    out = relink.move(c, root, linked["media_id"], "AI 외부 강의", "2026-09-29", trash=lambda p: None)
    assert out == {"lecture_id": linked["lecture_id"], "moved": False, "confirm": {"transcripts": 1}}
    assert c.execute("SELECT lecture_id FROM MediaAsset WHERE id=?", (linked["media_id"],)).fetchone()[0] == linked["lecture_id"]
    assert c.execute("SELECT count(*) FROM Lecture").fetchone()[0] == 1  # no topic lecture made yet


def test_with_the_users_yes_the_transcript_follows_its_recording(library):
    from lecturehub.index.lectures import in_use

    root, c = library
    linked = transcribed(root, c)
    moved = relink.move(c, root, linked["media_id"], "AI 외부 강의", "2026-09-29", with_transcripts=True,
                        trash=lambda p: None)
    c.commit()
    assert moved["moved"] and moved["transcripts"] == 1
    assert c.execute("SELECT lecture_id FROM Transcript WHERE id='t'").fetchone()[0] == moved["lecture_id"]
    assert in_use(c, moved["lecture_id"])["transcript_id"] == "t"
    assert in_use(c, linked["lecture_id"]) is None  # the old lecture no longer shows it
    evidence = c.execute("SELECT detail, source FROM Evidence WHERE subject_type='Transcript' AND subject_id='t' "
                         "AND kind='user_relink'").fetchone()
    assert json.loads(evidence[0])["from"] == linked["lecture_id"] and evidence[1] == "user"


def test_a_transcripts_lecture_cannot_change_without_the_users_record(library):
    import sqlite3

    root, c = library
    linked = transcribed(root, c)
    c.execute("INSERT INTO Lecture (id, course_id, date, session_index) VALUES ('other','rw','2026-09-30',9)")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        c.execute("UPDATE Transcript SET lecture_id='other' WHERE id='t'")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        c.execute("UPDATE Transcript SET sha256='x' WHERE id='t'")
    assert c.execute("SELECT lecture_id FROM Transcript WHERE id='t'").fetchone()[0] == linked["lecture_id"]


def test_move_refuses_when_something_was_built_on_the_transcript(library):
    root, c = library
    linked = transcribed(root, c)
    c.execute("INSERT INTO Correction (id,lecture_id,anchor,before,after,kind,status,decided_by,created_at) "
              "VALUES ('k',?,?,'합성','합성2','term','active','user',0)",
              (linked["lecture_id"], json.dumps({"transcript_id": "t", "seg_seq": 0})))
    with pytest.raises(ValueError, match="교정 1건"):
        relink.move(c, root, linked["media_id"], "AI 외부 강의", "2026-09-29", with_transcripts=True,
                    trash=lambda p: None)


def test_mic_and_pc_halves_of_one_recording_share_one_lecture(library):
    """User 2026-09-29: a mic + PC-sound recording showed up as two lectures (5강, 6강) — pair.json joins them."""
    root, c = library
    extra_courses.add(root, "AI 외부 강의")
    mic = session(root, "20260929_194942_mic", "AI 외부 강의", mode="mic")
    pc = session(root, "20260929_194942_loopback", "AI 외부 강의")
    pair = json.dumps({"mic": str(mic), "pc": str(pc)}, ensure_ascii=False)
    for folder in (mic, pc):
        (folder / "pair.json").write_text(pair, encoding="utf-8")
    first = app_ingest.link_recording(c, pc)  # either half may finish first
    second = app_ingest.link_recording(c, mic)
    alone = app_ingest.link_recording(c, session(root, "20260929_200000_loopback", "AI 외부 강의"))
    c.commit()
    assert first["lecture_id"] == second["lecture_id"] != alone["lecture_id"]
    assert c.execute("SELECT count(*) FROM Lecture").fetchone()[0] == 2


def test_a_recording_without_a_course_outside_class_hours_opens_under_unsorted(library):
    root, c = library
    night = "2026-10-03T19:29:19+09:00"  # a Saturday evening: no class in the timetable
    first = app_ingest.link_recording(c, session(root, "20261003_192919_loopback", "", "unknown", started=night))
    marker = app_ingest.link_recording(c, session(root, "20261003_213000_loopback", chr(0xFFFD) + "none",
                                                  started="2026-10-03T21:30:00+09:00"))
    c.commit()
    assert first["state"] == marker["state"] == "linked" and first["lecture_id"] != marker["lecture_id"]
    assert first["course"] == marker["course"] == extra_courses.UNSORTED
    assert (first["confidence"], first["decided_by"]) == ("low", "policy")
    assert extra_courses.is_extra(root, extra_courses.UNSORTED) and extra_courses.names(root) == []
    with pytest.raises(ValueError):
        extra_courses.add(root, extra_courses.UNSORTED)
    # during class hours a blank course is still the timetable's guess, and a wrong pick still waits for the user
    guessed = app_ingest.link_recording(c, session(root, "20260929_153447_loopback", "", "unknown"))
    assert guessed["course"] == "토질역학" and guessed["confidence"] == "medium"
    waiting = app_ingest.link_recording(c, session(root, "20261003_220000_loopback", "토질역학",
                                                   started="2026-10-03T22:00:00+09:00"))
    assert waiting["state"] == "pending"
    moved = app_ingest.confirm_pending(c, waiting["ingest_id"], extra_courses.UNSORTED)
    assert moved["state"] == "linked" and moved["course"] == extra_courses.UNSORTED


@pytest.mark.parametrize("build", ["full", "public"])
def test_unsorted_is_never_a_consent_unit(tmp_path, monkeypatch, build):
    """CONSENT-002: full standing consent never covers unsorted; public standing consent is refused."""
    monkeypatch.setenv("LECTUREHUB_EDITION", build)
    from lecturehub.transcription.consent import CONFIRMED, ConsentStore, named

    store = ConsentStore(tmp_path / "consent.json")
    if build == "public":
        with pytest.raises(ValueError):
            store.grant_all("whisper_groq", 1, "user", "합성")
        assert not store.granted("whisper_groq", "토질역학")
        store.grant("whisper_groq", "토질역학", 1, "합성")
    else:
        store.grant_all("whisper_groq", 1, "user", "합성")
    assert store.granted("whisper_groq", "토질역학")
    for course in ("", "  ", extra_courses.UNSORTED, chr(0xFFFD) + "none", chr(0) + "none"):
        assert not named(course) and not store.granted("whisper_groq", course)
        with pytest.raises(ValueError):
            store.grant("whisper_groq", course, 1, "합성", authority=CONFIRMED)
