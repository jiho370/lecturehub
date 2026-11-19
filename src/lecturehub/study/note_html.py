"""Evidence-checked note → one HTML 정리본 with transcript footnotes (user decision 2026-11-13).

The engine still answers in blocks with citations, and :func:`notes.validate` still drops every sentence whose
citation is not in the lecture's transcript or material (the note is saved only with evidence). What the user
reads is no longer a chip view inside the app: the validated note becomes a plain HTML file in the course's
정리본 folder — readable prose, and each citation a numbered footnote with the time and the words said.

The file is attached to the lecture like a hand-made note. A later run rewrites only a file this module wrote
and nobody edited since (same hash); a note the user or another tool made is never overwritten.
"""
from __future__ import annotations

import hashlib
import html
import json
import sqlite3
from pathlib import Path

from . import notes

QUOTE_CHARS = 90
GENERATOR = "lecturehub-note-html"

STYLE = """
:root{--bg:#fff;--fg:#1d1d1f;--muted:#6b6b70;--line:#e3e3e6;--accent:#2f6fde;--aside:#f5f6f8;--warn:#a15c00}
@media (prefers-color-scheme:dark){:root{--bg:#18181b;--fg:#ececee;--muted:#a0a0a8;--line:#33333a;--accent:#7aa7ff;
--aside:#222228;--warn:#e0a24a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.75 "Pretendard","Malgun Gothic",
system-ui,sans-serif}main{max-width:760px;margin:0 auto;padding:32px 16px 64px}
header{border-bottom:1px solid var(--line);margin-bottom:24px}h1{font-size:1.6rem;margin:0 0 4px}
header p{color:var(--muted);margin:0 0 16px;font-size:.92rem}h2{font-size:1.2rem;margin:32px 0 8px}
p{margin:0 0 14px}ul{margin:0 0 14px;padding-left:1.3em}li{margin:4px 0}
sup a{color:var(--accent);text-decoration:none;font-size:.75em;padding:0 1px}
.ext{background:var(--aside);border-left:3px solid var(--line);padding:10px 14px;margin:0 0 14px;font-size:.95rem}
.ext b{font-size:.82rem;color:var(--muted);display:block}.check{color:var(--warn);font-size:.8rem}
.notes{border-top:1px solid var(--line);margin-top:40px;padding-top:12px;font-size:.88rem;color:var(--muted)}
.notes ol{padding-left:1.6em}.notes li{margin:6px 0}.notes .t{font-variant-numeric:tabular-nums;color:var(--fg)}
.notes q{color:var(--fg)}
"""


