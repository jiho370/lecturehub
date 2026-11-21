"""Account connections for the settings screen: keys, tokens, outside tools, and what each feature needs.

A key is stored only in the Windows credential store (keyring) and never goes back to the window: the UI sees
``connected`` and where it came from. The "test" for each key calls that service's own harmless list endpoint on
its own host; no lecture data is sent. Keep this module free of sqlite3 / index imports (D-050).
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from lecturehub.core import etl_site, tools

KEYRING_SERVICE = "lecturehub"

# id -> label, keyring name, env var, where to get it, test request (url, header builder)
PROVIDERS: dict[str, dict] = {
    "gemini": {
        "label": "Google Gemini (전사·실시간 자막 보조)", "keyring": "gemini", "env": "GEMINI_API_KEY",
        "help_url": "https://aistudio.google.com/app/apikey",
        "steps": "Google AI Studio에서 'Create API key'를 눌러 만든 키를 붙여 넣으세요.",
        "test_url": "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1",
        "test_header": lambda key: {"x-goog-api-key": key},
        "note": "무료 등급은 입력 내용이 모델 개선에 쓰일 수 있습니다. 개인 정보가 든 녹음은 유료 등급을 쓰세요.",
    },
    "groq": {
        "label": "Groq Whisper (전사)", "keyring": "groq", "env": "GROQ_API_KEY",
        "help_url": "https://console.groq.com/keys",
        "steps": "Groq 콘솔에서 'Create API Key'를 눌러 만든 키를 붙여 넣으세요.",
        "test_url": "https://api.groq.com/openai/v1/models",
        "test_header": lambda key: {"Authorization": f"Bearer {key}"},
        "note": "녹음 조각이 Groq로 전송됩니다. 과목별 동의가 있어야 보냅니다.",
    },
    "etl": {
        "label": "서울대 eTL (공지·과제·자료)", "keyring": "etl-canvas", "env": "LECTUREHUB_ETL_TOKEN",
        "help_url": "https://myetl.snu.ac.kr/profile/settings",
        "steps": "eTL → 계정 → 설정 → '새 액세스 토큰'에서 용도에 Lecture Hub를 적고 만든 토큰을 붙여 넣으세요.",
        "test_url": "https://myetl.snu.ac.kr/api/v1/users/self",
        "test_header": lambda key: {"Authorization": f"Bearer {key}"},
        "note": "읽기만 합니다. 토큰은 eTL 주소로만 보냅니다.",
    },
}


def meta_for(provider: str) -> dict:
    """``PROVIDERS[provider]`` with the eTL row pointed at the Canvas site in use (another school's, 2026-11-20)."""
    meta = PROVIDERS[provider]
    if provider != "etl" or etl_site.is_default():
        return meta
    site = etl_site.base()
    return {**meta, "label": f"학교 Canvas ({etl_site.host()})", "help_url": f"{site}/profile/settings",
            "steps": "학교 Canvas → 계정 → 설정 → '새 액세스 토큰'에서 용도에 Lecture Hub를 적고 만든 토큰을 붙여 넣으세요.",
            "test_url": f"{site}/api/v1/users/self", "note": f"읽기만 합니다. 토큰은 {etl_site.host()}로만 보냅니다."}


def _stored(provider: str) -> str | None:
    meta = PROVIDERS[provider]
    try:
        import keyring

        value = keyring.get_password(KEYRING_SERVICE, meta["keyring"])
        if value and (provider != "etl" or etl_site.token_fits()):  # a token made for another school's site stays unused
            return value
    except Exception:  # noqa: BLE001 - a broken credential store counts as "not stored"
        pass
    return None


def _from_env(provider: str) -> str | None:
    return os.environ.get(PROVIDERS[provider]["env"]) or None


def key_for(provider: str) -> str | None:
    return _stored(provider) or _from_env(provider)


def status() -> list[dict]:
    """One row per key: id, label, steps, help_url, note, ``connected`` and ``source`` (keyring/env/None)."""
    rows = []
    for pid in PROVIDERS:
        meta = meta_for(pid)
        source = "keyring" if _stored(pid) else "env" if _from_env(pid) else None
        row = {"id": pid, "label": meta["label"], "steps": meta["steps"], "help_url": meta["help_url"],
               "note": meta["note"], "connected": source is not None, "source": source}
        if pid == "etl":
            row["site"] = {"base": etl_site.base(), "default": etl_site.is_default()}
        rows.append(row)
    return rows


def clean_key(key: str) -> str:
    """What people paste: a copied line with quotes, a 'Bearer ' prefix or a line break in the middle."""
    key = (key or "").strip().strip("\"'").strip()
    if key.lower().startswith("bearer "):
        key = key[7:]
    return "".join(key.split())


REJECTED = {
    "etl": "eTL이 이 토큰을 받지 않았습니다. 토큰을 만든 직후 한 번만 보이는 긴 글자를 끝까지 복사했는지, "
           "만료일이 지나지 않았는지 확인하세요. eTL 설정의 '승인된 통합'에서 새로 만들어 붙여 넣으면 됩니다.",
}


def save(provider: str, key: str) -> dict:
    if provider not in PROVIDERS:
        return {"ok": False, "error": "알 수 없는 연결입니다."}
    key = clean_key(key)
    if not key or any(ch.isspace() for ch in key) or len(key) > 400:
        return {"ok": False, "error": "키 모양이 올바르지 않습니다. 공백 없이 한 줄로 붙여 넣으세요."}
    try:
        import keyring

        keyring.set_password(KEYRING_SERVICE, PROVIDERS[provider]["keyring"], key)
    except Exception as exc:  # noqa: BLE001 - shown to the user
        return {"ok": False, "error": f"자격 증명 관리자에 저장하지 못했습니다({type(exc).__name__})"}
    if provider == "etl":
        etl_site.remember_token_site()
    return {"ok": True}


def remove(provider: str) -> dict:
    if provider not in PROVIDERS:
        return {"ok": False, "error": "알 수 없는 연결입니다."}
    try:
        import keyring

        try:
            keyring.delete_password(KEYRING_SERVICE, PROVIDERS[provider]["keyring"])
        except keyring.errors.PasswordDeleteError:
            pass
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"지우지 못했습니다({type(exc).__name__})"}
    return {"ok": True, "still_from_env": _from_env(provider) is not None}


