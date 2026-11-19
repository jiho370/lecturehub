"""S2: link an app recording to its lecture, and register its transcripts (§0.2, D-050).

Runs in its own process after a recording is saved; a failure here never
touches the recording. Linking uses the exact app start time and the course
the user picked, checked against the timetable with registered exceptions
(inferred ones included, D-052). Anything uncertain becomes a pending
IngestItem ("미확정 회차") that the user confirms with one click.
Nothing is moved; the master stays under _lecturehub\\recordings. The D-055
copy to 녹음본 runs after linking, in its own transaction (archive_copy).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from datetime import date, datetime
from pathlib import Path

from lecturehub.core.events import EventStore
from lecturehub.core.ids import new_ulid
from lecturehub.parsers import parse_file

from . import lectures
from .media import MediaProbeError, probe_media
from .schedule import daily_schedule

DEFAULT_LEAD_MIN = 15  # same window as the recorder's course guess (recorder.toml course_lead_minutes)


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def session_master(session_dir: Path) -> tuple[dict, Path | None, dict]:
    """(session.json, newest finalized master, stop.json)."""
    session = _read(session_dir / "session.json")
    stop = _read(session_dir / "stop.json") if (session_dir / "stop.json").exists() else {}
    finals = sorted(session_dir.glob("finalize_*.json"), key=lambda p: p.stat().st_mtime)
    master = Path(_read(finals[-1])["master"]) if finals else None
    return session, master, stop


def _picked(session: dict) -> str:
    """The course written at the start. A menu value that was never a name (the app once wrote its
    "no course" marker here, 2026-10-03) counts as no course."""
    course = str(session.get("course") or "").strip()
    return "" if course[:1] in ("\u0000", "\ufffd") else course


def _evidence(connection, subject_type, subject_id, kind, detail, source) -> None:
    connection.execute(
        "INSERT INTO Evidence(id,subject_type,subject_id,kind,detail,source,created_at) VALUES (?,?,?,?,?,?,?)",
        (new_ulid(), subject_type, subject_id, kind, json.dumps(detail, ensure_ascii=False, sort_keys=True), source,
         time.time_ns() // 1_000_000))


def candidates(connection: sqlite3.Connection, started: datetime, lead_min: int) -> list[dict]:
    """Classes whose window [start − lead, end] contains the recording start."""
    at = ((started.hour * 60 + started.minute) * 60 + started.second) * 1000
    lead = lead_min * 60_000
    return [{"course_id": c.course_id, "course": c.course, "start_ms": c.start_ms, "end_ms": c.end_ms, "kind": c.kind}
            for c in daily_schedule(connection, started.date()) if c.start_ms - lead <= at <= c.end_ms]


def decide(course: str, found: list[dict], course_source: str = "unknown") -> tuple[str | None, str, str, str]:
    """(course folder or None, confidence, decided_by, reason). Only a user pick earns "high"."""
    folders = sorted({c["course"] for c in found})
    if course and course in folders and course_source != "user_pick":
        # Pre-filled from this same timetable (or unknown origin): agreeing with it proves nothing more.
        why = "시간표로 자동 채운 과목" if course_source == "timetable_auto" else "과목을 고른 주체가 기록되지 않은 세션"
        return course, "medium", "policy", f"{why}이 녹음 시각과 일치(추정)"
    if course and course in folders:
        return course, "high", "metadata", "앱에서 고른 과목과 녹음 시작 시각이 시간표와 일치"
    if not course and len(folders) == 1:
        return folders[0], "medium", "policy", "과목을 고르지 않았지만 녹음 시작 시각이 시간표의 한 수업과 겹침(추정)"
    if course and folders:
        return None, "low", "policy", f"고른 과목({course})이 시간표의 수업({', '.join(folders)})과 다름"
    if course:
        return None, "low", "policy", f"고른 과목({course})이 그 시각의 시간표에 없음(보강·시간변경일 수 있음)"
    if folders:
        return None, "low", "policy", f"여러 수업과 겹침({', '.join(folders)})"
    return None, "low", "policy", "과목을 고르지 않았고 그 시각에 시간표 수업이 없음"


def _lecture_for(connection, course_id: str, day: str) -> str:
    rows = connection.execute("SELECT id FROM Lecture WHERE course_id=? AND date=? ORDER BY session_index, id",
                              (course_id, day)).fetchall()
    if len(rows) > 1:
        raise ValueError("그 날짜에 같은 과목 회차가 여러 개라 자동으로 고르지 않습니다")
    if rows:
        return rows[0][0]
    lecture_id = new_ulid()
    connection.execute("INSERT INTO Lecture(id,course_id,date,session_index,title,lecture_type) VALUES (?,?,?,?,?,?)",
                       (lecture_id, course_id, day, 1, None, None))
    EventStore(connection).append("LECTURE_CREATED", "Lecture", lecture_id, {"course_id": course_id, "date": day,
                                                                             "by": "app_recording"})
    return lecture_id


def _register_media(connection, lecture_id, master: Path, digest, session, stop, decided_by, confidence, ffprobe) -> str:
    meta = probe_media(master, ffprobe)
    media_id = new_ulid()
    connection.execute(
        "INSERT INTO MediaAsset VALUES (?,?,?,?,?,?,?,?,?,?,?,1,NULL,NULL,?,?,'{}',?,?)",
        (media_id, lecture_id, str(master), digest, meta.duration_ms, meta.codec, meta.sample_rate, meta.channels,
         master.stat().st_size, "app_loopback" if session.get("mode") == "loopback" else "app_mic", "laptop",
         int(session["started_at"]), int(bool(stop.get("partial"))), decided_by, confidence))
    EventStore(connection).append("MEDIA_REGISTERED", "MediaAsset", media_id, {"lecture_id": lecture_id, "by": "app"})
    return media_id


def partner_lecture(connection: sqlite3.Connection, session_dir: Path) -> str | None:
    """The lecture of the other half of a mic + PC-sound pair (pair.json), if that half is already linked."""
    try:
        pair = json.loads((Path(session_dir) / "pair.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    me = str(Path(session_dir))
    other = pair.get("pc") if pair.get("mic") == me else pair.get("mic") if pair.get("pc") == me else None
    if not other:
        return None
    row = connection.execute("SELECT lecture_id FROM MediaAsset WHERE path LIKE ? AND lecture_id IS NOT NULL "
                             "ORDER BY recorded_start, id LIMIT 1", (str(Path(other)) + "\\%",)).fetchone()
    if row is None:
        row = connection.execute("SELECT lecture_id FROM MediaAsset WHERE path LIKE ? AND lecture_id IS NOT NULL "
                                 "ORDER BY recorded_start, id LIMIT 1", (str(Path(other)) + "/%",)).fetchone()
    return row[0] if row else None


def link_recording(connection: sqlite3.Connection, session_dir: Path, *, lead_min: int = DEFAULT_LEAD_MIN,
                   ffprobe: str | None = None) -> dict:
    """Register a finished app recording; the caller commits."""
    session_dir = Path(session_dir)
    session, master, stop = session_master(session_dir)
    if master is None or not master.is_file():
        return {"state": "not_ready", "reason": "병합된 녹음 파일이 아직 없습니다"}
    digest = _sha256(master)
    existing = connection.execute("SELECT id, lecture_id FROM MediaAsset WHERE sha256=?", (digest,)).fetchone()
    if existing:
        return {"state": "linked", "lecture_id": existing[1], "media_id": existing[0], "master": str(master),
                "reason": "이미 등록된 녹음", "already": True}
    pending = connection.execute("SELECT id, suggestion FROM IngestItem WHERE source_path=? AND state='pending'",
                                 (str(master),)).fetchone()
    if pending:
        return {"state": "pending", "ingest_id": pending[0], "master": str(master), **json.loads(pending[1])}
    started = datetime.fromisoformat(session["started_local"])
    app_course = _picked(session)
    root = session_dir.parent.parent.parent  # <root>/_lecturehub/recordings/<session>
    from lecturehub.core import extra_courses

    if app_course and extra_courses.is_extra(root, app_course):  # '수업 외': no timetable, a lecture per recording
        from .extra import ensure_course, new_lecture

        picked = (session.get("course_source") or "unknown") == "user_pick"
        # the mic and PC-sound files of one recording (pair.json) belong to one lecture, whichever links first
        lecture_id = partner_lecture(connection, session_dir) or new_lecture(
            connection, ensure_course(connection, app_course), started.date().isoformat())
        decided_by, confidence = ("metadata", "high") if picked else ("policy", "medium")
        media_id = _register_media(connection, lecture_id, master, digest, session, stop, decided_by, confidence, ffprobe)
        _evidence(connection, "MediaAsset", media_id, "extra_topic",
                  {"topic": app_course, "course_source": session.get("course_source") or "unknown"},
                  "metadata" if picked else "heuristic")
        return {"state": "linked", "lecture_id": lecture_id, "media_id": media_id, "course": app_course,
                "confidence": confidence, "decided_by": decided_by, "reason": "수업 외 항목으로 녹음", "master": str(master)}
    found = candidates(connection, started, lead_min)
    if not app_course and not found:  # no course and no class at that time: open it under 미분류, moved later
        from .extra import ensure_course, new_lecture

        lecture_id = partner_lecture(connection, session_dir) or new_lecture(
            connection, ensure_course(connection, extra_courses.UNSORTED), started.date().isoformat())
        media_id = _register_media(connection, lecture_id, master, digest, session, stop, "policy", "low", ffprobe)
        _evidence(connection, "MediaAsset", media_id, "unsorted",
                  {"course_source": session.get("course_source") or "unknown", "started_local": session["started_local"]},
                  "heuristic")
        return {"state": "linked", "lecture_id": lecture_id, "media_id": media_id, "course": extra_courses.UNSORTED,
                "confidence": "low", "decided_by": "policy", "unsorted": True,
                "reason": "과목 없이 녹음했고 그 시각에 시간표 수업이 없음 — 미분류에 둠", "master": str(master)}
    course, confidence, decided_by, reason = decide(app_course, found,
                                                    session.get("course_source") or "unknown")
    detail = {"session_dir": str(session_dir), "started_local": session["started_local"],
              "app_course": app_course, "course_source": session.get("course_source") or "unknown",
              "candidates": found, "reason": reason, "lead_min": lead_min}
    course_row = connection.execute("SELECT id FROM Course WHERE folder_name=?", (course,)).fetchone() if course else None
    if course and course_row:
        try:
            lecture_id = _lecture_for(connection, course_row[0], started.date().isoformat())
        except ValueError as exc:
            course, reason = None, str(exc)
        else:
            media_id = _register_media(connection, lecture_id, master, digest, session, stop, decided_by, confidence,
                                       ffprobe)
            _evidence(connection, "MediaAsset", media_id, "timetable_overlap", detail,
                      "metadata" if decided_by == "metadata" else "heuristic")
            return {"state": "linked", "lecture_id": lecture_id, "media_id": media_id, "course": course,
                    "confidence": confidence, "decided_by": decided_by, "reason": reason, "master": str(master)}
    suggestion = {**detail, "reason": reason, "date": started.date().isoformat(),
                  "course_hint": app_course, "sha256": digest}
    ingest_id = new_ulid()
    connection.execute("INSERT INTO IngestItem(id,source_path,suggestion,state) VALUES (?,?,?, 'pending')",
                       (ingest_id, str(master), json.dumps(suggestion, ensure_ascii=False)))
    EventStore(connection).append("INGEST_PENDING", "IngestItem", ingest_id, {"reason": reason})
    return {"state": "pending", "ingest_id": ingest_id, "master": str(master), **suggestion}


def confirm_pending(connection: sqlite3.Connection, ingest_id: str, course: str, day: str | None = None, *,
                    ffprobe: str | None = None) -> dict:
    """The user's one click: link a pending recording to course/date (confirmed_by_user)."""
    row = connection.execute("SELECT source_path, suggestion, state FROM IngestItem WHERE id=?", (ingest_id,)).fetchone()
    if row is None or row[2] != "pending":
        raise ValueError("확인할 미확정 녹음이 없습니다")
    suggestion = json.loads(row[1])
    master = Path(row[0])
    day = day or suggestion["date"]
    date.fromisoformat(day)
    session_dir = Path(suggestion["session_dir"])
    session, _master, stop = session_master(session_dir)
    from lecturehub.core import extra_courses

    if extra_courses.is_extra(session_dir.parent.parent.parent, course):  # '수업 외' picked for a pending one
        from .extra import ensure_course, new_lecture

        lecture_id = new_lecture(connection, ensure_course(connection, course), day)
    else:
        course_row = connection.execute("SELECT id FROM Course WHERE folder_name=?", (course,)).fetchone()
        if course_row is None:
            raise ValueError(f"없는 과목: {course}")
        lecture_id = _lecture_for(connection, course_row[0], day)
    media_id = _register_media(connection, lecture_id, master, suggestion["sha256"], session, stop, "user", "exact",
                               ffprobe)
    _evidence(connection, "MediaAsset", media_id, "user", {"ingest_id": ingest_id, "course": course, "date": day,
                                                          "suggestion_reason": suggestion["reason"]}, "user")
    connection.execute("UPDATE IngestItem SET state='confirmed' WHERE id=?", (ingest_id,))
    EventStore(connection).append("INGEST_CONFIRMED", "IngestItem", ingest_id, {"lecture_id": lecture_id,
                                                                                "media_id": media_id})
    return {"state": "linked", "lecture_id": lecture_id, "media_id": media_id, "course": course, "master": str(master)}


