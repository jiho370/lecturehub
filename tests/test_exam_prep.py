"""Exam prep board and practice questions (P-031): bank files, attempts, what the board shows. Synthetic data only."""
import json
from contextlib import closing
from datetime import date

import pytest

from lecturehub.core import db
from lecturehub.index import calendar_events as cal
from lecturehub.index import exam_prep


def _bank(root, name="중간.json", **changes):
    data = {"version": 1, "course": "경제학원론", "bank": "중간고사 대비", "generator": "synthetic", "items": [
        {"type": "ox", "question": "자산 = 부채 + 자본이다(합성).", "answer": "o", "topic": "1장", "origin": "claude",
         "explanation": "회계등식", "sources": ["합성 p.1"]},
        {"type": "mcq", "question": "차변에 오는 것은(합성)?", "choices": ["자산의 증가", "부채의 증가"], "answer": "자산의 증가",
         "topic": "2장", "origin": "textbook"},
        {"type": "calc", "question": "100 + 20(합성)", "answer": "120", "topic": "2장", "origin": "class_example"},
    ]}
    data.update(changes)
    path = root / "_lecturehub" / "quiz" / "경제학원론" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture
def library(tmp_path):
    (tmp_path / "_lecturehub").mkdir()
    with closing(db.connect(tmp_path / "_lecturehub" / "lecturehub.sqlite")) as connection:
        db.migrate(connection)
        connection.execute("INSERT INTO Course VALUES ('ac','AC','경제학원론','ac','26-2')")
        connection.execute("INSERT INTO Course VALUES ('xx','XX','빈과목','xx','26-2')")
        connection.execute("INSERT INTO Schedule (course_id, weekday, start, end) VALUES ('ac', 4, 840, 1010)")
        connection.commit()
        yield connection, tmp_path


def test_bank_is_read_once_and_a_newer_file_keeps_the_attempts(library):
    connection, root = library
    first = exam_prep.import_bank(connection, root, _bank(root))
    assert (first["state"], first["added"]) == ("imported", 3)
    assert exam_prep.import_bank(connection, root, _bank(root))["state"] == "already"
    ox = next(i for i in exam_prep.items(connection, "ac", mode="all") if i["type"] == "ox")
    assert (ox["answer"], ox["origin"], ox["topic"], ox["state"]) == ("O", "claude", "1장", "new")
    exam_prep.answer(connection, ox["id"], "X")
    # a newer file of the same bank: one question gone, one new, the OX one unchanged but with a new explanation
    items = json.loads(_bank(root).read_text(encoding="utf-8"))["items"]
    items[0]["explanation"] = "고친 해설(합성)"
    newer = _bank(root, "중간_v2.json", items=[items[0], items[2], {"type": "short", "question": "새 문제(합성)", "answer": "답"}])
    second = exam_prep.import_bank(connection, root, newer)
    assert (second["added"], second["updated"], second["retired"]) == (1, 2, 1)
    after = {i["question"]: i for i in exam_prep.items(connection, "ac", mode="all")}
    assert len(after) == 3 and "차변에 오는 것은(합성)?" not in after
    assert (after[ox["question"]]["state"], after[ox["question"]]["explanation"]) == ("wrong", "고친 해설(합성)")
    assert connection.execute("SELECT count(*) FROM Event WHERE type='QUIZ_BANK_IMPORTED'").fetchone()[0] == 2


def test_wrong_then_right_twice_is_mastered_and_wrong_ones_come_first(library):
    connection, root = library
    exam_prep.import_bank(connection, root, _bank(root))
    ox, mcq, calc = exam_prep.items(connection, "ac", mode="all")
    assert exam_prep.answer(connection, ox["id"], " o ") == {"correct": True, "state": "mastered", "answer": "O"}
    assert exam_prep.answer(connection, mcq["id"], "부채의 증가")["state"] == "wrong"
    with pytest.raises(ValueError, match="직접 표시"):
        exam_prep.answer(connection, calc["id"], "120")  # shown the answer, the user marks it
    assert [i["id"] for i in exam_prep.items(connection, "ac")] == [mcq["id"], calc["id"], ox["id"]]
    assert [i["id"] for i in exam_prep.items(connection, "ac", mode="wrong")] == [mcq["id"]]
    assert [i["id"] for i in exam_prep.items(connection, "ac", mode="new")] == [calc["id"]]
    assert exam_prep.answer(connection, mcq["id"], "자산의 증가")["state"] == "reviewing"
    assert exam_prep.answer(connection, mcq["id"], "자산의 증가")["state"] == "mastered"
    assert exam_prep.answer(connection, calc["id"], "119", correct=False)["state"] == "wrong"
    found = exam_prep.stats(connection, "ac")
    assert (found["total"], found["new"], found["wrong"], found["mastered"]) == (3, 0, 1, 2)
    assert {t["topic"]: (t["total"], t["wrong"]) for t in found["topics"]} == {"1장": (1, 0), "2장": (2, 1)}
    assert connection.execute("SELECT count(*) FROM QuizAttempt").fetchone()[0] == 5


