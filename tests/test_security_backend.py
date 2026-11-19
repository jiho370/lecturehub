"""Security review 2026-11-05: Recycle Bin flags, zip bombs, eTL token kept on its own host."""
import io
import json
import zipfile
from pathlib import Path

import pytest

from lecturehub.core import edition, recycle_bin
from lecturehub.index import etl_sync, wav_cleanup
if edition.has("assignment"):
    from lecturehub.index import assignment_pack


# ---------- (2) never a silent permanent delete
def _fake_shell(seen, delete=True, aborted=False, code=0):
    def shell(pointer):  # stands in for SHFileOperationW; pointer is ctypes.byref(SHFILEOPSTRUCTW)
        struct = pointer._obj
        seen["flags"], seen["func"], seen["from"] = struct.fFlags, struct.wFunc, struct.pFrom
        if delete:
            for p in seen["paths"]:
                p.unlink()
        struct.fAnyOperationsAborted = aborted
        return code
    return shell


def test_SEC_recycle_flags_ask_before_any_permanent_delete():
    assert wav_cleanup.FOF_WANTNUKEWARNING == 0x4000
    assert wav_cleanup.RECYCLE_FLAGS & 0x4000 and wav_cleanup.RECYCLE_FLAGS & 0x40  # nuke warning + undo
    assert wav_cleanup.RECYCLE_FLAGS == 0x40 | 0x10 | 0x4 | 0x400 | 0x4000


def test_SEC_recycle_passes_the_flags_and_checks_the_bin(tmp_path):
    item = tmp_path / "master.wav"
    item.write_bytes(b"RIFF synthetic")
    seen = {"paths": [item.resolve()]}
    in_bin = lambda drive: [{"original": str(item.resolve()).upper()}]  # case-insensitive match
    wav_cleanup.to_recycle_bin([item], shell=_fake_shell(seen), entries=in_bin)
    assert seen["flags"] == wav_cleanup.RECYCLE_FLAGS and seen["func"] == 3
    assert seen["from"] == str(item.resolve())


def test_SEC_recycle_reports_failure_when_the_item_is_not_in_the_bin(tmp_path):
    item = tmp_path / "huge_session.wav"
    item.write_bytes(b"RIFF synthetic")
    seen = {"paths": [item.resolve()]}
    with pytest.raises(OSError, match="영구 삭제"):  # gone, but not in the Recycle Bin → not success
        wav_cleanup.to_recycle_bin([item], shell=_fake_shell(seen), entries=lambda drive: [],
                                   sleep=lambda s: None)


def test_SEC_recycle_waits_for_a_bin_record_that_shows_up_late(tmp_path):
    # 2026-11-12: 6 of 44 session folders were reported as permanently deleted although Explorer showed
    # them in the bin a moment later; the record appears ~0.1 s after SHFileOperationW returns.
    folder = tmp_path / "20261019_093513_mic"
    folder.mkdir()
    (folder / "segment_000001.flac").write_bytes(b"fLaC")
    def shell(pointer):
        import shutil
        shutil.rmtree(folder)
        return 0

    looks, naps = [], []

    def entries(drive):  # empty on the first two looks, then the folder (stored with a trailing slash)
        looks.append(drive)
        return [] if len(looks) < 3 else [{"original": str(folder.resolve()) + "\\"}]

    wav_cleanup.to_recycle_bin([folder], shell=shell, entries=entries, sleep=naps.append)
    assert len(looks) == 3 and len(naps) == 2


def test_SEC_recycle_still_raises_when_the_record_never_shows_up(tmp_path):
    item = tmp_path / "20261111_100413_mic"
    item.mkdir()
    looks, naps = [], []

    def shell(pointer):
        item.rmdir()
        return 0

    def entries(drive):
        looks.append(drive)
        return [{"original": str(tmp_path / "other_folder")}]

    with pytest.raises(OSError, match="영구 삭제"):
        wav_cleanup.to_recycle_bin([item], shell=shell, entries=entries, sleep=naps.append)
    assert len(looks) == recycle_bin.BIN_CHECK_TRIES and len(naps) == recycle_bin.BIN_CHECK_TRIES - 1


def test_SEC_recycle_reports_failure_when_items_are_left_or_aborted(tmp_path):
    item = tmp_path / "a.wav"
    item.write_bytes(b"RIFF")
    seen = {"paths": [item.resolve()]}
    with pytest.raises(OSError, match="옮기지 못한"):
        wav_cleanup.to_recycle_bin([item], shell=_fake_shell(seen, delete=False), entries=lambda d: [])
    with pytest.raises(OSError, match="옮기지 못했습니다"):  # the user said no to the nuke warning
        wav_cleanup.to_recycle_bin([item], shell=_fake_shell(seen, delete=False, aborted=True),
                                   entries=lambda d: [])


