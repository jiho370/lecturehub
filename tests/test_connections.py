"""Account connections: keys are tested before they are kept, never shown, and features branch on what is present."""
import io
import sys
import types
import urllib.error

import pytest

from lecturehub.core import connections


@pytest.fixture(autouse=True)
def fake_keyring(monkeypatch):
    store = {}
    module = types.ModuleType("keyring")
    module.get_password = lambda service, name: store.get((service, name))
    module.set_password = lambda service, name, value: store.__setitem__((service, name), value)

    class PasswordDeleteError(Exception):
        pass

    def delete(service, name):
        if (service, name) not in store:
            raise PasswordDeleteError()
        del store[(service, name)]

    module.delete_password = delete
    module.errors = types.SimpleNamespace(PasswordDeleteError=PasswordDeleteError)
    monkeypatch.setitem(sys.modules, "keyring", module)
    for var in ("GEMINI_API_KEY", "GROQ_API_KEY", "LECTUREHUB_ETL_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    return store


class Reply(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def opener_returning(body=b'{"data": []}', status=200, seen=None):
    def opener(request, timeout=0):
        if seen is not None:
            seen.append(request)
        reply = Reply(body)
        reply.status = status
        return reply
    return opener


def opener_failing(code):
    def opener(request, timeout=0):
        raise urllib.error.HTTPError(request.full_url, code, "x", {}, None)
    return opener


def test_status_never_contains_the_key(fake_keyring):
    connections.save("gemini", "AIza-secret-value")
    rows = {r["id"]: r for r in connections.status()}
    assert rows["gemini"]["connected"] and rows["gemini"]["source"] == "keyring" and not rows["groq"]["connected"]
    assert "AIza-secret-value" not in repr(connections.status())


def test_connect_keeps_the_key_only_when_the_service_accepts_it(fake_keyring):
    bad = connections.connect("groq", "gsk_bad", opener=opener_failing(401))
    assert not bad["ok"] and "받지 않았습니다" in bad["error"] and bad["saved"] is False and not fake_keyring
    assert bad["kind"] == "rejected" and not bad["can_save_anyway"]
    seen = []
    good = connections.connect("groq", "gsk_good", opener=opener_returning(seen=seen))
    assert good["ok"] and fake_keyring[("lecturehub", "groq")] == "gsk_good"
    assert seen[0].full_url.startswith("https://api.groq.com/") and seen[0].get_header("Authorization") == "Bearer gsk_good"


def test_each_key_goes_only_to_its_own_host():
    for provider, host in (("gemini", "generativelanguage.googleapis.com"), ("groq", "api.groq.com"), ("etl", "myetl.snu.ac.kr")):
        seen = []
        assert connections.test(provider, "k", opener=opener_returning(seen=seen))["ok"]
        assert seen[0].full_url.split("/")[2] == host


def test_test_handles_network_and_server_errors():
    def down(request, timeout=0):
        raise urllib.error.URLError("no route")
    assert "닿지 못" in connections.test("gemini", "k", opener=down)["error"]
    assert "500" in connections.test("gemini", "k", opener=opener_failing(500))["error"]
    assert "예상과" in connections.test("gemini", "k", opener=opener_returning(body=b"<html>"))["error"]
    assert "연결된 키가 없습니다" in connections.test("gemini")["error"]


@pytest.mark.parametrize("key", ["", "   ", "x" * 401, '""'])
def test_save_rejects_odd_keys(key, fake_keyring):
    assert not connections.save("gemini", key)["ok"] and not fake_keyring


@pytest.mark.parametrize("pasted", ['"tok123"', "Bearer tok123", " tok\n123 ", "'tok123'\n"])
def test_pasted_keys_are_cleaned_before_the_check_and_the_save(pasted, fake_keyring):
    seen = []
    assert connections.connect("etl", pasted, opener=opener_returning(seen=seen))["saved"]
    assert seen[0].get_header("Authorization") == "Bearer tok123" and fake_keyring[("lecturehub", "etl-canvas")] == "tok123"


def test_etl_connect_says_clearly_what_happened(fake_keyring):
    rejected = connections.connect("etl", "tok", opener=opener_failing(401))
    assert rejected["saved"] is False and "eTL이 이 토큰을 받지 않았습니다" in rejected["error"] and not fake_keyring

    def down(request, timeout=0):
        raise urllib.error.URLError("no route")
    offline = connections.connect("etl", "tok", opener=down)
    assert offline["saved"] is False and offline["can_save_anyway"] and not fake_keyring
    assert connections.save_unchecked("etl", "tok")["saved"] and fake_keyring[("lecturehub", "etl-canvas")] == "tok"

    good = connections.connect("etl", "tok2", opener=opener_returning(body='{"id": 1, "name": "홍길동"}'.encode()))
    assert good["saved"] and good["account"] == "홍길동"


def test_remove_and_env_source(fake_keyring, monkeypatch):
    connections.save("gemini", "k1")
    monkeypatch.setenv("GEMINI_API_KEY", "from-env")
    assert connections.remove("gemini") == {"ok": True, "still_from_env": True}
    rows = {r["id"]: r for r in connections.status()}
    assert rows["gemini"]["source"] == "env"
    assert connections.remove("gemini")["ok"]  # removing again is not an error
    assert not connections.save("nope", "k")["ok"]


def tool_rows(**found):
    return {name: {"found": bool(found.get(name))} for name in ("aside", "claude", "codex", "ffmpeg", "gcloud")}


def feature(rows, keys, wanted):
    return next(f for f in connections.features(rows, keys) if f["id"] == wanted)


def test_gsr_prefers_aside_then_claude_in_chrome_then_says_what_is_missing():
    keys = connections.status()
    assert feature(tool_rows(aside=1, claude=1), keys, "gsr")["via"] == "aside"
    fallback = feature(tool_rows(claude=1), keys, "gsr")
    assert fallback["state"] == "fallback" and fallback["via"] == "chrome"
    missing = feature(tool_rows(), keys, "gsr")
    assert missing["state"] == "missing" and "Aside" in missing["hint"]


def test_clova_needs_aside_and_otherwise_points_to_manual_import():
    keys = connections.status()
    assert feature(tool_rows(aside=1), keys, "clova")["state"] == "ready"
    no = feature(tool_rows(claude=1), keys, "clova")
    assert no["state"] == "missing" and "가져오기" in no["hint"]


def test_features_follow_connected_keys_and_ai_tools(fake_keyring):
    connections.save("groq", "k")
    connections.save("etl", "t")
    keys = connections.status()
    assert feature(tool_rows(), keys, "transcribe")["via"] == "groq"
    assert feature(tool_rows(), keys, "etl")["state"] == "ready"
    assert feature(tool_rows(), keys, "captions")["state"] == "missing"
    assert feature(tool_rows(codex=1, claude=1), keys, "notes")["via"] == "codex"
    assert feature(tool_rows(), keys, "notes")["state"] == "missing"
