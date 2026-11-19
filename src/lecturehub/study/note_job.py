"""AI-003: provider-consented transcript/material text → selected engine → deterministic note validation.

Codex is the default. Synthetic tests inject a local executor without opening an external engine.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from lecturehub.core.ids import new_ulid

from . import corrections, notes

PAGE_CHARS = 1500

SCHEMA = {
    "type": "object",
    "properties": {"blocks": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "key": {"type": "string"},
            "kind": {"type": "string", "enum": ["heading", "paragraph", "item", "external"]},
            "text": {"type": "string"},
            "cites": {"type": "array", "items": {"type": "object", "properties": {
                "t": {"type": "string"}, "s": {"type": "integer"}, "q": {"type": "string"},
                "m": {"type": "string"}, "p": {"type": "integer"}}}},
            "ext": {"type": "object", "properties": {
                "confidence": {"type": "string", "enum": ["high", "mid", "low"]},
                "source": {"type": "string", "enum": ["general", "textbook", "web"]},
                "ref": {"type": "string"}, "conflict": {"type": "boolean"}}},
        },
        "required": ["key", "kind", "text"],
    }}},
    "required": ["blocks"],
}

RULES = """너는 대학 강의 복습 노트를 만든다. 아래 전사(녹음)와 강의자료 쪽만이 1차 출처다.
규칙:
1. 강의 흐름 순서대로 heading(소제목)과 paragraph(본문)를 만든다. 본문은 공부하는 사람이 읽기 쉬운 문장으로 이어 쓴다.
   목록이 꼭 필요한 곳(단계·조건 나열)만 item으로 쓴다. 근거(cites)는 정리본에서 각주(녹음 시각·원문)로 붙는다.
2. 모든 paragraph·item에는 cites가 1개 이상 있어야 한다. 전사 근거는 {"t": 전사ID, "s": 구간번호, "q": 원문 그대로 짧은 인용(선택)},
   자료 근거는 {"m": 자료ID, "p": 쪽}. 근거가 없는 문장은 쓰지 않는다. 지어내지 않는다.
3. 숫자·기호·고유명사는 근거에 있는 그대로 쓴다. 말한 수치와 자료 수치가 다르면 둘 다 적고 다르다고 밝힌다.
4. 전사·자료에 없는 배경 설명이 꼭 필요하면 kind "external" 블록으로만 쓴다. ext.confidence(high/mid/low)와
   ext.source(general/textbook/web)를 반드시 붙이고, 강의 내용과 다르면 ext.conflict=true로 둘 다 보여 준다.
   외부 지식으로 강의 내용을 고치지 않는다.
