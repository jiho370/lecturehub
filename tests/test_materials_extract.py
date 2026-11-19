"""S4-0 (MAT-001~006): page text of materials into MaterialPage; originals only read. All text is synthetic."""
import hashlib
import json
from contextlib import closing

import pytest
from typer.testing import CliRunner

from lecturehub.core import db
from lecturehub.index.search import search
from lecturehub.materials import extract as ex

pymupdf = pytest.importorskip("pymupdf")
pptx = pytest.importorskip("pptx")


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _pdf(path, pages):
    document = pymupdf.open()
    for text in pages:
        page = document.new_page()
        if text:
            page.insert_text((72, 72), text, fontname="korea")
    document.save(path)


def _slides(path, titles, hidden=()):
    presentation = pptx.Presentation()
    for index, title in enumerate(titles):
        slide = presentation.slides.add_slide(presentation.slide_layouts[5])
        slide.shapes.title.text = title
        if index in hidden:
            slide._element.set("show", "0")
    presentation.save(path)


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "학기 (합성)"
    folder = root / "구조역학"
    folder.mkdir(parents=True)
    (root / "_lecturehub").mkdir()
    files = {
        "pdf": folder / "Week 5 합성 관수로.pdf",
        "pptx": folder / "합성 실습 슬라이드.pptx",
        "hwp": folder / "합성 공지.hwp",
    }
    _pdf(files["pdf"], ["관수로 마찰손실 계수 0.025", "", "레이놀즈 수 2300 기준"])
    _slides(files["pptx"], ["첫 합성 슬라이드", "숨긴 합성 슬라이드", "셋째 합성 슬라이드"], hidden={1})
    files["hwp"].write_bytes(b"HWP synthetic")
    path = root / "_lecturehub" / "lecturehub.sqlite"
    db.init_db(path)
    with closing(db.connect(path)) as connection:
        connection.execute("INSERT INTO Course VALUES ('su','SU','구조역학','su','26-2')")
        for mid, key in (("m_pdf", "pdf"), ("m_pptx", "pptx"), ("m_hwp", "hwp")):
            connection.execute("INSERT INTO Material (id,course_id,path,sha256,kind,title,page_count) "
                               "VALUES (?,?,?,?,?,?,0)",
                               (mid, "su", str(files[key]), _digest(files[key]), key, files[key].stem))
        connection.commit()
        yield root, connection, files


def _pages(connection, material_id):
    return [tuple(row) for row in connection.execute(
        "SELECT page_no, text FROM MaterialPage WHERE material_id = ? ORDER BY page_no", (material_id,))]


def test_MAT_001_pdf_pages_keep_numbering_and_empty_pages(library):
    root, connection, files = library
    before = {key: (_digest(p), p.stat().st_mtime_ns) for key, p in files.items()}
    report = ex.extract(connection, root)
    pages = _pages(connection, "m_pdf")
    assert [n for n, _ in pages] == [1, 2, 3] and pages[1][1] == ""  # a page without text keeps its number
    assert "0.025" in pages[0][1] and "2300" in pages[2][1]
    assert connection.execute("SELECT page_count FROM Material WHERE id='m_pdf'").fetchone()[0] == 3
    assert {key: (_digest(p), p.stat().st_mtime_ns) for key, p in files.items()} == before  # originals only read
    item = next(i for i in report.extracted if i["material_id"] == "m_pdf")
    assert (item["pages"], item["empty_pages"], item["source"]) == (3, 1, "pdf")


def test_MAT_002_slides_skip_hidden_like_the_powerpoint_export(library):
    root, connection, _files = library
    ex.extract(connection, root)
    texts = [text for _, text in _pages(connection, "m_pptx")]
    assert texts == ["첫 합성 슬라이드", "셋째 합성 슬라이드"]


def test_MAT_002_cached_powerpoint_export_wins_so_pages_match_chrome(library):
    root, connection, files = library
    from lecturehub.materials.opener import _cache, _key

    _pdf(_cache(root, "pdf") / f"{_key(files['pptx'])}.pdf", ["내보낸 1쪽", "내보낸 2쪽"])
    report = ex.extract(connection, root)
    assert [t for _, t in _pages(connection, "m_pptx")] == ["내보낸 1쪽", "내보낸 2쪽"]
    assert next(i for i in report.extracted if i["material_id"] == "m_pptx")["source"] == "powerpoint_pdf"


def test_MAT_003_hwp_missing_and_changed_files_are_reported_not_guessed(library):
    root, connection, files = library
    connection.execute("INSERT INTO Material (id,course_id,path,sha256,kind,title,page_count) "
                       "VALUES ('m_gone','su',?,'x','pdf','없는 파일',0)", (str(files["pdf"].with_name("없음.pdf")),))
    connection.commit()
    files["pptx"].write_bytes(files["pptx"].read_bytes() + b"\0")  # changed after indexing
    report = ex.extract(connection, root)
    reasons = {item["material_id"]: item["reason"] for item in report.skipped}
    assert "형식" in reasons["m_hwp"] and "파일 없음" == reasons["m_gone"] and "바뀜" in reasons["m_pptx"]
    assert _pages(connection, "m_hwp") == [] and _pages(connection, "m_pptx") == []


def test_MAT_004_idempotent_and_event_logged(library):
    root, connection, _files = library
    ex.extract(connection, root)
    again = ex.extract(connection, root)
    assert again.extracted == []  # nothing new
    events = [json.loads(row[0]) for row in connection.execute(
        "SELECT payload_json FROM Event WHERE type='MATERIAL_TEXT_EXTRACTED'")]
    assert len(events) == 2 and all(e["extractor_version"] == ex.EXTRACTOR_VERSION for e in events)


def test_MAT_005_recording_in_progress_stops_before_the_next_file(library):
    root, connection, _files = library
    calls = iter([None, "20260929_100504_mic"])
    report = ex.extract(connection, root, busy=lambda: next(calls, "20260929_100504_mic"))
    assert len(report.extracted) == 1 and report.skipped[-1]["reason"].startswith("녹음 중")
    assert connection.execute("SELECT count(DISTINCT material_id) FROM MaterialPage").fetchone()[0] == 1


def test_MAT_006_search_finds_page_text_with_page_number(library):
    root, connection, _files = library
    ex.extract(connection, root)
    hits = search(connection, "레이놀즈")
    assert [(h.source_kind, h.source_id, h.page_no) for h in hits] == [("material", "m_pdf", 3)]


def test_MAT_006_cli_extract_reports_counts(library):
    from lecturehub import cli

    root, connection, _files = library
    connection.close()
    cli._register_all()
    result = CliRunner().invoke(cli.app, ["materials", "extract", "--root", str(root)])
    assert result.exit_code == 0, result.output
    assert "자료 2개" in result.output and "건너뜀 1개" in result.output


def test_MAT_006_app_search_hit_carries_path_and_page_for_opening(library):
    """The app opens a page-text hit in the user's PDF program at that page (D-056)."""
    from lecturehub.ui.library import Library

    root, connection, files = library
    ex.extract(connection, root)
    connection.commit()
    result = Library(root).search("레이놀즈")
    assert result["ok"], result
    hit = next(r for r in result["results"] if r["kind"] == "material")
    assert (hit["path"], hit["page"]) == (str(files["pdf"]), 3)
