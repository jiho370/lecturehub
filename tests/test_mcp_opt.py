"""AI app link (D-072), branch mcp-opt: lean entry point, an honest size cap for the whole reply, compact shapes,
paging that returns every item exactly once, tool annotations, and the review findings of 2026-11-24 that the
attack tests (test_mcp_review_attacks.py) do not already pin. Synthetic data only.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path

import pytest
from test_mcp_access import ALLOWED, HIDDEN, MARKER, build_library, call

import lecturehub
from lecturehub.core import db, settings
from lecturehub.mcp import server, tools

WORDS = ["관수로", "마찰손실", "레이놀즈 수", "층류", "난류", "다르시 식", "에너지 경사", "손실수두", "유량", "단면적"]


@pytest.fixture
def library(tmp_path, monkeypatch):
    root = tmp_path / "학기 (합성) 26-2"
    build_library(root)
    monkeypatch.setenv("LECTUREHUB_ROOT", str(root))
    monkeypatch.setenv("LECTUREHUB_EDITION", "full")
    settings.set_ai_link(True)
    return root


def _db(root: Path):
    return closing(db.connect(root / "_lecturehub" / "lecturehub.sqlite"))


def _starts(n: int) -> list[int]:
    """~45 minutes of start times; every 50th segment shares the start of the one before it."""
    starts = []
    for seq in range(n):
        start = (seq // 2) * 7_701 + (seq % 2) * 3_333
        starts.append(starts[-1] if seq % 50 == 7 else start)
    return starts


def big_library(root: Path) -> None:
    """Real-size pieces on top of build_library: a 700-line transcript, 250 lectures, long material pages (one far
    longer than a reply), a long note, 60 long announcements and 300 calendar events, all in the allowed course."""
    from lecturehub.index import calendar_events

    with _db(root) as c:
        c.execute("INSERT INTO Lecture VALUES ('a3', 'ca', '2026-09-21', 1, NULL, NULL)")
        c.execute("INSERT INTO Transcript (id,lecture_id,media_id,engine,role,role_decided_by,status,source_file,sha256) "
                  "VALUES ('t7', 'a3', NULL, 'gemini_transcribe', 'timeline', 'policy', 'done', NULL, 't7')")
        for seq, start in enumerate(_starts(700)):
            text = f"오늘 {WORDS[seq % 10]}에 대해 이야기하면 \"{WORDS[(seq * 3) % 10]}\"과 연결됩니다 ({seq}번째 문장)."
            c.execute("INSERT INTO TranscriptSegment (transcript_id,seq,start_ms,end_ms,speaker,text,chunk_index) "
                      "VALUES (?,?,?,?,?,?,0)", ("t7", seq, start, start + 3_000, "교수" if seq % 9 == 0 else None, text))
        for n in range(250):
            day = f"2026-{9 + n // 120:02d}-{1 + n % 28:02d}"
            c.execute("INSERT INTO Lecture VALUES (?, 'ca', ?, ?, ?, NULL)", (f"x{n:03d}", day, 2 + n, f"합성 회차 {n}"))
        pages = [f"{p}쪽: " + "유량 계산 예제와 풀이. " * 150 for p in range(1, 31)]
        pages[11] = "긴 쪽 시작 " + "".join(f"[{i}] 단면적과 유속의 곱\n" for i in range(4_000))
        c.execute("INSERT INTO Material (id,course_id,path,sha256,kind,title,page_count) VALUES "
                  "('mz','ca',?, 'mz', 'pdf', '긴 자료', 30)", (str(root / ALLOWED / "강의자료" / "긴 자료.pdf"),))
        for no, text in enumerate(pages, 1):
            c.execute("INSERT INTO MaterialPage (material_id,page_no,text,terms) VALUES ('mz',?,?,'[]')", (no, text))
        for n in range(300):
            calendar_events.add(c, kind="assignment", title=f"과제 {n} " + "설명 " * 40, day=f"2026-10-{1 + n % 28:02d}",
                                course_id="ca", note="메모 " * 200)
        c.commit()
    note = root / ALLOWED / "정리본" / "가 정리.html"
    note.write_text("<p>" + "".join(f"{i}번 문단: 관수로의 \"마찰\" 손실은 <b>다르시</b> 식.</p><p>" for i in range(3_000))
                    + "</p>", encoding="utf-8")
    etl = root / "_lecturehub" / "etl" / "announcements.json"
    rows = json.loads(etl.read_text(encoding="utf-8"))
    rows += [{"id": f"etl:b{n}", "course": ALLOWED, "title": f"합성 공지 {n}", "posted": f"2026-10-{1 + n % 28:02d}T09:{n % 60:02d}",
              "text": "안내 문장. " * 900, "attachments": [f"첨부{i}.pdf" for i in range(30)]} for n in range(60)]
    etl.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")


def raw_text(name: str, arguments: dict | None = None) -> str:
    srv = server.Server()
    srv.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
    reply = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments or {}}})
    return reply["result"]["content"][0]["text"]


# ------------------------------------------------------------------ 1. lean entry point
FORBIDDEN = {"requests", "urllib3", "httpx", "truststore", "ssl", "_ssl", "socket", "_socket", "keyring", "typer",
             "click", "numpy", "pypdfium2", "pptx", "PIL", "webview", "http.client", "urllib.request", "certifi",
             "email", "lecturehub.cli", "lecturehub.index.ingest"}
CHILD = ("import atexit, json, runpy, sys\n"
         "atexit.register(lambda: sys.stderr.write('\\nMODULES ' + json.dumps(sorted(sys.modules)) + '\\n'))\n"
         "sys.argv = ['lecturehub.mcp']\n"
         "runpy.run_module('lecturehub.mcp', run_name='__main__', alter_sys=True)\n")  # as packaging/launcher_worker.py


def _child_env(root: Path) -> dict:
    env = dict(os.environ, LECTUREHUB_ROOT=str(root), PYTHONIOENCODING="cp949")
    src = str(Path(lecturehub.__file__).resolve().parents[1])
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    return env


def test_MCP_LEAN_001_the_serve_path_imports_no_network_tls_cli_or_heavy_library(library):
    big_library(library)
    calls = [("list_courses", {}), ("list_lectures", {"course": ALLOWED}), ("search", {"query": "마찰손실"}),
             ("search", {"query": "유량"}), ("get_transcript", {"lecture_id": "a3"}), ("get_note", {"lecture_id": "a2"}),
             ("get_material", {"material_id": "mz"}), ("list_calendar", {"from_date": "2026-01-01", "to_date": "2027-12-31"}),
             ("list_announcements", {}), ("list_assignments", {})]
    messages = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"}, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
    messages += [{"jsonrpc": "2.0", "id": 10 + i, "method": "tools/call", "params": {"name": n, "arguments": a}}
                 for i, (n, a) in enumerate(calls)]
    data = "".join(json.dumps(m, ensure_ascii=False) + "\n" for m in messages).encode("utf-8")
    done = subprocess.run([sys.executable, "-c", CHILD], input=data, capture_output=True, timeout=120,
                          env=_child_env(library), check=False)
    err = done.stderr.decode("utf-8", "replace")
    assert done.returncode == 0, err[-800:]
    replies = [json.loads(line) for line in done.stdout.decode("utf-8").splitlines()]
    assert [r["id"] for r in replies] == [1, 2, *range(10, 10 + len(calls))]
    assert not any(r["result"]["isError"] for r in replies[2:]), [r["result"] for r in replies[2:] if r["result"]["isError"]]
    modules = set(json.loads(err.rsplit("MODULES ", 1)[1].splitlines()[0]))
    assert "lecturehub.mcp.server" in modules and "lecturehub.mcp.tools" in modules
    assert not FORBIDDEN & modules, sorted(FORBIDDEN & modules)
    assert not {m for m in modules if m.startswith("lecturehub.") and m.endswith(".cli")}
    assert not {m for m in modules if m.split(".")[0] in {"requests", "urllib3", "truststore", "typer", "pptx"}}


def test_MCP_LEAN_002_alias_and_new_entry_answer_the_same(tmp_path):
    for args in (["-m", "lecturehub.mcp"], ["-m", "lecturehub", "mcp", "serve"]):
        done = subprocess.run([sys.executable, *args], input=json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode() + b"\n", capture_output=True, timeout=120,
            env=_child_env(tmp_path), check=False)
        assert done.returncode == 0 and json.loads(done.stdout)["error"]["code"] == server.NOT_INITIALIZED, args


def test_MCP_LEAN_003_frozen_build_finds_the_package_main():
    """launcher_worker runs ``-m <module>`` through runpy; the spec's module walk must list lecturehub.mcp.__main__."""
    spec = Path(lecturehub.__file__).resolve().parents[2] / "packaging" / "lecturehub.spec"
    if not spec.is_file():
        pytest.skip("public tree without packaging/")
    text = spec.read_text(encoding="utf-8")
    assert 'PKG.rglob("*.py")' in text and "parts[:-1] if parts[-1] == \"__init__\" else parts" in text
    assert (Path(lecturehub.__file__).parent / "mcp" / "__main__.py").is_file()  # walked like lecturehub/__main__.py
    launcher = (spec.parent / "launcher_worker.py").read_text(encoding="utf-8")
    assert "runpy.run_module(module" in launcher


