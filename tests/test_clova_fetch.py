"""클로바노트 새 노트 받기(2026-11-05): 다운로드 폴더에 녹음·전사를 같은 이름으로 두고, 받은 기록·상태를 남긴다.

실제 Aside·클로바노트는 부르지 않는다(execute를 가짜로 바꾼다). 합성 데이터만.
"""
import hashlib
import json
from contextlib import closing
from datetime import date, datetime, time
from pathlib import Path

import pytest

from lecturehub.core import db
from lecturehub.index import clova_fetch, clova_script, import_audio

NOW = datetime(2026, 11, 5, 17, 0).astimezone()
CLOVA_TXT = Path(__file__).parent / "fixtures" / "transcripts" / "clova.txt"


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "학기 (합성)"
    (root / "_lecturehub").mkdir(parents=True)
    db.init_db(root / "_lecturehub" / "lecturehub.sqlite")
    stage = tmp_path / "다운로드"
    session = tmp_path / "aside 세션"
    (session / "artifacts").mkdir(parents=True)
    with closing(db.connect(root / "_lecturehub" / "lecturehub.sqlite")) as c:
        yield root, c, stage, session


def browser(session, notes, scripts, **extra):
    """Stands in for aside repl: writes the downloads into its session folder and prints like Aside does."""
    def execute(script):
        scripts.append(script)
        out = []
        for item in notes:
            files = []
            for kind, name, data in item.pop("_files", []):
                (session / "artifacts" / name).write_bytes(data)
                files.append({"kind": kind, "path": f"artifacts/{name}"})
            out.append({**item, "files": files})
        result = {"ok": True, "notes": out, "skipped_uploading": 1, "more": False, **extra}
        stdout = (f"\x1b[2maccount: u0 (x@example.com)\nsessionDir: {session}\x1b[0m\n"
                  f"repl > RESULT: {json.dumps(result, ensure_ascii=False)}\n\x1b[2m[ok | 9ms]\x1b[0m\n")
        return stdout, "", 0
    return execute


def note(nid, created, audio=b"synthetic m4a", text=None):
    files = [("audio", f"{nid}.m4a", audio)]
    if text is not None:
        files.append(("text", f"{nid}.txt", text))
    return {"id": nid, "created": created, "duration": "73:28", "title": "새로운 노트", "_files": files}


def test_CLOVA_001_new_note_is_staged_under_one_name_and_remembered(env):
    root, c, stage, session = env
    notes = [note("n1", "2026-11-04T14:03:00", text=CLOVA_TXT.read_bytes())]
    result = clova_fetch.sync(c, root, execute=browser(session, notes, []), folder=stage, now=NOW)
    assert result["ok"] and result["staged"] == 1 and result["notes"][0]["state"] == "staged"
    audio, text = stage / "클로바노트 2026-11-04 14-03.m4a", stage / "클로바노트 2026-11-04 14-03.txt"
    assert audio.read_bytes() == b"synthetic m4a" and text.read_bytes() == CLOVA_TXT.read_bytes()
    assert import_audio.name_stamp(audio.name, date(2026, 11, 5)) == (date(2026, 11, 4), time(14, 3))  # prefilled
    assert clova_fetch.known_ids(root) == ["n1"]
    status = clova_fetch.load_status(root)
    assert status["ok"] and status["staged"] == 1 and "가져오기" in status["message"] and status["files"] == [str(audio)]
    assert sorted(p.name for p in root.iterdir()) == ["_lecturehub"]  # nothing written into the semester folder itself


def test_CLOVA_002_running_again_is_safe(env):
    root, c, stage, session = env
    clova_fetch.sync(c, root, execute=browser(session, [note("n1", "2026-11-04T14:03:00")], []), folder=stage, now=NOW)
    again = []
    # the same recording comes back under another note id (it happened: two downloads of one note)
    result = clova_fetch.sync(c, root, execute=browser(session, [note("n2", "2026-11-04T14:03:00")], again),
                              folder=stage, now=NOW)
    assert '"n1"' in again[0]  # notes already fetched are passed to the browser script to skip
    assert result["duplicate"] == 1 and result["staged"] == 0 and result["message"] == "새 노트가 없습니다"
    assert sorted(p.name for p in stage.iterdir()) == ["클로바노트 2026-11-04 14-03.m4a"]
    # a different note created in the same minute gets its own name, transcript alongside
    other = [note("n3", "2026-11-04T14:03:00", audio=b"other", text=b"t")]
    result = clova_fetch.sync(c, root, execute=browser(session, other, []), folder=stage, now=NOW)
    staged = result["notes"][0]
    assert staged["audio"].endswith("14-03 (2).m4a") and staged["text"].endswith("14-03 (2).txt")


