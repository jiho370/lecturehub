"""eTL submission state, eTL notifications, eTL announcements read by Codex, post-submission check (D-077).

Synthetic data only: a fake Canvas transport, a fake Codex client (``class_notices.FakeText``), a temporary
semester folder. Nothing leaves the machine and no real eTL, token or semester is touched.
"""
from __future__ import annotations

import json
from contextlib import closing
from datetime import date, datetime

import pytest

from lecturehub.core import db
from lecturehub.core.paths import database_path
from lecturehub.index import calendar_events as cal
from lecturehub.index import class_notices as cn
from lecturehub.index import etl_news, etl_notices, etl_submissions, etl_sync

TODAY = date(2026, 11, 20)
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=etl_submissions.KST)
COURSE_A, COURSE_B = "합성과목가", "합성과목나"
SCORE, GRADE = 87.25, "GRADE-MARKER-Q"  # must never appear in a stored file

COURSES = [{"id": 1, "name": f"2026-2 {COURSE_A} (001)"}, {"id": 2, "name": f"2026-2 {COURSE_B} (001)"}]


def _sub(**values):
    """A Canvas submission object with every score/grade field Canvas sends, plus the given values."""
    base = {"workflow_state": "unsubmitted", "submitted_at": None, "missing": False, "late": False, "excused": False,
            "score": SCORE, "grade": GRADE, "entered_score": SCORE, "entered_grade": GRADE, "published_score": SCORE,
            "published_grade": GRADE, "grade_matches_current_submission": True, "grader_id": 4242, "points_deducted": 3,
            "submission_comments": [{"comment": "GRADE-MARKER-Q comment"}], "preview_url": "https://x.invalid/preview"}
    return {**base, **values}


def canvas(assignments):
    def fetch(url, token):
        if "/courses?" in url:
            return COURSES, None
        course = int(url.split("/courses/")[1].split("/")[0])
        return assignments.get(course, []), None
    return fetch


def _assignment(ident, name, due, submission=None, **more):
    item = {"id": ident, "name": name, "due_at": due, "published": True, "html_url": f"https://etl.invalid/a/{ident}",
            "points_possible": 100, **more}
    if submission is not None:
        item["submission"] = submission
    return item


@pytest.fixture
def root(tmp_path, monkeypatch):
    folder = tmp_path / "26-2"
    (folder / "_lecturehub").mkdir(parents=True)
    (folder / "시간표.csv").write_text(f"폴더,과목,요일,시작,종료\n{COURSE_A},{COURSE_A},월,09:30,10:45\n"
                                     f"{COURSE_B},{COURSE_B},화,14:00,15:15\n", encoding="utf-8")
    monkeypatch.setattr(etl_sync, "load_token", lambda: None)
    return folder


def _stored(root):
    return json.loads((root / "_lecturehub" / "etl" / "assignments.json").read_text(encoding="utf-8"))


SET = {
    1: [_assignment(11, "제출한 과제", "2026-11-27T14:59:59Z",
                    _sub(workflow_state="submitted", submitted_at="2026-11-19T12:05:00Z",
                         attachments=[{"display_name": "보고서.pdf", "size": 2048, "url": "https://x.invalid/f"}])),
        _assignment(12, "채점된 과제", "2026-11-25T14:59:59Z", _sub(workflow_state="graded", submitted_at="2026-11-18T01:00:00Z", late=True)),
        _assignment(13, "안 낸 과제", "2026-11-18T14:59:59Z", _sub(missing=True)),
        _assignment(14, "아직 남은 과제", "2026-11-30T14:59:59Z", _sub()),
        _assignment(15, "면제 과제", "2026-11-29T14:59:59Z", _sub(excused=True))],
    2: [_assignment(21, "제출 안 받는 과제", "2026-11-28T14:59:59Z")],  # no submission object at all
}


