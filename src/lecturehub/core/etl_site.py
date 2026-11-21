"""Which Canvas site "eTL" means (user decision 2026-11-20: other schools that use Canvas should be able to connect).

SNU's eTL is Canvas LMS at ``myetl.snu.ac.kr``; that stays the default. A user at another school enters their
Canvas address once (settings ``etl_base``). The eTL token is bound to the site it was saved for
(``etl_token_host``): after the address changes, the old token is not used and never sent to the new host until
the user connects again. Keep this module free of sqlite3 / index imports (D-050).
"""
from __future__ import annotations

from urllib.parse import urlsplit

from lecturehub.core import settings

DEFAULT = "https://myetl.snu.ac.kr"


def clean(address: str) -> str:
    """``canvas.school.edu`` or ``https://canvas.school.edu/...`` → ``https://canvas.school.edu``. https only."""
    text = (address or "").strip()
    if not text:
        raise ValueError("학교 Canvas 주소를 넣어 주세요.")
    if "://" not in text:
        text = "https://" + text
    parts = urlsplit(text)
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("주소의 포트가 올바르지 않습니다.") from exc
    host = (parts.hostname or "").lower()
    if parts.scheme != "https":
        raise ValueError("https:// 로 시작하는 주소만 쓸 수 있습니다(토큰을 안전하게 보내기 위해).")
    if not host or "." not in host or parts.username or parts.password or any(c.isspace() for c in host):
        raise ValueError("주소 모양이 올바르지 않습니다. 예: canvas.school.ac.kr")
    return f"https://{host}" + (f":{port}" if port and port != 443 else "")


def base() -> str:
    """The Canvas site in use (settings ``etl_base``, else SNU's eTL)."""
    stored = settings.get("etl_base")
    if stored:
        try:
            return clean(str(stored))
        except ValueError:
            pass  # a hand-edited bad value falls back to the default instead of sending the token somewhere odd
    return DEFAULT


def host(address: str | None = None) -> str:
    parts = urlsplit(address or base())
    return (parts.hostname or "") + (f":{parts.port}" if parts.port and parts.port != 443 else "")


def is_default() -> bool:
    return base() == DEFAULT


def token_fits() -> bool:
    """The saved token was made for the site in use (tokens saved before 2026-11-20 belong to SNU's eTL)."""
    return (settings.get("etl_token_host") or host(DEFAULT)) == host()


def remember_token_site() -> None:
    settings.save({"etl_token_host": host()})


def set_base(address: str | None) -> dict:
    """Change the site (blank → SNU's eTL). Returns ``{"base", "default", "token_fits"}``."""
    value = clean(address) if (address or "").strip() else None
    settings.save({"etl_base": None if value in (None, DEFAULT) else value})
    return {"base": base(), "default": is_default(), "token_fits": token_fits()}


def name() -> str:
    """What the screen calls it: "eTL" for SNU, else the school's Canvas."""
    return "eTL" if is_default() else "Canvas"