def test_a_bad_bank_file_is_refused_whole_and_named(library):
    connection, root = library
    good = _bank(root)
    bad = _bank(root, "나쁨.json", bank="다른 묶음", items=[
        {"type": "ox", "question": "괜찮은 문제(합성)", "answer": "O"},
        {"type": "mcq", "question": "보기 밖의 답(합성)", "choices": ["가", "나"], "answer": "다"}])
    _bank(root, "남의과목.json", course="없는과목")
    outside = root.parent / "outside.json"
    outside.write_text(good.read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(ValueError, match="2번 문제"):
        exam_prep.read_bank(bad)
    with pytest.raises(ValueError, match="학기 폴더 안"):
        exam_prep.import_bank(connection, root, outside)
    result = exam_prep.import_new(connection, root)
    assert [b["file"] for b in result["imported"]] == ["중간.json"]
    assert sorted(e["file"] for e in result["errors"]) == ["나쁨.json", "남의과목.json"]
    assert connection.execute("SELECT count(*) FROM QuizItem").fetchone()[0] == 3  # nothing of the bad file came in
    assert connection.execute("SELECT count(*) FROM QuizSet").fetchone()[0] == 1


def test_board_shows_dates_heard_dates_cues_prep_files_and_practice(library):
    connection, root = library
    today = date(2026, 10, 11)
    cal.add(connection, kind="quiz", title="퀴즈 1", day="2026-10-16", course_id="ac", start="16:15", note="1~3장(합성)")
    cal.add(connection, kind="exam", title="지난 시험", day="2026-10-01", course_id="ac")
    cal.add(connection, kind="assignment", title="과제", day="2026-10-20", course_id="ac")
    cal.add(connection, kind="exam", title="시험 — 수업 중 언급", day="2026-10-30", course_id="ac", source="transcript",
            source_ref="t#1#10월 30일", status="candidate", date_text="10월 30일")
    connection.execute("INSERT INTO Lecture VALUES ('L1','ac','2026-09-18',1,NULL,NULL)")
    connection.execute("INSERT INTO ExamCue VALUES ('c1','L1',1400000,'scope_notice','1 2 3단원만 퀴즈(합성)','','',NULL,2,'t','medium','rule')")
    connection.execute("INSERT INTO ExamCue VALUES ('c2','L1',900000,'assignment','과제 얘기(합성)','','',NULL,1,'t','low','rule')")
    notes = root / "경제학원론" / "정리본"
    notes.mkdir(parents=True)
    (notes / "경제학원론_중간고사_대비.html").write_text("<p>합성</p>", encoding="utf-8")
    (notes / "9.11_경제학원론_강의노트.html").write_text("<p>합성</p>", encoding="utf-8")
    exam_prep.import_bank(connection, root, _bank(root))
    found = exam_prep.board(connection, root, today)
    assert [c["folder"] for c in found["courses"]] == ["경제학원론"]  # a course with nothing is not listed
    course = found["courses"][0]
    assert [(e["title"], e["d_day"]) for e in course["exams"]] == [("퀴즈 1", 5)]
    assert [(c["date"], c["d_day"]) for c in course["candidates"]] == [("2026-10-30", 19)]
    assert [(c["id"], c["date"], c["kind_label"]) for c in course["cues"]] == [("c1", "2026-09-18", "일정·범위 언급")]
    assert [f["name"] for f in course["files"]] == ["경제학원론_중간고사_대비"]
    assert (course["quiz"]["total"], course["quiz"]["new"]) == (3, 3)


def test_cues_picked_by_an_ai_are_kept_only_when_the_transcript_holds_the_words(library):
    connection, root = library
    connection.execute("INSERT INTO Lecture VALUES ('L1','ac','2026-09-18',1,NULL,NULL)")
    for transcript, engine in (("T1", "gemini_transcribe"), ("T2", "whisper")):
        connection.execute("INSERT INTO Transcript (id,lecture_id,media_id,engine,engine_version,role,role_decided_by,chunk_plan_id,"
                           "status,source_file,sha256) VALUES (?,'L1',NULL,?,NULL,'timeline','policy',NULL,'done',?,?)",
                           (transcript, engine, transcript, transcript))
    rows = [("T1", 0, 1_380_000, "그래서 일 이 삼 단원만 퀴즈예요. 사 오는 아니에요(합성)."), ("T1", 1, 1_460_000, "다음 얘기(합성)"),
            ("T2", 0, 1_385_000, "그래서 1 2 3단원만 퀴즈에요 4 5는 아니에요(합성)"), ("T2", 1, 3_000_000, "수정분개가 제일 중요합니다(합성)")]
    connection.executemany("INSERT INTO TranscriptSegment (transcript_id,seq,start_ms,end_ms,speaker,text,chunk_index) "
                           "VALUES (?,?,?,NULL,NULL,?,0)", rows)
    path = root / "_lecturehub" / "quiz" / "경제학원론" / "중간.cues.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "course": "경제학원론", "cues": [
        {"date": "2026-09-18", "at": "23:20", "kind": "scope_notice", "quote": "1 2 3단원만 퀴즈예요. 4 5는 아니에요(합성)", "note": "퀴즈 범위"},
        {"date": "2026-09-18", "at": "50:10", "kind": "emphasis", "quote": "수정분개가 제일 중요합니다(합성)"},
        {"date": "2026-09-18", "at": "10:00", "kind": "exam_mention", "quote": "이 문제는 시험에 그대로 냅니다(합성 — 전사에 없는 말)"},
        {"date": "2026-09-18", "at": "23:20", "kind": "emphasis", "quote": "수정분개가 제일 중요합니다(합성)"},
        {"date": "2026-09-19", "at": "01:00", "kind": "emphasis", "quote": "수업이 없는 날의 말(합성)"},
        {"date": "2026-09-18", "at": "1:2:3:4", "kind": "emphasis", "quote": "시각이 잘못된 줄(합성)"}]}, ensure_ascii=False), encoding="utf-8")
    result = exam_prep.import_new(connection, root)
    assert result["errors"] == [] and [c["added"] for c in result["cues"]] == [2]
    assert [s["n"] for s in result["cues"][0]["skipped"]] == [3, 4, 5, 6]  # 4 is the second quote at a time it was not said
    kept = connection.execute("SELECT at_ms, kind, engines_agree, confidence_class, decided_by, context_before FROM ExamCue "
                              "ORDER BY at_ms").fetchall()
    assert [tuple(r) for r in kept] == [(1_400_000, "scope_notice", 2, "medium", "ai", "퀴즈 범위"),
                                        (3_010_000, "emphasis", 1, "low", "ai", "")]
    assert exam_prep.import_new(connection, root)["cues"] == []  # the same file is not read again
    cues = exam_prep.board(connection, root, date(2026, 10, 11))["courses"][0]["cues"]
    assert [(c["by_label"], c["note"]) for c in cues] == [("AI가 전사에서 고른 것", "퀴즈 범위"), ("AI가 전사에서 고른 것", "")]