def test_MCP_LEAN_004_applog_without_network_skips_the_certificate_step(tmp_path, monkeypatch):
    from lecturehub.core import applog

    called = []
    monkeypatch.setattr(applog, "_system_certificates", lambda: called.append(1))
    monkeypatch.setattr(applog, "_state", {"process": None, "crash_file": None})
    monkeypatch.setattr(applog, "_hook_hard_crash", lambda path: None)
    monkeypatch.setattr(applog, "_hook_uncaught", lambda: None)
    before = list(applog.LOG.handlers)
    try:
        assert applog.setup("worker", network=False) is not None and called == []
    finally:
        for handler in applog.LOG.handlers[len(before):]:
            applog.LOG.removeHandler(handler)
            handler.close()


# ------------------------------------------------------------------ 3. annotations
def test_MCP_PROTO_012_every_tool_has_a_korean_title_and_read_only_annotations():
    srv = server.Server()
    srv.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
    listed = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
    assert len(listed) == 13
    for tool in listed:
        assert tool["title"] and len(tool["title"]) <= 12 and any("\uac00" <= ch <= "\ud7a3" for ch in tool["title"])
        assert tool["annotations"] == {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True,
                                       "openWorldHint": False}, tool["name"]
    transcript = next(t for t in listed if t["name"] == "get_transcript")
    assert "[mm:ss]" in transcript["description"] and "next_seq" in transcript["description"]


