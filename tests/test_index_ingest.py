"""INGEST-005 and B-2 indexing: only generated synthetic sources are used."""

import json
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from lecturehub.core.db import connect
from lecturehub.core.ids import new_ulid
from lecturehub.index import index_root, verify_sources
from lecturehub.index.ingest import _insert, _point_time
from lecturehub.index.matching import find_note_paths, material_ranges, parse_lecture_name
from lecturehub.index.media import MediaMetadata, MediaProbeError, probe_media
from lecturehub.index.scanner import scan_sources
from lecturehub.parsers.dedupe import sha256_file

FIXTURES = Path(__file__).parent / "fixtures" / "index"


def make_root(tmp_path):
    root = tmp_path / "학기 자료 (26-2)"
    root.mkdir()
    for name in ("녹음본/합성과목", "전사본/합성과목", "합성과목/수업 정리 html"):
        (root / name).mkdir(parents=True)
    (root / "시간표.csv").write_bytes((FIXTURES / "schedule.csv").read_bytes())
    (root / "강의자료_매칭.md").write_bytes((FIXTURES / "material_matching.md").read_bytes())
    (root / "녹음본/합성과목/26.9.22 합성과목.m4a").write_bytes(b"synthetic audio, measured by test double")
    transcript = "00:01 \r\n합성 수로의 자료를 설명합니다.\r\n00:04 \r\n가상 물의 흐름입니다.\r\n"
    (root / "전사본/합성과목/26.9.22 합성과목.txt").write_bytes(b"\xef\xbb\xbf" + transcript.encode())
    (root / "합성과목/Week 4 Synthetic.pdf").write_bytes(b"%PDF synthetic material fixture")
    (root / "합성과목/수업 정리 html/합성과목_0922_강의노트.html").write_text(
        "<html><body>합성 강의노트</body></html>", encoding="utf-8",
    )
    return root


@pytest.fixture
def synthetic_probe(monkeypatch):
    monkeypatch.setattr("lecturehub.index.ingest.probe_media", lambda path, ffprobe=None: MediaMetadata(
        60000, "aac", 16000, 1,
    ))


def counts(connection):
    tables = ("Course", "Schedule", "Lecture", "MediaAsset", "Transcript", "TranscriptSegment",
              "Material", "MaterialMatch", "StudyArtifact", "Evidence", "Event", "IngestItem")
    return {table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] for table in tables}


def test_INGEST_005_index_unicode_paths_and_idempotence(tmp_path, synthetic_probe):
    root = make_root(tmp_path)
    first = index_root(root)
    assert first.stats["media"]["indexed"] == first.stats["transcript"]["indexed"] == 1
    assert first.stats["material"]["indexed"] == first.stats["note"]["indexed"] == 1
    assert first.recording_match_rate == first.transcript_match_rate == 1
    assert first.material_matches == 1
    assert first.failures == []
    with closing(connect(root / "_lecturehub/lecturehub.sqlite")) as connection:
        before = counts(connection)
        identities = {table: connection.execute(f'SELECT id FROM "{table}"').fetchall()
                      for table in ("Lecture", "MediaAsset", "Transcript", "Material", "StudyArtifact")}
        second = index_root(root, connection)
        assert counts(connection) == before
        assert all(connection.execute(f'SELECT id FROM "{table}"').fetchall() == ids for table, ids in identities.items())
        assert connection.execute("SELECT decided_by FROM MaterialMatch").fetchone()[0] == "user"
        assert connection.execute("SELECT COUNT(*) FROM SearchIndex").fetchone()[0] == 2
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert all(stats["indexed"] == 0 for stats in second.stats.values())
    assert verify_sources(root, first.source_hashes) == []


def test_duplicate_hash_and_mislabeled_transcript_preserve_original_ids(tmp_path, synthetic_probe):
    root = make_root(tmp_path)
    path = root / "전사본/합성과목/26.9.22 합성과목.txt"
    duplicate = path.with_name("26.9.22 합성과목 (MAI).txt")
    duplicate.write_bytes(path.read_bytes())
    result = index_root(root)
    assert result.stats["transcript"] == {"total": 2, "indexed": 1, "matched": 2, "duplicate": 1, "held": 0}
    assert result.warnings
    with closing(connect(root / "_lecturehub/lecturehub.sqlite")) as connection:
        assert connection.execute("SELECT engine FROM Transcript").fetchone()[0] == "gemini_transcribe"


