"""eTL announcements and course files next to what the PC already has (2026-10-11). Synthetic data only."""
import os
import urllib.error
from pathlib import Path

import pytest

from lecturehub.index import etl_files, etl_sync

BASE = etl_sync.BASE


@pytest.fixture
def semester(tmp_path):
    root = tmp_path / "26-2"
    (root / "합성수리" / "강의자료").mkdir(parents=True)
    (root / "_lecturehub").mkdir()
    (root / "시간표.csv").write_text("폴더,과목,요일,시작,끝\n합성수리,합성 구조역학,화,10:00,11:50\n닫힌과목,닫힌 과목,수,10:00,11:50\n",
                                   encoding="utf-8")
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    return root, downloads


def canvas(files):
    def fetch(url, token):
        assert token == "합성토큰"
        if url.startswith(f"{BASE}/api/v1/courses?"):
            return [{"id": 1, "name": "2026-2 합성 구조역학 (001)"}, {"id": 2, "name": "2026-2 닫힌 과목 (001)"},
                    {"id": 3, "name": "2026-2 시간표에 없는 과목 (001)"}, {"id": 4, "name": "2025-2 합성 구조역학 (001)"}], None
        if "/announcements?" in url:
            if "course_2" in url:
                return [], None
            return [{"id": 9, "title": "퀴즈 안내", "posted_at": "2026-10-09T00:57:00Z", "html_url": "u",
                     "message": "<p>16일 수업은 <b>퀴즈</b>가 있습니다.</p><p>16시 15분부터</p>",
                     "attachments": [{"display_name": "안내.pdf"}]}], None
        if url.startswith(f"{BASE}/api/v1/courses/2/files"):
            raise urllib.error.HTTPError(url, 403, "forbidden", None, None)
        if url.startswith(f"{BASE}/api/v1/courses/1/files"):
            return files, None
        raise AssertionError(url)
    return fetch


def listed(file_id, name, size, updated="2026-09-29T01:00:00Z"):
    return {"id": file_id, "display_name": name, "size": size, "updated_at": updated, "url": f"https://myetl.snu.ac.kr/files/{file_id}"}


def test_lists_are_fetched_and_each_file_is_told_apart(semester):
    root, downloads = semester
    course = root / "합성수리"
    (course / "강의자료" / "Week 1.pdf").write_bytes(b"a" * 10)            # same size: have
    (course / "강의자료" / "Week 2 (필기).pdf").write_bytes(b"b" * 20)     # another name, same size: have
    noted = course / "강의자료" / "Week 3.pdf"
    noted.write_bytes(b"c" * 35)                                            # made now (the test's "download")
    os.utime(noted, (noted.stat().st_ctime + 3600, noted.stat().st_ctime + 3600))   # and written on an hour later
    stale = course / "강의자료" / "Week 4.pdf"
    stale.write_bytes(b"d" * 40)                                            # untouched since it was made
    both = course / "강의자료" / "Week 8.pdf"
    both.write_bytes(b"h" * 85)
    os.utime(both, (both.stat().st_ctime + 3600, both.stat().st_ctime + 3600))
    carried = course / "강의자료" / "Week 9.pdf"                            # copied here: written long before it was made
    carried.write_bytes(b"i" * 95)
    os.utime(carried, (1_780_000_000, 1_780_000_000))
    (downloads / "Week 5 (1).pdf").write_bytes(b"e" * 50)                  # downloaded by hand, never filed
    (downloads / "Week 6.pdf").write_bytes(b"x" * 7)                       # same name, another size: not it
    files = [listed(1, "Week 1.pdf", 10), listed(2, "Week 2.pdf", 20), listed(3, "Week 3.pdf", 30),
             listed(4, "Week 4.pdf", 44), listed(5, "Week 5.pdf", 50), listed(6, "Week 6.pdf", 60),
             listed(7, "GIS 자료.zip", etl_files.BIG + 1),
             listed(8, "Week 8.pdf", 80, "2099-01-01T00:00:00Z"), listed(9, "Week 9.pdf", 90)]
    files[3]["updated_at"] = "2099-01-01T00:00:00Z"                         # eTL put up a new Week 4 after my download
    made = etl_files.refresh(root, "합성토큰", canvas(files))
    assert made == {"announcements": 1, "files": 9, "closed": ["닫힌과목"]}
    note = etl_files.announcements(root)[0]
    assert note["course"] == "합성수리" and note["posted"] == "2026-10-09T09:57" and note["attachments"] == ["안내.pdf"]
    assert note["text"] == "16일 수업은 퀴즈가 있습니다.\n16시 15분부터"

    rows = {r["id"]: r for r in etl_files.compare(root, look_in=[downloads])}
    assert [rows[i]["state"] for i in range(1, 8)] == ["have", "have", "changed", "changed", "local", "missing", "missing"]
    # who changed, read from when my file was made, when it was written and when eTL's was put up
    assert [rows[i]["case"] for i in (3, 4, 8, 9)] == ["mine", "etl", "both", "unknown"]
    assert "내가 고친" in rows[3]["hint"] and rows[3]["mine_size"] == 35 and not rows[3]["certain"]
    assert "eTL 자료가 바뀌었습니다" in rows[4]["hint"] and "둘 다" in rows[8]["hint"] and "알 수 없습니다" in rows[9]["hint"]
    assert rows[7]["big"] and not rows[6]["big"]
    board = etl_files.board(root)
    assert board["total"] == 9 and board["have"] == 2 and board["closed"] == ["닫힌과목"]