# ---------------------------------------------------------------- submission state, no grades
def test_ETL_SUB_001_submission_kept_by_whitelist_and_no_grade_or_score_is_stored(root):
    calls = []

    def fetch(url, token):
        calls.append(url)
        return canvas(SET)(url, token)

    etl_sync.sync(root, token="t", fetch=fetch)
    assert all("include[]=submission" in u for u in calls if "/assignments?" in u)
    text = (root / "_lecturehub" / "etl" / "assignments.json").read_text(encoding="utf-8")
    for leak in (GRADE, str(SCORE), "score", "_grade", "grader", "points", "comment", "preview", "x.invalid/f"):
        assert leak not in text, leak  # ("graded" stays: it is Canvas's state name, not a grade)
    items = {i["id"]: i for i in _stored(root)}
    keys = {k for i in items.values() for k in (i.get("submission") or {})}
    assert keys <= {"workflow_state", "submitted_at", "missing", "late", "excused", "files"}
    assert items["etl:11"]["submission"] == {"workflow_state": "submitted", "submitted_at": "2026-11-19T21:05",
                                             "missing": False, "late": False, "excused": False,
                                             "files": [{"name": "보고서.pdf", "size": 2048}]}
    assert "submission" not in items["etl:21"]
    assert {k: etl_submissions.state(items[k].get("submission")) for k in items} == {
        "etl:11": "submitted", "etl:12": "submitted", "etl:13": "missing", "etl:14": "open", "etl:15": "excused",
        "etl:21": "open"}
    # graded without a hand-in time: not handed in, not folded away, says so (integration review)
    graded = {"submission": {"workflow_state": "graded"}}
    assert etl_submissions.state(graded["submission"]) == "graded"
    assert etl_submissions.summary(graded) == {"state": "graded", "label": "채점됨 · 제출 기록 없음", "done": False,
                                               "late": False, "submitted_at": None}


def _library_with(root):
    with closing(db.connect(database_path(root))) as connection:
        db.migrate(connection)
        for cid, name in (("ca", COURSE_A), ("cb", COURSE_B)):
            connection.execute("INSERT INTO Course VALUES (?,?,?,?,'26-2')", (cid, cid.upper(), name, cid))
        cal.import_etl(connection, root / "_lecturehub" / "etl" / "assignments.json")
        connection.commit()


def test_ETL_SUB_002_handed_in_leaves_the_calendar_missing_is_listed_and_late_is_kept(root):
    from lecturehub.ui.library import Library

    etl_sync.sync(root, token="t", fetch=canvas(SET))
    _library_with(root)
    view = Library(root).calendar(2026, 11, TODAY)
    titles = lambda rows: sorted(r["title"] for r in rows)
    assert titles(view["upcoming"]) == ["아직 남은 과제", "제출 안 받는 과제"]
    shown = titles(view["events"]) + titles(view["upcoming"])
    for gone in ("제출한 과제", "채점된 과제", "면제 과제"):
        assert gone not in shown
    assert "아직 남은 과제" in shown and "제출 안 받는 과제" in shown and "안 낸 과제" in titles(view["events"])
    assert titles(view["submitted"]) == ["면제 과제", "제출한 과제", "채점된 과제"]
    late = next(r for r in view["submitted"] if r["title"] == "채점된 과제")
    assert late["submission"]["late"] is True and late["submission"]["label"] == "제출함"
    missing_row = next(r for r in view["events"] if r["title"] == "안 낸 과제")
    # due 11/18, today 11/20: told on its day only, now an ordinary past item (user 2026-11-25)
    assert missing_row["submission"]["state"] == "missing" and missing_row["submission"]["lapsed"] is True
    assert view["missing"] == []
    on_the_day = Library(root).calendar(2026, 11, date(2026, 11, 18))
    assert [m["title"] for m in on_the_day["missing"]] == ["안 낸 과제"]
    assert "lapsed" not in next(r for r in on_the_day["events"] if r["title"] == "안 낸 과제")["submission"]
    assert isinstance(view["etl_at"], float)
    assert "submission" not in next(r for r in view["events"] if r["title"] == "제출 안 받는 과제")


# ---------------------------------------------------------------- post-submission check
def test_ETL_CHECK_001_zero_bytes_and_wrong_extensions_are_named_and_fine_files_are_silent():
    item = {"allowed_extensions": ["pdf", "docx"],
            "submission": {"workflow_state": "submitted", "files": [{"name": "a.pdf", "size": 10},
                                                                  {"name": "b.hwp", "size": 5},
                                                                  {"name": "c.docx", "size": 0}]}}
    assert etl_submissions.check(item) == ["허용되지 않은 형식(pdf, docx만 허용): b.hwp", "0바이트 파일: c.docx"]
    assert etl_submissions.check({"submission": {"files": [{"name": "x.zip", "size": 1}]}}) == []  # no list: any type
    assert etl_submissions.check({"submission": {"workflow_state": "submitted"}}) == []
    assert etl_submissions.check({}) == []