def test_missing_ffprobe_and_unknown_metadata_are_held_not_fabricated(tmp_path, monkeypatch):
    root = make_root(tmp_path)
    def unavailable(*args, **kwargs):
        raise MediaProbeError("ffprobe unavailable")
    monkeypatch.setattr("lecturehub.index.ingest.probe_media", unavailable)
    report = index_root(root)
    assert report.stats["media"]["matched"] == 1
    assert report.stats["media"]["indexed"] == 0
    assert any(failure.reason == "ffprobe unavailable" for failure in report.failures)
    with closing(connect(root / "_lecturehub/lecturehub.sqlite")) as connection:
        assert connection.execute("SELECT COUNT(*) FROM MediaAsset").fetchone()[0] == 0
        assert connection.execute("SELECT no_media FROM Transcript").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM IngestItem WHERE state='held'").fetchone()[0] == 1


def test_technical_holds_resolve_after_successful_retry(tmp_path, monkeypatch):
    root = make_root(tmp_path)
    monkeypatch.setattr("lecturehub.index.ingest.probe_media", lambda *args: (_ for _ in ()).throw(
        MediaProbeError("ffprobe unavailable"),
    ))
    index_root(root)
    monkeypatch.setattr("lecturehub.index.ingest.probe_media", lambda *args: MediaMetadata(1000, "aac", 16000, 1))
    index_root(root)
    with closing(connect(root / "_lecturehub/lecturehub.sqlite")) as connection:
        assert connection.execute("SELECT COUNT(*) FROM IngestItem WHERE state='held'").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM Event WHERE type='INGEST_RESOLVED'").fetchone()[0] == 1
        assert connection.execute("SELECT media_id,no_media FROM Transcript").fetchone()[:] == (None, 1)


def test_date_padding_alias_preserves_lecture_and_media_ids(tmp_path, synthetic_probe):
    root = make_root(tmp_path)
    initial = index_root(root)
    original = root / "녹음본/합성과목/26.9.22 합성과목.m4a"
    alias = original.with_name("2026.09.22 합성과목.m4a")
    alias.write_bytes(original.read_bytes())
    report = index_root(root)
    assert report.stats["media"]["duplicate"] == 2
    assert report.stats["media"]["indexed"] == 0
    with closing(connect(root / "_lecturehub/lecturehub.sqlite")) as connection:
        assert connection.execute("SELECT COUNT(*) FROM Lecture").fetchone()[0] == 1
    assert verify_sources(root, initial.source_hashes) == []


def test_recorded_start_conflict_is_held(tmp_path, synthetic_probe):
    root = make_root(tmp_path)
    move_log = root / "녹음본/_이동기록.csv"
    move_log.write_text(
        '이동후,원본,과목,녹음시작\n26.9.22 합성과목.m4a,소리 녹음/합성.m4a,합성과목,2026-09-21 10:00\n',
        encoding="utf-8",
    )
    result = index_root(root)
    assert result.stats["media"]["matched"] == 0
    assert any("conflicts" in failure.reason for failure in result.failures)


def test_unknown_engine_and_source_course_conflict_remain_held(tmp_path, synthetic_probe):
    root = make_root(tmp_path)
    (root / "전사본/합성과목/26.9.22 합성과목.txt").write_text("출처 불명 합성 텍스트", encoding="utf-8")
    (root / "녹음본/합성과목/26.9.23 다른과목.m4a").write_bytes(b"another fake audio")
    report = index_root(root)
    assert report.stats["media"]["matched"] == 1
    assert report.stats["media"]["total"] == 2
    assert report.recording_match_rate == 0.5
    assert any(failure.reason == "transcript engine is unknown" for failure in report.failures)
    assert all("\\" not in failure.filename and "/" not in failure.filename for failure in report.failures)


def test_original_byte_change_is_reported_without_replacing_registered_transcript(tmp_path, synthetic_probe):
    root = make_root(tmp_path)
    first = index_root(root)
    path = root / "전사본/합성과목/26.9.22 합성과목.txt"
    path.write_bytes(path.read_bytes() + "합성 추가 문장".encode())
    second = index_root(root)
    assert any("bytes changed" in failure.reason for failure in second.failures)
    assert verify_sources(root, first.source_hashes) == [path.name]
    with closing(connect(root / "_lecturehub/lecturehub.sqlite")) as connection:
        assert connection.execute("SELECT COUNT(*) FROM Transcript").fetchone()[0] == 1


