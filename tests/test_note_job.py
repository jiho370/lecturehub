"""S4-2: note job with a fake Claude executor (no network). All text is synthetic."""
import json
from contextlib import closing

import pytest

from lecturehub.core import db
from lecturehub.study import corrections, note_job, notes

SEGMENTS = ["오늘은 관수로 마찰손실", "마찰계수는 0.025 정도", "레이놀즈 수 2300 아래는 층류"]


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "학기 (합성)"
    (root / "_lecturehub").mkdir(parents=True)
    with closing(db.connect(root / "_lecturehub" / "lecturehub.sqlite")) as connection:
        db.migrate(connection)
        connection.execute("INSERT INTO Course VALUES ('su','SU','구조역학','su','26-2')")
        connection.execute("INSERT INTO Lecture VALUES ('l2','su','2026-09-22',1,NULL,NULL)")
        connection.execute("INSERT INTO Transcript (id,lecture_id,media_id,engine,role,role_decided_by,status,source_file,"
                           "sha256) VALUES ('m','l2',NULL,'mai','timeline','policy','done','m.txt','m')")
        for seq, text in enumerate(SEGMENTS):
            connection.execute("INSERT INTO TranscriptSegment (transcript_id,seq,start_ms,text,chunk_index) "
                               "VALUES ('m',?,?,?,0)", (seq, seq * 60_000, text))
        connection.execute("INSERT INTO Material (id,course_id,path,sha256,kind,title,page_count) "
                           "VALUES ('w5','su','w5.pdf','w5','pdf','Week 5 합성',8)")
        connection.execute("INSERT INTO MaterialPage (material_id,page_no,text,image_path,terms) "
                           "VALUES ('w5',6,'층류 f = 64/Re (합성)',NULL,'[]')")
        connection.execute("INSERT INTO MaterialMatch (lecture_id,material_id,page_from,page_to,confidence_class,"
                           "decided_by) VALUES ('l2','w5',5,7,'medium','heuristic')")
        connection.commit()
        yield root, connection


def fake(blocks):
    seen = {}

    def executor(prompt_text, schema):
        seen["prompt"], seen["schema"] = prompt_text, schema
        return {"blocks": blocks, "model": "fake", "cost_usd": 0}
    return executor, seen


BLOCKS = [
    {"key": "b01", "kind": "heading", "text": "관수로 마찰손실"},
    {"key": "b02", "kind": "paragraph", "text": "마찰계수는 0.026 정도", "cites": [{"t": "m", "s": 1}]},
    {"key": "b03", "kind": "item", "text": "층류 f = 64/Re", "cites": [{"m": "w5", "p": 6}]},
    {"key": "b04", "kind": "paragraph", "text": "근거 없는 문장"},
]


def test_note_job_prompt_carries_ids_corrections_and_linked_pages(library):
    root, connection = library
    at = SEGMENTS[1].index("0.025")
    corrections.add(connection, "m", 1, at, at + 5, "0.026")  # corrected text goes to Claude, raw stays
    connection.commit()
    executor, seen = fake(BLOCKS)
    result = note_job.run(connection, root, "l2", executor=executor)
    assert "[1 | 01:00] 마찰계수는 0.026 정도" in seen["prompt"] and "전사ID m" in seen["prompt"]
    assert "[w5 p6] Week 5 합성" in seen["prompt"] and "64/Re" in seen["prompt"]
    assert seen["schema"] == note_job.SCHEMA
    assert (result["state"], result["blocks"], result["rejected"]) == ("done", 3, 1)
    job = json.loads((root / "_lecturehub" / "jobs" / (result["job"].split("\\")[-1].split("/")[-1])
                      / "state.json").read_text(encoding="utf-8"))
    assert job["state"] == "done"
    assert connection.execute("SELECT state FROM ProcessingJob WHERE kind='note'").fetchone()[0] == "qa_warning"
    assert [b["key"] for b in notes.load(connection, "l2")["blocks"]] == ["b01", "b02", "b03"]


def test_note_job_refuses_while_recording_and_waits_for_user_without_cli(library):
    root, connection = library
    with pytest.raises(RuntimeError, match="녹음 중"):
        note_job.run(connection, root, "l2", executor=fake(BLOCKS)[0], busy=lambda: "20260929_100504_mic")

    def missing(prompt_text, schema):
        raise FileNotFoundError("claude 명령을 찾지 못했습니다")
    result = note_job.run(connection, root, "l2", executor=missing)
    assert result["state"] == "waiting_user" and notes.load(connection, "l2") is None
    assert connection.execute("SELECT state FROM ProcessingJob ORDER BY rowid DESC").fetchone()[0] == "waiting_user"


def test_regenerating_one_block_sends_only_its_neighbourhood_and_keeps_the_rest(library):
    root, connection = library
    note_job.run(connection, root, "l2", executor=fake(BLOCKS)[0])
    notes.edit_block(connection, "l2", "b03", "사용자가 고친 층류 문장 64", "표현")
    connection.commit()
    executor, seen = fake([{"key": "zz", "kind": "paragraph", "text": "다시 쓴 0.025", "cites": [{"t": "m", "s": 1}]}])
    note_job.run(connection, root, "l2", only_key="b02", executor=executor)
    assert "블록 b02(paragraph) 하나만" in seen["prompt"]
    note = notes.load(connection, "l2")
    texts = {b["key"]: b["text"] for b in note["blocks"]}
    assert texts == {"b01": "관수로 마찰손실", "b02": "다시 쓴 0.025", "b03": "사용자가 고친 층류 문장 64"}


def test_cli_import_accepts_a_hand_run_claude_envelope(library):
    from typer.testing import CliRunner

    from lecturehub import cli

    root, connection = library
    folder = note_job.prepare(connection, root, "l2")
    (folder / "output.json").write_text(json.dumps({"type": "result", "result": json.dumps({"blocks": BLOCKS[:2]}),
                                                    "structured_output": None}), encoding="utf-8")
    connection.close()
    cli._register_all()
    result = CliRunner().invoke(cli.app, ["note", "import", str(folder), "--root", str(root)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output.strip().splitlines()[-1])["blocks"] == 2
