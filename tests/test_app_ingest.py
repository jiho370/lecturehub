"""S2: app recording → lecture link (or pending), one-click confirm, Google job → in-use transcript."""
import codecs
import json
import shutil
from contextlib import closing
from pathlib import Path

import pytest

from lecturehub.core import db
from lecturehub.core.ids import new_ulid
from lecturehub.index import app_ingest, lectures
from lecturehub.recorder.capture import FakeSource
from lecturehub.recorder.finalize import finalize_session
from lecturehub.recorder.session import RecordingSession

pytestmark = pytest.mark.skipif(not shutil.which("ffprobe"), reason="ffprobe required")
H = 3_600_000


@pytest.fixture
def library(tmp_path):
    with closing(db.connect(tmp_path / "lecturehub.sqlite")) as connection:
        db.migrate(connection)
        connection.execute("INSERT INTO Course VALUES ('su','SU','구조역학','su','26-2')")
        connection.execute("INSERT INTO Course VALUES ('rw','RW','토질역학','rw','26-2')")
        connection.execute("INSERT INTO Schedule (course_id,weekday,start,end,id) VALUES ('su',1,?,?,?)",
                           (10 * H, 11 * H + 50 * 60_000, new_ulid()))
        connection.execute("INSERT INTO Schedule (course_id,weekday,start,end,id) VALUES ('rw',1,?,?,?)",
                           (14 * H, 17 * H + 50 * 60_000, new_ulid()))
        connection.commit()
        yield connection


