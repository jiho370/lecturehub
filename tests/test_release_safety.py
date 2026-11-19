"""Release review 2026-11-16, second bundle: folders, drives and networks that differ from the developer's PC."""
from __future__ import annotations

from pathlib import Path

import pytest

from lecturehub.core import net, recycle_bin, selfcheck
from lecturehub.ui import setup


# ---------- where the semester folder goes
def test_cloud_synced_removable_and_long_folders_get_advice_but_are_not_refused(tmp_path, monkeypatch):
    for name in setup.CLOUD_ENV:
        monkeypatch.delenv(name, raising=False)
    plain = tmp_path / "수업"
    assert setup.folder_notes(str(plain), drive_kind=lambda anchor: 3) == []
    synced = tmp_path / "OneDrive - 합성대학교" / "문서" / "수업"
    assert any("동기화" in note for note in setup.folder_notes(str(synced), drive_kind=lambda anchor: 3))
    assert any("이동식" in note for note in setup.folder_notes(str(plain), drive_kind=lambda anchor: 2))
    assert any("네트워크" in note for note in setup.folder_notes(str(plain), drive_kind=lambda anchor: 4))
    long = tmp_path.joinpath(*["아주 긴 폴더 이름 합성"] * 12)
    assert any("깁니다" in note for note in setup.folder_notes(str(long), drive_kind=lambda anchor: 3))
    assert setup.folder_notes("") == []


@pytest.mark.parametrize("folder", [
    r"G:\내 드라이브\수업",                                   # Google Drive for desktop, stream mode, Korean
    r"G:\My Drive\수업",                                      # stream mode, English
    r"G:\공유 드라이브\합성 팀\수업", r"G:\Shared drives\team\수업",
    r"C:\Users\student\내 드라이브\수업",                      # mirror mode
    r"C:\Users\student\My Drive (student@example.invalid)\수업",  # mirror mode, more than one account
    r"C:\Users\student\Google 드라이브\수업",                  # the client before Drive for desktop, Korean
    r"C:\Users\student\Google Drive\수업", r"C:\Users\student\Dropbox\수업", r"C:\Users\student\iCloudDrive\수업",
])
def test_google_drive_for_desktop_and_the_other_sync_folders_get_the_cloud_note(folder, monkeypatch):
    for name in setup.CLOUD_ENV:
        monkeypatch.delenv(name, raising=False)
    notes = setup.folder_notes(folder, drive_kind=lambda anchor: 3, volume_label=lambda anchor: "")
    assert any("동기화" in note for note in notes), folder


def test_a_google_drive_stream_drive_is_known_by_its_volume_label_whatever_its_folders_are_called(monkeypatch):
    for name in setup.CLOUD_ENV:
        monkeypatch.delenv(name, raising=False)
    asked = []

    def label(anchor):
        asked.append(anchor)
        return "Google Drive"

    other = r"G:\다른 컴퓨터\내 노트북\수업"  # a folder name that is not in the list
    assert any("동기화" in note for note in setup.folder_notes(other, drive_kind=lambda anchor: 3, volume_label=label))
    assert asked == ["G:\\"]
    assert setup.folder_notes(other, drive_kind=lambda anchor: 3, volume_label=lambda anchor: "새 볼륨") == []
    asked.clear()  # a network drive is not asked for its label (the call can hang); it gets the network note
    notes = setup.folder_notes(other, drive_kind=lambda anchor: 4, volume_label=label)
    assert asked == [] and len(notes) == 1 and "네트워크" in notes[0]

    def broken(anchor):
        raise OSError("합성: 읽을 수 없음")

    assert setup.folder_notes(other, drive_kind=lambda anchor: 3, volume_label=broken) == []  # advice never fails


def test_the_cloud_note_says_it_goes_by_the_look_of_the_path(tmp_path, monkeypatch):
    """The test is by folder name, so the text must not state it as a fact (a plain folder may be called "My Drive")."""
    for name in setup.CLOUD_ENV:
        monkeypatch.delenv(name, raising=False)
    note = setup.folder_notes(str(tmp_path / "My Drive" / "수업"), drive_kind=lambda anchor: 3,
                              volume_label=lambda anchor: "")[0]
    assert "보입니다" in note and "Google Drive" in note


