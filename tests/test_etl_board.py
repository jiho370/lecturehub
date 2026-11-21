"""eTL course boards (게시판 tab) read with the eTL token, dated posts → calendar candidates (2026-11-20).

Synthetic data only."""
import json
from contextlib import closing
from datetime import date

import pytest

from lecturehub.core import db
from lecturehub.index import calendar_events as cal
from lecturehub.index import etl_board, etl_files, etl_sync

BASE = etl_sync.BASE


@pytest.fixture
def semester(tmp_path):
    root = tmp_path / "26-2"
    (root / "_lecturehub").mkdir(parents=True)
    (root / "시간표.csv").write_text("폴더,과목,요일,시작,끝\n합성역학,합성 역학,화,10:00,11:50\n"
                                   "막힌과목,막힌 과목,수,10:00,11:50\n탭없는과목,탭 없는 과목,목,10:00,11:50\n",
                                   encoding="utf-8")
    return root


def canvas(url, token):
    assert token == "합성토큰"
    if url.startswith(f"{BASE}/api/v1/courses?"):
        return [{"id": 1, "name": "2026-2 합성 역학 (001)"}, {"id": 2, "name": "2026-2 막힌 과목 (001)"},
                {"id": 3, "name": "2026-2 탭 없는 과목 (001)"}], None
    if url == f"{BASE}/api/v1/courses/1/tabs" or url == f"{BASE}/api/v1/courses/2/tabs":
        return [{"id": "context_external_tool_83", "label": "주차학습"},
                {"id": "context_external_tool_85", "label": "게시판"}, {"id": "assignments", "label": "과제"}], None
    if url == f"{BASE}/api/v1/courses/3/tabs":
        return [{"id": "announcements", "label": "공지"}], None
    if url.startswith(f"{BASE}/api/v1/courses/1/users?enrollment_type[]=teacher&enrollment_type[]=ta"):
        return [{"name": "합성교수"}, {"name": "합성조교(학번)"}], None
    if url.startswith(f"{BASE}/api/v1/courses/2/users?"):
        return [], None
    raise AssertionError(url)


BOARDS = [{"id": 10, "title": "Q&A 게시판", "total_post_count": 0},
          {"id": 11, "title": "합성 과제 제출", "total_post_count": 3}]
POSTS = {"items": [
    {"id": 504, "depth": 0, "is_secret": False, "user_name": "합성학생(학번)", "title": "중간고사 풀이_합성학생",
     "created_at": "2026-11-19T05:00:00Z", "content": "<p>11월 30일까지 내는 풀이(합성)</p>"},  # a classmate's submission
    {"id": 505, "depth": 0, "is_secret": False, "is_notice": True, "user_name": "합성대표", "title": "공지로 올린 글",
     "created_at": "2026-11-18T05:00:00Z", "content": "<p>합성</p>"},
    {"id": 501, "depth": 0, "is_secret": False, "user_name": "합성조교(학번)", "title": "합성 상사법칙 과제제출",
     "created_at": "2026-11-19T02:11:12.000000Z", "updated_at": "2026-11-19T02:11:12.000000Z", "attachment_count": 1,
     "content": "<p>네 모형의 물리량 비를 유도해 1인당 1개씩 12월1일까지 제출하세요(합성)</p>"},
    {"id": 502, "depth": 1, "is_secret": False, "user_name": "합성학생", "title": "제출합니다",
     "created_at": "2026-11-20T01:00:00Z", "content": "<p>합성</p>"},
    {"id": 503, "depth": 0, "is_secret": True, "user_name": "합성학생", "title": "비밀글",
     "created_at": "2026-11-20T01:00:00Z", "content": "<p>합성</p>"}]}


def launch(course_id, tool_id, token):
    assert (tool_id, token) == (85, "합성토큰")
    if course_id == 2:
        raise ValueError("합성: 게시판이 열쇠를 주지 않음")

    def get(path):
        if path == "/boards":
            return BOARDS
        if path == "/boards/11/posts/501":
            return {"attachments": [{"id": 1, "filename": "합성 과제 양식.hwp", "filesize": 5, "canvas_file_id": 9001,
                                     "url": f"{BASE}/files/9001/download?verifier=합성"},
                                    {"id": 2, "filename": "파일탭에도있음.pdf", "filesize": 7, "canvas_file_id": 9002,
                                     "url": f"{BASE}/files/9002/download?verifier=합성"}],
                    "comments": [{"user_name": "합성학생", "attachments": [  # a classmate's submission: never listed
                        {"filename": "제출_합성학생.pdf", "filesize": 9, "canvas_file_id": 9003, "url": f"{BASE}/files/9003"}]}]}
        assert path.startswith("/boards/11/posts?"), path  # a board without posts is not asked
        return POSTS
    return get


