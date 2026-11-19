"""B-2 literal Korean search and incremental material indexing regressions."""

from contextlib import closing

import pytest

from lecturehub.core.db import connect, migrate
from lecturehub.core.ids import new_ulid
from lecturehub.index.search import rebuild_search_index, search


def insert(connection, table, **values):
    fields = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    connection.execute(f"INSERT INTO {table} ({fields}) VALUES ({placeholders})", tuple(values.values()))


@pytest.fixture
def connection(tmp_path):
    with closing(connect(tmp_path / "검색 합성 (26-2).sqlite")) as connection:
        migrate(connection)
        insert(connection, "Course", id="course", code="SYN", folder_name="합성 구조역학", abbr="su", term="26-2")
        insert(connection, "Lecture", id="lecture", course_id="course", date="2026-09-29", session_index=1)
        insert(connection, "MediaAsset", id="media", lecture_id="lecture", path="합성.wav", sha256="media-hash",
               duration_ms=5000, codec="pcm", sample_rate=16000, channels=1, bytes=160000,
               origin="win_recorder", device_type="laptop", is_master=1, recorded_start=1000, partial=0,
               loudness_profile="[]", decided_by="user", confidence_class="exact")
        insert(connection, "Transcript", id="transcript", lecture_id="lecture", media_id="media", engine="daglo",
               role="reference", role_decided_by="user", status="done", source_file="합성 전사.txt", sha256="text-hash")
        for seq, text in enumerate(["앞 문장입니다.", "부력과 마찰손실을 설명합니다.", "뒤 문장입니다."]):
            insert(connection, "TranscriptSegment", id=new_ulid(), transcript_id="transcript", seq=seq,
                   start_ms=seq * 1000, text=text, chunk_index=0)
        connection.commit()
        yield connection


@pytest.mark.parametrize("query", ["부력", "마찰손실"])
def test_search_two_and_three_plus_characters_with_context(connection, query):
    """SEARCH-001/002: short LIKE and long trigram queries return source context."""
    results = search(connection, query)
    assert len(results) == 1
    result = results[0]
    assert (result.course, result.date, result.start_ms, result.engine) == (
        "합성 구조역학", "2026-09-29", 1000, "daglo"
    )
    assert result.context_before == "앞 문장입니다."
    assert result.context_after == "뒤 문장입니다."
    assert result.path == "합성 전사.txt"


def test_short_query_wildcards_are_literal(connection):
    """SEARCH-003: '%' and '_' never turn short user queries into match-all."""
    insert(connection, "TranscriptSegment", id=new_ulid(), transcript_id="transcript", seq=3,
           start_ms=3000, text="실제 표시 %_ 그리고 역슬래시 \\ 하나", chunk_index=0)
    assert [result.start_ms for result in search(connection, "%_")] == [3000]
    assert len(search(connection, "\\")) == 1
    assert len(search(connection, "%")) == 1


def test_fts_query_syntax_is_treated_as_literal_text(connection):
    """SEARCH-004: FTS operators and quotation marks cannot expand a query."""
    assert search(connection, "부력 OR 없는말") == []
    insert(connection, "TranscriptSegment", id=new_ulid(), transcript_id="transcript", seq=3,
           start_ms=3000, text='설명에 "quoted" 문자열을 넣었습니다.', chunk_index=0)
    assert len(search(connection, '"quoted"')) == 1
    assert search(connection, "   ") == []


def test_future_material_pages_are_searchable_and_follow_source_edits(connection):
    """SEARCH-005: E may fill MaterialPage later; its changes update search."""
    insert(connection, "Material", id="material", course_id="course", path="합성 자료.pdf", sha256="pdf-hash",
           kind="pdf", title="합성 강의자료", page_count=3)
    insert(connection, "MaterialPage", id=new_ulid(), material_id="material", page_no=1,
           text="앞쪽 페이지", terms="[]")
    insert(connection, "MaterialPage", id=new_ulid(), material_id="material", page_no=2,
           text="베르누이 정리의 합성 설명", terms="[]")
    insert(connection, "MaterialPage", id=new_ulid(), material_id="material", page_no=3,
           text="다음 페이지", terms="[]")
    insert(connection, "MaterialMatch", id=new_ulid(), lecture_id="lecture", material_id="material",
           page_from=2, page_to=2, confidence_class="exact", decided_by="user")
    result = search(connection, "베르누이")[0]
    assert (result.source_kind, result.page_no, result.date, result.path) == (
        "material", 2, "2026-09-29", "합성 자료.pdf"
    )
    assert result.context_before == "앞쪽 페이지"
    assert result.context_after == "다음 페이지"
    connection.execute("UPDATE MaterialPage SET text = '레이놀즈 합성 설명' WHERE material_id = ? AND page_no = 2",
                       ("material",))
    assert search(connection, "베르누이") == []
    assert len(search(connection, "레이놀즈")) == 1
    connection.execute("DELETE FROM MaterialPage WHERE material_id = ? AND page_no = 2", ("material",))
    assert search(connection, "레이놀즈") == []


def test_rebuild_is_repeatable_and_does_not_commit_or_change_raw_segments(connection):
    """SEARCH-006: rebuilding a derived index preserves immutable source data."""
    original = [tuple(row) for row in connection.execute("SELECT * FROM TranscriptSegment ORDER BY seq")]
    connection.execute("DELETE FROM SearchIndex")
    connection.commit()
    assert rebuild_search_index(connection) == 3
    assert connection.in_transaction
    assert len(search(connection, "부력")) == 1
    connection.rollback()
    assert search(connection, "부력") == []
    assert rebuild_search_index(connection) == 3
    assert rebuild_search_index(connection) == 3
    assert [tuple(row) for row in connection.execute("SELECT * FROM TranscriptSegment ORDER BY seq")] == original


def test_search_limit_is_respected(connection):
    assert len(search(connection, "문장", limit=1)) == 1
    with pytest.raises(ValueError):
        search(connection, "문장", limit=0)


@pytest.mark.parametrize("query", ["소리", "녹음없는"])
def test_search_includes_transcript_without_media_and_marks_flag(connection, query):
    """SEARCH-007: confirmed lecture transcripts stay searchable without audio."""
    insert(connection, "Transcript", id="no-audio", lecture_id="lecture", media_id=None, engine="clova",
           role="reference", role_decided_by="user", status="done", source_file="합성 무음원 전사.txt",
           sha256="no-audio-text-hash")
    for seq, text in enumerate(["합성 앞 문맥", "녹음없는 전사의 소리 설명", "합성 뒤 문맥"]):
        insert(connection, "TranscriptSegment", id=new_ulid(), transcript_id="no-audio", seq=seq,
               start_ms=seq * 1000, text=text, chunk_index=0)
    results = search(connection, query)
    assert len(results) == 1
    assert results[0].no_media is True
    assert results[0].engine == "clova"
    assert results[0].context_before == "합성 앞 문맥"
    assert results[0].context_after == "합성 뒤 문맥"
    assert search(connection, "부력")[0].no_media is False
