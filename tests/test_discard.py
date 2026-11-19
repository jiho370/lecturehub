"""D-058: throwing away a recording goes to the Recycle Bin (faked here) and hides it everywhere. Synthetic only."""
import hashlib
import json
import shutil
from contextlib import closing

import pytest

from lecturehub.core import db
from lecturehub.index import archive_copy, discard, lectures

MIN = 60_000


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class FakeBin:
    """Stands in for the Windows Recycle Bin: moves paths aside so they can be put back."""

    def __init__(self, folder):
        self.folder, self.moved = folder, {}
        folder.mkdir()

    def __call__(self, paths):
        for p in paths:
            target = self.folder / f"{len(self.moved)}_{p.name}"
            shutil.move(str(p), target)
            self.moved[str(p)] = target

    def restore(self, original):
        shutil.move(str(self.moved.pop(str(original))), original)


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "학기 (합성)"
    rec = root / "_lecturehub" / "recordings"
    mic, pc = rec / "20260929_140120_mic", rec / "20260929_140121_loopback"
    for s in (mic, pc):
        s.mkdir(parents=True)
        (s / "segment_000001.wav").write_bytes(b"synthetic segment")
        (s / "stop.json").write_text("{}", encoding="utf-8")
    master = mic / "master_합성.mp3"
    master.write_bytes(b"synthetic master")
    pair = {"mic": str(mic), "pc": str(pc)}
    for s in (mic, pc):
        (s / "pair.json").write_text(json.dumps(pair), encoding="utf-8")
    derived = root / "_lecturehub" / "derived" / "master_합성_0s-600s.mp3"
    derived.parent.mkdir()
    derived.write_bytes(b"synthetic cut")
    archive = root / "녹음본" / "토질역학" / "26.9.29 토질역학.mp3"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(derived.read_bytes())
    (rec / "20260928_233037_mic").mkdir()
    (rec / "20260928_233037_mic" / "stop.json").write_text("{}", encoding="utf-8")
    path = root / "_lecturehub" / "lecturehub.sqlite"
    db.init_db(path)
    with closing(db.connect(path)) as c:
        c.execute("INSERT INTO Course VALUES ('rw','RW','토질역학','rw','26-2')")
        c.execute("INSERT INTO Lecture VALUES ('l','rw','2026-09-29',1,NULL,NULL)")
        c.execute("INSERT INTO Lecture VALUES ('k','rw','2026-09-22',1,NULL,NULL)")
        c.execute("INSERT INTO MediaAsset VALUES ('m','l',?, 'mh', ?, 'mp3', 48000, 1, 9, 'app_mic', 'laptop', 1, NULL, "
                  "NULL, 0, 0, '{}', 'metadata', 'medium')", (str(master), 60 * MIN))
        c.execute("INSERT INTO MediaAsset VALUES ('d','l',?, 'dh', ?, 'mp3', 48000, 1, 9, 'app_mic', 'laptop', 0, 'm', "
                  "NULL, 0, 0, '{}', 'user', 'exact')", (str(derived), 10 * MIN))
        c.execute("INSERT INTO Evidence VALUES ('e1','MediaAsset','m',NULL,'source_alias',?,NULL,'metadata',1)",
                  (json.dumps({"path": str(archive), "role": archive_copy.ROLE, "sha256": _sha(archive)}),))
        for tid, media, engine in (("g", "m", "gemini_transcribe"), ("x", None, "daglo")):
            c.execute("INSERT INTO Transcript (id,lecture_id,media_id,engine,role,role_decided_by,status,source_file,"
                      "sha256) VALUES (?,'l',?,?,'timeline','policy','done',?,?)", (tid, media, engine, tid, tid))
            c.execute("INSERT INTO TranscriptSegment (transcript_id,seq,start_ms,text,chunk_index) VALUES (?,0,0,?,0)",
                      (tid, f"합성 {engine} 토질역학 문장"))
        c.commit()
        yield root, c, FakeBin(root.parent / "bin"), {"mic": mic, "pc": pc, "archive": archive, "derived": derived}


def test_D058_plan_names_what_goes_to_the_recycle_bin(library):
    root, c, _bin, f = library
    p = discard.plan(c, root, "m")
    assert set(p["trash"]) == {str(f["mic"]), str(f["derived"]), str(f["archive"])}
    assert p["transcripts"] == 1 and p["archive_note"] is None and p["bytes"] > 0
    assert p["pair"] == {"session": f["pc"].name, "media_id": None, "transcripts": 0, "kind": "PC 소리"}


