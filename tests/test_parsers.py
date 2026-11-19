"""PARSE-001~005 and TIME-003; all transcript sentences are synthetic."""

from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from lecturehub.core.textio import EncodingSuspectError
from lecturehub.parsers import TranscriptParseError, clova, daglo, gemini_transcribe, mai, parse_file
from lecturehub.parsers.cli import register
from lecturehub.parsers.detect import detect_engine

FIXTURES = Path(__file__).parent / "fixtures" / "transcripts"


@pytest.mark.parametrize(
    ("fixture", "engine", "starts", "speakers"),
    [
        ("daglo.txt", "daglo", [1000, 62500, 3602000], [None, None, None]),
        ("clova.txt", "clova", [1000, 64000], ["참석자 1", "참석자 2"]),
        ("gemini_transcribe.txt", "gemini_transcribe", [2000, 67500], [None, None]),
        ("mai.txt", "mai", [1000, 61500, 1800000], ["S1", "S2", "S1"]),
    ],
    ids=["PARSE-001", "PARSE-002", "PARSE-003", "PARSE-004"],
)
def test_engine_segment_count_and_timestamps(fixture, engine, starts, speakers):
    parsed = parse_file(FIXTURES / fixture)
    assert parsed.engine == engine
    assert len(parsed.segments) == len(starts)
    assert [segment.start_ms for segment in parsed.segments] == starts
    assert [segment.speaker for segment in parsed.segments] == speakers
    assert all(segment.end_ms is None for segment in parsed.segments)
    assert all(segment.text and "clovanote.naver.com" not in segment.text for segment in parsed.segments)


@pytest.mark.parametrize("prefix", ["", "\ufeff"])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("parser", [daglo.parse, clova.parse, gemini_transcribe.parse, mai.parse])
def test_PARSE_005_parsers_accept_bom_and_line_endings(prefix, newline, parser):
    text = prefix + newline.join(["00:01 ", "합성 첫 문장입니다.", "", "01:02 ", "합성 마지막 문장입니다."])
    result = parser(text)
    assert [segment.start_ms for segment in result] == [1000, 62000]
    assert result[0].text == "합성 첫 문장입니다."


def test_PARSE_005_cp949_is_an_explicit_error(tmp_path):
    path = tmp_path / "한글 (합성 cp949).txt"
    path.write_bytes("00:01 \r\n합성 전사입니다.\r\n".encode("cp949"))
    with pytest.raises(EncodingSuspectError):
        parse_file(path)
    with pytest.raises(EncodingSuspectError, match="cp949"):
        detect_engine(path.read_bytes())


@pytest.mark.parametrize("encoding", ["utf-16", "utf-32"])
@pytest.mark.parametrize("engine", ["clova", "mai"])
def test_PARSE_005_explicit_unicode_bom_preserves_structural_detection(tmp_path, encoding, engine):
    path = tmp_path / "합성 유니코드 전사본.txt"
    text = (FIXTURES / f"{engine}.txt").read_text(encoding="utf-8-sig")
    path.write_bytes(text.encode(encoding))
    assert parse_file(path).engine == engine


def test_PARSE_005_utf16_generic_timestamps_remain_unknown(tmp_path):
    path = tmp_path / "합성 출처 불명.txt"
    path.write_bytes("00:01 \r\n합성 발화.\r\n".encode("utf-16"))
    assert parse_file(path).engine == "unknown"


@pytest.mark.parametrize("label", ["Speaker 1 0:01", "Speaker 1\n0:01", "Speaker 12 00:01"])
def test_TIME_003_speaker_label_removed_before_time(label):
    segments = mai.parse(f"{label}\n합성 발화입니다.\n")
    assert segments[0].start_ms == 1000  # Never 10:01 / 601000 ms.
    assert segments[0].text == "합성 발화입니다."
    assert segments[0].speaker in {"S1", "S12"}
    assert detect_engine(f"{label}\n합성 발화입니다.\n".encode()) == "mai"


def test_PARSE_004_standalone_mai_speaker_and_same_timestamp_speakers():
    segments = mai.parse("00:01\n[S1]\n합성 질문입니다.\n[S2] 합성 답입니다.\n00:04\n[S1] 다음 발화.")
    assert [(s.start_ms, s.speaker) for s in segments] == [(1000, "S1"), (1000, "S2"), (4000, "S1")]
    assert segments[0].text == "합성 질문입니다."


@pytest.mark.parametrize("body", ["[S1] 합성 문장.", "[S1]\n합성 문장."])
def test_PARSE_004_blank_lines_before_speaker_do_not_add_empty_segments(tmp_path, body):
    path = tmp_path / "빈 줄 있는 합성 MAI.txt"
    path.write_text(f"00:01\n\n{body}\n", encoding="utf-8")
    result = parse_file(path)
    assert result.engine == "mai"
    assert len(result.segments) == 1
    assert result.segments[0].speaker == "S1"
    assert result.segments[0].text == "합성 문장."


def test_PARSE_002_clova_export_without_speaker_labels(tmp_path):
    text = "합성 수업\n2026.09.01 화 오전 10:00 ・ 47분 34초\n합성 작성자\n\n01:59\n합성 문장.\n\nclovanote.naver.com"
    path = tmp_path / "꼬리표 없는 전사본.txt"
    path.write_bytes(b"\xef\xbb\xbf" + text.encode())
    parsed = parse_file(path)
    assert parsed.engine == "clova"
    assert parsed.segments[0].start_ms == 119000
    assert parsed.segments[0].speaker is None
    assert parsed.segments[0].text == "합성 문장."