def test_a_file_is_moved_from_downloads_or_fetched_and_never_written_over(semester):
    root, downloads = semester
    course = root / "합성수리"
    mine = course / "강의자료" / "Week 3.pdf"
    mine.write_bytes(b"c" * 35)
    waiting = downloads / "Week 5 (1).pdf"
    waiting.write_bytes(b"e" * 50)
    files = [listed(3, "Week 3.pdf", 30), listed(5, "Week 5.pdf", 50), listed(6, "과제 안내.hwp", 60),
             listed(7, "GIS 자료.zip", etl_files.BIG + 1), listed(8, "잘린 파일.pdf", 80)]
    etl_files.refresh(root, "합성토큰", canvas(files))
    calls = []

    def download(url, target):
        calls.append(url)
        target.write_bytes(b"z" * (60 if url.endswith("/6") else 30 if url.endswith("/3") else 5))

    moved = etl_files.take(root, 5, download=download, look_in=[downloads])
    assert moved["state"] == "moved" and not waiting.exists() and calls == []
    assert (course / "강의자료" / "Week 5.pdf").read_bytes() == b"e" * 50

    fetched = etl_files.take(root, 6, download=download, look_in=[downloads])
    assert fetched["state"] == "downloaded" and calls == ["https://myetl.snu.ac.kr/files/6"]
    assert (course / "과제" / "과제 안내.hwp").stat().st_size == 60          # filed by its name

    with pytest.raises(ValueError, match="이미"):                            # a changed file is never replaced
        etl_files.take(root, 3, download=download, look_in=[downloads])
    beside = etl_files.take(root, 3, beside=True, download=download, look_in=[downloads])
    assert beside["name"] == "Week 3 (eTL 9.29).pdf" and mine.read_bytes() == b"c" * 35

    with pytest.raises(ValueError, match="큰 파일"):
        etl_files.take(root, 7, download=download, look_in=[downloads])
    with pytest.raises(ValueError, match="크기"):                            # a short download is not filed
        etl_files.take(root, 8, download=download, look_in=[downloads])
    assert not list(course.rglob("잘린 파일.pdf")) and not list((root / "_lecturehub" / "etl" / "tmp").glob("*"))
    with pytest.raises(ValueError):
        etl_files.take(root, 5, download=download, look_in=[downloads])      # already there now
    assert len(etl_files._read(root / "_lecturehub" / "etl" / "placed.json", [])) == 3

    # a file this module placed is judged from the record, not from clock times
    placed = course / "과제" / "과제 안내.hwp"
    placed.write_bytes(b"z" * 59 + b"!!")                                    # I typed into it
    row = next(r for r in etl_files.compare(root, look_in=[downloads]) if r["id"] == 6)
    assert (row["state"], row["case"], row["certain"]) == ("changed", "mine", True)
    etl_files.refresh(root, "합성토큰", canvas([listed(6, "과제 안내.hwp", 64, "2026-10-20T01:00:00Z")]))
    row = next(r for r in etl_files.compare(root, look_in=[downloads]) if r["id"] == 6)
    assert (row["case"], row["certain"]) == ("both", True)


def test_keeping_my_copy_is_remembered_until_etl_changes_again(semester):
    root, downloads = semester
    (root / "합성수리" / "강의자료" / "Week 3.pdf").write_bytes(b"c" * 35)
    etl_files.refresh(root, "합성토큰", canvas([listed(3, "Week 3.pdf", 30)]))
    assert etl_files.keep(root, 3) == {"state": "kept"}
    assert etl_files.compare(root, look_in=[downloads])[0]["state"] == "kept" and etl_files.board(root)["files"] == []
    etl_files.refresh(root, "합성토큰", canvas([listed(3, "Week 3.pdf", 31, "2026-10-20T01:00:00Z")]))
    assert etl_files.compare(root, look_in=[downloads])[0]["state"] == "changed"   # a new upload asks again
    with pytest.raises(ValueError):
        etl_files.keep(root, 999)