def test_named_first_period_does_not_collapse_into_numeric_session(tmp_path, synthetic_probe):
    root = make_root(tmp_path)
    (root / "녹음본/합성과목/26.9.22 합성과목 1.m4a").write_bytes(b"synthetic period numeric")
    (root / "녹음본/합성과목/26.9.22 합성과목 1교시.m4a").write_bytes(b"synthetic period label")
    index_root(root)
    with closing(connect(root / "_lecturehub/lecturehub.sqlite")) as connection:
        assert connection.execute("SELECT COUNT(*) FROM Lecture").fetchone()[0] == 3
        assert connection.execute("SELECT COUNT(DISTINCT session_index) FROM Lecture").fetchone()[0] == 3


def test_matching_never_chooses_ambiguous_material_or_week_prefix():
    assert material_ranges("Week 1 2–5쪽", [Path("Week 10 Synthetic.pdf")]) == []
    assert material_ranges("`합성자료` 2–5쪽", [Path("합성자료.pdf"), Path("합성자료.pptx")]) == []
    assert parse_lecture_name("26.9.22 다른과목.txt", "합성과목") is None


def test_match_table_explicit_single_pages_and_mixed_ranges():
    material = Path("합성자료.pdf")
    assert set(material_ranges("`합성자료` 17, 26~32쪽 (합성 숫자 999)", [material])) == {
        (material, 17, 17), (material, 26, 32),
    }
    assert material_ranges("`합성자료` 슬라이드 37", [material]) == [(material, 37, 37)]
    assert material_ranges("`합성자료` 후반 (본문 숫자 17)", [material]) == []
    assert material_ranges("`합성자료.pdf` 2-5", [material]) == [(material, 2, 5)]


def test_abbreviated_note_reference_without_dot_requires_unique_match():
    first = Path("합성과목_2강_가상수로.html")
    second = Path("합성과목_2강_다른합성본.html")
    assert find_note_paths("`합성과목_2강_…html`", [first]) == [first]
    assert find_note_paths("`합성과목_2강_…html`", [first, second]) == []


def test_abbreviated_note_table_resolves_a_previous_hold(tmp_path, synthetic_probe):
    root = make_root(tmp_path)
    original = root / "합성과목/수업 정리 html/합성과목_0922_강의노트.html"
    renamed = original.with_name("합성과목_2강_가상수로.html")
    original.rename(renamed)
    first = index_root(root)
    assert first.stats["note"]["held"] == 1
    table = root / "강의자료_매칭.md"
    table.write_text(table.read_text(encoding="utf-8").replace(
        "합성과목_0922_강의노트.html", "합성과목_2강_…html",
    ), encoding="utf-8")
    second = index_root(root)
    assert second.stats["note"]["indexed"] == 1
    assert second.stats["note"]["held"] == 0
    with closing(connect(root / "_lecturehub/lecturehub.sqlite")) as connection:
        assert connection.execute("SELECT state FROM IngestItem WHERE source_path=?", (str(renamed),)).fetchone()[0] == "confirmed"


@pytest.mark.parametrize("master_present", [True, False])
def test_transcript_links_only_to_a_single_master(tmp_path, synthetic_probe, master_present):
    root = make_root(tmp_path)
    index_root(root)
    new_transcript = root / "전사본/합성과목/26.9.22 합성과목 (MAI).txt"
    new_transcript.write_text("00:01\n[S1] 다른 합성 엔진 발화입니다.\n", encoding="utf-8")
    with closing(connect(root / "_lecturehub/lecturehub.sqlite")) as connection:
        master = dict(connection.execute("SELECT * FROM MediaAsset").fetchone())
        derived = master | {"id": new_ulid(), "is_master": 0, "derived_from": master["id"],
                            "sha256": "synthetic-derived-hash", "path": "synthetic-derived-file"}
        with connection:
            _insert(connection, "MediaAsset", derived)
            if not master_present:
                connection.execute("UPDATE MediaAsset SET lecture_id=NULL WHERE id=?", (master["id"],))
        report = index_root(root, connection)
        mai = connection.execute("SELECT media_id FROM Transcript WHERE engine='mai'").fetchall()
        if master_present:
            assert [row[0] for row in mai] == [master["id"]]
            assert report.stats["transcript"]["indexed"] == 1
        else:
            assert [row[0] for row in mai] == [None]
            assert connection.execute("SELECT no_media FROM Transcript WHERE engine='mai'").fetchone()[0] == 1