def test_onedrive_environment_marks_its_folders_as_synced(tmp_path, monkeypatch):
    base = tmp_path / "클라우드"
    (base / "수업").mkdir(parents=True)
    monkeypatch.setenv("OneDrive", str(base))
    assert any("동기화" in note for note in setup.folder_notes(str(base / "수업"), drive_kind=lambda anchor: 3))


def test_suggested_folder_avoids_a_documents_folder_that_onedrive_took_over(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "Documents").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    for name in setup.CLOUD_ENV:
        monkeypatch.delenv(name, raising=False)
    assert setup.suggested_parent() == home / "Documents" / "LectureHub"
    (home / "Documents").rmdir()
    cloud = tmp_path / "OneDrive"
    (cloud / "문서").mkdir(parents=True)
    monkeypatch.setenv("OneDrive", str(cloud))
    assert setup.suggested_parent() == home / "LectureHub"  # Documents now lives inside OneDrive


# ---------- drives without a Recycle Bin
def test_usb_and_network_drives_have_no_bin_and_unknown_is_treated_as_a_normal_disk(tmp_path):
    assert recycle_bin.has_recycle_bin(tmp_path, drive_type=lambda anchor: 3) is True
    assert recycle_bin.has_recycle_bin(tmp_path, drive_type=lambda anchor: 2) is False
    assert recycle_bin.has_recycle_bin(tmp_path, drive_type=lambda anchor: 4) is False

    def cannot(anchor):
        raise OSError("알 수 없음")

    assert recycle_bin.has_recycle_bin(tmp_path, drive_type=cannot) is True


def test_nothing_is_deleted_and_no_window_is_asked_for_on_a_drive_without_a_bin(tmp_path, monkeypatch):
    victim = tmp_path / "지우면 안 되는 합성 파일.wav"
    victim.write_bytes(b"x")
    monkeypatch.setattr(recycle_bin, "has_recycle_bin", lambda path, drive_type=None: False)
    with pytest.raises(OSError, match="휴지통이 없어") as refused:
        recycle_bin.to_recycle_bin([victim])
    assert victim.is_file()
    # callers can tell this refusal from a failed move: its own type, with the drive it is about
    assert isinstance(refused.value, recycle_bin.NoRecycleBin) and refused.value.drives == [victim.anchor]


def test_one_item_on_a_drive_without_a_bin_stops_the_whole_call_before_anything_moves(tmp_path, monkeypatch):
    usb = tmp_path / "usb (합성)"
    usb.mkdir()
    here, there = tmp_path / "여기.wav", usb / "저기.wav"
    for item in (here, there):
        item.write_bytes(b"x")
    monkeypatch.setattr(recycle_bin, "has_recycle_bin", lambda path, drive_type=None: usb not in Path(path).parents)
    with pytest.raises(recycle_bin.NoRecycleBin):
        recycle_bin.to_recycle_bin([here, there])
    assert here.is_file() and there.is_file()  # not half done: the item that has a bin was not moved either


# ---------- what the features that use the Recycle Bin do on such a drive: nothing is lost, nothing is pretended
def _no_bin(monkeypatch):
    monkeypatch.setattr(recycle_bin, "has_recycle_bin", lambda path, drive_type=None: False)


def _session(root, name, *, mode="loopback"):
    import json

    folder = root / "_lecturehub" / "recordings" / name
    folder.mkdir(parents=True)
    (folder / "master_합성.mp3").write_bytes(f"synthetic {name}".encode())
    (folder / "session.json").write_text(json.dumps({"course": "", "course_source": "unknown", "mode": mode,
                                                     "started_local": "2026-09-29T15:34:47+09:00",
                                                     "started_at": 1790664887000}), encoding="utf-8")
    (folder / "stop.json").write_text(json.dumps({"cause": "user_stop"}), encoding="utf-8")
    (folder / "finalize_1.json").write_text(json.dumps({"master": str(folder / "master_합성.mp3")}), encoding="utf-8")
    return folder