5. 전사가 불분명하면 단정하지 말고 "녹취상 불분명"이라고 쓴다.
6. key는 b01, b02 … 순서로 붙인다. 한국어로 쓴다."""


def _clock(ms: int) -> str:
    seconds = ms // 1000
    return f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}" if seconds >= 3600 else \
        f"{seconds // 60:02d}:{seconds % 60:02d}"


def build_input(connection: sqlite3.Connection, lecture_id: str, only_key: str | None = None) -> dict:
    from lecturehub.index import lectures

    pointer = lectures.in_use(connection, lecture_id)
    if pointer is None:
        raise ValueError("이 회차에는 사용할 전사가 없습니다")
    transcript_id = pointer["transcript_id"]
    from lecturehub.index import media_range

    view = corrections.corrected_segments(connection, transcript_id)
    span = media_range.in_range(connection, transcript_id)  # D-057: nothing after the lecture ended
    segments = [s for s in view["segments"] if span is None or span[0] <= s["start_ms"] < span[1]]
    pages = [{"m": r[0], "p": r[1], "title": r[2], "text": r[3][:PAGE_CHARS]} for r in connection.execute(
        "SELECT mp.material_id, mp.page_no, m.title, mp.text FROM MaterialMatch mm "
        "JOIN MaterialPage mp ON mp.material_id = mm.material_id AND mp.page_no BETWEEN mm.page_from AND mm.page_to "
        "JOIN Material m ON m.id = mm.material_id WHERE mm.lecture_id = ? AND length(trim(mp.text)) > 0 "
        "ORDER BY m.title, mp.page_no", (lecture_id,))]
    target = None
    if only_key:
        note = notes.load(connection, lecture_id)
        target = next((b for b in (note or {}).get("blocks", []) if b["key"] == only_key), None)
        if target is None:
            raise ValueError("다시 만들 블록이 없습니다")
        near = {c["seg_seq"] for c in target["cites"] if c["kind"] == "transcript"}
        near = {s for seq in near for s in range(seq - 3, seq + 4)}
        segments = [s for s in segments if s["seq"] in near] or segments
        cited_pages = {(c["material_id"], c["page_no"]) for c in target["cites"] if c["kind"] == "material"}
        pages = [p for p in pages if (p["m"], p["p"]) in cited_pages]
    return {"lecture_id": lecture_id, "transcript_id": transcript_id, "engine": pointer.get("engine"),
            "segments": segments, "pages": pages, "orphaned_corrections": view["orphaned"],
            "target": {"key": target["key"], "kind": target["kind"], "text": target["text"]} if target else None}


def prompt(data: dict) -> str:
    lines = [RULES, ""]
    if data["target"]:
        t = data["target"]
        lines += [f"이번에는 블록 {t['key']}({t['kind']}) 하나만 다시 쓴다. 결과 blocks에는 key {t['key']} 블록 하나만 넣는다.",
                  f"지금 내용: {t['text']}", ""]
    lines += [f"## 전사 (전사ID {data['transcript_id']}) — 각 줄: [구간번호 | 시각] 내용"]
    lines += [f"[{s['seq']} | {_clock(s['start_ms'])}] {s['text']}" for s in data["segments"]]
    if data["pages"]:
        lines += ["", "## 강의자료 쪽 — 각 묶음: [자료ID 쪽] 제목"]
        for page in data["pages"]:
            lines += [f"[{page['m']} p{page['p']}] {page['title']}", page["text"]]
    return "\n".join(lines)


def claude_executor(prompt_text: str, schema: dict, timeout: float = 900) -> dict:
    """Compatibility entry point. Engine selection is used by normal jobs."""
    from lecturehub.ai.claude import ClaudeEngine

    return ClaudeEngine().run_json(prompt_text, schema, timeout=timeout)


def consent_info(connection: sqlite3.Connection, root: Path, lecture_id: str) -> dict:
    from lecturehub.ai import selected
    from lecturehub.transcription.consent import NOTICES, ConsentStore, named

    row = connection.execute("SELECT c.folder_name FROM Lecture l JOIN Course c ON c.id=l.course_id WHERE l.id=?",
                             (lecture_id,)).fetchone()
    if row is None:
        raise ValueError("회차가 없습니다.")
    engine = selected("notes")
    if engine == "off":
        raise RuntimeError("AI 사용이 꺼져 있습니다. 설정에서 엔진을 고르세요.")
    course, provider = row[0], f"{engine}_note"
    store = ConsentStore(Path(root) / "_lecturehub" / "consent.json")
    return {"course": course, "provider": provider, "engine": engine, "notice": NOTICES[provider],
            "needs_consent": not (named(course) and store.granted(provider, course)), "once_only": not named(course)}


def job_dir(root: Path, lecture_id: str) -> Path:
    folder = Path(root) / "_lecturehub" / "jobs" / f"note_{lecture_id}_{datetime.now().astimezone():%Y%m%d_%H%M%S_%f}"
    folder.mkdir(parents=True, exist_ok=False)
    return folder


def _state(folder: Path, **values) -> None:
    path = folder / "state.json"
    current = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    current.update(values, updated_at=time.time_ns() // 1_000_000)
    path.write_text(json.dumps(current, ensure_ascii=False, indent=1), encoding="utf-8")


def prepare(connection: sqlite3.Connection, root: Path, lecture_id: str, only_key: str | None = None,
            *, executor_name: str = "codex_cli") -> Path:
    data = build_input(connection, lecture_id, only_key)
    folder = job_dir(root, lecture_id)
    (folder / "prompt.txt").write_text(prompt(data), encoding="utf-8")
    (folder / "schema.json").write_text(json.dumps(SCHEMA, ensure_ascii=False, indent=1), encoding="utf-8")
    (folder / "JOB.md").write_text(
        "# 노트 작업\n\n"
        f"- 회차: {lecture_id}\n- 입력: prompt.txt(사용본 전사 {len(data['segments'])}구간, 자료 {len(data['pages'])}쪽)\n"
        f"- 엔진: {executor_name}\n"
        "- 자동 실행이 안 되면 로그인·한도를 확인한 뒤 앱에서 다시 실행하세요.\n"
        "- 직접 만든 {\"blocks\": [...]} 결과는 이 폴더의 output.json에 UTF-8로 저장하고,\n"
        "  lecturehub note import <이 폴더> --root <학기 폴더> 로 가져올 수 있습니다.\n"
        "- 가져온 결과도 근거 검증을 통과해야 저장됩니다.\n", encoding="utf-8")
    processing_id = new_ulid()
    _state(folder, job_id=folder.name, processing_id=processing_id, lecture_id=lecture_id, only_key=only_key,
           transcript_id=data["transcript_id"], state="queued", executor=executor_name,
           created_at=time.time_ns() // 1_000_000)
    connection.execute(
        "INSERT INTO ProcessingJob (id, kind, executor, lecture_id, state, attempts, manifest_path, log_path, "
        "next_eligible_at) VALUES (?, 'note', ?, ?, 'queued', 0, ?, ?, NULL)",
        (processing_id, executor_name, lecture_id, str(folder / "state.json"), str(folder / "state.json")))
    connection.commit()
    return folder


def _job_state(connection: sqlite3.Connection, folder: Path, state: str) -> None:
    processing_id = json.loads((folder / "state.json").read_text(encoding="utf-8")).get("processing_id")
    if processing_id:
        connection.execute("UPDATE ProcessingJob SET state = ?, attempts = attempts + 1 WHERE id = ?",
                           (state, processing_id))


def import_result(connection: sqlite3.Connection, folder: Path, output: dict) -> dict:
    """Validate the selected engine's blocks and store them as a new note version. Caller commits."""
    state = json.loads((folder / "state.json").read_text(encoding="utf-8"))
    lecture_id, only_key = state["lecture_id"], state.get("only_key")
    from lecturehub.ai.codex import without_nulls

    blocks = without_nulls(output.get("blocks", []))
    if not isinstance(blocks, list) or any(not isinstance(b, dict) for b in blocks):
        raise ValueError("노트 블록 형식이 잘못됐습니다.")
    if only_key:
        blocks = [dict(b, key=only_key) for b in blocks[:1]]
    validation = notes.validate(blocks, notes.Sources(connection), lecture_id)
    if not validation.blocks:
        raise ValueError("근거 검증을 통과한 블록이 없어 저장하지 않았습니다.")
    inputs = {"transcript_id": state["transcript_id"], "job": folder.name, "model": output.get("model"),
              "cost_usd": output.get("cost_usd"), "usage": output.get("usage")}
    artifact_id = notes.replace_blocks(connection, lecture_id, validation, {only_key} if only_key else None,
                                       created_by=state.get("executor", "claude_cli"), inputs=inputs)
    _job_state(connection, folder, "qa_warning" if validation.rejected else "done")
    result = {"artifact_id": artifact_id, "blocks": len(validation.blocks), "rejected": len(validation.rejected)}
    from . import note_html

    try:  # the readable 정리본: body + transcript footnotes, attached to the lecture (2026-11-13)
        page = note_html.publish(connection, folder.parents[2], lecture_id,
                                 created_by=state.get("executor", "claude_cli"))
        result.update(html=page["path"], footnotes=page["footnotes"])
    except (OSError, ValueError, KeyError) as exc:
        result["html_error"] = f"정리본 HTML을 쓰지 못했습니다: {exc}"
    return result