# ------------------------------------------------------------------ 2. size cap and compact shapes
def test_MCP_CAP_001_the_whole_reply_text_of_every_tool_stays_within_the_cap(library):
    big_library(library)
    cases = [("list_courses", {}), ("list_lectures", {}), ("list_lectures", {"course": ALLOWED}),
             ("list_lectures", {"course": ALLOWED, "offset": 40}), ("search", {"query": "연결됩니다", "limit": 50}),
             ("search", {"query": "유량", "limit": 50}), ("search", {"query": "곱", "limit": 50}),
             ("get_transcript", {"lecture_id": "a3"}), ("get_transcript", {"lecture_id": "a3", "from_seq": 350}),
             ("get_note", {"lecture_id": "a2"}), ("get_note", {"lecture_id": "a2", "offset": 5_000}),
             ("get_material", {"material_id": "mz"}), ("get_material", {"material_id": "mz", "page_from": 12}),
             ("list_calendar", {"from_date": "2026-01-01", "to_date": "2027-12-31"}),
             ("list_announcements", {"limit": 50}), ("list_assignments", {})]
    for name, arguments in cases:
        text = raw_text(name, arguments)
        payload = json.loads(text)
        assert "error" not in payload, (name, payload)
        assert len(text) <= tools.TEXT_CAP, (name, arguments, len(text))
        assert payload.get("truncated") in (True, False, None)


def test_MCP_CAP_002_transcript_is_one_string_of_floor_clock_lines(library):
    with _db(library) as c:
        c.execute("INSERT INTO Lecture VALUES ('a6', 'ca', '2026-09-28', 1, NULL, NULL)")
        c.execute("INSERT INTO Transcript (id,lecture_id,media_id,engine,role,role_decided_by,status,source_file,sha256) "
                  "VALUES ('t6', 'a6', NULL, 'mai', 'timeline', 'policy', 'done', NULL, 't6')")
        for seq, (start, speaker, text) in enumerate([(59_999, None, "첫 줄\n둘로 나뉜 글"), (3_725_001, "학생 1", "질문입니다")]):
            c.execute("INSERT INTO TranscriptSegment (transcript_id,seq,start_ms,speaker,text,chunk_index) VALUES (?,?,?,?,?,0)",
                      ("t6", seq, start, speaker, text))
        c.commit()
    payload, error, _ = call("get_transcript", {"lecture_id": "a6"})
    assert not error and payload["text"] == "[00:59] 첫 줄 둘로 나뉜 글\n[1:02:05] 학생 1: 질문입니다"
    assert payload["from_ms"] == 59_999 and payload["count"] == 2 and payload["engine"] == "MAI"
    assert set(payload) == {"lecture_id", "course", "date", "label", "available_engines", "engine", "in_use",
                            "from_ms", "count", "text", "truncated"}


