"""[모두 선택]/[모두 해제] of the per-course cards (AI app link, class notices through Codex), user 2026-11-25.

One native dialog names every course; the consent is stored per course, for those courses only, and only with the
one-time token of that dialog. It is not a standing consent.
"""
from __future__ import annotations

import json

import pytest

from lecturehub.transcription.consent import ConsentStore

A, B, C = "합성과목 가", "합성과목 나", "합성과목 다"


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
    """Records what the Api lets through; ``Api`` is the gate under test."""

    recorder = None

    def __init__(self):
        self.link, self.notices, self.revoked = [], [], []

    def state(self):
        return {"state": "idle"}

    def ai_link_allow_many(self, courses):
        self.link.append(list(courses))
        return {"ok": True}

    def notices_allow_many(self, courses):
        self.notices.append(list(courses))
        return {"ok": True}

    def consent_revoke_many(self, provider, courses):
        self.revoked.append((provider, list(courses)))
        return {"ok": True, "revoked": len(courses)}


@pytest.fixture
def api(tmp_path):
    from lecturehub.ui.app import Api

    backend = _Backend()
    made = Api(backend, _Library(tmp_path))
    made._main = _Window(True)
    return made, backend


def test_MANY_001_one_dialog_names_every_course_and_its_token_stores_exactly_those(api):
    made, backend = api
    q = made.consent_confirm_many("ai_app_link", [A, B])
    title, message = made._main.asked[-1]
    assert q["yes"] and "과목 2개" in title and f"· {A}" in message and f"· {B}" in message
    assert "나중에 생기는 과목은 그때 다시 묻습니다" in message and "녹음 파일(소리)은 건네지 않습니다" in message
    assert made.ai_link_allow_all([B, A], q["token"]) == {"ok": True} and backend.link == [[B, A]]  # order is free
    assert made.ai_link_allow_all([A, B], q["token"])["ok"] is False and len(backend.link) == 1  # one time only


def test_MANY_002_the_token_is_refused_for_anything_but_the_named_courses_and_provider(api):
    made, backend = api
    assert made.ai_link_allow_all([A, B])["needs_confirm"] and backend.link == []  # straight from page JS
    assert made.ai_link_allow_all([A, B], "made-up")["ok"] is False
    for wrong in ([A, B, C], [A], [A, C]):  # one more, one fewer, another course
        q = made.consent_confirm_many("ai_app_link", [A, B])
        assert made.ai_link_allow_all(wrong, q["token"])["ok"] is False
    q = made.consent_confirm_many("ai_app_link", [A, B])
    assert made.notices_allow_all([A, B], q["token"])["ok"] is False  # another provider's card
    q = made.consent_confirm_many("ai_app_link", [A, B])
    assert made.ai_link_allow(A, q["token"])["ok"] is False  # not a single-course token
    single = made.consent_confirm("ai_app_link", A)["token"]
    assert made.ai_link_allow_all([A], single)["ok"] is False  # and a single-course token is not a list token
    assert backend.link == [] and backend.notices == []


def test_MANY_003_a_single_course_dialog_cannot_mint_a_list_token(api):
    made, _ = api
    forged = "\x00many\x00" + "\x00".join(sorted([A, B]))  # the key shape of a list token, passed as a course name
    assert made.consent_confirm("ai_app_link", forged)["ok"] is False and made._main.asked == []
    assert made.consent_confirm_many("ai_app_link", [A, "나\x00쁜"])["ok"] is False and made._main.asked == []


def test_MANY_004_only_the_two_per_course_text_cards_take_it_and_only_named_courses(api):
    made, _ = api
    for provider in ("gemini_transcribe", "whisper_groq", "google_live", "codex_note", "claude_note", "", "nope"):
        assert made.consent_confirm_many(provider, [A, B])["ok"] is False
    for courses in ([], None, "가", [A, 3], [A, ""], ["미분류"], [A, "*"], [A] * 300 + [B] * 300):
        assert made.consent_confirm_many("ai_app_link", courses)["ok"] is False
    assert made._main.asked == []
    made._main = _Window(False)
    assert made.consent_confirm_many("codex_notices", [A, B]) == {"ok": True, "yes": False}
    made._main = None
    assert made.consent_confirm_many("codex_notices", [A, B])["ok"] is False


def test_MANY_005_notices_card_and_revoke_many(api):
    made, backend = api
    q = made.consent_confirm_many("codex_notices", [A, B, A])  # a repeated name counts once
    assert "과목 2개" in made._main.asked[-1][0] and "Codex(OpenAI)" in made._main.asked[-1][1]
    assert made.notices_allow_all([A, B], q["token"]) == {"ok": True} and backend.notices == [[A, B]]
    assert made.consent_revoke_many("ai_app_link", [A, B])["revoked"] == 2 and backend.revoked == [("ai_app_link", [A, B])]
    assert made.consent_revoke_many("gemini_transcribe", [A])["ok"] is False  # other consents: one by one, in the list
    assert made.consent_revoke_many("ai_app_link", [])["ok"] is False


def test_MANY_006_backend_stores_per_course_all_or_nothing_and_never_a_standing_consent(tmp_path, monkeypatch):
    from lecturehub.core import timetable
    from lecturehub.ui.backend import Backend

    monkeypatch.setenv("LECTUREHUB_SETTINGS", str(tmp_path / "settings.json"))
    monkeypatch.setenv("LECTUREHUB_RECORDER_CONFIG", str(tmp_path / "recorder.toml"))
    root = tmp_path / "학기"
    (root / "_lecturehub").mkdir(parents=True)
    backend = Backend.__new__(Backend)
    backend.root, backend.needs_setup = root, False
    backend.courses = [type("Course", (), {"folder": timetable.folder_name(name)})() for name in (A, B)]
    monkeypatch.setattr(Backend, "extras", lambda self: [], raising=False)
    monkeypatch.setattr(Backend, "ai_link_status", lambda self: {}, raising=False)
    store = ConsentStore(root / "_lecturehub" / "consent.json")

    assert backend.ai_link_allow_many([A, "없는 과목"])["ok"] is False  # one unknown name: nothing is stored
    assert not store.granted("ai_app_link", A)
    assert backend.ai_link_allow_many([A, B])["ok"] is True
    data = json.loads((root / "_lecturehub" / "consent.json").read_text(encoding="utf-8"))
    assert set(data["ai_app_link"]) == {A, B} and "*" not in data["ai_app_link"]  # per course, no standing entry
    assert all(entry["authority"] == "confirmed_by_user" for entry in data["ai_app_link"].values())
    assert store.granted("ai_app_link", A) and not store.granted("ai_app_link", C)  # a later course is not covered
    assert not store.granted("codex_notices", A)  # one card's consent is not the other's
    assert backend.notices_allow_many([A])["ok"] is True and store.granted("codex_notices", A)
    done = backend.consent_revoke_many("ai_app_link", [A, B, C])
    assert done["revoked"] == 2 and not store.granted("ai_app_link", A) and store.granted("codex_notices", A)
