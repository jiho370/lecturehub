from typer.testing import CliRunner

from lecturehub import cli


def test_version_command_runs():
    cli._register_all()
    result = CliRunner().invoke(cli.app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip()