# ---------------------------------------------------------------- notifications
def _write_assignments(root, rows, synced=True):
    """The stored list as a successful fetch with submission states leaves it (``sync.json`` ok_at +
    with_submission: etl_news compares only after such a fetch)."""
    path = root / "_lecturehub" / "etl" / "assignments.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    if synced:
        (path.parent / "sync.json").write_text(json.dumps({"ok_at": NOW.timestamp(), "with_submission": True}),
                                               encoding="utf-8")


def _write_files(root, rows):
    (root / "_lecturehub" / "etl" / "files.json").write_text(json.dumps({"files": rows}, ensure_ascii=False),
                                                              encoding="utf-8")


def _a(ident, due, sub=None, **more):
    row = {"id": ident, "course": COURSE_A, "title": f"합성 {ident}", "due": due, "url": "u", **more}
    if sub is not None:
        row["submission"] = sub
    return row


def test_ETL_NEWS_001_first_look_is_silent_then_each_change_is_told_once(root):
    _write_assignments(root, [_a("etl:1", "2026-11-25T23:59")])
    _write_files(root, [{"id": 7, "course": COURSE_A, "name": "1주차.pdf", "size": 10, "updated": "2026-11-01T10:00"}])
    assert etl_news.collect(root, NOW, look_in=[]) == []  # the first time: what is there is known
    _write_assignments(root, [_a("etl:1", "2026-11-26T23:59"), _a("etl:2", "2026-11-28T23:59"),
                              _a("etl:3", "2026-11-10T23:59")])  # etl:3 is new but past due: not told
    _write_files(root, [{"id": 7, "course": COURSE_A, "name": "1주차.pdf", "size": 10, "updated": "2026-11-01T10:00"},
                        {"id": 8, "course": COURSE_A, "name": "2주차.pdf", "size": 20, "updated": "2026-11-19T10:00"}])
    told = etl_news.collect(root, NOW, look_in=[])
    assert sorted(n["kind"] for n in told) == ["assignment_new", "due_changed", "files_new"]
    changed = next(n for n in told if n["kind"] == "due_changed")
    assert changed["before"] == "2026-11-25T23:59" and "11/25 23:59 → 11/26 23:59" in etl_news.line(changed)
    assert next(n for n in told if n["kind"] == "files_new")["names"] == ["2주차.pdf"]
    assert etl_news.collect(root, NOW, look_in=[]) == []  # told once
    banner = etl_news.unseen(root)
    assert len(banner) == 3 and all(b["line"] for b in banner)
    assert etl_news.mark_seen(root, [banner[0]["key"]]) == 1 and len(etl_news.unseen(root)) == 2


def test_ETL_NEWS_002_switched_off_kinds_are_not_told_and_not_brought_back_later(root):
    _write_assignments(root, [_a("etl:1", "2026-11-25T23:59")])
    etl_news.collect(root, NOW, look_in=[])
    assert etl_news.switches() == {"assignment_new": True, "due_changed": True, "files_new": True}  # all on
    etl_news.set_switch("assignment_new", False)
    with pytest.raises(ValueError):
        etl_news.set_switch("grades", True)
    _write_assignments(root, [_a("etl:1", "2026-11-27T23:59"), _a("etl:2", "2026-11-28T23:59")])
    told = etl_news.collect(root, NOW, look_in=[])
    assert [n["kind"] for n in told] == ["due_changed"]
    etl_news.set_switch("assignment_new", True)
    assert etl_news.collect(root, NOW, look_in=[]) == []  # etl:2 was noted as seen while off


def test_ETL_NEWS_003_a_new_submission_with_a_bad_file_warns_once_and_a_good_one_is_silent(root):
    _write_assignments(root, [_a("etl:1", "2026-11-25T23:59", {"workflow_state": "unsubmitted"}, allowed_extensions=["pdf"]),
                              _a("etl:2", "2026-11-26T23:59", {"workflow_state": "unsubmitted"})])
    etl_news.collect(root, NOW, look_in=[])
    _write_assignments(root, [
        _a("etl:1", "2026-11-25T23:59", {"workflow_state": "submitted", "submitted_at": "2026-11-20T11:00",
                                         "files": [{"name": "답안.hwp", "size": 0}]}, allowed_extensions=["pdf"]),
        _a("etl:2", "2026-11-26T23:59", {"workflow_state": "submitted", "submitted_at": "2026-11-20T11:30",
                                         "files": [{"name": "답안.pdf", "size": 900}]})])
    told = etl_news.collect(root, NOW, look_in=[])
    assert [n["kind"] for n in told] == ["submit_check"] and told[0]["assignment_id"] == "etl:1"
    assert told[0]["problems"] == ["0바이트 파일: 답안.hwp", "허용되지 않은 형식(pdf만 허용): 답안.hwp"]
    for kind in etl_news.SWITCHES:  # the check is not one of the switches: it is always on
        etl_news.set_switch(kind, False)
    assert etl_news.collect(root, NOW, look_in=[]) == []  # the same attempt is not checked again


