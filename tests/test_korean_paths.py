"""AGENTS.md §3: 한글·공백·괄호가 든 학기 경로, BOM+CRLF 파일, cp949로 깨진 파일 (합성 픽스처만)."""
import json
from contextlib import closing
from datetime import date

import pytest
from typer.testing import CliRunner

from lecturehub.cli import app
from lecturehub.core import db, extra_courses
from lecturehub.core.textio import EncodingSuspectError, read_text_any
from lecturehub.index import calendar_events as cal
from lecturehub.index import index_root, organize
from lecturehub.index.media import MediaMetadata
from lecturehub.parsers.dedupe import sha256_file

COURSE = "기초어학 과목"
BOM = b"\xef\xbb\xbf"


def crlf_bom(text: str) -> bytes:
    return BOM + text.replace("\n", "\r\n").encode("utf-8")


@pytest.fixture
def semester(tmp_path):
    root = tmp_path / "대학 (26-2)"
    for name in (f"녹음본/{COURSE}", f"전사본/{COURSE}", f"{COURSE}/수업 정리 html"):
        (root / name).mkdir(parents=True)
    return root


@pytest.fixture
def synthetic_probe(monkeypatch):
    monkeypatch.setattr("lecturehub.index.ingest.probe_media",
                        lambda path, ffprobe=None: MediaMetadata(60000, "aac", 16000, 1))


def write_sources(root):
    (root / "시간표.csv").write_bytes(crlf_bom(
        f"폴더,과목,요일,시작,종료\n{COURSE},합성 기초어학 (교양),화,10:00,11:50\n{COURSE},합성 기초어학 (교양),금,10:00,11:50\n"))
    (root / "강의자료_매칭.md").write_bytes(crlf_bom(
        "| 녹음 | 녹음 시각 | 자료 (쪽) | 근거 | 확신 | 강의노트 |\n|---|---|---|---|---|---|\n"
        f"| 26.9.22 {COURSE} | 화 10:05~11:13 (노트북 녹음) | Week 4 2–5쪽 (합성 단원) | 합성 | ◎ | `기초어학_0922_강의노트.html` |\n"))
    (root / f"녹음본/{COURSE}/26.9.22 {COURSE}.m4a").write_bytes(b"synthetic audio")
    (root / f"전사본/{COURSE}/26.9.22 {COURSE}.txt").write_bytes(
        crlf_bom("00:01 \n합성 문장 하나입니다.\n00:04 \n합성 문장 둘입니다.\n"))
    (root / f"{COURSE}/Week 4 Synthetic (합성).pdf").write_bytes(b"%PDF synthetic material")
    (root / f"{COURSE}/수업 정리 html/기초어학_0922_강의노트.html").write_bytes(
        crlf_bom("<html><body>합성 강의노트</body></html>\n"))


def test_PATH_001_index_korean_space_paren_root_with_bom_crlf(semester, synthetic_probe):
    write_sources(semester)
    result = index_root(semester)
    assert result.failures == []
    assert result.stats["media"]["indexed"] == result.stats["transcript"]["indexed"] == 1
    assert result.stats["material"]["indexed"] == result.stats["note"]["indexed"] == 1
    with closing(db.connect(semester / "_lecturehub/lecturehub.sqlite")) as connection:
        texts = [row[0] for row in connection.execute("SELECT text FROM TranscriptSegment ORDER BY start_ms")]
        assert texts and not any("﻿" in t or "\r" in t for t in texts)
        assert connection.execute("SELECT folder_name FROM Course").fetchone()[0] == COURSE


def test_PATH_002_cp949_transcript_is_held_with_a_clear_reason_not_garbled(semester, synthetic_probe):
    write_sources(semester)
    path = semester / f"전사본/{COURSE}/26.9.22 {COURSE}.txt"
    path.write_bytes("00:01 \r\n합성 문장입니다.\r\n".encode("cp949"))
    before = path.read_bytes()
    result = index_root(semester)
    assert result.stats["transcript"]["indexed"] == 0
    with closing(db.connect(semester / "_lecturehub/lecturehub.sqlite")) as connection:
        reasons = " ".join(str(row[0]) for row in connection.execute("SELECT suggestion FROM IngestItem"))
        assert connection.execute("SELECT COUNT(*) FROM TranscriptSegment").fetchone()[0] == 0
    assert "cp949" in reasons + " ".join(map(str, result.failures))
    assert path.read_bytes() == before  # 원본은 그대로


