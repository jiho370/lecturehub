"""Exam prep: one board per course and practice questions with a record of what went wrong (user 2026-10-11).

"시험 대비 화면 — 과목별 시험 일정과 범위, 수업 중 '시험에 낸다'고 한 발언, 대비 자료와 문제 풀기를 한곳에.
틀린 문제만 다시 푸는 기능까지."

Nothing here decides anything about an exam. Dates are the calendar's (user / eTL confirmed; a date heard in
class stays a candidate with its quote), cues are the ExamCue rows the transcript scan wrote, prep files are the
HTML files in the course folder whose name says so. Questions come from a bank file (JSON) under
``_lecturehub\\quiz\\<course>\\`` — written by an AI from the notes and slides, so every item carries where it
came from (``origin``: claude | class_example | textbook) and is shown with that label. QuizSet.created_by is
'ai' by schema; the user's answers are QuizAttempt rows and are never rewritten.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from datetime import UTC, date, datetime
from pathlib import Path

from lecturehub.core.events import EventStore
from lecturehub.core.ids import new_ulid

BANK_VERSION = 1
TYPES = ("mcq", "short", "ox", "calc", "essay")
AUTO = ("mcq", "ox")  # the app marks these; for the others the answer is shown and the user marks it
ORIGINS = {"claude": "Claude가 만든 문제(교수 출제 아님)", "class_example": "수업 예제", "textbook": "교재 예제",
           "past_exam": "기출 문제", "past_exam_variant": "기출 변형(Claude가 숫자·조건을 바꿈)"}
# the user's rule of 2026-10-11: assume an exam comes out nearly like the past ones — those questions come first
_ORIGIN_RANK = {"past_exam": 0, "past_exam_variant": 1}
MODES = ("mix", "wrong", "new", "past", "all")
_PREP_NAME = re.compile(r"대비|시험|고사|퀴즈|문제|기출|족보|prüfung|pruefung|exam", re.IGNORECASE)
_CUE_LABEL = {"exam_mention": "시험 언급", "exam_exclusion": "안 낸다고 함", "emphasis": "강조", "scope_notice": "일정·범위 언급"}


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def bank_dir(root: Path) -> Path:
    return Path(root) / "_lecturehub" / "quiz"


# ------------------------------------------------------------------ bank files


def _clean(item: dict, number: int) -> dict:
    """One item of a bank file, checked. Raises ValueError that names the item."""
    def bad(why: str) -> ValueError:
        return ValueError(f"{number}번 문제: {why}")

    kind = item.get("type")
    if kind not in TYPES:
        raise bad(f"type은 {', '.join(TYPES)} 중 하나입니다")
    question, answer = str(item.get("question") or "").strip(), str(item.get("answer") or "").strip()
    if not question or not answer:
        raise bad("question과 answer가 필요합니다")
    choices = item.get("choices")
    if kind == "mcq":
        if not isinstance(choices, list) or len(choices) < 2 or len({str(c).strip() for c in choices}) != len(choices):
            raise bad("객관식은 서로 다른 choices가 둘 이상 필요합니다")
        choices = [str(c).strip() for c in choices]
        if answer not in choices:
            raise bad("객관식의 answer는 choices 중 하나와 글자 그대로 같아야 합니다")
    elif kind == "ox":
        answer = answer.upper()
        if answer not in ("O", "X"):
            raise bad("OX의 answer는 O 또는 X입니다")
        choices = None
    else:
        choices = None
    origin = item.get("origin", "claude")
    if origin not in ORIGINS:
        raise bad(f"origin은 {', '.join(ORIGINS)} 중 하나입니다")
    sources = item.get("sources") or []
    if not isinstance(sources, list) or not all(isinstance(s, str) for s in sources):
        raise bad("sources는 글자 목록입니다")
    flags = [f for f in (item.get("flags") or []) if isinstance(f, str) and not f.startswith(("origin:", "topic:"))]
    flags.append(f"origin:{origin}")
    topic = str(item.get("topic") or "").strip()
    if topic:
        flags.append(f"topic:{topic}")
    return {"type": kind, "question": question, "choices": choices, "answer": answer,
            "explanation": str(item.get("explanation") or "").strip(), "sources": sources,
            "quoted_evidence": str(item.get("evidence") or "").strip(),
            "difficulty": str(item.get("difficulty") or "보통").strip(), "flags": flags}


def read_bank(path: Path) -> dict:
    """A bank file as {"course", "bank", "generator", "items"}; every item checked."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except ValueError as exc:
        raise ValueError(f"문제 파일을 읽지 못했습니다(JSON 형식 오류): {exc}") from exc
    if not isinstance(data, dict) or data.get("version") != BANK_VERSION:
        raise ValueError(f"문제 파일의 version은 {BANK_VERSION}이어야 합니다")
    course, bank = str(data.get("course") or "").strip(), str(data.get("bank") or "").strip()
    if not course or not bank:
        raise ValueError("문제 파일에 course(과목 폴더 이름)와 bank(묶음 이름)가 필요합니다")
    items = data.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("문제 파일에 items가 없습니다")
    cleaned = [_clean(item if isinstance(item, dict) else {}, number) for number, item in enumerate(items, 1)]
    keys = [(i["type"], i["question"]) for i in cleaned]
    if len(set(keys)) != len(keys):
        raise ValueError("같은 문제가 두 번 들어 있습니다")
    return {"course": course, "bank": bank, "generator": str(data.get("generator") or "unknown"), "items": cleaned}


