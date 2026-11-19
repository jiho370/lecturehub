"""REC-PERM-001: recording permission gates capture before opening devices or sending audio."""
from types import SimpleNamespace

import pytest

from lecturehub.core import recording_permission as permission


def test_REC_PERM_001_states_persist_without_database(tmp_path):
    root = tmp_path / "학기 (합성 시험)"
    assert permission.check(root, "가상 과목")["warning"]
    for state in permission.STATES:
        permission.set_state(root, "가상 과목", state)
        check = permission.for_output(root / "_lecturehub" / "recordings", "가상 과목")
        assert check["allowed"] == (state != "forbidden")
        assert bool(check["warning"]) == (state == "unknown")
    assert not list(root.rglob("*.sqlite"))
    with pytest.raises(ValueError):
        permission.set_state(root, "*", "allowed")
    with pytest.raises(ValueError):
        permission.set_state(root, "가상 과목", "yes")


def test_REC_PERM_001_backend_blocks_recording_and_captions_before_spawn(tmp_path, monkeypatch):
    from lecturehub.ui.backend import Backend

    app = object.__new__(Backend)
    app.root = tmp_path
    app._synthetic_out_refused = lambda: None
    permission.set_state(tmp_path, "가상 과목", "forbidden")
    assert app.start("가상 과목", course_source="user_pick")["ok"] is False
    assert app.captions_start("가상 과목")["ok"] is False
    # No engine/state/device attributes were needed: refusal happened first.


def test_REC_PERM_001_service_and_cli_cannot_bypass_forbidden(tmp_path, monkeypatch):
    from lecturehub.recorder import cli, service

    permission.set_state(tmp_path, "가상 과목", "forbidden")
    out = tmp_path / "_lecturehub" / "recordings"
    with pytest.raises(PermissionError):
        cli._check_permission(out, "가상 과목")
    args = SimpleNamespace(out=out, course="가상 과목")
    events = []
    monkeypatch.setattr(service, "_make_source", lambda _: pytest.fail("device opened"))
    assert service.record(args, lambda kind, **kw: events.append((kind, kw))) == 1
    assert events[0][0] == "error" and "금지" in events[0][1]["message"]
    assert not out.exists()


def test_REC_PERM_001_caption_cli_stops_before_consent_or_network(tmp_path):
    from typer.testing import CliRunner

    from lecturehub.cli import _register_all, app

    _register_all()

    permission.set_state(tmp_path, "가상 과목", "forbidden")
    result = CliRunner().invoke(app, ["captions", "start", "--root", str(tmp_path), "--course", "가상 과목"])
    assert result.exit_code == 1
    assert "금지" in result.output
    assert not (tmp_path / "_lecturehub" / "consent.json").exists()