# ---------- (3) zip bombs
def _zip(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return path


@pytest.mark.skipif(not edition.has("assignment"), reason="Assignment helper is excluded from the public edition")
def test_SEC_unzip_caps_each_file_total_and_count(tmp_path):
    archive = _zip(tmp_path / "bomb.zip", {"ok.txt": b"x" * 100, "big.bin": b"\0" * 5000,
                                           "b.txt": b"y" * 600, "c.txt": b"z" * 600})
    out = assignment_pack.unzip(archive, tmp_path / "out", max_total=1000, max_file=1000)
    names = sorted(p.name for p in out)
    assert names == ["b.txt", "ok.txt"]  # big.bin over the per-file cap; c.txt over the total
    assert not (tmp_path / "out" / "big.bin").exists() and not (tmp_path / "out" / "c.txt").exists()
    many = _zip(tmp_path / "many.zip", {f"f{i}.txt": b"a" for i in range(20)})
    assert len(assignment_pack.unzip(many, tmp_path / "many", max_entries=5)) == 5


@pytest.mark.skipif(not edition.has("assignment"), reason="Assignment helper is excluded from the public edition")
def test_SEC_unzip_does_not_trust_a_lying_size_header(tmp_path):
    """A synthetic bomb whose header claims 10 bytes while the stream inflates to 2 MB."""
    path = _zip(tmp_path / "liar.zip", {"bomb.txt": b"\0" * (2 * 1024 * 1024)})
    raw = bytearray(path.read_bytes())
    real = (2 * 1024 * 1024).to_bytes(4, "little")
    fake = (10).to_bytes(4, "little")
    assert raw.count(real) == 2  # local header + central directory
    raw = raw.replace(real, fake)
    path.write_bytes(bytes(raw))
    out = assignment_pack.unzip(path, tmp_path / "out", max_file=1024 * 1024)
    assert out == [] and not (tmp_path / "out" / "bomb.txt").exists()


@pytest.mark.skipif(not edition.has("assignment"), reason="Assignment helper is excluded from the public edition")
def test_SEC_unzip_default_caps():
    assert assignment_pack.ZIP_MAX_TOTAL == 200 * 1024 * 1024
    assert assignment_pack.ZIP_MAX_FILE == 50 * 1024 * 1024


# ---------- (5) eTL token stays on BASE's host
def test_SEC_next_link_to_another_host_is_not_followed():
    calls = []
    pages = {f"{etl_sync.BASE}/api/v1/courses?page=1": ([1], f"{etl_sync.BASE}/api/v1/courses?page=2"),
             f"{etl_sync.BASE}/api/v1/courses?page=2": ([2], "https://evil.example/api/v1/courses?page=3"),
             "https://evil.example/api/v1/courses?page=3": ([3], None)}

    def fetch(url, token):
        calls.append(url)
        return pages[url]

    assert etl_sync._all(fetch, f"{etl_sync.BASE}/api/v1/courses?page=1", "t-synthetic") == [1, 2]
    assert all(c.startswith(etl_sync.BASE) for c in calls)
    assert not etl_sync._same_host("http://myetl.snu.ac.kr/x")  # not https
    assert not etl_sync._same_host("https://myetl.snu.ac.kr.evil.example/x")
    assert not etl_sync._same_host("https://myetl.snu.ac.kr:8443/x")
    assert etl_sync._same_host(f"{etl_sync.BASE}/api/v1/x")


def test_SEC_redirect_to_another_host_drops_the_token():
    import urllib.request

    handler = etl_sync._KeepTokenHome()
    request = urllib.request.Request(f"{etl_sync.BASE}/api/v1/courses",
                                     headers={"Authorization": "Bearer t-synthetic", "Accept": "application/json"})
    away = handler.redirect_request(request, io.BytesIO(), 302, "Found", {}, "https://files.example/x")
    assert away is not None and not any(k.lower() == "authorization" for k in away.headers)
    home = handler.redirect_request(request, io.BytesIO(), 302, "Found", {}, f"{etl_sync.BASE}/api/v1/c2")
    assert home.get_header("Authorization") == "Bearer t-synthetic"


def test_SEC_http_never_sends_the_token_off_host(monkeypatch):
    sent = []

    class Response(io.BytesIO):
        headers = {"Link": ""}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_open(request, timeout=None):
        sent.append((request.full_url, request.get_header("Authorization")))
        return Response(json.dumps([]).encode())

    monkeypatch.setattr(etl_sync._OPENER, "open", fake_open)
    etl_sync._http(f"{etl_sync.BASE}/api/v1/courses", "t-synthetic")
    etl_sync._http("https://evil.example/api", "t-synthetic")
    assert sent == [(f"{etl_sync.BASE}/api/v1/courses", "Bearer t-synthetic"), ("https://evil.example/api", None)]