class _Backend:
    recorder = None

    def __init__(self, phase="idle"):
        self.phase = phase

    def state(self):
        return {"phase": self.phase}


class _Library:
    def __init__(self, root, news=None):
        self.root, self.news, self.calls = root, news or [], 0

    def etl_news_collect(self):
        self.calls += 1
        return self.news


def test_ETL_NEWS_004_nothing_is_collected_or_told_during_a_recording(tmp_path, monkeypatch):
    from lecturehub.ui import app as ui_app
    from lecturehub.ui import notify

    toasts = []
    monkeypatch.setattr(notify, "toast", lambda title, body, icon=None: toasts.append((title, body)))
    item = {"kind": "assignment_new", "course": COURSE_A, "title": "합성 과제", "due": "2026-11-28T23:59", "key": "k"}
    item["line"] = etl_news.line(item)  # as Library.etl_news_collect hands it over
    library = _Library(tmp_path, [item])
    api = ui_app.Api(_Backend("recording"), library)
    api._etl_news()
    assert library.calls == 0 and toasts == []
    api._backend = _Backend("idle")
    api._etl_news()
    assert library.calls == 1 and toasts == [("eTL 새 과제", f"{COURSE_A} · 새 과제: 합성 과제 (11/28 23:59 마감)")]


# ---------------------------------------------------------------- eTL announcements → Codex → calendar candidates
class Grants:
    def __init__(self, *pairs):
        self.pairs = set(pairs)

    def granted(self, provider, course):
        return (provider, course) in self.pairs


ANN_TEXT = "안녕하세요(합성 공지).\n중간 보고서 제출 마감은 12월 4일 23시 59분입니다.\n늦은 제출은 받지 않습니다."


def _announce(root, rows):
    path = root / "_lecturehub" / "etl" / "announcements.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")


def _ann(ident, course, text=ANN_TEXT, posted="2026-11-18T09:00"):
    return {"id": ident, "course": course, "title": f"합성 공지 {ident}", "text": text, "posted": posted,
            "url": f"https://etl.invalid/ann/{ident}", "attachments": []}


def deadline(quote="중간 보고서 제출 마감은 12월 4일 23시 59분입니다.", line="A#2"):
    return {"notices": [{"kind": "assignment", "date": "2026-12-04", "date_precision": "day", "date_text": "12월 4일",
                         "date_reasoning": "공지에 날짜가 적혀 있음", "summary": "중간 보고서 12/4 마감(합성)",
                         "speaker": "professor", "certainty": "high", "stt_note": "", "already_known": False,
                         "quotes": [{"line": line, "text": quote}]}]}


@pytest.fixture
def connection(root):
    with closing(db.connect(root / "_lecturehub" / "lecturehub.sqlite")) as conn:
        db.migrate(conn)
        for cid, name in (("ca", COURSE_A), ("cb", COURSE_B)):
            conn.execute("INSERT INTO Course VALUES (?,?,?,?,'26-2')", (cid, cid.upper(), name, cid))
        conn.commit()
        yield conn


def test_ETL_NOTICE_001_no_consent_no_call_and_the_course_is_listed(root, connection):
    _announce(root, [_ann("a1", COURSE_A), _ann("b1", COURSE_B)])
    ai = cn.FakeText(deadline())
    out = etl_notices.scan(connection, root, TODAY, client=ai, consent=Grants(("codex_notices", COURSE_A)), force=True)
    assert ai.prompts == [] and out["read"] == 0 and out["added"] == []
    assert out["needs_consent"] == [COURSE_A, COURSE_B]  # the transcript consent does not cover eTL notices