def test_MCP_CAP_003_lists_leave_out_empty_values_and_keep_every_source_field(library):
    found, _, _ = call("search", {"query": "다르시"})
    for item in found["results"]:
        assert all(v not in (None, "", []) for v in item.values()), item
        if item["kind"] == "transcript":
            assert {"course", "lecture_id", "date", "start_ms", "clock", "engine", "text"} <= set(item)
        else:
            assert {"course", "material_id", "page", "title", "text"} <= set(item)
    lectures, _, _ = call("list_lectures", {"course": ALLOWED})
    a2 = next(x for x in lectures["lectures"] if x["lecture_id"] == "a2")
    assert "transcript" not in a2 and "materials" not in a2 and "title" not in a2 and a2["has_note"] is True
    assert all(v not in (None, "", []) for x in lectures["lectures"] for v in x.values())


# ------------------------------------------------------------------ paging: every item exactly once
def _walk(name: str, args: dict, key: str, cursor: dict) -> list:
    """Follow next_* cursors; returns every item seen. ``cursor`` maps reply field -> argument name."""
    items, current = [], dict(args)
    for _ in range(400):
        payload, error, _ = call(name, current)
        assert not error, payload
        items += payload[key] if key != "text" else payload.get("text", "").splitlines()
        if not payload["truncated"]:
            return items
        moved = {arg: payload[field] for field, arg in cursor.items() if field in payload}
        assert moved, payload
        current = {**args, **moved}
    raise AssertionError("did not end")


def test_MCP_PAGE_001_transcript_walk_returns_every_line_once_and_never_splits_one_start_time(library, monkeypatch):
    big_library(library)
    monkeypatch.setattr(tools, "TEXT_CAP", 3_000)
    starts, pages, args = _starts(700), [], {"lecture_id": "a3"}
    for _ in range(200):
        payload, _, _ = call("get_transcript", args)
        pages.append(payload["text"].splitlines())
        if not payload["truncated"]:
            break
        assert "next_char" not in payload
        args = {"lecture_id": "a3", "from_seq": payload["next_seq"]}
        assert payload["next_from_ms"] == starts[payload["next_seq"]]
    lines = [line for page in pages for line in page]
    assert len(pages) > 5 and len(lines) == 700
    assert [int(line.rsplit("(", 1)[1].split("번째")[0]) for line in lines] == list(range(700))
    seq = 0
    for page in pages[:-1]:  # a page never ends inside a group of lines with one start time
        seq += len(page)
        assert starts[seq] != starts[seq - 1]


def test_MCP_PAGE_002_one_line_longer_than_a_page_is_continued_by_next_char(library, monkeypatch):
    long = "가나다라마바사 " * 500
    with _db(library) as c:
        c.execute("INSERT INTO Lecture VALUES ('a5', 'ca', '2026-09-28', 1, NULL, NULL)")
        c.execute("INSERT INTO Transcript (id,lecture_id,media_id,engine,role,role_decided_by,status,source_file,sha256) "
                  "VALUES ('t5', 'a5', NULL, 'gemini_transcribe', 'timeline', 'policy', 'done', NULL, 't5')")
        for seq, text in enumerate(["앞 줄", long, "뒤 줄"]):
            c.execute("INSERT INTO TranscriptSegment (transcript_id,seq,start_ms,text,chunk_index) VALUES (?,?,?,?,0)",
                      ("t5", seq, seq * 1_000, text))
        c.commit()
    monkeypatch.setattr(tools, "TEXT_CAP", 1_200)
    monkeypatch.setattr(tools, "MIN_ROOM", 300)
    body, args, replies = [], {"lecture_id": "a5"}, 0
    for _ in range(50):
        text = raw_text("get_transcript", args)
        payload = json.loads(text)
        replies += 1
        assert len(text) <= 1_200 + 300
        body += [line.split("] ", 1)[1] for line in payload["text"].splitlines()]
        if not payload["truncated"]:
            break
        args = {"lecture_id": "a5", "from_seq": payload["next_seq"]} | (
            {"from_char": payload["next_char"]} if "next_char" in payload else {})
    joined = "".join(body)
    assert replies > 3 and joined == "앞 줄" + " ".join(long.split()) + "뒤 줄"


