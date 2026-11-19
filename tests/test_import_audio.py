"""Recordings made outside the app (phone recorder, KakaoTalk file) — P-024. Synthetic data only."""
import hashlib
import json
from contextlib import closing
from datetime import date, datetime, time

import pytest

from lecturehub.core import db, extra_courses
from lecturehub.index import discard, import_audio, lectures
from lecturehub.index.media import MediaMetadata

TODAY = date(2026, 10, 1)
MIN = 60_000
TUESDAY = "2026-09-29"


def _ms(text: str) -> int:
    return int(datetime.fromisoformat(text).astimezone().timestamp() * 1000)


def _probe(duration=75 * MIN, tag=None):
    return lambda path, ffprobe=None: MediaMetadata(duration, "aac", 44100, 1, tag)


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "학기 (합성)"
    (root / "_lecturehub").mkdir(parents=True)
    path = root / "_lecturehub" / "lecturehub.sqlite"
    db.init_db(path)
    phone = tmp_path / "카카오톡 받은 파일"
    phone.mkdir()
    with closing(db.connect(path)) as c:
        c.execute("INSERT INTO Course VALUES ('hy','HY','구조역학','hy','26-2')")
        c.execute("INSERT INTO Course VALUES ('hx','HX','구조역학실험','hx','26-2')")
        c.execute("INSERT INTO Schedule (course_id,weekday,start,end,id) VALUES ('hy',1,?,?,'s1')",
                  (10 * 60 * MIN + 30 * MIN, 11 * 60 * MIN + 45 * MIN))
        c.execute("INSERT INTO Schedule (course_id,weekday,start,end,id) VALUES ('hx',1,?,?,'s2')",
                  (14 * 60 * MIN, 16 * 60 * MIN))
        extra_courses.add(root, "AI 외부 강의")
        c.commit()
        yield root, c, phone


def _file(folder, name, data=b"synthetic phone recording"):
    path = folder / name
    path.write_bytes(data)
    return path


def test_IMPORT_001_file_name_dates(library):
    assert import_audio.name_stamp("음성 260929_102801.m4a", TODAY) == (date(2026, 9, 29), time(10, 28, 1))
    assert import_audio.name_stamp("20260929_102801.m4a", TODAY) == (date(2026, 9, 29), time(10, 28, 1))
    assert import_audio.name_stamp("KakaoTalk_Audio_2026-09-29-10-28-01.m4a", TODAY) == (date(2026, 9, 29), time(10, 28, 1))
    assert import_audio.name_stamp("26.9.29 구조역학.m4a", TODAY) == (date(2026, 9, 29), None)
    assert import_audio.name_stamp("9월 29일 수업.m4a", TODAY) == (date(2026, 9, 29), None)
    assert import_audio.name_stamp("새로운 녹음 12.m4a", TODAY) == (None, None)
    assert import_audio.name_stamp("v10.2.3 build 999999_999999.m4a", TODAY) == (None, None)  # not a date
    assert import_audio.name_stamp("20270101_090000.m4a", TODAY) == (None, None)  # the future is not a recording


def test_IMPORT_002_guess_is_a_suggestion_with_its_basis(library):
    root, c, _phone = library
    # the name carries the start: 10:28 runs into the 10:30 class
    g = import_audio.guess(c, root, "음성 260929_102801.m4a", tag_ms=None, mtime_ms=None, duration_ms=75 * MIN, today=TODAY)
    assert (g["date"], g["time"], g["basis"], g["role"]) == (TUESDAY, "10:28", "filename", "start")
    assert (g["course"], g["course_basis"], g["class"]) == ("구조역학", "timetable", "10:30–11:45")
    assert "추정" not in g["note"] and "파일 이름의 날짜·시각" in g["note"] and "구조역학 10:30–11:45" in g["note"]
    # a recorder that stamps the end: 11:47 as a start overlaps nothing, 11:47 − 75 min is the class
    g = import_audio.guess(c, root, "음성 260929_114700.m4a", tag_ms=None, mtime_ms=None, duration_ms=75 * MIN, today=TODAY)
    assert (g["time"], g["role"], g["course"]) == ("10:32", "end", "구조역학") and "앞당김" in g["note"]
    # only a tag inside the file
    g = import_audio.guess(c, root, "새로운 녹음 3.m4a", tag_ms=_ms("2026-09-29T14:02:00"), mtime_ms=_ms("2026-10-01T09:00:00"),
                           duration_ms=100 * MIN, today=TODAY)
    assert (g["date"], g["time"], g["basis"], g["course"]) == (TUESDAY, "14:02", "creation_time", "구조역학실험")
    # nothing but the file time (when it was received): the date is offered, no class is claimed
    g = import_audio.guess(c, root, "새로운 녹음 4.m4a", tag_ms=None, mtime_ms=_ms("2026-10-01T09:00:00"),
                           duration_ms=30 * MIN, today=TODAY)
    assert (g["date"], g["time"], g["basis"], g["role"], g["course"]) == ("2026-10-01", "08:30", "mtime", "end", None)
    # the user's own naming: the course in the name wins, the longer name when one contains the other
    g = import_audio.guess(c, root, "26.9.29 구조역학실험.m4a", tag_ms=None, mtime_ms=_ms("2026-10-01T09:00:00"),
                           duration_ms=30 * MIN, today=TODAY)
    assert (g["date"], g["time"], g["basis"], g["course"], g["course_basis"]) == (TUESDAY, None, "filename_date",
                                                                                 "구조역학실험", "filename")
    # no hint at all: everything stays blank
    g = import_audio.guess(c, root, "녹음.m4a", tag_ms=-2_082_844_800_000, mtime_ms=None, duration_ms=MIN, today=TODAY)  # 1904
    assert (g["date"], g["time"], g["course"], g["note"]) == (None, None, None, "")


