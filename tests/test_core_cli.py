"""B-1 public CLI integration checks with synthetic data only."""

import sqlite3

from typer.testing import CliRunner

from lecturehub import cli


def test_db_init_unicode_root_is_idempotent_and_contained(tmp_path):
    """INGEST-005 (B-1 path subset): Korean, spaces and parentheses are safe."""
    root = tmp_path / "학기 자료 (26-2)"
    root.mkdir()
    original = root / "원본.txt"
    original.write_text("합성 원본", encoding="utf-8")
    cli._register_all()
    runner = CliRunner()
    for _ in range(2):
        result = runner.invoke(cli.app, ["db", "init", "--root", str(root)])
        assert result.exit_code == 0, result.output
        assert str(root / "_lecturehub" / "lecturehub.sqlite") in result.output
    database = root / "_lecturehub" / "lecturehub.sqlite"
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 9
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert original.read_text(encoding="utf-8") == "합성 원본"
    assert {path.name for path in root.iterdir()} == {"원본.txt", "_lecturehub"}


def test_db_init_rejects_file_as_root(tmp_path):
    source = tmp_path / "학기.txt"
    source.write_text("합성", encoding="utf-8")
    cli._register_all()
    result = CliRunner().invoke(cli.app, ["db", "init", "--root", str(source)])
    assert result.exit_code != 0
    assert not (tmp_path / "_lecturehub").exists()


def test_registration_is_idempotent():
    cli._register_all()
    count = (len(cli.app.registered_commands), len(cli.app.registered_groups))
    cli._register_all()
    assert (len(cli.app.registered_commands), len(cli.app.registered_groups)) == count
