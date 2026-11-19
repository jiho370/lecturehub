"""S5 (HOLD-001~005): held files become questions; answers are appended and only answered files rejoin.
All sources are synthetic (see test_index_ingest.make_root)."""
import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from test_index_ingest import make_root

from lecturehub.core.db import connect
from lecturehub.index import holds, index_root
from lecturehub.index.media import MediaMetadata

KST = timezone(timedelta(hours=9))


@pytest.fixture
def semester(tmp_path, monkeypatch):
    start = int(datetime(2026, 9, 25, 10, 3, tzinfo=KST).timestamp() * 1000)
    monkeypatch.setattr("lecturehub.index.ingest.probe_media", lambda path, ffprobe=None: MediaMetadata(
        60000, "aac", 16000, 1, recorded_start=start if "9.25" in path.name else None))
    root = make_root(tmp_path)
    t = root / "전사본/합성과목"
    (t / "2주차 수업(화요일).txt").write_text("00:01 \n합성 둘째 주 수업.\n00:09 \n합성 문장.\n", encoding="utf-8")
    (t / "26.9.22 합성과목 통째.txt").write_text("00:01\n형식을 모르는 합성 전사.\n00:05\n두 번째 합성 줄.\n", encoding="utf-8")
    (t / "26.9.23 합성과목.txt").write_text(
        "﻿합성 제목\n2026.09.22 화 오전 10:00 ・ 2분 30초\n합성 작성자\n\n참석자 1 00:01\n머리말 날짜가 다른 합성.\n",
        encoding="utf-8")
    (root / "녹음본/합성과목/26.9.25 합성과목.m4a").write_bytes(b"synthetic audio without device label")
    index_root(root)
    connection = connect(root / "_lecturehub/lecturehub.sqlite")
    yield root, connection
    connection.close()


def _digests(root):
    return {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob("*") if p.is_file()
            and "_lecturehub" not in p.parts}


def _card(connection, question):
    return next(c for c in holds.questions(connection)["cards"] if c["question"] == question)


def test_S5_questions_explain_each_hold_with_evidence_and_choices(semester):
    _root, connection = semester
    cards = {c["question"]: c for c in holds.questions(connection)["cards"]}
    assert set(cards) == {"identity", "engine", "date", "device"}
    date_card = cards["date"]
    assert {e["label"]: e["value"] for e in date_card["evidence"]}["전사 머리말 날짜"] == "2026-09-22"
    assert [o["value"]["date"] for o in date_card["options"]] == ["2026-09-23", "2026-09-22"]
    assert [o["value"]["engine"] for o in cards["engine"]["options"]] == ["gemini_transcribe", "mai", "daglo", "clova"]
    assert cards["identity"]["preview"]["type"] == "text" and cards["identity"]["preview"]["lines"][0] == "00:01 "
    assert cards["device"]["preview"] == {"type": "audio", "path": cards["device"]["items"][0]["path"]}
    # the redesigned 확인 card (2026-11-13) names the file and says why in plain Korean
    for card in cards.values():
        assert card["file"] == card["items"][0]["name"]
        assert card["reason_ko"] in holds.REASON_KO.values()
    assert cards["device"]["reason_ko"] == "녹음한 기기나 시작 시각을 모릅니다"


def test_HOLD_001_002_answer_is_appended_and_rejoin_registers_only_that_file_idempotently(semester):
    root, connection = semester
    card = _card(connection, "identity")
    ids = [i["id"] for i in card["items"]]
    held_before = connection.execute("SELECT id, suggestion FROM IngestItem WHERE id=?", (ids[0],)).fetchone()
    holds.answer(connection, ids, {"folder": "합성과목", "date": "2026-09-15", "session": ""})
    connection.commit()
    assert tuple(connection.execute("SELECT id, suggestion FROM IngestItem WHERE id=?", (ids[0],)).fetchone()) \
        == tuple(held_before)  # the answer is appended, the judgement row is not rewritten
    report = index_root(root, connection, only_paths={root / "전사본/합성과목/2주차 수업(화요일).txt"})
    assert report.stats["transcript"]["indexed"] == 1 and report.stats["media"]["indexed"] == 0
    out = holds.outcome(connection, ids)[0]
    assert out["state"] == "confirmed" and out["date"] == "2026-09-15" and out["next"] == "지금 복습 가능"
    decided = connection.execute("SELECT detail FROM Evidence WHERE subject_id=? AND kind='confirmed_resolution'",
                                 (ids[0],)).fetchone()
    assert decided is not None and '"decided_by":"user"' in decided[0].replace(" ", "")
    counts = connection.execute("SELECT (SELECT count(*) FROM Transcript), (SELECT count(*) FROM Lecture)").fetchone()
    index_root(root, connection, only_paths={root / "전사본/합성과목/2주차 수업(화요일).txt"})
    assert connection.execute("SELECT (SELECT count(*) FROM Transcript), (SELECT count(*) FROM Lecture)").fetchone() \
        == counts
    assert "engine" in {c["question"] for c in holds.questions(connection)["cards"]}  # the others stay held