def test_same_note_bytes_with_other_lecture_links_remain_held(tmp_path, synthetic_probe):
    root = make_root(tmp_path)
    original = root / "합성과목/수업 정리 html/합성과목_0922_강의노트.html"
    alias = root / "합성과목/합성과목_0923_강의노트.html"
    alias.write_bytes(original.read_bytes())
    report = index_root(root)
    assert report.stats["note"]["indexed"] == 1
    assert any("note hash" in failure.reason for failure in report.failures)


def test_recurring_schedule_notice_uses_registered_per_date_exceptions(tmp_path, synthetic_probe):
    root = make_root(tmp_path)
    path = root / "강의자료_매칭.md"
    path.write_text(path.read_text(encoding="utf-8") + "\n앞으로 수요일은 과제로 대체합니다.\n", encoding="utf-8")
    report = index_root(root)
    assert any("registered per-date ScheduleException" in warning for warning in report.warnings)
    with closing(connect(root / "_lecturehub/lecturehub.sqlite")) as connection:
        assert connection.execute("SELECT COUNT(*) FROM ScheduleException").fetchone()[0] == 0


def test_scanner_excludes_chunks_backups_and_reads_both_note_locations(tmp_path):
    root = make_root(tmp_path)
    (root / "녹음본/_mai조각").mkdir()
    (root / "녹음본/_mai조각/합성 조각.mp3").write_bytes(b"do not index")
    (root / "합성과목/백업").mkdir()
    (root / "합성과목/백업/합성.html").write_text("backup", encoding="utf-8")
    (root / "합성과목/26.9.23_합성.html").write_text("another note", encoding="utf-8")
    found = scan_sources(root, ["합성과목"])
    assert len([source for source in found if source.kind == "note"]) == 2
    assert all("백업" not in source.path.parts and "_mai조각" not in source.path.parts for source in found)


def test_probe_duration_uses_floor_and_rejects_incomplete_metadata(tmp_path, monkeypatch):
    payload = {"streams": [{"codec_name": "aac", "sample_rate": "16000", "channels": 1}],
               "format": {"duration": "89.9999", "tags": {"creation_time": "2026-09-22T01:05:00Z"}}}
    monkeypatch.setattr("lecturehub.index.media.subprocess.run", lambda *args, **kwargs: SimpleNamespace(
        returncode=0, stdout=json.dumps(payload),
    ))
    result = probe_media(tmp_path / "합성 녹음.m4a", "fake-ffprobe")
    assert result.duration_ms == 89999
    assert result.recorded_start is not None
    payload["streams"] = []
    with pytest.raises(MediaProbeError, match="incomplete"):
        probe_media(tmp_path / "합성 녹음.m4a", "fake-ffprobe")


def write_clova(path, export_date, *, title="합성 전사", body="합성 추가 수로 설명입니다."):
    text = f"{title}\n{export_date.replace('-', '.')} 화 오전 10:00 ・ 20분 10초\n합성 작성자\n\n참석자 1 00:01\n{body}\n"
    path.write_bytes(b"\xef\xbb\xbf" + text.encode())


