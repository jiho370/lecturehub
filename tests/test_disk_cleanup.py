"""Disk cleanup (user decision 2026-11-09, resource review): finished jobs' upload chunks, DB backup retention.

Synthetic temp roots only (never the real semester folder); fake chunk bytes and tiny SQLite files.
"""
import json
import os
import sqlite3
import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lecturehub import cli
from lecturehub.core import db
from lecturehub.transcription import chunk_cleanup, mai
from lecturehub.transcription.job import Job, run

from test_mai import deps as mai_deps
from test_mai import make_job as make_mai_job
from test_mai import result_text
from test_migration_v3 import install_candidate, version_two, version_two_template  # noqa: F401 - pytest fixtures
from test_transcription import FakeClient, make_deps, new_job
from test_transcription import semester  # noqa: F401 - pytest fixture


def chunk_mp3s(job_dir: Path) -> list[str]:
    folder = job_dir / "chunks"
    return sorted(p.name for p in folder.glob("chunk_*.mp3")) if folder.is_dir() else []


def kept_files(job_dir: Path) -> set[str]:
    return {p.relative_to(job_dir).as_posix() for p in job_dir.rglob("*") if p.is_file() and "chunks" not in p.parts}


# ---------------------------------------------------------------- chunks
def test_DISK_001_finished_google_job_prunes_its_chunks_and_keeps_results(semester):  # noqa: F811
    """A finished job: chunk MP3s and the empty chunks folder go; state, raw, parsed and the output stay."""
    root, source = semester
    state = run(new_job(root, source), make_deps(FakeClient()))
    job_dir = Path(state["output"]).parents[1] / "jobs" / state["job_id"]
    assert state["state"] == "done"
    assert not (job_dir / "chunks").exists()
    saved = json.loads((job_dir / "state.json").read_text(encoding="utf-8"))
    assert saved["chunks_pruned_at"] and saved["chunks_pruned_bytes"] > 0 and "chunks_prune_error" not in saved
    assert all(Path(p).is_file() for c in saved["chunks"] for p in c["raw_outputs"])
    assert all(Path(c["parsed_path"]).is_file() for c in saved["chunks"])
    assert Path(saved["output"]).is_file() and source.read_bytes() == b"synthetic audio bytes"


def test_DISK_002_unfinished_job_keeps_its_chunks_for_resume(semester):  # noqa: F811
    """A failed chunk is re-sent from its chunk file by resume, so nothing is deleted until the job is done."""
    from lecturehub.transcription.gemini import TransientError

    root, source = semester
    flaky = [TransientError("503")] * 3
    job = new_job(root, source)
    state = run(job, make_deps(FakeClient({"chunk_02": flaky})))
    assert state["state"] == "failed"
    assert chunk_mp3s(job.dir) == ["chunk_01.mp3", "chunk_02.mp3", "chunk_03.mp3"]
    result = chunk_cleanup.prune_chunks(job.dir)
    assert result["pruned"] is False and result["reason"] and len(chunk_mp3s(job.dir)) == 3
    healthy = FakeClient()
    state = run(Job(job.dir, root), make_deps(healthy))  # resume still finds chunk_02, then prunes
    assert healthy.calls == ["chunk_02"] and state["state"] == "done" and chunk_mp3s(job.dir) == []


def test_DISK_003_prune_failure_never_breaks_the_job(semester, monkeypatch):  # noqa: F811
    root, source = semester

    def broken(*args, **kwargs):
        raise OSError("잠긴 파일(합성)")

    monkeypatch.setattr(chunk_cleanup, "prune_state", broken)
    state = run(new_job(root, source), make_deps(FakeClient()))
    assert state["state"] == "done" and "잠긴 파일" in state["chunks_prune_error"]