def import_bank(connection: sqlite3.Connection, root: Path, path: str | Path) -> dict:
    """Read a bank file into QuizSet/QuizItem. Caller commits.

    The same file twice changes nothing. A newer file of the same bank keeps the items whose question is
    unchanged (and so their attempts), takes over their answer and explanation, adds the new ones and
    retires (status rejected) the ones it no longer has."""
    root, source = Path(root).resolve(), Path(path).resolve()
    if not source.is_file() or not source.is_relative_to(root):
        raise ValueError("학기 폴더 안의 문제 파일만 가져옵니다.")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    bank = read_bank(source)
    course = connection.execute("SELECT id FROM Course WHERE folder_name = ?", (bank["course"],)).fetchone()
    if course is None:
        raise ValueError(f"없는 과목입니다: {bank['course']}")
    sets = [(set_id, json.loads(scope)) for set_id, scope in connection.execute(
        "SELECT id, scope FROM QuizSet WHERE course_id = ?", (course[0],))]
    if any(scope.get("sha256") == digest for _, scope in sets):
        return {"state": "already", "course": bank["course"], "bank": bank["bank"], "added": 0, "updated": 0, "retired": 0}
    earlier = [set_id for set_id, scope in sets if scope.get("bank") == bank["bank"]]
    old = {}
    for set_id in earlier:
        for item_id, kind, question, status in connection.execute(
                "SELECT id, type, question, status FROM QuizItem WHERE quiz_set_id = ?", (set_id,)):
            old[(kind, question)] = (item_id, status)
    set_id = new_ulid()
    connection.execute(
        "INSERT INTO QuizSet (id, course_id, scope, created_by, created_at, generator_version) VALUES (?,?,?,'ai',?,?)",
        (set_id, course[0], json.dumps({"bank": bank["bank"], "sha256": digest, "file": source.name},
                                       ensure_ascii=False), _now_ms(), bank["generator"]))
    added = updated = 0
    for item in bank["items"]:
        values = (json.dumps(item["choices"], ensure_ascii=False) if item["choices"] is not None else None,
                  item["answer"], item["explanation"], json.dumps(item["sources"], ensure_ascii=False),
                  item["quoted_evidence"], item["difficulty"], json.dumps(item["flags"], ensure_ascii=False))
        known = old.pop((item["type"], item["question"]), None)
        if known:
            connection.execute(
                "UPDATE QuizItem SET choices=?, answer=?, explanation=?, sources=?, quoted_evidence=?, difficulty=?, "
                "flags=?, status='active' WHERE id=?", (*values, known[0]))
            updated += 1
        else:
            connection.execute(
                "INSERT INTO QuizItem (id, quiz_set_id, type, question, choices, answer, explanation, sources, "
                "quoted_evidence, difficulty, flags, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,'active')",
                (new_ulid(), set_id, item["type"], item["question"], *values))
            added += 1
    retired = [item_id for item_id, status in old.values() if status == "active"]
    connection.executemany("UPDATE QuizItem SET status='rejected' WHERE id=?", [(i,) for i in retired])
    EventStore(connection).append("QUIZ_BANK_IMPORTED", "QuizSet", set_id, {
        "course": bank["course"], "bank": bank["bank"], "file": source.name, "sha256": digest,
        "added": added, "updated": updated, "retired": len(retired)})
    return {"state": "imported", "course": bank["course"], "bank": bank["bank"], "added": added, "updated": updated,
            "retired": len(retired)}