def test_CLOVA_002_recording_already_in_the_app_is_not_staged(env):
    root, c, stage, session = env
    digest = hashlib.sha256(b"synthetic m4a").hexdigest()
    c.execute("INSERT INTO MediaAsset (id, lecture_id, path, sha256, duration_ms, codec, sample_rate, channels, bytes, "
              "origin, device_type, is_master, derived_from, preset_id, recorded_start, partial, loudness_profile, "
              "decided_by, confidence_class) VALUES ('m1',NULL,'x.m4a',?,1,'aac',44100,1,13,'other','phone',1,NULL,NULL,"
              "0,0,'{}','user','exact')", (digest,))
    result = clova_fetch.sync(c, root, execute=browser(session, [note("n1", "2026-11-04T14:03:00")], []), folder=stage,
                              now=NOW)
    assert result["duplicate"] == 1 and result["notes"][0]["why"] == "이미 앱에 있는 녹음"
    assert not stage.exists() or not any(stage.iterdir())


def test_CLOVA_002_browser_download_is_renamed_not_copied(env, tmp_path):
    """Aside saves straight into Downloads ("새로운 노트 (5).m4a"); staging renames it there, so one copy remains."""
    root, c, stage, session = env
    stage.mkdir()
    audio, text = stage / "새로운 노트 (5).m4a", stage / "새로운 노트 (4).txt"
    audio.write_bytes(b"real m4a")
    text.write_bytes(CLOVA_TXT.read_bytes())

    def execute(script):
        result = {"ok": True, "more": False, "skipped_uploading": 0, "notes": [
            {"id": "n1", "created": "2026-11-05T13:00:45", "duration_ms": 3540000, "title": "새로운 노트",
             "files": [{"kind": "text", "path": str(text)}, {"kind": "audio", "path": str(audio)}]}]}
        return f"sessionDir: {session}\nRESULT: " + json.dumps(result, ensure_ascii=False) + "\n", "", 0

    result = clova_fetch.sync(c, root, execute=execute, folder=stage, now=NOW)
    assert result["staged"] == 1 and result["notes"][0]["duration"] == 3540000
    assert sorted(p.name for p in stage.iterdir()) == ["클로바노트 2026-11-05 13-00.m4a", "클로바노트 2026-11-05 13-00.txt"]
    assert (stage / "클로바노트 2026-11-05 13-00.m4a").read_bytes() == b"real m4a"


def test_CLOVA_003_logged_out_stops_and_says_so(env):
    root, c, stage, session = env

    def logged_out(script):
        return f"sessionDir: {session}\nRESULT: " + json.dumps({"ok": False, "login_required": True}) + "\n", "", 0

    result = clova_fetch.sync(c, root, execute=logged_out, folder=stage, now=NOW)
    assert result["login_required"] and not result["ok"] and result["notes"] == []
    status = clova_fetch.load_status(root)
    assert status["login_required"] and "Aside에서 네이버 로그인 필요" in status["message"]
    assert status["checked_at"] == NOW.isoformat(timespec="seconds")


def test_CLOVA_003_browser_errors_are_recorded(env):
    root, c, stage, session = env
    result = clova_fetch.sync(c, root, execute=lambda s: {"ok": False, "error": "Aside가 2분 안에 끝내지 못했습니다"},
                              folder=stage, now=NOW)
    assert not result["ok"] and "2분" in clova_fetch.load_status(root)["message"]
    broken = [{"id": "n9", "created": "2026-11-04T09:00:00", "error": "다운로드 버튼을 찾지 못함"}]
    missing = clova_fetch.sync(c, root, execute=browser(session, broken, []), folder=stage, now=NOW)
    assert missing["failed"] == 1 and missing["notes"][0]["why"] == "다운로드 버튼을 찾지 못함"
    assert clova_fetch.known_ids(root) == []  # a failed note is tried again next time