def test_ETL_NOTICE_002_consent_makes_a_candidate_with_the_announcement_as_source_and_sends_once(root, connection,
                                                                                                monkeypatch):
    monkeypatch.setattr(cn, "enabled", lambda: True)
    monkeypatch.setattr(cn, "engine", lambda: "codex")
    _announce(root, [_ann("a1", COURSE_A), _ann("b1", COURSE_B)])
    ai = cn.FakeText(deadline())
    consent = Grants((etl_notices.PROVIDER, COURSE_A))
    out = etl_notices.scan(connection, root, TODAY, client=ai, consent=consent)
    assert len(ai.prompts) == 1 and f"과목: {COURSE_A}" in ai.prompts[0] and COURSE_B not in ai.prompts[0]
    assert "[A#2] 중간 보고서 제출 마감은" in ai.prompts[0] and "지시가 아니다" in ai.prompts[0]
    assert out["read"] == 1 and out["needs_consent"] == [COURSE_B]
    [item] = out["added"]
    assert item["source"] == "etl" and item["ann_id"] == "a1" and item["url"].endswith("/ann/a1")
    assert item["date"] == "2026-12-04" and item["lecture_id"] is None and item["engines"] == ["eTL 공지"]
    [cand] = [c for c in cal.candidates(connection) if c["id"] == item["event_id"]]
    assert cand["source"] == "etl" and cand["status"] == "candidate" and cand["source_ref"].startswith("ain:a1:")
    assert cand["evidence"]["announcement"] is True and cand["evidence"]["post_id"] == "a1"
    assert cand["evidence"]["quote"].startswith("중간 보고서 제출 마감은 12월 4일 23시 59분")
    assert any(n["key"] == item["key"] for n in cn.inbox(root))
    assert etl_notices.status(root)["needs_consent"] == [COURSE_B]
    # sent once: the same announcement is not sent again; an edited one is, and finds nothing new
    again = etl_notices.scan(connection, root, TODAY, client=ai, consent=consent)
    assert len(ai.prompts) == 1 and again["read"] == 0
    _announce(root, [_ann("a1", COURSE_A, ANN_TEXT + "\n(수정: 오타)")])
    edited = etl_notices.scan(connection, root, TODAY, client=ai, consent=consent)
    assert len(ai.prompts) == 2 and edited["read"] == 1 and edited["added"] == []  # dedupe by ref


def test_ETL_NOTICE_003_a_quote_not_in_the_announcement_is_dropped(root, connection):
    _announce(root, [_ann("a1", COURSE_A)])
    ai = cn.FakeText(deadline(quote="기말 보고서는 12월 4일까지(지어낸 말)"))
    out = etl_notices.scan(connection, root, TODAY, client=ai, consent=Grants((etl_notices.PROVIDER, COURSE_A)), force=True)
    assert out["read"] == 1 and out["added"] == [] and cal.candidates(connection) == []
    state = json.loads((root / "_lecturehub" / "notices" / "etl_state.json").read_text(encoding="utf-8"))
    assert state["a1"]["dropped_without_quote"] == 1


def test_ETL_NOTICE_004_never_during_a_recording_and_a_cancel_reads_it_again_next_round(root, connection, monkeypatch):
    monkeypatch.setattr(cn, "enabled", lambda: True)
    monkeypatch.setattr(cn, "engine", lambda: "codex")
    _announce(root, [_ann("a1", COURSE_A)])
    consent = Grants((etl_notices.PROVIDER, COURSE_A))
    ai = cn.FakeText(deadline())
    out = etl_notices.scan(connection, root, TODAY, client=ai, consent=consent, busy=lambda: True)
    assert out.get("recording") is True and ai.prompts == []

    class Cancelled(cn.FakeText):
        def generate_json(self, prompt, schema, *, max_posts=None, may_send=None):
            self.prompts.append(prompt)
            raise cn.Stopped("녹음이 시작되어 멈췄습니다")

    stopped = Cancelled()
    out = etl_notices.scan(connection, root, TODAY, client=stopped, consent=consent)
    assert out.get("stopped") and len(stopped.prompts) == 1 and out["added"] == []
    later = etl_notices.scan(connection, root, TODAY, client=ai, consent=consent)  # no wait after a cancel
    assert len(ai.prompts) == 1 and len(later["added"]) == 1