def pending_items(connection: sqlite3.Connection) -> list[dict]:
    return [{"ingest_id": r[0], "master": r[1], **json.loads(r[2])} for r in connection.execute(
        "SELECT id, source_path, suggestion FROM IngestItem WHERE state='pending' ORDER BY id")]


def register_job_transcript(connection: sqlite3.Connection, job_state: dict, *, allow_mock: bool = False) -> dict:
    """Register a finished transcription job's output(s) as Transcripts of the recording's lecture, then
    refresh the pointer. Any engine: Google, Whisper (Groq), MAI (one Transcript per MAI run, MAI-N-001).

    Mock jobs (verification transport) are refused unless explicitly allowed for a synthetic library.
    """
    from lecturehub.transcription.providers import provider as provider_info

    if job_state.get("state") not in ("done", "qa_warning") or not (job_state.get("outputs") or job_state.get("output")):
        return {"state": "not_ready"}
    if job_state.get("mock") and not allow_mock:
        return {"state": "refused_mock", "reason": "모의 전사는 실제 전사본으로 등록하지 않습니다"}
    info = provider_info(job_state.get("provider") or "gemini_transcribe")
    outputs = [Path(p) for p in (job_state.get("outputs") or [job_state["output"]])]
    results = [_register_output(connection, job_state, info, output) for output in outputs]
    now = [r.get("path") or str(o) for r, o in zip(results, outputs, strict=True)]
    if now != [str(o) for o in outputs]:  # moved into 전사본 (2026-11-14): the job remembers where its files are now
        if job_state.get("outputs"):
            job_state["outputs"] = now
        job_state["output"] = now[0]
        job_state["_paths_changed"] = True
    if len(results) == 1:
        return results[0]
    first = next((r for r in results if r["state"] == "registered"), results[0])
    return {**first, "transcript_ids": [r.get("transcript_id") for r in results if r.get("transcript_id")],
            "already": all(r.get("already") for r in results)}