def test_D058_discard_hides_the_recording_everywhere_and_moves_files_to_the_bin(library):
    root, c, fake, f = library
    assert lectures.in_use(c, "l")["transcript_id"] == "g"
    discard.discard(c, root, "m", trash=fake)
    c.commit()
    assert not f["mic"].exists() and not f["archive"].exists() and not f["derived"].exists()
    detail = lectures.lecture_detail(c, "l")
    assert detail["recordings"] == [] and [t["id"] for t in detail["transcripts"]] == ["x"]
    assert lectures.in_use(c, "l")["transcript_id"] == "x"  # the remaining transcript takes over
    assert archive_copy.pending(c) == []
    assert c.execute("SELECT count(*) FROM MediaAsset").fetchone()[0] == 2  # raw rows stay
    from lecturehub.index.app_ingest import register_job_transcript

    out = root / "_lecturehub" / "out" / "합성 전사.txt"
    out.parent.mkdir()
    out.write_text("00:01 \n합성 문장\n", encoding="utf-8")
    result = register_job_transcript(c, {"state": "done", "output": str(out), "audio": str(f["mic"] / "master_합성.mp3"),
                                         "job_id": "j"})
    assert result["state"] == "discarded"
    with pytest.raises(ValueError, match="이미"):
        discard.discard(c, root, "m", trash=fake)


def test_D058_search_leaves_out_a_thrown_away_recordings_transcript(library):
    from lecturehub.ui.library import Library

    root, c, fake, _f = library
    discard.discard(c, root, "m", trash=fake)
    c.commit()
    hits = Library(root).search("토질역학 문장")["results"]
    assert len([r for r in hits if r["kind"] == "transcript"]) == 1  # only the daglo transcript (no media) is left
    assert all("gemini" not in r["text"] for r in hits)


def test_D058_changed_archive_file_is_left_alone(library):
    root, c, fake, f = library
    f["archive"].write_bytes(b"the user edited this file")
    p = discard.plan(c, root, "m")
    assert str(f["archive"]) not in p["trash"] and "바뀌어서" in p["archive_note"]
    discard.discard(c, root, "m", trash=fake)
    assert f["archive"].exists()


def test_D058_with_pair_also_bins_the_unlinked_pc_sound_session(library):
    root, c, fake, f = library
    discard.discard(c, root, "m", with_pair=True, trash=fake)
    c.commit()
    assert not f["pc"].exists()
    assert c.execute("SELECT count(*) FROM Event WHERE type='RECORDING_SESSION_DISCARDED'").fetchone()[0] == 1


def test_D058_restore_takes_the_files_back_out_of_the_recycle_bin(library):
    """User 2026-09-29: [되살리기] must work without restoring by hand in Explorer."""
    root, c, fake, f = library
    discard.discard(c, root, "m", trash=fake)
    c.commit()
    with pytest.raises(ValueError, match="휴지통"):
        discard.restore(c, "m", undelete=lambda paths, not_before_ms: [])  # e.g. the bin was emptied
    asked = []

    def undelete(paths, not_before_ms):
        asked.extend(paths)
        back = [q for q in paths if str(q) in fake.moved]
        for q in back:
            fake.restore(q)
        return back

    result = discard.restore(c, "m", undelete=undelete)
    c.commit()
    assert f["mic"].is_dir() and f["archive"].is_file() and f["derived"].is_file()
    assert set(map(str, asked)) == {str(f["mic"]), str(f["derived"]), str(f["archive"])}
    assert len(result["restored"]) == 3
    assert discard.discarded_ids(c) == set() and lectures.lecture_detail(c, "l")["recordings"]


