"""S5: held items as questions the user answers once, then rejoin (§0.4).

A held IngestItem becomes a question card: what is uncertain, the evidence side by side,
a preview, and answer buttons. Answers are appended as Evidence (kind ``user_answer``,
source ``user``) with an Event; the item row is only moved to ``rejected`` when the user
says it is not study material. Nothing in the semester folder is moved, renamed or deleted.

Indexing (``ingest.index_root(..., only_paths=...)``) reads the latest answer per file and
registers just the answered files. Answers never apply to other files by themselves:
a file with the same bytes elsewhere is only *suggested* the same answer.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from lecturehub.core.events import append
from lecturehub.core.ids import new_ulid

KINDS = {
    "recording device or start time is unconfirmed": "device",
    "filename course/date/session is unconfirmed": "identity",
    "filename lecture date conflicts with Clova export header": "date",
    "filename date conflicts with Clova export header": "date",
    "filename date conflicts with recording start evidence": "date",
    "transcript engine is unknown": "engine",
}
# The stored reason stays the ingest's own words (append-only records); the inbox shows it in Korean.
REASON_KO = {
    "recording device or start time is unconfirmed": "녹음한 기기나 시작 시각을 모릅니다",
    "filename course/date/session is unconfirmed": "파일 이름만으로는 과목·날짜·회차를 확정할 수 없습니다",
    "filename lecture date conflicts with Clova export header": "파일 이름의 날짜와 클로바노트에 적힌 날짜가 다릅니다",
    "filename date conflicts with Clova export header": "파일 이름의 날짜와 클로바노트에 적힌 날짜가 다릅니다",
    "filename date conflicts with recording start evidence": "파일 이름의 날짜와 녹음 시작 기록의 날짜가 다릅니다",
    "transcript engine is unknown": "어느 전사 도구로 만든 파일인지 알 수 없습니다",
    "same audio hash has a different lecture identity": "같은 녹음이 이미 다른 회차에 연결돼 있습니다",
    "registered source bytes changed; original record preserved": "이미 등록된 녹음 파일의 내용이 바뀌었습니다(처음 기록은 그대로 둠)",
    "same transcript hash has a different lecture identity": "같은 전사본이 이미 다른 회차에 연결돼 있습니다",
    "registered transcript bytes changed; original record preserved": "이미 등록된 전사본의 내용이 바뀌었습니다(처음 기록은 그대로 둠)",
    "original audio is missing or ambiguous; no_media requires migration 0003": "이 전사본의 원래 녹음을 찾지 못했거나 여러 개입니다",
    "no_media import requires user-confirmed lecture linkage; filename alone is insufficient":
        "원래 녹음이 없는 전사본입니다 — 어느 수업 것인지 직접 골라 주세요(파일 이름만으로는 정하지 않습니다)",
    "same material hash belongs to another course": "같은 자료가 이미 다른 과목에 있습니다",
    "registered material bytes changed; source review required": "이미 등록된 자료의 내용이 바뀌었습니다",
    "note lecture is unconfirmed": "이 정리본이 어느 회차 것인지 모릅니다",
    "same note hash has different lecture links": "같은 정리본이 여러 회차에 연결돼 있습니다",
    "registered note bytes changed; version review required": "이미 등록된 정리본의 내용이 바뀌었습니다",
    "confirmed day contains multiple lectures; session is unresolved": "그날 수업이 여러 번이라 몇 번째 회차인지 모릅니다",
    "export date could not be verified": "내보낸 날짜를 확인할 수 없습니다",
}
_FORMAT_MISMATCH = re.compile(r"transcript does not read as the answered format \((\w+)\)$")
_READ_FAILED = re.compile(r"source read(?:/parse)? failed \((\w+)\)$")


def reason_ko(reason: str) -> str:
    """The hold reason in plain Korean (unknown reasons are shown as they are)."""
    if reason in REASON_KO:
        return REASON_KO[reason]
    if match := _FORMAT_MISMATCH.match(reason or ""):
        name = ENGINES.get(match.group(1), match.group(1))
        return f"고른 형식({name})으로는 이 전사본을 읽을 수 없습니다 — 다른 형식을 골라 주세요"
    if _READ_FAILED.match(reason or ""):
        return "파일을 읽지 못했습니다(깨졌거나 다른 프로그램이 쓰는 중일 수 있음)"
    if (reason or "").startswith("ffprobe"):
        return "소리 파일로 읽을 수 없습니다(깨졌거나 녹음 파일이 아님)"
    return reason


def question_for(reason: str) -> str:
    """Which question the card asks. A wrongly picked transcript format asks the format again (not a dead end)."""
    if _FORMAT_MISMATCH.match(reason or ""):
        return "engine"
    return KINDS.get(reason, "other")


DEVICES = {"laptop": "노트북", "phone": "휴대폰", "external_mic": "외장 마이크"}
ENGINES = {"gemini_transcribe": "Google Transcribe", "mai": "MAI", "daglo": "다글로", "clova": "클로바노트"}
QUESTION = {
    "device": "이 녹음은 어느 기기로 했나요?",
    "identity": "어느 과목, 어느 날 수업인가요?",
    "date": "실제 수업 날짜는 언제인가요?",
    "engine": "이 전사본은 어디서 만든 것인가요?",
    "other": "이 파일을 어떻게 할까요?",
}


def _evidence(connection: sqlite3.Connection, item_id: str, kind: str, detail: dict) -> str:
    evidence_id = new_ulid()
    connection.execute(
        "INSERT INTO Evidence (id, subject_type, subject_id, field, kind, detail, weight, source, created_at) "
        "VALUES (?, 'IngestItem', ?, NULL, ?, ?, NULL, 'user', ?)",
        (evidence_id, item_id, kind, json.dumps(detail, ensure_ascii=False), time.time_ns() // 1_000_000))
    return evidence_id


def _items(connection: sqlite3.Connection, ids: list[str]) -> list[sqlite3.Row]:
    rows = connection.execute(
        f"SELECT id, source_path, suggestion, state FROM IngestItem WHERE id IN ({','.join('?' * len(ids))})",
        ids).fetchall()
    if len(rows) != len(set(ids)):
        raise ValueError("없는 보류 항목이 있습니다")
    return rows


def latest_answers(connection: sqlite3.Connection) -> dict[str, dict]:
    """source_path → {kind, answer, evidence_id} for the newest answer not revoked later."""
    found: dict[str, dict] = {}
    for item_id, path, kind, detail, evidence_id in connection.execute(
            "SELECT i.id, i.source_path, e.kind, e.detail, e.id FROM Evidence e JOIN IngestItem i ON i.id = e.subject_id "
            "WHERE e.subject_type = 'IngestItem' AND e.source = 'user' AND e.kind IN ('user_answer', 'user_answer_revoked') "
            "ORDER BY e.created_at, e.rowid"):
        if kind == "user_answer_revoked":
            found.pop(path, None)
        else:
            found[path] = {**json.loads(detail), "evidence_id": evidence_id, "item_id": item_id}
    return found


def _deferred_until(connection: sqlite3.Connection, item_id: str) -> int:
    row = connection.execute(
        "SELECT detail FROM Evidence WHERE subject_type='IngestItem' AND subject_id=? AND kind='ask_later' "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1", (item_id,)).fetchone()
    return json.loads(row[0])["ask_after"] if row else 0


def _course_folders(connection: sqlite3.Connection) -> list[str]:
    return [r[0] for r in connection.execute("SELECT folder_name FROM Course ORDER BY rowid")]


def _guesses(path: Path, folders: list[str], year: int) -> dict:
    """Candidates read from the file name only (shown as candidates, never applied)."""
    course = next((f for f in folders if f in path.name), None) or next((f for f in folders if f in path.parts), None)
    day = None
    full = re.search(r"(?<!\d)(\d{2})\.(\d{1,2})\.(\d{1,2})(?!\d)", path.name)
    short = re.search(r"(?<!\d)(\d{1,2})\.(\d{1,2})(?!\d)", path.name)
    try:
        if full:
            day = date(2000 + int(full[1]), int(full[2]), int(full[3])).isoformat()
        elif short:
            day = date(year, int(short[1]), int(short[2])).isoformat()
    except ValueError:
        day = None
    return {"course": course, "date": day}


def _preview(path: Path, kind: str) -> dict:
    if kind == "transcript" and path.is_file():
        try:
            from lecturehub.core.textio import decode_text

            lines = [line for line in decode_text(path.read_bytes(), source=path).splitlines() if line.strip()]
            return {"type": "text", "lines": lines[:6], "total_lines": len(lines)}
        except (OSError, ValueError):
            return {"type": "none"}
    if kind == "media" and path.is_file():
        return {"type": "audio", "path": str(path)}
    return {"type": "none"}


def questions(connection: sqlite3.Connection, *, now_ms: int | None = None, year: int = 2026) -> dict:
    """Open question cards (items with the same bytes share one card) and counts of deferred ones."""
    from .ingest import _filename_date, _header_date

    now_ms = now_ms if now_ms is not None else time.time_ns() // 1_000_000
    folders = _course_folders(connection)
    answers = latest_answers(connection)
    cards: dict[str, dict] = {}
    deferred = 0
    for item_id, raw_path, suggestion in connection.execute(
            "SELECT id, source_path, suggestion FROM IngestItem WHERE state = 'held' ORDER BY source_path"):
        info = json.loads(suggestion)
        if _deferred_until(connection, item_id) > now_ms:
            deferred += 1
            continue
        path = Path(raw_path)
        reason, source_kind = info.get("reason", ""), info.get("kind", "")
        question = question_for(reason)
        group = info.get("sha256") or item_id
        card = cards.get(group)
        if card is None:
            evidence = [{"label": "파일", "value": path.name}, {"label": "보류 이유", "value": reason_ko(reason)}]
            guesses = _guesses(path, folders, year)
            options: list[dict] = []
            if question == "device":
                options = [{"value": {"device_type": k}, "label": v} for k, v in DEVICES.items()]
            elif question == "engine":
                options = [{"value": {"engine": k}, "label": v} for k, v in ENGINES.items()]
            elif question == "date":
                named = _filename_date(path.name, year)
                header = None
                try:
                    header = _header_date(path) if path.is_file() else None
                except (OSError, ValueError):
                    header = None
                evidence += [{"label": "파일 이름 날짜", "value": named or "없음"},
                             {"label": "전사 머리말 날짜", "value": header or "없음"}]
                options = [{"value": {"folder": guesses["course"] or path.parent.name, "date": d},
                            "label": f"{d} ({why})"} for d, why in ((named, "파일 이름"), (header, "머리말")) if d]
            elif question == "identity":
                evidence.append({"label": "이름에서 읽은 후보", "value":
                                 f"{guesses['course'] or '과목 불명'} · {guesses['date'] or '날짜 불명'} (추정)"})
            card = {"id": group, "question": question, "text": QUESTION[question], "kind": source_kind,
                    "file": path.name, "reason_ko": reason_ko(reason),
                    "evidence": evidence, "options": options, "guesses": guesses, "folders": folders,
                    "items": [], "preview": _preview(path, source_kind), "answered": None}
            cards[group] = card
        card["items"].append({"id": item_id, "path": raw_path, "name": path.name})
        if raw_path in answers:
            card["answered"] = answers[raw_path]
    return {"cards": list(cards.values()), "deferred": deferred}


def open_count(connection: sqlite3.Connection, now_ms: int | None = None) -> int:
    """Question cards waiting (identical bytes counted once, deferred ones left out) — for the sidebar."""
    now_ms = now_ms if now_ms is not None else time.time_ns() // 1_000_000
    groups = set()
    for item_id, suggestion in connection.execute("SELECT id, suggestion FROM IngestItem WHERE state = 'held'"):
        if _deferred_until(connection, item_id) <= now_ms:
            groups.add(json.loads(suggestion).get("sha256") or item_id)
    return len(groups)


def answer(connection: sqlite3.Connection, item_ids: list[str], value: dict) -> list[str]:
    """Record the user's answer for these items (append-only). Caller commits, then re-indexes them."""
    rows = _items(connection, item_ids)
    evidence_ids = []
    for item_id, raw_path, suggestion, state in rows:
        if state != "held":
            raise ValueError("이미 정리된 항목입니다")
        reason = json.loads(suggestion).get("reason", "")
        question = question_for(reason)
        clean = _validate(question, value)
        evidence_ids.append(_evidence(connection, item_id, "user_answer", {"question": question, "answer": clean,
                                                                          "decided_by": "user"}))
        append(connection, "USER_CONFIRMED", "IngestItem", item_id, {"question": question, "answer": clean})
    return evidence_ids