def test_IMPORT_003_probe_says_why_a_file_cannot_come_in(library):
    root, c, phone = library
    ok = import_audio.probe(c, root, _file(phone, "음성 260929_102801.m4a"), today=TODAY, probe_media=_probe())
    assert ok["ok"] and ok["duration_ms"] == 75 * MIN and ok["kakao"] and not ok["convert"]
    assert ok["guess"]["course"] == "구조역학"
    assert import_audio.probe(c, root, _file(phone, "old.amr"), today=TODAY, probe_media=_probe())["convert"]
    assert "녹음 파일이 아닙니다" in import_audio.probe(c, root, _file(phone, "사진.jpg"), today=TODAY)["error"]
    assert "폴더" in import_audio.probe(c, root, phone, today=TODAY)["error"]
    assert "찾지 못" in import_audio.probe(c, root, phone / "없음.m4a", today=TODAY)["error"]
    inside = root / "_lecturehub" / "recordings" / "s"
    inside.mkdir(parents=True)
    assert "앱이 보관" in import_audio.probe(c, root, _file(inside, "master.m4a"), today=TODAY, probe_media=_probe())["error"]


def test_IMPORT_003_broken_file_reason_is_plain_korean(library):
    from lecturehub.index.media import MediaProbeError

    root, c, phone = library

    def broken(path):
        raise MediaProbeError("ffprobe could not read audio")

    error = import_audio.probe(c, root, _file(phone, "깨진 녹음.m4a"), today=TODAY, probe_media=broken)["error"]
    assert "깨졌거나" in error and "ffprobe could not" not in error


def test_IMPORT_004_commit_copies_and_links_what_the_user_confirmed(library):
    root, c, phone = library
    source = _file(phone, "음성 260929_102801.m4a")
    before = (source.read_bytes(), source.stat().st_mtime_ns)
    item = import_audio.probe(c, root, source, today=TODAY, probe_media=_probe())
    result = import_audio.commit(c, root, {"path": str(source), "course": "구조역학", "date": TUESDAY, "time": "10:28",
                                           "device": "phone", "guess": item["guess"]}, today=TODAY, probe_media=_probe())
    c.commit()
    assert (source.read_bytes(), source.stat().st_mtime_ns) == before  # the user's file is untouched
    folder = root / "_lecturehub" / "imports" / "20260929_102800_import"
    copy = folder / source.name
    assert result["master"] == str(copy) and copy.read_bytes() == source.read_bytes()
    assert result["copied"] and not result["converted"] and result["start_basis"] == "filename"
    assert (result["other_recordings"], result["transcripts"]) == (0, 0)
    row = c.execute("SELECT lecture_id, path, sha256, origin, device_type, is_master, recorded_start, decided_by, "
                    "confidence_class, duration_ms FROM MediaAsset WHERE id = ?", (result["media_id"],)).fetchone()
    assert tuple(row) == (result["lecture_id"], str(copy), hashlib.sha256(source.read_bytes()).hexdigest(), "kakao_phone",
                          "phone", 1, _ms("2026-09-29T10:28:00"), "user", "exact", 75 * MIN)
    assert tuple(c.execute("SELECT course_id, date FROM Lecture WHERE id = ?", (result["lecture_id"],)).fetchone()) == (
        "hy", TUESDAY)
    detail = json.loads(c.execute("SELECT detail FROM Evidence WHERE subject_id = ? AND kind = 'imported_from' AND "
                                  "source = 'user'", (result["media_id"],)).fetchone()[0])
    assert detail["source_path"] == str(source) and detail["decided_by"] == "user"
    assert detail["prefilled"]["basis"] == "filename" and detail["start_basis"] == "filename"
    assert json.loads((folder / "session.json").read_text(encoding="utf-8"))["course"] == "구조역학"
    assert json.loads((folder / "import.json").read_text(encoding="utf-8"))["media_id"] == result["media_id"]
    shown = lectures.lecture_detail(c, result["lecture_id"])["recordings"]
    assert [(r["origin"], r["exists"]) for r in shown] == [("kakao_phone", True)]
    # the same bytes never come in twice, under any name
    again = _file(phone, "복사본.m4a", source.read_bytes())
    assert "이미 있는 녹음" in import_audio.probe(c, root, again, today=TODAY, probe_media=_probe())["error"]
    with pytest.raises(ValueError, match="이미 있는 녹음.*구조역학 1강"):
        import_audio.commit(c, root, {"path": str(again), "course": "구조역학", "date": TUESDAY}, today=TODAY,
                            probe_media=_probe())
    assert sorted(p.name for p in (root / "_lecturehub" / "imports").iterdir()) == ["20260929_102800_import"]


