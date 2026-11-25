"""Independent review of the AI app link (D-072), 2026-11-24: attacks on its promises. Synthetic data only.

Tests that were marked ``xfail(strict=True)`` reproduced a REAL BUG found in review; the bugs are fixed on branch
mcp-opt and the markers removed (the fix for each is named in docs/DECISIONS.md D-072). The other tests pin
behaviour that held up under attack. Transcripts now come as one text of "[mm:ss] ..." lines paged by ``next_seq``.
"""
from __future__ import annotations

import io
import json
import sqlite3
from contextlib import closing

import pytest
from test_mcp_access import ALLOWED, HIDDEN, MARKER, build_library, call

from lecturehub.core import db, settings
from lecturehub.mcp import server, tools


@pytest.fixture
def library(tmp_path, monkeypatch):
    root = tmp_path / "학기 (합성) 26-2"
    build_library(root)
    monkeypatch.setenv("LECTUREHUB_ROOT", str(root))
    monkeypatch.setenv("LECTUREHUB_EDITION", "full")
    settings.set_ai_link(True)
    return root


def _write(root, sql: str, args=()) -> None:
    with closing(db.connect(root / "_lecturehub" / "lecturehub.sqlite")) as c:
        c.execute(sql, args)
        c.commit()


def _transcript(root, lecture_id: str, starts, text=lambda seq: f"s{seq}-" + "x" * 8) -> None:
    """A new lecture of the allowed course with one transcript (segments are immutable once written)."""
    with closing(db.connect(root / "_lecturehub" / "lecturehub.sqlite")) as c:
        c.execute("INSERT INTO Lecture VALUES (?, 'ca', '2026-09-21', 1, NULL, NULL)", (lecture_id,))
        c.execute("INSERT INTO Transcript (id,lecture_id,media_id,engine,role,role_decided_by,status,source_file,sha256) "
                  "VALUES (?, ?, NULL, 'gemini_transcribe', 'timeline', 'policy', 'done', ?, ?)",
                  ("t" + lecture_id, lecture_id, "x.txt", "t" + lecture_id))
        for seq, start in enumerate(starts):
            c.execute("INSERT INTO TranscriptSegment (transcript_id,seq,start_ms,text,chunk_index) VALUES (?,?,?,?,0)",
                      ("t" + lecture_id, seq, start, text(seq)))
        c.commit()


def _read_all(lecture_id: str) -> list[str]:
    seen, args = [], {"lecture_id": lecture_id}
    for _ in range(20):
        payload, _, _ = call("get_transcript", args)
        lines = payload.get("text", "").splitlines()
        assert len(lines) == payload["count"]
        seen += [line.split("] ", 1)[1][:2] for line in lines]
        if not payload["truncated"]:
            break
        args = {"lecture_id": lecture_id, "from_seq": payload["next_seq"]}
    return seen


def _serve(lines: list[bytes]) -> list[dict]:
    out = io.BytesIO()
    server.serve(io.BytesIO(b"".join(lines)), out)
    return [json.loads(line) for line in out.getvalue().splitlines()]


INIT = b'{"jsonrpc":"2.0","id":0,"method":"initialize","params":{}}\n'
PING = b'{"jsonrpc":"2.0","id":99,"method":"ping"}\n'


# ------------------------------------------------------------------ robustness of the stdio loop
@pytest.mark.parametrize("line", [
    b'{"jsonrpc":"2.0","id":"\\ud800","method":"ping"}\n',
    b'{"jsonrpc":"2.0","id":1,"method":"x\\udfff"}\n',
    b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"search","arguments":{"\\ud800":1}}}\n',
])
def test_REVIEW_lone_surrogate_never_kills_the_loop(line):
    replies = _serve([INIT, line, PING])
    assert replies[-1]["id"] == 99