def test_a_newer_cues_file_updates_the_note_of_a_cue_already_kept(library):
    connection, root = library
    connection.execute("INSERT INTO Lecture VALUES ('L1','ac','2026-09-18',1,NULL,NULL)")
    connection.execute("INSERT INTO Transcript (id,lecture_id,media_id,engine,engine_version,role,role_decided_by,chunk_plan_id,"
                       "status,source_file,sha256) VALUES ('T1','L1',NULL,'whisper',NULL,'timeline','policy',NULL,'done','T1','T1')")
    connection.executemany("INSERT INTO TranscriptSegment (transcript_id,seq,start_ms,end_ms,speaker,text,chunk_index) "
                           "VALUES ('T1',?,?,NULL,NULL,?,0)",
                           [(0, 600_000, "혼합벽은 꼭 나옵니다(합성)"), (1, 1_200_000, "원통 관 문제를 자주 냅니다(합성)"),
                            (2, 2_400_000, "혼합벽은 꼭 나옵니다(합성)")])
    connection.execute("INSERT INTO ExamCue VALUES ('r1','L1',1200000,'emphasis','원통 관 문제를 자주 냅니다(합성)','','',NULL,1,'t',"
                       "'low','user')")
    path = root / "_lecturehub" / "quiz" / "경제학원론" / "중간.cues.json"
    path.parent.mkdir(parents=True, exist_ok=True)

    def write(*cues):
        path.write_text(json.dumps({"version": 1, "course": "경제학원론", "cues": [
            {"date": "2026-09-18", "at": at, "kind": kind, "quote": quote, "note": note} for at, kind, quote, note in cues]},
            ensure_ascii=False), encoding="utf-8")
        return exam_prep.import_cues(connection, root, path)

    first = write(("10:00", "emphasis", "혼합벽은 꼭 나옵니다(합성)", "옛 메모"),
                  ("20:00", "exam_mention", "원통 관 문제를 자주 냅니다(합성)", "AI 메모"))
    assert (first["added"], first["updated"]) == (1, 0)   # the cue the user decided is not added twice
    again = write(("10:00", "exam_mention", "혼합벽은 꼭 나옵니다(합성)", "새 메모 — 혼합벽 53~54쪽"),
                  ("20:00", "exam_mention", "원통 관 문제를 자주 냅니다(합성)", "AI 메모"),
                  ("40:00", "exam_mention", "혼합벽은 꼭 나옵니다(합성)", "두 번째로 한 말"))
    assert (again["added"], again["updated"], again["skipped"]) == (1, 1, [])
    rows = connection.execute("SELECT id, at_ms, kind, context_before, decided_by FROM ExamCue ORDER BY at_ms, id").fetchall()
    assert [tuple(r)[1:] for r in rows] == [(600_000, "exam_mention", "새 메모 — 혼합벽 53~54쪽", "ai"),
                                            (1_200_000, "emphasis", "", "user"),
                                            (2_400_000, "exam_mention", "두 번째로 한 말", "ai")]
    assert write(("10:00", "exam_mention", "혼합벽은 꼭 나옵니다(합성)", "새 메모 — 혼합벽 53~54쪽"),
                 ("40:00", "exam_mention", "혼합벽은 꼭 나옵니다(합성)", "두 번째로 한 말"),
                 ("20:00", "emphasis", "원통 관 문제를 자주 냅니다(합성)", "x"))["updated"] == 0   # nothing changed