@pytest.fixture
def library(tmp_path, monkeypatch):
    """A synthetic semester with one class on the timetable; yields (root, open connection)."""
    from contextlib import closing

    from lecturehub.core import db
    from lecturehub.index.media import MediaMetadata

    root = tmp_path / "학기 (합성)"
    (root / "_lecturehub").mkdir(parents=True)
    monkeypatch.setattr("lecturehub.index.app_ingest.probe_media",
                        lambda path, ffprobe=None: MediaMetadata(30 * 60_000, "mp3", 48000, 1))
    path = root / "_lecturehub" / "lecturehub.sqlite"
    db.init_db(path)
    with closing(db.connect(path)) as c:
        c.execute("INSERT INTO Course VALUES ('rw','RW','합성역학','rw','26-2')")
        c.execute("INSERT INTO Schedule (course_id,weekday,start,end,id) VALUES ('rw',1,?,?,'s1')",
                  (14 * 3_600_000, 17 * 3_600_000 + 50 * 60_000))
        c.commit()
        yield root, c


def test_throwing_a_recording_away_on_a_drive_without_a_bin_is_refused_and_changes_nothing(library, monkeypatch):
    from lecturehub.index import app_ingest, discard

    root, c = library
    folder = _session(root, "20260929_153447_loopback")
    media_id = app_ingest.link_recording(c, folder)["media_id"]
    stray = _session(root, "20260930_100000_mic", mode="mic")  # never linked: a test run
    c.commit()
    _no_bin(monkeypatch)
    with pytest.raises(recycle_bin.NoRecycleBin, match="파일은 그대로"), c:  # `with c` = what ui/library.py does
        discard.discard(c, root, media_id)
    assert media_id not in discard.discarded_ids(c) and (folder / "master_합성.mp3").is_file()
    with pytest.raises(recycle_bin.NoRecycleBin), c:
        discard.discard_lecture(c, root, c.execute("SELECT lecture_id FROM MediaAsset WHERE id=?",
                                                   (media_id,)).fetchone()[0])
    assert media_id not in discard.discarded_ids(c) and (folder / "master_합성.mp3").is_file()
    with pytest.raises(recycle_bin.NoRecycleBin), c:
        discard.discard_session(c, root, stray.name)
    assert (stray / "master_합성.mp3").is_file()
    assert c.execute("SELECT count(*) FROM Event WHERE type LIKE '%DISCARDED'").fetchone()[0] == 0


def test_the_cleanup_after_the_google_transcript_keeps_the_files_and_reports_why(library, monkeypatch):
    from lecturehub.index import audio_cleanup

    root, c = library
    master = _session(root, "20260929_153447_loopback") / "master_합성.mp3"
    monkeypatch.setattr(audio_cleanup, "options", lambda: {"enabled": True, "days": 7})
    monkeypatch.setattr(audio_cleanup, "candidates", lambda connection, root, now=None, days=7: [
        {"media_id": "m", "lecture_id": "l", "files": [str(master)], "bytes": master.stat().st_size}])
    _no_bin(monkeypatch)
    result = audio_cleanup.run(c, root)
    assert result["recycled"] == 0 and result["bytes"] == 0 and master.is_file()
    assert len(result["errors"]) == 1 and "휴지통이 없어" in result["errors"][0]
    assert c.execute("SELECT count(*) FROM Evidence WHERE kind='recycled_after_transcript'").fetchone()[0] == 0


