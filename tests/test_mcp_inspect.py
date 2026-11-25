"""MCP-INSPECT-001..012 (D-082): synthetic inspection, privacy, paging and no side effects."""
import json
import socket
import sqlite3
from contextlib import closing
from datetime import date, datetime

import keyring
import pytest
import test_mcp_access
from test_mcp_access import ALLOWED, HIDDEN, MARKER, call

import lecturehub
from lecturehub.core import db, settings, timetable
from lecturehub.index import class_notices, etl_sync, lectures
from lecturehub.mcp import access, changes, server, tools, writes

library = test_mcp_access.library  # the shared synthetic fixture (an assignment, so test arguments do not redefine an import)


def database(root):
    return root / "_lecturehub" / "lecturehub.sqlite"


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def add_change(root, identity, course=ALLOWED, *, at=None):
    changes.append(root, {"id": identity, "course": course, "tool": "add_calendar_event", "summary": "합성 일정 추가",
                          "at_ms": at if at is not None else int(datetime.now().timestamp() * 1000),
                          "at": f"C:/{MARKER}", "undo": {"token": MARKER, "path": f"C:/{MARKER}"}})


def test_MCP_INSPECT_001_app_status_whitelists_settings_and_allowed_counts(library, monkeypatch):
    settings.set_ai_link_write(True)
    settings.set_ai_link_auto("save_note", True)
    settings.save({"ai_engine": {"notes": "claude"}, "class_notices_engine": "google",
                   "reminders": {"deadlines": False, "briefing": True, "muted_courses": [ALLOWED, HIDDEN]},
                   "account": MARKER, "api_key": MARKER})
    keyring.set_password(etl_sync.KEYRING_SERVICE, etl_sync.KEYRING_NAME, MARKER)
    write_json(library / "_lecturehub/notices/status.json", {"engine": "codex", "at": 123456,
        "unavailable": f"Codex에 로그인되어 있지 않습니다. {MARKER}", "needs_consent": [ALLOWED, HIDDEN],
        "recording": True, "account": MARKER})
    add_change(library, "allowed-today")
    add_change(library, "hidden-today", HIDDEN)
    add_change(library, "voided")
    changes.append(library, {"void_of": "voided"})
    # Normal app helpers must consume a strict snapshot, never settings.load()'s repairing/fallback reader.
    monkeypatch.setattr(settings, "load", lambda: pytest.fail("MCP must not recover settings"))
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("no network"))
    found, error, raw = call("get_app_status")
    assert not error and MARKER not in raw and HIDDEN not in raw and "C:/" not in raw
    assert found["version"] == lecturehub.__version__ and found["edition"] == "full"
    assert found["allowed_courses"] == 1
    assert found["courses"] == [{"course": ALLOWED, "lectures": 2, "with_transcript": 1, "with_note": 2,
                                 "materials": 1, "pending_calendar_candidates": 1}]
    assert found["ai_link"]["enabled"] and found["ai_link"]["write"] and found["ai_link"]["auto"]["save_note"]
    assert not found["ai_link"]["auto"]["add_calendar_event"]
    assert found["engines"] == {"notes": "claude", "class_notices": "google"}
    assert found["class_notices"]["last_round"] == {"recorded": True, "engine": "codex", "at_ms": 123456,
        "recording": True, "missing": "not_signed_in"}
    assert found["notice_needs_consent"] == [ALLOWED] and found["muted_courses"] == [ALLOWED]
    assert found["etl"]["received"] is True and found["etl"]["fetched_at"]
    assert found["reminders"] == {"deadlines": False, "briefing": True}
    assert found["ai_changes"] == {"today": 1, "scope": "allowed_courses", "burst": writes.BURST,
                                   "burst_seconds": writes.BURST_S, "daily": writes.DAILY}


def test_MCP_INSPECT_002_timetable_uses_daily_schedule_and_term(library):
    timetable.save_term(library, "2026-09-01", "2026-12-01")
    with closing(db.connect(database(library))) as c:
        c.execute("INSERT INTO ScheduleException(course_id,date,kind,note) VALUES ('ca','2026-09-07','휴강','합성 휴강')")
        c.execute("INSERT INTO ScheduleException(course_id,date,kind,note) VALUES ('ca','2026-09-09','보강','13:10-14:00 합성 보강')")
        c.commit()
    found, error, raw = call("get_timetable", {"week_of": "2026-09-09"})
    assert not error and MARKER not in raw and HIDDEN not in raw and "instructor" not in raw
    assert found["term"] == {"start": "2026-09-01", "end": "2026-12-01"}
    assert found["days"] == [f"2026-09-{n:02}" for n in range(7, 14)]
    assert found["weekly"] == [{"course": ALLOWED, "day": "월", "weekday": 0,
                                "start": "09:00", "end": "10:00", "room": None}]
    assert found["schedule"] == [{"course": ALLOWED, "date": "2026-09-09", "day": "수", "start": "13:10",
                                  "end": "14:00", "kind": "보강", "room": None}]
    outside = call("get_timetable", {"week_of": "2027-01-04"})[0]
    assert outside["schedule"] == [] and outside["weekly"]
    default = call("get_timetable")[0]
    assert default["week_of"] == (date.today().fromordinal(date.today().toordinal() - date.today().weekday())).isoformat()


