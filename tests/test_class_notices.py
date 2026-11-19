"""Schedule notices heard in class, read by AI from the whole transcript (user request 2026-10-30).

All text is synthetic; the AI is a fake that returns a prepared answer and sends nothing anywhere. Where the
real client (``GeminiText``) is used it talks to ``httpx.MockTransport``: no network. Waits are checked with an
injected clock, never by sleeping.
"""
import json
from contextlib import closing
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from lecturehub.core import db
from lecturehub.index import calendar_events as cal
from lecturehub.index import class_notices as cn
from lecturehub.index import schedule

SAID = "이 시험을 보죠. 그쵸? 그리고 그 주 금요일도 제가 학기가 있어서 11월에 보게 될 거예요.(합성)"
SAID_GOOGLE = "그리고 보도 금요일도 제가 확인해 있어서 11월에 보게 될 거예요(합성)"


class Consent:
    def __init__(self, ok=True):
        self.ok = ok

    def granted(self, provider, course):
        return self.ok and provider == "gemini_transcribe"


def closure_answer(line="E1#0", text="그 주 금요일도 제가 학기가 있어서", day="2026-10-30"):
    return {"notices": [{
        "kind": "closure", "date": day, "date_precision": "day", "date_text": "그 주 금요일",
        "date_reasoning": "시험 주(10/26~) 금요일 = 10/30", "summary": "학회로 10/30(금) 휴강, 11월에 다시 수업",
        "speaker": "professor", "certainty": "medium", "stt_note": "E1 '학기'는 맥락상 '학회'로 추정",
        "already_known": False, "quotes": [{"line": line, "text": text}]}]}


class Clock:
    """The clock ``scan`` is given for its waits (epoch milliseconds); it moves only when a test moves it."""

    def __init__(self, at=1_792_000_000_000):
        self.at = at

    def __call__(self):
        return self.at

    def minutes(self, minutes):
        self.at += minutes * 60_000

    def hours(self, hours):
        self.minutes(hours * 60)


def answer_body(answer):
    return {"candidates": [{"content": {"parts": [{"text": json.dumps(answer, ensure_ascii=False)}]}}]}


GOOD = (200, answer_body({"notices": []}))
BLOCKED = (200, {"promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}})
CUT_OFF = (200, {"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": [{"text": '{"notices": [{"ki'}]}}]})
ERROR_PAGE = (200, "<html>login to the network (synthetic)</html>")
BUSY = (503, {"error": {"message": "high demand (synthetic)"}})
QUOTA = (429, {"error": {"message": "quota exceeded, limit per day (synthetic)"}})


def gemini(replies):
    """The real client over MockTransport. ``replies``: (status, body) pairs or exceptions, the last one repeats;
    or a function of the prompt. Returns (make_client, posts): each call of make_client is a fresh client, as
    after an app restart, and ``posts`` lists every request the transport got, with its model and prompt."""
    posts = []

    def handler(request):
        prompt = json.loads(request.content)["contents"][0]["parts"][0]["text"]
        posts.append({"model": request.url.path.rsplit("/", 1)[-1].split(":")[0], "prompt": prompt})
        reply = replies(prompt) if callable(replies) else replies[min(len(posts), len(replies)) - 1]
        if isinstance(reply, Exception):
            raise reply
        status, body = reply
        return httpx.Response(status, content=body.encode("utf-8")) if isinstance(body, str) \
            else httpx.Response(status, json=body)

    return (lambda: cn.GeminiText("synthetic-key", http=httpx.Client(transport=httpx.MockTransport(handler)))), posts


def add_lecture(connection, lecture_id, day, transcript_id, text=SAID):
    connection.execute("INSERT INTO Lecture VALUES (?,'sr',?,1,NULL,NULL)", (lecture_id, day))
    add_transcript(connection, lecture_id, transcript_id, text)


def add_transcript(connection, lecture_id, transcript_id, text=SAID):
    connection.execute("INSERT INTO Transcript (id,lecture_id,media_id,engine,role,role_decided_by,status,"
                       "source_file,sha256) VALUES (?, ?, NULL, 'mai', 'timeline', 'policy', 'done', ?, ?)",
                       (transcript_id, lecture_id, transcript_id, transcript_id))
    connection.execute("INSERT INTO TranscriptSegment (transcript_id,seq,start_ms,text,chunk_index) "
                       "VALUES (?,0,0,?,0)", (transcript_id, text))
    connection.commit()


def notice_file(root, name):
    return root / "_lecturehub" / "notices" / name


