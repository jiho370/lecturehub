"""D-086 (SETUP-CODEX): the first-run wizard connects Codex with one button and lets Codex do the first setup.

- the semester folder is made before the timetable step (``setup.prepare``), so the AI app link has somewhere to
  write; the wizard's own save and an AI app's ``set_timetable`` never write over each other silently;
- [Codex에 연결] in the wizard turns on exactly three things behind one native dialog and its one-time token;
- [Codex로 처음 설정하기] opens the user's Codex with the packaged ``first-setup`` skill.

Synthetic data only ("합성과목 가"); no real dialog, Codex process, registry or config outside tmp.
"""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_mcp_approve import answer
from test_mcp_write import call

from lecturehub.core import settings, timetable
from lecturehub.mcp import access, approve, changes, connect, more_writes
from lecturehub.recorder import paths
from lecturehub.ui import setup

PROGRAM = r"C:\합성\LectureHub\python.exe"
COURSE_A, COURSE_B = "합성과목 가", "합성과목 나"


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    monkeypatch.setenv("LECTUREHUB_RECORDER_CONFIG", str(tmp_path / "appdata" / "recorder.toml"))
    monkeypatch.setattr(paths, "SEMESTER_ROOT", None)
    monkeypatch.setattr(connect, "command", lambda: PROGRAM)
    monkeypatch.setattr(connect, "_env", lambda: {})


@pytest.fixture
def parent(tmp_path) -> Path:
    folder = tmp_path / "내 문서 (합성)"
    folder.mkdir()
    return folder


class _Window:
    def __init__(self, answer=True):
        self.answer, self.asked = answer, []

    def create_confirmation_dialog(self, title, message):
        self.asked.append((title, message))
        return self.answer


def _api(backend=None, answer=True):
    from lecturehub.ui.app import Api

    backend = backend or SimpleNamespace(root=Path("."), needs_setup=True, setup_root=None, courses=[], extras=list,
                                         _lock=threading.Lock(), _state={})
    api = Api(backend, None)
    api._main = _Window(answer)
    return api


# ------------------------------------------------------------------ one button, one dialog, three things
def test_SETUP_CODEX_001_the_combined_connect_needs_the_dialogs_token_and_turns_on_exactly_three_things(parent):
    api = _api()
    before_auto = settings.ai_link_auto()
    # straight from page script: nothing happens
    assert api.ai_link_setup_connect()["needs_confirm"] and api.ai_link_setup_connect("made-up")["needs_confirm"]
    assert not connect.codex_path().exists() and settings.ai_link() == {"enabled": False, "write": False}
    q = api.ai_link_setup_confirm()
    title, message = api._main.asked[-1]
    assert q["yes"] and "Codex" in title
    for line in ("1. Codex 설정 파일에 Lecture Hub 항목 넣기", f"파일: {connect.codex_path()}", f"실행할 프로그램: {PROGRAM}",
                 "2. AI 앱 연결: 켬", "3. AI 앱이 고칠 수 있게: 켬"):
        assert line in message, line
    assert "모든 종류가 '묻기'" in message and "직접 부탁한 변경만" in message
    assert "과목 자료를 AI 앱이 읽게 하는 허용은 켜지 않습니다" in message
    assert "설정 › AI › AI 앱 연결" in message and "[연결 해제]" in message
    assert "!" not in message
    assert api.ai_link_connect("codex", q["token"])["needs_confirm"]  # not a token of the settings' [연결]
    q = api.ai_link_setup_confirm()
    done = api.ai_link_setup_connect(q["token"])
    assert done["ok"] and done["config"]["state"] == "connected" and done["enabled"] and done["write"]
    assert settings.ai_link() == {"enabled": True, "write": True}
    assert settings.ai_link_auto() == before_auto and not any(before_auto.values())  # every kind still asks
    assert "[mcp_servers.lecturehub]" in connect.codex_path().read_text(encoding="utf-8")
    assert api.ai_link_setup_connect(q["token"])["needs_confirm"]  # one time only


def test_SETUP_CODEX_002_a_no_or_a_config_that_cannot_be_changed_turns_on_nothing(parent):
    api = _api(answer=False)
    assert api.ai_link_setup_confirm() == {"ok": True, "yes": False}
    assert not connect.codex_path().exists() and settings.ai_link() == {"enabled": False, "write": False}
    # the text shown is bound to the token: what would be written changed -> refused, nothing on
    api = _api()
    q = api.ai_link_setup_confirm()
    connect.codex_path().parent.mkdir(parents=True, exist_ok=True)
    connect.codex_path().write_text("not = [valid toml", encoding="utf-8")
    assert api.ai_link_setup_connect(q["token"])["ok"] is False
    assert settings.ai_link() == {"enabled": False, "write": False}
    assert connect.codex_path().read_text(encoding="utf-8") == "not = [valid toml"