def test_the_wav_cleanup_command_is_refused_and_records_nothing(library, monkeypatch):
    from lecturehub.index import wav_cleanup

    root, c = library
    wav = root / "_lecturehub" / "recordings" / "합성" / "segment_0001.wav"
    wav.parent.mkdir(parents=True)
    wav.write_bytes(b"RIFF synthetic")
    monkeypatch.setattr(wav_cleanup, "candidates", lambda connection: [
        {"media_id": "m", "ready": True, "wav": [str(wav)], "bytes": wav.stat().st_size}])
    _no_bin(monkeypatch)
    with pytest.raises(recycle_bin.NoRecycleBin), c:  # what lecture_cli's wav-cleanup does
        wav_cleanup.clean(c, trash=recycle_bin.to_recycle_bin)
    assert wav.is_file() and c.execute("SELECT count(*) FROM Event WHERE type='WAV_RECYCLED'").fetchone()[0] == 0


# ---------- the app's own WAV copies after a recording (recorder/compact.py; owner's decision 2026-11-19)
def _finished_session(tmp_path, *, verified=True):
    """A finished synthetic session: 3 one-second segment WAVs, the merged master WAV, the MP3 master and its
    finalize report. No ffmpeg needed for the MP3-only default."""
    import json

    from lecturehub.recorder.segments import SegmentWriter

    folder = tmp_path / "_lecturehub" / "recordings" / "20261109_090000"
    folder.mkdir(parents=True)
    (folder / "session.json").write_text('{"sample_rate":16000}', encoding="utf-8")
    with SegmentWriter(folder, 16000, segment_seconds=1) as writer:
        writer.write(bytes(16000 * 2 * 3))
    (folder / "stop.json").write_text(json.dumps({"cause": "user_stop"}), encoding="utf-8")
    (folder / "master_합성.wav").write_bytes(b"RIFF synthetic")
    (folder / "master_합성.mp3").write_bytes(b"synthetic mp3")
    (folder / "finalize_1.json").write_text(json.dumps({"verified": verified, "master": str(folder / "master_합성.mp3")}),
                                            encoding="utf-8")
    wavs = sorted(p.name for p in folder.glob("*.wav"))
    assert len(wavs) == 4
    return folder, wavs


def _never_called(paths):
    raise AssertionError("the Recycle Bin must not be asked on a drive without one")


def _record(folder):
    import json

    from lecturehub.recorder import compact

    path = folder / compact.COMPACT
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def test_on_a_drive_without_a_bin_the_wav_copies_go_for_good_once_the_mp3_is_verified(tmp_path, monkeypatch):
    """HEAD before 2026-11-19 kept every WAV for good there (state "skipped"); the USB stick filled up."""
    from lecturehub.recorder import compact

    folder, wavs = _finished_session(tmp_path)
    sizes = sum((folder / name).stat().st_size for name in wavs)
    _no_bin(monkeypatch)
    result = compact.compact_session(folder, trash=_never_called)
    assert not list(folder.glob("*.wav"))
    assert (folder / "master_합성.mp3").is_file() and compact.verified_report(folder) is not None
    assert result["state"] == "compacted" and result["permanent"] is True and result["mp3_only"] is True
    assert sorted(result["deleted_permanently"]) == wavs and result["recycled"] == []
    assert result["bytes_freed"] == sizes and "not_deleted" not in result
    record = _record(folder)
    assert sorted(record["deleted_permanently"]) == wavs and record["recycled"] == []
    assert not record.get("masters_recycled") and record["not_deleted"] is None and record["recycle_error"] is None


def test_a_wav_that_cannot_be_deleted_stays_and_is_named(tmp_path, monkeypatch):
    from lecturehub.recorder import compact

    folder, wavs = _finished_session(tmp_path)
    stuck = wavs[0]
    real_unlink = Path.unlink

    def unlink(self, *args, **kwargs):
        if self.name == stuck:
            raise PermissionError(13, "합성: 다른 프로그램이 사용 중")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    _no_bin(monkeypatch)
    result = compact.compact_session(folder, trash=_never_called)
    assert [p.name for p in folder.glob("*.wav")] == [stuck]
    assert result["state"] == "partial" and result["permanent"] is True and result["not_deleted"] == [stuck]
    assert sorted(result["deleted_permanently"]) == wavs[1:] and stuck in result["reason"]
    record = _record(folder)
    assert sorted(record["deleted_permanently"]) == wavs[1:] and list(record["not_deleted"]) == [stuck]
    assert stuck in record["delete_error"]