def test_CLOVA_004_script_only_logs_in_reads_and_downloads():
    script = clova_script.build(["n1", "n2"])
    line, rest = script.split("\n", 1)
    assert rest == "exit\n" and 'new Set(["n1", "n2"])' in line and "__KNOWN__" not in line
    assert "클로바노트에서 노트 하나를" not in line  # full-line comments are dropped (node --check below proves the rest)
    # login: only clicks after the password manager filled both fields; never types; stops elsewhere
    assert line.index("cnFilled") < line.index("name: '로그인', exact: true")
    assert line.index("cnFilled") < line.index("loginStay") < line.index("name: '로그인', exact: true")  # 로그인 상태 유지
    assert line.index("login_required: true") > line.index("nid\\.naver\\.com")
    assert not any(word in line for word in (".fill(", ".type(", "keyboard", "insertText", ".value ="))
    # Clova: only the download menu items; nothing that deletes, shares, edits or renames
    assert "'음성 기록 다운로드'" in line and "'음성 파일 다운로드'" in line
    assert all(i in line for i in ("radio_down_type_1_txt", "checkbox_time", "checkbox_participant"))  # txt + 시간 + 참석자
    assert "dl.path()" in line and "saveAs" not in line  # Aside saves into Downloads; the app renames it there
    assert not any(word in line for word in ("삭제", "공유", "휴지통", "편집", "이름 변경", "이동", "복사"))
    assert "noteStatus === 'DONE'" in line and "closeTab" in line
    import shutil
    import subprocess

    if shutil.which("node"):
        check = subprocess.run(["node", "--check", "-"], input="(async () => {" + line + "})", capture_output=True,
                               text=True, encoding="utf-8", check=False)
        assert check.returncode == 0, check.stderr


def test_CLOVA_005_cli_reports_and_status(tmp_path):
    from typer.testing import CliRunner

    from lecturehub.cli import _register_all, app

    root = tmp_path / "학기"
    (root / "_lecturehub").mkdir(parents=True)
    db.init_db(root / "_lecturehub" / "lecturehub.sqlite")
    _register_all()
    out = CliRunner().invoke(app, ["clova", "sync", "--root", str(root)])
    assert out.exit_code == 1  # no real Aside under pytest
    assert "Aside CLI" in json.loads(out.output.strip().splitlines()[-1])["message"]
    shown = CliRunner().invoke(app, ["clova", "status", "--root", str(root)])
    assert json.loads(shown.output)["ok"] is False


def test_CLOVA_006_weekly_check_is_due_after_seven_days_or_a_day_after_a_failure(tmp_path):
    from datetime import timedelta

    root = tmp_path / "학기"
    (root / "_lecturehub").mkdir(parents=True)
    assert clova_fetch.due(root, NOW)  # never checked
    clova_fetch._save_status(root, {"checked_at": NOW.isoformat(timespec="seconds"), "ok": True})
    assert not clova_fetch.due(root, NOW + timedelta(days=6, hours=23))
    assert clova_fetch.due(root, NOW + timedelta(days=7))
    clova_fetch._save_status(root, {"checked_at": NOW.isoformat(timespec="seconds"), "ok": False,
                                    "login_required": True})
    assert not clova_fetch.due(root, NOW + timedelta(hours=23)) and clova_fetch.due(root, NOW + timedelta(days=1))


def test_CLOVA_006_app_weekly_check_runs_only_when_due_and_one_at_a_time(tmp_path, monkeypatch):
    from lecturehub.booking import aside_cli
    from lecturehub.core import settings
    from lecturehub.ui import library as library_module
    from lecturehub.ui.app import Api

    root = tmp_path / "학기"
    (root / "_lecturehub").mkdir(parents=True)
    db.init_db(root / "_lecturehub" / "lecturehub.sqlite")
    calls = []
    monkeypatch.setattr(clova_fetch, "sync", lambda c, r, **k: calls.append(k) or {"ok": True, "staged": 2,
                                                                                    "message": "새 노트 2개"})
    lib = library_module.Library(root)
    assert lib.clova_sync(auto=True)["result"] is None and calls == []  # optional feature is off on a new install
    settings.set_lab("clova", True)  # this test exercises the user's explicit opt-in
    monkeypatch.setattr(aside_cli, "find_aside", lambda: None)
    assert lib.clova_sync(auto=True)["result"] is None and calls == []  # no Aside CLI: the timer stays quiet
    monkeypatch.setattr(aside_cli, "find_aside", lambda: "aside.exe")
    monkeypatch.setattr(clova_fetch, "due", lambda r: False)
    assert lib.clova_sync(auto=True)["result"] is None and calls == []  # not due
    monkeypatch.setattr(clova_fetch, "due", lambda r: True)
    assert lib.clova_sync(auto=True)["result"]["staged"] == 2 and calls == [{"limit": 3}]
    with library_module._CLOVA_LOCK:  # the [확인] button while the weekly check runs
        assert lib.clova_sync()["result"]["message"] == "이미 확인하는 중입니다" and len(calls) == 1
    told = []
    monkeypatch.setattr("lecturehub.ui.notify.toast", lambda title, body, icon=None: told.append(title))
    api = Api(object(), library=lib)
    api._clova_tell({"staged": 0, "duplicate": 1})
    api._clova_tell({"staged": 2, "message": "새 노트 2개"})
    api._clova_tell({"ok": False, "login_required": True, "message": "로그인"})
    assert told == ["클로바노트 새 노트 2개를 받았습니다", "클로바노트: Aside에서 네이버 로그인 필요"]
