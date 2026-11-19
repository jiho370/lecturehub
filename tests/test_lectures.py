"""S1: lecture list/detail and the transcript-in-use pointer with history (D-049)."""
import sqlite3
from contextlib import closing
from datetime import date

import pytest

from lecturehub.core import db
from lecturehub.index import lectures


@pytest.fixture
def library(tmp_path):
    audio = tmp_path / "녹음 (합성).m4a"
    audio.write_bytes(b"synthetic")
    with closing(db.connect(tmp_path / "lecturehub.sqlite")) as connection:
        db.migrate(connection)
        connection.execute("INSERT INTO Course VALUES ('su','SU','구조역학','su','26-2')")
        for lecture_id, day in (("l1", "2026-09-15"), ("l2", "2026-09-22"), ("l3", "2026-09-29")):
            connection.execute("INSERT INTO Lecture VALUES (?, 'su', ?, 1, NULL, NULL)", (lecture_id, day))
        connection.execute(
            "INSERT INTO MediaAsset VALUES ('m2','l2',?, 'mh', 60000, 'aac', 48000, 1, 9, 'win_recorder', "
            "'laptop', 1, NULL, NULL, 123, 0, '[]', 'user', 'exact')", (str(audio),))
        for tid, engine, media in (("g", "gemini_transcribe", "m2"), ("m", "mai", "m2"), ("d", "daglo", None)):
            connection.execute(
                "INSERT INTO Transcript (id,lecture_id,media_id,engine,role,role_decided_by,status,source_file,sha256) "
                "VALUES (?, 'l2', ?, ?, 'timeline', 'policy', 'done', ?, ?)", (tid, media, engine, tid + ".txt", tid))
            for seq in range(3):
                connection.execute("INSERT INTO TranscriptSegment (transcript_id,seq,start_ms,text,chunk_index) "
                                   "VALUES (?,?,?,?,0)", (tid, seq, seq * 60000, f"{engine} 합성 문장 {seq}"))
        connection.commit()
        yield connection


def test_D049_policy_prefers_mai_over_google_and_daglo(library):
    pointer = lectures.in_use(library, "l2")
    assert pointer["transcript_id"] == "m" and pointer["decided_by"] == "policy" and not pointer["recorded"]
    assert lectures.in_use(library, "l1") is None


def test_D049_user_choice_is_recorded_with_history_and_never_overridden(library):
    assert lectures.refresh_policy(library, "l2")  # records the policy pick (MAI)
    lectures.choose_in_use(library, "l2", "g", decided_by="user", reason="앱에서 사용자가 선택(합성)")
    library.commit()
    assert lectures.in_use(library, "l2")["transcript_id"] == "g"
    assert lectures.refresh_policy(library, "l2") is None  # policy never overrides a user choice
    steps = lectures.history(library, "l2")
    assert [(s["from"], s["to"], s["decided_by"]) for s in steps] == [(None, "m", "policy"), ("m", "g", "user")]
    assert library.execute("SELECT count(*) FROM Event WHERE type='TRANSCRIPT_IN_USE_CHANGED'").fetchone()[0] == 2
    with pytest.raises(sqlite3.DatabaseError):
        library.execute("UPDATE TranscriptInUse SET reason='x'")
    with pytest.raises(sqlite3.DatabaseError):
        library.execute("DELETE FROM TranscriptInUse")


def test_pointer_must_stay_inside_its_lecture(library):
    with pytest.raises(sqlite3.DatabaseError, match="same lecture"):
        lectures.choose_in_use(library, "l1", "m", decided_by="user", reason="잘못된 회차(합성)")


def test_lecture_detail_state_badges_and_media_link(library):
    detail = lectures.lecture_detail(library, "l2")
    assert detail["n"] == 2 and detail["course"] == "구조역학" and detail["weekday"] == "화"
    assert {t["label"] for t in detail["transcripts"]} == {"Google", "MAI", "다글로"}
    assert [t["id"] for t in detail["transcripts"] if t["in_use"]] == ["m"]
    assert detail["state"]["key"] == "reviewable"
    assert lectures.media_for_transcript(library, "m")["exists"]
    assert lectures.media_for_transcript(library, "d") is None  # no recorded link → "연결된 원음 없음"
    assert lectures.lecture_detail(library, "l1")["state"]["key"] == "no_sources"