@pytest.mark.parametrize("condition", ["accepted", "date_conflict", "unconfirmed_date", "multiple_lectures"])
def test_confirmed_course_day_requires_export_agreement_and_unique_lecture(tmp_path, synthetic_probe, condition):
    root = make_root(tmp_path)
    table = root / "강의자료_매칭.md"
    table.write_text(table.read_text(encoding="utf-8") +
                     "\n| 합성과목 9/4·9/11 | | 자료 없음 | 사용자 확인 | ◎ | 모두 있음 |\n", encoding="utf-8")
    index_root(root)
    if condition == "multiple_lectures":
        with closing(connect(root / "_lecturehub/lecturehub.sqlite")) as connection, connection:
            course_id = connection.execute("SELECT id FROM Course").fetchone()[0]
            for session in (1, 2):
                _insert(connection, "Lecture", {"id": new_ulid(), "course_id": course_id, "date": "2026-09-04",
                                                "session_index": session, "title": f"26.9.4 합성과목 {session}"})
    filename = "26.9.5 약칭1.txt" if condition == "date_conflict" else "26.9.6 약칭1.txt" if condition == "unconfirmed_date" else "26.9.4 약칭1.txt"
    path = root / "전사본/합성과목" / filename
    day = "2026-09-06" if condition == "unconfirmed_date" else "2026-09-04"
    write_clova(path, day)
    report = index_root(root)
    with closing(connect(root / "_lecturehub/lecturehub.sqlite")) as connection:
        registered = connection.execute("SELECT id,media_id,no_media FROM Transcript WHERE source_file=?", (str(path),)).fetchall()
        if condition == "accepted":
            assert len(registered) == 1 and registered[0]["media_id"] is None and registered[0]["no_media"] == 1
            transcript_id = registered[0]["id"]
            assert connection.execute("SELECT COUNT(*) FROM SearchIndex WHERE source_id=?", (transcript_id,)).fetchone()[0] == 1
            evidence = connection.execute("SELECT detail FROM Evidence WHERE subject_id=? AND source='user'", (transcript_id,)).fetchone()
            detail = json.loads(evidence[0])
            assert detail["decided_by"] == "user"
            assert detail["references"][0]["source_sha256"] == sha256_file(table)
            assert detail["references"][0]["line_start"] > 0
        else:
            assert registered == []
            assert any(failure.filename == filename for failure in report.failures)


def test_clova_title_date_cannot_override_the_actual_export_date(tmp_path, synthetic_probe):
    root = make_root(tmp_path)
    path = root / "전사본/합성과목/26.9.22 합성과목 (클로바).txt"
    write_clova(path, "2026-09-21", title="2026-09-22 합성 제목")
    report = index_root(root)
    assert any(failure.filename == path.name and "export header" in failure.reason for failure in report.failures)


def test_exact_original_move_label_and_move_chain_keep_user_provenance(tmp_path, synthetic_probe):
    root = make_root(tmp_path)
    final = root / "녹음본/합성과목/26.9.22 합성과목.m4a"
    csv_path = root / "녹음본/_이동기록.csv"
    csv_path.write_text(
        "원본,이동후,과목,녹음시작,녹음종료\n"
        "다글로 다운로드: 합성과목 9.22 확인제목.m4a,C:\\합성\\중간.m4a,합성과목,2026-09-22 10:05,2026-09-22 11:13\n"
        f"C:\\합성\\중간.m4a,{final},합성과목,2026-09-22 10:05,2026-09-22 11:13\n",
        encoding="utf-8",
    )
    alias = root / "전사본/합성과목/합성과목 9.22 확인제목.txt"
    alias.write_text("00:01 \n합성 다운로드 제목의 전사입니다.\n", encoding="utf-8")
    index_root(root)
    with closing(connect(root / "_lecturehub/lecturehub.sqlite")) as connection:
        media = connection.execute("SELECT id,origin,device_type,decided_by FROM MediaAsset").fetchone()
        assert media["origin"] == "daglo" and media["device_type"] == "laptop"  # Explicit notebook match-table text.
        assert media["decided_by"] == "user"
        assert connection.execute("SELECT media_id FROM Transcript WHERE source_file=?", (str(alias),)).fetchone()[0] == media["id"]
        details = [json.loads(row[0]) for row in connection.execute(
            "SELECT detail FROM Evidence WHERE subject_id=? AND source='user'", (media["id"],))]
        references = [ref for detail in details for ref in detail.get("references", [])]
        log_refs = [ref for ref in references if ref["source_file"].endswith("_이동기록.csv")]
        assert {ref["line_start"] for ref in log_refs} == {2, 3}
        assert all(ref["source_sha256"] == sha256_file(csv_path) for ref in log_refs)


