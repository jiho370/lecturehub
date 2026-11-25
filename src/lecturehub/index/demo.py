"""D-085 example semester, built locally with invented course material.

Existing folders are preserved; legacy API identifiers stay compatible with installed clients.
No network, credentials, personal data or global settings are used by make().
"""
from __future__ import annotations

import hashlib
import json
import re
from contextlib import closing
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path

from .demo_content import COURSE, COURSES, SLIDES, SOIL_TRANSCRIPT, TOPICS, TRANSCRIPT, UNIT_SLIDES, UNIT_TRANSCRIPT

LECTURE_TITLE = "합리식과 도달시간"
CLASS_LENGTH = timedelta(minutes=170)  # three-hour blocks, 09:00–11:50 (the week grid reads better than 75-minute ones)


def default_dest() -> Path:
    from lecturehub.core import settings

    # Existing data_root settings are never rewritten or moved; new examples use the new display name.
    return settings.settings_path().parent / "예시 학기"


@lru_cache(maxsize=8)
def _audio_metadata(path: Path):
    from .media import probe_media

    return probe_media(path)


def _recording(connection, root: Path, started: datetime, course: str, lecture_id: str | None = None) -> dict:
    """Compact silent audio with a measured 50-minute timeline; never opens a capture device.

    A small ISO-BMFF free box makes each invented recording's identity unique. The audio stream is shared
    conceptually, but each file is independently valid. No speech or real class recording is distributed.
    """
    import shutil
    import subprocess

    from lecturehub.core import proc
    from lecturehub.core.events import EventStore
    from lecturehub.core.ids import new_ulid
    from . import app_ingest, schedule

    template = root / "_lecturehub" / "example_audio.m4a"
    if not template.is_file():
        subprocess.run([shutil.which("ffmpeg") or "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                        "-f", "lavfi", "-i", "anullsrc=r=8000:cl=mono", "-t", "3000", "-c:a", "aac", "-b:a", "8k",
                        "-threads", "1", "-n", str(template)], check=True, capture_output=True, timeout=30,
                       creationflags=proc.NO_WINDOW | proc.BELOW_NORMAL)
    cid = schedule.resolve_course(connection, course)
    lecture_id = lecture_id or app_ingest._lecture_for(connection, cid, str(started.date()))
    path = root / "_lecturehub" / "recordings" / f"{started:%Y%m%d_%H%M}_{course}" / "master.m4a"
    path.parent.mkdir(parents=True, exist_ok=True)
    tag = f"{course} {started.isoformat()}".encode("utf-8")
    path.write_bytes(template.read_bytes() + (len(tag) + 8).to_bytes(4, "big") + b"free" + tag)
    measured = _audio_metadata(template)
    mid = new_ulid()
    connection.execute("INSERT INTO MediaAsset(id,lecture_id,path,sha256,duration_ms,codec,sample_rate,channels,bytes,"
                       "origin,device_type,is_master,recorded_start,partial,loudness_profile,decided_by,confidence_class) "
                       "VALUES (?,?,?,?,?,?,?,?,?,'app_mic','laptop',1,?,0,'{}','user','high')",
                       (mid, lecture_id, str(path), hashlib.sha256(path.read_bytes()).hexdigest(), measured.duration_ms,
                        measured.codec, measured.sample_rate, measured.channels, path.stat().st_size, int(started.timestamp() * 1000)))
    EventStore(connection).append("MEDIA_REGISTERED", "MediaAsset", mid, {"lecture_id": lecture_id, "by": "example"})
    return {"lecture_id": lecture_id, "master": str(path), "state": "linked"}