def put(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def library(tmp_path):
    with closing(db.connect(tmp_path / "lecturehub.sqlite")) as connection:
        db.migrate(connection)
        connection.execute("INSERT INTO Course VALUES ('sr','SR','구조역학','sr','26-2')")
        connection.execute("INSERT INTO Schedule (course_id, weekday, start, end) VALUES ('sr', 1, 37800000, 42300000)")  # Tue 10:30–11:45
        connection.execute("INSERT INTO Schedule (course_id, weekday, start, end) VALUES ('sr', 4, 37800000, 42300000)")  # Fri
        connection.execute("INSERT INTO Lecture VALUES ('l16','sr','2026-10-16',1,NULL,NULL)")
        for tid, engine, text in (("t1", "mai", SAID), ("t2", "gemini_transcribe", SAID_GOOGLE)):
            connection.execute("INSERT INTO Transcript (id,lecture_id,media_id,engine,role,role_decided_by,status,"
                               "source_file,sha256) VALUES (?, 'l16', NULL, ?, 'timeline', 'policy', 'done', ?, ?)",
                               (tid, engine, tid, tid))
            connection.execute("INSERT INTO TranscriptSegment (transcript_id,seq,start_ms,text,chunk_index) "
                               "VALUES (?,0,0,?,0)", (tid, text))
        connection.commit()
        yield connection


def test_notice_without_the_word_closure_becomes_candidate_and_inbox_entry(library, tmp_path):
    """NOTICE-001: no '휴강' word and a misheard 학회 — the AI reading of the context still yields a candidate."""
    ai = cn.FakeText(closure_answer())
    result = cn.scan(library, tmp_path, date(2026, 10, 20), client=ai, consent=Consent())
    library.commit()
    assert result["read"] == 1 and len(result["added"]) == 1
    item = result["added"][0]
    assert item["date"] == "2026-10-30" and item["kind"] == "closure" and not item["seen"]
    candidate = cal.candidates(library)[0]
    assert candidate["kind"] == "closure" and candidate["status"] == "candidate" and candidate["source"] == "transcript"
    assert "확인 필요" in candidate["note"] and "학회" in candidate["note"]
    assert candidate["evidence"]["quote"] == "그 주 금요일도 제가 학기가 있어서"  # the transcript's own text
    reading = json.loads(library.execute("SELECT detail FROM Evidence WHERE kind='ai_reading'").fetchone()[0])
    assert reading["certainty"] == "medium" and reading["date_reasoning"]
    assert [i["key"] for i in cn.inbox(tmp_path, unseen_only=True)] == [item["key"]]
    # both engines and the timetable go to the AI, with the lecture's weekday
    prompt = ai.prompts[0]
    assert "2026-10-16(금)" in prompt and "화 10:30–11:45" in prompt and "[E1#0 00:00]" in prompt and "[E2#0" in prompt


def test_same_transcripts_are_not_read_twice_and_a_new_engine_is(library, tmp_path):
    ai = cn.FakeText(closure_answer())
    cn.scan(library, tmp_path, date(2026, 10, 20), client=ai, consent=Consent())
    cn.scan(library, tmp_path, date(2026, 10, 20), client=ai, consent=Consent())
    assert len(ai.prompts) == 1
    library.execute("INSERT INTO Transcript (id,lecture_id,media_id,engine,role,role_decided_by,status,source_file,sha256)"
                    " VALUES ('t3','l16',NULL,'whisper','timeline','policy','done','t3','t3')")
    library.execute("INSERT INTO TranscriptSegment (transcript_id,seq,start_ms,text,chunk_index) VALUES "
                    "('t3',0,0,'그 주 금요일도 제가 학회가 있어서(합성)',0)")
    again = cn.scan(library, tmp_path, date(2026, 10, 20), client=ai, consent=Consent())
    assert len(ai.prompts) == 2 and again["added"] == []  # read again, but the same notice is not added twice
    assert len(cal.candidates(library)) == 1


def test_quote_not_in_transcript_is_thrown_away(library, tmp_path):
    """AGENTS 8: no quote, no output."""
    ai = cn.FakeText(closure_answer(text="다음 주는 휴강입니다"))
    result = cn.scan(library, tmp_path, date(2026, 10, 20), client=ai, consent=Consent())
    assert result["added"] == [] and cal.candidates(library) == []
    state = json.loads((tmp_path / "_lecturehub" / "notices" / "state.json").read_text(encoding="utf-8"))
    assert state["l16"]["dropped_without_quote"] == 1


def test_quote_with_other_spacing_or_wrong_line_is_found_as_written(library, tmp_path):
    ai = cn.FakeText(closure_answer(line="E2#7", text="보도 금요일도 제가, 확인해있어서"))
    cn.scan(library, tmp_path, date(2026, 10, 20), client=ai, consent=Consent())
    assert cal.candidates(library)[0]["evidence"]["quote"] == "보도 금요일도 제가 확인해 있어서"


def test_no_consent_sends_nothing(library, tmp_path):
    ai = cn.FakeText(closure_answer())
    result = cn.scan(library, tmp_path, date(2026, 10, 20), client=ai, consent=Consent(ok=False))
    assert ai.prompts == [] and result["skipped"] and result["read"] == 0


def test_what_the_calendar_already_has_is_not_added_or_announced(library, tmp_path):
    cal.add(library, kind="closure", title="구조역학 휴강", day="2026-10-30", course_id="sr")
    result = cn.scan(library, tmp_path, date(2026, 10, 20), client=cn.FakeText(closure_answer()), consent=Consent())
    assert result["added"] == [] and cal.candidates(library) == []


def test_past_notice_is_kept_but_not_announced_and_far_or_earlier_dates_have_no_day(library, tmp_path):
    result = cn.scan(library, tmp_path, date(2026, 11, 2), client=cn.FakeText(closure_answer()), consent=Consent())
    assert result["added"][0]["seen"] is True  # 10/30 is past on 11/2
    assert cn.inbox(tmp_path, unseen_only=True) == []


def test_undated_notice_goes_to_inbox_only(library, tmp_path):
    answer = closure_answer(day=None)
    answer["notices"][0]["date_precision"] = "unknown"
    result = cn.scan(library, tmp_path, date(2026, 10, 20), client=cn.FakeText(answer), consent=Consent())
    assert result["added"][0]["date"] is None and result["added"][0]["event_id"] is None
    assert cal.candidates(library) == []


def test_failed_reading_is_tried_again_later_but_not_in_the_next_round(library, tmp_path):
    class Down:  # keeps no count of its requests: taken as one request per call
        model = "x"

        def generate_json(self, prompt, schema, **_):
            raise cn.NoticeError("일일 한도(429)")

    clock, today = Clock(), date(2026, 10, 20)
    result = cn.scan(library, tmp_path, today, client=Down(), consent=Consent(), now_ms=clock)
    assert result["errors"] and result["read"] == 0
    assert cn.usage(tmp_path, today)["readings"] == 1  # the transcript went out, so it is in the figure
    ai = cn.FakeText(closure_answer())
    soon = cn.scan(library, tmp_path, today, client=ai, consent=Consent(), now_ms=clock)  # the 10-minute round
    assert ai.prompts == [] and soon["read"] == 0 and soon["waiting"][0]["lecture_id"] == "l16"
    clock.hours(1)
    later = cn.scan(library, tmp_path, today, client=ai, consent=Consent(), now_ms=clock)
    assert len(later["added"]) == 1 and len(ai.prompts) == 1


def test_mark_seen(library, tmp_path):
    result = cn.scan(library, tmp_path, date(2026, 10, 20), client=cn.FakeText(closure_answer()), consent=Consent())
    assert cn.mark_seen(tmp_path, [result["added"][0]["key"]]) == 1
    assert cn.inbox(tmp_path, unseen_only=True) == [] and len(cn.inbox(tmp_path)) == 1


def test_confirmed_calendar_closure_cancels_that_days_class(library):
    """TODAY: a whole-day closure the user confirmed removes the class; an approximate one or a candidate does not."""
    friday = date(2026, 10, 30)
    assert [c.course for c in schedule.daily_schedule(library, friday)] == ["구조역학"]
    approx = cal.add(library, kind="closure", title="학회 주", day="2026-10-30", course_id="sr", date_precision="approx",
                     date_text="10월 말")
    cal.add(library, kind="closure", title="후보", day="2026-10-30", course_id="sr", source="transcript",
            source_ref="r1", status="candidate")
    assert [c.course for c in schedule.daily_schedule(library, friday)] == ["구조역학"]
    cal.set_status(library, approx, "cancelled")
    cal.add(library, kind="closure", title="휴강", day="2026-10-30", course_id="sr")
    assert schedule.daily_schedule(library, friday) == []
    today = schedule.today(library, datetime(2026, 10, 30, 9, 0, tzinfo=timezone(timedelta(hours=9))))
    assert any("달력에서 확정" in w for w in today.warnings)


def test_NOTICE_LOCK_001_network_wait_does_not_hold_the_write_lock(library, tmp_path):
    """Review #1: while Gemini reads lecture 2, another connection (ingest-app) can still write."""
    library.execute("INSERT INTO Lecture VALUES ('l13','sr','2026-10-13',1,NULL,NULL)")
    library.execute("INSERT INTO Transcript (id,lecture_id,media_id,engine,role,role_decided_by,status,"
                    "source_file,sha256) VALUES ('t3','l13',NULL,'mai','timeline','policy','done','t3','t3')")
    library.execute("INSERT INTO TranscriptSegment (transcript_id,seq,start_ms,text,chunk_index) "
                    "VALUES ('t3',0,0,?,0)", (SAID,))
    library.commit()
    seen = []

    def answer(prompt):
        # by the second reading, the first lecture's candidate is already stored; a second writer must not wait
        with closing(db.connect(tmp_path / "lecturehub.sqlite")) as other:
            other.execute("PRAGMA busy_timeout = 200")
            other.execute("BEGIN IMMEDIATE")  # raises 'database is locked' if scan still holds the lock
            other.execute("INSERT INTO Lecture VALUES (?,'sr','2026-10-09',1,NULL,NULL)", (f"x{len(seen)}",))
            other.commit()
        seen.append(prompt)
        return closure_answer(day="2026-10-30" if len(seen) == 1 else "2026-11-03")

    result = cn.scan(library, tmp_path, date(2026, 10, 20), client=cn.FakeText(answer), consent=Consent())
    assert result["read"] == 2 and len(seen) == 2 and not result["errors"]
    assert not library.in_transaction  # nothing left uncommitted for the caller
    with closing(db.connect(tmp_path / "lecturehub.sqlite")) as other:
        assert other.execute("SELECT COUNT(*) FROM CalendarEvent WHERE source='transcript'").fetchone()[0] >= 1
        assert other.execute("SELECT COUNT(*) FROM Lecture WHERE id LIKE 'x%'").fetchone()[0] == 2


def test_background_reading_can_be_switched_off_and_its_use_is_counted(library, tmp_path):
    """Release review 2026-11-16: the reading uses the user's Gemini key, so it has a switch and a visible count."""
    today = date(2026, 10, 20)
    cn.set_enabled(False)
    ai = cn.FakeText(closure_answer())
    off = cn.scan(library, tmp_path, today, client=ai, consent=Consent())
    assert off["disabled"] is True and off["read"] == 0 and cn.usage(tmp_path, today)["readings"] == 0
    # force reads regardless of the switch. Nothing in the app passes it (no screen, no command): tests and tools only
    asked = cn.scan(library, tmp_path, today, client=ai, consent=Consent(), force=True)
    assert asked["read"] == 1
    cn.set_enabled(True)
    use = cn.usage(tmp_path, today)
    assert use == {"month": "2026-10", "readings": 1, "characters": use["characters"]} and use["characters"] > 100
    assert cn.usage(tmp_path, date(2026, 11, 1))["readings"] == 0  # a new month starts at zero


# ---------------------------------------------------------------- release review 2026-11-16: switch, count, waits
TODAY = date(2026, 10, 20)


def test_NOTICE_SWITCH_001_switching_off_stops_the_round_that_is_running(library, tmp_path):
    """The switch is asked again right before each lecture: after "off" nothing more goes out in that round."""
    add_lecture(library, "l13", "2026-10-13", "t13")
    sent = []

    def answer(prompt):
        sent.append(prompt)
        cn.set_enabled(False)  # the user unticks the box while the first lecture is at Gemini
        return closure_answer()

    result = cn.scan(library, tmp_path, TODAY, client=cn.FakeText(answer), consent=Consent())
    assert len(sent) == 1 and "2026-10-13" in sent[0]  # the second lecture (10/16) stayed on this PC
    assert result["disabled"] is True and result["read"] == 1 and len(result["added"]) == 1  # the answer under way is used
    assert cn.scan(library, tmp_path, TODAY, client=cn.FakeText(answer), consent=Consent())["disabled"] is True
    assert len(sent) == 1
    cn.set_enabled(True)  # on again: the lecture that was held back is read now
    ai = cn.FakeText({"notices": []})
    again = cn.scan(library, tmp_path, TODAY, client=ai, consent=Consent())
    assert again["read"] == 1 and len(ai.prompts) == 1 and "2026-10-16" in ai.prompts[0] and "disabled" not in again


@pytest.mark.parametrize("reply", [BLOCKED, CUT_OFF, ERROR_PAGE, QUOTA, httpx.ReadTimeout("synthetic read timeout")],
                         ids=["blocked", "cut-off", "error-page", "quota-429", "read-timeout"])
def test_NOTICE_USE_001_a_request_that_went_out_is_counted_whatever_came_back(library, tmp_path, reply):
    make, posts = gemini([reply])
    result = cn.scan(library, tmp_path, TODAY, client=make(), consent=Consent(), now_ms=Clock())
    assert len(posts) == 1 and result["read"] == 0 and len(result["errors"]) == 1
    assert cn.usage(tmp_path, TODAY) == {"month": "2026-10", "readings": 1, "characters": len(posts[0]["prompt"])}


def test_NOTICE_USE_002_each_model_of_a_fall_through_is_counted(library, tmp_path):
    make, posts = gemini([BUSY, GOOD])
    result = cn.scan(library, tmp_path, TODAY, client=make(), consent=Consent(), now_ms=Clock())
    assert [p["model"] for p in posts] == list(cn.MODELS) and result["read"] == 1
    assert cn.usage(tmp_path, TODAY) == {"month": "2026-10", "readings": 2, "characters": 2 * len(posts[0]["prompt"])}
    assert json.loads(notice_file(tmp_path, "state.json").read_text(encoding="utf-8"))["l16"]["sends"] == 2


def test_NOTICE_USE_003_no_connection_sends_nothing_counts_nothing_and_is_tried_next_round(library, tmp_path):
    clock = Clock()
    make, asked = gemini([httpx.ConnectError("synthetic: no route"), GOOD])
    client = make()
    first = cn.scan(library, tmp_path, TODAY, client=client, consent=Consent(), now_ms=clock)
    assert len(asked) == 1 and client.posts == 0  # the transport was asked once and no connection came about
    assert first["errors"] and first["read"] == 0
    assert cn.usage(tmp_path, TODAY)["readings"] == 0 and cn.usage(tmp_path, TODAY)["characters"] == 0
    assert "l16" not in json.loads(notice_file(tmp_path, "state.json").read_text(encoding="utf-8"))
    second = cn.scan(library, tmp_path, TODAY, client=make(), consent=Consent(), now_ms=clock)  # no wait: nothing was sent
    assert second["read"] == 1 and cn.usage(tmp_path, TODAY)["readings"] == 1


def test_NOTICE_RETRY_001_a_failed_send_waits_1h_then_4h_then_for_new_transcripts(library, tmp_path):
    """One set of transcripts: at most 3 requests over all rounds and restarts, not one every 10 minutes."""
    clock = Clock()
    make, posts = gemini([BLOCKED])

    def round_():  # a fresh client and nothing kept in memory: what is known comes from the state file
        return cn.scan(library, tmp_path, TODAY, client=make(), consent=Consent(), now_ms=clock)

    assert round_()["errors"] and len(posts) == 1
    again = round_()                            # the next 10-minute round
    assert len(posts) == 1
    assert again["waiting"] == [{"lecture_id": "l16", "sends": 1, "until_ms": clock.at + 3_600_000}]
    clock.minutes(54)
    round_()
    assert len(posts) == 1                      # still inside the first hour
    clock.minutes(6)
    round_()
    assert len(posts) == 2                      # 1 h after the first failure
    clock.minutes(234)
    round_()
    assert len(posts) == 2                      # 4 h after the second have not passed
    clock.minutes(6)
    round_()
    assert len(posts) == 3
    clock.hours(24 * 30)
    waiting = round_()["waiting"]
    assert len(posts) == 3 and waiting == [{"lecture_id": "l16", "sends": 3, "until_ms": None}]
    assert cn.usage(tmp_path, TODAY)["readings"] == 3
    add_transcript(library, "l16", "t9", "새로 받아 적은 전사(합성)")  # the transcripts changed: read again
    round_()
    assert len(posts) == 4


def test_NOTICE_RETRY_002_a_lecture_with_an_unusable_answer_does_not_hold_up_the_next_one(library, tmp_path):
    add_lecture(library, "l13", "2026-10-13", "t13")  # read first (older)
    make, posts = gemini(lambda prompt: BLOCKED if "이 수업 날짜: 2026-10-13" in prompt else GOOD)
    result = cn.scan(library, tmp_path, TODAY, client=make(), consent=Consent(), now_ms=Clock())
    assert len(posts) == 2 and result["read"] == 1 and [e["lecture_id"] for e in result["errors"]] == ["l13"]
    state = json.loads(notice_file(tmp_path, "state.json").read_text(encoding="utf-8"))
    assert state["l16"]["fingerprint"] and state["l13"]["failed"]["sends"] == 1 and "fingerprint" not in state["l13"]


def test_NOTICE_RETRY_003_after_a_timeout_the_round_ends_and_the_next_round_reads_the_others(library, tmp_path):
    add_lecture(library, "l13", "2026-10-13", "t13")
    clock = Clock()
    make, posts = gemini(lambda prompt: httpx.ReadTimeout("synthetic") if "이 수업 날짜: 2026-10-13" in prompt else GOOD)
    first = cn.scan(library, tmp_path, TODAY, client=make(), consent=Consent(), now_ms=clock)
    assert len(posts) == 1 and first["read"] == 0  # a service that does not answer: the rest waits for the next round
    second = cn.scan(library, tmp_path, TODAY, client=make(), consent=Consent(), now_ms=clock)
    assert second["read"] == 1 and [w["lecture_id"] for w in second["waiting"]] == ["l13"]
    assert len(posts) == 2 and "이 수업 날짜: 2026-10-16" in posts[1]["prompt"]  # 10/13 was not sent again


def test_NOTICE_RETRY_004_two_models_failing_are_two_of_the_three_sends(library, tmp_path):
    clock = Clock()
    make, posts = gemini([BUSY])

    def round_():
        return cn.scan(library, tmp_path, TODAY, client=make(), consent=Consent(), now_ms=clock)

    round_()
    assert [p["model"] for p in posts] == list(cn.MODELS)
    clock.hours(1)
    round_()
    assert len(posts) == 2                      # two failed requests: the wait is the 4-hour one
    clock.hours(3)
    round_()
    assert [p["model"] for p in posts] == [*cn.MODELS, cn.MODELS[-1]]  # one request was left: one model is asked
    clock.hours(24 * 30)
    round_()
    assert len(posts) == 3 and cn.usage(tmp_path, TODAY)["readings"] == 3


def test_NOTICE_RETRY_005_a_clock_set_back_does_not_make_the_wait_longer(library, tmp_path):
    clock = Clock()
    make, posts = gemini([BLOCKED, GOOD])
    cn.scan(library, tmp_path, TODAY, client=make(), consent=Consent(), now_ms=clock)
    clock.hours(-24 * 365)  # the PC clock is now a year before the recorded failure
    held = cn.scan(library, tmp_path, TODAY, client=make(), consent=Consent(), now_ms=clock)
    assert len(posts) == 1  # the failure is on record: not sent again at once
    assert held["waiting"][0]["until_ms"] == clock.at + 3_600_000  # one hour from now, not a year and an hour
    clock.hours(1)
    assert cn.scan(library, tmp_path, TODAY, client=make(), consent=Consent(), now_ms=clock)["read"] == 1


def test_NOTICE_STATE_001_no_send_when_the_state_file_cannot_be_written(library, tmp_path, monkeypatch):
    """Without a record of the request the same transcript would go out again every round."""
    real = cn._write

    def write(path, data):
        if path.name == "state.json":
            raise PermissionError("synthetic: folder is read-only")
        real(path, data)

    monkeypatch.setattr(cn, "_write", write)
    ai = cn.FakeText(closure_answer())
    result = cn.scan(library, tmp_path, TODAY, client=ai, consent=Consent())
    assert ai.prompts == [] and result["read"] == 0 and "보내지 않았습니다" in result["errors"][0]["error"]
    assert cn.usage(tmp_path, TODAY)["readings"] == 0


def test_NOTICE_STATE_002_a_request_the_app_did_not_see_the_end_of_is_on_record(library, tmp_path):
    class Cut:  # the request is under way and the round ends with something that is not a NoticeError
        model = "x"

        def generate_json(self, prompt, schema, **_):
            raise RuntimeError("synthetic: cut short")

    clock = Clock()
    with pytest.raises(RuntimeError):
        cn.scan(library, tmp_path, TODAY, client=Cut(), consent=Consent(), now_ms=clock)
    assert cn.usage(tmp_path, TODAY)["readings"] == 1
    ai = cn.FakeText(closure_answer())
    assert cn.scan(library, tmp_path, TODAY, client=ai, consent=Consent(), now_ms=clock)["waiting"] and ai.prompts == []
    clock.hours(1)
    assert cn.scan(library, tmp_path, TODAY, client=ai, consent=Consent(), now_ms=clock)["read"] == 1


def test_NOTICE_STATE_005_a_state_file_that_fails_after_the_answer_keeps_candidate_and_inbox_together(tmp_path,
                                                                                                    monkeypatch):
    """The record after the answer cannot be written: the reading is not thrown away (the inbox names its candidate),
    and the request stays on record as sent, so the next one waits."""
    from lecturehub.core.paths import database_path
    from lecturehub.ui.library import Library

    database_path(tmp_path).parent.mkdir(parents=True)
    with closing(db.connect(database_path(tmp_path))) as connection:
        db.migrate(connection)
        connection.execute("INSERT INTO Course VALUES ('sr','SR','구조역학','sr','26-2')")
        connection.execute("INSERT INTO Schedule (course_id, weekday, start, end) VALUES ('sr', 4, 37800000, 42300000)")
        connection.commit()
        add_lecture(connection, "l16", "2026-10-16", "t1")
    real, state_writes = cn._write, []

    def write(path, data):
        if path.name == "state.json":
            state_writes.append(1)
            if len(state_writes) > 1:  # the record before sending is written; the one after the answer is not
                raise PermissionError("synthetic: file locked")
        real(path, data)

    monkeypatch.setattr(cn, "_write", write)
    monkeypatch.setattr(cn, "default_client", lambda: cn.FakeText(closure_answer()))
    monkeypatch.setattr("lecturehub.transcription.consent.ConsentStore", lambda path: Consent())
    result = Library(tmp_path).notices_scan(TODAY)
    assert result["ok"] is True and result["read"] == 1 and len(result["added"]) == 1
    event_id = cn.inbox(tmp_path)[0]["event_id"]
    with closing(db.connect(database_path(tmp_path))) as connection:
        assert connection.execute("SELECT 1 FROM CalendarEvent WHERE id=?", (event_id,)).fetchone()
    state = json.loads(notice_file(tmp_path, "state.json").read_text(encoding="utf-8"))
    assert state["l16"]["failed"]["sends"] == 1 and "fingerprint" not in state["l16"]


@pytest.mark.parametrize("damaged", ["[]", "not json {", '{"l16": 5}', '{"l16": {"failed": "x"}}',
                                     '{"l16": {"failed": {"fingerprint": 3, "sends": "many", "at": []}}, "zz": null}'])
def test_NOTICE_STATE_003_a_damaged_state_file_does_not_stop_the_reading(library, tmp_path, damaged):
    put(notice_file(tmp_path, "state.json"), damaged)
    result = cn.scan(library, tmp_path, TODAY, client=cn.FakeText(closure_answer()), consent=Consent())
    assert result["read"] == 1 and len(result["added"]) == 1
    assert json.loads(notice_file(tmp_path, "state.json").read_text(encoding="utf-8"))["l16"]["fingerprint"]


def test_NOTICE_STATE_004_a_damaged_inbox_does_not_break_a_reading_or_the_banner_list(library, tmp_path):
    put(notice_file(tmp_path, "inbox.json"), '{"not": "a list"}')
    assert cn.inbox(tmp_path) == [] and cn.mark_seen(tmp_path, ["x"]) == 0
    put(notice_file(tmp_path, "inbox.json"), '["text", 7, {"no_key": 1}, {"key": "kept", "seen": true}]')
    result = cn.scan(library, tmp_path, TODAY, client=cn.FakeText(closure_answer()), consent=Consent())
    assert len(result["added"]) == 1
    assert [i["key"] for i in cn.inbox(tmp_path)] == ["kept", result["added"][0]["key"]]


def test_NOTICE_USE_004_a_damaged_usage_file_keeps_the_switch_and_goes_on_counting(library, tmp_path):
    from lecturehub.ui.library import Library

    usage_file = notice_file(tmp_path, "usage.json")
    put(usage_file, '{"2026-10-01": {"readings": "x", "characters": [1]}, "2026-10-02": {"readings": 2, "characters": 50},'
                    ' "2026-10-03": "text", "2026-10-20": 7}')
    assert cn.usage(tmp_path, TODAY) == {"month": "2026-10", "readings": 2, "characters": 50}
    setting = Library(tmp_path).notices_setting(TODAY)
    assert setting["ok"] is True and setting["on"] is True and setting["readings"] == 2  # the switch is still drawn
    cn.scan(library, tmp_path, TODAY, client=cn.FakeText(closure_answer()), consent=Consent())
    assert cn.usage(tmp_path, TODAY)["readings"] == 3  # the damaged day (10/20) counts from zero, the good day is kept
    assert json.loads(usage_file.read_text(encoding="utf-8"))["2026-10-02"] == {"readings": 2, "characters": 50}
    for wrong in ("[]", "not json {", "null", '"text"'):
        put(usage_file, wrong)
        assert cn.usage(tmp_path, TODAY)["readings"] == 0 and Library(tmp_path).notices_setting(TODAY)["ok"] is True
        assert cn._count_use(tmp_path, TODAY, 1, 10) is True and cn.usage(tmp_path, TODAY)["readings"] == 1


def test_NOTICE_USE_005_the_switch_is_drawn_even_when_the_count_cannot_be_read(tmp_path, monkeypatch):
    from lecturehub.ui.library import Library

    monkeypatch.setattr(cn, "usage", lambda root, today: (_ for _ in ()).throw(ValueError("synthetic")))
    setting = Library(tmp_path).notices_setting(TODAY, set_to=False)
    assert setting == {"ok": True, "on": False, "month": "2026-10", "readings": 0, "characters": 0}


def test_NOTICE_SWITCH_002_with_the_switch_off_the_word_rules_on_this_pc_still_run(tmp_path, monkeypatch):
    """calendar_refresh: no AI reading (off, or no key) -> the local rule scan looks at the transcripts instead."""
    from lecturehub.core.paths import database_path
    from lecturehub.index import etl_sync
    from lecturehub.ui.library import Library

    database_path(tmp_path).parent.mkdir(parents=True)
    with closing(db.connect(database_path(tmp_path))) as connection:
        db.migrate(connection)
        connection.execute("INSERT INTO Course VALUES ('sr','SR','구조역학','sr','26-2')")
        connection.commit()
    monkeypatch.setattr(etl_sync, "sync", lambda root, **_: {"state": "not_connected"})  # no network in tests
    monkeypatch.setenv("LECTUREHUB_FAKE_GEMINI", "1")
    calls, real = [], cal.scan_transcripts
    monkeypatch.setattr(cal, "scan_transcripts", lambda connection, *a, **k: calls.append(1) or real(connection, *a, **k))
    on = Library(tmp_path).calendar_refresh()
    assert on["ok"] and on["notices"]["ok"] and "disabled" not in on["notices"] and calls == []  # the AI reads: rules rest
    cn.set_enabled(False)
    off = Library(tmp_path).calendar_refresh()
    assert off["ok"] and off["notices"]["disabled"] is True and calls == [1]


def test_NOTICE_SWITCH_003_switched_off_while_a_request_is_out_the_text_does_not_go_to_the_next_model(library, tmp_path):
    """The server answers the first model with an error; normally the same text goes to the next model at once.
    Not when the reading was switched off while that first request was out (pre-merge review 2026-11-19)."""
    def replies(prompt):
        cn.set_enabled(False)  # the user unticks the box while the request is at the server
        return BUSY

    make, posts = gemini(replies)
    result = cn.scan(library, tmp_path, TODAY, client=make(), consent=Consent(), now_ms=Clock())
    assert [p["model"] for p in posts] == [cn.MODELS[0]]  # before: both models got the transcript
    assert result["read"] == 0 and result["errors"]
    assert cn.usage(tmp_path, TODAY)["readings"] == 1  # the one that went out is counted


def test_NOTICE_RULES_001_lectures_the_ai_could_not_read_are_given_to_the_word_rules(tmp_path, monkeypatch):
    """calendar_refresh: a lecture whose send failed, or that waits (also for good, after three sends), is looked
    at by the local word rules, so an outage does not lose a plain notice. Lectures the AI read are left alone."""
    from lecturehub.core.paths import database_path
    from lecturehub.index import etl_sync
    from lecturehub.ui.library import Library

    database_path(tmp_path).parent.mkdir(parents=True)
    with closing(db.connect(database_path(tmp_path))) as connection:
        db.migrate(connection)
        connection.execute("INSERT INTO Course VALUES ('sr','SR','구조역학','sr','26-2')")
        connection.commit()
    monkeypatch.setattr(etl_sync, "sync", lambda root, **_: {"state": "not_connected"})  # no network in tests
    asked, real = [], cal.scan_transcripts
    monkeypatch.setattr(cal, "scan_transcripts",
                        lambda connection, *a, **k: asked.append(a[0] if a else None) or real(connection, *a, **k))
    outcome = {"ok": True, "read": 1, "added": [], "skipped": [], "errors": [], "waiting": []}
    monkeypatch.setattr(Library, "notices_scan", lambda self, today, *a, **k: dict(outcome))
    assert Library(tmp_path).calendar_refresh()["ok"] and asked == []  # everything was read: the rules rest
    outcome.update(errors=[{"lecture_id": "l19", "error": "합성: 실패"}],
                   waiting=[{"lecture_id": "l16", "sends": 3, "until_ms": None}, {"lecture_id": "l19", "sends": 1,
                                                                                  "until_ms": 5}])
    assert Library(tmp_path).calendar_refresh()["ok"]
    assert asked == [["l16", "l19"]]  # exactly the unread ones, once each
