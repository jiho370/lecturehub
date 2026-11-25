"""과목별 시험 페이지 링크(P-045): 주소·파일 허용 범위, 원자적 저장, 여는 방식, 쓰기 범위, Api 연결, 앱 화면.

실제 브라우저는 열지 않는다(popen·which·isfile·open_default를 가짜로 바꾼다). 주소는 합성 값이다.
"""
import json
from pathlib import Path

import pytest

from lecturehub.ui import exam_pages as ep
from lecturehub.ui import browser_links as ml
from lecturehub.ui.app import Api
from lecturehub.ui.exam_pages import ExamPages

GOOD = "https://claude.ai/artifact/TestOnly_exam-123"
GOOD2 = "https://claude.ai/code/artifact/0b1c2d3e-aaaa-bbbb"
WIN_ENV = {"ProgramFiles(x86)": r"C:\Program Files (x86)", "ProgramFiles": r"C:\Program Files",
           "LOCALAPPDATA": r"C:\Users\학생 (합성)\AppData\Local"}
EDGE = str(Path(WIN_ENV["ProgramFiles(x86)"]).joinpath("Microsoft", "Edge", "Application", "msedge.exe"))
COURSE = "경제학원론 (합성)"
WEB = Path(__file__).resolve().parents[1] / "src" / "lecturehub" / "ui" / "web"


@pytest.fixture
def root(tmp_path):
    path = tmp_path / "대학" / "26-2 (합성) 폴더"
    (path / COURSE / "시험대비").mkdir(parents=True)
    (path / COURSE / "시험대비" / "1-5장 문제.html").write_text("<p>합성</p>", encoding="utf-8")
    return path


class Popen:
    def __init__(self):
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append(args)


def make(root, files=(EDGE,), default_ok=True):
    popen, opened, ids = Popen(), [], iter(f"id{n}" for n in range(100))

    def open_default(url):
        opened.append(url)
        return default_ok

    pages = ExamPages(root, env=WIN_ENV, which=lambda name: None, isfile=lambda p: p in files, popen=popen,
                      open_default=open_default, new_id=lambda: next(ids))
    return pages, popen, opened


def test_EXP_001_artifact_link_is_stored_under_lecturehub_exam_and_opens_in_an_app_window(root):
    pages, popen, _ = make(root)
    r = pages.add(COURSE, f"  {GOOD}\n", "기말 대비")
    assert r["ok"] and r["pages"] == [{"id": "id0", "title": "기말 대비", "url": GOOD, "kind": "artifact",
                                       "target": GOOD, "missing": False}]
    stored = json.loads((root / "_lecturehub" / "exam" / "pages.json").read_text(encoding="utf-8"))
    assert stored == {"version": 1, "courses": {COURSE: [{"id": "id0", "title": "기말 대비", "url": GOOD}]}}
    assert pages.open(COURSE, "id0") == {"ok": True, "browser_found": "edge", "title": "기말 대비"}
    assert popen.calls == [[EDGE, f"--app={GOOD}"]]


@pytest.mark.parametrize("target", [
    "javascript:alert(1)", "http://claude.ai/artifact/abc", "https://evil.example/artifact/abc",
    "https://claude.ai/chat/abc", f"{GOOD} --foo", "file:///C:/Windows/win.ini",
])
def test_EXP_002_other_addresses_are_refused(root, target):
    pages, _, _ = make(root)
    assert not pages.add(COURSE, target)["ok"]
    assert not (root / "_lecturehub" / "exam" / "pages.json").exists()


def test_EXP_003_local_pages_must_be_html_files_inside_the_semester_root(root, tmp_path):
    pages, popen, _ = make(root)
    outside = tmp_path / "밖.html"
    outside.write_text("x", encoding="utf-8")
    (root / COURSE / "메모.txt").write_text("x", encoding="utf-8")
    for bad in (str(outside), f"{COURSE}/../../밖.html", f"{COURSE}/메모.txt", f"{COURSE}/없는.html", ""):
        assert not pages.add(COURSE, bad)["ok"], bad
    page = root / COURSE / "시험대비" / "1-5장 문제.html"
    r = pages.add(COURSE, f'"{page}"')  # "경로로 복사" adds quotes
    assert r["ok"] and r["pages"][0]["title"] == "1-5장 문제" and r["pages"][0]["path"] == f"{COURSE}/시험대비/1-5장 문제.html"
    assert pages.add(COURSE, f"{COURSE}\\시험대비\\1-5장 문제.html")["error"] == "이미 넣은 시험 페이지예요"  # relative, same file
    assert pages.open(COURSE, "id0")["ok"] and popen.calls == [[EDGE, f"--app={page.resolve().as_uri()}"]]
    page.unlink()  # moved or deleted later: shown as missing, not opened
    assert pages.pages(COURSE)["pages"][0]["missing"]
    assert not pages.open(COURSE, "id0")["ok"] and len(popen.calls) == 1