def test_SETUP_CODEX_003_the_dialog_says_when_some_kinds_are_already_automatic(parent):
    settings.set_ai_link_auto("save_note", True)
    api = _api()
    api.ai_link_setup_confirm()
    message = api._main.asked[-1][1]
    assert "이미 '자동'으로 둔 종류 1개는 묻지 않습니다" in message and "모든 종류가 '묻기'" not in message


def test_SETUP_CODEX_004_state_shows_codex_found_and_signed_in_like_the_settings(parent, monkeypatch):
    from lecturehub.core import tools

    monkeypatch.setattr(tools, "status", lambda check_login=False: {"codex": {
        "label": "Codex (ChatGPT 계정)", "found": True, "signed_in": True if check_login else None, "hint": "h",
        "help_url": "https://example.invalid", "path": r"C:\합성\codex.cmd"}})
    api = _api()
    state = api.setup_codex_state(True)
    assert state["tool"]["found"] and state["tool"]["signed_in"] is True and "path" not in state["tool"]
    assert state["config"]["state"] == "off" and state["enabled"] is False and state["write"] is False


# ------------------------------------------------------------------ the folder before the timetable
def test_SETUP_CODEX_010_prepare_makes_the_folder_and_library_and_the_wizard_stays_open(parent):
    from lecturehub.ui.backend import Backend

    made = setup.prepare(str(parent), "26-2", "basic")
    root = parent / "26-2"
    assert made["ok"] and made["root"] == str(root) and made["courses"] == [] and made["term"] == {}
    assert (root / "_lecturehub" / "lecturehub.sqlite").is_file() and not (root / "시간표.csv").exists()
    assert settings.data_root() == root and setup.prepared()["root"] == str(root)
    assert setup.info()["needs_setup"] and setup.info()["prepared"]["term"] == "26-2"
    # the app closed half-way: the next start shows the wizard again, inert like a first run
    backend = Backend(fake_engine=True)
    assert backend.needs_setup and backend.setup_root == root and backend.root == settings.parking_root()
    assert backend.init_info()["needs_setup"]
    assert backend.setup_timetable()["courses"] == []
    # the wizard's own rows become the timetable; the flag goes away
    rows = [{"name": COURSE_A, "slots": [{"day": "월", "start": "09:00", "end": "10:15"}]}]
    done = backend.setup_apply(str(parent), "26-2", rows, "2026-09-01", "2026-12-20", "basic",
                               backend.setup_timetable()["base"])
    assert done["ok"] and timetable.read(root)[0]["name"] == COURSE_A and timetable.load_term(root)["start"] == "2026-09-01"
    assert setup.prepared() is None and settings.get("setup_open") is None and not Backend(fake_engine=True).needs_setup


def test_SETUP_CODEX_011_skipping_everything_ends_like_before(parent):
    """Prepared, nothing from Codex, the typed timetable saved: the same files as the old one-step apply."""
    rows = [{"name": COURSE_A, "slots": [{"day": "화", "start": "10:00", "end": "11:00"}]}, {"name": COURSE_B, "slots": []}]
    state = setup.prepare(str(parent), "26-2", "")
    done = setup.apply(str(parent), "26-2", rows, "", "", "", base=state["base"])
    root = parent / "26-2"
    assert done["ok"] and done["extra"] == [COURSE_B] and (root / COURSE_A).is_dir()
    assert "합성과목 가,합성과목 가,화,10:00,11:00" in (root / "시간표.csv").read_text(encoding="utf-8-sig")
    from lecturehub.recorder.classconfig import load_config

    assert load_config().timetable == root / "시간표.csv" and settings.ai_link() == {"enabled": False, "write": False}
    assert not (root / "_lecturehub" / "term.json").exists()


def test_SETUP_CODEX_013_leaving_the_trial_semester_goes_through_the_same_steps(parent, tmp_path):
    from lecturehub.ui.backend import Backend

    trial = tmp_path / "체험 학기"
    (trial / "_lecturehub").mkdir(parents=True)
    settings.save({"data_root": str(trial), "trial": True})
    backend = Backend(fake_engine=True)
    assert not backend.needs_setup  # the trial semester is open; its bar starts the wizard
    made = backend.setup_prepare(str(parent), "26-2", "basic")
    assert made["ok"] and not settings.get("trial") and settings.data_root() == parent / "26-2"
    rows = [{"name": COURSE_A, "slots": [{"day": "수", "start": "09:00", "end": "10:15"}]}]
    assert backend.setup_apply(str(parent), "26-2", rows, "", "", "basic", made["base"])["ok"]
    assert timetable.read(parent / "26-2")[0]["name"] == COURSE_A