def test_MCP_INSPECT_003_lecture_transcripts_notes_materials_and_notice_reading(library):
    with closing(sqlite3.connect(database(library))) as c:
        c.execute("UPDATE StudyArtifact SET created_by='ai_app', inputs=? WHERE id='fa'",
                  (json.dumps({"written_by": "ChatGPT가 씀", "private": MARKER}),))
        mark = class_notices._fingerprint(lectures._transcripts(c, "a1"))
        c.commit()
    write_json(library / "_lecturehub/notices/state.json", {"a1": {"fingerprint": mark, "read_at": 123456,
               "model": "codex_cli", "notices": 1, "dropped_without_quote": 2}})
    found, error, _ = call("get_lecture_status", {"lecture_id": "a1"})
    assert not error and found["date"] == "2026-09-07" and found["has_note"]
    assert found["transcripts"] == [{"transcript_id": "ta", "engine": "gemini_transcribe", "label": "Google",
        "status": "done", "in_use": True, "segments": 3, "length_ms": 124998, "length_clock": "02:04",
        "first_ms": 999, "last_ms": 125997}]
    assert found["materials"] == [{"material_id": "ma", "title": "ma 제목", "page_from": 1, "page_to": 2}]
    assert found["class_notices"] == {"was_read": True, "current": True, "engine": "codex", "engine_from_model": True,
        "read_at_ms": 123456, "last_attempt_failed": False, "notices": 1, "dropped_without_quote": 2}
    candidate = found["candidates"][0]
    assert candidate["title"] == "퀴즈 — 수업 중 언급" and candidate["status"] == "candidate"
    assert candidate["evidence"]["quote"] == "다음 주 수요일 퀴즈" and candidate["evidence"]["clock"] == "01:02"
    assert candidate["evidence"]["start_ms"] == 62998
    assert call("get_lecture_status", {"lecture_id": "a2"})[0]["notes"][0]["author"] == "ChatGPT가 씀"


def test_MCP_INSPECT_004_never_read_stale_failed_and_unknown_model(library):
    assert call("get_lecture_status", {"lecture_id": "a1"})[0]["class_notices"]["was_read"] is False
    write_json(library / "_lecturehub/notices/state.json", {"a1": {"fingerprint": "old", "read_at": 1,
               "model": f"C:/{MARKER}", "failed": {"error": MARKER}}})
    found, error, raw = call("get_lecture_status", {"lecture_id": "a1"})
    assert not error and MARKER not in raw and "C:/" not in raw
    assert found["class_notices"]["was_read"] and not found["class_notices"]["current"]
    assert found["class_notices"]["last_attempt_failed"] and found["class_notices"]["engine"] is None


def test_MCP_INSPECT_005_notice_without_date_and_resolved_candidate(library):
    write_json(library / "_lecturehub/notices/inbox.json", [
        {"key": "no-day", "course_id": "ca", "lecture_id": "a1", "summary": "날짜 미정(합성)",
         "quote": "추후 보강하겠습니다", "at_ms": 62998},
        {"key": "wrong-course", "course_id": "cb", "lecture_id": "a1", "summary": MARKER, "quote": MARKER},
        {"key": "wrong-lecture", "course_id": "ca", "lecture_id": "b1", "summary": MARKER, "quote": MARKER}])
    with closing(sqlite3.connect(database(library))) as c:
        c.execute("UPDATE CalendarEvent SET status='confirmed' WHERE course_id='ca' AND status='candidate'")
        c.commit()
    found, error, raw = call("get_lecture_status", {"lecture_id": "a1"})
    assert not error and MARKER not in raw
    assert {r["status"] for r in found["candidates"]} == {"confirmed", "no_date"}
    assert next(r for r in found["candidates"] if r["status"] == "no_date")["date"] is None


def test_MCP_INSPECT_006_ai_changes_filters_before_paging_and_keeps_undo(library):
    add_change(library, "one")
    changes.append(library, {"undo_of": "one", "at": MARKER})
    add_change(library, "two")
    add_change(library, "void")
    changes.append(library, {"void_of": "void"})
    for n in range(40):
        add_change(library, f"hidden-{n}", HIDDEN)
    found, error, raw = call("list_ai_changes", {"limit": 1})
    assert not error and MARKER not in raw and HIDDEN not in raw
    assert found["total"] == 2 and found["changes"][0]["change_id"] == "two" and found["next_offset"] == 1
    more = call("list_ai_changes", {"offset": 1})[0]
    assert more["changes"][0]["undone"] is True
    assert set(more["changes"][0]) == {"change_id", "at", "tool", "course", "summary", "undone"}