def import_new(connection: sqlite3.Connection, root: Path) -> dict:
    """Every bank file (and ``*.cues.json``) under ``_lecturehub\\quiz`` that is not in yet. A bad file is
    reported, never half read."""
    done, errors, cues = [], [], []
    folder = bank_dir(root)
    for path in sorted(folder.rglob("*.json")) if folder.is_dir() else []:
        connection.execute("SAVEPOINT bank")
        try:
            if path.name.endswith(".cues.json"):
                result = import_cues(connection, root, path)
                connection.execute("RELEASE bank")
                if result["state"] == "imported":
                    cues.append({**result, "file": path.name})
                continue
            result = import_bank(connection, root, path)
            connection.execute("RELEASE bank")
            if result["state"] == "imported":
                done.append({**result, "file": path.name})
        except (OSError, ValueError, sqlite3.Error) as exc:
            connection.execute("ROLLBACK TO bank")
            connection.execute("RELEASE bank")
            errors.append({"file": path.name, "error": str(exc)})
    return {"imported": done, "errors": errors, "cues": cues}


# ------------------------------------------------------------------ what was said in class, picked by an AI

CUE_KINDS = ("exam_mention", "exam_exclusion", "emphasis", "scope_notice")
CUE_WINDOW_MS = 240_000  # the quote must be found in a transcript within four minutes of the stated time
CUE_MATCH = 0.6          # share of the quote's letter pairs that the transcript must hold


def _letters(text: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]", "", text.casefold())


def _heard(quote: str, heard: str) -> float:
    """How much of the quote is in the transcript text: the share of its letter pairs (engines spell differently)."""
    q, h = _letters(quote), _letters(heard)
    pairs = {q[i:i + 2] for i in range(len(q) - 1)}
    return sum(p in h for p in pairs) / len(pairs) if pairs else 0.0


def _clock(value) -> int:
    """'23:20', '1:02:03' or milliseconds → milliseconds."""
    if isinstance(value, int | float):
        return int(value)
    parts = [int(p) for p in str(value).strip().split(":")]
    if not 2 <= len(parts) <= 3 or any(p < 0 for p in parts):
        raise ValueError(f"시각 형식 오류: {value}")
    seconds = parts[-1] + 60 * parts[-2] + (3600 * parts[0] if len(parts) == 3 else 0)
    return seconds * 1000


