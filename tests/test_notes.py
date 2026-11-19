"""S4: evidence-linked notes and corrections (CITE-001/002, SRC-001~004, NOTE-002/003, CORR-001~004).
All transcript and material text is synthetic."""
import sqlite3
import time
from contextlib import closing

import pytest

from lecturehub.core import db
from lecturehub.study import corrections, notes

SEGMENTS = {
    "m": ["오늘은 관수로 마찰손실을 봅니다", "마찰계수는 영점영이오, 0.025 정도로 둡니다", "레이놀즈 수 2300 아래는 층류"],
    "g": ["오늘은 관수로 마찰 손실을 봅니다", "마찰계수는 0.025 정도로 둡니다", "레이놀즈 수 2300 아래는 층류입니다"],
}


@pytest.fixture
def library(tmp_path):
    with closing(db.connect(tmp_path / "lecturehub.sqlite")) as connection:
        db.migrate(connection)
        connection.execute("INSERT INTO Course VALUES ('su','SU','구조역학','su','26-2')")
        connection.execute("INSERT INTO Lecture VALUES ('l2','su','2026-09-22',1,NULL,NULL)")
        connection.execute("INSERT INTO Lecture VALUES ('l3','su','2026-09-29',1,NULL,NULL)")
        for tid, engine in (("m", "mai"), ("g", "gemini_transcribe")):
            connection.execute(
                "INSERT INTO Transcript (id,lecture_id,media_id,engine,role,role_decided_by,status,source_file,sha256) "
                "VALUES (?, 'l2', NULL, ?, 'timeline', 'policy', 'done', ?, ?)", (tid, engine, tid + ".txt", tid))
            for seq, text in enumerate(SEGMENTS[tid]):
                connection.execute("INSERT INTO TranscriptSegment (transcript_id,seq,start_ms,text,chunk_index) "
                                   "VALUES (?,?,?,?,0)", (tid, seq, seq * 60_000 + (500 if tid == "g" else 0), text))
        connection.execute("INSERT INTO Transcript (id,lecture_id,media_id,engine,role,role_decided_by,status,"
                           "source_file,sha256) VALUES ('other','l3',NULL,'mai','timeline','policy','done','o','o')")
        connection.execute("INSERT INTO TranscriptSegment (transcript_id,seq,start_ms,text,chunk_index) "
                           "VALUES ('other',0,0,'다른 회차 합성 문장',0)")
        connection.execute("INSERT INTO Material (id,course_id,path,sha256,kind,title,page_count) "
                           "VALUES ('w5','su','w5.pdf','w5','pdf','Week 5 합성',2)")
        connection.execute("INSERT INTO MaterialPage (material_id,page_no,text,image_path,terms) "
                           "VALUES ('w5',6,'Darcy-Weisbach 합성 식 f = 64/Re',NULL,'[]')")
        connection.commit()
        yield connection


def draft():
    return [
        {"key": "h1", "kind": "heading", "text": "관수로 마찰손실"},
        {"key": "p1", "kind": "paragraph", "text": "마찰계수는 0.025 정도로 둔다.",
         "cites": [{"t": "m", "s": 1, "q": "0.025 정도로 둡니다"}]},
        {"key": "p2", "kind": "item", "text": "층류 기준은 레이놀즈 수 2300, 층류 f = 64/Re",
         "cites": [{"t": "m", "s": 2}, {"m": "w5", "p": 6}]},
        {"key": "p3", "kind": "paragraph", "text": "근거 없이 지어낸 문장", "cites": []},
        {"key": "p4", "kind": "paragraph", "text": "없는 구간과 다른 회차를 인용",
         "cites": [{"t": "m", "s": 99}, {"t": "other", "s": 0}]},
        {"key": "p5", "kind": "item", "text": "마찰계수 0.03을 쓴다", "cites": [{"t": "m", "s": 1, "q": "0.03을 씁니다"}]},
        {"key": "e1", "kind": "external", "text": "레이놀즈 수는 관성력과 점성력의 비다",
         "ext": {"confidence": "high", "source": "general"}},
        {"key": "e2", "kind": "external", "text": "확신도 없는 외부 지식", "ext": {"source": "web"}},
        {"key": "e3", "kind": "external", "text": "기억에 의존한 보충", "ext": {"confidence": "low", "source": "general"}},
    ]


def make(library):
    result = notes.validate(draft(), notes.Sources(library), "l2")
    artifact = notes.save(library, "l2", result, created_by="claude_cli", inputs={"transcript_id": "m"}, reason="새 노트")
    library.commit()
    return result, artifact


def test_CITE_001_and_SRC_001_blocks_without_evidence_or_labels_are_discarded(library):
    result, _ = make(library)
    rejected = {r["key"]: r["reason"] for r in result.rejected}
    assert set(rejected) == {"p3", "p4", "e2"}
    assert "CITE-001" in rejected["p3"] and "CITE-001" in rejected["p4"] and "SRC-001" in rejected["e2"]
    assert [b["key"] for b in result.blocks] == ["h1", "p1", "p2", "p5", "e1", "e3"]


def test_SRC_002_and_NOTE_003_quote_and_number_checks_warn(library):
    result, _ = make(library)
    warnings = {b["key"]: b["warnings"] for b in result.blocks}
    assert warnings["p1"] == [] and warnings["p2"] == []  # 2300 from speech, 64 from the slide page
    assert any("SRC-002" in w for w in warnings["p5"]) and any("0.03" in w for w in warnings["p5"])


