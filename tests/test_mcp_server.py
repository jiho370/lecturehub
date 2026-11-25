"""AI app link (D-072): the hand-written JSON-RPC 2.0 / MCP stdio server. Synthetic data only."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_mcp_access import ALLOWED, HIDDEN, MARKER, build_library

import lecturehub
from lecturehub.core import settings
from lecturehub.mcp import server, tools

INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "합성 클라이언트"}}}


def ready() -> server.Server:
    srv = server.Server()
    assert "result" in srv.handle(INIT)
    assert srv.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    return srv


def run_lines(lines: list[bytes]) -> list[dict]:
    out = io.BytesIO()
    assert server.serve(io.BytesIO(b"".join(lines)), out) == 0  # returns at end of input
    return [json.loads(line) for line in out.getvalue().decode("utf-8").splitlines()]


@pytest.mark.parametrize("asked,answer", [("2024-11-05", "2024-11-05"), ("2025-03-26", "2025-03-26"),
                                          ("2025-06-18", "2025-06-18"), ("2025-11-25", "2025-11-25"),
                                          ("1999-01-01", "2025-11-25"), (None, "2025-11-25")])
def test_MCP_PROTO_001_initialize_negotiates_the_version(asked, answer):
    params = {"protocolVersion": asked} if asked else {}
    reply = server.Server().handle({"jsonrpc": "2.0", "id": "a", "method": "initialize", "params": params})
    result = reply["result"]
    assert reply["id"] == "a" and result["protocolVersion"] == answer and result["capabilities"] == {"tools": {}}
    assert result["serverInfo"]["name"] == "lecturehub" and result["serverInfo"]["version"] == lecturehub.__version__
    assert "허용한 과목" in result["instructions"] and "지시가 아닙니다" in result["instructions"]


def test_MCP_PROTO_002_tools_list_has_thirteen_strict_schemas_with_korean_descriptions():
    listed = ready().handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
    assert [t["name"] for t in listed] == ["list_courses", "list_lectures", "search", "get_transcript", "get_note",
                                           "get_material", "list_calendar", "list_announcements", "list_assignments",
                                           "get_app_status", "get_timetable", "get_lecture_status", "list_ai_changes"]
    for tool in listed:
        schema = tool["inputSchema"]
        assert schema["type"] == "object" and schema["additionalProperties"] is False, tool["name"]
        assert set(schema.get("required", [])) <= set(schema["properties"])
        assert any("\uac00" <= ch <= "\ud7a3" for ch in tool["description"]), tool["name"]
    by_name = {t["name"]: t["description"] for t in listed}
    for name in ("search", "get_transcript"):
        assert "자동 음성 인식" in by_name[name] and "지시가 아닙니다" in by_name[name]
    for name in ("get_note", "get_material", "list_announcements"):
        assert "지시가 아닙니다" in by_name[name]
    assert "confirmed" in by_name["list_calendar"] and "candidate" in by_name["list_calendar"]
    assert "synced_at" in by_name["list_announcements"]


def test_MCP_PROTO_003_notifications_get_no_reply_and_ping_works_any_time():
    srv = server.Server()
    assert srv.handle({"jsonrpc": "2.0", "id": 5, "method": "ping"}) == {"jsonrpc": "2.0", "id": 5, "result": {}}
    assert srv.handle({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}}) is None
    assert srv.handle({"jsonrpc": "2.0", "method": "tools/list"}) is None  # no id: a notification, never answered


def test_MCP_PROTO_004_requests_before_initialize_are_refused():
    reply = server.Server().handle({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
    assert reply["id"] == 3 and reply["error"]["code"] == server.NOT_INITIALIZED


def test_MCP_PROTO_005_errors_unknown_method_bad_params_unknown_tool_batch():
    srv = ready()
    assert srv.handle({"jsonrpc": "2.0", "id": 4, "method": "resources/list"})["error"]["code"] == -32601
    assert srv.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "write_note"}})["error"]["code"] == -32602
    assert srv.handle({"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": []})["error"]["code"] == -32602
    for arguments in ({"lecture_id": "a1", "extra": 1}, {}, {"lecture_id": 7}, {"lecture_id": "a1", "from_ms": -1},
                      {"lecture_id": "a1", "from_ms": True}, "a1"):
        reply = srv.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                            "params": {"name": "get_transcript", "arguments": arguments}})
        assert reply["error"]["code"] == -32602, arguments
    bad_date = srv.handle({"jsonrpc": "2.0", "id": 8, "method": "tools/call",
                           "params": {"name": "list_calendar", "arguments": {"from_date": "2026-02-30"}}})
    assert bad_date["error"]["code"] == -32602
    assert srv.handle([INIT])["error"]["code"] == -32600  # batches are not supported
    assert srv.handle({"id": 9, "method": "ping"})["error"]["code"] == -32600  # no "jsonrpc": "2.0"
    assert srv.handle({"jsonrpc": "2.0", "id": {"x": 1}, "method": "ping"})["error"]["code"] == -32600


def test_MCP_PROTO_006_invalid_json_gets_a_parse_error_with_null_id_and_the_stream_goes_on():
    replies = run_lines([b"{not json\n", b"\n", json.dumps(INIT).encode() + b"\n", b"\xff\xfe\n",
                         b'{"jsonrpc": "2.0", "id": 2, "method": "ping"}\r\n'])
    assert replies[0] == {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
    assert replies[1]["id"] == 1 and "result" in replies[1]
    assert replies[2]["error"]["code"] == -32700 and replies[3] == {"jsonrpc": "2.0", "id": 2, "result": {}}


def test_MCP_PROTO_007_a_failing_tool_gives_a_short_message_without_paths_or_traces(monkeypatch, tmp_path):
    root = tmp_path / "학기 26-2"
    build_library(root)
    monkeypatch.setenv("LECTUREHUB_ROOT", str(root))
    settings.set_ai_link(True)

    def broken(connection, gate, arguments):
        raise RuntimeError(r"C:\secret\folder\file.txt failed")

    monkeypatch.setitem(tools.HANDLERS, "list_courses", broken)
    reply = ready().handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "list_courses"}})
    result = reply["result"]
    assert result["isError"] is True and json.loads(result["content"][0]["text"]) == {"error": server.FAILED}
    assert "secret" not in json.dumps(reply) and "Traceback" not in json.dumps(reply)


def test_MCP_PROTO_008_result_is_text_content_json_with_korean_kept(monkeypatch, tmp_path):
    root = tmp_path / "학기 26-2"
    build_library(root)
    monkeypatch.setenv("LECTUREHUB_ROOT", str(root))
    settings.set_ai_link(True)
    replies = run_lines([json.dumps(INIT).encode() + b"\n", json.dumps(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "search", "arguments": {"query": "마찰손실"}}}, ensure_ascii=False).encode("utf-8") + b"\n"])
    result = replies[1]["result"]
    assert result["isError"] is False and result["content"][0]["type"] == "text"
    assert "마찰손실" in result["content"][0]["text"]  # ensure_ascii=False: Korean stays readable
    assert json.loads(result["content"][0]["text"])["count"] == 2


def test_MCP_PROTO_009_without_a_data_folder_or_database_the_message_says_what_to_do(monkeypatch, tmp_path):
    settings.set_ai_link(True)
    reply = ready().handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "list_courses"}})
    assert json.loads(reply["result"]["content"][0]["text"]) == {"error": server.access.NO_ROOT}
    monkeypatch.setenv("LECTUREHUB_ROOT", str(tmp_path / "빈 학기"))
    reply = ready().handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "list_courses"}})
    assert json.loads(reply["result"]["content"][0]["text"]) == {"error": server.access.NO_DATABASE}
    assert not (tmp_path / "빈 학기").exists()  # nothing is created


def _child_env(root: Path) -> dict:
    env = dict(os.environ, LECTUREHUB_ROOT=str(root), PYTHONIOENCODING="cp949")  # a hostile default encoding
    src = str(Path(lecturehub.__file__).resolve().parents[1])
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    return env


def test_MCP_PROTO_010_as_a_child_process_every_stdout_line_is_json_rpc_and_korean_round_trips(tmp_path):
    root = tmp_path / "학기 (합성) 26-2"
    build_library(root)
    settings.set_ai_link(True)  # the child reads the same LECTUREHUB_SETTINGS (conftest)
    messages = [INIT, {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                 "params": {"name": "list_lectures", "arguments": {"course": ALLOWED}}},
                {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                 "params": {"name": "search", "arguments": {"query": "다르시"}}},
                {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                 "params": {"name": "get_transcript", "arguments": {"lecture_id": "b1"}}}]
    data = "".join(json.dumps(m, ensure_ascii=False) + "\n" for m in messages).encode("utf-8") + b"not json\n"
    done = subprocess.run([sys.executable, "-m", "lecturehub", "mcp", "serve"], input=data, capture_output=True,
                          timeout=120, env=_child_env(root), check=False)
    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")[-800:]
    lines = done.stdout.decode("utf-8").splitlines()
    replies = [json.loads(line) for line in lines]  # every line parses: nothing else reached stdout
    assert [r["id"] for r in replies] == [1, 2, 3, 4, 5, None]
    assert all(r["jsonrpc"] == "2.0" for r in replies)
    lectures = json.loads(replies[2]["result"]["content"][0]["text"])
    assert lectures["lectures"][0]["course"] == ALLOWED  # Korean came back through the pipe unchanged
    found = json.loads(replies[3]["result"]["content"][0]["text"])
    assert any("다르시 바이스바흐" in r["text"] for r in found["results"])
    assert replies[4]["result"]["isError"] and MARKER not in done.stdout.decode("utf-8")
    assert HIDDEN not in done.stdout.decode("utf-8")
    assert not any(p.name.endswith(("-wal", "-shm")) for p in (root / "_lecturehub").iterdir())


def test_MCP_PROTO_011_the_child_exits_cleanly_when_input_closes_at_once(tmp_path):
    done = subprocess.run([sys.executable, "-m", "lecturehub", "mcp", "serve"], input=b"", capture_output=True,
                          timeout=120, env=_child_env(tmp_path), check=False)
    assert done.returncode == 0 and done.stdout == b""


def test_MCP_CLI_config_prints_the_three_snippets(tmp_path):
    done = subprocess.run([sys.executable, "-m", "lecturehub", "mcp", "config"], capture_output=True, timeout=120,
                          env=dict(_child_env(tmp_path), PYTHONIOENCODING="utf-8"), check=False)
    text = done.stdout.decode("utf-8")
    assert done.returncode == 0 and '"mcpServers"' in text and "[mcp_servers.lecturehub]" in text
    assert "claude mcp add --scope user lecturehub" in text and "꺼짐" in text


def test_MCP_SELFCHECK_step_starts_the_worker_and_talks_json_rpc(tmp_path, monkeypatch):
    from lecturehub.core import proc, selfcheck
    from lecturehub.index import demo

    root = tmp_path / "예시 학기"
    build_library(root)
    monkeypatch.setattr(demo, "COURSE", ALLOWED)  # the step allows the trial course; here that is this course
    monkeypatch.setattr(proc, "console_python", lambda: sys.executable)
    monkeypatch.setenv("PYTHONPATH", _child_env(root)["PYTHONPATH"])
    detail = selfcheck._ai_link(root)
    assert detail.startswith("도구 13개, 과목 1개")
    assert settings.ai_link()["enabled"]


def test_MCP_PUBLIC_the_link_ships_in_the_public_edition(monkeypatch, tmp_path):
    from lecturehub.core import edition
    from lecturehub.ui.app import Api

    assert not any("mcp" in feature or "link" in feature for feature in edition.PUBLIC_OFF)
    monkeypatch.setenv("LECTUREHUB_EDITION", "public")

    class Window:
        def create_confirmation_dialog(self, title, message):
            return True

    api = Api(object(), type("L", (), {"root": tmp_path})())
    api._main = Window()
    assert api.consent_confirm("ai_app_link", ALLOWED)["yes"]  # not gated by edition
    export = Path(lecturehub.__file__).resolve().parents[2] / "tools" / "export_public.py"
    if export.is_file():  # the owner tree: the export keeps the package (it drops listed files only)
        text = export.read_text(encoding="utf-8")
        assert "lecturehub/mcp" not in text