def test_board_tool_is_the_tab_named_게시판():
    assert etl_board.board_tool([{"id": "context_external_tool_83", "label": "주차학습"},
                                 {"id": "context_external_tool_85", "label": "게시판"}]) == 85
    assert etl_board.board_tool([{"id": "announcements", "label": "게시판"}]) is None


def test_only_professor_or_ta_posts_are_kept_and_one_bad_course_does_not_stop_the_rest(semester):
    found = etl_board.fetch(semester, "합성토큰", canvas, launch)
    # no classmate's submission, reply or secret post; a post marked 공지 stays whoever wrote it
    assert [p["id"] for p in found["posts"]] == ["lxb:501", "lxb:505"]
    post = found["posts"][0]
    assert post["course"] == "합성역학" and post["board"] == "합성 과제 제출" and post["posted"] == "2026-11-19T11:11"
    assert post["text"] == "네 모형의 물리량 비를 유도해 1인당 1개씩 12월1일까지 제출하세요(합성)"
    assert post["url"] == f"{BASE}/courses/1/external_tools/85"
    assert found["missing"] == ["탭없는과목"] and found["failed"] == ["막힌과목(ValueError)"]


def test_refresh_writes_the_list_and_runs_again_only_after_a_day(semester):
    assert etl_board.due(semester)
    result = etl_board.refresh(semester, "합성토큰", canvas, launch)
    assert result["posts"] == 2 and etl_board.posts(semester)[0]["id"] == "lxb:501"
    ok_at = etl_board.status(semester)["ok_at"]
    assert not etl_board.due(semester, ok_at + 23 * 3600) and etl_board.due(semester, ok_at + 24 * 3600)


def test_new_posts_are_told_once_and_shown_until_seen_even_without_files(semester):
    from datetime import datetime

    etl_board.refresh(semester, "합성토큰", canvas, launch)
    now = datetime(2026, 11, 20, 12, 0)
    # 505 has no file and no date: it is news all the same
    assert [p["id"] for p in etl_board.unseen(semester, now)] == ["lxb:501", "lxb:505"]
    assert [p["id"] for p in etl_board.to_tell(semester, now)] == ["lxb:501", "lxb:505"]
    assert etl_board.to_tell(semester, now) == []                       # one Windows notification per post
    etl_board.mark_seen(semester, ["lxb:505"])
    assert [p["id"] for p in etl_board.unseen(semester, now)] == ["lxb:501"]   # the banner keeps the other
    assert etl_board.unseen(semester, datetime(2026, 12, 10)) == []     # two weeks on it is not new any more


@pytest.mark.parametrize(("title", "text", "expected"), [
    ("합성 상사법칙 과제제출", "1인당 1개씩 12월1일까지 제출하세요", [("assignment", "2026-12-01")]),
    ("보고서 안내", "초안은 12/3(목)까지, 최종본은 12월 10일", [("assignment", "2026-12-10"), ("assignment", "2026-12-03")]),
    ("기말고사 안내", "기말고사는 12월 11일 10시입니다", [("exam", "2026-12-11")]),
    ("중간고사 문제풀기 (11월 26일까지)", "풀이를 올리세요", [("assignment", "2026-11-26")]),  # a deadline, not the exam
    ("중간고사 성적 문의", "10월 20일 시험 성적은 조교에게", []),   # a date before the post
    ("자료 올림", "12월 1일 자료입니다", []),                        # no 과제/시험 word
    ("과제 안내", "식 3/4 배를 곱해 구하세요", []),                  # a fraction is not a date
    ("실습자료 및 과제 참고자료", "11.29(일) 까지 제출할 파일", [("assignment", "2026-11-29")]),
    ("설계홍수량 산정 과제", "11/25 (수) 23:59까지 제출", [("assignment", "2026-11-25")]),
    ("과제 안내", "1.5일 정도 걸리는 과제입니다", []),
])
def test_post_dates(title, text, expected):
    found = etl_board.post_dates(title, text, date(2026, 11, 19))
    assert [(kind, day.isoformat()) for kind, day, _ in found] == expected