def test_provider_origin_does_not_fabricate_physical_device(tmp_path, synthetic_probe):
    root = make_root(tmp_path)
    table = root / "강의자료_매칭.md"
    table.write_text(table.read_text(encoding="utf-8").replace("노트북 녹음", "다글로 원본"), encoding="utf-8")
    result = index_root(root)
    assert result.stats["media"]["held"] == 1
    with closing(connect(root / "_lecturehub/lecturehub.sqlite")) as connection:
        assert connection.execute("SELECT COUNT(*) FROM MediaAsset").fetchone()[0] == 0
        assert connection.execute("SELECT no_media FROM Transcript").fetchone()[0] == 1
        reason = json.loads(connection.execute("SELECT detail FROM Evidence WHERE kind='media_link_unresolved'").fetchone()[0])
        assert reason["reason"] == "master_metadata_unconfirmed"
        assert reason["observed_audio_filenames"] == ["26.9.22 합성과목.m4a"]
        assert connection.execute("SELECT COUNT(*) FROM Evidence WHERE kind='recording_absence_confirmed'").fetchone()[0] == 0


def test_only_explicit_course_folder_transcript_is_added(tmp_path, synthetic_probe):
    root = make_root(tmp_path)
    allowed = root / "합성과목/26.9.7 합성과목.txt"
    ignored = root / "합성과목/26.9.8 합성과목.txt"
    write_clova(allowed, "2026-09-07")
    write_clova(ignored, "2026-09-08")
    table = root / "강의자료_매칭.md"
    table.write_text(table.read_text(encoding="utf-8") + "\n사용자가 합성과목\\26.9.7 합성과목.txt에 전사본을 넣었습니다.\n", encoding="utf-8")
    report = index_root(root)
    assert report.stats["transcript"]["total"] == 2
    with closing(connect(root / "_lecturehub/lecturehub.sqlite")) as connection:
        assert connection.execute("SELECT no_media FROM Transcript WHERE source_file=?", (str(allowed),)).fetchone()[0] == 1
        assert connection.execute("SELECT 1 FROM Transcript WHERE source_file=?", (str(ignored),)).fetchone() is None


def test_existing_note_history_and_confirmed_absence_are_explicit_facts(tmp_path, synthetic_probe):
    root = make_root(tmp_path)
    for ordinal in (1, 2):
        (root / f"합성과목/합성_{ordinal}강_기록.html").write_text(
            f'<html><header><div class="meta"><span>2026.09.04</span></div></header>합성 노트 {ordinal}</html>', encoding="utf-8")
    table = root / "강의자료_매칭.md"
    table.write_text(table.read_text(encoding="utf-8") +
                     "\n- 이미 있던 노트: 합성과목 1·2강(9/4).\n"
                     "- 음성이 없는 노트(합성과목 1강)는 보강하지 않았습니다.\n"
                     "- **9/7(월) 합성과목 수업 녹음 없음**.\n"
                     "- ~~9/8(화) 합성과목 수업 녹음 없음~~ → 원본을 받았습니다.\n", encoding="utf-8")
    first = index_root(root)
    assert first.stats["note"]["indexed"] == 3
    with closing(connect(root / "_lecturehub/lecturehub.sqlite")) as connection:
        absent = connection.execute("SELECT l.date,e.detail FROM Evidence e JOIN Lecture l ON l.id=e.subject_id "
                                    "WHERE e.kind='recording_absence_confirmed' AND e.source='user'").fetchall()
        assert {row["date"] for row in absent} == {"2026-09-04", "2026-09-07"}
        assert all(json.loads(row["detail"])["decided_by"] == "user" for row in absent)
        before = counts(connection)
        index_root(root, connection)
        assert counts(connection) == before


@pytest.mark.parametrize("text", ["2026-09-22 13:00대(추정)", "2026-09-22 14:25 이전(추정)", "2026-09-22 14:00~15:15 중 59.5분"])
def test_coarse_time_ranges_are_not_converted_into_invented_start_points(text):
    assert _point_time(text) is None


def test_ambiguous_master_audio_imports_unlinked_raw_transcript(tmp_path, synthetic_probe):
    root = make_root(tmp_path)
    index_root(root)
    additional = root / "전사본/합성과목/26.9.22 합성과목 (MAI).txt"
    additional.write_text("00:01\n[S1] 다중 원음에 대한 합성 전사입니다.\n", encoding="utf-8")
    with closing(connect(root / "_lecturehub/lecturehub.sqlite")) as connection:
        master = dict(connection.execute("SELECT * FROM MediaAsset").fetchone())
        with connection:
            _insert(connection, "MediaAsset", master | {"id": new_ulid(), "sha256": "other-synthetic-master", "path": "other-master.m4a"})
        index_root(root, connection)
        row = connection.execute("SELECT id,media_id,no_media FROM Transcript WHERE engine='mai'").fetchone()
        assert row["media_id"] is None and row["no_media"] == 1
        detail = json.loads(connection.execute("SELECT detail FROM Evidence WHERE kind='media_link_unresolved' AND subject_id=?", (row["id"],)).fetchone()[0])
        assert detail["reason"] == "ambiguous_master" and detail["candidate_count"] == 2