def test_ETL_NOTICE_005_off_or_google_reads_nothing_and_old_posts_are_left(root, connection, monkeypatch):
    _announce(root, [_ann("a1", COURSE_A), _ann("old", COURSE_A, posted="2026-09-01T09:00")])
    consent = Grants((etl_notices.PROVIDER, COURSE_A))
    ai = cn.FakeText(deadline())
    monkeypatch.setattr(cn, "enabled", lambda: True)
    monkeypatch.setattr(cn, "engine", lambda: "google")  # no fallback to Google: nothing at all
    assert etl_notices.scan(connection, root, TODAY, client=ai, consent=consent).get("off") and ai.prompts == []
    monkeypatch.setattr(cn, "enabled", lambda: False)
    monkeypatch.setattr(cn, "engine", lambda: "codex")
    assert etl_notices.scan(connection, root, TODAY, client=ai, consent=consent).get("off") and ai.prompts == []
    monkeypatch.setattr(cn, "enabled", lambda: True)
    etl_notices.scan(connection, root, TODAY, client=ai, consent=consent)
    assert len(ai.prompts) == 1 and "합성 공지 a1" in ai.prompts[0]  # the post older than 28 days is not sent


# ---------------------------------------------------------------- consent tokens for the new card
class _ConsentBackend:
    recorder = None

    def __init__(self):
        self.one, self.many = [], []

    def state(self):
        return {"phase": "idle"}

    def etl_notices_allow(self, course):
        self.one.append(course)
        return {"ok": True}

    def notices_allow_many(self, courses):
        self.many.append(("transcripts", list(courses)))
        return {"ok": True}

    def etl_notices_allow_many(self, courses):
        self.many.append(list(courses))
        return {"ok": True}


class _Window:
    def __init__(self):
        self.asked = []

    def create_confirmation_dialog(self, title, message):
        self.asked.append((title, message))
        return True


def test_ETL_NOTICE_006_the_card_stores_consent_only_with_its_own_dialog_token(tmp_path):
    from lecturehub.ui.app import Api

    backend = _ConsentBackend()
    api = Api(backend, _Library(tmp_path))
    api._main = _Window()
    assert "codex_etl_notices" in Api.MANY_PROVIDERS
    assert api.etl_notices_allow(COURSE_A)["needs_confirm"] and backend.one == []  # straight from page JS
    wrong = api.consent_confirm("codex_notices", COURSE_A)["token"]
    assert api.etl_notices_allow(COURSE_A, wrong)["ok"] is False  # the transcript card's token does not work
    q = api.consent_confirm("codex_etl_notices", COURSE_A)
    title, message = api._main.asked[-1]
    assert "eTL 공지" in title and "첨부" in message and "녹음" in message
    assert api.etl_notices_allow(COURSE_A, q["token"]) == {"ok": True} and backend.one == [COURSE_A]
    many = api.consent_confirm_many("codex_etl_notices", [COURSE_A, COURSE_B])
    assert api.notices_allow_all([COURSE_A, COURSE_B], many["token"])["ok"] is False  # another card
    many = api.consent_confirm_many("codex_etl_notices", [COURSE_A, COURSE_B])
    assert api.etl_notices_allow_all([COURSE_B, COURSE_A], many["token"]) == {"ok": True}
    assert backend.many == [[COURSE_B, COURSE_A]]


# ---------------------------------------------------------------- integration review (one test per finding)
def test_ETL_SUB_003_missing_wins_and_graded_without_a_hand_in_stays_on_the_calendar(root):
    """Finding 1: graded without submitted_at counted as handed in (folded away), and missing came after graded."""
    from lecturehub.ui.library import Library

    assert etl_submissions.state({"workflow_state": "graded", "missing": True}) == "missing"
    assert etl_submissions.state({"workflow_state": "graded", "submitted_at": None}) == "graded"
    assert etl_submissions.state({"workflow_state": "graded", "submitted_at": "2026-11-18T10:00"}) == "submitted"
    graded = {1: [_assignment(31, "채점만 된 과제", "2026-11-27T14:59:59Z", _sub(workflow_state="graded")),
                  _assignment(32, "누락 채점 과제", "2026-11-18T14:59:59Z", _sub(workflow_state="graded", missing=True))]}
    etl_sync.sync(root, token="t", fetch=canvas(graded))
    _library_with(root)
    view = Library(root).calendar(2026, 11, TODAY)
    upcoming = {r["title"]: r for r in view["upcoming"]}
    assert upcoming["채점만 된 과제"]["submission"]["label"] == "채점됨 · 제출 기록 없음"
    assert view["missing"] == [] and view["submitted"] == []  # due passed before today: no longer told (user 11/25)
    on_the_day = Library(root).calendar(2026, 11, date(2026, 11, 18))  # 2026-11-18T14:59:59Z is 11/18 23:59 in Korea
    assert [m["title"] for m in on_the_day["missing"]] == ["누락 채점 과제"] and on_the_day["submitted"] == []