def make_session(tmp_path, course: str, started_local: str, *, course_source: str = "user_pick", seconds: float = 1.0):
    """A real (synthetic) recording, finalized to m4a, with the start time set for the test (default: app user pick)."""
    source = FakeSource(sample_rate=16000, duration_seconds=seconds)
    session = RecordingSession(source, tmp_path / "녹음 (합성)", course=course, course_source=course_source)
    folder = session.start()
    while source.active:
        pass
    session.stop()
    meta = json.loads((folder / "session.json").read_text(encoding="utf-8"))
    from datetime import datetime
    meta.update(started_local=started_local, started_at=int(datetime.fromisoformat(started_local).timestamp() * 1000))
    (folder / "session.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    finalize_session(folder)
    return folder


def test_S2_selected_course_matching_the_timetable_links_the_lecture(library, tmp_path):
    folder = make_session(tmp_path, "구조역학", "2026-09-29T10:05:00+09:00")
    result = app_ingest.link_recording(library, folder)
    library.commit()
    assert (result["state"], result["course"], result["confidence"]) == ("linked", "구조역학", "high")
    media = library.execute("SELECT origin, is_master, recorded_start, lecture_id FROM MediaAsset").fetchone()
    assert media[0] == "app_mic" and media[1] == 1 and media[3] == result["lecture_id"]
    assert library.execute("SELECT date FROM Lecture WHERE id=?", (result["lecture_id"],)).fetchone()[0] == "2026-09-29"
    assert app_ingest.link_recording(library, folder)["already"] is True  # idempotent


def test_S2_no_course_but_one_class_is_inferred(library, tmp_path):
    folder = make_session(tmp_path, "", "2026-09-29T09:50:00+09:00")  # 10 min before class
    result = app_ingest.link_recording(library, folder)
    assert (result["state"], result["course"], result["confidence"], result["decided_by"]) == (
        "linked", "구조역학", "medium", "policy")


@pytest.mark.parametrize(("course", "when", "reason"), [
    ("토질역학", "2026-09-29T12:30:00+09:00", "시간표에 없음"),  # a blank course at such a time goes to 미분류 (test_extra)
    ("토질역학", "2026-09-29T10:05:00+09:00", "다름"),
])
def test_S2_uncertain_recordings_are_kept_pending_and_confirmed_with_one_click(library, tmp_path, course, when, reason):
    folder = make_session(tmp_path, course, when)
    result = app_ingest.link_recording(library, folder)
    library.commit()
    assert result["state"] == "pending" and reason in result["reason"]
    assert library.execute("SELECT count(*) FROM MediaAsset").fetchone()[0] == 0
    assert [p["ingest_id"] for p in app_ingest.pending_items(library)] == [result["ingest_id"]]
    linked = app_ingest.confirm_pending(library, result["ingest_id"], "구조역학")
    library.commit()
    assert linked["state"] == "linked"
    assert tuple(library.execute("SELECT decided_by, confidence_class FROM MediaAsset").fetchone()) == ("user", "exact")
    assert app_ingest.pending_items(library) == []


def test_S2_inferred_closure_day_keeps_the_recording_pending(library, tmp_path):
    library.execute("INSERT INTO ScheduleException (id,course_id,date,kind,note) VALUES (?,NULL,'2026-10-06','휴강','합성')",
                    (new_ulid(),))
    folder = make_session(tmp_path, "구조역학", "2026-10-06T10:05:00+09:00")
    assert app_ingest.link_recording(library, folder)["state"] == "pending"


def _job_output(tmp_path, name="26.9.29 구조역학.txt"):
    out = tmp_path / "out" / name
    out.parent.mkdir(parents=True, exist_ok=True)
    text = "00:00 \r\n합성 전사 첫 문단입니다.\r\n\r\n01:02 \r\n합성 전사 둘째 문단입니다.\r\n"
    out.write_bytes(codecs.BOM_UTF8 + text.encode("utf-8"))
    return out


def test_S2_finished_job_becomes_the_lectures_transcript_in_use(library, tmp_path):
    folder = make_session(tmp_path, "구조역학", "2026-09-29T10:05:00+09:00")
    linked = app_ingest.link_recording(library, folder)
    job = {"job_id": "job1", "state": "qa_warning", "audio": linked["master"], "output": str(_job_output(tmp_path)),
           "preset": {"verified_at": "2026-09-25"}, "chunks": [{"qa": [{"check": "density", "detail": "합성"}]}]}
    result = app_ingest.register_job_transcript(library, job)
    library.commit()
    assert result["state"] == "registered" and result["in_use_changed"] and result["qa"] == ["density"]
    pointer = lectures.in_use(library, linked["lecture_id"])
    assert pointer["transcript_id"] == result["transcript_id"] and pointer["recorded"]
    detail = lectures.lecture_detail(library, linked["lecture_id"])
    assert detail["state"]["key"] == "reviewable" and detail["transcripts"][0]["label"] == "Google"
    assert app_ingest.register_job_transcript(library, job)["already"] is True


def test_S2_job_for_a_pending_recording_waits_for_the_link(library, tmp_path):
    folder = make_session(tmp_path, "토질역학", "2026-09-29T12:30:00+09:00")
    pending = app_ingest.link_recording(library, folder)
    job = {"job_id": "job2", "state": "done", "audio": pending["master"], "output": str(_job_output(tmp_path, "b.txt"))}
    assert app_ingest.register_job_transcript(library, job)["state"] == "waiting_link"
    app_ingest.confirm_pending(library, pending["ingest_id"], "구조역학")
    assert app_ingest.register_job_transcript(library, job)["state"] == "registered"


def test_S2_rough_google_transcript_with_qa_warning_suggests_mai(library, tmp_path):
    folder = make_session(tmp_path, "구조역학", "2026-09-29T10:05:00+09:00")
    linked = app_ingest.link_recording(library, folder)
    job = {"job_id": "j", "state": "qa_warning", "audio": linked["master"], "output": str(_job_output(tmp_path)),
           "chunks": [{"qa": [{"check": "density", "detail": "합성"}]}]}
    app_ingest.register_job_transcript(library, job)
    state = lectures.lecture_detail(library, linked["lecture_id"])["state"]
    assert (state["action"], state["action_label"], state["why"]) == ("mai_prepare", "MAI 보강 권장", "전사 밀도·길이 경고")


def test_S2_fake_gemini_transcribes_offline_in_the_real_response_shape(tmp_path):
    from lecturehub.transcription.fake import FakeGeminiClient
    from lecturehub.transcription.gemini import paragraphs

    folder = make_session(tmp_path, "구조역학", "2026-09-29T10:05:00+09:00")
    master = next(p for p in folder.glob("master_*") if p.suffix in (".mp3", ".m4a"))
    result = FakeGeminiClient().transcribe(master)
    assert result.raw["mock"] is True and paragraphs(result)[0][0] == 0


def test_review_S2_1_mock_transcripts_are_never_registered_for_real(library, tmp_path):
    folder = make_session(tmp_path, "구조역학", "2026-09-29T10:05:00+09:00")
    linked = app_ingest.link_recording(library, folder)
    job = {"job_id": "mock", "state": "done", "mock": True, "audio": linked["master"],
           "output": str(_job_output(tmp_path, "mock.txt"))}
    assert app_ingest.register_job_transcript(library, job)["state"] == "refused_mock"
    assert library.execute("SELECT count(*) FROM Transcript").fetchone()[0] == 0
    assert app_ingest.register_job_transcript(library, job, allow_mock=True)["state"] == "registered"


def test_review_S2_1_mock_transport_refuses_the_real_semester_folder(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from lecturehub import cli
    cli._register_all()
    audio = tmp_path / "합성.m4a"
    audio.write_bytes(b"x")
    monkeypatch.setenv("LECTUREHUB_FAKE_GEMINI", "1")
    # Never run a command against the real semester folder from a test (AGENTS: synthetic data only; a class may be
    # recording there). The real root simply has no synthetic marker — checked without touching it.
    from lecturehub.transcription.cli import is_synthetic_root

    assert not is_synthetic_root(Path(r"C:\\Users\\student\\Documents\\학기\\26-2"))
    other = tmp_path / "다른 학기"
    (other / "_lecturehub").mkdir(parents=True)
    result = CliRunner().invoke(cli.app, ["transcribe", "run", str(audio), "--course", "합성", "--root", str(other)])
    assert result.exit_code == 1 and "합성 표지" in result.output  # allow-list: any unmarked root is refused


def test_review_S2_2_a_timetable_prefill_is_not_a_user_pick(library, tmp_path):
    folder = make_session(tmp_path, "구조역학", "2026-09-29T10:05:00+09:00", course_source="timetable_auto")
    meta = json.loads((folder / "session.json").read_text(encoding="utf-8"))
    assert RecordingSession(FakeSource(), tmp_path / "x").course_source == "unknown"  # conservative default
    (folder / "session.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    result = app_ingest.link_recording(library, folder)
    assert (result["state"], result["confidence"], result["decided_by"]) == ("linked", "medium", "policy")
    assert "자동 채운" in result["reason"]



def test_review_S2_2b_only_a_user_pick_links_high(library, tmp_path):
    for seconds, (source, expected) in enumerate((("user_pick", "high"), ("unknown", "medium")), start=1):
        folder = make_session(tmp_path / source, "구조역학", "2026-09-29T10:05:00+09:00", course_source=source,
                              seconds=float(seconds))  # different audio → different recording
        assert app_ingest.link_recording(library, folder)["confidence"] == expected


def test_D055_linked_recording_is_copied_to_the_archive_add_only(library, tmp_path, monkeypatch):
    from lecturehub.index import archive_copy

    monkeypatch.setattr(archive_copy, "MIN_COPY_MS", 0)  # the synthetic recording is a few seconds long

    root = tmp_path / "학기(합성)"
    folder = make_session(tmp_path, "구조역학", "2026-09-29T10:05:00+09:00")
    linked = app_ingest.link_recording(library, folder)
    suffix = Path(linked["master"]).suffix  # the master's own format (MP3 by default)
    other = root / "녹음본" / "구조역학" / f"26.9.29 구조역학{suffix}"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"someone else's recording")  # the name is taken by a different file
    library.commit()
    assert archive_copy.pending(library) == [linked["media_id"]]
    result = archive_copy.copy_media(library, root, linked["media_id"])
    library.commit()
    assert result["state"] == "copied" and result["path"].endswith(f"26.9.29 구조역학 2{suffix}")
    assert other.read_bytes() == b"someone else's recording"  # never overwritten
    assert Path(result["path"]).read_bytes() == Path(linked["master"]).read_bytes()
    assert Path(linked["master"]).is_file()  # the original stays under the session folder
    assert archive_copy.pending(library) == []
    assert archive_copy.copy_media(library, root, linked["media_id"])["state"] == "already"
    assert len(list((root / "녹음본" / "구조역학").iterdir())) == 2


def test_D055_pending_recording_is_not_copied(library, tmp_path):
    from lecturehub.index import archive_copy

    folder = make_session(tmp_path, "토질역학", "2026-09-29T12:30:00+09:00")
    assert app_ingest.link_recording(library, folder)["state"] == "pending"
    assert archive_copy.pending(library) == []
    assert not (tmp_path / "녹음본").exists()


def test_wav_goes_to_recycle_bin_only_after_the_finished_transcript(library, tmp_path, monkeypatch):
    """User decision 2026-09-29: WAV → Recycle Bin when master verified + 녹음본 copy matches + C3 transcript."""
    from lecturehub.index import archive_copy, wav_cleanup

    monkeypatch.setattr(archive_copy, "MIN_COPY_MS", 0)  # the synthetic recording is a few seconds long

    root = tmp_path / "학기(합성)"
    folder = make_session(tmp_path, "구조역학", "2026-09-29T10:05:00+09:00")
    linked = app_ingest.link_recording(library, folder)
    trashed = []
    fake_trash = lambda paths: trashed.extend(paths)
    assert wav_cleanup.clean(library, fake_trash) == [] and trashed == []
    assert wav_cleanup.candidates(library)[0]["reason"].startswith("녹음본 복사본")
    archive_copy.copy_media(library, root, linked["media_id"])
    assert "완성 전사본" in wav_cleanup.candidates(library)[0]["reason"]
    library.execute("INSERT INTO CanonicalTranscript (id,lecture_id,version,created_at,created_by,inputs,material_ids,"
                    "stage_reached,status) VALUES ('c1',?,1,0,'pipeline','[]','[]','C2','done')", (linked["lecture_id"],))
    assert wav_cleanup.clean(library, fake_trash) == []  # C2 is not the finished transcript
    library.execute("INSERT INTO CanonicalTranscript (id,lecture_id,version,created_at,created_by,inputs,material_ids,"
                    "stage_reached,status) VALUES ('c2',?,2,0,'user','[]','[]','C3','final')", (linked["lecture_id"],))
    done = wav_cleanup.clean(library, fake_trash)
    assert len(done) == 1 and {p.name.split("_")[0] for p in trashed} == {"segment", "master"}
    assert all(p.suffix == ".wav" for p in trashed)  # the compressed master and metadata stay
    assert Path(linked["master"]).suffix in (".mp3", ".m4a")


def test_D057_recording_far_past_class_end_waits_for_its_range(library, tmp_path):
    """녹음본 gets only the trimmed file: 15+ min past the timetable end → held until the range is decided."""
    from lecturehub.index import archive_copy, wav_cleanup

    folder = make_session(tmp_path, "구조역학", "2026-09-29T10:05:00+09:00")
    linked = app_ingest.link_recording(library, folder)
    columns = [r[1] for r in library.execute("PRAGMA table_info(MediaAsset)")]
    row = dict(zip(columns, library.execute("SELECT * FROM MediaAsset WHERE id=?", (linked["media_id"],)).fetchone(),
                   strict=True))
    row.update(id="LONG", sha256="long-sha", duration_ms=(2 * 60 + 1) * 60_000)  # 10:05 → 12:06, 16 min past 11:50
    library.execute(f"INSERT INTO MediaAsset ({','.join(columns)}) VALUES ({','.join('?' * len(columns))})",
                    tuple(row.values()))
    reason, use = archive_copy.range_hold(library, "LONG")
    assert "16분" in reason and use == "LONG"
    result = archive_copy.copy_media(library, tmp_path / "학기(합성)", "LONG")
    assert result["state"] == "held" and not (tmp_path / "학기(합성)" / "녹음본").exists()
    assert archive_copy.range_hold(library, linked["media_id"])[0] is None  # a normal-length recording is not held
    held = next(i for i in wav_cleanup.candidates(library) if i["media_id"] == "LONG")
    assert held["reason"] == "녹음 범위(끝 지점) 결정 전"


def test_a_copy_in_the_archive_makes_no_lecture_and_old_empty_ones_are_removed(library, tmp_path, monkeypatch):
    """User request 2026-10-09: the index made an empty lecture from the file name of each 녹음본 copy."""
    from lecturehub.index import archive_copy, ingest, tidy

    monkeypatch.setattr(archive_copy, "MIN_COPY_MS", 0)
    root = tmp_path / "학기(합성)"
    linked = app_ingest.link_recording(library, make_session(tmp_path, "구조역학", "2026-09-29T10:05:00+09:00"))
    copy = Path(archive_copy.copy_media(library, root, linked["media_id"])["path"])
    assert ingest._copy_lecture(library, copy) == linked["lecture_id"]
    assert ingest._copy_lecture(library, copy.with_name("26.9.22 구조역학.m4a")) is None  # a file the app did not copy
    # what the older index left behind: a lecture named after the copy, with nothing in it
    for lecture_id, in_use in (("empty", False), ("used", True)):
        library.execute("INSERT INTO Lecture VALUES (?,?,?,?,?,NULL)",
                        (lecture_id, "su", "2026-09-29", 2 + in_use, copy.stem))
        library.execute("INSERT INTO Evidence (id,subject_type,subject_id,kind,detail,source,created_at) "
                        "VALUES (?,'Lecture',?,'filename_keyword','{}','heuristic',1)", (f"e-{lecture_id}", lecture_id))
    library.execute("INSERT INTO Transcript (id,lecture_id,media_id,engine,role,role_decided_by,status,source_file,"
                    "sha256) VALUES ('t','used',NULL,'daglo','timeline','policy','done','x','x')")
    found = {item["lecture_id"]: item for item in tidy.empty_copy_lectures(library)}
    assert set(found) == {"empty", "used"} and found["empty"]["removable"] and not found["used"]["removable"]
    assert "Transcript.lecture_id" in found["used"]["kept_because"]
    assert [item["lecture_id"] for item in tidy.remove_empty_copy_lectures(library)] == ["empty"]
    left = {r[0] for r in library.execute("SELECT id FROM Lecture")}
    assert "empty" not in left and {"used", linked["lecture_id"]} <= left
    assert library.execute("SELECT count(*) FROM Event WHERE type = 'LECTURE_REMOVED'").fetchone()[0] == 1
    assert tidy.remove_empty_copy_lectures(library) == []
