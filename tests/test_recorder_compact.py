"""P-053 (user decision 2026-11-09): master WAV → Recycle Bin, segment WAVs → one verified FLAC."""
from __future__ import annotations

import hashlib
import json
import random
import shutil
from pathlib import Path

import pytest

from lecturehub.recorder import compact
from lecturehub.recorder.finalize import finalize_session
from lecturehub.recorder.heartbeat import SessionLock
from lecturehub.recorder.segments import SegmentWriter, segment_paths

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg 필요")


@pytest.fixture(autouse=True)
def lossless(request):
    """These cases cover the lossless FLAC path (kept on request); the MP3-only default has its own test."""
    from lecturehub.core import settings

    if "mp3_only" not in request.node.name:
        settings.save({"keep_lossless": True})


def test_mp3_only_default_recycles_the_wavs_without_a_flac(tmp_path):
    folder = _session(tmp_path)
    finalize_session(folder)
    trash = Bin(tmp_path / "bin")
    result = compact.compact_session(folder, trash=trash)
    assert result["state"] == "compacted" and result.get("mp3_only") is True
    assert not (folder / compact.FLAC).exists() and not segment_paths(folder) and not list(folder.glob("master_*.wav"))
    assert compact.verified_report(folder) is not None  # the MP3 master stays


class Bin:
    """Stand-in for the Recycle Bin: moves the files aside and remembers them."""

    def __init__(self, folder: Path, fail: bool = False):
        self.folder, self.fail, self.items = folder, fail, []
        folder.mkdir(exist_ok=True)

    def __call__(self, paths):
        if self.fail:
            raise OSError("휴지통으로 옮기지 못했습니다(합성)")
        for path in paths:
            shutil.move(str(path), str(self.folder / f"{len(self.items)}_{path.name}"))
            self.items.append(path.name)


def _session(root: Path, name: str = "20261109_090000") -> Path:
    folder = root / "_lecturehub" / "recordings" / name
    folder.mkdir(parents=True)
    (folder / "session.json").write_text('{"sample_rate":48000}', encoding="utf-8")
    rng = random.Random(7)
    with SegmentWriter(folder, 48000, segment_seconds=1) as writer:  # 2.5 s of speech-like noise
        writer.write(bytes(rng.randrange(256) if i % 2 == 0 else rng.randrange(8) for i in range(48000 * 5)))
    (folder / "stop.json").write_text(json.dumps({"cause": "user_stop", "certainty": "confirmed"}),
                                      encoding="utf-8")
    finalize_session(folder)
    return folder


def _hashes(folder: Path) -> dict:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in segment_paths(folder)}


def _live_job(root: Path, session: Path, *, closed: bool, job_id: str = "JOB1") -> None:
    job = root / "_lecturehub" / "jobs" / job_id
    job.mkdir(parents=True)
    (job / "state.json").write_text(json.dumps({"job_id": job_id, "live": {"session_dir": str(session),
                                                                           "closed": closed}}), encoding="utf-8")


def test_compact_recycles_wavs_after_verified_flac_and_restore_is_byte_identical(tmp_path):
    session = _session(tmp_path)
    before = _hashes(session)
    mp3 = next(session.glob("master_*.mp3"))
    master_wav = next(session.glob("master_*.wav")).name
    trash = Bin(tmp_path / "bin")

    result = compact.compact_session(session, trash=trash)

    assert result["state"] == "compacted" and result["segments"] == 3
    assert not list(session.glob("*.wav")) and (session / "segments.flac").is_file() and mp3.is_file()
    assert sorted(trash.items) == sorted([*before, master_wav])
    record = json.loads((session / "compact.json").read_text(encoding="utf-8"))
    assert record["samples"] == 120000 and [s["samples"] for s in record["segments"]] == [48000, 48000, 24000]
    assert record["flac_bytes"] < record["wav_bytes"] and result["bytes_freed"] > 0

    restored = compact.restore_segments(session)
    assert restored["segments"] == 3 and _hashes(session) == before