def test_engine_date_and_device_answers_each_rejoin(semester):
    root, connection = semester
    for question, value in (("engine", {"engine": "daglo"}), ("date", {"folder": "합성과목", "date": "2026-09-22"}),
                            ("device", {"device_type": "phone"})):
        holds.answer(connection, [i["id"] for i in _card(connection, question)["items"]], value)
    connection.commit()
    paths = {root / "전사본/합성과목/26.9.22 합성과목 통째.txt", root / "전사본/합성과목/26.9.23 합성과목.txt",
             root / "녹음본/합성과목/26.9.25 합성과목.m4a"}
    index_root(root, connection, only_paths=paths)
    rows = dict(connection.execute("SELECT source_file, engine FROM Transcript").fetchall())
    assert rows[str(root / "전사본/합성과목/26.9.22 합성과목 통째.txt")] == "daglo"
    clova = connection.execute("SELECT l.date FROM Transcript t JOIN Lecture l ON l.id = t.lecture_id "
                               "WHERE t.source_file LIKE '%26.9.23%'").fetchone()
    assert clova[0] == "2026-09-22"  # the user's date, not the file name's
    assert connection.execute("SELECT device_type FROM MediaAsset WHERE path LIKE '%26.9.25%'").fetchone()[0] == "phone"
    assert holds.questions(connection)["cards"][0]["question"] == "identity"  # only the unanswered one is left


def test_answered_format_that_does_not_parse_stays_held_with_a_reason(semester):
    root, connection = semester
    memo = root / "전사본/합성과목/26.9.22 합성과목 메모.txt"
    memo.write_text("시각 표시가 없는 합성 메모.\n둘째 줄.\n", encoding="utf-8")
    index_root(root, connection, only_paths={memo})
    item = connection.execute("SELECT id FROM IngestItem WHERE source_path=?", (str(memo),)).fetchone()[0]
    holds.answer(connection, [item], {"engine": "daglo"})
    connection.commit()
    index_root(root, connection, only_paths={memo})
    state, reason = connection.execute("SELECT state, suggestion FROM IngestItem WHERE id=?", (item,)).fetchone()
    assert state == "held" and "answered format (daglo)" in reason


def test_HOLD_003_later_keeps_held_and_not_study_material_rejects_without_touching_files(semester):
    root, connection = semester
    before = _digests(root)
    later = [i["id"] for i in _card(connection, "engine")["items"]]
    holds.defer(connection, later)
    rejected = [i["id"] for i in _card(connection, "identity")["items"]]
    holds.reject(connection, rejected)
    connection.commit()
    q = holds.questions(connection)
    assert q["deferred"] == 1 and {c["question"] for c in q["cards"]} == {"date", "device"}
    states = dict(connection.execute("SELECT id, state FROM IngestItem").fetchall())
    assert states[later[0]] == "held" and states[rejected[0]] == "rejected"
    assert _digests(root) == before
    tomorrow = int((datetime.now().astimezone() + timedelta(days=2)).timestamp() * 1000)
    assert holds.questions(connection, now_ms=tomorrow)["deferred"] == 0


def test_HOLD_004_answers_are_the_users_and_inferred_links_are_not(semester):
    _root, connection = semester
    holds.answer(connection, [i["id"] for i in _card(connection, "device")["items"]], {"device_type": "laptop"})
    connection.commit()
    sources = {r[0] for r in connection.execute(
        "SELECT source FROM Evidence WHERE subject_type='IngestItem' AND kind='user_answer'")}
    assert sources == {"user"}
    with pytest.raises(ValueError):
        holds.answer(connection, [i["id"] for i in _card(connection, "engine")["items"]], {"engine": "모름"})