def test_ETL_NEWS_005_no_baseline_before_a_good_fetch_and_a_new_course_starts_silent(root):
    """Finding 2: a baseline was taken from an empty or old-format list (then everything was "new"), and a newly
    matched course told all of its assignments at once."""
    seen = root / "_lecturehub" / "etl" / "news_seen.json"
    base = [_a("etl:1", "2026-11-25T23:59")]
    _write_assignments(root, base, synced=False)
    assert etl_news.collect(root, NOW, look_in=[]) == [] and not seen.exists()  # eTL never fetched
    (root / "_lecturehub" / "etl" / "sync.json").write_text(json.dumps({"ok_at": NOW.timestamp()}), encoding="utf-8")
    assert etl_news.collect(root, NOW, look_in=[]) == [] and not seen.exists()  # a fetch without submission states
    handed = _a("etl:2", "2026-11-26T23:59", {"workflow_state": "submitted", "submitted_at": "2026-11-19T10:00",
                                               "files": [{"name": "x.hwp", "size": 0}]}, allowed_extensions=["pdf"])
    _write_assignments(root, base + [handed])
    assert etl_news.collect(root, NOW, look_in=[]) == [] and seen.exists()  # the first good fetch: all known
    other = {**_a("etl:9", "2026-11-29T23:59"), "course": COURSE_B}
    _write_assignments(root, base + [handed, _a("etl:3", "2026-11-28T23:59"), other])
    told = etl_news.collect(root, NOW, look_in=[])
    assert [(n["kind"], n["assignment_id"]) for n in told] == [("assignment_new", "etl:3")]  # COURSE_B: silent
    _write_assignments(root, base + [handed, _a("etl:3", "2026-11-28T23:59"), other,
                                     {**_a("etl:10", "2026-11-30T23:59"), "course": COURSE_B}])
    assert [n["assignment_id"] for n in etl_news.collect(root, NOW, look_in=[])] == ["etl:10"]


def test_ETL_CHECK_002_an_unknown_size_is_not_called_zero_bytes():
    """Finding 3: a file without a size was stored as 0 and warned about as an empty file."""
    kept = etl_submissions.clean({"workflow_state": "submitted", "submitted_at": "2026-11-19T12:05:00Z",
                                  "attachments": [{"display_name": "a.pdf"}, {"display_name": "b.pdf", "size": "?"},
                                                  {"display_name": "c.pdf", "size": 0}]})
    assert [f["size"] for f in kept["files"]] == [None, None, 0]
    assert etl_submissions.check({"submission": kept}) == ["0바이트 파일: c.pdf"]


def test_ETL_NEWS_006_broken_files_do_not_stop_it_and_news_is_saved_before_the_seen_record(root, monkeypatch):
    """Finding 7: a state file of the wrong shape stopped the notifications for good, and news_seen.json was written
    before news.json (a crash in between lost the news)."""
    etl = root / "_lecturehub" / "etl"
    _write_assignments(root, [_a("etl:1", "2026-11-25T23:59")])
    (etl / "news_seen.json").write_text("[1, 2]", encoding="utf-8")
    assert etl_news.collect(root, NOW, look_in=[]) == []  # read as no record: a fresh baseline, no error
    (etl / "news.json").write_text(json.dumps([1, {"key": 5}, {"key": "old", "found_at": "x", "kind": "x"}]),
                                   encoding="utf-8")
    assert [(n["key"], n["found_at"]) for n in etl_news.inbox(root)] == [("old", 0)]
    _write_assignments(root, [_a("etl:1", "2026-11-25T23:59"), _a("etl:2", "2026-11-28T23:59")])
    real = etl_news._write

    def crash(path, data):
        if path.name == "news_seen.json":
            raise OSError("disk full")
        real(path, data)

    monkeypatch.setattr(etl_news, "_write", crash)
    with pytest.raises(OSError):
        etl_news.collect(root, NOW, look_in=[])
    assert [n.get("assignment_id") for n in etl_news.inbox(root)] == ["etl:2"]  # saved first
    monkeypatch.setattr(etl_news, "_write", real)
    assert etl_news.collect(root, NOW, look_in=[]) == []  # not told a second time
    assert [n.get("assignment_id") for n in etl_news.inbox(root)] == ["etl:2"]