@pytest.mark.parametrize("name,args", [("get_app_status", {}), ("get_timetable", {"week_of": "2026-09-07"}),
    ("get_lecture_status", {"lecture_id": "a1"}), ("list_ai_changes", {})])
def test_MCP_INSPECT_007_paging_advances_and_preserves_all_sections(library, monkeypatch, name, args):
    for n in range(8):
        add_change(library, f"change-{n}")
    whole, error, _ = call(name, args)
    assert not error
    monkeypatch.setattr(tools, "TEXT_CAP", 1500)
    parts, cursor = [], 0
    for _ in range(30):
        part, error, _ = call(name, {**args, "offset": cursor})
        assert not error
        parts.append(part)
        if not part["truncated"]:
            break
        assert part["next_offset"] > cursor
        cursor = part["next_offset"]
    else:
        pytest.fail("paging did not finish")
    keys = {"get_app_status": ("courses", "muted_courses", "notice_needs_consent"),
            "get_timetable": ("weekly", "schedule", "exceptions"),
            "get_lecture_status": ("transcripts", "notes", "materials", "candidates"),
            "list_ai_changes": ("changes",)}[name]
    for key in keys:
        assert [r for part in parts for r in part[key]] == whole[key]


@pytest.mark.parametrize("week", ["2026-02-30", "9999-12-31"])
def test_MCP_INSPECT_008_invalid_week_is_rejected(week):
    if week == "2026-02-30":
        with pytest.raises(tools.InvalidArguments):
            tools.check_arguments("get_timetable", {"week_of": week})
    else:
        # The overflow is checked in the handler after the normal gate.
        with pytest.raises(tools.InvalidArguments):
            tools.get_timetable(None, access.Gate(None, {}), {"week_of": week})


def test_MCP_INSPECT_013_the_inspector_never_reads_the_etl_token(library, monkeypatch):
    """D-072 rule 3: the MCP process does not read the eTL token or API keys, not even to say "connected"."""
    def forbidden(*_args, **_kwargs):
        raise AssertionError("the MCP inspector read the token store")

    monkeypatch.setattr(keyring, "get_password", forbidden)
    monkeypatch.setattr(etl_sync, "load_token", forbidden)
    for name in ("get_app_status", "get_timetable", "list_ai_changes"):
        _found, error, raw = call(name)
        assert not error and MARKER not in raw, name


def test_MCP_INSPECT_009_damaged_optional_files_and_etl_site_mismatch(library):
    for path in ("notices/state.json", "notices/status.json", "notices/inbox.json", "etl/sync.json", "term.json"):
        (library / "_lecturehub" / path).write_text("{broken", encoding="utf-8")
    settings.save({"etl_base": "https://canvas.example.invalid", "ai_engine": "invalid", "etl_token_host": "elsewhere.invalid"})
    keyring.set_password(etl_sync.KEYRING_SERVICE, etl_sync.KEYRING_NAME, MARKER)
    found, error, raw = call("get_app_status")
    assert not error and MARKER not in raw and found["etl"] == {"received": False, "fetched_at": None}
    assert found["engines"]["notes"] == "invalid" and not found["class_notices"]["last_round"]["recorded"]
    assert call("get_timetable")[0]["term"] == {}
    assert call("get_lecture_status", {"lecture_id": "a1"})[0]["class_notices"]["was_read"] is False


def test_MCP_INSPECT_010_cross_course_links_do_not_appear(library):
    """Mismatched material/quote links and multi-course notes cannot smuggle a hidden course into status."""
    with closing(db.connect(database(library))) as c:
        c.execute("INSERT INTO MaterialMatch(lecture_id,material_id,page_from,page_to,confidence_class,decided_by) "
                  "VALUES ('a1','mb',1,1,'high','user')")
        c.execute("UPDATE StudyArtifact SET lecture_ids=? WHERE id='na'", (json.dumps(["a1", "b1"]),))
        event_id = c.execute("SELECT id FROM CalendarEvent WHERE course_id='ca' AND status='candidate'").fetchone()[0]
        c.execute("INSERT INTO Evidence(id,subject_type,subject_id,kind,detail,source,created_at) "
                  "VALUES ('bad-link','CalendarEvent',?,'transcript_quote',?,'engine',0)",
                  (event_id, json.dumps({"lecture_id": "b1", "quote": MARKER, "at_ms": 0})))
        c.commit()
    found, error, raw = call("get_lecture_status", {"lecture_id": "a1"})
    assert not error and MARKER not in raw and not found["has_note"] and found["notes"] == []
    assert [m["material_id"] for m in found["materials"]] == ["ma"]
    assert len(found["candidates"]) == 1
    assert call("get_app_status")[0]["courses"][0]["with_note"] == 1