def test(provider: str, key: str | None = None, *, opener=None, timeout: float = 10.0) -> dict:
    """Ask the service's own list endpoint whether ``key`` (or the stored one) is accepted. Sends no lecture data."""
    if provider not in PROVIDERS:
        return {"ok": False, "error": "알 수 없는 연결입니다."}
    meta = meta_for(provider)
    key = clean_key(key or "") or key_for(provider)
    if not key:
        return {"ok": False, "kind": "missing", "error": "연결된 키가 없습니다."}
    request = urllib.request.Request(meta["test_url"], headers={"Accept": "application/json", **meta["test_header"](key)})
    try:
        with (opener or urllib.request.urlopen)(request, timeout=timeout) as response:
            code = getattr(response, "status", 200)
            if code != 200:
                return {"ok": False, "kind": "service", "error": f"서비스가 {code}로 답했습니다. 잠시 뒤 다시 해 보세요."}
            try:
                body = json.loads(response.read(200_000).decode("utf-8", "replace") or "null")
            except ValueError:
                return {"ok": False, "kind": "service", "error": "예상과 다른 답이 왔습니다. 잠시 뒤 다시 해 보세요."}
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return {"ok": False, "kind": "rejected",
                    "error": REJECTED.get(provider, "서비스가 이 키를 받지 않았습니다. 키를 끝까지 복사했는지 확인하고, 안 되면 새로 만들어 붙여 넣으세요.")}
        return {"ok": False, "kind": "service", "error": f"서비스가 {exc.code}로 답했습니다. 잠시 뒤 다시 해 보세요."}
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return {"ok": False, "kind": "network", "error": f"서비스에 닿지 못했습니다({type(exc).__name__}). 인터넷 연결을 확인하세요."}
    out = {"ok": True}
    if provider == "etl" and isinstance(body, dict) and body.get("name"):
        out["account"] = str(body["name"])[:40]  # shown once on screen so the user sees which account; never stored
    return out