def test_no_media_standard_filename_without_user_confirmation_stays_held(tmp_path, synthetic_probe):
    root = make_root(tmp_path)
    unconfirmed = root / "전사본/합성과목/26.9.24 합성과목.txt"
    write_clova(unconfirmed, "2026-09-24")
    report = index_root(root)
    assert any(failure.filename == unconfirmed.name and "user-confirmed lecture linkage" in failure.reason
               for failure in report.failures)
    with closing(connect(root / "_lecturehub/lecturehub.sqlite")) as connection:
        assert connection.execute("SELECT 1 FROM Transcript WHERE source_file=?", (str(unconfirmed),)).fetchone() is None


def test_no_media_can_reuse_explicit_user_lecture_evidence(tmp_path, synthetic_probe):
    root = make_root(tmp_path)
    index_root(root)
    table = root / "강의자료_매칭.md"
    table.write_text("# 합성 매칭표\n", encoding="utf-8")
    additional = root / "전사본/합성과목/26.9.22 합성과목 (MAI).txt"
    additional.write_text("00:01\n[S1] 기존 확인 근거를 사용하는 합성 전사입니다.\n", encoding="utf-8")
    with closing(connect(root / "_lecturehub/lecturehub.sqlite")) as connection:
        master = dict(connection.execute("SELECT * FROM MediaAsset").fetchone())
        with connection:
            _insert(connection, "MediaAsset", master | {"id": new_ulid(), "sha256": "another-synthetic-master", "path": "another-master.m4a"})
        index_root(root, connection)
        row = connection.execute("SELECT id,no_media FROM Transcript WHERE source_file=?", (str(additional),)).fetchone()
        assert row["no_media"] == 1
        detail = json.loads(connection.execute("SELECT detail FROM Evidence WHERE subject_id=? AND kind='confirmed_source_context'",
                                               (row["id"],)).fetchone()[0])
        assert detail["decided_by"] == "user"
        assert all(ref["inherited_lecture_id"] == master["lecture_id"] and ref["inherited_evidence_id"]
                   for ref in detail["references"])


def test_observed_audio_prevents_absence_even_when_device_metadata_is_held(tmp_path, synthetic_probe):
    root = make_root(tmp_path)
    table = root / "강의자료_매칭.md"
    table.write_text(table.read_text(encoding="utf-8").replace("노트북 녹음", "다글로 원본")
                     + "\n9/22(화) 합성과목 수업 녹음 없음\n", encoding="utf-8")
    report = index_root(root)
    assert report.stats["media"]["held"] == 1
    assert any("absence declaration conflicts with an observed audio" in warning for warning in report.warnings)
    with closing(connect(root / "_lecturehub/lecturehub.sqlite")) as connection:
        assert connection.execute("SELECT 1 FROM Evidence WHERE kind='recording_absence_confirmed'").fetchone() is None


@pytest.mark.parametrize("metadata", ['<div class="meta">2026.09.11</div>', '<p>2026.09.04</p>'])
def test_ordinal_note_absence_requires_agreeing_metadata_not_body_dates(tmp_path, synthetic_probe, metadata):
    root = make_root(tmp_path)
    (root / "합성과목/합성_1강_기록.html").write_text(f"<html>{metadata}</html>", encoding="utf-8")
    table = root / "강의자료_매칭.md"
    table.write_text(table.read_text(encoding="utf-8") +
                     "\n이미 있던 노트: 합성과목 1강(9/4).\n음성이 없는 노트(합성과목 1강)\n", encoding="utf-8")
    report = index_root(root)
    assert any("agreeing HTML date metadata" in warning for warning in report.warnings)
    with closing(connect(root / "_lecturehub/lecturehub.sqlite")) as connection:
        assert connection.execute("SELECT 1 FROM Evidence WHERE kind='recording_absence_confirmed'").fetchone() is None