def test_ETL_NOTICE_007_the_date_must_be_the_written_one_and_a_short_quote_is_not_enough(root, connection, monkeypatch):
    """Finding 5 (and 9: in the normal background mode, no force): the quote check did not tie the date to the
    announcement, and a two-word quote passed."""
    monkeypatch.setattr(cn, "enabled", lambda: True)
    monkeypatch.setattr(cn, "engine", lambda: "codex")
    _announce(root, [_ann("a1", COURSE_A)])
    wrong_day = deadline()["notices"][0] | {"date": "2026-12-11"}  # the quote and date_text say 12월 4일
    short = deadline(quote="12월 4일")["notices"][0]  # in the line, but shorter than MIN_QUOTE
    assert len("12월 4일") < etl_notices.MIN_QUOTE
    ai = cn.FakeText({"notices": [wrong_day, short]})
    out = etl_notices.scan(connection, root, TODAY, client=ai, consent=Grants((etl_notices.PROVIDER, COURSE_A)))
    assert len(ai.prompts) == 1 and out["read"] == 1 and out["added"] == [] and cal.candidates(connection) == []
    state = json.loads((root / "_lecturehub" / "notices" / "etl_state.json").read_text(encoding="utf-8"))
    assert state["a1"]["dropped_without_quote"] == 2


def test_ETL_NOTICE_008_a_few_per_round_an_edited_title_is_read_again(root, connection, monkeypatch):
    """Finding 8: turning it on sent four weeks of announcements at once; the consent text names the retries and
    that an edited title or text is sent again."""
    from lecturehub.transcription.consent import NOTICES

    monkeypatch.setattr(cn, "enabled", lambda: True)
    monkeypatch.setattr(cn, "engine", lambda: "codex")
    many = [_ann(f"n{i}", COURSE_A, text=f"합성 공지 {i}번째 글입니다.") for i in range(etl_notices.PER_ROUND + 2)]
    _announce(root, many)
    consent = Grants((etl_notices.PROVIDER, COURSE_A))
    ai = cn.FakeText({"notices": []})
    first = etl_notices.scan(connection, root, TODAY, client=ai, consent=consent)
    assert len(ai.prompts) == etl_notices.PER_ROUND and first["more"] is True
    second = etl_notices.scan(connection, root, TODAY, client=ai, consent=consent)
    assert len(ai.prompts) == etl_notices.PER_ROUND + 2 and "more" not in second
    many[0] = {**many[0], "title": "합성 공지 n0 (제목 고침)"}
    _announce(root, many)
    third = etl_notices.scan(connection, root, TODAY, client=ai, consent=consent)
    assert len(ai.prompts) == etl_notices.PER_ROUND + 3 and third["read"] == 1 and "제목 고침" in ai.prompts[-1]
    assert "최대 3번" in NOTICES["codex_etl_notices"] and "제목이나 본문이 고쳐지면" in NOTICES["codex_etl_notices"]


@pytest.mark.parametrize("provider", ["codex_notices", "codex_etl_notices", "ai_app_link"])
def test_ETL_NOTICE_009_text_to_openai_is_allowed_course_by_course_even_in_the_full_edition(tmp_path, monkeypatch,
                                                                                            provider):
    """Finding 8: the two notice readers took a standing all-courses consent (D-062) in the full edition."""
    from lecturehub.transcription.consent import CONFIRMED, ConsentStore

    monkeypatch.setenv("LECTUREHUB_EDITION", "full")
    store = ConsentStore(tmp_path / "consent.json")
    with pytest.raises(ValueError, match="과목마다"):
        store.grant_all(provider, 1, "user", "합성")
    (tmp_path / "consent.json").write_text(json.dumps({provider: {"*": {"authority": CONFIRMED}}}), encoding="utf-8")
    assert not store.granted(provider, COURSE_A)  # an entry written some other way does not count
    store.grant_all("whisper_groq", 1, "user", "합성")  # other providers keep D-062
    assert store.granted("whisper_groq", COURSE_A)