def connect(provider: str, key: str, *, opener=None) -> dict:
    """Test ``key`` and keep it only when the service accepts it. ``saved`` says plainly whether it was kept."""
    if provider in PROVIDERS and not clean_key(key):
        return {"ok": False, "saved": False, "kind": "format", "error": "붙여 넣은 키가 비어 있습니다."}
    checked = test(provider, key, opener=opener)
    if not checked["ok"]:
        return {**checked, "saved": False, "can_save_anyway": checked.get("kind") in ("network", "service")}
    saved = save(provider, key)
    return {**checked, **saved, "saved": bool(saved.get("ok"))}


def save_unchecked(provider: str, key: str) -> dict:
    """Keep a key the service could not be asked about (offline). The next sync tells whether it works."""
    saved = save(provider, key)
    return {**saved, "saved": bool(saved.get("ok"))}


# ---------- what each feature needs

def features(tool_rows: dict[str, dict] | None = None, keys: list[dict] | None = None) -> list[dict]:
    """Per feature: ``state`` is "ready", "fallback" (works another way) or "missing", with a short ``hint``."""
    rows = tool_rows if tool_rows is not None else tools.status()
    connected = {k["id"]: k["connected"] for k in (keys if keys is not None else status())}
    have = lambda name: bool(rows.get(name, {}).get("found"))  # noqa: E731
    out = []

    if have("aside"):
        gsr = {"state": "ready", "via": "aside", "hint": "Aside로 예약합니다."}
    elif have("claude"):
        gsr = {"state": "fallback", "via": "chrome", "hint": "Aside가 없어 Claude in Chrome으로 예약합니다(조금 느립니다)."}
    else:
        gsr = {"state": "missing", "via": None, "hint": "Aside 또는 Claude Code(+ Chrome 확장)를 연결하면 쓸 수 있어요."}
    out.append({"id": "gsr", "label": "그스룸 예약", **gsr})

    out.append({"id": "clova", "label": "Clova 가져오기", **(
        {"state": "ready", "via": "aside", "hint": "Aside로 가져옵니다(선택 기능)."} if have("aside") else
        {"state": "missing", "via": None, "hint": "Aside를 연결하거나, Clova에서 내보낸 파일을 '가져오기'로 넣으세요."})})

    out.append({"id": "etl", "label": "eTL 공지·과제", **(
        {"state": "ready", "via": "token", "hint": "연결됨."} if connected.get("etl") else
        {"state": "missing", "via": None, "hint": "eTL 토큰을 연결하면 공지와 과제가 들어옵니다."})})

    transcribe = [name for name in ("gemini", "groq") if connected.get(name)]
    out.append({"id": "transcribe", "label": "녹음 전사(글로 바꾸기)", **(
        {"state": "ready", "via": ",".join(transcribe), "hint": "연결됨: " + ", ".join(transcribe)} if transcribe else
        {"state": "missing", "via": None, "hint": "Gemini 또는 Groq 키를 연결하면 녹음을 글로 바꿀 수 있어요."})})

    out.append({"id": "captions", "label": "실시간 자막", **(
        {"state": "ready", "via": "gemini", "hint": "Gemini 키로 켜집니다(선택 기능)."} if connected.get("gemini") else
        {"state": "missing", "via": None, "hint": "Gemini 키가 필요합니다(선택 기능)."})})

    from lecturehub.ai import selected

    try:
        ai = selected("notes")
    except ValueError:
        ai = "off"
    ready = ai != "off" and have(ai)
    out.append({"id": "notes", "label": "정리본(AI)", "state": "ready" if ready else "missing",
                "via": ai if ready else None,
                "hint": f"선택한 {ai} 프로그램이 있습니다. 로그인·사용 한도는 별도 확인하세요." if ready else
                "AI 사용이 꺼져 있습니다." if ai == "off" else f"선택한 {ai} 프로그램을 설치하고 로그인하세요."})
    return out
