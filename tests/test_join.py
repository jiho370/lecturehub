"""Join recordings of one class (user request 2026-09-29). Synthetic data only."""
import json
from contextlib import closing

import pytest

from lecturehub.core import db, extra_courses
from lecturehub.index import app_ingest, archive_copy, join, lectures
from lecturehub.index.media import MediaMetadata


@pytest.fixture
def library(tmp_path, monkeypatch):
    root = tmp_path / "학기 (합성)"
    (root / "_lecturehub").mkdir(parents=True)
    durations = {"a": 5 * 60_000, "b": 30 * 60_000}
    monkeypatch.setattr("lecturehub.index.app_ingest.probe_media", lambda path, ffprobe=None: MediaMetadata(
        durations["a" if "_a" in str(path) else "b"], "mp3", 48000, 1))
    path = root / "_lecturehub" / "lecturehub.sqlite"
    db.init_db(path)
    with closing(db.connect(path)) as c:
        extra_courses.add(root, "AI 외부 강의")
        yield root, c


def session(root, name, started, started_at, mode="loopback"):
    folder = root / "_lecturehub" / "recordings" / name
    folder.mkdir(parents=True)
    (folder / "master_합성.mp3").write_bytes(f"synthetic {name}".encode())
    (folder / "session.json").write_text(json.dumps({"course": "AI 외부 강의", "course_source": "user_pick", "mode": mode,
                                                     "started_local": started, "started_at": started_at}), encoding="utf-8")
    (folder / "stop.json").write_text(json.dumps({"cause": "user_stop"}), encoding="utf-8")
    (folder / "finalize_1.json").write_text(json.dumps({"master": str(folder / "master_합성.mp3")}), encoding="utf-8")
    return folder


def _transcript(c, media_id, lecture_id, texts, step=60_000):
    tid = "t_" + media_id[-6:]
    c.execute("INSERT INTO Transcript (id,lecture_id,media_id,engine,role,role_decided_by,status,source_file,sha256) "
              "VALUES (?,?,?,'gemini_transcribe','timeline','policy','done',?,?)", (tid, lecture_id, media_id, tid, tid))
    for seq, text in enumerate(texts):
        c.execute("INSERT INTO TranscriptSegment (transcript_id,seq,start_ms,end_ms,speaker,text,chunk_index) "
                  "VALUES (?,?,?,?,NULL,?,0)", (tid, seq, seq * step, seq * step + 1000, text))
    return tid


def _two(library):
    root, c = library
    first = app_ingest.link_recording(c, session(root, "20260929_194942_a_loopback", "2026-09-29T19:49:42+09:00",
                                                 1790679000000))
    second = app_ingest.link_recording(c, session(root, "20260929_201500_b_loopback", "2026-09-29T20:15:00+09:00",
                                                  1790679000000 + 25 * 60_000))
    return first, second


def test_join_001_joins_audio_and_transcripts_and_hides_the_pieces(library, monkeypatch):
    root, c = library
    first, second = _two(library)
    _transcript(c, first["media_id"], first["lecture_id"], ["앞 조각 첫 문장(합성)", "앞 조각 둘째(합성)"])
    _transcript(c, second["media_id"], second["lecture_id"], ["뒤 조각 첫 문장(합성)"])
    assert first["lecture_id"] != second["lecture_id"]
    written = []

    def concat(sources, target):
        written.append([s.parent.name for s in sources])
        target.write_bytes(b"".join(s.read_bytes() for s in sources))

    plan = join.plan(c, [second["media_id"], first["media_id"]])
    assert [p["id"] for p in plan["pieces"]] == [first["media_id"], second["media_id"]]  # time order, not click order
    assert plan["pieces"][1]["gap_before_ms"] == 20 * 60_000 and plan["engines"] == ["gemini_transcribe"]
    result = join.join(c, root, [second["media_id"], first["media_id"]], concat=concat,
                       probe=lambda path: MediaMetadata(35 * 60_000, "mp3", 48000, 1))
    c.commit()
    assert written == [["20260929_194942_a_loopback", "20260929_201500_b_loopback"]]
    assert result["lecture_id"] == first["lecture_id"] and result["duration_ms"] == 35 * 60_000
    # originals untouched on disk; the joined file lives under _lecturehub\derived
    assert (root / "_lecturehub" / "recordings" / "20260929_194942_a_loopback" / "master_합성.mp3").is_file()
    assert result["path"].startswith(str(root / "_lecturehub" / "derived"))
    detail = lectures.lecture_detail(c, first["lecture_id"])
    assert [r["id"] for r in detail["recordings"]] == [result["joined_id"]]
    assert [t["id"] for t in detail["transcripts"]] == result["transcripts"]
    segments = c.execute("SELECT start_ms, text FROM TranscriptSegment WHERE transcript_id=? ORDER BY seq",
                         (result["transcripts"][0],)).fetchall()
    assert [tuple(s) for s in segments] == [(0, "앞 조각 첫 문장(합성)"), (60_000, "앞 조각 둘째(합성)"),
                                            (5 * 60_000, "뒤 조각 첫 문장(합성)")]
    assert detail["in_use"]["transcript_id"] == result["transcripts"][0]
    listed = {x["id"] for x in lectures.lecture_list(c)}
    assert first["lecture_id"] in listed and second["lecture_id"] not in listed  # the empty second lecture is gone
    monkeypatch.setattr(archive_copy, "MIN_COPY_MS", 0)
    assert archive_copy.pending(c) == [result["joined_id"]]
    assert join.candidates(c, result["joined_id"]) == []


