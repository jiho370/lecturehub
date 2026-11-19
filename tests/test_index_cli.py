"""B-2 CLI wiring with synthetic library rows and no external services."""

import json
from contextlib import closing

from typer.testing import CliRunner

from lecturehub import cli
from lecturehub.core.db import connect, init_db
from lecturehub.core.ids import new_ulid
from lecturehub.core.paths import database_path


def insert(connection, table, **values):
    connection.execute(
        f"INSERT INTO {table} ({','.join(values)}) VALUES ({','.join('?' for _ in values)})",
        tuple(values.values()),
    )


def synthetic_library(root):
    root.mkdir()
    init_db(database_path(root))
    with closing(connect(database_path(root))) as connection, connection:
        course, lecture, media, transcript = [new_ulid() for _ in range(4)]
        insert(connection, "Course", id=course, code="SYN", folder_name="가상 <과목>", abbr="syn", term="26-2")
        insert(connection, "Lecture", id=lecture, course_id=course, date="2026-09-28", session_index=1,
               title="합성 강의")
        insert(connection, "MediaAsset", id=media, lecture_id=lecture, path="synthetic.wav", sha256="media",
               duration_ms=10000, codec="pcm_s16le", sample_rate=16000, channels=1, bytes=320000,
               origin="app_mic", device_type="laptop", is_master=1, recorded_start=0, partial=0,
               loudness_profile="[]", decided_by="user", confidence_class="exact")
        insert(connection, "Transcript", id=transcript, lecture_id=lecture, media_id=media, engine="daglo",
               role="reference", role_decided_by="policy", status="done", sha256="transcript")
        for seq, text in enumerate(["합성 앞 문맥", "부력과 정수압을 비교합니다.", "합성 뒤 문맥"]):
            insert(connection, "TranscriptSegment", id=new_ulid(), transcript_id=transcript,
                   seq=seq, start_ms=seq * 1000, text=text, chunk_index=0)


def test_search_and_status_do_not_create_a_missing_database(tmp_path, monkeypatch):
    """The owner's source checkout: reading commands never make a database. (An installed build and the public
    edition make one, because their users have no command line: tests/test_release_first_run.py.)"""
    from lecturehub.core import db

    monkeypatch.setattr(db, "auto_upgrade_allowed", lambda: False)
    cli._register_all()
    runner = CliRunner()
    for arguments in (["search", "부력"], ["status"]):
        result = runner.invoke(cli.app, [*arguments, "--root", str(tmp_path)])
        assert result.exit_code == 1, result.output
        assert "색인된 DB가 없습니다" in result.output
    assert not (tmp_path / "_lecturehub").exists()


def test_index_cli_with_timetable_but_no_sources_reports_no_denominator(tmp_path):
    (tmp_path / "시간표.csv").write_text(
        "폴더,과목,요일,시작,종료\n합성과목,합성과목,화,10:00,11:50\n", encoding="utf-8",
    )
    cli._register_all()
    result = CliRunner().invoke(cli.app, ["index", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "녹음 → Lecture 매칭률: 대상 없음" in result.output
    assert "전사 → Lecture 매칭률: 대상 없음" in result.output


def test_ingest_005_search_cli_unicode_root_short_and_long_queries(tmp_path):
    """INGEST-005: public search accepts Korean paths, spaces and parentheses."""
    root = tmp_path / "학기 자료 (합성)"
    synthetic_library(root)
    cli._register_all()
    for query in ("부력", "정수압"):
        result = CliRunner().invoke(cli.app, ["search", query, "--root", str(root), "--json"])
        assert result.exit_code == 0, result.output
        rows = json.loads(result.stdout)
        assert len(rows) == 1
        assert rows[0]["engine"] == "daglo"
        assert rows[0]["start_ms"] == 1000
        assert "앞 문맥" in rows[0]["context_before"]
        assert "뒤 문맥" in rows[0]["context_after"]


def test_status_cli_html_and_backlog_use_only_owned_output(tmp_path):
    root = tmp_path / "학기 자료 (합성)"
    synthetic_library(root)
    original = root / "원본.html"
    original.write_text("합성 원본", encoding="utf-8")
    cli._register_all()
    runner = CliRunner()
    result = runner.invoke(cli.app, ["status", "--root", str(root), "--as-of", "2026-09-28", "--html"])
    assert result.exit_code == 0, result.output
    html = (root / "_lecturehub" / "status.html").read_text(encoding="utf-8")
    assert "&lt;과목&gt;" in html
    assert "가상 <과목>" not in html
    assert original.read_text(encoding="utf-8") == "합성 원본"
    assert {item.name for item in root.iterdir()} == {"원본.html", "_lecturehub"}
    result = runner.invoke(cli.app, ["status", "--root", str(root), "--backlog", "--json"])
    assert result.exit_code == 0, result.output
    assert len(json.loads(result.stdout)) == 1


def test_library_root_environment_variable(tmp_path):
    root = tmp_path / "library"
    synthetic_library(root)
    cli._register_all()
    result = CliRunner().invoke(cli.app, ["search", "부력", "--json"], env={"LECTUREHUB_ROOT": str(root)})
    assert result.exit_code == 0, result.output
    assert len(json.loads(result.stdout)) == 1


def test_status_rejects_invalid_date_without_creating_files(tmp_path):
    cli._register_all()
    result = CliRunner().invoke(cli.app, ["status", "--root", str(tmp_path), "--as-of", "2026-99-99"])
    assert result.exit_code == 2, result.output
    assert "YYYY-MM-DD" in result.output
    assert not (tmp_path / "_lecturehub").exists()


def test_search_cli_reports_no_media_in_text_and_json(tmp_path):
    root = tmp_path / "원음 없는 학기"
    synthetic_library(root)
    with closing(connect(database_path(root))) as connection, connection:
        lecture = connection.execute("SELECT id FROM Lecture").fetchone()[0]
        transcript = new_ulid()
        insert(connection, "Transcript", id=transcript, lecture_id=lecture, media_id=None,
               engine="clova", role="reference", role_decided_by="user", status="done", sha256="no-media")
        insert(connection, "TranscriptSegment", id=new_ulid(), transcript_id=transcript, seq=0,
               start_ms=0, text="가상무음원 전사 검색", chunk_index=0)
    cli._register_all()
    runner = CliRunner()
    result = runner.invoke(cli.app, ["search", "가상무음원", "--root", str(root)])
    assert result.exit_code == 0, result.output
    assert "원음 없음 (no_media)" in result.output
    result = runner.invoke(cli.app, ["search", "가상무음원", "--root", str(root), "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)[0]["no_media"] is True


def test_index_refuses_while_recording_unless_forced(tmp_path):
    """Audit #7 (11/5): a full `index <root>` scan waits for the recording to end (AGENTS §2.7)."""
    from lecturehub.recorder.heartbeat import SessionLock

    live = tmp_path / "_lecturehub" / "recordings" / "20261105_100500_mic"
    live.mkdir(parents=True)
    cli._register_all()
    with SessionLock(live):
        refused = CliRunner().invoke(cli.app, ["index", str(tmp_path)])
        assert refused.exit_code == 1 and "녹음 중" in refused.output
        forced = CliRunner().invoke(cli.app, ["index", str(tmp_path), "--force"])
    assert "녹음 중" not in forced.output