def test_REVIEW_deeply_nested_json_is_a_parse_error_not_a_crash():
    deep = b"[" * 200_000 + b"]" * 200_000 + b"\n"
    replies = _serve([INIT, deep, PING])
    assert replies[-1]["id"] == 99


@pytest.mark.parametrize("line", [
    b"\xff\xfe\x00garbage\n", b'{"jsonrpc":"2.0","id":1.5,"method":"ping"}\n',
    b'{"jsonrpc":"2.0","id":{"a":1},"method":"ping"}\n', b'{"jsonrpc":"2.0","id":' + b"9" * 4000 + b',"method":"ping"}\n',
    b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":[1,2]}\n', b'{"jsonrpc":"2.0","id":1,"method":"\\u0000"}\n',
    b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"search","arguments":{"query":7}}}\n',
    b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"get_transcript","arguments":{"lecture_id":"a1","from_ms":true}}}\n',
    b'\x00\x00\x00\n', b'{"jsonrpc":"2.0","id":1,"method":"ping"',  # closed mid-line
])
def test_REVIEW_odd_messages_keep_the_loop_alive(line):
    replies = _serve([INIT, line, b"\n", PING])
    assert replies[-1]["id"] == 99 or not line.endswith(b"\n")


def test_REVIEW_fts_and_like_special_characters_are_literal(library):
    for query in ('"', '""', "*", "a*", "NEAR(a b)", "col:x", "관수로 OR 비밀", "%", "_", "\\", "%_", "^관", "'; --", "\x00ab"):
        payload, is_error, raw = call("search", {"query": query})
        assert MARKER not in raw and HIDDEN not in raw
        assert not is_error or payload["error"] == server.FAILED, (query, payload)


# ------------------------------------------------------------------ leaks
def test_REVIEW_candidate_evidence_from_a_course_that_is_not_allowed(library):
    _write(library, "UPDATE CalendarEvent SET course_id = 'ca' WHERE source_ref = 'ref-cb'")
    _, _, raw = call("list_calendar", {"from_date": "2026-10-01", "to_date": "2026-10-31"})
    assert f"{MARKER} 인용" not in raw


def test_REVIEW_note_file_in_the_folder_of_a_course_that_is_not_allowed(library):
    path = library / HIDDEN / "정리본" / "비공개 정리.html"
    _write(library, "UPDATE StudyArtifact SET lecture_ids = ? WHERE id = 'fb'", (json.dumps(["a1", "b1"]),))
    payload, _, raw = call("get_note", {"lecture_id": "a1", "note_id": "fb"})
    assert path.is_file()
    assert HIDDEN not in raw and MARKER not in raw, payload


def test_REVIEW_broken_settings_after_switching_off_must_mean_off(library):
    settings.set_ai_link(True)
    settings.set_ai_link(False)  # .bak now holds the 'on' file
    settings.settings_path().write_text("{broken", encoding="utf-8")
    payload, is_error, _ = call("list_courses")
    assert is_error and "꺼져" in payload["error"], payload


def test_REVIEW_space_variants_of_course_names_do_not_cross(library):
    """Allowed "합성과목 가" and a not-allowed "합성과목가": the spaceless name resolves to the allowed one only."""
    _write(library, "INSERT INTO Course VALUES ('cc','CC','합성과목가','cc','26-2')")
    _write(library, "INSERT INTO Lecture VALUES ('c1','cc','2026-09-10',1,NULL,NULL)")
    for name in ("합성과목가", " 합성과목 가 ", "합성과목\u3000가"):
        _, _, raw = call("list_lectures", {"course": name})
        assert "c1" not in raw and "'cc'" not in raw
    _, is_error, _ = call("get_transcript", {"lecture_id": "c1"})
    assert is_error