def test_MCP_PAGE_003_search_walk_returns_every_hit_once(library):
    big_library(library)
    hits = _walk("search", {"query": "연결됩니다", "limit": 50}, "results", {"next_offset": "offset"})
    keys = [(h["lecture_id"], h["start_ms"], h["text"]) for h in hits]
    assert len(keys) == len(set(keys)) == 700


def test_MCP_PAGE_004_lectures_announcements_calendar_walks(library, monkeypatch):
    big_library(library)
    monkeypatch.setattr(tools, "TEXT_CAP", 4_000)
    lectures = _walk("list_lectures", {"course": ALLOWED}, "lectures", {"next_offset": "offset"})
    ids = [x["lecture_id"] for x in lectures]
    assert len(ids) == len(set(ids)) == 253
    notices = _walk("list_announcements", {"limit": 50}, "announcements", {"next_offset": "offset"})
    assert len(notices) == len({n["announcement_id"] for n in notices}) == 61  # limit is per reply; walk = all
    events = _walk("list_calendar", {"from_date": "2026-01-01", "to_date": "2027-12-31"}, "events", {"next_offset": "offset"})
    assert len(events) == len({e["event_id"] for e in events}) == 302


def test_MCP_PAGE_005_material_walk_rebuilds_every_page_exactly(library):
    big_library(library)
    with _db(library) as c:
        expected = dict(c.execute("SELECT page_no, text FROM MaterialPage WHERE material_id = 'mz'"))
    got: dict[int, str] = {}
    args = {"material_id": "mz"}
    for _ in range(100):
        text = raw_text("get_material", args)
        payload = json.loads(text)
        assert len(text) <= tools.TEXT_CAP
        for page in payload["pages"]:
            assert page.get("from_char", 0) == len(got.get(page["page"], ""))
            got[page["page"]] = got.get(page["page"], "") + page["text"]
        if not payload["truncated"]:
            break
        args = {"material_id": "mz", "page_from": payload["next_page"]} | (
            {"page_offset": payload["next_page_offset"]} if "next_page_offset" in payload else {})
    assert got == expected


def test_MCP_PAGE_006_a_cursor_that_would_not_move_on_is_an_error_not_a_loop():
    with pytest.raises(RuntimeError):
        tools._advance({}, {"next_offset": 3}, (3,), (3,))
    result = {}
    tools._advance(result, {"next_seq": 4, "next_char": 1}, (4, 0), (4, 1))
    assert result == {"next_seq": 4, "next_char": 1}


# ------------------------------------------------------------------ review findings not covered elsewhere
def test_REVIEW_L1_board_quote_without_a_lecture_is_not_shown(library):
    with _db(library) as c:
        c.execute("UPDATE Evidence SET kind = 'board_quote', detail = ? WHERE id = 'ev-ca'",
                  (json.dumps({"quote": "게시판 인용 문장"}, ensure_ascii=False),))
        c.commit()
    calendar, _, raw = call("list_calendar", {"from_date": "2026-10-01", "to_date": "2026-10-31"})
    candidate = next(e for e in calendar["events"] if e["status"] == "candidate")
    assert "evidence" not in candidate and "게시판 인용" not in raw


def test_REVIEW_L2_notes_linked_to_two_courses_or_in_another_courses_folder_are_not_listed(library):
    with _db(library) as c:  # the app note of a1 is now linked to a lecture of the course that is not allowed too
        c.execute("UPDATE StudyArtifact SET lecture_ids = ? WHERE id = 'na'", (json.dumps(["a1", "b1"]),))
        c.commit()
    lectures, _, _ = call("list_lectures", {"course": ALLOWED})
    a1 = next(x for x in lectures["lectures"] if x["lecture_id"] == "a1")
    assert a1["has_note"] is False
    note, error, raw = call("get_note", {"lecture_id": "a1"})
    assert not error and note["count"] == 0 and MARKER not in raw
    assert "na" not in call("get_note", {"lecture_id": "a1", "note_id": "na"})[2]
    # a file note of the allowed course kept in the allowed course's folder still shows
    assert call("get_note", {"lecture_id": "a2"})[0]["path"] == f"{ALLOWED}/정리본/가 정리.html"
    # the same file moved into the other course's folder (still linked to a2 only) is no longer read
    moved = library / HIDDEN / "정리본" / "옮긴 정리.html"
    moved.write_text(f"<p>{MARKER}</p>", encoding="utf-8")
    with _db(library) as c:
        c.execute("UPDATE StudyArtifact SET path = ? WHERE id = 'fa'", (str(moved),))
        c.commit()
    note, _, raw = call("get_note", {"lecture_id": "a2"})
    assert note["count"] == 0 and MARKER not in raw and HIDDEN not in raw
    lectures, _, _ = call("list_lectures", {"course": ALLOWED})
    assert next(x for x in lectures["lectures"] if x["lecture_id"] == "a2")["has_note"] is False