def _clock(ms: int | None) -> str:
    seconds = max(0, int(ms or 0) // 1000)
    return f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}" if seconds >= 3600 else \
        f"{seconds // 60:02d}:{seconds % 60:02d}"


def _short(text: str) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= QUOTE_CHARS else text[:QUOTE_CHARS - 1].rstrip() + "…"


def render(note: dict, *, title: str, subtitle: str, sources: notes.Sources, materials: dict[str, str]) -> str:
    """HTML for a loaded note (:func:`notes.load`). Same citation → same footnote number."""
    numbers: dict[tuple, int] = {}
    footnotes: list[str] = []

    def mark(cite: dict) -> str:
        if cite["kind"] == "transcript":
            spot = ("t", cite["transcript_id"], cite["seg_seq"], cite.get("quote") or "")
        else:
            spot = ("m", cite["material_id"], cite["page_no"])
        if spot not in numbers:
            numbers[spot] = len(numbers) + 1
            if cite["kind"] == "transcript":
                segment = sources.segment(cite["transcript_id"], cite["seg_seq"]) or {}
                said = cite.get("quote") or _short(segment.get("text", ""))
                footnotes.append(f'<li id="fn{numbers[spot]}"><span class="t">{_clock(cite.get("start_ms"))}</span> '
                                 f'<q>{html.escape(said)}</q></li>')
            else:
                name = materials.get(cite["material_id"], "강의자료")
                footnotes.append(f'<li id="fn{numbers[spot]}">{html.escape(name)} {cite["page_no"]}쪽</li>')
        n = numbers[spot]
        return f'<sup><a href="#fn{n}">{n}</a></sup>'

    body: list[str] = []
    items: list[str] = []

    def flush() -> None:
        if items:
            body.append("<ul>" + "".join(items) + "</ul>")
            items.clear()

    for block in note["blocks"]:
        text = html.escape(block["text"])
        marks = '<sup>,</sup>'.join(mark(c) for c in block["cites"])
        check = ' <span class="check">(확인 필요)</span>' if any("숫자" in w or "SRC-002" in w
                                                                 for w in block["warnings"]) else ""
        if block["kind"] == "item":
            items.append(f"<li>{text}{marks}{check}</li>")
            continue
        flush()
        if block["kind"] == "heading":
            body.append(f"<h2>{text}</h2>")
        elif block["kind"] == "external":
            ext = block.get("ext") or {}
            level = {"high": "높음", "mid": "보통", "low": "낮음"}.get(ext.get("confidence"), "")
            conflict = " · 수업 내용과 다름" if ext.get("conflict") else ""
            body.append(f'<div class="ext"><b>배경 설명(수업 밖 지식, 확신도 {level}{conflict})</b>{text}</div>')
        else:
            body.append(f"<p>{text}{marks}{check}</p>")
    flush()
    notes_part = ('<section class="notes"><h2>근거(녹음 시각과 원문)</h2><ol>' + "".join(footnotes) + "</ol></section>"
                  if footnotes else "")
    return ("<!doctype html>\n<html lang=\"ko\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<meta name=\"generator\" content=\"{GENERATOR}\"><title>{html.escape(title)}</title>"
            f"<style>{STYLE}</style></head><body><main><header><h1>{html.escape(title)}</h1>"
            f"<p>{html.escape(subtitle)}</p></header>{''.join(body)}{notes_part}</main></body></html>\n")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ours(connection: sqlite3.Connection, path: Path) -> bool:
    """The file was written by this module and is unchanged since."""
    row = connection.execute("SELECT inputs FROM StudyArtifact WHERE kind = 'note' AND path = ?",
                             (str(path),)).fetchone()
    if row is None or not path.is_file():
        return False
    return json.loads(row[0]).get("generated_sha256") == _sha(path)


def _target(connection: sqlite3.Connection, lecture_id: str, pack: dict) -> Path:
    previous = connection.execute(
        "SELECT path FROM StudyArtifact WHERE kind = 'note' AND EXISTS (SELECT 1 FROM json_each(lecture_ids) j "
        "WHERE j.value = ?) AND json_extract(inputs, '$.generated_sha256') IS NOT NULL ORDER BY rowid DESC",
        (lecture_id,)).fetchall()
    for (path,) in previous:
        if _ours(connection, Path(path)):
            return Path(path)
    target = Path(pack["target"])
    stem, n = target.stem, 1
    while target.exists():
        n += 1
        target = target.with_name(f"{stem}_{n}.html")
    return target


def publish(connection: sqlite3.Connection, root: Path, lecture_id: str, *, created_by: str) -> dict:
    """Write the lecture's latest validated note as an HTML file and attach it. Caller commits."""
    from lecturehub.index import note_attach, note_pack

    note = notes.load(connection, lecture_id)
    if note is None or not note["blocks"]:
        raise ValueError("검증을 통과한 정리본 내용이 없습니다.")
    pack = note_pack.pack(connection, root, lecture_id)
    target = _target(connection, lecture_id, pack)
    materials = {r[0]: r[1] for r in connection.execute("SELECT id, title FROM Material")}
    title = f"{pack['course']} {pack['label']} 정리본"
    subtitle = f"{pack['date']} · 본문의 작은 번호를 누르면 그 말을 한 녹음 시각과 원문이 나옵니다."
    text = render(note, title=title, subtitle=subtitle, sources=notes.Sources(connection), materials=materials)
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(".html.part")
    part.write_text(text, encoding="utf-8")
    part.replace(target)
    linked = note_attach.attach(connection, root, lecture_id, target)
    row = connection.execute("SELECT inputs FROM StudyArtifact WHERE id = ?", (linked["artifact_id"],)).fetchone()
    inputs = {**json.loads(row[0]), "generated_sha256": _sha(target), "lecture_note": note["artifact_id"],
              "generator": GENERATOR}
    warned = any(b["warnings"] for b in note["blocks"]) or bool(note["rejected"])
    connection.execute("UPDATE StudyArtifact SET inputs = ?, created_by = ?, qa_status = ? WHERE id = ?",
                       (json.dumps(inputs, ensure_ascii=False), created_by, "warning" if warned else "passed",
                        linked["artifact_id"]))
    return {"path": str(target), "artifact_id": linked["artifact_id"], "footnotes": text.count('<li id="fn')}