def test_IMPORT_005_changed_time_is_the_users_and_a_blank_time_is_recorded_as_an_assumption(library):
    root, c, phone = library
    first = _file(phone, "음성 260929_102801.m4a", b"synthetic one")
    guess = import_audio.probe(c, root, first, today=TODAY, probe_media=_probe())["guess"]
    edited = import_audio.commit(c, root, {"path": str(first), "course": "구조역학", "date": TUESDAY, "time": "10:35",
                                           "guess": guess}, today=TODAY, probe_media=_probe())
    assert edited["start_basis"] == "user"
    blank = import_audio.commit(c, root, {"path": str(_file(phone, "녹음 a.m4a", b"synthetic two")), "course": "구조역학실험",
                                          "date": TUESDAY}, today=TODAY, probe_media=_probe())
    assert blank["start_basis"] == "timetable_class_start"
    assert c.execute("SELECT recorded_start FROM MediaAsset WHERE id = ?", (blank["media_id"],)).fetchone()[0] == _ms(
        "2026-09-29T14:00:00")
    unknown = import_audio.commit(c, root, {"path": str(_file(phone, "녹음 b.m4a", b"synthetic three")), "course": "구조역학",
                                            "date": "2026-09-30", "device": "laptop"}, today=TODAY, probe_media=_probe())
    assert unknown["start_basis"] == "unknown"  # no class that day: midnight stands in and the record says so
    assert edited["lecture_id"] != unknown["lecture_id"]
    # a second recording of a class that already has one joins the same lecture
    second = import_audio.commit(c, root, {"path": str(_file(phone, "녹음 c.m4a", b"synthetic four")), "course": "구조역학",
                                           "date": TUESDAY, "time": "10:30"}, today=TODAY, probe_media=_probe())
    assert second["lecture_id"] == edited["lecture_id"] and second["other_recordings"] == 1


def test_IMPORT_006_refusals_leave_nothing_behind(library):
    root, c, phone = library
    source = _file(phone, "녹음.m4a")
    ok = {"path": str(source), "course": "구조역학", "date": TUESDAY}
    for change, message in (({"course": ""}, "과목을 골라"), ({"course": "없는과목"}, "없는 과목"),
                            ({"date": "어제"}, "날짜"), ({"date": "2026-10-02"}, "오늘보다 뒤"),
                            ({"time": "25:00"}, "시각"), ({"device": "tablet"}, "기기"),
                            ({"path": str(phone / "없음.m4a")}, "찾지 못"), ({"path": str(_file(phone, "a.txt"))}, "녹음 파일이 아닙")):
        with pytest.raises(ValueError, match=message):
            import_audio.commit(c, root, {**ok, **change}, today=TODAY, probe_media=_probe())

    def broken(path, ffprobe=None):
        raise OSError("합성 실패")

    with pytest.raises(OSError):
        import_audio.commit(c, root, ok, today=TODAY, probe_media=broken)
    c.rollback()
    assert not list((root / "_lecturehub" / "imports").iterdir())  # our own half-made folder is removed
    assert c.execute("SELECT count(*) FROM MediaAsset").fetchone()[0] == 0 and source.is_file()