def _validate(question: str, value: dict) -> dict:
    if question == "device":
        if value.get("device_type") not in DEVICES:
            raise ValueError("기기를 골라 주세요")
        start = str(value.get("start") or "").strip()
        if start and not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", start):
            raise ValueError("녹음 시작 시각은 10:05처럼 적어 주세요")
        return {"device_type": value["device_type"], "start": start}
    if question == "engine":
        if value.get("engine") not in ENGINES:
            raise ValueError("전사 형식을 골라 주세요")
        return {"engine": value["engine"]}
    if question in ("identity", "date"):
        folder, day = value.get("folder"), value.get("date")
        try:
            date.fromisoformat(str(day))
        except ValueError as exc:
            raise ValueError("날짜를 YYYY-MM-DD로 적어 주세요") from exc
        if not folder:
            raise ValueError("과목을 골라 주세요")
        session = str(value.get("session") or "").strip()
        return {"folder": folder, "date": day, "session": session}
    raise ValueError("이 항목은 답으로 연결할 수 없습니다. 나중에 보거나 학습 대상 아님으로 두세요")


def defer(connection: sqlite3.Connection, item_ids: list[str], *, days: int = 1, now: datetime | None = None) -> None:
    now = now or datetime.now().astimezone()
    ask_after = int((now + timedelta(days=days)).timestamp() * 1000)
    for item_id, *_ in _items(connection, item_ids):
        _evidence(connection, item_id, "ask_later", {"ask_after": ask_after, "decided_by": "user"})
        append(connection, "HOLD_DEFERRED", "IngestItem", item_id, {"ask_after": ask_after})