def test_PATH_003_textio_bom_crlf_and_cp949(semester):
    good, bad = semester / "좋은 (파일).txt", semester / "깨진 (파일).txt"
    good.write_bytes(crlf_bom("가\n나\n"))
    bad.write_bytes("가나다".encode("cp949"))
    assert read_text_any(good) == "가\n나\n"
    with pytest.raises(EncodingSuspectError, match="cp949"):
        read_text_any(bad)


@pytest.fixture
def library(semester):
    (semester / "_lecturehub").mkdir()
    course = semester / COURSE
    pdf = course / "Week 5 기초어학 (2과).pdf"
    pdf.write_bytes(b"synthetic week5")
    with closing(db.connect(semester / "_lecturehub" / "lecturehub.sqlite")) as connection:
        db.migrate(connection)
        connection.execute("INSERT INTO Course VALUES ('de','DE',?,'de','26-2')", (COURSE,))
        connection.execute("INSERT INTO Material (id,course_id,path,sha256,kind,title,page_count) VALUES (?,?,?,?,?,?,0)",
                           ("w5", "de", str(pdf), sha256_file(pdf), "pdf", pdf.stem))
        connection.commit()
        yield semester, connection, pdf


def test_PATH_004_organize_moves_and_undoes_inside_a_spaced_course_folder(library):
    root, connection, pdf = library
    target = f"{COURSE}/강의자료/{pdf.name}"
    plan = [{"from": f"{COURSE}/{pdf.name}", "to": target}]
    assert organize.check(connection, root, plan) == []
    assert len(organize.apply(connection, root, plan)["moved"]) == 1
    assert (root / target).is_file()
    assert connection.execute("SELECT path FROM Material").fetchone()[0] == str(root / target)
    organize.undo(connection, root)
    assert pdf.is_file()


def test_PATH_005_organize_cli_reads_a_bom_crlf_plan_and_rejects_cp949(library):
    root, connection, pdf = library
    connection.close()
    plan = root / "이동 목록 (확인).json"
    plan.write_bytes(crlf_bom(json.dumps([{"from": f"{COURSE}/{pdf.name}", "to": f"{COURSE}/강의자료/{pdf.name}"}],
                                         ensure_ascii=False, indent=1)))
    runner = CliRunner()
    from lecturehub.cli import _register_all
    _register_all()
    result = runner.invoke(app, ["organize", "apply", str(plan), "--root", str(root), "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "옮길 수 있습니다: 1건" in result.output
    plan.write_bytes(json.dumps([{"from": f"{COURSE}/{pdf.name}", "to": f"{COURSE}/강의자료/x.pdf"}],
                                ensure_ascii=False).encode("cp949"))
    result = runner.invoke(app, ["organize", "apply", str(plan), "--root", str(root), "--dry-run"])
    assert result.exit_code != 0 and "cp949" in result.output
    assert pdf.is_file()


def test_PATH_006_calendar_etl_file_with_bom_crlf_and_cp949(library):
    root, connection, _ = library
    path = root / "_lecturehub" / "과제 목록 (eTL).json"
    path.write_bytes(crlf_bom(json.dumps([{"id": "e1", "course": COURSE, "title": "과제 1 (합성)", "due": "2026-10-05T23:59"}],
                                         ensure_ascii=False, indent=1)))
    assert cal.import_etl(connection, path)["added"] == 1
    path.write_bytes(json.dumps([{"id": "e2", "course": COURSE, "title": "과제 2", "due": "2026-10-06"}],
                                ensure_ascii=False).encode("cp949"))
    with pytest.raises(EncodingSuspectError, match="cp949"):
        cal.import_etl(connection, path)


def test_PATH_007_spoken_dates_in_crlf_text(library):
    found = cal.spoken_dates("다음 주 화요일에 시험 봅니다.\r\n10월 6일 퀴즈", date(2026, 9, 29))
    assert found


def test_PATH_008_extra_course_names_survive_a_bom(semester):
    (semester / "_lecturehub").mkdir()
    extra_courses._path(semester).write_bytes(crlf_bom(json.dumps({"names": ["기초어학 (보충)"]}, ensure_ascii=False)))
    assert extra_courses.names(semester) == ["기초어학 (보충)"]