def test_IMPORT_007_other_formats_get_an_mp3_and_extra_topics_get_their_own_lecture(library):
    root, c, phone = library
    source = _file(phone, "통화 녹음 260929_190000.amr", b"synthetic amr")
    made = []

    def convert(src, target):
        made.append((src, target))
        target.write_bytes(b"synthetic mp3 of " + src.read_bytes())

    result = import_audio.commit(c, root, {"path": str(source), "course": "AI 외부 강의", "date": TUESDAY, "time": "19:00"},
                                 today=TODAY, convert=convert, probe_media=_probe(40 * MIN))
    folder = root / "_lecturehub" / "imports" / "20260929_190000_import"
    assert result["converted"] and result["master"] == str(folder / "통화 녹음 260929_190000.mp3")
    assert made == [(folder / source.name, folder / "통화 녹음 260929_190000.mp3")]  # made from the copy
    assert (folder / source.name).read_bytes() == b"synthetic amr"  # the original bytes are kept next to it
    detail = json.loads(c.execute("SELECT detail FROM Evidence WHERE kind = 'imported_from'").fetchone()[0])
    assert detail["converted"] and detail["source_sha256"] == hashlib.sha256(b"synthetic amr").hexdigest()
    course = c.execute("SELECT c.folder_name FROM Lecture l JOIN Course c ON c.id = l.course_id WHERE l.id = ?",
                       (result["lecture_id"],)).fetchone()[0]
    assert course == "AI 외부 강의"
    another = import_audio.commit(c, root, {"path": str(_file(phone, "b.m4a", b"synthetic other talk")),
                                            "course": "AI 외부 강의", "date": TUESDAY}, today=TODAY, probe_media=_probe())
    assert another["lecture_id"] != result["lecture_id"]  # several talks a day are normal


def test_IMPORT_008_a_file_already_in_the_semester_folder_is_registered_where_it_is(library):
    root, c, _phone = library
    archive = root / "녹음본" / "구조역학"
    archive.mkdir(parents=True)
    source = _file(archive, "26.9.29 구조역학.m4a")
    result = import_audio.commit(c, root, {"path": str(source), "course": "구조역학", "date": TUESDAY, "time": "10:30"},
                                 today=TODAY, probe_media=_probe())
    assert result["master"] == str(source) and not result["copied"]
    assert not (root / "_lecturehub" / "imports").exists()
    c.commit()
    plan = discard.plan(c, root, result["media_id"])
    assert plan["trash"] == []  # throwing it away only hides it: the user's own file is never recycled


def test_IMPORT_009_recent_files_and_what_was_already_taken(library):
    root, c, phone = library
    taken = _file(phone, "음성 260929_102801.m4a", b"synthetic taken")
    _file(phone, "새 녹음.mp3", b"synthetic new")
    _file(phone, "사진.jpg")
    (phone / "폴더.m4a").mkdir()
    import_audio.commit(c, root, {"path": str(taken), "course": "구조역학", "date": TUESDAY}, today=TODAY, probe_media=_probe())
    now = int(max(p.stat().st_mtime for p in phone.iterdir()) * 1000) + 1000
    rows = import_audio.recent(c, [phone, phone / "없는 폴더"], now_ms=now)
    assert {r["name"]: r["imported"] for r in rows} == {"음성 260929_102801.m4a": True, "새 녹음.mp3": False}
    assert rows[0]["folder"] == "카카오톡 받은 파일"
    assert import_audio.recent(c, [phone], now_ms=now + 31 * 86_400_000) == []


class FakeBin:
    def __init__(self, folder):
        self.folder, self.moved = folder, {}

    def __call__(self, paths):
        self.folder.mkdir(exist_ok=True)
        for path in paths:
            target = self.folder / path.name
            path.rename(target)
            self.moved[path] = target

    def undelete(self, wanted, *, not_before_ms=0):
        back = []
        for path in wanted:
            if path in self.moved and not path.exists():
                self.moved.pop(path).rename(path)
                back.append(path)
        return back