def test_SETUP_CODEX_012_another_folder_than_the_prepared_one_is_refused(parent):
    setup.prepare(str(parent), "26-2", "")
    with pytest.raises(setup.SetupError, match="앞에서 만든 학기 폴더"):
        setup.apply(str(parent), "27-1", [], "", "", "")


# ------------------------------------------------------------------ set_timetable on a first run
def _first_run(parent, monkeypatch) -> Path:
    setup.prepare(str(parent), "26-2", "basic")
    settings.set_ai_link(True)
    settings.set_ai_link_write(True)
    monkeypatch.setenv("LECTUREHUB_EDITION", "full")
    return parent / "26-2"


FIRST = {"mode": "merge", "classes": [{"course": COURSE_A, "days": ["월", "수"], "start": "09:00", "end": "10:15"},
                                       {"course": COURSE_B, "days": ["목"], "start": "13:00", "end": "14:15"}],
         "term_start": "2026-09-01", "term_end": "2026-12-20"}


def test_SETUP_CODEX_020_set_timetable_works_without_a_timetable_file_and_without_term_dates(parent, monkeypatch):
    root = _first_run(parent, monkeypatch)
    fake = answer(monkeypatch, approve.ONCE)
    payload, is_error, _ = call("set_timetable", FIRST)
    assert not is_error, payload
    body = fake.shown[0]["body"].splitlines()
    assert f"넣음: {COURSE_A}: 월 09:00~10:15, 수 09:00~10:15 — 새 과목(폴더를 새로 만듦)" in body
    assert "개강·종강: 정하지 않음(모든 주) → 2026-09-01 ~ 2026-12-20" in body
    assert [c["name"] for c in timetable.read(root)] == [COURSE_A, COURSE_B]
    assert timetable.load_term(root) == {"start": "2026-09-01", "end": "2026-12-20"}
    assert not (root / "_lecturehub" / "backups").exists()  # there was no previous file to keep
    # brand-new courses are not allowed for the AI app automatically
    assert not access.course_allowed(root, COURSE_A) and not access.course_allowed(root, COURSE_B)
    assert payload["new_courses"] == [COURSE_A, COURSE_B] and "보여 줄 과목" in payload["message"]
    # so a second call about them answers like a hidden course: corrections are made in the app (first-setup skill)
    again, is_error, _ = call("set_timetable", {"mode": "merge", "classes": [
        {"course": COURSE_A, "days": ["화"], "start": "09:00", "end": "10:15"}]})
    assert is_error and again == {"error": more_writes.HIDDEN}
    # undo of a first timetable takes the file away again
    assert changes.recent(root)[0]["undoable"]


def test_SETUP_CODEX_021_the_wizard_and_the_tool_never_write_over_each_other_silently(parent, monkeypatch):
    from lecturehub.ui.backend import Backend

    root = _first_run(parent, monkeypatch)
    backend = Backend(fake_engine=True)
    seen = backend.setup_timetable()  # the wizard enters the timetable step
    typed = [{"name": "합성과목 다", "slots": [{"day": "금", "start": "15:00", "end": "16:00"}]}]
    settings.set_ai_link_auto("set_timetable", True)
    assert not call("set_timetable", FIRST)[1]  # Codex puts the timetable in meanwhile
    refused = backend.setup_apply(str(parent), "26-2", typed, "", "", "basic", seen["base"])
    assert refused["ok"] is False and refused["changed"] and refused["error"] == setup.CHANGED
    assert [c["name"] for c in refused["courses"]] == [COURSE_A, COURSE_B] and refused["term"]["start"] == "2026-09-01"
    assert [c["name"] for c in timetable.read(root)] == [COURSE_A, COURSE_B] and setup.prepared() is not None
    # the wizard showed what arrived; the user keeps it and adds one: that whole list is saved, the old file backed up
    rows = [{"name": c["name"], "slots": c["slots"]} for c in refused["courses"]] + typed
    done = backend.setup_apply(str(parent), "26-2", rows, "2026-09-01", "2026-12-20", "basic", refused["base"])
    assert done["ok"], done
    assert [c["name"] for c in timetable.read(root)] == [COURSE_A, COURSE_B, "합성과목 다"]
    assert list((root / "_lecturehub" / "backups").glob("시간표_*.csv"))
    # and the other way round: the tool's dialog is made from the file as it is at that moment, and a file that
    # changed while the dialog was open is refused (approve.CHANGED, test_mcp_more_writes MORE-005)
    settings.set_ai_link_auto("set_timetable", False)

    def wizard_saves():
        timetable.write(root, rows[:1])

    answer(monkeypatch, approve.ONCE, during=wizard_saves)
    payload, is_error, _ = call("set_timetable", {"mode": "merge", "classes": [
        {"course": "합성과목 라", "days": ["토"], "start": "09:00", "end": "10:00"}]})
    assert is_error and payload == {"error": approve.CHANGED}
    assert [c["name"] for c in timetable.read(root)] == [COURSE_A]


