"""eTL assignment deadlines fetched with the user's token. A fake transport only; nothing leaves the machine."""
import io
import json
import urllib.error

import pytest

from lecturehub.index import etl_sync

COURSES = [
    {"id": 1, "name": "2026-2 도시계획론 (001)"},
    {"id": 2, "name": "2026-2 수자원설계 (001)"},
    {"id": 3, "name": "2025-2 응용공학수학 2 (001)"},  # another term
    {"id": 4, "name": "2026-2 교양 특강 (001)"},       # not on the timetable
]
ASSIGNMENTS = {
    1: [[{"id": 11, "name": "과제 #2", "due_at": "2026-09-30T14:59:59Z", "published": True, "html_url": "u11"}],
        [{"id": 12, "name": "초안", "due_at": None, "published": True}]],  # second page; no due date
    2: [[{"id": 21, "name": "(관개) 과제제출", "due_at": "2026-10-11T14:59:59Z", "published": True, "html_url": "u21"},
         {"id": 22, "name": "숨김", "due_at": "2026-10-12T14:59:59Z", "published": False}]],
}


def fake(calls):
    def fetch(url, token):
        calls.append((url, token))
        if "/courses?" in url:
            return COURSES, None
        course = int(url.split("/courses/")[1].split("/")[0])
        pages = ASSIGNMENTS.get(course, [[]])
        page = int(url.split("page=")[-1]) if "&page=" in url else 0
        return pages[page], (f"{url.split('&page=')[0]}&page={page + 1}" if page + 1 < len(pages) else None)
    return fetch


@pytest.fixture
def root(tmp_path, monkeypatch):
    folder = tmp_path / "26-2"
    (folder / "_lecturehub").mkdir(parents=True)
    (folder / "시간표.csv").write_text("폴더,과목,요일,시작,종료\n도시계,도시계획론,월,09:30,10:45\n"
                                     "토질역학,수자원설계,화,14:00,17:50\n", encoding="utf-8")
    monkeypatch.setattr(etl_sync, "load_token", lambda: None)
    return folder


def test_this_terms_deadlines_are_mapped_by_the_timetable_in_korean_time(root):
    calls = []
    out = etl_sync.sync(root, token="t", fetch=fake(calls))
    items = json.loads((root / "_lecturehub" / "etl" / "assignments.json").read_text(encoding="utf-8"))
    assert out == {"state": "synced", "count": 2, "unmatched": ["교양 특강"]}
    assert items == [
        {"id": "etl:11", "course": "도시계", "title": "과제 #2", "due": "2026-09-30T23:59", "url": "u11"},
        {"id": "etl:21", "course": "토질역학", "title": "(관개) 과제제출", "due": "2026-10-11T23:59", "url": "u21"}]
    assert all(token == "t" for _, token in calls) and not any("/courses/3/" in url for url, _ in calls)


def test_not_connected_does_nothing_and_a_recent_sync_is_not_repeated(root):
    assert etl_sync.sync(root, fetch=fake([])) == {"state": "not_connected"}
    etl_sync.sync(root, token="t", fetch=fake([]))
    calls = []
    assert etl_sync.sync(root, token="t", fetch=fake(calls))["state"] == "skipped" and calls == []
    assert etl_sync.sync(root, token="t", fetch=fake(calls), force=True)["state"] == "synced"


def test_a_rejected_token_keeps_the_last_good_list(root):
    etl_sync.sync(root, token="t", fetch=fake([]))

    def refused(url, token):
        raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, io.BytesIO(b""))

    out = etl_sync.sync(root, token="old", fetch=refused, force=True)
    assert out["state"] == "error" and "만료" in out["error"]
    state = etl_sync.status(root)
    assert state["count"] == 2 and "만료" in state["error"] and state["error_at"] >= state["ok_at"]
    assert len(json.loads((root / "_lecturehub" / "etl" / "assignments.json").read_text(encoding="utf-8"))) == 2


def test_term_prefix_from_the_semester_folder(tmp_path):
    assert etl_sync.term_prefix(tmp_path / "26-2") == "2026-2"
    assert etl_sync.term_prefix(tmp_path / "2027-1") == "2027-1"
