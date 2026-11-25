"""Settings menu follow-ups (owner 2026-11-25): every course's recording permission at once, and the class-notice
banner once the default reader is ChatGPT (Codex) in every edition (D-075). Synthetic course names only."""
from __future__ import annotations

import json

import pytest

from lecturehub.core import recording_permission as permission
from lecturehub.core import settings
from lecturehub.index import class_notices as cn

A, B, C = "합성과목 가", "합성과목 나", "수문학"


class _Window:
    def __init__(self, answer=True):
        self.answer, self.asked = answer, []

    def create_confirmation_dialog(self, title, message):
        self.asked.append((title, message))
        return self.answer


class _Library:
    def __init__(self, root):
        self.root = root


class _Backend:
    recorder = None

    def __init__(self):
        self.stored = []

    def state(self):
        return {"state": "idle"}

    def recording_permission_many(self, courses, state):
        self.stored.append((list(courses), state))
        return {"ok": True, "changed": len(courses)}

    def ai_link_allow_many(self, courses):  # reached only with a valid ai_app_link token
        self.stored.append(("link", list(courses)))
        return {"ok": True}


@pytest.fixture
def api(tmp_path):
    from lecturehub.ui.app import Api

    backend = _Backend()
    made = Api(backend, _Library(tmp_path))
    made._main = _Window(True)
    return made, backend


# ---------------------------------------------------------------- 모든 과목을 [상태]로
def test_PERM_ALL_001_allowed_for_every_course_asks_once_naming_them_with_the_warning(api):
    made, backend = api
    q = made.recording_permission_confirm_many("allowed", [A, B, C])
    title, message = made._main.asked[-1]
    assert q["yes"] and "과목 3개" in title and "허락함" in title
    assert all(f"· {name}" in message for name in (A, B, C))
    assert "교수님이 녹음을 허락했다는 것을 직접 확인했을 때만" in message
    assert permission.WARNING.split(". ", 1)[1] in message  # the existing warning's advice, word for word
    assert made.recording_permission_all("allowed", [C, A, B], q["token"]) == {"ok": True, "changed": 3}  # order free
    assert backend.stored == [([C, A, B], "allowed")]
    assert made.recording_permission_all("allowed", [A, B, C], q["token"])["needs_confirm"]  # one time only
    q = made.recording_permission_confirm_many("syllabus", [A])
    assert "강의계획서에서 녹음을 허용한다" in made._main.asked[-1][1]
    assert made.recording_permission_all("syllabus", [A], q["token"])["ok"] is True


def test_PERM_ALL_002_the_token_is_bound_to_that_state_and_that_list(api):
    made, backend = api
    assert made.recording_permission_all("allowed", [A, B])["needs_confirm"]  # straight from page JS
    assert made.recording_permission_all("allowed", [A, B], "made-up")["ok"] is False
    for state, names in (("allowed", [A, B, C]), ("allowed", [A]), ("syllabus", [A, B])):
        q = made.recording_permission_confirm_many("allowed", [A, B])
        assert made.recording_permission_all(state, names, q["token"])["ok"] is False, (state, names)
    # a consent token (another dialog, another meaning) never changes a recording permission, and the other way round
    consent = made.consent_confirm_many("ai_app_link", [A, B])["token"]
    assert made.recording_permission_all("allowed", [A, B], consent)["ok"] is False
    q = made.recording_permission_confirm_many("allowed", [A, B])
    assert made.ai_link_allow_all([A, B], q["token"])["ok"] is False
    assert backend.stored == []


def test_PERM_ALL_003_unknown_and_forbidden_need_no_dialog_and_bad_requests_are_refused(api):
    made, backend = api
    assert made.recording_permission_all("unknown", [A, B])["ok"] is True
    assert made.recording_permission_all("forbidden", [A])["ok"] is True
    assert made._main.asked == [] and backend.stored == [([A, B], "unknown"), ([A], "forbidden")]
    for state in ("unknown", "forbidden", "", "nope"):  # only the two states that state a permission are asked
        assert made.recording_permission_confirm_many(state, [A])["ok"] is False
    for courses in ([], None, "가", [A, 3], [A, ""], [A, "나\x00쁜"], [A] * 300 + [B] * 300):
        assert made.recording_permission_confirm_many("allowed", courses)["ok"] is False
        assert made.recording_permission_all("unknown", courses)["ok"] is False
    assert made.recording_permission_all("maybe", [A])["ok"] is False and made._main.asked == []
    made._main = _Window(False)
    assert made.recording_permission_confirm_many("allowed", [A]) == {"ok": True, "yes": False}
    made._main = None
    assert made.recording_permission_confirm_many("allowed", [A])["ok"] is False