def test_REVIEW_announcement_course_whitespace_or_case_variant_is_not_shown(library):
    path = library / "_lecturehub" / "etl" / "announcements.json"
    rows = json.loads(path.read_text(encoding="utf-8"))
    rows += [{"id": "etl:9", "course": f" {HIDDEN}", "title": MARKER, "text": MARKER},
             {"id": "etl:10", "course": f"{ALLOWED} ", "title": MARKER, "text": MARKER}]
    path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    _, _, raw = call("list_announcements", {"limit": 50})
    assert MARKER not in raw


def test_REVIEW_cross_course_material_match_and_neighbors_stay_inside(library):
    _write(library, "INSERT INTO MaterialMatch (lecture_id,material_id,page_from,page_to,confidence_class,decided_by) "
                    "VALUES ('a1','mb',1,1,'high','user')")
    _write(library, "INSERT INTO MaterialMatch (lecture_id,material_id,page_from,page_to,confidence_class,decided_by) "
                    "VALUES ('b1','ma',1,2,'high','user')")
    for name, args in (("list_lectures", {}), ("list_lectures", {"course": ALLOWED}), ("get_material", {"material_id": "ma"}),
                       ("search", {"query": "관수로"}), ("search", {"query": "다르시"})):
        _, _, raw = call(name, args)
        assert MARKER not in raw and "\"mb\"" not in raw and "\"b1\"" not in raw, (name, raw[:300])


@pytest.mark.parametrize("consent", [
    {"ai_app_link": {ALLOWED: "yes"}}, {"ai_app_link": [ALLOWED]}, {"ai_app_link": {ALLOWED: {"granted_by": "agent"}}},
    {"ai_app_link": {ALLOWED: {"authority": "synthetic_test", "granted_by": "user"}}},
    {"ai_app_link": {f"{ALLOWED} ": {"authority": "confirmed_by_user"}}}, [1, 2], "x",
])
def test_REVIEW_malformed_or_synthetic_consent_shows_nothing(library, consent):
    (library / "_lecturehub" / "consent.json").write_text(json.dumps(consent, ensure_ascii=False), encoding="utf-8")
    _, _, raw = call("list_courses")
    assert ALLOWED not in raw
    # NOTE (review): the first two shapes fail closed with the generic FAILED error for EVERY tool (AttributeError in
    # ConsentStore.granted/authority_of), not with "no course allowed".


# ------------------------------------------------------------------ paging honesty
def test_REVIEW_search_paging_neither_drops_nor_repeats(library):
    # a stale index row (segment gone) sorts first among the hits
    _write(library, "INSERT INTO SearchIndex(id, source_kind, source_id, position, text) "
                    "VALUES ('stale', 'transcript', 'ta', '-5', '관수로 옛 줄')")
    seen, offsets, offset = [], [], 0
    for _ in range(10):
        payload, _, _ = call("search", {"query": "관수", "limit": 1, "offset": offset})
        seen += [(r["kind"], r.get("lecture_id") or r.get("material_id"), r.get("start_ms") or r.get("page"))
                 for r in payload["results"]]
        if not payload["truncated"]:
            break
        offset = payload["next_offset"]
        offsets.append(offset)
    assert len(offsets) == len(set(offsets)), offsets  # observed: [1, 1, 1, ...] - the pager never moves on
    assert len(seen) == len(set(seen)), seen


def test_REVIEW_transcript_paging_with_non_monotonic_start(library, monkeypatch):
    monkeypatch.setattr(tools, "TEXT_CAP", 25)
    _transcript(library, "a9", (0, 5_000, 3_000, 6_000, 1_000))
    seen = _read_all("a9")
    assert seen == [f"s{i}" for i in range(5)], seen


def test_REVIEW_transcript_paging_with_shared_start_ms(library, monkeypatch):
    monkeypatch.setattr(tools, "TEXT_CAP", 25)
    _transcript(library, "a8", (0, 1_000, 1_000, 1_000, 2_000, 2_000, 3_000))
    assert _read_all("a8") == [f"s{i}" for i in range(7)]