def test_past_exam_questions_come_first_and_have_their_own_round(library):
    connection, root = library
    items = json.loads(_bank(root).read_text(encoding="utf-8"))["items"]
    items += [{"type": "calc", "question": "2023 중간 3번(합성)", "answer": "1", "origin": "past_exam", "sources": ["2023 중간 3번"]},
              {"type": "calc", "question": "2023 중간 3번의 숫자를 바꾼 것(합성)", "answer": "2", "origin": "past_exam_variant"}]
    exam_prep.import_bank(connection, root, _bank(root, "기출.json", bank="기출", items=items))
    mixed = exam_prep.items(connection, "ac")
    assert [i["origin"] for i in mixed[:2]] == ["past_exam", "past_exam_variant"] and len(mixed) == 5
    assert mixed[0]["origin_label"] == "기출 문제"
    assert [i["origin"] for i in exam_prep.items(connection, "ac", mode="past")] == ["past_exam", "past_exam_variant"]
    exam_prep.answer(connection, mixed[0]["id"], "1", correct=True)
    exam_prep.answer(connection, mixed[2]["id"], "X")  # an ordinary one goes wrong: mistakes still come before unseen past ones
    assert [i["origin"] for i in exam_prep.items(connection, "ac")][:2] == ["claude", "past_exam_variant"]
    found = exam_prep.stats(connection, "ac")
    assert (found["past"], found["past_left"]) == (2, 1)