def test_a_drive_with_a_bin_whose_recycling_fails_deletes_nothing(tmp_path, monkeypatch):
    from lecturehub.recorder import compact

    folder, wavs = _finished_session(tmp_path)
    monkeypatch.setattr(recycle_bin, "has_recycle_bin", lambda path, drive_type=None: True)

    def failing(paths):
        raise OSError("휴지통으로 옮기지 못했습니다(코드 32)")

    result = compact.compact_session(folder, trash=failing)
    assert sorted(p.name for p in folder.glob("*.wav")) == wavs
    assert result["state"] == "skipped" and "permanent" not in result and "코드 32" in result["reason"]
    assert not _record(folder).get("deleted_permanently")


def test_without_a_verified_mp3_nothing_is_deleted_on_a_drive_without_a_bin(tmp_path, monkeypatch):
    from lecturehub.recorder import compact

    folder, wavs = _finished_session(tmp_path, verified=False)
    _no_bin(monkeypatch)
    assert compact.compact_session(folder, trash=_never_called)["state"] == "skipped"
    assert compact.recycle_master_wav(folder, trash=_never_called)["state"] == "skipped"
    assert sorted(p.name for p in folder.glob("*.wav")) == wavs
    assert not _record(folder).get("deleted_permanently")


def test_the_master_wav_while_a_live_job_reads_the_segments(tmp_path, monkeypatch):
    """after_finalize's live-job branch: MP3 only → the master WAV goes for good; with keep_lossless and no verified
    FLAC yet it stays and the drive's refusal is the error (nothing deleted)."""
    from lecturehub.core import settings
    from lecturehub.recorder import compact

    folder, wavs = _finished_session(tmp_path)
    _no_bin(monkeypatch)
    settings.save({"keep_lossless": True})
    with pytest.raises(recycle_bin.NoRecycleBin):
        compact.recycle_master_wav(folder)  # the real _trash: refused before anything is touched
    assert sorted(p.name for p in folder.glob("*.wav")) == wavs
    settings.save({"keep_lossless": False})
    result = compact.recycle_master_wav(folder, trash=_never_called)
    assert result["state"] == "deleted" and result["permanent"] is True
    assert result["deleted_permanently"] == ["master_합성.wav"] and not (folder / "master_합성.wav").exists()
    assert len(list(folder.glob("segment_*.wav"))) == 3  # the live job still reads these
    assert _record(folder)["deleted_permanently"] == ["master_합성.wav"]


def test_the_folder_note_for_a_drive_without_a_bin_says_the_wav_copies_go_for_good(tmp_path):
    for kind in (2, 4):
        notes = setup.folder_notes(str(tmp_path / "수업"), drive_kind=lambda anchor, k=kind: k)
        assert any("휴지통이 없어" in n and "바로 지웁니다" in n for n in notes), kind


# ---------- school and company networks
def test_https_is_checked_against_the_windows_certificate_store(monkeypatch):
    import ssl

    monkeypatch.setattr(net, "_done", False)
    assert net.use_system_certificates() is True
    assert "truststore" in type(ssl.create_default_context()).__module__
    assert net.use_system_certificates() is True  # second call: nothing to do


def test_a_missing_truststore_keeps_the_app_working_with_the_bundled_list(monkeypatch):
    import builtins

    real = builtins.__import__

    def without(name, *args, **kwargs):
        if name == "truststore":
            raise ImportError("합성: 없음")
        return real(name, *args, **kwargs)

    import logging

    from lecturehub.core import applog

    class Keep(logging.Handler):
        def __init__(self):
            super().__init__()
            self.records = []

        def emit(self, record):
            self.records.append(record)

    keep = Keep()
    applog.LOG.addHandler(keep)
    monkeypatch.setattr(net, "_done", False)
    monkeypatch.setattr(builtins, "__import__", without)
    try:
        assert net.use_system_certificates() is False  # never raised
    finally:
        applog.LOG.removeHandler(keep)
    said = [r for r in keep.records if "Windows 인증서 저장소 사용" in r.getMessage()]
    assert len(said) == 1 and said[0].levelno == logging.WARNING  # said to the app's logger, with the reason
    assert said[0].exc_info and issubclass(said[0].exc_info[0], ImportError)
    # that this line also lands in the log *file* is tested with applog.setup in test_release_diagnostics.py