def test_DISK_004_mai_chunks_stay_until_every_run_is_imported(semester):  # noqa: F811
    root, source = semester
    job = make_mai_job(root, source, runs=2)
    inbox = job.dir / "inbox"
    inbox.mkdir()
    for entry in job.state["chunks"]:  # run 1 complete, run 2 still missing
        (inbox / mai.result_name(job, entry["index"], 1)).write_text(result_text([(0, 0), (10, 0)]), encoding="utf-8")
    mai.import_results(job, mai_deps(), [inbox])
    assert job.state["state"] == "waiting_agent" and len(chunk_mp3s(job.dir)) == 3
    assert chunk_cleanup.prune_chunks(job.dir)["pruned"] is False
    mai.finish(job, allow_partial=True)  # --partial: run 2 can still be uploaded later
    assert job.state["state"] in ("done", "qa_warning") and len(chunk_mp3s(job.dir)) == 3
    assert chunk_cleanup.finished(job.state)[0] is False
    for entry in job.state["chunks"]:
        (inbox / mai.result_name(job, entry["index"], 2)).write_text(result_text([(0, 0), (10, 0)]), encoding="utf-8")
    mai.import_results(job, mai_deps(), [inbox])
    mai.finish(job)  # all runs in → outputs for both runs, then the chunks go
    assert chunk_mp3s(job.dir) == [] and job.state["chunks_pruned_at"]
    assert (job.dir / "manifest.json").is_file() and len(list((job.dir / "raw").glob("*.txt"))) == 6
    assert len(list((job.dir / "parsed").glob("*.json"))) == 6 and all(Path(p).is_file() for p in job.state["outputs"])


def test_DISK_005_mai_import_of_the_last_run_prunes_automatically(semester):  # noqa: F811
    root, source = semester
    job = make_mai_job(root, source, runs=1)
    inbox = job.dir / "inbox"
    inbox.mkdir()
    for entry in job.state["chunks"]:
        (inbox / mai.result_name(job, entry["index"], 1)).write_text(result_text([(0, 0), (10, 0)]), encoding="utf-8")
    mai.import_results(job, mai_deps(), [inbox])
    assert job.state["state"] in ("done", "qa_warning") and chunk_mp3s(job.dir) == []
    saved = json.loads((job.dir / "state.json").read_text(encoding="utf-8"))
    assert saved["chunks_pruned_at"]


def _old_style_finished_job(root: Path, folder="jobs", name="01OLDJOB", state="done") -> Path:
    """A job finished before automatic pruning existed: chunks still on disk."""
    job_dir = root / "_lecturehub" / folder / name
    (job_dir / "chunks").mkdir(parents=True)
    (job_dir / "parsed").mkdir()
    chunks = []
    for i in range(2):
        (job_dir / "chunks" / f"chunk_{i + 1:02d}.mp3").write_bytes(b"ID3" + b"x" * 100)
        (job_dir / "parsed" / f"chunk_{i + 1:02d}.json").write_text("[]", encoding="utf-8")
        chunks.append({"index": i, "start_ms": i * 1000, "end_ms": (i + 1) * 1000, "state": "done" if state == "done" else "queued",
                       "path": str(job_dir / "chunks" / f"chunk_{i + 1:02d}.mp3"),
                       "parsed_path": str(job_dir / "parsed" / f"chunk_{i + 1:02d}.json"), "qa": [], "raw_outputs": []})
    (job_dir / "chunks" / "notes.txt").write_text("남겨야 할 다른 파일", encoding="utf-8")
    (job_dir / "state.json").write_text(json.dumps({
        "job_id": name, "provider": "gemini_transcribe", "state": state, "chunks": chunks,
        "output": str(root / "_lecturehub" / "out" / "x.txt") if state == "done" else None}, ensure_ascii=False),
        encoding="utf-8")
    return job_dir


def test_DISK_006_cli_dry_run_lists_without_deleting_then_prunes_only_finished(tmp_path):
    root = tmp_path / "학기 (합성)"
    done = _old_style_finished_job(root)
    waiting = _old_style_finished_job(root, folder="jobs_whisper", name="01WAITING", state="waiting_for_quota")
    cli._register_all()
    runner = CliRunner()
    result = runner.invoke(cli.app, ["transcribe", "prune-chunks", "--root", str(root), "--dry-run", "--show-skipped"])
    assert result.exit_code == 0, result.output
    summary = json.loads(result.output)
    assert summary["dry_run"] and summary["jobs"] == 1 and summary["files"] == 2 and summary["bytes"] == 206
    assert summary["skipped"] == 1 and len(chunk_mp3s(done)) == 2 and len(chunk_mp3s(waiting)) == 2
    before = kept_files(done)
    result = runner.invoke(cli.app, ["transcribe", "prune-chunks", "--root", str(root)])
    assert result.exit_code == 0, result.output
    summary = json.loads(result.output)
    assert summary["jobs"] == 1 and summary["files"] == 2 and summary["bytes"] == 206
    assert chunk_mp3s(done) == [] and (done / "chunks" / "notes.txt").is_file()  # only chunk_NN.mp3 is deleted
    assert kept_files(done) == before and len(chunk_mp3s(waiting)) == 2
    assert json.loads((done / "state.json").read_text(encoding="utf-8"))["chunks_pruned_at"]
    assert "chunks_pruned_at" not in json.loads((waiting / "state.json").read_text(encoding="utf-8"))