def test_IMPORT_010_thrown_away_import_goes_to_the_bin_as_a_folder_and_can_come_back(library):
    root, c, phone = library
    source = _file(phone, "음성 260929_102801.m4a")
    result = import_audio.commit(c, root, {"path": str(source), "course": "구조역학", "date": TUESDAY, "time": "10:28"},
                                 today=TODAY, probe_media=_probe())
    c.commit()
    folder = root / "_lecturehub" / "imports" / "20260929_102800_import"
    rows = {s["name"]: s for s in discard.sessions(c, root)}
    assert rows[folder.name]["state"] == "linked" and rows[folder.name]["lecture"] == "구조역학 1강 (9/29)"
    assert discard.plan(c, root, result["media_id"])["trash"] == [str(folder)]
    bin_ = FakeBin(root.parent / "bin")
    discard.discard(c, root, result["media_id"], trash=bin_)
    c.commit()
    assert not folder.exists() and source.is_file()
    assert lectures.lecture_detail(c, result["lecture_id"])["recordings"] == []
    # the folder is in the Recycle Bin, yet the row stays so that [되살리기] can be pressed
    rows = {s["name"]: s for s in discard.sessions(c, root)}
    assert rows[folder.name]["state"] == "discarded" and rows[folder.name]["media_id"] == result["media_id"]
    later = c.execute("SELECT max(created_at) FROM Evidence WHERE kind = 'discarded'").fetchone()[0] + 31 * 86_400_000
    assert folder.name not in {s["name"] for s in discard.sessions(c, root, now_ms=later)}
    # the same file dropped again is not taken twice: the thrown-away one is brought back instead
    assert "되살리세요" in import_audio.probe(c, root, source, today=TODAY, probe_media=_probe())["error"]
    discard.restore(c, result["media_id"], undelete=bin_.undelete)
    c.commit()
    assert (folder / source.name).is_file()
    assert {s["name"]: s["state"] for s in discard.sessions(c, root)}[folder.name] == "linked"


# ---------------------------------------------------------------- sidecar transcript (클로바노트 가져오기, 2026-11-05)
from pathlib import Path  # noqa: E402

CLOVA_TXT = Path(__file__).parent / "fixtures" / "transcripts" / "clova.txt"


def _commit(c, root, source, duration=75 * MIN):
    return import_audio.commit(c, root, {"path": str(source), "course": "구조역학", "date": TUESDAY, "time": "10:28"},
                               today=TODAY, probe_media=_probe(duration))


def test_IMPORT_011_clova_transcript_next_to_the_recording_comes_in_with_it(library):
    root, c, phone = library
    source = _file(phone, "클로바노트 2026-09-29 10-28.m4a")
    text = phone / "클로바노트 2026-09-29 10-28.txt"
    text.write_bytes(CLOVA_TXT.read_bytes())
    before = text.read_bytes()
    result = _commit(c, root, source)
    c.commit()
    sidecar = result["sidecar"]
    assert sidecar["state"] == "attached" and sidecar["engine"] == "clova" and sidecar["segments"] == 2
    row = c.execute("SELECT lecture_id, media_id, engine, source_file, sha256, role FROM Transcript WHERE id = ?",
                    (sidecar["transcript_id"],)).fetchone()
    copied = Path(result["master"]).parent / text.name
    assert tuple(row) == (result["lecture_id"], result["media_id"], "clova", str(copied),
                          hashlib.sha256(before).hexdigest(), "timeline")
    assert copied.read_bytes() == before and text.read_bytes() == before  # raw transcript kept byte for byte
    assert lectures.lecture_detail(c, result["lecture_id"])  # the lecture still reads
    kinds = [r[0] for r in c.execute("SELECT kind FROM Evidence WHERE subject_id = ?", (sidecar["transcript_id"],))]
    assert kinds == ["imported_sidecar"]


def test_IMPORT_011_sidecar_is_optional_and_checked(library):
    root, c, phone = library
    assert _commit(c, root, _file(phone, "a.m4a", b"one"))["sidecar"] is None  # no transcript beside it
    (phone / "b.txt").write_text("그냥 메모\n", encoding="utf-8")
    skipped = _commit(c, root, _file(phone, "b.m4a", b"two"))["sidecar"]
    assert skipped["state"] == "skipped" and "형식" in skipped["reason"]
    long_text = CLOVA_TXT.read_bytes().decode("utf-8-sig").replace("참석자 2 01:04", "참석자 2 10:04")
    (phone / "c.txt").write_text(chr(0xFEFF) + long_text, encoding="utf-8")  # reaches 10:04, audio is 1 min
    short = import_audio.commit(c, root, {"path": str(_file(phone, "c.m4a", b"three")), "course": "구조역학",
                                          "date": "2026-09-22", "time": "10:28"}, today=TODAY,
                                probe_media=_probe(duration=MIN))
    assert short["sidecar"]["state"] == "skipped" and "녹음보다" in short["sidecar"]["reason"]
    assert c.execute("SELECT count(*) FROM Transcript").fetchone()[0] == 0