def test_join_002_refuses_mixed_or_single_pieces(library):
    root, c = library
    first, _second = _two(library)
    mic = app_ingest.link_recording(c, session(root, "20260929_194942_a_mic", "2026-09-29T19:49:42+09:00",
                                               1790679000000, mode="mic"))
    with pytest.raises(ValueError, match="마이크 녹음과 PC 소리"):
        join.plan(c, [first["media_id"], mic["media_id"]])
    with pytest.raises(ValueError, match="두 개 이상"):
        join.plan(c, [first["media_id"]])
    assert [x["id"] for x in join.candidates(c, first["media_id"])] == [_second["media_id"]]


def test_join_does_not_hold_the_library_while_ffmpeg_encodes(library):
    """Release review 2026-11-16: a join can take minutes of encoding. A helper process (linking a new recording,
    registering a transcript) must be able to write meanwhile: nothing may be written before the encoding ends."""
    root, c = library
    first, second = _two(library)
    c.commit()
    wrote = []

    def concat(sources, target):
        assert not c.in_transaction  # no write is pending on the joining connection while ffmpeg would run
        with closing(db.connect(root / "_lecturehub" / "lecturehub.sqlite")) as other:
            other.execute("PRAGMA busy_timeout = 200")
            other.execute("UPDATE Lecture SET title = '다른 프로세스가 쓴 제목(합성)' WHERE id = ?", (first["lecture_id"],))
            other.commit()
            wrote.append(True)
        target.write_bytes(b"".join(s.read_bytes() for s in sources))

    join.join(c, root, [first["media_id"], second["media_id"]], concat=concat,
              probe=lambda path: MediaMetadata(35 * 60_000, "mp3", 48000, 1))
    c.commit()
    assert wrote == [True]
    assert c.execute("SELECT title FROM Lecture WHERE id = ?", (first["lecture_id"],)).fetchone()[0].startswith("다른 프로세스")


def test_join_003_a_laptop_mic_piece_joins_a_phone_piece_but_pc_sound_stays_apart(library):
    """2026-11-09: the laptop battery died and the class went on in the phone — mic and phone are one room recording."""
    root, c = library
    a = app_ingest.link_recording(c, session(root, "20260929_194942_a_mic", "2026-09-29T19:49:42+09:00",
                                             1790679000000, mode="mic"))
    b = app_ingest.link_recording(c, session(root, "20260929_201500_b_mic", "2026-09-29T20:15:00+09:00",
                                             1790679000000 + 25 * 60_000, mode="mic"))
    assert join._kind("app_mic") == join._kind("other") == join._kind("phone") == "room"
    assert join._kind("app_loopback") == "pc"
    assert [x["id"] for x in join.candidates(c, a["media_id"])] == [b["media_id"]]
    assert len(join.plan(c, [a["media_id"], b["media_id"]])["pieces"]) == 2


def test_join_004_short_result_is_refused_and_discarding_a_join_frees_its_pieces(library):
    from lecturehub.index import discard

    root, c = library
    first, second = _two(library)

    def concat(sources, target):
        target.write_bytes(b"short synthetic join")

    total = join.plan(c, [first["media_id"], second["media_id"]])["duration_ms"]
    with pytest.raises(ValueError, match="조각들보다 짧습니다"):
        join.join(c, root, [first["media_id"], second["media_id"]], concat=concat,
                  probe=lambda path: MediaMetadata(total - 60_000, "mp3", 48000, 1))
    assert not list((root / "_lecturehub" / "derived").glob("joined_*.mp3"))
    result = join.join(c, root, [first["media_id"], second["media_id"]], concat=concat,
                       probe=lambda path: MediaMetadata(total, "mp3", 48000, 1))
    assert join.joined_parts(c) == {first["media_id"], second["media_id"]}
    discard._evidence(c, result["joined_id"], "discarded", {"decided_by": "user"})
    assert join.joined_parts(c) == set()


def test_join_005_real_ffmpeg_joins_pieces_with_different_codecs(tmp_path):
    """The concat demuxer dropped an M4A after an MP3 (83 of 108 min); the concat filter keeps both."""
    import shutil
    import subprocess

    from lecturehub.index.media import probe_media

    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        pytest.skip("ffmpeg not installed")
    mp3, m4a, out = tmp_path / "앞 (합성).mp3", tmp_path / "뒤 (합성).m4a", tmp_path / "joined.mp3"
    for target, seconds, codec in ((mp3, 3, "libmp3lame"), (m4a, 4, "aac")):
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
                        "-ac", "2", "-ar", "44100", "-c:a", codec, str(target)], check=True)
    join._ffmpeg_concat([mp3, m4a], out)
    assert abs(probe_media(out).duration_ms - 7000) < 400