def test_REVIEW_L3_strict_settings_read_writes_nothing(library):
    path = settings.settings_path()
    settings.set_ai_link(False)
    path.write_text("[1, 2]", encoding="utf-8")  # valid JSON, not an object
    assert call("list_courses")[0] == {"error": server.access.OFF}
    path.write_text("{broken", encoding="utf-8")
    before = sorted(p.name for p in path.parent.iterdir())
    assert call("list_courses")[0] == {"error": server.access.OFF}
    assert sorted(p.name for p in path.parent.iterdir()) == before  # no .corrupt-* copy from the MCP side
    path.unlink()
    assert call("list_courses")[0] == {"error": server.access.OFF}


def test_REVIEW_C1_bad_text_from_the_database_or_files_never_kills_a_reply(library):
    with _db(library) as c:  # bytes that are not UTF-8 (an encoded lone surrogate) stored as TEXT
        c.execute("INSERT INTO Lecture VALUES ('a4', 'ca', '2026-09-30', 1, NULL, NULL)")
        c.execute("INSERT INTO Transcript (id,lecture_id,media_id,engine,role,role_decided_by,status,source_file,sha256) "
                  "VALUES ('t4', 'a4', NULL, 'gemini_transcribe', 'timeline', 'policy', 'done', NULL, 't4')")
        c.execute("INSERT INTO TranscriptSegment (transcript_id,seq,start_ms,text,chunk_index) "
                  "VALUES ('t4', 0, 0, CAST(X'EAB4800AEDA080EAB480' AS TEXT), 0)")
        c.commit()
    etl = library / "_lecturehub" / "etl" / "announcements.json"
    etl.write_text(etl.read_text(encoding="utf-8").replace("보고서는 금요일까지", "보고서\\ud800는"), encoding="utf-8")
    lines = [json.dumps({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}}).encode() + b"\n"]
    for i, (name, args) in enumerate([("get_transcript", {"lecture_id": "a4"}), ("list_announcements", {}),
                                      ("search", {"query": "관"})]):
        lines.append(json.dumps({"jsonrpc": "2.0", "id": i + 1, "method": "tools/call",
                                 "params": {"name": name, "arguments": args}}).encode() + b"\n")
    import io

    out = io.BytesIO()
    server.serve(io.BytesIO(b"".join(lines)), out)
    replies = [json.loads(line) for line in out.getvalue().decode("utf-8").splitlines()]  # strict UTF-8 decode
    assert [r["id"] for r in replies] == [0, 1, 2, 3]
    assert all(r["result"]["isError"] is False for r in replies[1:])


def test_REVIEW_C1_encode_never_raises():
    line = server.encode({"jsonrpc": "2.0", "id": "\ud800", "result": {}})
    assert json.loads(line)["id"] == "\ud800"
    assert json.loads(server.encode({"jsonrpc": "2.0", "id": 1, "result": {"x": object()}}))["error"]["code"] == -32603


def test_REVIEW_P5_snippet_contains_the_hit_whatever_its_case(library):
    long = "앞부분 " * 200 + "Darcy Weisbach equation" + " 뒷부분" * 200
    with _db(library) as c:
        c.execute("UPDATE MaterialPage SET text = ? WHERE material_id = 'ma' AND page_no = 1", (long,))
        c.commit()
    found, _, _ = call("search", {"query": "darcy weisbach"})
    assert found["count"] == 1 and "Darcy Weisbach" in found["results"][0]["text"]


def test_REVIEW_P6_one_malformed_consent_entry_hides_that_course_only(library):
    path = library / "_lecturehub" / "consent.json"
    consent = json.loads(path.read_text(encoding="utf-8"))
    consent["ai_app_link"][HIDDEN] = "yes"
    consent["ai_app_link"]["다른 과목"] = ["x"]
    path.write_text(json.dumps(consent, ensure_ascii=False), encoding="utf-8")
    courses, error, raw = call("list_courses")
    assert not error and [c["course"] for c in courses["courses"]] == [ALLOWED] and HIDDEN not in raw
    path.write_text(json.dumps({"ai_app_link": [ALLOWED]}, ensure_ascii=False), encoding="utf-8")
    courses, error, _ = call("list_courses")
    assert not error and courses["courses"] == []  # no course, and not the generic failure


