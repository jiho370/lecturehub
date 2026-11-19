"""Trial semester: built in a scratch folder, the real pipeline runs and throws out the made-up sentence."""
import shutil
from contextlib import closing
from datetime import datetime

import pytest

from lecturehub.core import db
from lecturehub.index import demo

pytestmark = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg required")


def test_trial_semester_has_a_recording_transcript_slides_and_a_checked_note(tmp_path):
    dest = tmp_path / "체험 학기"
    result = demo.make(dest, now=datetime(2026, 11, 13, 14, 0).astimezone())
    assert result["state"] == "made" and result["rejected"] == 1  # the made-up sentence (b06) is thrown out
    note = open(result["note"], encoding="utf-8").read()
    assert "레이놀즈 수를 항상 4000" not in note and "약 4.08 m" in note and '<li id="fn' in note
    with closing(db.connect(dest / "_lecturehub" / "lecturehub.sqlite")) as c:
        assert c.execute("SELECT count(*) FROM MediaAsset WHERE origin='app_mic'").fetchone()[0] == 1
        assert c.execute("SELECT count(*) FROM Transcript").fetchone()[0] == 1
        assert c.execute("SELECT count(*) FROM Material").fetchone()[0] == 1
        assert c.execute("SELECT count(*) FROM StudyArtifact WHERE kind='note'").fetchone()[0] == 1
    assert demo.make(dest)["state"] == "ready"  # second run reuses it


def test_trial_writes_a_recorder_config_and_leaving_it_replaces_that_config(tmp_path, monkeypatch):
    from lecturehub.core import settings
    from lecturehub.ui import setup
    from lecturehub.ui.library import Library

    monkeypatch.setenv("LECTUREHUB_SETTINGS", str(tmp_path / "app" / "settings.json"))
    monkeypatch.setenv("LECTUREHUB_RECORDER_CONFIG", str(tmp_path / "app" / "recorder.toml"))
    monkeypatch.delenv("LECTUREHUB_ROOT", raising=False)
    made = Library.make_trial()
    assert made["ok"] and settings.get("trial")
    config = (tmp_path / "app" / "recorder.toml").read_text(encoding="utf-8")
    assert "recordings" in config and str(made["root"]).replace("\\", "/") in config.replace("\\\\", "/").replace("\\", "/")
    real = tmp_path / "real"
    real.mkdir()
    done = setup.apply(str(real), "26-2", [{"name": "구조역학", "slots": [{"day": "월", "start": "09:00", "end": "10:15"}]}])
    assert done["ok"] and not settings.get("trial")
    config = (tmp_path / "app" / "recorder.toml").read_text(encoding="utf-8")
    assert "체험" not in config and str(real / "26-2").replace("\\", "/") in config.replace("\\\\", "/").replace("\\", "/")