def test_MCP_INSPECT_011_real_character_cap_and_complete_large_lists(library):
    """All four tools must stay below the normal wire cap and recover every item over multiple pages."""
    path = library / "_lecturehub/consent.json"
    consents = json.loads(path.read_text(encoding="utf-8"))
    entry = consents["ai_app_link"][ALLOWED]
    with closing(db.connect(database(library))) as c:
        for n in range(120):
            name = f"합성과목 {n:03d} " + "가" * 45
            consents["ai_app_link"][name] = entry
            c.execute("INSERT INTO Course(id,code,folder_name,abbr,term) VALUES (?,?,?,?,?)",
                      (f"synthetic-{n}", f"C{n}", name, f"c{n}", "26-2"))
            c.execute("INSERT INTO Schedule(course_id,weekday,start,end) VALUES (?,0,32400000,36000000)", (f"synthetic-{n}",))
        for n in range(80):
            c.execute("INSERT INTO Material(id,course_id,path,sha256,kind,title,page_count) VALUES (?,'ca',?,?,'pdf',?,1)",
                      (f"many-{n}", str(library / ALLOWED / f"합성{n}.pdf"), f"sha-{n}", "합성 자료 " + "가" * 290))
            c.execute("INSERT INTO MaterialMatch(lecture_id,material_id,page_from,page_to,confidence_class,decided_by) "
                      "VALUES ('a1',?,1,1,'high','user')", (f"many-{n}",))
        c.commit()
    write_json(path, consents)
    for n in range(80):
        changes.append(library, {"id": f"many-change-{n}", "course": ALLOWED, "tool": "add_calendar_event",
                                "summary": "합성 변경 " + "가" * 290, "at_ms": n})
    cases = [("get_app_status", {}, "courses", 121),
             ("get_timetable", {"week_of": "2026-09-07"}, "weekly", 121),
             ("get_lecture_status", {"lecture_id": "a1"}, "materials", 81),
             ("list_ai_changes", {}, "changes", 80)]
    for name, args, key, expected in cases:
        rows, cursor, pages = [], 0, 0
        for _ in range(15):
            found, error, _ = call(name, {**args, "offset": cursor})
            assert not error, (name, found)
            assert len(json.dumps(found, ensure_ascii=False)) <= tools.TEXT_CAP, name
            rows.extend(found[key])
            pages += 1
            if not found["truncated"]:
                break
            assert found["next_offset"] > cursor
            cursor = found["next_offset"]
        else:
            pytest.fail(f"{name} did not finish")
        assert len(rows) == expected and pages > 1, (name, len(rows), pages)
        assert len({json.dumps(row, sort_keys=True) for row in rows}) == expected, name


def test_MCP_INSPECT_012_same_server_rechecks_consent_and_ignores_empty_transcript_for_freshness(library):
    from lecturehub.transcription.consent import ConsentStore

    with closing(db.connect(database(library))) as c:
        mark = class_notices._fingerprint(lectures._transcripts(c, "a1"))
        c.execute("INSERT INTO Transcript(id,lecture_id,engine,role,role_decided_by,status,source_file,sha256) "
                  "VALUES ('empty','a1','whisper','reference','policy','pending','','synthetic')")
        c.commit()
    write_json(library / "_lecturehub/notices/state.json", {"a1": {"fingerprint": mark, "read_at": 1,
                                                               "model": "gemini-2.5-flash"}})
    found, error, _ = call("get_lecture_status", {"lecture_id": "a1"})
    assert not error and found["class_notices"]["current"] and found["class_notices"]["engine"] == "google"
    srv = server.Server()
    srv.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})

    def request(name, args):
        result = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                             "params": {"name": name, "arguments": args}})["result"]
        return json.loads(result["content"][0]["text"])

    assert request("get_app_status", {})["allowed_courses"] == 1
    ConsentStore(library / "_lecturehub/consent.json").revoke(access.PROVIDER, ALLOWED, 2)
    assert request("get_app_status", {})["allowed_courses"] == 0
    assert request("get_timetable", {})["weekly"] == []
    assert request("get_lecture_status", {"lecture_id": "a1"}) == {"error": access.NOT_FOUND}
    assert request("list_ai_changes", {})["changes"] == []
    settings.set_ai_link(False)
    for name, args in (("get_app_status", {}), ("get_timetable", {}),
                       ("get_lecture_status", {"lecture_id": "a1"}), ("list_ai_changes", {})):
        assert request(name, args) == {"error": access.OFF}