def import_cues(connection: sqlite3.Connection, root: Path, path: str | Path) -> dict:
    """Statements about the exam that an AI picked while reading the transcripts (a ``*.cues.json`` file). Caller commits.

    A cue is kept only when its words are found in a transcript of that lecture near the stated time — a
    quote nobody can find in the recording's own transcript is not shown as something the teacher said.
    Rows are ExamCue with decided_by 'ai'. A quote already kept for that lecture is not added again: a newer
    file updates its note, kind and time (an edited file is the corrected one), and a cue someone else decided
    (decided_by other than 'ai') is left as it is. The same quote twice in one file is two mentions."""
    root, source = Path(root).resolve(), Path(path).resolve()
    if not source.is_file() or not source.is_relative_to(root):
        raise ValueError("학기 폴더 안의 파일만 가져옵니다.")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    if connection.execute("SELECT 1 FROM Event WHERE type = 'EXAM_CUES_IMPORTED' AND payload_json LIKE ?",
                          (f'%"sha256":"{digest}"%',)).fetchone():
        return {"state": "already", "added": 0, "updated": 0, "skipped": []}
    try:
        data = json.loads(source.read_text(encoding="utf-8-sig"))
    except ValueError as exc:
        raise ValueError(f"발언 파일을 읽지 못했습니다(JSON 형식 오류): {exc}") from exc
    if not isinstance(data, dict) or data.get("version") != BANK_VERSION or not isinstance(data.get("cues"), list):
        raise ValueError(f"발언 파일에는 version {BANK_VERSION}과 cues 목록이 필요합니다")
    course = connection.execute("SELECT id FROM Course WHERE folder_name = ?", (str(data.get("course") or ""),)).fetchone()
    if course is None:
        raise ValueError(f"없는 과목입니다: {data.get('course')}")
    added, updated, skipped, touched = 0, 0, [], set()
    for number, cue in enumerate(data["cues"], 1):
        try:
            kind, quote, day = cue.get("kind"), str(cue.get("quote") or "").strip(), str(cue.get("date") or "")
            if kind not in CUE_KINDS or len(_letters(quote)) < 6:
                raise ValueError(f"kind는 {', '.join(CUE_KINDS)} 중 하나이고 quote는 여섯 글자 이상입니다")
            at_ms = _clock(cue.get("at"))
        except (ValueError, TypeError, AttributeError) as exc:
            skipped.append({"n": number, "why": str(exc)})
            continue
        best = None  # (share, lecture, transcripts that hold it, segment start)
        for (lecture_id,) in connection.execute("SELECT id FROM Lecture WHERE course_id = ? AND date = ?", (course[0], day)):
            agree, top = 0, (0.0, at_ms)
            for (transcript_id,) in connection.execute("SELECT id FROM Transcript WHERE lecture_id = ?", (lecture_id,)):
                rows = connection.execute(
                    "SELECT start_ms, text FROM TranscriptSegment WHERE transcript_id = ? ORDER BY seq", (transcript_id,)).fetchall()
                near = [(start, text) for index, (start, text) in enumerate(rows)
                        if (start or 0) <= at_ms + CUE_WINDOW_MS
                        and (rows[index + 1][0] if index + 1 < len(rows) else 10 ** 12) >= at_ms - CUE_WINDOW_MS]
                share = max(((_heard(quote, text), start or 0) for start, text in near), default=(0.0, at_ms))
                agree += share[0] >= CUE_MATCH
                top = max(top, share)
            if best is None or (agree, top[0]) > (best[2], best[0]):
                best = (top[0], lecture_id, agree, top[1])
        if best is None:
            skipped.append({"n": number, "why": f"{day}에 이 과목의 회차가 없습니다"})
            continue
        if not best[2]:
            skipped.append({"n": number, "why": f"전사에서 이 말을 찾지 못했습니다(가장 비슷한 대목 {round(best[0] * 100)}%)"})
            continue
        note, confidence = str(cue.get("note") or "").strip(), "medium" if best[2] > 1 else "low"
        same = [r for r in connection.execute("SELECT id, at_ms, kind, context_before, decided_by FROM ExamCue "
                                              "WHERE lecture_id = ? AND quote = ?", (best[1], quote)) if r[0] not in touched]
        if same:
            row = min(same, key=lambda r: abs((r[1] or 0) - at_ms))  # the earlier import of this mention
            touched.add(row[0])
            if row[4] != "ai" or (row[1], row[2], row[3]) == (at_ms, kind, note):
                continue
            connection.execute("UPDATE ExamCue SET at_ms = ?, kind = ?, context_before = ?, engines_agree = ?, source = ?, "
                               "confidence_class = ? WHERE id = ?",
                               (at_ms, kind, note, best[2], f"ai_note:{source.name}", confidence, row[0]))
            updated += 1
            continue
        cue_id = new_ulid()
        connection.execute(
            "INSERT INTO ExamCue (id, lecture_id, at_ms, kind, quote, context_before, context_after, material_page, "
            "engines_agree, source, confidence_class, decided_by) VALUES (?,?,?,?,?,?,'',NULL,?,?,?,'ai')",
            (cue_id, best[1], at_ms, kind, quote, note, best[2], f"ai_note:{source.name}", confidence))
        touched.add(cue_id)
        added += 1
    EventStore(connection).append("EXAM_CUES_IMPORTED", "Course", course[0], {
        "file": source.name, "sha256": digest, "added": added, "updated": updated, "skipped": skipped, "decided_by": "ai"})
    return {"state": "imported", "course": data["course"], "added": added, "updated": updated, "skipped": skipped}


