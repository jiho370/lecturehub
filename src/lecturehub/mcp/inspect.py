"""D-082: projections for inspecting the app, over an already opened read-only connection and Gate.

No engine instances, CLI/login probes, network calls or raw settings/status/log dictionaries leave here.
Stored errors are deliberately reduced to fixed states: they can contain credentials and absolute paths.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from . import access, changes, tools


def _sections(base: dict, sections: dict[str, list], arguments: dict) -> dict:
    """One advancing cursor across named lists; every item appears once, including large semesters."""
    offset = arguments.get("offset", 0)
    rows = [(key, item) for key, items in sections.items() for item in items]
    result = {**base, **{key: [] for key in sections}, "count": 0, "total": len(rows), "truncated": False}
    page, index = tools._Page(result), offset
    for key, item in rows[offset:offset + arguments.get("limit", len(rows))]:
        if not page.add({key: item}):
            break
        result[key].append(item)
        index += 1
    result.update(count=index - offset, truncated=index < len(rows))
    if index < len(rows):
        tools._advance(result, {"next_offset": index}, (offset,), (index,))
    return result


def _integer(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _stamp(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        return datetime.fromtimestamp(value).astimezone().isoformat(timespec="seconds")
    except (ValueError, OSError, OverflowError):
        return None


def _engine(model):
    """Historical state.json has a model, no provider. Infer only recognised labels, never echo arbitrary text."""
    if not isinstance(model, str):
        return None
    if model in ("codex_cli", "fake_codex"):
        return "codex"
    if model.startswith("gemini-"):
        return "google"
    return None


def _visible_changes(gate):
    # Filter BEFORE paging; a hidden course must not affect total, offsets or the visible result window.
    return [r for r in changes.recent(gate.root, limit=changes.READ_TAIL)
            if r.get("course") in gate.courses.values()]


def get_app_status(connection, gate, arguments):
    from lecturehub import __version__
    from lecturehub.ai import engines
    from lecturehub.core import edition, reminder_settings, settings
    from lecturehub.index import class_notices, etl_submissions, lectures

    from . import writes

    saved = settings.load_strict() or {}
    notes = tools._note_links(connection, gate)
    materials = dict(connection.execute("SELECT course_id, count(*) FROM Material GROUP BY course_id"))
    pending = dict(connection.execute("SELECT course_id, count(*) FROM CalendarEvent "
                                      "WHERE status='candidate' GROUP BY course_id"))
    courses = []
    for cid in gate.ids():
        listed = lectures.lecture_list(connection, cid)
        used = 0
        for item in listed:
            transcripts = lectures._transcripts(connection, item["id"])
            pointer = lectures.in_use(connection, item["id"], transcripts)
            used += any(t["segments"] and pointer and t["id"] == pointer["transcript_id"] for t in transcripts)
        courses.append({"course": gate.name(cid), "lectures": len(listed), "with_transcript": used,
                        "with_note": sum(bool(notes.get(x["id"])) for x in listed),
                        "materials": materials.get(cid, 0), "pending_calendar_candidates": pending.get(cid, 0)})
    prefs = reminder_settings.preferences(saved=saved)
    last = class_notices.status(gate.root)
    unavailable = last.get("unavailable")
    missing = ("not_installed" if isinstance(unavailable, str) and "없습니다" in unavailable else
               "not_signed_in" if isinstance(unavailable, str) and "로그인" in unavailable else
               "unavailable" if unavailable else None)
    try:
        note_engine = engines.selected("notes", saved=saved)
    except (ValueError, AttributeError):
        note_engine = "invalid"
    midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    now_ms = int(datetime.now().timestamp() * 1000)
    today = sum(1 for r in _visible_changes(gate)
                if (at := _integer(r.get("at_ms"))) is not None and int(midnight.timestamp() * 1000) <= at <= now_ms)
    fetched = _stamp(etl_submissions.fetched_at(gate.root))
    base = {"version": __version__, "edition": edition.name(), "allowed_courses": len(gate.courses),
            "ai_link": {"enabled": access.enabled(), "write": access.write_enabled(),
                        "auto": settings.ai_link_auto(saved=saved)},
            "engines": {"notes": note_engine, "class_notices": class_notices.engine(saved=saved)},
            "class_notices": {"enabled": class_notices.enabled(saved=saved), "last_round": {
                "recorded": bool(last), "engine": last.get("engine") if isinstance(last.get("engine"), str)
                and last["engine"] in class_notices.ENGINES else None,
                "at_ms": _integer(last.get("at")), "recording": last.get("recording") is True,
                "missing": missing}},
            # The MCP process never reads the eTL token (D-072): only whether the app has fetched the lists, and when.
            "etl": {"received": fetched is not None, "fetched_at": fetched},
            "reminders": {"deadlines": prefs["deadlines"], "briefing": prefs["briefing"]},
            "ai_changes": {"today": today, "scope": "allowed_courses", "burst": writes.BURST,
                           "burst_seconds": writes.BURST_S, "daily": writes.DAILY}}
    needs = last.get("needs_consent")
    return _sections(base, {"courses": courses,
                           "muted_courses": [c for c in prefs["muted_courses"] if c in gate.courses.values()],
                           "notice_needs_consent": sorted({c for c in needs if isinstance(c, str)
                                                           and c in gate.courses.values()}) if isinstance(needs, list) else []},
                     arguments)


def get_timetable(connection, gate, arguments):
    from lecturehub.core import timetable
    from lecturehub.index import schedule

    only = tools._course_arg(gate, arguments)
    day = date.fromisoformat(arguments.get("week_of") or date.today().isoformat())
    monday = day - timedelta(days=day.weekday())
    if monday.toordinal() + 6 > date.max.toordinal():
        raise tools.InvalidArguments("week_of의 한 주가 날짜 범위를 벗어납니다")
    days = [monday + timedelta(days=i) for i in range(7)]

    def allowed(cid):
        return gate.allows(cid) and (only is None or cid == only)

    weekly = [{"course": gate.name(cid), "day": timetable.DAYS[weekday], "weekday": weekday,
               "start": tools._day_clock(start), "end": tools._day_clock(end), "room": None}
              for cid, weekday, start, end in connection.execute(
                  "SELECT course_id, weekday, start, end FROM Schedule ORDER BY weekday, start, course_id")
              if allowed(cid)]
    actual = [{"course": gate.name(item.course_id), "date": item.date, "day": timetable.DAYS[d.weekday()],
               "start": tools._day_clock(item.start_ms), "end": tools._day_clock(item.end_ms),
               "kind": item.kind, "room": None}
              for d in days for item in schedule.daily_schedule(connection, d) if allowed(item.course_id)]
    # Explicit cancellations are useful even on days with no remaining classes. Never pass free-form notes.
    exceptions = [{"course": gate.name(cid), "date": on, "kind": kind}
                  for cid, on, kind in connection.execute(
                      "SELECT course_id, date, kind FROM ScheduleException WHERE date BETWEEN ? AND ? "
                      "ORDER BY date, course_id, kind", (days[0].isoformat(), days[-1].isoformat())) if allowed(cid)]
    return _sections({"week_of": monday.isoformat(), "week_end": days[-1].isoformat(),
                      "days": [d.isoformat() for d in days], "term": timetable.load_term(gate.root),
                      "room_note": "앱 시간표에 강의실 정보가 저장되어 있지 않습니다."},
                     {"weekly": weekly, "schedule": actual, "exceptions": exceptions}, arguments)


def _notice_candidates(connection, gate, lecture_id, cid):
    """All statuses, including resolved/dismissed candidates, with only quotes of this very lecture/course."""
    grouped = {}
    for row in connection.execute(
            "SELECT c.id, c.title, c.date, c.status, e.detail FROM CalendarEvent c JOIN Evidence e "
            "ON e.subject_type='CalendarEvent' AND e.subject_id=c.id "
            "WHERE c.course_id=? AND e.kind='transcript_quote' ORDER BY c.date, c.id, e.id", (cid,)):
        try:
            detail = json.loads(row[4])
        except (ValueError, TypeError):
            continue
        if not isinstance(detail, dict) or detail.get("lecture_id") != lecture_id:
            continue
        evidence = tools._evidence(gate, connection, detail, cid)
        if not evidence:
            continue
        # One quote per row bounds the item even when a candidate has hundreds of sources.
        key = (row[0], json.dumps(evidence, ensure_ascii=False, sort_keys=True))
        grouped[key] = {"event_id": row[0], "title": tools._cut(row[1], tools.SHORT), "date": row[2],
                        "status": row[3], "evidence": evidence}
    out = list(grouped.values())
    # A reading can find a notice with no resolvable date: it lives only in the inbox, not CalendarEvent.
    from lecturehub.index import class_notices

    for item in class_notices.inbox(gate.root):
        if item.get("lecture_id") != lecture_id or item.get("course_id") != cid or item.get("event_id"):
            continue
        evidence = tools._evidence(gate, connection, item, cid)
        if evidence:
            out.append({"title": tools._cut(str(item.get("summary") or ""), tools.SHORT),
                        "date": None, "status": "no_date", "evidence": evidence})
    return out


def get_lecture_status(connection, gate, arguments):
    from lecturehub.index import class_notices, lectures

    lecture, _ = tools._lecture(connection, gate, arguments["lecture_id"])
    lid, cid, day = lecture
    transcripts = lectures._transcripts(connection, lid)
    pointer = lectures.in_use(connection, lid, transcripts)
    listed = []
    for t in transcripts:
        span = connection.execute("SELECT min(start_ms), max(coalesce(end_ms,start_ms)) FROM TranscriptSegment "
                                  "WHERE transcript_id=?", (t["id"],)).fetchone()
        length = max(0, (span[1] or 0) - (span[0] or 0))
        listed.append({"transcript_id": t["id"], "engine": t["engine"] if t["engine"] in lectures.ENGINE_LABEL else "unknown",
                       "label": lectures.ENGINE_LABEL.get(t["engine"], "알 수 없음"),
                       "status": t["status"], "in_use": bool(pointer and pointer["transcript_id"] == t["id"]),
                       "segments": t["segments"], "length_ms": length, "length_clock": tools._clock(length),
                       "first_ms": span[0], "last_ms": span[1]})
    notes = []
    for aid, kind, _path, version in tools._note_links(connection, gate).get(lid, []):
        author, inputs = connection.execute("SELECT created_by, inputs FROM StudyArtifact WHERE id=?", (aid,)).fetchone()
        if author == "ai_app":
            try:
                saved = json.loads(inputs or "{}")
            except (ValueError, TypeError):
                saved = {}
            author = saved.get("written_by") if isinstance(saved, dict) else None
        # These are authorship labels, not a local account's arbitrary identifier.
        safe_author = author if author in ("ChatGPT가 씀", "Claude가 씀", "AI 앱이 씀", "codex", "codex_cli",
                                          "claude", "claude_cli", "user", "ai", "user_attach") else "unknown"
        notes.append({"note_id": aid, "kind": kind, "version": version, "author": safe_author})
    materials = [{"material_id": r[0], "title": tools._cut(r[1], tools.SHORT), "page_from": r[2], "page_to": r[3]}
                 for r in connection.execute("SELECT m.id, m.title, mm.page_from, mm.page_to FROM MaterialMatch mm "
                                             "JOIN Material m ON m.id=mm.material_id WHERE mm.lecture_id=? AND m.course_id=? "
                                             "ORDER BY m.id", (lid, cid))]
    state = tools._read_json(gate.root / "_lecturehub" / "notices" / "state.json", {})
    read = state.get(lid) if isinstance(state, dict) else None
    read = read if isinstance(read, dict) else {}
    read_at = _integer(read.get("read_at"))
    current = bool(read_at is not None and read.get("fingerprint") == class_notices._fingerprint(
        [t for t in transcripts if t["segments"]]))
    return _sections({"lecture_id": lid, "course": gate.name(cid), "date": day, "has_note": bool(notes),
                      "length_note": "전사 길이는 첫 구간 시작부터 기록된 마지막 끝까지입니다. 끝 시각이 없으면 시작 시각까지로, 실제보다 짧을 수 있습니다.",
                      "class_notices": {"was_read": read_at is not None, "current": current,
                                        "engine": _engine(read.get("model")), "engine_from_model": True,
                                        "read_at_ms": read_at, "last_attempt_failed": isinstance(read.get("failed"), dict),
                                        "notices": _integer(read.get("notices")),
                                        "dropped_without_quote": _integer(read.get("dropped_without_quote"))}},
                     {"transcripts": listed, "notes": notes, "materials": materials,
                      "candidates": _notice_candidates(connection, gate, lid, cid)}, arguments)


def list_ai_changes(connection, gate, arguments):
    only = tools._course_arg(gate, arguments)
    rows = [{"change_id": r["id"], "at": _stamp(at / 1000) if (at := _integer(r.get("at_ms"))) is not None else None,
             "tool": tools._cut(str(r.get("tool") or ""), 100), "course": r["course"],
             "summary": tools._cut(str(r.get("summary") or ""), tools.SHORT), "undone": r["undone"] is True}
            for r in _visible_changes(gate) if only is None or r["course"] == gate.name(only)]
    return _sections({"undo_in_app": "설정 → AI → AI 앱 연결 → AI가 바꾼 것"}, {"changes": rows}, arguments)
