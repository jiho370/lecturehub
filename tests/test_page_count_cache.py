"""pdf_page_count reads a PDF once per (path, mtime, size); etl_files._scan lists files in one pass."""
import os

from lecturehub.index import etl_files
from lecturehub.index import materials_pick as mp


def _counting(monkeypatch):
    calls = []

    def fake(path):
        calls.append(path)
        return 7 + len(calls)

    monkeypatch.setattr(mp, "_read_page_count", fake)
    monkeypatch.setattr(mp, "_PAGE_CACHE", {})
    return calls


def test_page_count_cached_until_file_changes(tmp_path, monkeypatch):
    calls = _counting(monkeypatch)
    pdf = tmp_path / "강의 자료 (1).pdf"
    pdf.write_bytes(b"%PDF-1 aaaa")
    assert mp.pdf_page_count(str(pdf)) == 8
    assert mp.pdf_page_count(str(pdf)) == 8
    assert len(calls) == 1

    pdf.write_bytes(b"%PDF-1 aaaaaaaa")  # size changes → read again
    assert mp.pdf_page_count(str(pdf)) == 9
    assert len(calls) == 2

    stat = pdf.stat()  # same size, newer mtime → read again
    os.utime(pdf, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    assert mp.pdf_page_count(str(pdf)) == 10
    assert mp.pdf_page_count(str(pdf)) == 10
    assert len(calls) == 3


def test_page_count_missing_or_not_pdf(tmp_path, monkeypatch):
    calls = _counting(monkeypatch)
    assert mp.pdf_page_count(str(tmp_path / "없음.pdf")) is None
    (tmp_path / "a.pptx").write_bytes(b"x")
    assert mp.pdf_page_count(str(tmp_path / "a.pptx")) is None
    assert calls == []


def test_scan_one_pass(tmp_path):
    (tmp_path / "강의자료" / "sub").mkdir(parents=True)
    (tmp_path / "top.pdf").write_bytes(b"12")
    (tmp_path / "강의자료" / "Week 1 (1).PDF").write_bytes(b"1234")
    (tmp_path / "강의자료" / "sub" / "c.txt").write_bytes(b"1")
    deep = {f.path.name: f for f in etl_files._scan(tmp_path, deep=True)}
    assert set(deep) == {"top.pdf", "Week 1 (1).PDF", "c.txt"}
    assert deep["Week 1 (1).PDF"].size == 4
    assert deep["Week 1 (1).PDF"].key == etl_files._key("week 1.pdf")
    assert [f.path.name for f in etl_files._scan(tmp_path, deep=False)] == ["top.pdf"]
    assert etl_files._scan(tmp_path / "없음", deep=True) == []
