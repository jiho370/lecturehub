"""Recordings go to the Recycle Bin (faked) some days after the Google transcript (user decision 2026-11-13).

Synthetic folders and rows only; the real 26-2 library is never touched.
"""
import json
import os
import shutil
from contextlib import closing

import pytest

from lecturehub.core import db, settings
from lecturehub.index import audio_cleanup

DAY = 86400
NOW = 1_800_000_000.0


class FakeBin:
    def __init__(self, folder, fail=False):
        self.folder, self.items, self.fail = folder, [], fail
        folder.mkdir()

    def __call__(self, paths):
        if self.fail:
            raise OSError("bin unavailable")
        for p in paths:
            shutil.move(str(p), str(self.folder / f"{len(self.items)}_{p.name}"))
            self.items.append(p.name)


def _age(path, days):
    stamp = NOW - days * DAY
    os.utime(path, (stamp, stamp))


@pytest.fixture
def lib(tmp_path):
    root = tmp_path / "학기 (합성)"
    rec = root / "_lecturehub" / "recordings"
    files = {}
    for name in ("a", "b", "c", "d"):
        session = rec / f"2026110{ord(name) - 96}_090000_mic"
        session.mkdir(parents=True)
        master = session / f"master_{name}.mp3"
        master.write_bytes(b"synthetic mp3 " * 10)
        files[name] = master
    (files["a"].parent / "segments.flac").write_bytes(b"synthetic flac")
    imported = root / "합성과목" / "녹음.mp3"  # an imported file outside _lecturehub/recordings
    imported.parent.mkdir(parents=True)
    imported.write_bytes(b"imported")
    files["imp"] = imported
    transcripts = root / "_lecturehub" / "transcripts"
    transcripts.mkdir()
    path = root / "_lecturehub" / "lecturehub.sqlite"
    db.init_db(path)
    with closing(db.connect(path)) as c:
        c.execute("INSERT INTO Course VALUES ('co','CO','합성과목','co','26-2')")
        # a: old Google transcript + note → goes; b: transcript too new; c: no note; d: only another engine;
        # imp: imported recording (not an app recording) → stays
        for lid, media, origin, engine, age, note in (
                ("la", "a", "app_mic", "gemini_transcribe", 8, True),
                ("lb", "b", "app_mic", "gemini_transcribe", 3, True),
                ("lc", "c", "app_mic", "gemini_transcribe", 30, False),
                ("ld", "d", "app_mic", "whisper", 30, True),
                ("li", "imp", "other", "gemini_transcribe", 30, True)):
            c.execute("INSERT INTO Lecture VALUES (?,'co','2026-11-01',1,NULL,NULL)", (lid,))
            c.execute("INSERT INTO MediaAsset VALUES (?,?,?,?,1000,'mp3',48000,1,9,?,'laptop',1,NULL,NULL,0,0,'{}',"
                      "'metadata','medium')", (f"m{media}", lid, str(files[media]), f"h{media}", origin))
            source = transcripts / f"{engine}_{media}.txt"
            source.write_text("합성 전사", encoding="utf-8")
            _age(source, age)
            c.execute("INSERT INTO Transcript (id,lecture_id,media_id,engine,role,role_decided_by,status,source_file,"
                      "sha256) VALUES (?,?,?,?,'timeline','policy','done',?,?)",
                      (f"t{media}", lid, f"m{media}", engine, str(source), f"s{media}"))
            if note:
                c.execute("INSERT INTO StudyArtifact VALUES (?,?,?,'note',1,NULL,NULL,'new','{}','app','ok')",
                          (f"n{media}", json.dumps([lid]), str(root / f"note_{media}.html")))
        c.commit()
        yield root, c, files


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(audio_cleanup, "options", lambda: {"enabled": True, "days": 7})


def test_only_old_google_transcripts_with_a_note_are_candidates(lib):
    root, c, files = lib
    found = audio_cleanup.candidates(c, root, now=NOW, days=7)
    assert [item["media_id"] for item in found] == ["ma"]
    assert found[0]["files"] == [str(files["a"]), str(files["a"].parent / "segments.flac")]
    assert found[0]["bytes"] > 0


def test_run_sends_files_to_the_bin_and_notes_it(lib, enabled, tmp_path):
    root, c, files = lib
    fake = FakeBin(tmp_path / "bin")
    result = audio_cleanup.run(c, root, now=NOW, trash=fake)
    assert result["recycled"] == 1 and result["errors"] == [] and result["bytes"] > 0
    assert sorted(fake.items) == ["master_a.mp3", "segments.flac"]
    assert not files["a"].exists() and files["b"].exists() and files["imp"].exists()
    row = [tuple(r) for r in c.execute("SELECT subject_id, kind FROM Evidence WHERE kind = 'recycled_after_transcript'")]
    assert row == [("ma", "recycled_after_transcript")]
    assert audio_cleanup.candidates(c, root, now=NOW, days=7) == []  # nothing left to do: a second run is a no-op


def test_off_does_nothing(lib, monkeypatch, tmp_path):
    root, c, files = lib
    monkeypatch.setattr(audio_cleanup, "options", lambda: {"enabled": False, "days": 7})
    fake = FakeBin(tmp_path / "bin")
    assert audio_cleanup.run(c, root, now=NOW, trash=fake)["state"] == "off"
    assert fake.items == [] and files["a"].exists()


def test_bin_failure_keeps_files_and_reports(lib, enabled, tmp_path):
    root, c, files = lib
    result = audio_cleanup.run(c, root, now=NOW, trash=FakeBin(tmp_path / "bin", fail=True))
    assert result["recycled"] == 0 and len(result["errors"]) == 1 and files["a"].exists()
    assert c.execute("SELECT COUNT(*) FROM Evidence WHERE kind = 'recycled_after_transcript'").fetchone()[0] == 0


def test_settings_defaults_and_validation(monkeypatch):
    monkeypatch.setenv("LECTUREHUB_EDITION", "public")
    assert settings.audio_cleanup() == {"enabled": False, "days": 7}
    monkeypatch.setenv("LECTUREHUB_EDITION", "full")
    assert settings.audio_cleanup()["enabled"] is True
    assert settings.set_audio_cleanup(False, 14) == {"enabled": False, "days": 14}
    with pytest.raises(ValueError):
        settings.set_audio_cleanup(True, 0)
    assert settings.keep_lossless() is False


def test_backend_endpoints_validate(monkeypatch):
    from lecturehub.ui.backend import Backend

    monkeypatch.setenv("LECTUREHUB_EDITION", "full")
    assert Backend.audio_cleanup_set(True, "abc")["ok"] is False
    assert Backend.audio_cleanup_set(True, 400)["ok"] is False
    saved = Backend.audio_cleanup_set(True, "10")
    assert saved["ok"] and saved["days"] == 10
    assert Backend.audio_cleanup_get()["days"] == 10