def test_REVIEW_P7_a_lecture_left_with_only_a_thrown_away_recording_is_not_found(library):
    with _db(library) as c:
        c.execute("INSERT INTO Lecture VALUES ('a9', 'ca', '2026-09-29', 1, NULL, NULL)")
        c.execute("INSERT INTO MediaAsset VALUES ('mdz', 'a9', ?, 'sha-mdz', 60000, 'aac', 48000, 1, 9, 'app_mic', 'laptop', "
                  "1, NULL, NULL, 123, 0, '[]', 'user', 'exact')", (str(library / "_lecturehub" / "z.mp3"),))
        c.execute("INSERT INTO Evidence (id, subject_type, subject_id, field, kind, detail, weight, source, created_at) "
                  "VALUES ('ev-z', 'MediaAsset', 'mdz', NULL, 'discarded', '{}', NULL, 'user', ?)", (int(time.time() * 1000),))
        c.commit()
    for name in ("get_transcript", "get_note"):
        assert call(name, {"lecture_id": "a9"})[0] == {"error": server.access.NOT_FOUND}
    assert "a9" not in {x["lecture_id"] for x in call("list_lectures", {})[0]["lectures"]}


# ------------------------------------------------------------------ material helper (H2-H4)
def _semester(tmp_path: Path, files: dict[str, bytes]) -> Path:
    root = tmp_path / "학기"
    (root / "_lecturehub").mkdir(parents=True)
    with closing(db.connect(root / "_lecturehub" / "lecturehub.sqlite")) as c:
        db.migrate(c)
        c.execute("INSERT INTO Course VALUES ('c','C','과목','c','26-2')")
        for i, (name, data) in enumerate(files.items()):
            path = root / "과목" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            c.execute("INSERT INTO Material (id,course_id,path,sha256,kind,title,page_count) VALUES (?,?,?,?,?,?,0)",
                      (f"m{i}", "c", str(path), hashlib.sha256(data).hexdigest(), path.suffix[1:], name))
        c.commit()
    return root


def test_REVIEW_H2_size_zip_and_page_bounds_are_written_down_and_not_retried(tmp_path, monkeypatch):
    import zipfile

    pytest.importorskip("pypdfium2")
    from lecturehub.materials import background, extract
    from lecturehub.materials.pdfdoc import write_slides

    pdf = tmp_path / "src.pdf"
    write_slides(pdf, [(f"Slide {i}", ["text"]) for i in range(3)])
    bomb = tmp_path / "bomb.pptx"
    with zipfile.ZipFile(bomb, "w") as archive:
        for i in range(5):
            archive.writestr(f"ppt/x{i}.xml", "a" * 10)
    size = pdf.stat().st_size
    root = _semester(tmp_path, {"big.pdf": b"%PDF" + b"0" * (size + 100), "bomb.pptx": bomb.read_bytes(),
                                "long.pdf": pdf.read_bytes()})
    monkeypatch.setattr(extract, "MAX_FILE_BYTES", size + 10)
    monkeypatch.setattr(extract, "MAX_ZIP_ENTRIES", 4)
    monkeypatch.setattr(extract, "MAX_PAGES", 2)
    result = background.run(root)
    assert result["state"] == "done" and result["capped"] == ["m2"] and result["no_text"] == 2
    tried = json.loads(background.state_path(root).read_text(encoding="utf-8"))["materials"]
    assert "너무 큼" in tried["m0"]["reason"] and "너무 큼" in tried["m1"]["reason"]
    with closing(db.connect_readonly(root / "_lecturehub" / "lecturehub.sqlite")) as c:
        assert c.execute("SELECT count(*) FROM MaterialPage WHERE material_id = 'm2'").fetchone()[0] == 2
        assert c.execute("SELECT page_count FROM Material WHERE id = 'm2'").fetchone()[0] == 0  # not the capped count
        assert background.pending(c, root) == []


def test_REVIEW_H3_a_long_file_stops_between_pages_when_a_recording_starts(tmp_path):
    pytest.importorskip("pypdfium2")
    from lecturehub.materials.extract import extract
    from lecturehub.materials.pdfdoc import write_slides

    pdf = tmp_path / "src.pdf"
    write_slides(pdf, [(f"Slide {i}", ["text"]) for i in range(45)])
    root = _semester(tmp_path, {"long.pdf": pdf.read_bytes()})
    asked = []
    busy = lambda: asked.append(1) or ("20260907_090000_mic" if len(asked) >= 3 else None)  # file, page 6, page 11
    with closing(db.connect(root / "_lecturehub" / "lecturehub.sqlite")) as c:
        report = extract(c, root, busy=busy)
        assert c.execute("SELECT count(*) FROM MaterialPage").fetchone()[0] == 0  # nothing half-written
    assert report.extracted == [] and report.skipped[0]["reason"].startswith("녹음 중") and len(asked) == 3