def test_names_and_addresses_from_etl_are_not_trusted(semester):
    root, downloads = semester
    files = [listed(1, "..\\..\\밖으로.pdf", 10), listed(2, "하위/폴더.pdf", 10), listed(3, "..", 10),
             listed(4, "CON:.pdf", 10), listed(5, "정상.pdf", 10), listed(6, "http.pdf", 10), listed(7, "남의 서버.pdf", 10),
             listed(8, "파일.pdf", 10)]
    files[4]["url"] = "file:///C:/Windows/win.ini"
    files[5]["url"] = "http://myetl.snu.ac.kr/files/6"
    files[6]["url"] = "https://example.test/files/7"
    etl_files.refresh(root, "합성토큰", canvas(files))
    calls = []

    def download(url, target):
        calls.append(url)
        target.write_bytes(b"z" * 10)

    for file_id in range(1, 8):
        with pytest.raises(ValueError):
            etl_files.take(root, file_id, download=download, look_in=[downloads])
    assert calls == [] and [p.name for p in root.parent.rglob("*.pdf")] == []
    assert not (root / "_lecturehub" / "etl" / "placed.json").exists()
    assert etl_files.take(root, 8, download=download, look_in=[downloads])["state"] == "downloaded"
    with pytest.raises(ValueError):
        etl_files._download("file:///C:/Windows/win.ini", root / "x")
    handler = etl_files._HttpsOnly()
    with pytest.raises(urllib.error.HTTPError):
        handler.redirect_request(None, None, 302, "Found", {}, "http://files.example.test/x")


def test_a_pdf_downloaded_again_over_the_same_name_is_not_read_as_a_new_upload(semester):
    """Measured 2026-10-11: the file's "made" time was older than its content, so eTL looked changed."""
    import pymupdf

    root, downloads = semester
    mine = root / "합성수리" / "강의자료" / "통합.pdf"
    with pymupdf.open() as document:
        document.new_page()
        document.set_metadata({"creationDate": "D:20990101095555+09'00'"})   # the content was made long after the file
        document.save(mine)
    made = mine.stat().st_ctime
    os.utime(mine, (made + 3600, made + 3600))                               # and I wrote on it later
    assert etl_files._pdf_made(mine) and etl_files._pdf_made(root / "시간표.csv") is None
    for uploaded, case in (("2099-01-01T00:56:28Z", "mine"),                 # eTL 09:56:28 KST: 33 s after the PDF was made
                           ("2099-01-03T00:00:00Z", "both")):                # two days later: a real new version
        etl_files.refresh(root, "합성토큰", canvas([listed(1, "통합.pdf", mine.stat().st_size + 5, uploaded)]))
        assert etl_files.compare(root, look_in=[downloads])[0]["case"] == case


def test_audit10_move_never_replaces_a_file_that_appears_meanwhile(tmp_path, monkeypatch):
    src, dest = tmp_path / "a.pdf", tmp_path / "course" / "a.pdf"
    dest.parent.mkdir()
    src.write_bytes(b"new")
    dest.write_bytes(b"appeared")
    with pytest.raises(ValueError, match="덮어쓰지"):
        etl_files._move_no_clobber(src, dest)
    assert dest.read_bytes() == b"appeared" and src.exists()
    real_link = etl_files.os.link

    def cross_drive(a, b):  # the first link (source → target) fails as across drives
        if Path(a) == src:
            raise OSError(18, "cross-device")
        return real_link(a, b)
    monkeypatch.setattr(etl_files.os, "link", cross_drive)
    with pytest.raises(ValueError, match="덮어쓰지"):
        etl_files._move_no_clobber(src, dest)
    assert dest.read_bytes() == b"appeared" and src.exists()
    assert not list(dest.parent.glob(".etl-*"))  # no temp file left behind
    dest.unlink()
    etl_files._move_no_clobber(src, dest)
    assert dest.read_bytes() == b"new" and not src.exists()


def test_take_all_moves_or_fetches_every_missing_file_and_leaves_my_notes_and_big_files(semester):
    """User 2026-11-14: one click for every file that is not in the course folder yet."""
    root, downloads = semester
    course = root / "합성수리"
    (course / "강의자료" / "Week 3.pdf").write_bytes(b"c" * 35)              # my notes on week 3 (another size)
    (downloads / "Week 5.pdf").write_bytes(b"e" * 50)
    files = [listed(3, "Week 3.pdf", 30), listed(5, "Week 5.pdf", 50), listed(6, "Week 6.pdf", 60),
             listed(7, "GIS 자료.zip", etl_files.BIG + 1), listed(8, "잘린 파일.pdf", 80)]
    etl_files.refresh(root, "합성토큰", canvas(files))

    def download(url, target):
        target.write_bytes(b"z" * (60 if url.endswith("/6") else 5))

    result = etl_files.take_all(root, download=download, look_in=[downloads])
    assert (result["moved"], result["downloaded"]) == (1, 1)
    assert {f["name"] for f in result["failed"]} == {"GIS 자료.zip", "잘린 파일.pdf"}
    assert (course / "강의자료" / "Week 3.pdf").read_bytes() == b"c" * 35   # never touched
    assert (course / "강의자료" / "Week 6.pdf").stat().st_size == 60
    again = etl_files.take_all(root, download=download, look_in=[downloads])
    assert again["taken"] == [] and len(again["failed"]) == 2