# ------------------------------------------------------------------ practice


def _item(row) -> dict:
    item_id, kind, question, choices, answer, explanation, sources, evidence, difficulty, flags, state, last_at, tries = row
    flags = json.loads(flags)
    origin = next((f[7:] for f in flags if f.startswith("origin:")), "claude")
    return {"id": item_id, "type": kind, "question": question, "choices": json.loads(choices) if choices else None,
            "answer": answer, "explanation": explanation, "sources": json.loads(sources), "evidence": evidence,
            "difficulty": difficulty, "origin": origin, "origin_label": ORIGINS.get(origin, ORIGINS["claude"]),
            "topic": next((f[6:] for f in flags if f.startswith("topic:")), ""), "auto": kind in AUTO,
            "state": state or "new", "last_at": last_at, "tries": tries}


_ITEMS = """
SELECT i.id, i.type, i.question, i.choices, i.answer, i.explanation, i.sources, i.quoted_evidence, i.difficulty, i.flags,
       (SELECT a.review_state FROM QuizAttempt a WHERE a.quiz_item_id = i.id ORDER BY a.at DESC, a.rowid DESC LIMIT 1),
       (SELECT max(a.at) FROM QuizAttempt a WHERE a.quiz_item_id = i.id),
       (SELECT count(*) FROM QuizAttempt a WHERE a.quiz_item_id = i.id)
FROM QuizItem i JOIN QuizSet s ON s.id = i.quiz_set_id
WHERE s.course_id = ? AND i.status = 'active' ORDER BY i.rowid
"""


def items(connection: sqlite3.Connection, course_id: str, *, mode: str = "mix", topic: str = "", limit: int = 20) -> list[dict]:
    """Questions to practise next. ``mix``: wrong ones first (oldest mistake first), then unseen, then the rest."""
    if mode not in MODES:
        raise ValueError(f"mode는 {', '.join(MODES)} 중 하나입니다")
    found = [_item(row) for row in connection.execute(_ITEMS, (course_id,))]
    if topic:
        found = [i for i in found if i["topic"] == topic]
    if mode == "wrong":
        found = [i for i in found if i["state"] in ("wrong", "reviewing")]
    elif mode == "new":
        found = [i for i in found if i["state"] == "new"]
    elif mode == "past":
        found = [i for i in found if i["origin"] in _ORIGIN_RANK]
    if mode in ("mix", "wrong", "new", "past"):
        rank = {"wrong": 0, "new": 1, "reviewing": 2, "mastered": 3}
        found.sort(key=lambda i: (rank[i["state"]], _ORIGIN_RANK.get(i["origin"], 2), i["last_at"] or 0))
    return found[:max(1, int(limit))]


def _same(given: str, answer: str) -> bool:
    return re.sub(r"\s+", "", given).casefold() == re.sub(r"\s+", "", answer).casefold()


def answer(connection: sqlite3.Connection, item_id: str, given: str, correct: bool | None = None) -> dict:
    """Record one attempt. mcq/ox are marked here; for the others ``correct`` is the user's own mark. Caller commits.

    After a mistake an item is 'wrong'; the first right answer after that makes it 'reviewing', the next one
    'mastered'. Right at the first try is 'mastered'."""
    row = connection.execute("SELECT type, answer FROM QuizItem WHERE id = ? AND status = 'active'", (item_id,)).fetchone()
    if row is None:
        raise ValueError("없는 문제입니다.")
    if row[0] in AUTO:
        correct = _same(str(given), row[1])
    elif correct is None:
        raise ValueError("이 유형은 답을 본 뒤 맞았는지 직접 표시합니다.")
    before = connection.execute("SELECT review_state FROM QuizAttempt WHERE quiz_item_id = ? ORDER BY at DESC, rowid DESC "
                                "LIMIT 1", (item_id,)).fetchone()
    state = "wrong" if not correct else "reviewing" if before and before[0] == "wrong" else "mastered"
    connection.execute("INSERT INTO QuizAttempt (quiz_item_id, at, answer_given, correct, review_state, id) VALUES (?,?,?,?,?,?)",
                       (item_id, _now_ms(), str(given), int(bool(correct)), state, new_ulid()))
    return {"correct": bool(correct), "state": state, "answer": row[1]}