def _answer(prompt_text: str, schema: dict) -> dict:
    """Prepared local engine response; the real evidence validation still runs."""
    tid = re.search(r"전사ID ([^)\s]+)", prompt_text).group(1)
    mid = re.search(r"\[([^\s]+) p1\]", prompt_text).group(1)
    unit = "직접유출수문곡선으로" in prompt_text
    refs = lambda seq, page: [{"t": tid, "s": seq}, {"m": mid, "p": page}]  # noqa: E731
    if unit:
        sections = [
            ("기저유출 분리와 체적", "관측 유량에서 기저유출을 분리한다. 직접유출수문곡선을 적분할 때 시간 간격은 초로 환산한다.", 1, 1),
            ("단위도 유도", "단위 유효우량은 1 cm이다. 유효우량이 2 cm라면 직접유출 유량의 각 종거를 2로 나눈다. 지속시간도 함께 적는다.", 9, 2),
            ("중첩과 검산", "선형 비례와 시간 불변을 가정해 확대·시간 이동한 단위도를 합산한다. 단위도의 체적은 유역 면적 × 0.01 m와 같아야 한다.", 14, 3),
        ]
    else:
        sections = [
            ("합리식의 단위", "Q = 0.278 C I A에서 Q는 m³/s, I는 mm/h, A는 km²이다. 유출계수 C는 무차원이다.", 2, 1),
            ("유출계수와 도달시간", "포장면 0.8 km²와 녹지 1.2 km²에 각각 0.8, 0.3을 적용하면 면적가중 유출계수는 0.5이다. 강우 지속시간은 도달시간과 같게 잡는다.", 8, 2),
            ("설계강우와 계산", "도달시간 30분에서 읽은 강우강도 60 mm/h와 면적 2 km²를 적용한다. 첨두유량은 0.278 × 0.5 × 60 × 2 = 16.68 m³/s이다.", 15, 3),
            ("과제와 중간 점검", "과제는 면적을 km²로 통일하고 단위를 쓴다. 퀴즈에는 계산기를 준비하고 유출계수·도달시간·IDF 곡선 읽기를 복습한다.", 19, 4),
        ]
    blocks = []
    for i, (heading, text, seq, page) in enumerate(sections):
        citations = refs(seq, page)
        if not unit and i == 1:
            citations.append({"t": tid, "s": 11})  # rainfall duration = time of concentration
        if not unit and i == 3:
            citations += [{"t": tid, "s": 17}, {"t": tid, "s": 20}]  # assignment condition and quiz scope
        blocks += [{"key": f"h{i}", "kind": "heading", "text": heading},
                   {"key": f"p{i}", "kind": "paragraph", "text": text, "cites": citations}]
    return {"model": "codex", "blocks": blocks}