def test_HOLD_005_undo_is_recorded_and_brings_a_rejected_file_back(semester):
    _root, connection = semester
    item = _card(connection, "identity")["items"][0]["id"]
    holds.reject(connection, [item])
    connection.commit()
    assert holds.undo(connection, item) == "rejected"
    connection.commit()
    assert connection.execute("SELECT state FROM IngestItem WHERE id=?", (item,)).fetchone()[0] == "held"
    engine_item = _card(connection, "engine")["items"][0]["id"]
    holds.answer(connection, [engine_item], {"engine": "daglo"})
    holds.undo(connection, engine_item)
    connection.commit()
    assert all(a["item_id"] != engine_item for a in holds.latest_answers(connection).values())
    kinds = [r[0] for r in connection.execute("SELECT kind FROM Evidence WHERE subject_id=? ORDER BY rowid",
                                              (engine_item,))]
    assert kinds[-2:] == ["user_answer", "user_answer_revoked"]


def test_S5_app_library_answers_and_rejoins_end_to_end(semester):
    """The app's inbox: sidebar count, cards, one answer → that file registered, count goes down."""
    from datetime import date as day

    from lecturehub.ui.library import Library

    root, connection = semester
    connection.close()
    library = Library(root)
    assert library.sidebar(day(2026, 9, 29))["holds"] == 4
    cards = library.holds()["cards"]
    card = next(c for c in cards if c["question"] == "engine")
    result = library.hold_answer([i["id"] for i in card["items"]], card["options"][2]["value"])  # daglo
    assert result["ok"], result
    assert result["outcome"][0]["state"] == "confirmed" and result["outcome"][0]["next"] == "지금 복습 가능"
    assert library.sidebar(day(2026, 9, 29))["holds"] == 3
    device = next(c for c in library.holds()["cards"] if c["question"] == "device")
    assert library.hold_defer([i["id"] for i in device["items"]])["ok"]
    assert library.sidebar(day(2026, 9, 29))["holds"] == 2


def test_device_answer_with_start_time_registers_a_recording_that_had_no_start(semester, monkeypatch):
    root, connection = semester
    clip = root / "녹음본/합성과목/26.9.26 합성과목.m4a"
    clip.write_bytes(b"synthetic audio with neither device nor start")
    index_root(root, connection, only_paths={clip})  # probe gives no start for this file
    item = connection.execute("SELECT id FROM IngestItem WHERE source_path=?", (str(clip),)).fetchone()[0]
    holds.answer(connection, [item], {"device_type": "laptop"})
    connection.commit()
    index_root(root, connection, only_paths={clip})
    assert connection.execute("SELECT state FROM IngestItem WHERE id=?", (item,)).fetchone()[0] == "held"
    with pytest.raises(ValueError, match="10:05"):
        holds.answer(connection, [item], {"device_type": "laptop", "start": "25:99"})
    holds.answer(connection, [item], {"device_type": "laptop", "start": "10:05"})
    connection.commit()
    index_root(root, connection, only_paths={clip})
    start = connection.execute("SELECT recorded_start FROM MediaAsset WHERE path=?", (str(clip),)).fetchone()[0]
    assert start == int(datetime(2026, 9, 26, 10, 5, tzinfo=KST).timestamp() * 1000)


def test_wrong_format_asks_the_format_again_in_korean(semester):
    """Cloud loop r01: a wrongly picked format was a dead end ("이 파일을 어떻게 할까요?") with an English reason."""
    root, connection = semester
    memo = root / "전사본/합성과목/26.9.22 합성과목 메모.txt"
    memo.write_text("시각 표시가 없는 합성 메모.\n둘째 줄.\n", encoding="utf-8")
    index_root(root, connection, only_paths={memo})
    item = connection.execute("SELECT id FROM IngestItem WHERE source_path=?", (str(memo),)).fetchone()[0]
    for wrong, label in (("daglo", "다글로"), ("clova", "클로바노트")):
        holds.answer(connection, [item], {"engine": wrong})  # allowed again: the card still asks the format
        connection.commit()
        index_root(root, connection, only_paths={memo})
        card = next(c for c in holds.questions(connection)["cards"] if any(i["id"] == item for i in c["items"]))
        assert card["question"] == "engine" and len(card["options"]) == 4
        why = {e["label"]: e["value"] for e in card["evidence"]}["보류 이유"]
        assert label in why and "다른 형식" in why and "answered" not in why


def test_hold_reasons_read_in_korean():
    for reason in holds.REASON_KO:
        assert holds.reason_ko(reason) != reason
    assert "읽지 못했습니다" in holds.reason_ko("source read failed (ValueError)")
    assert "소리 파일" in holds.reason_ko("ffprobe could not read audio")
    assert holds.reason_ko("something new") == "something new"  # unknown reasons are shown, not hidden