def test_D058_recycle_bin_records_are_read_and_put_back(tmp_path):
    import struct

    from lecturehub.index import wav_cleanup

    sid = tmp_path / "$Recycle.Bin" / "S-1-5-21-합성"
    sid.mkdir(parents=True)
    original = tmp_path / "녹음 (합성)" / "20260929_194942_loopback"
    for name, when in (("$IAAAAAA", 1000), ("$IBBBBBB", 2000)):  # two deletions of the same path: newest wins
        (sid / ("$R" + name[2:])).mkdir()
        (sid / ("$R" + name[2:]) / "master.mp3").write_bytes(name.encode())
        path = str(original).encode("utf-16-le") + b"\0\0"
        filetime = (when + 11_644_473_600_000) * 10_000
        (sid / name).write_bytes(struct.pack("<qqqi", 2, 0, filetime, len(path) // 2) + path)
    entries = wav_cleanup.recycle_bin_entries(tmp_path)
    assert [e["deleted_ms"] for e in entries] == [2000, 1000]
    assert wav_cleanup.from_recycle_bin([original], not_before_ms=1500, entries=entries) == [original]
    assert (original / "master.mp3").read_bytes() == b"$IBBBBBB" and not (sid / "$IBBBBBB").exists()
    assert wav_cleanup.from_recycle_bin([original], entries=entries) == []  # exists again: left alone


def test_D058_plan_says_which_lecture_and_what_the_pair_holds(library):
    root, c, _fake, f = library
    c.execute("INSERT INTO MediaAsset VALUES ('p','l',?, 'ph', ?, 'mp3', 48000, 1, 9, 'app_loopback', 'laptop', 1, NULL, "
              "NULL, 0, 0, '{}', 'metadata', 'medium')", (str(f["pc"] / "master_pc.mp3"), 60 * MIN))
    (f["pc"] / "master_pc.mp3").write_bytes(b"synthetic pc")
    c.execute("INSERT INTO Transcript (id,lecture_id,media_id,engine,role,role_decided_by,status,source_file,sha256) "
              "VALUES ('pt','l','p','gemini_transcribe','timeline','policy','done','pt','pt')")
    plan = discard.plan(c, root, "m")
    assert plan["lecture"] == "토질역학 2강 (9/29)"
    assert plan["pair"]["kind"] == "PC 소리" and plan["pair"]["transcripts"] == 1
    rows = {s["name"]: s for s in discard.sessions(c, root)}
    assert rows["20260929_140120_mic"]["lecture"] == "토질역학 2강 (9/29)" and rows["20260929_140120_mic"]["segments"] == 1


def test_D058_sessions_list_and_unlinked_session_discard(library):
    root, c, fake, _f = library
    states = {s["name"]: s["state"] for s in discard.sessions(c, root)}
    assert states == {"20260929_140121_loopback": "unlinked", "20260929_140120_mic": "linked",
                      "20260928_233037_mic": "unlinked"}
    with pytest.raises(ValueError, match="회차에 등록"):
        discard.discard_session(c, root, "20260929_140120_mic", trash=fake)
    discard.discard_session(c, root, "20260928_233037_mic", trash=fake)
    assert "20260928_233037_mic" not in {s["name"] for s in discard.sessions(c, root)}


def test_D058_lecture_that_only_had_the_thrown_away_recording_leaves_the_list(library):
    root, c, fake, _f = library
    c.execute("INSERT INTO Lecture VALUES ('only','rw','2026-09-30',1,NULL,NULL)")
    other = root / "_lecturehub" / "recordings" / "20260930_140000_mic"
    other.mkdir()
    (other / "stop.json").write_text("{}", encoding="utf-8")
    (other / "master.mp3").write_bytes(b"synthetic other")
    c.execute("INSERT INTO MediaAsset VALUES ('o','only',?, 'oh', 60000, 'mp3', 48000, 1, 9, 'app_mic', 'laptop', 1, "
              "NULL, NULL, 0, 0, '{}', 'metadata', 'medium')", (str(other / "master.mp3"),))
    c.commit()
    assert "only" in {x["id"] for x in lectures.lecture_list(c)}
    discard.discard(c, root, "o", trash=fake)
    c.commit()
    ids = {x["id"] for x in lectures.lecture_list(c)}
    assert "only" not in ids and {"l", "k"} <= ids


def test_D058_refuses_while_recording(library, monkeypatch):
    root, c, _fake, _f = library
    monkeypatch.setattr(discard, "_recording_now", lambda folder: True)
    with pytest.raises(ValueError, match="녹음 중"):
        discard.plan(c, root, "m")


def test_D058_refuses_while_a_live_job_follows_the_session(library):
    """D-059 live jobs know only the session folder until the master exists."""
    root, c, _fake, f = library
    job = root / "_lecturehub" / "jobs" / "live1"
    job.mkdir(parents=True)
    (job / "state.json").write_text(json.dumps({"state": "running", "audio": None,
                                                "live": {"session_dir": str(f["mic"])}}), encoding="utf-8")
    with pytest.raises(ValueError, match="전사가 진행 중"):
        discard.plan(c, root, "m")


def test_D058_lecture_discard_bins_all_its_recordings_and_numbers_match_the_sidebar(library):
    """User 2026-09-29: "3강 폐기하고 다시 녹음" — one [이 회차 버리기] for the whole lecture."""
    root, c, fake, f = library
    # a later lecture of the same course that only had a recording thrown away earlier: not counted
    c.execute("INSERT INTO Lecture VALUES ('old','rw','2026-09-29',0,NULL,NULL)")
    gone = root / "_lecturehub" / "recordings" / "20260929_090000_mic"
    gone.mkdir()
    (gone / "stop.json").write_text("{}", encoding="utf-8")
    (gone / "master.mp3").write_bytes(b"synthetic gone")
    c.execute("INSERT INTO MediaAsset VALUES ('g0','old',?, 'g0h', 60000, 'mp3', 48000, 1, 9, 'app_mic', 'laptop', 1, "
              "NULL, NULL, 0, 0, '{}', 'metadata', 'medium')", (str(gone / "master.mp3"),))
    discard.discard(c, root, "g0", trash=fake)
    c.commit()
    assert lectures.ordinal(c, "l") == 2 and lectures.lecture_detail(c, "l")["n"] == 2
    what = discard.lecture_plan(c, root, "l")
    assert what["lecture"] == "토질역학 2강 (9/29)" and [p["media_id"] for p in what["recordings"]] == ["m"]
    assert what["transcripts"] == 1 and what["stays"] == {"transcripts": 1, "slides": 0}  # the imported daglo one
    assert what["pairs"] and what["pairs"][0]["session"] == "20260929_140121_loopback"
    discard.discard_lecture(c, root, "l", with_pair=True, trash=fake)
    c.commit()
    assert not f["mic"].exists() and not f["pc"].exists() and not f["archive"].exists()
    assert "m" in discard.discarded_ids(c)
    kinds = [r[0] for r in c.execute("SELECT type FROM Event WHERE subject_type='Lecture' AND subject_id='l'")]
    assert "LECTURE_RECORDINGS_DISCARDED" in kinds
    with pytest.raises(ValueError, match="버릴 녹음이 없습니다"):
        discard.lecture_plan(c, root, "l")
    with pytest.raises(ValueError, match="버릴 녹음이 없습니다"):
        discard.lecture_plan(c, root, "k")  # a lecture without app recordings


def test_D058_lecture_discard_refuses_while_recording(library, monkeypatch):
    root, c, fake, _f = library
    monkeypatch.setattr(discard, "_recording_now", lambda folder: True)
    with pytest.raises(ValueError, match="녹음 중인 세션"):
        discard.discard_lecture(c, root, "l", trash=fake)


def test_a_transcript_is_saved_as_text_without_overwriting(library, tmp_path):
    from lecturehub.ui.app import Api
    from lecturehub.ui.library import Library

    root, _c, _fake, _f = library
    made = Library(root).transcript_export("g")
    assert made["ok"] and made["name"].startswith("토질역학_2026-09-29_") and made["name"].endswith(".txt")
    assert "[0:00:00] 합성 gemini_transcribe 토질역학 문장" in made["text"] and made["text"].startswith("토질역학 · 2026-09-29")
    api, out = Api(object(), library=Library(root)), tmp_path / "받은 파일"
    first = api.transcript_save("g", folder=str(out), reveal=False)
    second = api.transcript_save("g", folder=str(out), reveal=False)
    assert first["ok"] and second["ok"] and first["path"] != second["path"] and second["name"].endswith("(2).txt")
    assert "토질역학 문장" in (out / first["name"]).read_text(encoding="utf-8-sig")
    assert api.transcript_save("없는 전사", folder=str(out), reveal=False)["ok"] is False
    assert len(list(out.iterdir())) == 2


def test_audit3_crashed_session_is_needs_recovery_and_needs_a_second_confirmation(library, monkeypatch):
    root, c, fake, _f = library
    crashed = root / "_lecturehub" / "recordings" / "20261104_100000_mic"
    crashed.mkdir()
    (crashed / "session.json").write_text("{}", encoding="utf-8")  # started, but no stop.json and no master
    (crashed / "segment_000001.wav").write_bytes(b"synthetic class audio")
    states = {s["name"]: s["state"] for s in discard.sessions(c, root)}
    assert states["20261104_100000_mic"] == "needs_recovery"
    assert states["20260928_233037_mic"] == "unlinked"  # a finished test run is still plain junk
    with pytest.raises(ValueError, match="복구 필요"):
        discard.discard_session(c, root, "20261104_100000_mic", trash=fake)
    assert crashed.is_dir()
    calls = []
    monkeypatch.setattr("lecturehub.recorder.recovery.recover_session", lambda p: calls.append(("recover", p)) or {"total_ms": 1})
    monkeypatch.setattr("lecturehub.recorder.finalize.finalize_session", lambda p: calls.append(("finalize", p)) or {"master": "m"})
    assert discard.recover_session(root, "20261104_100000_mic")["finalize"] == {"master": "m"}
    assert calls == [("recover", crashed), ("finalize", crashed)]
    with pytest.raises(ValueError, match="복구가 필요한"):
        discard.recover_session(root, "20260928_233037_mic")
    with pytest.raises(ValueError, match="찾지 못했"):
        discard.recover_session(root, "../20260928_233037_mic")
    discard.discard_session(c, root, "20261104_100000_mic", trash=fake, confirm_crashed=True)
    assert "20261104_100000_mic" not in {s["name"] for s in discard.sessions(c, root)}