@pytest.fixture
def library(tmp_path):
    with closing(db.connect(tmp_path / "lecturehub.sqlite")) as connection:
        db.migrate(connection)
        connection.execute("INSERT INTO Course VALUES ('hy','HY','합성역학','hy','26-2')")
        connection.commit()
        yield connection


def test_dated_post_becomes_a_candidate_once_with_the_post_as_evidence(library, semester):
    posts = etl_board.fetch(semester, "합성토큰", canvas, launch)["posts"]
    assert etl_board.import_candidates(library, posts, today=date(2026, 12, 2))["candidates_added"] == 0  # past
    first = etl_board.import_candidates(library, posts, today=date(2026, 11, 20))
    again = etl_board.import_candidates(library, posts, today=date(2026, 11, 20))
    library.commit()
    assert (first["candidates_added"], again["candidates_added"]) == (1, 0)
    [item] = cal.candidates(library)
    assert (item["kind"], item["date"], item["status"], item["source"]) == ("assignment", "2026-12-01", "candidate", "etl")
    assert item["source_ref"] == "lxb:501#12월1일" and item["title"] == "합성 상사법칙 과제제출"
    assert item["evidence"]["post_id"] == "lxb:501" and "12월1일까지" in item["evidence"]["quote"]


def test_no_candidate_when_the_same_deadline_is_already_on_the_calendar(library, semester):
    cal.add(library, kind="assignment", title="상사법칙 과제(직접 입력, 합성)", day="2026-12-01", course_id="hy")
    posts = etl_board.fetch(semester, "합성토큰", canvas, launch)["posts"]
    assert etl_board.import_candidates(library, posts, today=date(2026, 11, 20))["candidates_added"] == 0
    assert cal.candidates(library) == []


def test_board_attachments_are_compared_and_fetched_like_files_tab_files(semester):
    (semester / "합성역학").mkdir()
    etl_board.refresh(semester, "합성토큰", canvas, launch)
    assert [f["id"] for f in etl_board.posts(semester)[0]["files"]] == [9001, 9002]
    etl_sync._write(semester / "_lecturehub" / "etl" / "files.json", {"files": [
        {"id": 9002, "course": "합성역학", "name": "파일탭에도있음.pdf", "size": 7, "url": f"{BASE}/files/9002/x",
         "updated": "2026-11-18T10:00"}], "closed": []})
    rows = {r["id"]: r for r in etl_files.compare(semester, look_in=[])}
    assert set(rows) == {9001, 9002}                      # listed once, the classmate's file not at all
    assert rows[9001]["state"] == "missing" and rows[9001]["where"] == "게시판 · 합성 과제 제출 · 합성 상사법칙 과제제출"
    assert rows[9002]["where"] == ""

    asked = []

    def download(url, target):
        asked.append(url)
        target.write_bytes(b"12345")

    got = etl_files.take(semester, 9001, download=download, look_in=[])
    assert asked == [f"{BASE}/files/9001/download?verifier=합성"] and got["name"] == "합성 과제 양식.hwp"
    assert {r["id"]: r["state"] for r in etl_files.compare(semester, look_in=[])}[9001] == "have"


def test_launch_never_leaves_etl(monkeypatch):
    monkeypatch.setattr(etl_sync, "_http", lambda url, token: ({"url": "https://elsewhere.example/launch"}, None))
    with pytest.raises(ValueError, match="eTL 밖"):
        etl_board._launch(1, 85, "합성토큰")
    action, fields = etl_board._form('<form action="https://myetl.snu.ac.kr/learningx/lti/x" method="POST">'
                                     '<input type="hidden" name="oauth_nonce" value="a&amp;b"><input name="e"></form>')
    assert action == "https://myetl.snu.ac.kr/learningx/lti/x" and fields == {"oauth_nonce": "a&b", "e": ""}
    assert json.dumps(fields)
