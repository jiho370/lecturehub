"""Evidence-linked lecture notes (S4, §0.3 layer 3, §8.18, §8.19 #1).

A draft is a list of blocks:

    {"key": "b03", "kind": "paragraph" | "item" | "heading" | "external", "text": "...",
     "cites": [{"t": transcript_id, "s": seg_seq, "q": "quoted words"?}, {"m": material_id, "p": page}],
     "ext": {"confidence": "high|mid|low", "source": "general|textbook|web", "ref": "...", "conflict": false}}

The deterministic validator (no LLM) decides what is stored:
- a cite whose transcript segment or material page does not exist is dropped; a paragraph or
  item left without any cite is discarded (AGENTS 8, CITE-001);
- an external block without confidence and source kind is discarded (SRC-001);
- a quote not found in the cited text (corrections applied) is a warning (SRC-002, CITE-002);
- a number not found in any cited text is a warning (NOTE-003).
Notes are versions (StudyArtifact rows); blocks never change once stored.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass, field

from lecturehub.core.events import append
from lecturehub.core.ids import new_ulid

from . import corrections

KINDS = {"heading", "paragraph", "item", "external"}
CONFIDENCE = {"high", "mid", "low"}
SOURCES = {"general", "textbook", "web"}
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
_SPACE = re.compile(r"[\s ]+")
_PUNCT = re.compile(r"[\"'“”‘’.,!?…·:;()\[\]{}~\-–—]")


def _norm(text: str) -> str:
    return _PUNCT.sub("", _SPACE.sub("", text)).lower()


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "") for n in _NUMBER.findall(text)}


@dataclass
class Validation:
    blocks: list[dict] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)


class Sources:
    """Cited texts for one lecture: segments with corrections applied, and material pages."""

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        self._segments: dict[str, dict[int, dict]] = {}

    def segment(self, transcript_id: str, seq: int) -> dict | None:
        if transcript_id not in self._segments:
            data = corrections.corrected_segments(self.connection, transcript_id)["segments"]
            self._segments[transcript_id] = {s["seq"]: s for s in data}
        return self._segments[transcript_id].get(seq)

    def page(self, material_id: str, page: int) -> str | None:
        row = self.connection.execute("SELECT text FROM MaterialPage WHERE material_id = ? AND page_no = ?",
                                      (material_id, page)).fetchone()
        return row[0] if row else None


def validate(draft: list[dict], sources: Sources, lecture_id: str) -> Validation:
    result = Validation()
    seen_keys: set[str] = set()
    for index, raw in enumerate(draft):
        kind, text = raw.get("kind"), (raw.get("text") or "").strip()
        key = str(raw.get("key") or f"b{index + 1:02d}")
        if kind not in KINDS or not text:
            result.rejected.append({"key": key, "reason": "형식이 맞지 않는 블록"})
            continue
        if key in seen_keys:
            key = f"{key}_{index}"
        seen_keys.add(key)
        block = {"key": key, "kind": kind, "text": text, "cites": [], "warnings": [], "ext": None}
        if kind == "external":
            ext = raw.get("ext") or {}
            if ext.get("confidence") not in CONFIDENCE or ext.get("source") not in SOURCES:
                result.rejected.append({"key": key, "reason": "외부 지식에 확신도·출처 종류가 없음(SRC-001)"})
                continue
            block["ext"] = {"confidence": ext["confidence"], "source": ext["source"], "ref": ext.get("ref"),
                            "conflict": bool(ext.get("conflict"))}
            result.blocks.append(block)
            continue
        cited_texts = []
        for cite in raw.get("cites") or []:
            if not isinstance(cite, dict):
                continue
            try:
                int(cite.get("s", cite.get("p", 0)))
            except (TypeError, ValueError):
                block["warnings"].append("근거 형식이 잘못돼 뺌")
                continue
            if "t" in cite:
                segment = sources.segment(str(cite["t"]), int(cite.get("s", -1)))
                if segment is None or not _belongs(sources.connection, str(cite["t"]), lecture_id):
                    block["warnings"].append("없는 전사 근거를 뺌")
                    continue
                quote = (cite.get("q") or "").strip() or None
                if quote and _norm(quote) not in _norm(segment["text"]):
                    block["warnings"].append("인용이 원문에 없음(SRC-002)")
                block["cites"].append({"kind": "transcript", "transcript_id": str(cite["t"]), "seg_seq": segment["seq"],
                                       "quote": quote, "start_ms": segment["start_ms"]})
                cited_texts.append(segment["text"])
            elif "m" in cite:
                page_text = sources.page(str(cite["m"]), int(cite.get("p", -1)))
                if page_text is None:
                    block["warnings"].append("없는 자료 쪽 근거를 뺌")
                    continue
                block["cites"].append({"kind": "material", "material_id": str(cite["m"]), "page_no": int(cite["p"])})
                cited_texts.append(page_text)
        if kind in ("paragraph", "item") and not block["cites"]:
            result.rejected.append({"key": key, "reason": "근거가 없는 문장(CITE-001)"})
            continue
        if cited_texts:
            known = set().union(*(_numbers(t) for t in cited_texts))
            missing = sorted(n for n in _numbers(text) if n not in known)
            if missing:
                block["warnings"].append("근거에 없는 숫자: " + ", ".join(missing) + "(NOTE-003)")
        result.blocks.append(block)
    return result


def _belongs(connection: sqlite3.Connection, transcript_id: str, lecture_id: str) -> bool:
    row = connection.execute("SELECT lecture_id FROM Transcript WHERE id = ?", (transcript_id,)).fetchone()
    return row is not None and row[0] == lecture_id


def _latest(connection: sqlite3.Connection, lecture_id: str) -> tuple[str, int] | None:
    row = connection.execute(
        "SELECT id, version FROM StudyArtifact WHERE kind = 'lecture_note' AND lecture_ids = ? "
        "ORDER BY version DESC LIMIT 1", (json.dumps([lecture_id]),)).fetchone()
    return (row[0], row[1]) if row else None


def save(connection: sqlite3.Connection, lecture_id: str, validation: Validation, *, created_by: str,
         inputs: dict, reason: str) -> str:
    """Store a validated note as a new version (the previous one stays). Caller commits."""
    previous = _latest(connection, lecture_id)
    version = (previous[1] + 1) if previous else 1
    artifact_id = new_ulid()
    now = time.time_ns() // 1_000_000
    warnings = sum(1 for b in validation.blocks if b["warnings"])
    connection.execute(
        "INSERT INTO StudyArtifact (id, lecture_ids, path, kind, version, parent_version, build_script_path, mode, "
        "inputs, created_by, qa_status) VALUES (?, ?, ?, 'lecture_note', ?, ?, NULL, 'new', ?, ?, ?)",
        (artifact_id, json.dumps([lecture_id]), f"db:lecture_note/{lecture_id}/v{version}", version,
         previous[1] if previous else None,
         json.dumps({**inputs, "rejected": validation.rejected, "reason": reason}, ensure_ascii=False),
         created_by, "warning" if warnings or validation.rejected else "passed"))
    for seq, block in enumerate(validation.blocks):
        block_id = new_ulid()
        ext = block.get("ext") or {}
        connection.execute(
            "INSERT INTO NoteBlock (id, artifact_id, block_key, seq, kind, text, ext_confidence, ext_source, ext_ref, "
            "ext_conflict, authored_by, warnings, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (block_id, artifact_id, block["key"], seq, block["kind"], block["text"], ext.get("confidence"),
             ext.get("source"), ext.get("ref"), int(bool(ext.get("conflict"))), block.get("authored_by", "ai"),
             json.dumps(block["warnings"], ensure_ascii=False), block.get("created_at", now)))
        for cite in block["cites"]:
            connection.execute(
                "INSERT INTO NoteCitation (id, block_id, kind, transcript_id, seg_seq, material_id, page_no, quote) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (new_ulid(), block_id, cite["kind"], cite.get("transcript_id"), cite.get("seg_seq"),
                 cite.get("material_id"), cite.get("page_no"), cite.get("quote")))
    append(connection, "NOTE_VERSION_CREATED", "StudyArtifact", artifact_id,
           {"lecture_id": lecture_id, "version": version, "blocks": len(validation.blocks),
            "rejected": len(validation.rejected), "created_by": created_by, "reason": reason})
    return artifact_id


def load(connection: sqlite3.Connection, lecture_id: str) -> dict | None:
    """Latest note with citations, stale marks (a cited segment was corrected later, CORR-003)
    and the external-knowledge summary for the header (SRC-004)."""
    latest = _latest(connection, lecture_id)
    if latest is None:
        return None
    artifact_id, version = latest
    corrected_after: dict[tuple[str, int], int] = {}
    for c in corrections.active(connection, lecture_id):
        spot = (c["anchor"]["transcript_id"], c["anchor"]["seg_seq"])
        corrected_after[spot] = max(corrected_after.get(spot, 0), c["created_at"])
    blocks = []
    for r in connection.execute(
            "SELECT id, block_key, kind, text, ext_confidence, ext_source, ext_ref, ext_conflict, authored_by, warnings, "
            "created_at FROM NoteBlock WHERE artifact_id = ? ORDER BY seq", (artifact_id,)):
        cites = [{"kind": c[0], "transcript_id": c[1], "seg_seq": c[2], "material_id": c[3], "page_no": c[4],
                  "quote": c[5], "start_ms": c[6]}
                 for c in connection.execute(
                     "SELECT n.kind, n.transcript_id, n.seg_seq, n.material_id, n.page_no, n.quote, s.start_ms "
                     "FROM NoteCitation n LEFT JOIN TranscriptSegment s ON s.transcript_id = n.transcript_id "
                     "AND s.seq = n.seg_seq WHERE n.block_id = ? ORDER BY n.rowid", (r[0],))]
        stale = any(corrected_after.get((c["transcript_id"], c["seg_seq"]), 0) > r[10]
                    for c in cites if c["kind"] == "transcript")
        blocks.append({"id": r[0], "key": r[1], "kind": r[2], "text": r[3],
                       "ext": {"confidence": r[4], "source": r[5], "ref": r[6], "conflict": bool(r[7])}
                       if r[2] == "external" else None,
                       "authored_by": r[8], "warnings": json.loads(r[9]), "cites": cites, "stale": stale,
                       "created_at": r[10]})
    external = [b["ext"]["confidence"] for b in blocks if b["kind"] == "external"]
    inputs = json.loads(connection.execute("SELECT inputs FROM StudyArtifact WHERE id = ?",
                                           (artifact_id,)).fetchone()[0])
    return {"artifact_id": artifact_id, "version": version, "blocks": blocks,
            "external": {"total": len(external), "high": external.count("high"), "mid": external.count("mid"),
                         "low": external.count("low")},
            "rejected": inputs.get("rejected", []), "verify_later": [b["key"] for b in blocks
                                                                    if b["ext"] and b["ext"]["confidence"] == "low"]}


def _carry(block: dict) -> dict:
    """A stored block as validator output, so a new version can carry it over unchanged."""
    return {"key": block["key"], "kind": block["kind"], "text": block["text"], "ext": block["ext"],
            "warnings": block["warnings"], "authored_by": block["authored_by"], "created_at": block["created_at"],
            "cites": [{k: c[k] for k in ("kind", "transcript_id", "seg_seq", "material_id", "page_no", "quote")}
                      for c in block["cites"]]}


def edit_block(connection: sqlite3.Connection, lecture_id: str, key: str, text: str, reason: str) -> str:
    """The user rewrites one block: a new version, that block marked user-authored (kept on regeneration)."""
    note = load(connection, lecture_id)
    if note is None or not any(b["key"] == key for b in note["blocks"]):
        raise ValueError("고칠 노트 블록이 없습니다")
    if not text.strip():
        raise ValueError("빈 내용으로 바꿀 수 없습니다")
    blocks = []
    for block in note["blocks"]:
        carried = _carry(block)
        if block["key"] == key:
            carried.update(text=text.strip(), authored_by="user", warnings=[], created_at=time.time_ns() // 1_000_000)
        blocks.append(carried)
    artifact_id = save(connection, lecture_id, Validation(blocks=blocks), created_by="user",
                       inputs={"edited_block": key, "parent": note["artifact_id"]}, reason=reason)
    append(connection, "NOTE_BLOCK_EDITED", "StudyArtifact", artifact_id, {"block_key": key, "reason": reason})
    return artifact_id


def replace_blocks(connection: sqlite3.Connection, lecture_id: str, fresh: Validation, keys: set[str] | None,
                   *, created_by: str, inputs: dict) -> str:
    """Regenerate: take ``fresh`` blocks for ``keys`` (all when None) but never overwrite a block the user
    wrote (CORR-004). Blocks not regenerated are carried over."""
    note = load(connection, lecture_id)
    if note is None:
        return save(connection, lecture_id, fresh, created_by=created_by, inputs=inputs, reason="새 노트")
    fresh_by_key = {b["key"]: b for b in fresh.blocks}
    blocks, kept = [], []
    for block in note["blocks"]:
        wanted = keys is None or block["key"] in keys
        if wanted and block["authored_by"] == "user":
            kept.append(block["key"])
        if wanted and block["authored_by"] != "user" and block["key"] in fresh_by_key:
            blocks.append(fresh_by_key.pop(block["key"]))
        else:
            blocks.append(_carry(block))
    if keys is None:
        blocks.extend(fresh_by_key.values())  # new sections the regenerated note added
    return save(connection, lecture_id, Validation(blocks=blocks, rejected=fresh.rejected), created_by=created_by,
                inputs={**inputs, "kept_user_blocks": kept, "parent": note["artifact_id"]},
                reason="다시 만들기" if keys is None else f"블록 다시 만들기: {', '.join(sorted(keys))}")