def reject(connection: sqlite3.Connection, item_ids: list[str]) -> None:
    """Not study material: the row says so; the file is untouched (HOLD-003)."""
    for item_id, _path, _s, state in _items(connection, item_ids):
        if state != "held":
            raise ValueError("이미 정리된 항목입니다")
        _evidence(connection, item_id, "not_study_material", {"decided_by": "user"})
        connection.execute("UPDATE IngestItem SET state = 'rejected' WHERE id = ?", (item_id,))
        append(connection, "HOLD_REJECTED", "IngestItem", item_id, {})


def undo(connection: sqlite3.Connection, item_id: str) -> str:
    """Take back an answer or a 'not study material' (HOLD-005). A file already registered stays registered:
    raw records are immutable, so the undo is recorded and the card reappears only for held/rejected items."""
    row = _items(connection, [item_id])[0]
    _evidence(connection, item_id, "user_answer_revoked", {"decided_by": "user", "previous_state": row[3]})
    if row[3] == "rejected":
        connection.execute("UPDATE IngestItem SET state = 'held' WHERE id = ?", (item_id,))
    append(connection, "USER_CONFIRMATION_REVOKED", "IngestItem", item_id, {"previous_state": row[3]})
    return row[3]


def outcome(connection: sqlite3.Connection, item_ids: list[str]) -> list[dict]:
    """After re-indexing: state per item and the next action for the lecture it joined."""
    from . import lectures

    results = []
    for item_id, raw_path, suggestion, state in _items(connection, item_ids):
        info = json.loads(suggestion)
        entry = {"id": item_id, "name": Path(raw_path).name, "state": state, "reason": info.get("reason")}
        registered = (info.get("resolution") or {}).get("registered_id")
        if state == "confirmed" and registered:
            table = (info.get("resolution") or {}).get("registered_type")
            if table in ("Transcript", "MediaAsset"):
                lecture_id = connection.execute(f"SELECT lecture_id FROM {table} WHERE id = ?",  # table is one of two fixed names
                                                (registered,)).fetchone()[0]
                detail = lectures.lecture_detail(connection, lecture_id)
                entry.update(lecture_id=lecture_id, course=detail["course"], date=detail["date"],
                             next=lectures.lecture_state(detail)["label"])
        results.append(entry)
    return results