def test_REVIEW_one_huge_material_page_says_it_was_cut(library, monkeypatch):
    monkeypatch.setattr(tools, "TEXT_CAP", 50)
    _write(library, "UPDATE MaterialPage SET text = ? WHERE material_id = 'ma' AND page_no = 2", ("나" * 200,))
    payload, _, _ = call("get_material", {"material_id": "ma", "page_from": 2})
    assert payload["truncated"] or len(payload["pages"][0]["text"]) == 200, payload


def test_REVIEW_list_lectures_pager_always_moves_on(library, monkeypatch):
    monkeypatch.setattr(tools, "TEXT_CAP", 50)
    payload, _, _ = call("list_lectures", {})
    assert payload["count"] >= 1 or payload.get("next_offset", 0) > 0, payload


def test_REVIEW_background_helper_crash_is_remembered(tmp_path):
    import hashlib
    import subprocess
    import sys

    from lecturehub.materials import background

    root = tmp_path / "학기"
    (root / "_lecturehub").mkdir(parents=True)
    with closing(db.connect(root / "_lecturehub" / "lecturehub.sqlite")) as c:
        db.migrate(c)
        c.execute("INSERT INTO Course VALUES ('c','C','과목','c','26-2')")
        for i, name in enumerate(("a_no_text.hwp", "b_crash.pdf", "c_after.pdf")):
            path = root / "과목" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(name.encode() * 10)
            c.execute("INSERT INTO Material (id,course_id,path,sha256,kind,title,page_count) VALUES (?,?,?,?,?,?,0)",
                      (f"m{i}", "c", str(path), hashlib.sha256(path.read_bytes()).hexdigest(), path.suffix[1:], name))
        c.commit()
    child = ("import os, sys\nfrom lecturehub.materials import extract, background\nreal = extract.page_texts\n"
             "def fake(path, root):\n    if path.name == 'b_crash.pdf':\n        os._exit(139)\n    return real(path, root)\n"
             "extract.page_texts = fake\nbackground.run(sys.argv[1])\n")
    for _ in range(2):
        subprocess.run([sys.executable, "-c", child, str(root)], capture_output=True, timeout=120, check=False)
    with closing(db.connect_readonly(root / "_lecturehub" / "lecturehub.sqlite")) as c:
        left = background.pending(c, root)
    assert "m0" not in left, left  # observed: ['m0', 'm1', 'm2'] after every run, material_text.json never written
    # fixed (H1): the crasher is given up on the second run and the file after it is reached
    assert left == [], left
    tried = json.loads(background.state_path(root).read_text(encoding="utf-8"))["materials"]
    assert tried["m1"]["reason"] == background.CRASHED_REASON and set(tried) == {"m0", "m1", "m2"}
    assert not background.marker_path(root).exists()


def test_REVIEW_floor_clocks(library):
    _transcript(library, "a7", (3_599_999, 3_600_000))
    payload, _, _ = call("get_transcript", {"lecture_id": "a7"})
    assert payload["text"].splitlines()[0].startswith("[59:59] "), payload
    assert payload["text"].splitlines()[1].startswith("[1:00:00] "), payload
    assert tools._day_clock(59_999) == "00:00" and tools._day_clock(86_399_999) == "23:59"


def test_REVIEW_readonly_connection_never_writes(library):
    with closing(db.connect_readonly(library / "_lecturehub" / "lecturehub.sqlite")) as c:
        for sql in ("INSERT INTO Course VALUES ('z','z','z','z','z')", "PRAGMA journal_mode=DELETE",
                    "CREATE TEMP TABLE t(x)", "ATTACH DATABASE ':memory:' AS m"):
            try:
                c.execute(sql)
            except sqlite3.Error:
                continue
            assert sql.startswith(("CREATE TEMP", "ATTACH", "PRAGMA journal_mode")), sql  # temp/in-memory only
    assert not (library / "_lecturehub" / "lecturehub.sqlite-wal").exists()