def _handout(path: Path) -> None:
    """Small OOXML hand-out, readable by Word and the app's own text extractor; stdlib only."""
    from xml.sax.saxutils import escape
    from zipfile import ZIP_DEFLATED, ZipFile

    paragraphs = ["관개 용수량 산정", "풀이 순서", "작물증발산량 ETc = Kc × ETo",
                  "ETo가 4 mm/day이고 Kc가 1.1이면 ETc는 4.4 mm/day이다.",
                  "작물증발산량에서 유효우량과 이용 가능한 토양수분을 빼 순관개수량을 구한다.",
                  "순관개수량 30 mm, 관개 효율 0.75이면 총관개수량은 40 mm이다.",
                  "관개 일정", "근권의 유효수분과 허용 고갈률을 바탕으로 관개 시기를 정한다.",
                  "수로는 Manning 식, 관수로는 마찰손실과 국부손실을 고려해 통수 능력을 확인한다.",
                  "제출할 것", "단위 면적당 수심(mm)과 전체 체적(m³)을 구분하고 관개 효율의 적용 과정을 쓴다."]
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(f'<w:p><w:r><w:t>{escape(t)}</w:t></w:r></w:p>' for t in paragraphs)
    with ZipFile(path, "w", ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        z.writestr("_rels/.rels", '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        z.writestr("word/document.xml", f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>{body}<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/></w:sectPr></w:body></w:document>')


def _quote(connection, event_id, detail, now, *, etl=False):
    from lecturehub.core.ids import new_ulid

    connection.execute("INSERT INTO Evidence(id,subject_type,subject_id,kind,detail,source,created_at) VALUES (?,?,?,?,?,?,?)",
                       (new_ulid(), "CalendarEvent", event_id, "board_quote" if etl else "transcript_quote",
                        json.dumps(detail, ensure_ascii=False), "metadata" if etl else "engine", int(now.timestamp() * 1000)))


def _showcase(connection, dest: Path, now: datetime, rich: list[dict], notes: list[dict]) -> None:
    from lecturehub.mcp import changes, rescan_queue
    from . import calendar_events as cal, schedule

    today = now.date()
    change_count = 0

    def log(record):
        nonlocal change_count
        stamp = now - timedelta(minutes=35 - change_count * 5)
        record.update(at=stamp.isoformat(timespec="seconds"), at_ms=int(stamp.timestamp() * 1000))
        changes.append(dest, record)
        change_count += 1
    monday = today - timedelta(days=today.weekday())
    course_id = schedule.resolve_course(connection, COURSE)
    soil_id = schedule.resolve_course(connection, "토질역학")
    # The quiz and "that week's Friday" both resolve from the dated lecture, never the host's current date.
    quiz_day = rich[0]["day"].date() + timedelta(days=(2 - rich[0]["day"].weekday()) % 7 + 7)
    moved_day = quiz_day + timedelta(days=2)
    for seq, kind, title, day in [(19, "quiz", "중간 점검 퀴즈", quiz_day),
                                  (21, "closure", "학회 참석으로 수업 이동", moved_day)]:
        eid = cal.add(connection, kind=kind, title=title, day=str(day), course_id=course_id, source="transcript",
                      source_ref=f"{rich[0]['transcript_id']}#{seq}", status="candidate", note="발언과 날짜를 확인해 주세요.")
        mm, ss = map(int, TRANSCRIPT[seq][0].split(":"))
        _quote(connection, eid, {"lecture_id": rich[0]["lecture_id"], "transcript_id": rich[0]["transcript_id"],
                                "seq": seq, "at_ms": (mm * 60 + ss) * 1000, "quote": TRANSCRIPT[seq][1]}, now)
    # A term's worth of dated items, so the month calendar is not empty outside this week: midterms five weeks ago,
    # a presentation and a report in two weeks, finals in the term's last week (the class's own weekday and hour).
    for course, kind, title, offset_days, start in [
            ("관개배수공학", "exam", "중간고사", -35, "13:00"), ("토질역학", "exam", "중간고사", -34, "10:00"),
            (COURSE, "exam", "중간고사", -33, "09:00"),
            ("관개배수공학", "presentation", "관개 계획 발표", 16, "13:00"), ("토질역학", "report", "투수시험 보고서", 18, None),
            ("관개배수공학", "exam", "기말고사", 21, "13:00"), ("토질역학", "exam", "기말고사", 22, "10:00"),
            (COURSE, "exam", "기말고사", 23, "09:00")]:
        cal.add(connection, kind=kind, title=title, day=str(monday + timedelta(days=offset_days)),
                course_id=schedule.resolve_course(connection, course), start=start)
    cancel_day = monday + timedelta(days=3)
    closure = cal.add(connection, kind="closure", title="실험실 점검으로 휴강", day=str(cancel_day),
                      course_id=soil_id, source="transcript", status="candidate", source_ref="trial-confirmed")
    _quote(connection, closure, {"lecture_id": rich[2]["lecture_id"], "transcript_id": rich[2]["transcript_id"],
                                "seq": 21, "at_ms": 1920000, "quote": rich[2]["rows"][21][1]}, now)
    cal.set_status(connection, closure, "confirmed", "발언을 듣고 날짜 확인")
    schedule.add_exceptions(connection, [cancel_day], course_id=soil_id, kind="휴강", note="실험실 점검 · 수업 중 안내 확인")
    makeup_day = monday + timedelta(days=4)
    makeup_note = "15:00-17:50 실험실 점검에 따른 보강 · 강의실: 301호"
    exception = schedule.add_exceptions(connection, [makeup_day], course_id=soil_id, kind="보강", note=makeup_note)
    etl = dest / "_lecturehub" / "etl"
    etl.mkdir(parents=True, exist_ok=True)
    assignments = [{"id": f"trial-{n}", "course": course, "title": title, "due": due.strftime("%Y-%m-%dT%H:%M"),
                    "submission": {"workflow_state": state, **({"submitted_at": (due - timedelta(hours=6)).isoformat()} if state == "submitted" else {})}}
                   for n, course, title, due, state in [
                       (1, COURSE, "합리식으로 첨두유량 구하기", now.replace(hour=23, minute=59, second=0, microsecond=0), "unsubmitted"),
                       (2, "토질역학", "다짐 시험 보고서", (now - timedelta(days=2)).replace(hour=23, minute=59), "submitted"),
                       (3, "관개배수공학", "관개 용수량 산정", (now - timedelta(days=1)).replace(hour=23, minute=59), "submitted"),
                       (4, COURSE, "단위도 유도 과제", (now + timedelta(days=7)).replace(hour=23, minute=59), "unsubmitted")]]
    (etl / "assignments.json").write_text(json.dumps(assignments, ensure_ascii=False), encoding="utf-8")
    (etl / "sync.json").write_text(json.dumps({"ok_at": now.timestamp(), "count": 4, "with_submission": True}), encoding="utf-8")
    cal.import_etl(connection, etl / "assignments.json")
    review_day = monday + timedelta(days=7)
    quote = f"{review_day:%m월 %d일} 수업에는 관개 용수량 계산표를 지참하세요. 관개 일정 검토를 진행합니다."
    announcements = [
        {"id": "trial-notice", "course": "관개배수공학", "title": "관개 일정 검토 준비", "text": quote},
        {"id": "trial-idf", "course": COURSE, "title": "IDF 곡선 자료와 풀이 안내", "text": "자료실에 설계강우와 IDF 곡선 자료를 올렸습니다. 과제에서는 재현기간 10년 곡선을 사용하세요."},
        {"id": "trial-soil", "course": "토질역학", "title": "투수시험 측정표 안내", "text": "정수위·변수위 투수시험 측정표를 올렸습니다. 수온과 수두차를 빠짐없이 기록하세요."},
        {"id": "trial-unit", "course": COURSE, "title": "단위도 유도 과제 제출 형식", "text": "계산표와 그래프를 PDF 하나로 제출하세요. 기저유출 분리 방법과 유효우량 지속시간을 함께 적습니다."},
    ]
    for a in announcements:
        a.update(posted=now.isoformat(), attachments=[])
    (etl / "announcements.json").write_text(json.dumps(announcements, ensure_ascii=False), encoding="utf-8")
    (etl / "files.json").write_text(json.dumps({"at": now.timestamp(), "closed": [], "files": [
        {"id": "trial-file-idf", "course": COURSE, "name": "설계강우 IDF 곡선 읽기.pdf", "size": 48216,
         "updated": now.isoformat(), "url": "", "where": "강의자료"},
    ]}, ensure_ascii=False), encoding="utf-8")
    (etl / "news.json").write_text(json.dumps([
        {"kind": "files_new", "course": COURSE, "names": ["설계강우 IDF 곡선 읽기.pdf"], "count": 1,
         "key": "trial-new-idf", "found_at": now.timestamp()},
    ], ensure_ascii=False), encoding="utf-8")
    cid = schedule.resolve_course(connection, "관개배수공학")
    candidate = cal.add(connection, kind="presentation", title="관개 일정 검토", day=str(review_day),
                        course_id=cid, source="etl", source_ref="ain:trial-notice:presentation", status="candidate")
    _quote(connection, candidate, {"post_id": "trial-notice", "announcement": True, "board": "공지", "title": announcements[0]["title"],
                                  "posted": now.isoformat(), "quote": quote, "quotes": [quote], "url": ""}, now, etl=True)
    exam_id = cal.add(connection, kind="quiz", title="중간 점검 퀴즈 준비", day=str(quiz_day), course_id=course_id,
                      start="09:00", note="계산기 · 유출계수·도달시간·IDF 곡선", source_ref="ai_app:trial-quiz", by="ai_app")
    log(changes.entry("add_calendar_event", COURSE, "중간 점검 퀴즈 준비 일정 추가",
                                      {"kind": "calendar", "event_id": exam_id, "after": cal.snapshot(connection, exam_id)}))
    # by lecture, not by the path the note job returned: a temp folder can be spelled two ways (8.3 short name on the
    # build machine), and then the path never matched (self-check of the built app, 2026-11-26)
    note_row = connection.execute("SELECT id,path,lecture_ids FROM StudyArtifact WHERE kind='note' AND lecture_ids LIKE ? "
                                  "ORDER BY rowid DESC", (f'%{rich[0]["lecture_id"]}%',)).fetchone()
    log(changes.entry("save_note", COURSE, "합리식과 도달시간 정리본 저장",
                                      {"kind": "note", "artifact_id": note_row[0], "path": note_row[1],
                                       "lecture_ids": json.loads(note_row[2]), "sha256": hashlib.sha256(Path(note_row[1]).read_bytes()).hexdigest()}))
    log(changes.entry("add_schedule_change", "토질역학", "금요일 15:00 보강 추가 · 301호",
                                      {"kind": "schedule_change", "exception_id": exception.created[0], "course_id": soil_id,
                                       "date": str(makeup_day), "change": "보강", "note": makeup_note}))
    request = changes.entry("request_notice_rescan", COURSE, "합리식 수업에서 공지 다시 찾기 요청",
                            {"kind": "rescan", "lecture_id": rich[0]["lecture_id"]})
    request["undo"]["change_id"] = request["id"]
    rescan_queue.add(dest, rich[0]["lecture_id"], request["id"], COURSE)
    log(request)
    # Same evidence in the notice banner and the calendar, including the confirmed cancellation.
    inbox = []
    for item in cal.candidates(connection):
        ev = item["evidence"]
        inbox.append({"key": "trial-" + item["id"], "event_id": item["id"], "course": item["course"],
                      "course_id": item["course_id"], "lecture_id": ev.get("lecture_id"),
                      "lecture_date": str(rich[0]["day"].date()), "kind": item["kind"], "kind_label": item["kind_label"],
                      "date": item["date"], "summary": item["title"], "quote": ev["quote"],
                      "certainty": "high", "certainty_label": "높음", "speaker": "professor", "stt_note": "",
                      "at_ms": ev.get("at_ms", 0), "engines": ["Google"], "engine": "Google", "model": "codex",
                      "source": item["source"], "ann_title": ev.get("title", ""), "posted": ev.get("posted", ""),
                      "date_reasoning": "수업 날짜에서 다음 주 수요일을 구하고, 그 주 금요일은 이틀 뒤로 계산했습니다."
                                        if item["kind"] == "closure" else "발언 또는 공지에 적힌 날짜를 기준으로 계산했습니다.",
                      "found_at": int(now.timestamp() * 1000), "seen": True})
    notices = dest / "_lecturehub" / "notices"
    notices.mkdir(parents=True, exist_ok=True)
    (notices / "inbox.json").write_text(json.dumps(inbox, ensure_ascii=False), encoding="utf-8")


def make(dest: Path | None = None, *, now: datetime | None = None) -> dict:
    """Build once in the app-private destination; reopening preserves the existing semester."""
    from lecturehub.core import db, timetable
    from lecturehub.core.ids import new_ulid
    from lecturehub.materials import extract, pdfdoc
    from lecturehub.study import note_job
    from . import app_ingest, index_root, lectures, schedule

    dest = Path(dest or default_dest())
    now = (now or datetime.now()).astimezone()
    database = dest / "_lecturehub" / "lecturehub.sqlite"
    if database.is_file():
        with closing(db.connect(database)) as connection:
            if connection.execute("SELECT count(*) FROM StudyArtifact WHERE kind='note'").fetchone()[0] >= 2:
                return {"root": str(dest), "state": "ready", "course": COURSE}
    dest.mkdir(parents=True, exist_ok=True)
    timetable.write(dest, [
        {"name": COURSE, "slots": [{"day": d, "start": "09:00", "end": "11:50"} for d in "수금"]},
        {"name": "토질역학", "slots": [{"day": d, "start": "10:00", "end": "12:50"} for d in "화목"]},
        {"name": "관개배수공학", "slots": [{"day": d, "start": "13:00", "end": "15:50"} for d in "월수"]},
    ], now=now)
    monday = (now - timedelta(days=now.weekday())).replace(hour=9, minute=5, second=0, microsecond=0)
    timetable.save_term(dest, str((monday - timedelta(weeks=11)).date()), str((monday + timedelta(weeks=4)).date()))
    pdfdoc.write_slides(dest / COURSE / "강의자료" / "강우–유출과 합리식.pdf", SLIDES)
    pdfdoc.write_slides(dest / COURSE / "강의자료" / "단위도와 홍수추적.pdf", UNIT_SLIDES)
    pdfdoc.write_slides(dest / "토질역학" / "강의자료" / "투수계수와 유효응력.pdf", [
        ("Darcy의 법칙", ["Q = k i A · i = Δh / L", "k: m/s · 정수위 시험과 변수위 시험", "Darcy 유속과 공극 속 침투유속 구분"]),
        ("유효응력과 압밀", ["포화토: 유효응력 = 전응력 − 간극수압", "과잉간극수압 소산에 따른 일차압밀", "다짐: 함수비–건조밀도 곡선과 최적함수비"]),
    ])
    _handout(dest / "관개배수공학" / "강의자료" / "관개 용수량 산정 안내.docx")
    index_root(dest)
    rich = []
    notes = []
    # Three full transcripts, two evidence-checked notes. All dates derive from the supplied clock.
    def held(start: datetime) -> datetime:
        """The latest class of that weekly slot that is already over at ``now``."""
        return start if start + CLASS_LENGTH <= now else start - timedelta(weeks=1)

    # The second 수문학 lecture is the Wednesday before the first, whichever week the first falls in: early in a
    # week (before Wednesday's class is over) the first moves back a week and used to land on the second's date,
    # two recordings with one identity (found by building the semester at 128 clocks, 2026-11-26).
    main = held(monday + timedelta(days=2))
    specs = [(COURSE, main, TRANSCRIPT, LECTURE_TITLE),
             (COURSE, main - timedelta(weeks=1), UNIT_TRANSCRIPT, "단위도 유도와 중첩"),
             ("토질역학", held((monday + timedelta(days=1)).replace(hour=10)), SOIL_TRANSCRIPT, "투수계수와 유효응력")]
    with closing(db.connect(database)) as connection:
        extract.extract(connection, dest)
        for course, started, rows, title in specs:
            rows = list(rows)
            if course == "토질역학":
                when = "다음 주" if started.date() < monday.date() else "이번 주"
                rows[21] = ("32:00", f"{when} 목요일은 실험실 점검으로 휴강합니다. 그 주 금요일 오후 세 시에 301호에서 보강할게요.")
            linked = _recording(connection, dest, started, course)
            connection.execute("UPDATE Lecture SET title=? WHERE id=?", (title, linked["lecture_id"]))
            output = dest / "_lecturehub" / "transcripts" / f"{started:%y.%m.%d} {course}.txt"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text("".join(f"{clock} \n{text}\n\n" for clock, text in rows), encoding="utf-8-sig")
            tr = app_ingest.register_job_transcript(connection, {"job_id": "trial", "state": "done", "audio": linked["master"],
                                                                 "output": str(output), "preset": {"verified_at": "1.1.0"}, "chunks": []})
            rich.append({**linked, **tr, "day": started, "rows": rows})
            cid = schedule.resolve_course(connection, course)
            material = connection.execute("SELECT id,page_count FROM Material WHERE course_id=? AND title LIKE ?",
                                           (cid, "%" + ("단위도" if rows == list(UNIT_TRANSCRIPT) else "합리식" if course == COURSE else "투수계수") + "%")).fetchone()
            if material:
                connection.execute("INSERT INTO MaterialMatch(lecture_id,material_id,page_from,page_to,confidence_class,decided_by) "
                                   "VALUES (?,?,?,?,?,?)", (linked["lecture_id"], material[0], 1, material[1], "high", "user"))
            connection.commit()
        # A term's dated history, including classes without recordings. No fabricated attendance claims.
        for course in COURSES:
            cid = schedule.resolve_course(connection, course)
            days = {COURSE: [2, 4], "토질역학": [1, 3], "관개배수공학": [0, 2]}[course]
            for week in range(12):
                for slot, day in enumerate(days):
                    lecture_day = (monday - timedelta(weeks=11 - week) + timedelta(days=day)).date()
                    history_start = datetime.combine(lecture_day, monday.time(), tzinfo=now.tzinfo).replace(
                        hour={COURSE: 9, "토질역학": 10, "관개배수공학": 13}[course])
                    if history_start + CLASS_LENGTH > now:
                        continue
                    if course == "토질역학" and lecture_day == (monday + timedelta(days=3)).date():
                        continue
                    if not connection.execute("SELECT 1 FROM Lecture WHERE course_id=? AND date=?", (cid, str(lecture_day))).fetchone():
                        connection.execute("INSERT INTO Lecture(id,course_id,date,session_index,title,lecture_type) VALUES (?,?,?,?,?,?)",
                                           (new_ulid(), cid, str(lecture_day), 1, TOPICS[course][min(week, 11)], "실습·대화" if slot else "교수단독"))
                        if week or slot:  # three early classes intentionally have no recording
                            lid = connection.execute("SELECT id FROM Lecture WHERE course_id=? AND date=?", (cid, str(lecture_day))).fetchone()[0]
                            _recording(connection, dest, history_start, course, lid)
        # Generate after the whole history exists, so note titles agree with the sidebar's term-wide numbering.
        for r in rich[:2]:
            notes.append(note_job.run(connection, dest, r["lecture_id"], executor=_answer, executor_label="app"))
        _showcase(connection, dest, now, rich, notes)
        connection.commit()
        for r in rich:
            lectures.refresh_policy(connection, r["lecture_id"])
        connection.commit()
    return {"root": str(dest), "state": "made", "course": COURSE, "lecture_id": rich[0]["lecture_id"],
            "note": notes[0].get("html"), "rejected": notes[0].get("rejected")}


if __name__ == "__main__":
    import sys

    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(make(), ensure_ascii=False))