def test_EXP_004_limits_and_removing(root, monkeypatch):
    pages, _, _ = make(root)
    monkeypatch.setattr(ep, "MAX_LINKS", 2)
    assert pages.add(COURSE, GOOD, "가" * 100)["pages"][0]["title"] == "가" * 60
    assert not pages.add(COURSE, GOOD)["ok"]                                     # same address twice
    assert pages.add(COURSE, GOOD2)["ok"]
    assert "2개까지" in pages.add(COURSE, f"{COURSE}/시험대비/1-5장 문제.html")["error"]
    assert not pages.add("", GOOD)["ok"]
    assert not pages.remove(COURSE, "nope")["ok"]
    assert [x["id"] for x in pages.remove(COURSE, "id0")["pages"]] == ["id1"]
    assert pages.remove(COURSE, "id1")["pages"] == []
    stored = json.loads((root / "_lecturehub" / "exam" / "pages.json").read_text(encoding="utf-8"))
    assert stored["courses"] == {}                                                # an empty course is dropped
    assert sorted(p.name for p in (root / "_lecturehub" / "exam").iterdir()) == ["pages.json"]  # no temp file left


def test_EXP_005_hand_edited_junk_is_ignored_and_open_rechecks(root, monkeypatch):
    folder = root / "_lecturehub" / "exam"
    folder.mkdir(parents=True)
    (folder / "pages.json").write_bytes(b"\xef\xbb\xbf" + json.dumps({"version": 1, "courses": {
        COURSE: [{"id": "a", "title": "좋음", "url": GOOD}, {"id": "b", "url": "javascript:alert(1)"},
                 {"id": "c", "path": "../../밖.html"}, {"id": "d", "path": f"{COURSE}/메모.txt"}, "junk", {"title": "id 없음", "url": GOOD}],
        "다른 과목": "junk"}}, ensure_ascii=False).encode("utf-8"))
    pages, popen, _ = make(root)
    assert [x["id"] for x in pages.pages(COURSE)["pages"]] == ["a"]
    assert pages.pages()["courses"].keys() == {COURSE}
    monkeypatch.setattr(pages, "_load", lambda: {COURSE: [{"id": "a", "title": "x", "url": "--foo"}]})
    assert not pages.open(COURSE, "a")["ok"] and popen.calls == []
    (folder / "pages.json").write_text("{깨짐", encoding="utf-8")
    assert make(root)[0].pages() == {"ok": True, "courses": {}}


def test_EXP_006_default_browser_when_no_edge_or_chrome(root):
    pages, popen, opened = make(root, files=())
    pages.add(COURSE, GOOD)
    assert pages.open(COURSE, "id0")["browser_found"] == "default" and opened == [GOOD] and popen.calls == []
    failing, _, _ = make(root, files=(), default_ok=False)
    assert not failing.open(COURSE, "id0")["ok"]


def test_EXP_007_writes_only_inside_lecturehub_exam(root):
    before = {p for p in root.parent.rglob("*")}
    pages, _, _ = make(root)
    pages.add(COURSE, GOOD)
    pages.add(COURSE, f"{COURSE}/시험대비/1-5장 문제.html")
    pages.open(COURSE, "id1")
    pages.remove(COURSE, "id0")
    added = {p.relative_to(root) for p in root.parent.rglob("*")} - {p.relative_to(root) for p in before if p.is_relative_to(root)}
    assert added == {Path("_lecturehub"), Path("_lecturehub/exam"), Path("_lecturehub/exam/pages.json")}


def test_EXP_008_api_adds_links_to_the_exam_board_and_delegates(root, monkeypatch):
    class FakeBackend:
        recording = False

    class FakeLibrary:
        def exam_board(self, today):
            return {"ok": True, "today": today.isoformat(), "courses": [{"folder": COURSE}, {"folder": "다른 과목"}]}

    FakeBackend.root = root
    calls = []
    monkeypatch.setattr(ep.subprocess, "Popen", lambda args, **kw: calls.append(args))
    monkeypatch.setattr(ml.shutil, "which", lambda n: None)
    monkeypatch.setattr(ml.os.path, "isfile", lambda p: p == EDGE)
    monkeypatch.setattr(ml.os, "environ", dict(WIN_ENV))
    api = Api(FakeBackend(), FakeLibrary())
    added = api.exam_page_add(COURSE, GOOD, "기말")
    assert added["ok"]
    board = api.exam_board()
    assert [len(c["pages"]) for c in board["courses"]] == [1, 0] and board["courses"][0]["pages"][0]["title"] == "기말"
    link = added["pages"][0]["id"]
    assert api.exam_page_open(COURSE, link)["ok"] and calls == [[EDGE, f"--app={GOOD}"]]
    assert api.exam_page_remove(COURSE, link)["pages"] == []


def test_EXP_009_exam_screen_links_pages_and_has_no_practice_screen():
    app = (WEB / "app.js").read_text(encoding="utf-8")
    body = app[app.index("function openExam("):app.index("function openCalendar(")]
    for gone in ("quiz_items", "quiz_answer", "data-quiz", "문제 풀기", "renderQuiz"):
        assert gone not in body, gone
    for kept in ("exam_page_open", "exam_page_add", "exam_page_remove", "exam_page_pick", "시험 페이지",
                 "대비 자료", "수업 중 발언", "openLecture"):
        assert kept in body, kept
    assert body.index("시험 일정") < body.index("pagesSection(c)") < body.index("대비 자료")
    css = (WEB / "app.css").read_text(encoding="utf-8")
    assert ".qinput{" not in css and ".exrow.page{" in css