def _job_media(connection: sqlite3.Connection, job_state: dict):
    media = connection.execute("SELECT id, lecture_id FROM MediaAsset WHERE path=?", (job_state["audio"],)).fetchone()
    if media is None and job_state.get("audio_sha256"):  # e.g. run on the 녹음본 copy of an app master (D-055)
        media = connection.execute("SELECT id, lecture_id FROM MediaAsset WHERE sha256=? AND lecture_id IS NOT NULL "
                                   "ORDER BY is_master DESC, id LIMIT 1", (job_state["audio_sha256"],)).fetchone()
    return media


def _root_of(output: Path) -> Path | None:
    for parent in Path(output).parents:
        if parent.name == "_lecturehub":
            return parent.parent
    return None


def _register_output(connection: sqlite3.Connection, job_state: dict, info, output: Path) -> dict:
    from . import transcript_files

    if not output.is_file():  # moved into 전사본 earlier (transcript_files.move_registered)
        row = connection.execute(
            "SELECT t.id, t.lecture_id, t.source_file FROM Evidence e JOIN Transcript t ON t.id = e.subject_id "
            "WHERE e.subject_type = 'Transcript' AND e.kind = 'user_file_move' AND json_valid(e.detail) "
            "AND json_extract(e.detail, '$.from') = ? ORDER BY e.created_at DESC LIMIT 1", (str(output),)).fetchone()
        if row is not None:
            return {"state": "registered", "transcript_id": row[0], "lecture_id": row[1], "already": True, "path": row[2]}
    digest = _sha256(output)
    existing = connection.execute("SELECT id, lecture_id, source_file FROM Transcript WHERE sha256=?", (digest,)).fetchone()
    if existing:
        return {"state": "registered", "transcript_id": existing[0], "lecture_id": existing[1], "already": True}
    media = _job_media(connection, job_state)
    if media is not None:
        from .discard import discarded_ids

        if media[0] in discarded_ids(connection):
            return {"state": "discarded", "reason": "버린 녹음의 전사는 등록하지 않습니다"}
    if media is None or media[1] is None:
        return {"state": "waiting_link", "reason": "녹음이 아직 회차에 연결되지 않았습니다(미확정)"}
    parsed = parse_file(output)
    if parsed.engine != info.output_format or not parsed.segments:
        raise ValueError(f"전사 결과를 읽지 못했습니다(판별 {parsed.engine})")
    preset = job_state.get("preset", {})
    version = preset.get("model") or preset.get("verified_at")
    root = _root_of(output)
    if root is not None and transcript_files.is_app_output(root, output):  # saved in 전사본\<과목> (2026-11-14)
        output = transcript_files.place(root, connection, media[1], info.engine, output)
    transcript_id = new_ulid()
    connection.execute(
        "INSERT INTO Transcript (id,lecture_id,media_id,engine,engine_version,role,role_decided_by,chunk_plan_id,"
        "status,source_file,sha256) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (transcript_id, media[1], media[0], info.engine, version, info.role, "policy", None, job_state["state"],
         str(output), digest))
    for seq, segment in enumerate(parsed.segments):
        connection.execute("INSERT INTO TranscriptSegment (transcript_id,seq,start_ms,end_ms,speaker,text,chunk_index) "
                           "VALUES (?,?,?,?,?,?,0)", (transcript_id, seq, segment.start_ms, segment.end_ms,
                                                      segment.speaker, segment.text))
    qa = sorted({issue["check"] for chunk in job_state.get("chunks", []) for issue in chunk.get("qa", [])})
    _evidence(connection, "Transcript", transcript_id, "app_job", {"job_id": job_state["job_id"], "qa": qa,
                                                                  "chunks": len(job_state.get("chunks", [])),
                                                                  "provider": info.name}, "engine")
    if qa:
        _evidence(connection, "Transcript", transcript_id, "qa_warning", {"checks": qa}, "engine")
    EventStore(connection).append("TRANSCRIPT_IMPORTED", "Transcript", transcript_id,
                                  {"lecture_id": media[1], "job_id": job_state["job_id"], "engine": info.engine})
    changed = lectures.refresh_policy(connection, media[1])
    return {"state": "registered", "transcript_id": transcript_id, "lecture_id": media[1], "qa": qa,
            "engine": info.engine, "in_use_changed": bool(changed), "path": str(output)}


def sync_jobs(connection: sqlite3.Connection, jobs_dir: Path, *, allow_mock: bool = False) -> list[dict]:
    """Register every finished job whose recording is linked (idempotent; used at app start)."""
    results = []
    if not Path(jobs_dir).is_dir():
        return results
    for folder in sorted(Path(jobs_dir).iterdir()):
        state_path = folder / "state.json"
        if not state_path.is_file():
            continue
        state = _read(state_path)
        if state.get("dry_run_1min"):
            continue
        try:
            result = register_job_transcript(connection, state, allow_mock=allow_mock)
            if state.pop("_paths_changed", False):
                tmp = state_path.with_suffix(".tmp")
                tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
                tmp.replace(state_path)
        except (OSError, ValueError, MediaProbeError) as exc:
            result = {"state": "error", "reason": str(exc)}
        results.append({"job_id": state.get("job_id"), **result})
    return results