def stats(connection: sqlite3.Connection, course_id: str) -> dict:
    found = [_item(row) for row in connection.execute(_ITEMS, (course_id,))]
    count = {state: sum(i["state"] == state for i in found) for state in ("new", "wrong", "reviewing", "mastered")}
    topics: dict[str, dict] = {}
    for i in found:
        t = topics.setdefault(i["topic"] or "", {"topic": i["topic"] or "", "total": 0, "wrong": 0, "done": 0})
        t["total"] += 1
        t["wrong"] += i["state"] in ("wrong", "reviewing")
        t["done"] += i["state"] != "new"
    past = [i for i in found if i["origin"] in _ORIGIN_RANK]
    return {"total": len(found), **count, "topics": list(topics.values()),
            "past": len(past), "past_left": sum(i["state"] != "mastered" for i in past)}


# ------------------------------------------------------------------ the board


def prep_files(root: Path, folder: str) -> list[dict]:
    """HTML files of a course whose name says exam prep (the course folder itself and its 정리본)."""
    base = Path(root) / folder
    found = []
    for place in (base, base / "정리본"):
        for path in sorted(place.glob("*.html")) if place.is_dir() else []:
            if _PREP_NAME.search(path.stem):
                stat = path.stat()
                found.append({"name": path.stem, "path": str(path), "bytes": stat.st_size,
                              "changed": datetime.fromtimestamp(stat.st_mtime, UTC).astimezone().strftime("%Y-%m-%d")})
    return found


def board(connection: sqlite3.Connection, root: Path, today: date) -> dict:
    """What the exam screen shows: per course its exam dates, what was said in class, prep files and practice state."""
    from . import calendar_events

    upcoming = [e for e in calendar_events.events(connection, today.isoformat(), "9999-12-31")
                if e["kind"] in ("exam", "quiz")]
    heard = [c for c in calendar_events.candidates(connection) if c["kind"] in ("exam", "quiz") and c["date"] >= today.isoformat()]
    scheduled = {r[0] for r in connection.execute("SELECT DISTINCT course_id FROM Schedule")}
    courses = []
    for course_id, folder in connection.execute("SELECT id, folder_name FROM Course ORDER BY rowid").fetchall():
        exams = [{**e, "d_day": (date.fromisoformat(e["date"]) - today).days} for e in upcoming if e["course_id"] == course_id]
        cues = [{"id": cue_id, "lecture_id": lecture_id, "date": day, "at_ms": at_ms, "kind": kind,
                 "kind_label": _CUE_LABEL[kind], "quote": quote, "decided_by": decided_by, "confidence": confidence,
                 "note": note, "by_label": {"ai": "AI가 전사에서 고른 것", "rule": "규칙으로 찾은 것", "user": "직접 표시"}[decided_by]}
                for cue_id, lecture_id, day, at_ms, kind, quote, decided_by, confidence, note in connection.execute(
                    "SELECT x.id, x.lecture_id, l.date, x.at_ms, x.kind, x.quote, x.decided_by, x.confidence_class, "
                    "CASE WHEN x.decided_by = 'ai' THEN x.context_before ELSE '' END "
                    f"FROM ExamCue x JOIN Lecture l ON l.id = x.lecture_id WHERE l.course_id = ? AND x.kind IN ({','.join('?' * len(CUE_KINDS))}) "
                    "ORDER BY l.date DESC, x.at_ms", (course_id, *CUE_KINDS))]
        files, quiz = prep_files(root, folder), stats(connection, course_id)
        candidates = [{**c, "d_day": (date.fromisoformat(c["date"]) - today).days} for c in heard if c["course_id"] == course_id]
        if not (course_id in scheduled or exams or candidates or cues or files or quiz["total"]):
            continue
        courses.append({"id": course_id, "folder": folder, "exams": exams, "candidates": candidates, "cues": cues,
                        "files": files, "quiz": quiz})
    courses.sort(key=lambda c: (c["exams"][0]["d_day"] if c["exams"] else 10_000))
    return {"today": today.isoformat(), "courses": courses}