def test_SRC_004_header_counts_external_blocks_and_lists_low_ones(library):
    make(library)
    note = notes.load(library, "l2")
    assert note["external"] == {"total": 2, "high": 1, "mid": 0, "low": 1}
    assert note["verify_later"] == ["e3"]
    p2 = next(b for b in note["blocks"] if b["key"] == "p2")
    assert [(c["kind"], c.get("start_ms"), c.get("page_no")) for c in p2["cites"]] == [
        ("transcript", 120_000, None), ("material", None, 6)]


def test_note_blocks_are_append_only(library):
    make(library)
    with pytest.raises(sqlite3.DatabaseError):
        library.execute("UPDATE NoteBlock SET text = '바꿈'")
    with pytest.raises(sqlite3.DatabaseError):
        library.execute("DELETE FROM NoteCitation")


def test_CORR_001_correction_changes_the_view_not_the_raw_segment(library):
    raw = library.execute("SELECT text FROM TranscriptSegment WHERE transcript_id='m' AND seq=1").fetchone()[0]
    start = raw.index("영점영이오, ")
    corrections.add(library, "m", 1, start, start + len("영점영이오, "), "")
    library.commit()
    view = corrections.corrected_segments(library, "m")
    assert view["segments"][1]["text"] == "마찰계수는 0.025 정도로 둡니다" and view["orphaned"] == []
    assert library.execute("SELECT text FROM TranscriptSegment WHERE transcript_id='m' AND seq=1").fetchone()[0] == raw
    with pytest.raises(sqlite3.DatabaseError):
        library.execute("UPDATE TranscriptSegment SET text='x' WHERE transcript_id='m' AND seq=1")


def test_CITE_002_quote_passes_against_the_corrected_text(library):
    raw = SEGMENTS["m"][2]
    corrections.add(library, "m", 2, raw.index("층류"), raw.index("층류") + 2, "층류입니다")
    library.commit()
    result = notes.validate([{"key": "q", "kind": "item", "text": "2300 아래는 층류",
                              "cites": [{"t": "m", "s": 2, "q": "아래는 층류입니다"}]}], notes.Sources(library), "l2")
    assert result.blocks[0]["warnings"] == []


def test_CORR_002_corrections_follow_a_replaced_transcript_or_become_orphaned(library):
    raw = SEGMENTS["m"][0]
    corrections.add(library, "m", 0, raw.index("관수로"), raw.index("관수로") + 3, "관(管)수로")
    corrections.add(library, "m", 1, 0, len("마찰계수는 영점영이오"), "마찰계수 표기")  # words absent in Google's text
    library.commit()
    view = corrections.corrected_segments(library, "g")
    assert view["segments"][0]["text"] == "오늘은 관(管)수로 마찰 손실을 봅니다"
    assert len(view["orphaned"]) == 1
    orphaned = corrections.mark_orphans(library, "g")
    library.commit()
    assert orphaned == view["orphaned"]
    assert library.execute("SELECT status FROM Correction WHERE id=?", (orphaned[0],)).fetchone()[0] == "orphaned"
    assert library.execute("SELECT count(*) FROM Event WHERE type='CORRECTION_ORPHANED'").fetchone()[0] == 1


def test_CORR_003_only_blocks_citing_the_corrected_segment_become_stale(library):
    make(library)
    time.sleep(0.01)
    raw = SEGMENTS["m"][1]
    corrections.add(library, "m", 1, raw.index("0.025"), raw.index("0.025") + 5, "0.026")
    library.commit()
    stale = {b["key"] for b in notes.load(library, "l2")["blocks"] if b["stale"]}
    assert stale == {"p1", "p5"}


def test_NOTE_002_edit_makes_a_new_version_and_CORR_004_regeneration_keeps_user_blocks(library):
    make(library)
    first = notes.load(library, "l2")
    notes.edit_block(library, "l2", "p1", "마찰계수는 대략 0.025 (내가 고침)", "표현 수정")
    library.commit()
    second = notes.load(library, "l2")
    assert (first["version"], second["version"]) == (1, 2)
    assert next(b for b in second["blocks"] if b["key"] == "p1")["authored_by"] == "user"
    fresh = notes.validate([{"key": "p1", "kind": "paragraph", "text": "AI가 다시 쓴 문장 0.025",
                             "cites": [{"t": "m", "s": 1}]},
                            {"key": "p2", "kind": "item", "text": "AI가 다시 쓴 2300", "cites": [{"t": "m", "s": 2}]}],
                           notes.Sources(library), "l2")
    notes.replace_blocks(library, "l2", fresh, {"p1", "p2"}, created_by="claude_cli", inputs={})
    library.commit()
    third = notes.load(library, "l2")
    texts = {b["key"]: b["text"] for b in third["blocks"]}
    assert texts["p1"] == "마찰계수는 대략 0.025 (내가 고침)" and texts["p2"] == "AI가 다시 쓴 2300"
    assert third["version"] == 3
    assert library.execute("SELECT count(*) FROM StudyArtifact WHERE kind='lecture_note'").fetchone()[0] == 3


def test_CORR_003_stale_mark_survives_carrying_a_block_into_a_new_version(library):
    make(library)
    time.sleep(0.01)
    raw = SEGMENTS["m"][2]
    corrections.add(library, "m", 2, raw.index("2300"), raw.index("2300") + 4, "2,300")
    library.commit()
    notes.edit_block(library, "l2", "p1", "다른 블록만 고침 0.025", "표현 수정")  # p2 is carried, not rewritten
    library.commit()
    assert next(b for b in notes.load(library, "l2")["blocks"] if b["key"] == "p2")["stale"] is True
