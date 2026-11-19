"""CONSENT-002: public consent is bound to one provider and one named course."""
import json

import pytest

from lecturehub.transcription.consent import CONFIRMED, NOTICES, SYNTHETIC, ConsentStore, ensure_consent


@pytest.fixture(autouse=True)
def public(monkeypatch):
    monkeypatch.setenv("LECTUREHUB_EDITION", "public")


def test_CONSENT_002_old_wildcard_never_authorizes_public_upload(tmp_path):
    path = tmp_path / "동의 (합성).json"
    raw = json.dumps({"gemini_transcribe": {"*": {"authority": CONFIRMED}}})
    path.write_text(raw, encoding="utf-8")
    store = ConsentStore(path)
    assert not store.granted("gemini_transcribe", "합성 과목")
    assert not store.granted("gemini_transcribe", "*")
    asked = []
    assert not ensure_consent(store, "gemini_transcribe", "합성 과목", lambda msg: asked.append(msg) or False, 0)
    assert asked and "Google" in asked[0]
    assert path.read_text(encoding="utf-8") == raw
    assert ensure_consent(store, "gemini_transcribe", "합성 과목", lambda _: True, 1)
    assert store.granted("gemini_transcribe", "합성 과목")
    assert not store.granted("whisper_groq", "합성 과목")
    assert not store.granted("gemini_transcribe", "다른 합성 과목")


def test_CONSENT_002_no_public_standing_grant_or_synthetic_authority(tmp_path):
    store = ConsentStore(tmp_path / "consent.json")
    with pytest.raises(ValueError):
        store.grant_all("codex_note", 1, "user", "synthetic test")
    with pytest.raises(ValueError):
        store.grant("codex_note", "*", 1, "notice")
    assert not store.path.exists()
    store.grant("codex_note", "합성 과목", 1, "notice", authority=SYNTHETIC)
    assert not store.granted("codex_note", "합성 과목")
    assert store.granted("codex_note", "합성 과목", allow_synthetic=True)


def test_CONSENT_002_public_cli_cannot_write_wildcard(tmp_path):
    from typer.testing import CliRunner
    from lecturehub.cli import app, _register_all

    _register_all()

    result = CliRunner().invoke(app, ["transcribe", "consent", "grant-all", "--provider", "google",
                                     "--by", "user", "--reason", "synthetic", "--root", str(tmp_path)])
    assert result.exit_code != 0
    assert not (tmp_path / "_lecturehub" / "consent.json").exists()


@pytest.mark.parametrize("provider,company", [("gemini_transcribe", "Google"), ("whisper_groq", "Groq"),
    ("google_live", "Google"), ("codex_note", "OpenAI"), ("claude_note", "Anthropic")])
def test_CONSENT_002_provider_notice_names_recipient_and_policy(provider, company):
    assert company in NOTICES[provider]
    assert "https://" in NOTICES[provider]
    assert "개인정보" in NOTICES[provider]