def test_format_evidence_overrides_filename_and_unknown_is_preserved(tmp_path):
    mislabeled = tmp_path / "합성 강의 (MAI).txt"
    mislabeled.write_bytes((FIXTURES / "daglo.txt").read_bytes())
    assert parse_file(mislabeled).engine == "daglo"
    unknown = tmp_path / "합성 강의 (Transcribe).txt"
    unknown.write_text("00:01\n출처를 알 수 없는 합성 문장.\n", encoding="utf-8")
    parsed = parse_file(unknown)
    assert parsed.engine == "unknown"
    assert parsed.segments == ()


def test_parser_preserves_source_order_and_does_not_invent_end_times():
    result = daglo.parse("01:00 \n합성 먼저.\n00:30 \n합성 뒤로 간 시각.")
    assert [segment.start_ms for segment in result] == [60000, 30000]
    assert all(segment.end_ms is None for segment in result)


def test_malformed_timestamp_is_clear_without_disclosing_transcript():
    with pytest.raises(TranscriptParseError, match="invalid timestamp at line 1") as raised:
        daglo.parse("00:99 \n합성 비공개 문장.")  # real exports put the time alone on its line
    assert "비공개" not in str(raised.value)
    with pytest.raises(TranscriptParseError, match="no timestamped segments") as raised:
        daglo.parse("00:99 합성 비공개 문장.")  # K1: time + words on one line is body text
    assert "비공개" not in str(raised.value)


def test_PARSE_cli_summary_does_not_print_transcript_and_leaves_source_unchanged(tmp_path):
    path = tmp_path / "한글 폴더 (합성)" / "합성 (전사본).txt"
    path.parent.mkdir()
    original = (FIXTURES / "daglo.txt").read_bytes()
    path.write_bytes(original)
    app = typer.Typer()
    app.callback()(lambda: None)
    register(app)
    result = CliRunner().invoke(app, ["parse", str(path)])
    assert result.exit_code == 0, result.output
    assert "engine: daglo" in result.output
    assert "segments: 3" in result.output
    assert "first: 00:01" in result.output
    assert "last: 1:00:02" in result.output
    assert "가상" not in result.output
    assert path.read_bytes() == original


def test_PARSE_cli_reports_encoding_error(tmp_path):
    path = tmp_path / "합성 cp949.txt"
    path.write_bytes("합성 텍스트".encode("cp949"))
    app = typer.Typer()
    app.callback()(lambda: None)
    register(app)
    result = CliRunner().invoke(app, ["parse", str(path)])
    assert result.exit_code == 1
    assert "Error:" in result.output


@pytest.mark.parametrize("engine", ["daglo", "gemini_transcribe", "mai", "clova"])
def test_K1_body_line_starting_with_a_time_is_not_a_timestamp(engine):
    """K1: "10:30 에 다시 모입니다" is speech, not a new segment; time must never run backwards."""
    from lecturehub.parsers import _blocks

    label = {"mai": "[S1] ", "clova": ""}.get(engine, "")
    stamp = (lambda t: f"참석자 1 {t}") if engine == "clova" else (lambda t: f"{t} ")
    text = (f"{stamp('00:01')}\n{label}합성 문장입니다.\n10:30 에 다시 모입니다.\n"
            f"{stamp('00:05')}\n{label}다음 합성 문장.\n")
    segments = _blocks.parse_blocks(text, engine="mai" if engine == "mai" else engine)
    assert [s.start_ms for s in segments] == [1000, 5000]
    assert "10:30 에 다시 모입니다." in segments[0].text


def test_K1_parse_check_reports_changed_transcripts_without_touching_them(tmp_path):
    """§0.3: a parser fix is checked against stored results; raw file and DB rows stay as they are."""
    import sqlite3
    from contextlib import closing

    from lecturehub import cli
    from lecturehub.core import db

    root = tmp_path / "학기 (합성)"
    source = root / "전사본" / "합성.txt"
    source.parent.mkdir(parents=True)
    source.write_text("00:01 \n합성 문장입니다.\n10:30 에 다시 모입니다.\n00:05 \n다음 합성 문장.\n", encoding="utf-8")
    path = root / "_lecturehub" / "lecturehub.sqlite"
    db.init_db(path)
    with closing(db.connect(path)) as connection:
        connection.execute("INSERT INTO Course VALUES ('course','TEST','합성','su','26-2')")
        connection.execute("INSERT INTO Lecture VALUES ('lecture','course','2026-09-22',1,NULL,NULL)")
        connection.execute("INSERT INTO Transcript (id,lecture_id,media_id,engine,role,role_decided_by,status,"
                           "source_file,sha256) VALUES ('t','lecture',NULL,'daglo','timeline','policy','done',?,'h')",
                           (str(source),))
        for seq, (start, text) in enumerate([(1000, "합성 문장입니다."), (630000, "에 다시 모입니다."),
                                             (5000, "다음 합성 문장.")]):  # what the old parser stored
            connection.execute("INSERT INTO TranscriptSegment (transcript_id,seq,start_ms,text,chunk_index) "
                               "VALUES ('t',?,?,?,0)", (seq, start, text))
        connection.commit()
    cli._register_all()
    raw = source.read_bytes()
    result = CliRunner().invoke(cli.app, ["parse-check", "--root", str(root), "--record"])
    assert result.exit_code == 2 and "changed 1" in result.output and "t" in result.output.split(":")[-1]
    assert source.read_bytes() == raw
    with closing(sqlite3.connect(path)) as check:
        assert check.execute("SELECT count(*) FROM TranscriptSegment WHERE transcript_id='t'").fetchone()[0] == 3
        assert check.execute("SELECT count(*) FROM Event WHERE type='parser_revalidated'").fetchone()[0] == 1