def test_open_live_job_keeps_segments_but_master_wav_goes(tmp_path, monkeypatch):
    monkeypatch.delenv("LECTUREHUB_NO_AUTO_COMPACT")
    session = _session(tmp_path)
    _live_job(tmp_path, session, closed=False)
    trash = Bin(tmp_path / "bin")
    before = _hashes(session)

    result = compact.after_finalize(session, trash=trash, background=False)

    assert result["state"] == "deferred" and result["master"]["state"] == "recycled"
    assert _hashes(session) == before and not list(session.glob("master_*.wav"))
    assert not (session / "segments.flac").exists()
    # the live job itself lets go of the session (closed in memory, not yet saved)
    assert compact.after_finalize(session, trash=trash, ignore_job="JOB1", background=False)["state"] == "compacted"


def test_closed_live_job_does_not_block(tmp_path):
    session = _session(tmp_path)
    _live_job(tmp_path, session, closed=True)
    assert compact.compact_session(session, trash=Bin(tmp_path / "bin"))["state"] == "compacted"


def test_without_verified_compressed_master_nothing_moves(tmp_path):
    session = _session(tmp_path)
    for report in session.glob("finalize_*.json"):
        data = json.loads(report.read_text(encoding="utf-8"))
        report.write_text(json.dumps({**data, "master": data["wav_master"]}), encoding="utf-8")
    names = sorted(p.name for p in session.iterdir())
    result = compact.compact_session(session, trash=Bin(tmp_path / "bin"))
    assert result["state"] == "skipped" and sorted(p.name for p in session.iterdir()) == names


def test_dry_run_changes_nothing(tmp_path):
    session = _session(tmp_path)
    names = sorted(p.name for p in session.iterdir())
    result = compact.compact_session(session, trash=Bin(tmp_path / "bin"), dry_run=True)
    assert result["state"] == "would_compact" and sorted(p.name for p in session.iterdir()) == names


def test_bad_round_trip_keeps_everything(tmp_path, monkeypatch):
    session = _session(tmp_path)
    before = _hashes(session)
    monkeypatch.setattr(compact, "_decode", lambda *a, **k: ("0" * 64, 0))
    with pytest.raises(RuntimeError, match="FLAC"):
        compact.compact_session(session, trash=Bin(tmp_path / "bin"))
    assert _hashes(session) == before and list(session.glob("master_*.wav"))
    assert not list(session.glob("segments.flac*"))


def test_master_wav_that_differs_from_segments_stops_it(tmp_path):
    session = _session(tmp_path)
    master = next(session.glob("master_*.wav"))
    data = bytearray(master.read_bytes())
    data[-1] ^= 0xFF
    master.write_bytes(bytes(data))
    with pytest.raises(RuntimeError, match="병합 WAV"):
        compact.compact_session(session, trash=Bin(tmp_path / "bin"))
    assert len(segment_paths(session)) == 3 and not (session / "segments.flac").exists()


def test_recycle_failure_keeps_wavs_and_next_run_finishes(tmp_path):
    session = _session(tmp_path)
    before = _hashes(session)
    first = compact.compact_session(session, trash=Bin(tmp_path / "bin", fail=True))
    assert first["state"] == "flac_only" and _hashes(session) == before
    assert json.loads((session / "compact.json").read_text(encoding="utf-8"))["recycle_error"]
    second = compact.compact_session(session, trash=Bin(tmp_path / "bin2"))
    assert second["state"] == "compacted" and not list(session.glob("*.wav"))


def _no_bin(monkeypatch):
    from lecturehub.core import recycle_bin

    monkeypatch.setattr(recycle_bin, "has_recycle_bin", lambda path, drive_type=None: False)


def _never_called(paths):
    raise AssertionError("the Recycle Bin must not be asked on a drive without one")