def test_lecture_list_ordinals_and_groups(library):
    items = lectures.lecture_list(library)
    assert [(i["id"], i["n"]) for i in items] == [("l3", 3), ("l2", 2), ("l1", 1)]
    today = date(2026, 9, 29)
    assert lectures.group_label("2026-09-29", today) == "오늘"
    assert lectures.group_label("2026-09-28", today) == "어제"
    assert lectures.group_label("2026-09-22", today) == "9월 21일 – 27일"
    assert lectures.group_label("2026-09-30", date(2026, 10, 9)) == "9월 28일 – 10월 4일"


def test_app_library_sidebar_lecture_search_and_choice(library, tmp_path):
    """S1 app adapter over a real schema: groups, lecture detail with segments/media, search, user choice."""
    from lecturehub.index.search import rebuild_search_index
    from lecturehub.ui.library import Library

    rebuild_search_index(library)
    library.commit()
    root = tmp_path / "학기"
    (root / "_lecturehub").mkdir(parents=True)
    import shutil
    library.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    shutil.copy(tmp_path / "lecturehub.sqlite", root / "_lecturehub" / "lecturehub.sqlite")
    app = Library(root)
    side = app.sidebar(date(2026, 9, 29))
    assert side["ok"] and [g["label"] for g in side["groups"]] == ["오늘", "9월 21일 – 27일", "9월 14일 – 20일"]
    assert side["groups"][1]["items"][0]["reviewable"] is True
    opened = app.lecture("l2")
    assert opened["ok"] and opened["lecture"]["in_use"]["transcript_id"] == "m"
    assert [g["start_ms"] for g in opened["lecture"]["segments"]] == [0, 60000, 120000]
    assert opened["lecture"]["media"]["exists"]
    found = app.search("합성 문장")
    assert found["ok"] and {r["lecture_id"] for r in found["results"]} == {"l2"}
    assert all(r["n"] == 2 for r in found["results"])
    assert app.choose("l2", "g")["ok"]
    assert app.lecture("l2")["lecture"]["in_use"] == {
        "transcript_id": "g", "decided_by": "user", "reason": "앱에서 사용자가 사용본으로 선택", "recorded": True}


def test_media_server_serves_only_registered_files_with_ranges(tmp_path):
    import urllib.error
    import urllib.request

    from lecturehub.ui.media_server import MediaServer

    audio = tmp_path / "합성 녹음.m4a"
    audio.write_bytes(bytes(range(256)) * 4)
    server = MediaServer()
    try:
        url = server.url_for(audio)
        with urllib.request.urlopen(urllib.request.Request(url, headers={"Range": "bytes=10-19"})) as response:
            assert response.status == 206 and response.read() == bytes(range(10, 20))
            assert response.headers["Content-Range"] == "bytes 10-19/1024"
            assert response.headers["Content-Type"] == "audio/mp4"
        with urllib.request.urlopen(url) as response:
            assert response.status == 200 and len(response.read()) == 1024
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(url.rsplit("/", 1)[0] + "/not-registered")
        assert server.url_for(audio) == url  # stable token per file
    finally:
        server.close()


def test_a_re_transcription_with_the_same_engine_and_length_wins_over_the_older_run():
    from lecturehub.index.lectures import policy_pick

    older = {"id": "01M500000000000000000000AA", "engine": "gemini_transcribe", "segments": 40}
    newer = {"id": "01M597EZG3YBX9PE7S3KBK911T", "engine": "gemini_transcribe", "segments": 40}
    assert policy_pick([newer, older])["id"] == newer["id"]
    assert policy_pick([older, newer])["id"] == newer["id"]
    longer_old = {**older, "segments": 41}
    assert policy_pick([longer_old, newer])["id"] == older["id"]   # more segments still comes first