def test_PERM_ALL_004_one_write_all_or_nothing_and_only_listed_courses(tmp_path, monkeypatch):
    from lecturehub.ui.backend import Backend

    root = tmp_path / "학기 (합성)"
    (root / "_lecturehub").mkdir(parents=True)
    permission.set_state(root, C, "forbidden")
    with pytest.raises(ValueError):
        permission.set_many(root, [A, "*"], "allowed")
    assert permission.load(root) == {C: "forbidden"}  # nothing changed
    rows = permission.set_many(root, [A, B], "syllabus")
    assert [r["state"] for r in rows] == ["syllabus", "syllabus"] and permission.load(root)[C] == "forbidden"

    backend = Backend.__new__(Backend)
    backend.root, backend.needs_setup = root, False
    backend.courses = [type("Course", (), {"folder": name})() for name in (A, B)]
    monkeypatch.setattr(Backend, "extras", lambda self: [C], raising=False)
    refused = backend.recording_permission_many([A, "없는 과목"], "allowed")
    assert refused["ok"] is False and "없는 과목" in refused["error"] and permission.check(root, A)["state"] == "syllabus"
    done = backend.recording_permission_many([A, B, C], "allowed")
    assert done["ok"] and done["changed"] == 3
    data = json.loads((root / "_lecturehub" / "recording_permissions.json").read_text(encoding="utf-8"))
    assert data == {A: "allowed", B: "allowed", C: "allowed"}


# ---------------------------------------------------------------- the class-notice banner (D-075)
def _status(root, **value):
    folder = root / "_lecturehub" / "notices"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "status.json").write_text(json.dumps({"engine": "codex", "unavailable": None, "needs_consent": [],
                                                    "recording": False, **value}, ensure_ascii=False), encoding="utf-8")


@pytest.mark.parametrize("edition", ["public", "full"])
def test_NOTICE_ATTN_001_said_once_per_state_in_both_editions(tmp_path, monkeypatch, edition):
    """The full edition used to start on Google; now it starts on Codex. An install with the reading on but no
    codex_notices consent must not go quiet without the user knowing: the banner says it once per state."""
    monkeypatch.setenv("LECTUREHUB_EDITION", edition)
    assert cn.engine() == "codex"
    assert cn.attention(tmp_path) == {"show": False}  # no round yet
    _status(tmp_path, needs_consent=[B, A, A])
    first = cn.attention(tmp_path)
    assert first["show"] and first["needs_consent"] == [A, B] and first["unavailable"] is None
    assert cn.attention(tmp_path)["key"] == first["key"]  # the same state every round (10 minutes) is the same key
    assert cn.attention_seen(tmp_path, "an older key") is False and cn.attention(tmp_path)["show"]
    assert cn.attention_seen(tmp_path, first["key"]) is True and cn.attention(tmp_path)["show"] is False
    _status(tmp_path, needs_consent=[A, B, C])  # another course waits: said again
    again = cn.attention(tmp_path)
    assert again["show"] and again["key"] != first["key"]
    _status(tmp_path, unavailable="Codex(ChatGPT)가 이 PC에 없습니다(합성).")
    missing = cn.attention(tmp_path)
    assert missing["show"] and missing["unavailable"].startswith("Codex(ChatGPT)가 이 PC에 없습니다")


def test_NOTICE_ATTN_002_nothing_to_say_when_off_on_google_or_all_fine(tmp_path, monkeypatch):
    monkeypatch.setenv("LECTUREHUB_EDITION", "full")
    _status(tmp_path, needs_consent=[A])
    cn.set_enabled(False)
    assert cn.attention(tmp_path)["show"] is False  # switched off: nothing waits on the user
    cn.set_enabled(True)
    cn.set_engine("google")  # an explicit Google choice is honoured (never switched back silently) ...
    assert cn.engine() == "google" and cn.attention(tmp_path)["show"] is False  # ... and the Codex word is stale
    settings.save({"class_notices_engine": None})
    _status(tmp_path, engine="google", needs_consent=[A])
    assert cn.attention(tmp_path)["show"] is False  # the last round was another engine's
    _status(tmp_path, recording=True)
    assert cn.attention(tmp_path)["show"] is False  # held by a recording: not a problem to report


def test_NOTICE_ATTN_003_library_and_window_pass_it_through(tmp_path, monkeypatch):
    from lecturehub.ui.app import Api
    from lecturehub.ui.library import Library

    monkeypatch.setenv("LECTUREHUB_EDITION", "full")
    _status(tmp_path, needs_consent=[A])
    lib = Library(tmp_path)
    got = lib.notices_attention()
    assert got["ok"] and got["show"] and got["needs_consent"] == [A]
    made = Api(_Backend(), lib)
    assert made.notices_attention()["key"] == got["key"]
    assert made.notices_attention_seen(got["key"]) == {"ok": True, "changed": True}
    assert made.notices_attention()["show"] is False