def test_DISK_007_cli_refuses_while_recording(tmp_path, monkeypatch):
    root = tmp_path / "학기"
    done = _old_style_finished_job(root)
    from lecturehub.transcription import cli as transcription_cli

    monkeypatch.setattr(transcription_cli, "_recording_in_progress", lambda root: "20261109_090000_mic")
    cli._register_all()
    result = CliRunner().invoke(cli.app, ["transcribe", "prune-chunks", "--root", str(root)])
    assert result.exit_code == 1 and "녹음 중" in result.output and len(chunk_mp3s(done)) == 2


# ---------------------------------------------------------------- DB backups
T0 = 1_790_000_000  # 2026-09 (ZIP stores times from 1980 on)


def _sqlite(path: Path, rows: int, mtime: float) -> Path:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE t (x TEXT)")
        connection.executemany("INSERT INTO t VALUES (?)", [("합성 백업 행 " * 20,)] * rows)
    connection.close()
    os.utime(path, (mtime, mtime))
    return path


@pytest.fixture
def backups(tmp_path):
    owned = tmp_path / "학기 (합성)" / "_lecturehub"
    folder = owned / "backup"
    folder.mkdir(parents=True)
    live = _sqlite(owned / "lecturehub.sqlite", 5, T0)
    names = ["lecturehub_before_0003_20260928T144913726632Z_01M3M7VK7YMBJCHT9GJ5MBCQ92.sqlite",
             "lecturehub_before_0005_0006_20260929_132044.sqlite"]
    names += [f"lecturehub_before_task{i:02d}_202610{i + 1:02d}_120000.sqlite" for i in range(8)]
    names += ["lecturehub_20261106_before_mai_sync.sqlite"]
    for n, name in enumerate(names):
        _sqlite(folder / name, 50, T0 + 1_000 + n * 100)
    (folder / (names[0] + "-wal")).write_bytes(b"")  # leftovers of a pre-migration backup stay with it
    (folder / (names[2] + "-shm")).write_bytes(b"s" * 10)  # ...and leave with an old one
    (folder / "moves_20261106_acct.csv").write_text("undo log", encoding="utf-8")
    return folder, live, names


def test_DISK_011_pre_migration_backups_are_told_apart_by_name():
    assert db.is_pre_migration_backup("lecturehub_before_0003_20260928T144913726632Z_01M3M7VK7YMBJCHT9GJ5MBCQ92.sqlite")
    assert db.is_pre_migration_backup("lecturehub_before_0004_20260929T064046.sqlite")
    assert db.is_pre_migration_backup("lecturehub_before_0005_0006_20260929_132044.sqlite")
    for name in ("lecturehub_before_notes_20261002_100330.sqlite", "lecturehub_before_mai1016_20261023_011335.sqlite",
                 "lecturehub_20261106_before_note8.sqlite", "lecturehub_before_res1021_20261021_154951.sqlite"):
        assert not db.is_pre_migration_backup(name)


def test_DISK_012_dry_run_changes_nothing(backups):
    folder, _live, names = backups
    before = sorted(p.name for p in folder.iterdir())
    result = db.prune_backups(folder, dry_run=True)
    assert sorted(p.name for p in folder.iterdir()) == before and result["zip"] is None
    assert result["pre_migration"] == [names[1], names[0]]
    assert result["kept_latest"] == [names[10], names[9], names[8], names[7], names[6]]
    assert result["zipped"] == names[2:6] and result["files"] == 5  # 4 databases + one -shm


def test_DISK_013_old_backups_are_zipped_verified_and_removed(backups):
    folder, live, names = backups
    live_bytes = live.read_bytes()
    sizes = {p.name: p.stat().st_size for p in folder.iterdir()}
    result = db.prune_backups(folder, today="20261109")
    assert result["errors"] == [] and Path(result["zip"]) == folder / "old_backups_20261109.zip"
    left = {p.name for p in folder.iterdir()}
    assert left == {*names[:2], *names[6:], names[0] + "-wal", "moves_20261106_acct.csv", "old_backups_20261109.zip"}
    with zipfile.ZipFile(folder / "old_backups_20261109.zip") as archive:
        assert archive.testzip() is None
        assert {i.filename: i.file_size for i in archive.infolist()} == {
            name: sizes[name] for name in [*names[2:6], names[2] + "-shm"]}
        assert all(i.compress_type == zipfile.ZIP_DEFLATED for i in archive.infolist())
    assert result["bytes_freed"] > 0 and live.read_bytes() == live_bytes
    again = db.prune_backups(folder, today="20261109")  # nothing left to zip; the archive is never rewritten
    assert again["zipped"] == [] and again["zip"] is None


