"""그스룸 예약 파일: ``_lecturehub\\gsr\\`` 안에서만 읽고 쓴다(AGENTS 규칙 1). DB는 쓰지 않는다.

- ``rules.json``  정기 예약 규칙 + 앱 자동반납 켜기/끄기 (앱이 쓰고, 예약 작업이 읽는다)
- ``status.json`` 마지막으로 받아 온 내 예약 목록 (앱과 예약 작업이 둘 다 쓴다)
- ``log.jsonl``   작업 기록 한 줄 = JSON 하나 (추가만 한다)
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime
from pathlib import Path

from lecturehub.core.paths import app_dir

WEEKDAYS = "월화수목금토일"
USES = ("과제", "강의", "동아리활동", "연구", "취업", "기타")
STEP_MIN = 30
# 사이트의 운영 시각을 아직 못 읽었을 때의 넓은 한계. 방 목록(OPENTIME·CLOSETIME)을 읽으면 status.json의 site_hours가 대신한다.
# 한계를 넘는 시각은 사이트의 빈 방 목록에 없어 "빈 방이 없습니다"로 끝난다.
DEFAULT_OPEN_MIN, DEFAULT_CLOSE_MIN = 6 * 60, 24 * 60
# 길이 30분 단위 30분~12시간. 사이트는 한 번에 3시간까지라 3시간을 넘으면 3시간 블록으로 이어서 예약한다(마지막 블록만 짧을 수 있다).
HOURS = tuple(m // 60 if m % 60 == 0 else m / 60 for m in range(30, 12 * 60 + 1, 30))
# 4층 6인실 중 TV 모니터가 있는 방. 근거: 이전 예약 스크립트의 TV 방 목록 — 사이트 방 정보로는
# 확인하지 못했다. 2층 방은 모르므로 순서를 바꾸지 않는다. gsr_core.js의 CFG.monitorRooms와 같아야 한다(테스트가 비교).
MONITOR_ROOMS = ("G405", "G407", "G411", "G413")
_TIME = re.compile(r"^([01]\d|2[0-3]):(00|30)$")


def public_mode() -> bool:
    from lecturehub.core import edition

    return edition.name() == "public"


def folder(root: Path) -> Path:
    return app_dir(root) / "gsr"


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default


def to_min(text: str) -> int:
    return int(text[:2]) * 60 + int(text[3:5])


def hm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def valid_start(start: str, open_min: int = DEFAULT_OPEN_MIN, close_min: int = DEFAULT_CLOSE_MIN) -> bool:
    return bool(_TIME.match(start or "")) and open_min <= to_min(start) <= close_min - STEP_MIN


def start_error(open_min: int = DEFAULT_OPEN_MIN, close_min: int = DEFAULT_CLOSE_MIN) -> str:
    return f"시작은 {hm(open_min)}~{hm(close_min - STEP_MIN)}, 30분 단위입니다"


def clean_hours(hours):
    """길이(시간): 0.5 단위로 0.5~12. 정수면 int, 아니면 float."""
    try:
        value = float(3 if hours in (None, "") else hours)
    except (TypeError, ValueError):
        value = 0.0
    limit = 3 if public_mode() else 12
    if not math.isfinite(value) or not 0.5 <= value <= limit or value * 2 != int(value * 2):
        raise ValueError(f"길이는 30분 단위로 30분~{limit}시간입니다")
    return int(value) if value == int(value) else value


def hours_text(hours) -> str:
    minutes = round(float(hours) * 60)
    if minutes % 60 == 0:
        return f"{minutes // 60}시간"
    return f"{minutes}분" if minutes < 60 else f"{minutes // 60}시간 {minutes % 60}분"


def fits_day(start: str, hours, close_min: int = DEFAULT_CLOSE_MIN) -> bool:
    """start(HH:MM)부터 hours시간이 운영 시간(닫는 시각) 안에 들어가나."""
    return to_min(start) + round(float(hours) * 60) <= close_min


def clean_site_hours(value) -> dict | None:
    """코어가 방 목록에서 읽은 ['0900', '2300'] → {'open': '09:00', 'close': '23:00'}. 이상하면 None."""
    try:
        opened, closed = (str(v) for v in value)
        lo, hi = int(opened[:2]) * 60 + int(opened[2:4]), int(closed[:2]) * 60 + int(closed[2:4])
    except (TypeError, ValueError):
        return None
    if len(opened) != 4 or len(closed) != 4 or not 0 <= lo < hi <= 24 * 60:
        return None
    return {"open": hm(lo), "close": hm(hi)}


def site_bounds(root: Path) -> tuple[int, int]:
    """예약할 수 있는 시작·끝의 분 한계: 사이트가 알려 준 운영 시각, 아직 모르면 넓은 기본값."""
    seen = load_status(root).get("site_hours")
    try:
        return to_min(seen["open"]), to_min(seen["close"])
    except (TypeError, KeyError, ValueError):
        return DEFAULT_OPEN_MIN, DEFAULT_CLOSE_MIN


# ------------------------------------------------------------------ rules
def load_settings(root: Path) -> dict:
    data = _read_json(folder(root) / "rules.json", {})
    rules = [r for r in data.get("rules", []) if isinstance(r, dict)] if isinstance(data, dict) else []
    auto = data.get("auto_return", not public_mode()) if isinstance(data, dict) else not public_mode()
    return {"rules": [] if public_mode() else rules, "auto_return": bool(auto)}


def clean_rule(rule: dict, open_min: int = DEFAULT_OPEN_MIN, close_min: int = DEFAULT_CLOSE_MIN) -> dict:
    weekday = str(rule.get("weekday", "")).strip()
    start = str(rule.get("start", "")).strip()
    use = str(rule.get("use", "과제")).strip() or "과제"
    if weekday not in WEEKDAYS or len(weekday) != 1:
        raise ValueError("요일은 월~일 중 하나입니다")
    if not valid_start(start, open_min, close_min):
        raise ValueError(start_error(open_min, close_min))
    if use not in USES:
        raise ValueError("용도는 " + "·".join(USES) + " 중 하나입니다")
    hours = clean_hours(rule.get("hours"))
    if not fits_day(start, hours, close_min):
        raise ValueError(f"{start}부터 {hours_text(hours)}은 {hm(close_min)}을 넘습니다")
    return {"weekday": weekday, "start": start, "use": use, "hours": hours, "enabled": bool(rule.get("enabled", True)),
            "memo": str(rule.get("memo", ""))[:60]}


def save_settings(root: Path, rules: list[dict], auto_return: bool) -> dict:
    if public_mode() and rules:
        raise ValueError("공개판은 정기·자동 예약을 지원하지 않습니다. 예약마다 직접 확인해 주세요.")
    open_min, close_min = site_bounds(root)
    cleaned = [clean_rule(r, open_min, close_min) for r in rules]
    keys = [(r["weekday"], r["start"]) for r in cleaned]
    if len(keys) != len(set(keys)):
        raise ValueError("같은 요일·시작 규칙이 두 번 있습니다")
    data = {"rules": cleaned, "auto_return": bool(auto_return),
            "note": "공개판: 예약마다 사용자 확인, 최대 3시간" if public_mode() else
                    "정기 예약: 예약 작업이 매일 오늘+3일 요일에 맞는 enabled 규칙을 hours시간(3시간 블록 연속) 예약한다"}
    _write_json(folder(root) / "rules.json", data)
    return {"rules": cleaned, "auto_return": bool(auto_return)}


# ------------------------------------------------------------------ status
def load_status(root: Path) -> dict:
    data = _read_json(folder(root) / "status.json", {})
    if not isinstance(data, dict):
        data = {}
    return {"updated_at": data.get("updated_at"), "by": data.get("by"),
            "bookings": [b for b in data.get("bookings", []) if isinstance(b, dict)],
            "site_hours": data.get("site_hours") if isinstance(data.get("site_hours"), dict) else None}


def save_status(root: Path, bookings: list[dict], by: str, now: datetime, site_hours: dict | None = None) -> None:
    """내 예약 목록(+사이트 운영 시각: 새로 읽은 값이 없으면 지난 값을 그대로 둔다)."""
    data = {"updated_at": now.isoformat(timespec="seconds"), "by": by, "bookings": bookings,
            "site_hours": site_hours or load_status(root).get("site_hours")}
    _write_json(folder(root) / "status.json", data)


# ------------------------------------------------------------------ log
def append_log(root: Path, entry: dict) -> None:
    path = folder(root) / "log.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_log(root: Path, limit: int | None = None) -> list[dict]:
    try:
        lines = (folder(root) / "log.jsonl").read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out[-limit:] if limit else out


def cancelled_rows(entry: dict) -> list[list]:
    """cancel 기록이 담은 취소된 예약 행 [방, 'YYYY-MM-DD HH:MM', 끝, id]."""
    rows = entry.get("cancelled") if entry.get("kind") == "cancel" else None
    return [r for r in rows if isinstance(r, list) and len(r) >= 4] if isinstance(rows, list) else []


def return_attempted(root: Path, booking_id: str) -> bool:
    """누가(앱·예약 작업) 이미 이 예약의 반납을 시도했나 — 두 쪽이 같은 반납을 두 번 돌리지 않게.
    취소한 예약도 같다(반납할 것이 없다)."""
    wanted = str(booking_id)
    return any((e.get("kind") == "return" and str(e.get("id")) == wanted and (e.get("ok") or e.get("skipped")))
               or any(str(r[3]) == wanted for r in cancelled_rows(e)) for e in read_log(root))