class _Proc:
    def __init__(self):
        self.terminated = self.killed = False

    def poll(self):
        return 0 if (self.terminated or self.killed) else None

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return 0


def _api(root, phase="idle"):
    from lecturehub.ui.app import Api

    backend = type("B", (), {"root": root, "needs_setup": False, "phase": phase,
                             "state": lambda self: {"phase": self.phase},
                             "start": lambda self, *a: {"ok": True}})()
    return Api(backend, type("L", (), {"root": root})())


def test_REVIEW_H3_starting_a_recording_stops_the_helper_and_clears_its_marker(tmp_path):
    from lecturehub.materials import background

    root = _semester(tmp_path, {})
    api = _api(root)
    api._material_proc = _Proc()
    background.marker_path(root).parent.mkdir(parents=True, exist_ok=True)
    background.marker_path(root).write_text('{"material_id": "m0"}', encoding="utf-8")
    assert api.start("과목") == {"ok": True}
    for _ in range(100):  # stopped from a daemon thread, after the recording started
        if api._material_proc.terminated and not background.marker_path(root).exists():
            break
        time.sleep(0.05)
    assert api._material_proc.terminated and not background.marker_path(root).exists()
    api._material_proc = _Proc()  # a recording found by the regular kick stops it too
    settings.set_ai_link(True)
    api._backend.phase = "recording"
    assert api._material_text_kick() == "recording" and api._material_proc.terminated


@pytest.mark.parametrize("how", ["blocks", "raises"])
def test_D050_start_never_waits_for_the_helper_stop(tmp_path, how):
    """Api.start / start_selected start the backend at once; the helper is stopped off the caller's path."""
    import threading

    root = _semester(tmp_path, {})
    api = _api(root)
    release, entered, started = threading.Event(), threading.Event(), []
    api._backend.start = lambda *a: started.append(a) or {"ok": True}
    api._backend.start_selected = lambda: started.append("selected") or {"ok": True}
    api._backend.selection = {"course": "과목", "course_source": "user_pick"}
    api._backend.recording_permission = lambda course: {"allowed": True}

    def stop(why):
        entered.set()
        if how == "raises":
            raise RuntimeError("stop failed")
        release.wait(10)

    api._material_stop = stop
    try:
        begun = time.monotonic()
        assert api.start("과목") == {"ok": True}
        assert api.start_selected() == {"ok": True}
        assert time.monotonic() - begun < 1 and len(started) == 2 and started[1] == "selected"
        assert entered.wait(5)  # the stop did run, in its own thread
    finally:
        release.set()


def test_REVIEW_H4_one_helper_across_restarts_and_a_stuck_one_is_ended(tmp_path):
    from lecturehub.materials import background
    from lecturehub.recorder.heartbeat import SessionLock

    root = _semester(tmp_path, {"a.hwp": b"hwp"})
    settings.set_ai_link(True)
    folder = background.lock_dir(root)
    folder.mkdir(parents=True)
    with SessionLock(folder):  # another helper (started before an app restart) is at work
        assert background.running(root) is True
        assert background.run(root) == {"state": "running"}
        assert _api(root)._material_text_kick() == "running"
    assert background.running(root) is False
    api = _api(root)
    api._material_proc = _Proc()
    api._material_started = time.monotonic() - background.TIMEOUT_S - 1
    assert api._material_text_kick() == "stopped" and api._material_proc.killed


def test_MCP_ARGS_a_limit_out_of_range_is_clamped_and_other_numbers_are_still_refused():
    from lecturehub.mcp import tools

    assert tools.check_arguments("search", {"query": "가", "limit": 500})["limit"] == 50
    assert tools.check_arguments("search", {"query": "가", "limit": 0})["limit"] == 1
    assert tools.check_arguments("list_announcements", {"limit": 10**9})["limit"] == 50
    with pytest.raises(tools.InvalidArguments):
        tools.check_arguments("search", {"query": "가", "offset": -1})
    with pytest.raises(tools.InvalidArguments):
        tools.check_arguments("search", {"query": "가", "limit": True})