def test_DISK_014_second_zip_on_the_same_day_gets_a_new_name(backups):
    folder, _live, names = backups
    db.prune_backups(folder, today="20261109")
    _sqlite(folder / "lecturehub_before_more_20261109_130000.sqlite", 5, T0 + 9_000)
    result = db.prune_backups(folder, today="20261109")
    assert Path(result["zip"]).name == "old_backups_20261109_2.zip" and result["zipped"] == [names[6]]
    assert (folder / "old_backups_20261109.zip").is_file()


def test_DISK_015_a_bad_zip_deletes_nothing(backups, monkeypatch):
    folder, _live, names = backups
    before = sorted(p.name for p in folder.iterdir())
    monkeypatch.setattr(zipfile.ZipFile, "testzip", lambda self: names[2])
    with pytest.raises(OSError):
        db.prune_backups(folder, today="20261109")
    assert sorted(p.name for p in folder.iterdir()) == before


def test_DISK_016_only_a_backup_folder_is_pruned(backups):
    folder, _live, _names = backups
    with pytest.raises(ValueError):
        db.prune_backups(folder.parent)


def test_DISK_017_cli_prune_backups_dry_run_then_apply_and_refuse_while_recording(backups, monkeypatch):
    folder, _live, names = backups
    root = folder.parent.parent
    cli._register_all()
    runner = CliRunner()
    result = runner.invoke(cli.app, ["db", "prune-backups", "--root", str(root), "--dry-run"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["zipped"] == names[2:6] and not list(folder.glob("*.zip"))
    from lecturehub.core import cli as core_cli

    monkeypatch.setattr(core_cli, "recording_session", lambda directory: "20261109_090000_mic")
    result = runner.invoke(cli.app, ["db", "prune-backups", "--root", str(root)])
    assert result.exit_code == 1 and "녹음 중" in result.output and not list(folder.glob("*.zip"))
    monkeypatch.setattr(core_cli, "recording_session", lambda directory: None)
    result = runner.invoke(cli.app, ["db", "prune-backups", "--root", str(root)])
    assert result.exit_code == 0, result.output
    assert len(list(folder.glob("old_backups_*.zip"))) == 1 and not (folder / names[2]).exists()


def test_DISK_018_db_backup_command_writes_a_checked_backup_then_keeps_the_folder_bounded(backups):
    folder, _live, _names = backups
    root = folder.parent.parent
    cli._register_all()
    result = CliRunner().invoke(cli.app, ["db", "backup", "--root", str(root), "--label", "notes1109"])
    assert result.exit_code == 0, result.output
    made = list(folder.glob("lecturehub_before_notes1109_*.sqlite"))
    assert len(made) == 1 and not db.is_pre_migration_backup(made[0].name)
    with sqlite3.connect(made[0]) as connection:
        assert connection.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 5
    connection.close()
    non_migration = [p for p in folder.glob("*.sqlite") if not db.is_pre_migration_backup(p.name)]
    assert len(non_migration) == db.KEEP_LATEST and made[0] in non_migration
    assert len(list(folder.glob("old_backups_*.zip"))) == 1
    bad = CliRunner().invoke(cli.app, ["db", "backup", "--root", str(root), "--label", "0008"])
    assert bad.exit_code == 1


def test_DISK_019_new_database_makes_no_backup_and_prunes_nothing(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(db, "prune_backups", lambda folder, *a, **k: calls.append(Path(folder)) or {})
    db.init_db(tmp_path / "lecturehub.sqlite")
    assert calls == [] and not (tmp_path / "backup").exists()


@pytest.mark.parametrize("prune_fails", [False, True])
def test_DISK_020_backup_before_a_migration_then_retention(version_two, tmp_path, monkeypatch, prune_fails):  # noqa: F811
    """migrate(): the pre-migration backup is written, then the folder is pruned; a pruning error never breaks it."""
    calls = []

    def prune(folder, *a, **k):
        calls.append(Path(folder))
        if prune_fails:
            raise OSError("합성 실패")
        return {}

    monkeypatch.setattr(db, "prune_backups", prune)
    install_candidate(tmp_path, monkeypatch)
    assert db.migrate(version_two) == [3]
    assert calls == [tmp_path / "backup"] and len(list((tmp_path / "backup").glob("*_before_0003_*.sqlite"))) == 1