def test_keep_lossless_on_a_drive_without_a_bin_deletes_the_wavs_only_after_the_flac_is_verified(tmp_path,
                                                                                                 monkeypatch):
    """Owner's decision 2026-11-19. Before, the WAVs stayed for good next to a verified FLAC (state flac_only)."""
    session = _session(tmp_path)
    before = _hashes(session)
    wavs = sorted(p.name for p in session.glob("*.wav"))
    _no_bin(monkeypatch)
    result = compact.compact_session(session, trash=_never_called)
    assert result["state"] == "compacted" and result["permanent"] is True and result["recycled"] == []
    assert sorted(result["deleted_permanently"]) == wavs and not list(session.glob("*.wav"))
    assert (session / "segments.flac").is_file() and next(session.glob("master_*.mp3")).is_file()
    record = json.loads((session / "compact.json").read_text(encoding="utf-8"))
    assert sorted(record["deleted_permanently"]) == wavs and record["recycled"] == []
    assert compact.restore_segments(session)["segments"] == 3 and _hashes(session) == before


def test_keep_lossless_on_a_drive_without_a_bin_a_bad_flac_deletes_nothing(tmp_path, monkeypatch):
    session = _session(tmp_path)
    before = _hashes(session)
    monkeypatch.setattr(compact, "_decode", lambda *a, **k: ("0" * 64, 0))
    _no_bin(monkeypatch)
    with pytest.raises(RuntimeError, match="FLAC"):
        compact.compact_session(session, trash=_never_called)
    assert _hashes(session) == before and list(session.glob("master_*.wav"))


def test_after_finalize_never_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("LECTUREHUB_NO_AUTO_COMPACT")
    folder = tmp_path / "_lecturehub" / "recordings" / "broken"
    folder.mkdir(parents=True)
    (folder / "finalize_x.json").write_text("{not json", encoding="utf-8")
    assert compact.after_finalize(folder, background=False)["state"] in ("skipped", "error")


def _recording_now(root: Path, name: str = "20261109_103000_mic") -> SessionLock:
    """Another session that is recording: its lock is held and it has no stop.json."""
    folder = root / "_lecturehub" / "recordings" / name
    folder.mkdir(parents=True)
    lock = SessionLock(folder)
    lock.__enter__()
    return lock


def test_another_recording_defers_everything_and_the_next_run_picks_it_up(tmp_path, monkeypatch):
    """D-059/D-050: no PCM reading, FLAC encoding or recycling while another session records (D-065 [바꾸기])."""
    monkeypatch.delenv("LECTUREHUB_NO_AUTO_COMPACT")
    session = _session(tmp_path)
    before = _hashes(session)
    names = sorted(p.name for p in session.iterdir())
    lock = _recording_now(tmp_path)
    trash = Bin(tmp_path / "bin")
    try:
        assert compact.after_finalize(session, trash=trash, background=False)["state"] == "deferred_recording"
        assert compact.compact_session(session, trash=trash)["state"] == "deferred_recording"
    finally:
        lock.__exit__()
    assert _hashes(session) == before and list(session.glob("master_*.wav")) and trash.items == []
    assert sorted(p.name for p in session.iterdir()) == sorted([*names, "compact.json"])
    assert json.loads((session / "compact.json").read_text(encoding="utf-8"))["pending"] == "recording"
    # the recording ended; the next session that finishes takes the deferred one along
    later = _session(tmp_path, "20261109_120000")
    result = compact.after_finalize(later, trash=trash, background=False)
    assert result["state"] == "compacted" and [p["state"] for p in result["pending"]] == ["compacted"]
    assert not list(session.glob("*.wav")) and (session / "segments.flac").is_file()
    assert json.loads((session / "compact.json").read_text(encoding="utf-8"))["pending"] is None


def test_recording_that_starts_during_the_encode_stops_it(tmp_path, monkeypatch):
    session = _session(tmp_path)
    before = _hashes(session)
    calls = []
    monkeypatch.setattr(compact, "BLOCK", 1024)  # many small blocks, so the encode asks several times
    result = compact.compact_session(session, trash=Bin(tmp_path / "bin"),
                                     busy=lambda: calls.append(1) or len(calls) > 1)
    assert result["state"] == "deferred_recording" and len(calls) >= 2
    assert _hashes(session) == before and list(session.glob("master_*.wav"))
    assert not list(session.glob("segments.flac*"))