def run(connection: sqlite3.Connection, root: Path, lecture_id: str, *, only_key: str | None = None,
        executor: Callable[[str, dict], dict] | None = None, busy: Callable[[], str | None] = lambda: None,
        engine=None, consent_provider: str | None = None, executor_label: str = "claude_cli") -> dict:
    """Prepare, run and import one note job. Never while recording (AGENTS 7)."""
    session = busy()
    if session:
        raise RuntimeError(f"녹음 중({session})에는 노트를 만들지 않습니다. 녹음이 끝난 뒤 다시 누르세요.")
    executor_name = executor_label  # injected executors are local test/manual/demo adapters
    if executor is None:
        from lecturehub.ai import get_engine

        info = consent_info(connection, root, lecture_id)
        engine = engine or get_engine("notes")
        if engine.name != info["engine"]:
            raise PermissionError("AI 엔진이 바뀌었습니다. 전송 동의를 다시 확인하세요.")
        if info["needs_consent"] and not (info["once_only"] and consent_provider == info["provider"]):
            raise PermissionError("전사본·자료 글을 보내기 전에 제공자·과목별 동의를 확인하세요.")
        executor, executor_name = engine.run_json, f"{engine.name}_cli"
    folder = prepare(connection, root, lecture_id, only_key, executor_name=executor_name)
    _state(folder, state="running")
    try:
        output = executor((folder / "prompt.txt").read_text(encoding="utf-8"), SCHEMA)
    except (OSError, subprocess.TimeoutExpired, RuntimeError, ValueError) as exc:
        _state(folder, state="waiting_user", error=str(exc))
        with connection:
            _job_state(connection, folder, "waiting_user")
        return {"state": "waiting_user", "job": str(folder), "error": str(exc)}
    try:
        (folder / "output.json").write_text(json.dumps(output, ensure_ascii=False, indent=1), encoding="utf-8")
        with connection:
            result = import_result(connection, folder, output)
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        _state(folder, state="waiting_user", error=str(exc))
        with connection:
            _job_state(connection, folder, "waiting_user")
        return {"state": "waiting_user", "job": str(folder), "error": str(exc)}
    _state(folder, state="done", **result)
    return {"state": "done", "job": str(folder), **result}