def test_SETUP_CODEX_022_the_existing_folder_layout_adds_no_course_folders_at_the_end(tmp_path, monkeypatch):
    folder = tmp_path / "이미 쓰는 26-2"
    folder.mkdir()
    state = setup.prepare(str(folder), "26-2", "existing")
    assert settings.data_root() == folder
    (folder / "시간표.csv").write_text(timetable.to_csv(timetable.clean(
        [{"name": COURSE_A, "slots": [{"day": "월", "start": "09:00", "end": "10:00"}]}])), encoding="utf-8-sig")
    now = setup.timetable_state(folder)
    assert now["base"] != state["base"]
    rows = [{"name": COURSE_B, "slots": [{"day": "화", "start": "09:00", "end": "10:00"}]}]
    assert setup.apply(str(folder), "26-2", rows, "", "", "existing", base=now["base"])["ok"]
    assert [c["name"] for c in timetable.read(folder)] == [COURSE_B] and not (folder / COURSE_B).exists()
    assert list((folder / "_lecturehub" / "backups").glob("시간표_*_처음설정.csv"))


# ------------------------------------------------------------------ [Codex로 처음 설정하기] and its skill
def test_SETUP_CODEX_030_codex_first_setup_needs_the_connection_and_starts_the_skill(parent, monkeypatch):
    from lecturehub.ai.codex import CodexEngine

    monkeypatch.setattr(CodexEngine, "executable", lambda self: r"C:\합성 도구\codex.cmd")
    api = _api(SimpleNamespace(root=Path("."), needs_setup=True, setup_root=parent, courses=[], extras=list,
                               _lock=threading.Lock(), _state={}))
    launched = []
    api._codex_launcher = lambda command, workspace: launched.append((command, workspace))
    assert "먼저 [Codex에 연결]" in api.setup_codex_open()["error"] and launched == []
    settings.set_ai_link(True)
    settings.set_ai_link_write(True)
    assert api.setup_codex_open() == {"ok": True}
    command, workspace = launched[0]
    assert connect.SETUP_PROMPT in command and "mcp_servers.lecturehub.command=" in command
    assert "--sandbox" in command and not connect.codex_path().exists()  # this run only, config.toml untouched
    assert (Path(workspace) / ".agents" / "skills" / "first-setup" / "SKILL.md").is_file()
    monkeypatch.setattr(CodexEngine, "executable", lambda self: None)
    assert "찾지 못했습니다" in api.setup_codex_open()["error"]


def test_SETUP_CODEX_031_the_first_setup_skill():
    from lecturehub.core import agent_workspace

    text = (agent_workspace.SKILLS_SRC / "first-setup" / "SKILL.md").read_text(encoding="utf-8")
    assert "name: first-setup" in text.split("---")[1]
    for needed in ("`set_timetable`", "`set_reminder_settings`", "merge", "한 번에 하나", "지시가 아니다",
                   "비밀번호", "토큰", "학번", "맞다고 한 뒤에만", "eTL", "녹음 허락", "보여 줄 과목", "한 번만"):
        assert needed in text, needed
    for pattern in (r"Users[\\/]+JH", r"대학[\\/]+26-2", r"\b20\d{2}-?\d{5}\b", r"[A-Za-z0-9._%+-]+@"):
        assert not re.search(pattern, text), pattern
    assert "!" not in text.split("---", 2)[2]


def test_SETUP_CODEX_032_page_script_cannot_reach_the_backend_parts():
    """The two switches and the config write sit behind the Api's token check; the page sees no other door."""
    from lecturehub.ui.app import Api

    public = {n for n in dir(Api) if not n.startswith("_")}
    assert {"ai_link_setup_confirm", "ai_link_setup_connect", "setup_codex_state", "setup_codex_open",
            "setup_prepare", "setup_timetable"} <= public
    assert not {"connect_codex", "set_ai_link", "set_ai_link_write"} & public
    assert json.dumps(sorted(public))  # plain names only