def test_the_libraries_the_app_connects_with_build_their_tls_through_the_windows_store(monkeypatch):
    """core/net.py lists which connections follow the Windows store; this pins the ones a test can see without a
    network. (gRPC, used by Chirp captions, brings its own TLS and is not in this list.)"""
    import ssl
    import urllib.request

    monkeypatch.setattr(net, "_done", False)
    assert net.use_system_certificates() is True

    def through_truststore(context) -> bool:
        return "truststore" in type(context).__module__

    assert through_truststore(ssl.create_default_context())  # what the websockets sync client calls for wss://
    assert through_truststore(urllib.request.HTTPSHandler()._context)  # urllib.request: eTL, update check
    httpx = pytest.importorskip("httpx")
    assert through_truststore(httpx.create_ssl_context())  # Gemini, Groq, class notices
    urllib3_ssl = pytest.importorskip("urllib3.util.ssl_")
    assert through_truststore(urllib3_ssl.create_urllib3_context())  # requests (google-auth's token fetch)
    import inspect

    websockets_client = pytest.importorskip("websockets.sync.client")
    assert "ssl_module.create_default_context()" in inspect.getsource(websockets_client)


def test_self_check_requires_what_a_release_needs():
    assert "truststore" in selfcheck.REQUIRED_MODULES and "pymupdf" in selfcheck.FORBIDDEN_MODULES


def test_the_hidden_window_walk_names_things_the_page_really_has():
    """The self-check walks the real page by element id. A renamed element must fail here, on every machine, and
    not only in the build's hidden-window step."""
    import re
    from pathlib import Path

    web = Path(selfcheck.__file__).resolve().parents[1] / "ui" / "web"
    page, script = (web / "index.html").read_text(encoding="utf-8"), (web / "app.js").read_text(encoding="utf-8")
    walked = " ".join(step["do"] + " " + step["wait"] for step in selfcheck._WALK)
    ids = set(re.findall(r"getElementById\('([A-Za-z0-9_]+)'\)", walked)) | {"courseList"}
    assert ids >= {"lvList", "audio", "htmlTab", "htmlFrame", "settingsBtn"}
    for name in ids:
        assert f'id="{name}"' in page, name
    assert "window.__lecturehubReady = true" in script  # what the walk waits for first
    assert "data-lecture=" in script and "securitypolicyviolation" in script  # what it clicks, what reports a block
    assert 'report(`csp: ' in script  # the prefix the walk treats as a failure
    assert "앱 정보" in script  # the settings screen's last section, which the walk waits for


def test_the_page_policy_blocks_inline_script_and_talks_to_no_network():
    """The page's own security policy, as written. Whether WebView2 enforces it without breaking the window is
    checked by the hidden-window walk of the self-check (build step); this guards the text."""
    import re
    from pathlib import Path

    page = (Path(selfcheck.__file__).resolve().parents[1] / "ui" / "web" / "index.html").read_text(encoding="utf-8")
    found = re.search(r'<meta http-equiv="Content-Security-Policy" content="([^"]+)">', page)
    assert found
    policy = {part.split()[0]: part.split()[1:] for part in found.group(1).split(";") if part.strip()}
    assert policy["default-src"] == ["'none'"] and policy["connect-src"] == ["'none'"]
    assert "'unsafe-inline'" not in policy["script-src"] and "'unsafe-eval'" not in policy["script-src"]
    assert not any(source.startswith("http") for source in policy["script-src"])
    assert policy["object-src"] == ["'none'"] and policy["base-uri"] == ["'none'"]
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", page)  # no inline script in the page itself
    assert not re.search(r"\son[a-z]+\s*=\s*[\"']", page)  # no inline event handler either
